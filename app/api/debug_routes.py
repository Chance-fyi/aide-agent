"""调试路由：知识库效果验证工具。

仅当 settings.debug=True 时注册（见 main.py）。

/search 已切换为走正式的 Milvus 检索链路（store.search），
保证调试接口看到的就是生产链路的真实效果。
/memory_search 保留内存版实现，用于两者结果对照：
两者分数一致说明检索链路口径一致，不一致则说明某条链路有问题。
"""

from functools import lru_cache

from fastapi import APIRouter, Query

from app.core.config import get_settings
from app.rag import store
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
    """所有块的向量缓存（仅内存版检索使用）。"""
    chunks = _load_chunks()
    return tuple(embed_texts([c.content for c in chunks]))


@router.get("/chunks")
async def list_chunks(
    source: str | None = Query(default=None, description="按文件名过滤，如 returns.md"),
    limit: int = Query(default=5, le=50),
):
    """查看切块结果：总块数、长度分布、抽样内容。用于人工检查切块质量。"""
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
async def milvus_search(
    q: str = Query(..., description="查询串"),
    top_k: int = Query(default=3, le=10),
):
    """正式链路检索：走 Milvus（与生产 /chat 用的同一条路径）。"""
    results = store.search(q, top_k=top_k)
    return {
        "query": q,
        "backend": "milvus",
        "results": [
            {
                "score": round(r["score"], 4),
                "source": r["source"],
                "h1": r["h1"],
                "h2": r["h2"],
                "content": r["content"],
            }
            for r in results
        ],
    }


@router.get("/memory_search")
async def memory_search(
    q: str = Query(..., description="查询串"),
    top_k: int = Query(default=3, le=10),
):
    """内存版对照检索：不经过 Milvus，用于验证 Milvus 链路口径是否一致。"""
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
        "backend": "memory",
        "results": [
            {"score": round(score, 4), "source": c.source, "h1": c.h1, "h2": c.h2, "content": c.content}
            for score, c in scored[:top_k]
        ],
    }


@router.post("/rebuild")
async def rebuild():
    """触发知识库全量重建（改了语料后调用，无需重启服务）。"""
    count = store.rebuild_index(KB_DIR)
    return {"status": "ok", "chunks": count}


def register_debug_routes(app) -> None:
    """按配置开关注册调试路由（生产环境关闭）。"""
    if get_settings().debug:
        app.include_router(router)
