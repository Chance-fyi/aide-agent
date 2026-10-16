"""API 路由层。

MVP 阶段只有 /health；/chat 在步骤 8 接入。
路由层只做参数校验和调用编排，不写业务逻辑。
"""

from fastapi import APIRouter

from app.core.config import get_settings

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
