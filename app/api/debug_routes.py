"""调试路由：知识库效果验证工具。

仅当 settings.debug=True 时注册（见 main.py）。

/search 已切换为走正式的 Milvus 检索链路（store.search），
保证调试接口看到的就是生产链路的真实效果。
/memory_search 保留内存版实现，用于两者结果对照：
两者分数一致说明检索链路口径一致，不一致则说明某条链路有问题。
/split_compare 是切分策略对比工具（step-17）：用同一批查询在不同
策略下比检索效果，选定策略后再改配置重建。
"""

from functools import lru_cache

from fastapi import APIRouter, HTTPException, Query

from app.core.config import get_settings
from app.rag import store
from app.rag.embedder import embed_query, embed_texts
from app.rag.splitter import Chunk, load_and_split_all

router = APIRouter(prefix="/debug/kb", tags=["debug"])

KB_DIR = "data/knowledge_base"

@lru_cache
def _load_chunks(strategy: str = "header") -> tuple[Chunk, ...]:
    """切块结果缓存（元组保证不可变，可被 lru_cache 使用）。"""
    return tuple(load_and_split_all(KB_DIR, strategy=strategy))

@lru_cache
def _chunk_vectors(strategy: str = "header") -> tuple[list[float], ...]:
    """所有块的向量缓存（仅内存版检索使用）。"""
    chunks = _load_chunks(strategy)
    return tuple(embed_texts([c.content for c in chunks]))

def _memory_search(strategy: str, q: str, top_k: int) -> list[dict]:
    """内存版检索：不经过 Milvus，供对照与策略比较复用。"""
    chunks = _load_chunks(strategy)
    vectors = _chunk_vectors(strategy)
    qv = embed_query(q)
    # 纯 Python 内积；块数量小（几十个），无需引入 numpy/FAISS
    scored = sorted(
        zip((sum(a * b for a, b in zip(qv, vec)) for vec in vectors), chunks, strict=True),
        key=lambda pair: pair[0],
        reverse=True,
    )
    return [
        {"score": round(score, 4), "source": c.source, "h1": c.h1, "h2": c.h2, "content": c.content}
        for score, c in scored[:top_k]
    ]

@router.get("/chunks")
async def list_chunks(
    source: str | None = Query(default=None, description="按文件名过滤，如 returns.md"),
    limit: int = Query(default=5, le=50),
):
    """查看切块结果：总块数、长度分布、抽样内容。用于人工检查切块质量。"""
    chunks = _load_chunks(get_settings().kb_split_strategy)
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
    return {
        "query": q,
        "backend": "memory",
        "results": _memory_search(get_settings().kb_split_strategy, q, top_k),
    }

@router.get("/split_compare")
async def split_compare(
    strategies: str = Query(default="header,paragraph,fixed", description="逗号分隔的切分策略"),
    q: str = Query(default="", description="对比用的查询串；为空只看切块统计"),
    top_k: int = Query(default=3, le=10),
):
    """切分策略对比工具（step-17）：策略效果难以凭直觉判断，用数据说话。

    每个策略返回：切块统计（数量/均长/最长）+ 同一查询的内存检索 top_k。
    判读标准：目标问题的目标知识应排第一且分数明显甩开第三名。
    选定策略后：改 KB_SPLIT_STRATEGY + bump KB_SCHEMA_VERSION + rebuild。
    """
    from app.rag.splitter import _STRATEGIES

    wanted = [s.strip() for s in strategies.split(",") if s.strip()]
    unknown = [s for s in wanted if s not in _STRATEGIES]
    if unknown:
        raise HTTPException(status_code=422, detail=f"未知切分策略: {unknown}，可选: {sorted(_STRATEGIES)}")

    report = []
    for strategy in wanted:
        chunks = _load_chunks(strategy)
        entry = {
            "strategy": strategy,
            "chunk_count": len(chunks),
            "avg_len": round(sum(len(c.content) for c in chunks) / len(chunks), 1),
            "max_len": max(len(c.content) for c in chunks),
        }
        if q.strip():
            entry["top_k"] = _memory_search(strategy, q, top_k)
        report.append(entry)

    return {"query": q or None, "strategies": report}

@router.post("/rebuild")
async def rebuild():
    """触发知识库蓝绿全量重建（改了语料/切分逻辑后调用，无需重启服务）。"""
    count = store.rebuild_index(KB_DIR)
    return {"status": "ok", "chunks": count}

def register_debug_routes(app) -> None:
    """按配置开关注册调试路由（生产环境关闭）。"""
    if get_settings().debug:
        app.include_router(router)
