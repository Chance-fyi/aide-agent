"""API 路由层：/health 健康检查与坐席操作接口。

路由层只做参数校验和调用编排，不写业务逻辑。
"""

import logging

from fastapi import APIRouter, HTTPException, Path

from app.agents import handoff
from app.api.chat import THREAD_ID_PATTERN
from app.api.schemas import AgentActionRequest
from app.core.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter()

@router.get("/health")
async def health():
    """健康检查：中间件连通性在这里汇总上报（后续步骤逐步补充检查项）。"""
    s = get_settings()
    return {
        "status": "ok",
        "llm_model": s.llm_model,
        "milvus": s.milvus_uri,
    }


# ============================================================
# 坐席操作接口（模拟坐席端/管理后台的数据入口）
# 鉴权待坐席端迭代统一接入：这两个接口当前仅供内网调试使用
# ============================================================

agent_router = APIRouter(prefix="/agent", tags=["agent"])

@agent_router.post("/threads/{thread_id}/accept")
async def accept_thread(
    thread_id: str = Path(..., pattern=THREAD_ID_PATTERN),
    body: AgentActionRequest | None = None,
):
    """坐席接入排队会话：queued → in_service。

    409 而非 404：会话不存在/已被其他坐席接走/超时回退，
    对坐席端来说是同一个含义——"工单不在队列里了，请刷新"。
    """
    body = body or AgentActionRequest()
    if not handoff.agent_accept(thread_id, body.agent_name):
        raise HTTPException(
            status_code=409,
            detail="该会话不在排队中（可能已被接入、超时回退或不存在）",
        )
    return {"thread_id": thread_id, "status": handoff.STATUS_IN_SERVICE}

@agent_router.post("/threads/{thread_id}/close")
async def close_thread(
    thread_id: str = Path(..., pattern=THREAD_ID_PATTERN),
    body: AgentActionRequest | None = None,
):
    """坐席结束处理：→ closed；用户下次发消息视为新问题回自助。"""
    body = body or AgentActionRequest()
    if not handoff.agent_close(thread_id, body.resolution or "处理完成"):
        raise HTTPException(status_code=409, detail="该会话不处于待处理状态")
    return {"thread_id": thread_id, "status": handoff.STATUS_CLOSED}
