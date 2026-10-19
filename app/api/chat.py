"""核心对话接口。

处理顺序（短路优先级）：
1. 输入安检命中 → 友好拒答，不进 graph（省一次 LLM 调用，不写污染状态）
2. 已转接（排队中/服务中） → 消息直达人工工作台，不进 graph
3. 其他 → 正常走 Agent
"""

import logging
import uuid

import openai
from fastapi import APIRouter, HTTPException, Path
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
from app.core import guardrails
from app.core.config import get_settings

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


def _save_messages(thread_id: str, *pairs: tuple[str, str], user_id: str | None = None) -> bool:
    """批量写入会话记录表，返回是否全部成功。

    user_id 绑定到 user 角色的消息上（消息归属用户，支持按用户查历史）；
    agent/human 消息属于会话本身，不带归属。
    调用方按业务决定失败后果：拦截记录落库失败只记日志；
    用户消息落库失败则必须报错（不能吞掉用户的消息）。
    """
    try:
        for role, content in pairs:
            # 只给用户消息打归属标记
            handoff.append_message(thread_id, role, content, user_id if role == ROLE_USER else None)
        return True
    except Exception:
        logger.exception(
            "会话记录落库失败 thread=%s roles=%s",
            thread_id, [r for r, _ in pairs],
        )
        return False


def _handle_guarded(thread_id: str, message: str, category: str, user_id: str | None = None) -> ChatResponse:
    """输入安检命中后的处理：友好拒答 + 记录留痕（安全事件必须可追溯）。"""
    guardrails.log_rejection(thread_id, category, message)
    reply = guardrails.refusal_reply(category)
    # 拦截记录也进会话记录表：坐席端能看到用户发了什么、系统为何拦截
    _save_messages(thread_id, (ROLE_USER, message), (ROLE_AGENT, reply), user_id=user_id)
    return ChatResponse(message=reply, thread_id=thread_id, transferred=False)


def _handle_live(thread_id: str, message: str, status: str, user_id: str | None = None) -> ChatResponse:
    """已转接会话的处理：用户消息直达人工工作台，不再消耗 LLM。

    应答话术只回给前端、不落库——它不是对话内容，落库会在坐席端制造噪音；
    代价是刷新后临时应答消失，但用户消息本身已在记录表里，可接受。
    """
    if not _save_messages(thread_id, (ROLE_USER, message), user_id=user_id):
        # 落库失败不能吞掉用户的消息：让前端知道发送失败，提示重试
        raise HTTPException(status_code=503, detail="消息发送失败，请重试") from None
    # 排队中：回一句确认话术让用户知道消息没丢；
    # 坐席已接入：不再回机器人应答（message 置空，前端不渲染气泡）——
    # 用户此时在和人工对话，每次发言都被机器人"已收到"刷屏反而干扰
    ack = _QUEUED_ACK if status == handoff.STATUS_QUEUED else ""
    return ChatResponse(message=ack, thread_id=thread_id, transferred=True)


async def _invoke_agent(thread_id: str, message: str) -> str:
    """执行 Agent 图并提取纯文本回复；LLM 链路异常翻译为友好 HTTP 错误。"""
    config = {"configurable": {"thread_id": thread_id}}
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
    # 统一走 .text() 取纯文本；空回复给兜底话术，避免前端渲染出空白气泡
    text = reply.text() if isinstance(reply, AIMessage) else str(reply.content)
    if not text.strip():
        logger.warning("chat 生成空回复 thread=%s", thread_id)
        return "抱歉，刚才的回复没有生成成功，请您再试一次。"
    return text


@router.post("/chat", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    user: OptionalUser,
) -> ChatResponse:
    """核心对话接口：一个问题进，一个回复出。

    thread_id 语义：
    - 不传：服务端生成新会话 ID
    - 传入：续接该会话的多轮上下文（Checkpointer 自动恢复历史）

    认证为"可选"：未登录仍可自助问答（匿名会话，坐席端显示为游客）；
    登录用户则会话绑定到账号，坐席端才能看到"谁在提问"。

    登录用户的 thread_id 由服务端解析（前端传值被忽略）：
    按账号反查最近的聊天流，没有则新建——"一个用户一条聊天记录"，
    换设备/清缓存/退出重登后发消息即自动接回同一条流，消息不丢。
    thread_id 因此成为纯实现细节，前端无需再持久化它。
    """
    if user is not None:
        # 服务端解析当前聊天流：优先复用，无则新建并立即绑定归属。
        # 当前流已 closed（上一次人工服务结束）则开新 thread：
        # 新问题从干净的 Agent 上下文开始，旧 thread 的 Checkpointer
        # 快照与 chat_message 记录都保留，后续排查/审计有据可查
        thread_id = handoff.get_user_current_thread(user["id"])
        if thread_id and handoff.get_status(thread_id) == handoff.STATUS_CLOSED:
            thread_id = None
        if not thread_id:
            thread_id = str(uuid.uuid4())
        handoff.set_thread_user(thread_id, user["id"])
    else:
        # 匿名用户：维持旧行为，前端传什么用什么（localStorage 续接）
        thread_id = req.thread_id or str(uuid.uuid4())

    # 输入安检（API 层短路）
    if get_settings().input_guardrail:
        category = guardrails.check_message(req.message)
        if category:
            return _handle_guarded(thread_id, req.message, category, user_id=user["id"] if user else None)

    # 已转接会话：用户消息直达人工
    status = _get_thread_status(thread_id)
    if handoff.is_live_status(status):
        return _handle_live(thread_id, req.message, status, user_id=user["id"] if user else None)

    # 正常自助问答：Agent 生成回复后落记录表（跨天恢复/坐席端查看的统一数据源）
    text = await _invoke_agent(thread_id, req.message)
    # 落库失败只记日志：回复本身已生成，因记录表抖动而报错得不偿失
    _save_messages(thread_id, (ROLE_USER, req.message), (ROLE_AGENT, text), user_id=user["id"] if user else None)

    # 转接可能在 Agent 执行中发生（LLM 调了转接工具）：重新查一次状态。
    # 查询失败降级为"未转接"——代价是前端 UI 少一次提示，可接受
    return ChatResponse(
        message=text,
        thread_id=thread_id,
        transferred=handoff.is_live_status(_get_thread_status(thread_id)),
    )


@router.get("/chat/my/history", response_model=SessionHistoryResponse)
async def my_history(user: CurrentUser) -> SessionHistoryResponse:
    """当前用户的完整聊天记录（必须登录，跨设备恢复）。

    数据源是 chat_message 表按 user_id 拉平合并的全部消息（跨 thread
    按时间序），登录后无论换设备还是清 localStorage 都能看到完整聊天流。
    status 取当前活跃 thread 的状态（排队/服务中状态跨设备也正确）。
    """
    messages = handoff.get_user_messages(user["id"])
    status = handoff.STATUS_ACTIVE
    tid = handoff.get_user_current_thread(user["id"])
    if tid:
        status = handoff.get_status(tid)
    return SessionHistoryResponse(
        thread_id=tid or "",
        status=status,
        messages=[SessionMessage(**m) for m in messages],
    )

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
            content=m.text(),
        )
        for m in state.values["messages"]
        if isinstance(m, (HumanMessage, AIMessage))
        # 过滤掉 tool_calls 中间消息（content 为空的 AIMessage）：
        # 它是 Agent 内部的"工具调用记录"，不是要展示给用户的对话内容
        and not (isinstance(m, AIMessage) and not m.text().strip())
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
