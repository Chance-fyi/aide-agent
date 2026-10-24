"""核心对话处理。

处理顺序（短路优先级）：
1. 输入安检命中 → 友好拒答，不进 graph（省一次 LLM 调用，不写污染状态）
2. 已转接（排队中/服务中） → 消息直达人工工作台，不进 graph
3. 其他 → 正常走 Agent

本模块被两条入口共用：
- `POST /chat`（REST：脚本/调试/WS 不可用时的兜底）
- `WS /ws/chat`（前端主链路：回复走推送）
因此业务编排收在 `process_message` 里，两个入口只做各自的协议适配——
否则"转接守卫""落库""推送"这些规则会在两处各写一遍，迟早走偏。
"""

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass

import openai
from fastapi import APIRouter, HTTPException, Path, Request
from langchain_core.messages import AIMessage, HumanMessage

from app.agents import handoff
from app.agents.runtime import get_graph
from app.api.deps import CurrentUser, OptionalUser
from app.api.schemas import (
    ChatRequest,
    ChatResponse,
    SessionHistoryResponse,
    SessionListResponse,
    SessionMessage,
    SessionSummary,
)
from app.core import guardrails, ratelimit, tracing
from app.core.config import get_settings
from app.core.hub import hub

logger = logging.getLogger(__name__)

# ---- 会话记录角色枚举（chat_message.role 取值，与前端展示约定一致）----
# 智能客服与人工坐席分立角色，双端才能按角色区分展示气泡
ROLE_USER = "user"      # 用户消息
ROLE_AGENT = "agent"    # 智能客服回复
ROLE_HUMAN = "human"    # 人工坐席回复

# 已转接状态下用户继续发消息时的应答话术
# （仅排队中提示一次，不进 LLM，消息本身直达坐席工作台）
_QUEUED_ACK = "您的消息已收到，人工客服会尽快回复您，请稍候。"

# thread_id 格式约束：服务端生成的是 UUID，历史接口按此校验。
# 参数化 SQL 本身没有注入风险，这里限制字符集与长度属于纵深防御，
# 也避免奇形怪状的 ID 进 Checkpointer 与业务表
THREAD_ID_PATTERN = r"^[A-Za-z0-9_\-]{1,64}$"

router = APIRouter(tags=["chat"])

@dataclass
class ChatOutcome:
    """一次对话处理的结果（与传输层无关的中间结构）。

    为什么不让内部逻辑直接返回 ChatResponse：WS 入口不需要 thread_id
    之外的 HTTP 语义（也不该抛 HTTP 异常），它要的是"回复内容 + 当前状态"。
    """

    thread_id: str
    reply: str          # 要展示给用户的文本；空串 = 无需气泡（坐席已接入）
    status: str         # 处理后的会话状态
    transferred: bool   # 是否处于已转接（排队中/服务中）
    # 落库后的权威消息标识。流式回复收尾时前端要拿它给占位气泡"转正"，
    # 之后重连/重载才能按 id 与历史对齐（否则同一条会出现两次）
    reply_id: int | None = None
    reply_ts: str | None = None

def _friendly_llm_error(exc: Exception) -> tuple[int, str]:
    """把 LLM 链路的异常翻译成（HTTP 状态码, 用户可读文案）。

    原则：对外只说"发生了什么类别的问题 + 该怎么办"，
    绝不泄露 base_url / 模型名 / 堆栈等内部细节；细节进日志。
    """
    # 注意继承关系：APITimeoutError 是 APIConnectionError 的子类，必须先判
    if isinstance(exc, openai.APITimeoutError):
        return 503, "AI 服务响应超时，请稍后重试"
    if isinstance(exc, openai.APIConnectionError):
        return 503, "暂时无法连接 AI 服务，请稍后重试"
    if isinstance(exc, openai.RateLimitError):
        return 503, "当前咨询人数较多，请稍后重试"
    if isinstance(exc, openai.AuthenticationError):
        # 配置错误（Key 失效）属于服务端问题，对用户话术与普通故障一致，
        # 但日志要打 CRITICAL 提醒运维，这是"必须马上修"的故障
        logger.critical("LLM 认证失败，请检查 LLM_API_KEY 配置")
        return 503, "客服服务暂时不可用，请稍后重试"
    return 503, "客服服务暂时不可用，请稍后重试"

def _get_thread_status(thread_id: str) -> str:
    """查询会话转接状态，查询失败降级为 active。

    降级理由：状态查询失败不应拖垮对话（Postgres 抖动时用户还能
    继续自助问答）；代价是极端情况下已转接会话多走一轮 LLM，可接受。
    """
    try:
        return handoff.get_status(thread_id)
    except Exception:
        logger.exception("转接状态查询失败，按 active 处理 thread=%s", thread_id)
        return handoff.STATUS_ACTIVE

# ============================================================
# 落库 + 推送（两条入口共用的"写一条消息"动作）
# ============================================================

def _append(
    thread_id: str, role: str, content: str,
    user_id: str | None = None, sender_id: int | None = None,
) -> dict | None:
    """写一条会话记录并返回 `{id, ts}`；失败向上抛（由调用方决定后果）。"""
    return handoff.append_message(
        thread_id, role, content,
        # 只给用户消息打归属标记；坐席消息带 sender_id（展示"是谁回复的"）
        user_id if role == ROLE_USER else None,
        sender_id,
    )

def _append_safe(
    thread_id: str, role: str, content: str, user_id: str | None = None,
) -> dict | None:
    """写一条会话记录，失败只记日志。

    适用场景：记录表抖动不该让用户看不到已经生成好的回复。
    """
    try:
        return _append(thread_id, role, content, user_id)
    except Exception:
        logger.exception("会话记录落库失败 thread=%s role=%s", thread_id, role)
        return None

def _notify_agents_message(
    thread_id: str, role: str, content: str, saved: dict | None,
    user_id: int | None = None,
) -> None:
    """把新消息推给正在看这个会话的坐席（替代坐席端的 4 秒轮询）。"""
    hub.push_agents(
        {
            "type": "message",
            "thread_id": thread_id,
            # user_id 让坐席端能判断"这条消息属不属于我正在看的那个用户"
            # （坐席详情面板展示的是跨 thread 的用户完整历史流）
            "user_id": user_id,
            # id 是服务端主键：前端靠它去重（消息会同时经 REST 与 WS 两条路径到达）
            "message": {
                "id": saved["id"] if saved else None,
                "role": role,
                "content": content,
                "ts": saved["ts"] if saved else None,
            },
        },
        thread_id=thread_id,
    )

def _push_user_message(
    user_id: int | None, role: str, content: str, saved: dict | None,
    sender_name: str | None = None,
) -> None:
    """把一条消息推给用户端（WS 入口专用；REST 入口用响应体回传）。"""
    if user_id is None:
        return
    hub.push_user(user_id, {
        "type": "message",
        "message": {
            "id": saved["id"] if saved else None,
            "role": role,
            "content": content,
            "ts": saved["ts"] if saved else None,
            "sender_name": sender_name,
        },
    })

# ============================================================
# 三条处理分支
# ============================================================

def _handle_guarded(
    thread_id: str, message: str, category: str,
    user_id: int | None = None, *, push_reply: bool = False,
) -> ChatOutcome:
    """输入安检命中后的处理：友好拒答 + 记录留痕（安全事件必须可追溯）。"""
    guardrails.log_rejection(thread_id, category, message)
    reply = guardrails.refusal_reply(category)
    # 拦截记录也进会话记录表：坐席端能看到用户发了什么、系统为何拦截
    saved_user = _append_safe(thread_id, ROLE_USER, message, user_id)
    _notify_agents_message(thread_id, ROLE_USER, message, saved_user, user_id)
    saved_agent = _append_safe(thread_id, ROLE_AGENT, reply)
    _notify_agents_message(thread_id, ROLE_AGENT, reply, saved_agent)
    if push_reply:
        _push_user_message(user_id, ROLE_AGENT, reply, saved_agent)
    # 状态用真实值而非硬编码 active：安检只是短路了本轮问答，
    # 会话可能仍在排队中，前端 UI 不该被这一轮错误地切回"自助"
    status = _get_thread_status(thread_id)
    return ChatOutcome(thread_id, reply, status, handoff.is_live_status(status))

def _handle_live(
    thread_id: str, message: str, status: str, user_id: int | None = None,
) -> ChatOutcome:
    """已转接会话的处理：用户消息直达人工工作台，不再消耗 LLM。

    注意这里**不推送任何消息气泡**：
    - 排队中：转接横幅已经写着"已转接人工，坐席会尽快回复"，
      再给每条消息回一句"您的消息已收到"会让用户连发几条时被刷屏
    - 坐席已接入：用户在跟人对话，机器人插话反而干扰

    发送是否成功由 `sent` 事件单独确认（见 app/api/ws.py），
    不能靠"有没有回复"去猜——那样坐席接入后用户永远等不到回复，
    输入框就会永久锁死（曾是一个真实 bug）。

    返回值里的 reply 只给 REST 客户端用（/chat 的响应体）。
    """
    try:
        saved = _append(thread_id, ROLE_USER, message, user_id)
    except Exception:
        # 落库失败不能吞掉用户的消息：让前端知道发送失败，提示重试
        logger.exception("会话记录落库失败 thread=%s", thread_id)
        raise HTTPException(status_code=503, detail="消息发送失败，请重试") from None
    # 即时推给坐席：这是"用户补发消息"的主链路，晚 4 秒都可能让坐席漏看
    _notify_agents_message(thread_id, ROLE_USER, message, saved, user_id)

    ack = _QUEUED_ACK if status == handoff.STATUS_QUEUED else ""
    return ChatOutcome(thread_id, ack, status, True)

# LLM 生成空回复时的兜底话术（前端至少能看到"失败了"，而不是空白气泡）
_EMPTY_REPLY = "抱歉，刚才的回复没有生成成功，请您再试一次。"

def _agent_config(thread_id: str, user_id: int | None):
    """构造图执行配置（含 Langfuse trace 透传）。"""
    return tracing.build_config(
        thread_id,
        user_id=user_id,
        trace_name="chat",
        base={"configurable": {"thread_id": thread_id}},
    )

async def _invoke_agent(thread_id: str, message: str, user_id: int | None = None) -> str:
    """执行 Agent 图并一次性取回纯文本回复（REST 入口用）。

    WS 入口走 `_stream_agent`；这里保留非流式实现是因为 REST 响应体
    本来就要一次给完整文本，用流式反而多绕一圈。
    """
    config = _agent_config(thread_id, user_id)
    try:
        graph = await get_graph()
        result = await graph.ainvoke(
            {"messages": [HumanMessage(content=message)]},
            config,
        )
    except Exception as exc:
        # 捕获并记录完整异常后转为友好提示：对外不泄露堆栈，对内留全日志。
        # LLM 超时/限流/断连在 get_llm 的 max_retries=2 仍失败后走到这里，
        # 按异常类别给出不同的可操作文案，而不是千篇一律的"服务不可用"
        logger.exception("chat 执行失败 thread=%s", thread_id)
        status_code, detail = _friendly_llm_error(exc)
        raise HTTPException(status_code=status_code, detail=detail) from None

    reply = result["messages"][-1]
    # content 可能是非字符串形态（分段 content / 空 tool_calls 消息），
    # 统一走 .text 取纯文本；空回复给兜底话术，避免前端渲染出空白气泡
    text = reply.text if isinstance(reply, AIMessage) else str(reply.content)
    if not text.strip():
        logger.warning("chat 生成空回复 thread=%s", thread_id)
        return _EMPTY_REPLY
    return text

async def _stream_agent(
    thread_id: str,
    message: str,
    user_id: int | None,
    on_delta: Callable[[str], None],
) -> str:
    """流式执行 Agent 图：每生成一段文本就回调 `on_delta`，返回完整文本。

    为什么用 astream_events 而不是 astream：
    `astream` 只给出**节点级**的输出（agent 节点跑完才拿到整段 AIMessage），
    拿不到 token。`astream_events` 能捕获 `on_chat_model_stream`，
    才是真正的逐 token 流。

    **必须过滤空 content**：Agent 是 tool-calling 结构，一轮对话至少两次
    模型调用（先决定调工具、再根据结果作答）。实测第一轮 100% 是
    空 content + tool_calls，只有最后一轮才有正文。若不过滤，前端会收到
    大量空 delta，且可能把工具调用的中间文本当成回答推给用户。

    异常处理与 `_invoke_agent` 一致：翻译成友好 HTTPException。
    """
    config = _agent_config(thread_id, user_id)
    parts: list[str] = []
    try:
        graph = await get_graph()
        async for event in graph.astream_events(
            {"messages": [HumanMessage(content=message)]}, config, version="v2"
        ):
            if event["event"] != "on_chat_model_stream":
                continue
            chunk = event["data"]["chunk"]
            # 工具调用轮次的 chunk 没有正文（content 为空），跳过；
            # 这是区分"中间轮次"和"最终回答"最简单可靠的口径
            text = getattr(chunk, "content", "") or ""
            if not text:
                continue
            parts.append(text)
            on_delta(text)
    except Exception as exc:
        logger.exception("chat 流式执行失败 thread=%s", thread_id)
        status_code, detail = _friendly_llm_error(exc)
        raise HTTPException(status_code=status_code, detail=detail) from None

    full = "".join(parts)
    if not full.strip():
        logger.warning("chat 生成空回复 thread=%s", thread_id)
        return _EMPTY_REPLY
    return full

# ============================================================
# 业务编排入口（REST / WS 共用）
# ============================================================

def _resolve_thread(user: dict | None, thread_id: str | None) -> str:
    """解析本轮消息所属的 thread_id。

    登录用户的 thread_id 由服务端解析（前端传值被忽略）：
    按账号反查最近的聊天流，没有则新建——"一个用户一条聊天记录"，
    换设备/清缓存/退出重登后发消息即自动接回同一条流，消息不丢。
    thread_id 因此成为纯实现细节，前端无需再持久化它。
    """
    if user is None:
        # 匿名用户：维持旧行为，前端传什么用什么（localStorage 续接）
        return thread_id or str(uuid.uuid4())

    tid = handoff.get_user_current_thread(user["id"])
    if tid and handoff.get_status(tid) == handoff.STATUS_CLOSED:
        # 当前流已 closed（上一次人工服务结束）则开新 thread：
        # 新问题从干净的 Agent 上下文开始，旧 thread 的 Checkpointer
        # 快照与 chat_message 记录都保留，后续排查/审计有据可查
        tid = None
    if not tid:
        tid = str(uuid.uuid4())
    handoff.set_thread_user(tid, user["id"])
    return tid

def _rate_key(user: dict | None, thread_id: str | None, client_ip: str | None) -> str:
    """限流键：登录用户按账号，匿名按 IP。

    为什么匿名不能用 thread_id：它是客户端自己传的（localStorage 里的值），
    每次换个新 ID 就是一个新的计数桶——等于没限流。
    IP 能伪造（代理头），但比"完全免费的客户端 ID"强得多；
    真要精确需要网关层配合，不在本步范围。

    拿不到 IP 时（如脚本直接调内部函数）退回 thread_id：
    这是"有总比没有好"的降级，不假装它安全。
    """
    if user is not None:
        return f"user:{user['id']}"
    if client_ip:
        return f"ip:{client_ip}"
    return f"anon:{thread_id or 'unknown'}"

async def process_message(
    message: str,
    user: dict | None,
    thread_id: str | None = None,
    *,
    push_reply: bool = False,
    client_ip: str | None = None,
    on_delta: Callable[[str], None] | None = None,
) -> ChatOutcome:
    """一轮对话的完整业务处理（落库 + 推送 + 生成回复）。

    push_reply：回复是否通过 WebSocket 推给用户。
    REST 入口传 False（回复走响应体，避免同一个气泡到两次）；
    WS 入口传 True（回复没有响应体可回，只能推）。

    client_ip：匿名用户的限流键。登录用户用账号 ID，匿名必须用 IP——
    匿名会话的 thread_id 是客户端自己传的，换个 ID 就能绕过限流。

    on_delta：流式回调。传了就走流式（每段文本回调一次），不传则一次性生成。
    只有"正常自助问答"分支会流式——安检拦截、已转人工、LLM 异常这些
    都是瞬时结果，没有可流的内容，走原来的整体推送。
    """
    # 限流放在最前面：它比后面的安检/落库/LLM 都便宜，
    # 且被拦下的请求不应该产生任何副作用（不写库、不打 LLM）
    ratelimit.check_rate(_rate_key(user, thread_id, client_ip))

    thread_id = _resolve_thread(user, thread_id)
    # 匿名用户无归属，user 消息不带 user_id；登录用户统一取账号 ID
    uid = user["id"] if user else None

    # 输入安检（API 层短路）
    if get_settings().input_guardrail:
        category = guardrails.check_message(message)
        if category:
            return _handle_guarded(thread_id, message, category, uid, push_reply=push_reply)

    # 已转接会话：用户消息直达人工
    status = _get_thread_status(thread_id)
    if handoff.is_live_status(status):
        return _handle_live(thread_id, message, status, uid)

    # 正常自助问答。
    # 用户消息**先落库再调 Agent**（与原实现相反），两个收益：
    # 1. 坐席端不必等 LLM 跑完才看到"用户在等"，实时感来自这里
    # 2. LLM 超时/失败时用户消息不会凭空消失（history 里留有据可查）
    saved_user = _append_safe(thread_id, ROLE_USER, message, uid)
    _notify_agents_message(thread_id, ROLE_USER, message, saved_user, uid)

    # 并发闸门只包住 LLM 调用（真正的昂贵资源），不包落库：
    # 落库是毫秒级操作，限它反而会在繁忙时丢掉用户消息
    guard = ratelimit.get_guard()
    await guard.acquire()
    try:
        if on_delta is not None:
            text = await _stream_agent(thread_id, message, uid, on_delta)
        else:
            text = await _invoke_agent(thread_id, message, user_id=uid)
    finally:
        # finally 里释放：LLM 超时/抛异常也必须归还名额，
        # 否则几个失败请求就能把闸门永久占满
        guard.release()

    saved_agent = _append_safe(thread_id, ROLE_AGENT, text)
    _notify_agents_message(thread_id, ROLE_AGENT, text, saved_agent)
    # 流式分支下内容已经边生成边推给用户了，这里只落库 + 通知坐席。
    # 再整条推一次会让用户看到两条相同内容（id 不同，去重拦不住）
    if push_reply and on_delta is None:
        _push_user_message(uid, ROLE_AGENT, text, saved_agent)

    # 转接可能在 Agent 执行中发生（LLM 调了转接工具 / 关键词兜底）：
    # 重新查一次状态。查询失败降级为"未转接"——代价是前端少一次提示，可接受。
    # 状态本身的推送由 handoff 状态机负责（见 handoff._notify_status），
    # 这里只取结果给响应体用
    status = _get_thread_status(thread_id)
    return ChatOutcome(
        thread_id, text, status, handoff.is_live_status(status),
        reply_id=saved_agent["id"] if saved_agent else None,
        reply_ts=saved_agent["ts"] if saved_agent else None,
    )

# ============================================================
# REST 路由（保留：脚本/调试/WS 不可用时兜底）
# ============================================================

@router.post("/chat", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    request: Request,
    user: OptionalUser,
) -> ChatResponse:
    """核心对话接口：一个问题进，一个回复出。

    认证为"可选"：未登录仍可自助问答（匿名会话，坐席端显示为游客）；
    登录用户则会话绑定到账号，坐席端才能看到"谁在提问"。
    """
    try:
        outcome = await process_message(
            req.message, user, req.thread_id, client_ip=_client_ip(request)
        )
    except ratelimit.RateLimitExceeded as exc:
        # 429 而非 503：语义是"你发得太快"，客户端应该退避而不是换台服务器
        raise HTTPException(
            status_code=429,
            detail=f"发送太频繁，请 {exc.retry_after} 秒后再试",
            headers={"Retry-After": str(exc.retry_after)},
        ) from None
    except ratelimit.BusyError:
        # 503：系统繁忙（并发闸门满了），与"你发太快"区分开
        raise HTTPException(
            status_code=503,
            detail="当前咨询人数较多，请稍后重试",
            headers={"Retry-After": "5"},
        ) from None
    return ChatResponse(
        message=outcome.reply,
        thread_id=outcome.thread_id,
        transferred=outcome.transferred,
    )

def _client_ip(request: Request) -> str | None:
    """取客户端 IP 作为匿名限流键。

    优先 X-Forwarded-For 的第一段（反向代理后的真实客户端）。
    这个头客户端可以伪造——所以它只用于"提高绕过成本"，
    不当成安全边界；真正需要精确限流时应该在网关层做。
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip() or None
    return request.client.host if request.client else None

def build_user_snapshot(user_id: int) -> dict:
    """用户端连接建立时的状态快照（WS 的 ready 事件，也是 /chat/my/history 的数据源）。

    为什么用"全量快照 + 增量推送"而不是"纯增量"：
    断线重连、换设备、多端同时在线都会让增量流不完整，
    每次连接都给一份全量真相，客户端不需要自己补齐缺口。
    """
    messages = handoff.get_user_messages(user_id)
    tid = handoff.get_user_current_thread(user_id)
    status = handoff.get_status(tid) if tid else handoff.STATUS_ACTIVE
    return {
        "thread_id": tid or "",
        "status": status,
        "messages": [SessionMessage(**m).model_dump() for m in messages],
    }

@router.get("/chat/my/history", response_model=SessionHistoryResponse)
async def my_history(user: CurrentUser) -> SessionHistoryResponse:
    """当前用户的完整聊天记录（必须登录，跨设备恢复）。

    数据源是 chat_message 表按 user_id 拉平合并的全部消息（跨 thread
    按时间序），登录后无论换设备还是清 localStorage 都能看到完整聊天流。
    status 取当前活跃 thread 的状态（排队/服务中状态跨设备也正确）。
    """
    return SessionHistoryResponse(**build_user_snapshot(user["id"]))

@router.get("/chat/sessions", response_model=SessionListResponse)
async def list_sessions(user: CurrentUser) -> SessionListResponse:
    """当前用户的会话列表（必须登录）。

    消息与用户的绑定关系：chat_message.user_id 存发送者、
    conversation_status.user_id 存会话归属——列表按会话归属查，
    登录后无论换浏览器还是清 localStorage 都能找回全部历史。
    """
    threads = handoff.list_user_threads(user["id"])
    return SessionListResponse(threads=[SessionSummary(**t) for t in threads])

def _messages_from_checkpointer(state) -> list[SessionMessage]:
    """从 Checkpointer 快照恢复消息列表（存量老会话的数据源）。"""
    if not state or not state.values.get("messages"):
        raise HTTPException(status_code=404, detail="会话不存在")
    return [
        SessionMessage(
            role=ROLE_USER if isinstance(m, HumanMessage) else ROLE_AGENT,
            content=m.text,
        )
        for m in state.values["messages"]
        if isinstance(m, (HumanMessage, AIMessage))
        # 过滤掉 tool_calls 中间消息（content 为空的 AIMessage）：
        # 它是 Agent 内部的"工具调用记录"，不是要展示给用户的对话内容
        and not (isinstance(m, AIMessage) and not m.text.strip())
    ]

@router.get("/chat/{thread_id}/history", response_model=SessionHistoryResponse)
async def history(
    user: OptionalUser,  # 依赖参数无默认值，必须放在有默认值的 thread_id 之前
    thread_id: str = Path(..., pattern=THREAD_ID_PATTERN),
) -> SessionHistoryResponse:
    """会话历史查询：人工坐席端与用户端跨天恢复的数据来源。

    访问控制：登录后，非本人且非坐席不可看别人的会话；
    未登录请求保持放行（匿名会话无归属，且 thread_id 为 UUID 不可枚举）。

    数据源优先级：
    1. 会话记录表 chat_message：包含用户/智能客服/人工坐席的完整消息流
    2. 回退 Checkpointer 快照：step-20 之前的存量会话没有记录表数据，
       保证升级后老会话历史不丢
    """
    owner = handoff.get_thread_user(thread_id)
    if (
        user is not None
        and owner is not None
        and owner != user["id"]
        and user["role"] != "agent"
    ):
        raise HTTPException(status_code=403, detail="无权访问该会话")

    try:
        rows = handoff.get_messages(thread_id)
        if rows:
            messages = [
                SessionMessage(role=r["role"], content=r["content"], ts=r["ts"])
                for r in rows
            ]
        else:
            # 注意用 aget_state（'读取'模式）——只取快照不推进图执行
            graph = await get_graph()
            state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
            messages = _messages_from_checkpointer(state)
        status = handoff.get_status(thread_id)
    except HTTPException:
        raise  # 业务性 404 直接透传，不进"服务不可用"分支
    except Exception:
        # Postgres/Milvus 故障等基础设施异常：对外 503，对内留全日志
        logger.exception("history 查询失败 thread=%s", thread_id)
        raise HTTPException(status_code=503, detail="会话查询暂时不可用，请稍后重试") from None

    return SessionHistoryResponse(
        thread_id=thread_id,
        status=status,
        messages=messages,
    )
