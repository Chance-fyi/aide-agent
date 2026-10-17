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
            return ChatResponse(
                message=guardrails.refusal_reply(category),
                thread_id=thread_id,
                transferred=False,
            )

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
        transferred=status == handoff.STATUS_PENDING_HUMAN,
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
        graph = await get_graph()
        config = {"configurable": {"thread_id": thread_id}}

        state = await graph.aget_state(config)
        if not state or not state.values.get("messages"):
            raise HTTPException(status_code=404, detail="会话不存在")

        messages = [
            SessionMessage(
                role="user" if isinstance(m, HumanMessage) else "assistant",
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
