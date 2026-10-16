"""调试路由：验证切块与 embedding 效果（步骤 4 阶段的临时验证工具）。

仅当 settings.debug=True 时注册（见 main.py）。
等步骤 5 Milvus 接入后，这里的内存检索会被正式的 search_kb 取代，
但接口本身保留，作为"不依赖向量库的效果对照"很有价值。

内存检索的实现：进程启动后首次调用时把所有块向量化并缓存，
之后每次查询只向量化查询串，与缓存向量算内积（归一化向量：内积即余弦相似度）。
"""

from functools import lru_cache

from fastapi import APIRouter, Query

from app.core.config import get_settings
from app.rag.embedder import embed_query, embed_texts
from app.rag.splitter import Chunk, load_and_split_all

router = APIRouter(prefix="/debug/kb", tags=["debug"])

KB_DIR = "data/knowledge_base"


@lru_cache
def _load_chunks() -> tuple[Chunk, ...]:
    """切块结果缓存（元组保证不可变，可被 lru_cache 使用）。"""
    return tuple(load_and_split_all(KB_DIR))


@lru_cache
def _chunk_vectors() -> tuple[list[float], ...]:
    """所有块的向量缓存：只在首次调用时算一次。"""
    chunks = _load_chunks()
    return tuple(embed_texts([c.content for c in chunks]))


@router.get("/chunks")
async def list_chunks(
    source: str | None = Query(default=None, description="按文件名过滤，如 returns.md"),
    limit: int = Query(default=5, le=50),
):
    """查看切块结果：总块数、每块长度分布、抽样内容。用于人工检查切块质量。"""
    chunks = _load_chunks()
    filtered = [c for c in chunks if source is None or c.source == source]
    return {
        "total_chunks": len(chunks),
        "filtered_count": len(filtered),
        "max_len": max(len(c.content) for c in chunks),
        "avg_len": round(sum(len(c.content) for c in chunks) / len(chunks), 1),
        "chunks": [
            {
                "source": c.source,
                "h1": c.h1,
                "h2": c.h2,
                "len": len(c.content),
                "preview": c.content[:120],
            }
            for c in filtered[:limit]
        ],
    }


@router.get("/search")
async def memory_search(
    q: str = Query(..., description="查询串"),
    top_k: int = Query(default=3, le=10),
):
    """内存版语义检索：验证 embedding 召回效果（不需要 Milvus）。

    归一化向量的内积 = 余弦相似度，范围 [-1, 1]，越接近 1 越相关。
    """
    chunks = _load_chunks()
    vectors = _chunk_vectors()
    qv = embed_query(q)

    # 纯 Python 内积；块数量小（几十个），无需引入 numpy/FAISS
    scored = sorted(
        zip((sum(a * b for a, b in zip(qv, vec)) for vec in vectors), chunks, strict=True),
        key=lambda pair: pair[0],
        reverse=True,
    )
    return {
        "query": q,
        "results": [
            {"score": round(score, 4), "source": c.source, "h1": c.h1, "h2": c.h2, "content": c.content}
            for score, c in scored[:top_k]
        ],
    }


def register_debug_routes(app) -> None:
    """按配置开关注册调试路由（生产环境关闭）。"""
    if get_settings().debug:
        app.include_router(router)
