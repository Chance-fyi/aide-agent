"""核心对话接口。"""

import logging
import uuid

from fastapi import APIRouter, HTTPException
from langchain_core.messages import AIMessage, HumanMessage

from app.agents import handoff
from app.agents.runtime import get_graph
from app.api.schemas import (
    ChatRequest,
    ChatResponse,
    SessionHistoryResponse,
    SessionMessage,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    """核心对话接口：一个问题进，一个回复出。

    thread_id 语义：
    - 不传：服务端生成新会话 ID
    - 传入：续接该会话的多轮上下文（Checkpointer 自动恢复历史）
    """
    thread_id = req.thread_id or str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    try:
        graph = await get_graph()
        result = await graph.ainvoke(
            {"messages": [HumanMessage(content=req.message)]},
            config,
        )
    except Exception:
        # 捕获并记录完整异常后转为友好提示：对外不泄露堆栈，对内留全日志
        logger.exception("chat 执行失败 thread=%s", thread_id)
        raise HTTPException(status_code=503, detail="客服服务暂时不可用，请稍后重试") from None

    reply = result["messages"][-1]
    status = handoff.get_status(thread_id)

    return ChatResponse(
        message=reply.content,
        thread_id=thread_id,
        transferred=status == handoff.STATUS_PENDING_HUMAN,
    )


@router.get("/chat/{thread_id}/history", response_model=SessionHistoryResponse)
async def history(thread_id: str) -> SessionHistoryResponse:
    """会话历史查询：人工坐席端与前端 Debugger 的数据来源。

    从 Checkpointer 的持久化状态中恢复消息列表，
    注意要用 None 输入 invoke（'读取'模式）——只取快照不推进图执行。
    """
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

    return SessionHistoryResponse(
        thread_id=thread_id,
        status=handoff.get_status(thread_id),
        messages=messages,
    )
