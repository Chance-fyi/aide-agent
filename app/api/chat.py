"""核心对话接口。"""

import logging
import uuid

import openai
from fastapi import APIRouter, HTTPException, Path
from langchain_core.messages import AIMessage, HumanMessage

from app.agents import handoff
from app.agents.runtime import get_graph
from app.api.schemas import (
    ChatRequest,
    ChatResponse,
    SessionHistoryResponse,
    SessionMessage,
)
from app.core import guardrails
from app.core.config import get_settings

logger = logging.getLogger(__name__)

# 已转接状态下用户继续发消息时的应答话术（仅排队中提示一次，不进 LLM，消息本身直达坐席工作台）
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

@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    """核心对话接口：一个问题进，一个回复出。

    thread_id 语义：
    - 不传：服务端生成新会话 ID
    - 传入：续接该会话的多轮上下文（Checkpointer 自动恢复历史）
    """
    thread_id = req.thread_id or str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    # 输入安检（API 层短路）：命中注入/违禁词直接友好拒答，
    # 不进 graph——省一次 LLM 调用，也不往 Checkpointer 写污染状态。
    # 拦截后记日志留痕（安全事件必须可追溯）
    if get_settings().input_guardrail:
        category = guardrails.check_message(req.message)
        if category:
            guardrails.log_rejection(thread_id, category, req.message)
            reply = guardrails.refusal_reply(category)
            # 拦截记录也进会话记录表：坐席端能看到用户发了什么、系统为何拦截
            try:
                handoff.append_message(thread_id, "user", req.message)
                handoff.append_message(thread_id, "bot", reply)
            except Exception:
                logger.exception("拦截记录落库失败 thread=%s", thread_id)
            return ChatResponse(
                message=reply,
                thread_id=thread_id,
                transferred=False,
            )

    # 已转接会话（排队中/坐席服务中）：用户消息直达人工工作台，不再消耗 LLM。
    # 应答话术只回给前端、不落库——它不是对话内容，落库会在坐席端制造噪音；
    # 代价是刷新后这两条临时应答消失，但用户消息本身已在记录表里，可接受
    try:
        status = handoff.get_status(thread_id)
    except Exception:
        logger.exception("转接状态查询失败 thread=%s", thread_id)
        status = handoff.STATUS_ACTIVE
    if handoff.is_live_status(status):
        try:
            handoff.append_message(thread_id, "user", req.message)
        except Exception:
            # 落库失败不能吞掉用户的消息：至少让前端知道发送失败，提示重试
            logger.exception("用户消息落库失败 thread=%s", thread_id)
            raise HTTPException(status_code=503, detail="消息发送失败，请重试") from None
        # 排队中：回一句确认话术让用户知道消息没丢；
        # 坐席已接入：不再回机器人应答（message 置空，前端不渲染气泡）——
        # 用户此时在和人工对话，每次发言都被机器人"已收到"刷屏反而干扰
        ack = _QUEUED_ACK if status == handoff.STATUS_QUEUED else ""
        return ChatResponse(message=ack, thread_id=thread_id, transferred=True)

    try:
        graph = await get_graph()
        result = await graph.ainvoke(
            {"messages": [HumanMessage(content=req.message)]},
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
        text = "抱歉，刚才的回复没有生成成功，请您再试一次。"

    # 用户消息与回复都进会话记录表：这是用户端跨天恢复、坐席端查看的统一数据源。
    # 落库失败只记日志：回复本身已生成，因记录表抖动而报错得不偿失
    try:
        handoff.append_message(thread_id, "user", req.message)
        handoff.append_message(thread_id, "bot", text)
    except Exception:
        logger.exception("会话记录落库失败 thread=%s", thread_id)

    # 状态查询失败不应让已生成的回复报废：降级为"未转接"并记日志。
    # 代价是极端情况下 transferred 标记丢失（前端 UI 少一次提示），可接受
    try:
        status = handoff.get_status(thread_id)
    except Exception:
        logger.exception("转接状态查询失败 thread=%s", thread_id)
        status = handoff.STATUS_ACTIVE

    return ChatResponse(
        message=text,
        thread_id=thread_id,
        # 排队中/坐席服务中都算"已转接"，前端据此展示对应状态 UI
        transferred=handoff.is_live_status(status),
    )

@router.get("/chat/{thread_id}/history", response_model=SessionHistoryResponse)
async def history(
    thread_id: str = Path(..., pattern=THREAD_ID_PATTERN),
) -> SessionHistoryResponse:
    """会话历史查询：人工坐席端与前端 Debugger 的数据来源。

    从 Checkpointer 的持久化状态中恢复消息列表，
    注意要用 None 输入 invoke（'读取'模式）——只取快照不推进图执行。
    """
    try:
        # 优先从会话记录表读：包含用户/机器人/人工坐席的完整消息流。
        rows = handoff.get_messages(thread_id)
        if rows:
            messages = [SessionMessage(role=r["role"], content=r["content"], ts=r["ts"]) for r in rows]
        else:
            # 回退：step-15 之前的存量会话没有记录表数据，从 Checkpointer 快照恢复，
            # 保证升级后老会话历史不丢。角色统一映射为 bot（当时没有人工消息）
            graph = await get_graph()
            config = {"configurable": {"thread_id": thread_id}}
            state = await graph.aget_state(config)
            if not state or not state.values.get("messages"):
                raise HTTPException(status_code=404, detail="会话不存在")
            messages = [
                SessionMessage(
                    role="user" if isinstance(m, HumanMessage) else "bot",
                    content=m.text(),
                )
                for m in state.values["messages"]
                if isinstance(m, (HumanMessage, AIMessage))
                # 过滤掉 tool_calls 中间消息（content 为空的 AIMessage）：
                # 它是 Agent 内部的"工具调用记录"，不是要展示给用户的对话内容
                and not (isinstance(m, AIMessage) and not m.text().strip())
            ]
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
