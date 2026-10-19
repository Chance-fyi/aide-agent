"""调试路由：知识库效果验证工具。

仅当 settings.debug=True 时注册（见 main.py）。

/search 已切换为走正式的检索链路（store.search）——Milvus 向量检索 +
Postgres 父块回查，保证调试接口看到的就是生产链路的真实效果。
/memory_search 保留内存版实现，用于两者结果对照：
两者分数一致说明检索链路口径一致，不一致则说明某条链路有问题。
/split_compare 是切分策略对比工具（step-17）：强制全目录用同一策略对比效果。
/strategies 管理按文件切分策略（step-30）：不同文档结构不同，按文件配置。
"""

from functools import lru_cache

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.rag import kb_store, store
from app.rag.embedder import embed_query, embed_texts
from app.rag.splitter import _STRATEGIES, Chunk, load_and_split_all

router = APIRouter(prefix="/debug/kb", tags=["debug"])

KB_DIR = "data/knowledge_base"

@lru_cache
def _load_chunks(force: str | None = None) -> tuple[Chunk, ...]:
    """切块结果缓存（按策略参数区分；None = 生产口径：按文件策略）。

    生产口径下策略可能被 /strategies 接口修改，重建后应调 clear_caches()。
    """
    strategies = None if force else kb_store.get_strategies()
    return tuple(load_and_split_all(KB_DIR, strategies=strategies, force=force))

def clear_caches() -> None:
    """策略变更/重建后清缓存，让调试接口立即看到新切块。"""
    _load_chunks.cache_clear()
    _chunk_vectors.cache_clear()

@lru_cache
def _chunk_vectors(force: str | None = None) -> tuple[list[float], ...]:
    """所有块的向量缓存（仅内存版检索使用）。"""
    chunks = _load_chunks(force)
    return tuple(embed_texts([c.content for c in chunks]))

def _memory_search(force: str | None, q: str, top_k: int) -> list[dict]:
    """内存版检索：不经过 Milvus，供对照与策略比较复用。"""
    chunks = _load_chunks(force)
    vectors = _chunk_vectors(force)
    qv = embed_query(q)
    # 纯 Python 内积；块数量小（几十个），无需引入 numpy/FAISS
    scored = sorted(
        zip((sum(a * b for a, b in zip(qv, vec)) for vec in vectors), chunks, strict=True),
        key=lambda pair: pair[0],
        reverse=True,
    )
    return [
        {
            "score": round(score, 4),
            "source": c.source,
            "h1": c.h1,
            "h2": c.h2,
            "content": c.content,
            # 与生产链路同口径：parent_key 即“同一父块”的判定依据
            "parent_id": c.parent_key,
        }
        for score, c in scored[:top_k]
    ]

@router.get("/chunks")
async def list_chunks(
    source: str | None = Query(default=None, description="按文件名过滤，如 returns.md"),
    strategy: str | None = Query(default=None, description="强制统一策略（对照用）；不传则按文件策略"),
    limit: int = Query(default=5, le=50),
):
    """查看切块结果：总块数、长度分布、抽样内容。用于人工检查切块质量。"""
    if strategy is not None and strategy not in _STRATEGIES:
        raise HTTPException(status_code=422, detail=f"未知切分策略: {strategy}，可选: {sorted(_STRATEGIES)}")
    chunks = _load_chunks(strategy)
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
    strategy: str | None = Query(default=None, description="强制统一策略；不传则按文件策略"),
    top_k: int = Query(default=3, le=10),
):
    """内存版对照检索：不经过 Milvus，用于验证 Milvus 链路口径是否一致。"""
    if strategy is not None and strategy not in _STRATEGIES:
        raise HTTPException(status_code=422, detail=f"未知切分策略: {strategy}，可选: {sorted(_STRATEGIES)}")
    return {
        "query": q,
        "backend": "memory",
        "results": _memory_search(strategy, q, top_k),
    }

# ---- 按文件切分策略管理（step-30）----

class StrategyRequest(BaseModel):
    """按文件设置切分策略：strategy=None 恢复后缀默认。"""

    path: str = Field(min_length=1, max_length=256, description="文件名，如 returns.md")
    strategy: str | None = Field(default=None, description="header / paragraph / fixed；空则恢复默认")

@router.get("/strategies")
async def get_strategies():
    """查看目录内每个文件的切分策略（含默认值与覆盖状态）。

    对照展示「默认策略/覆盖策略」两列：覆盖值重建后生效，默认值
    由后缀决定（.md → header，其余 → paragraph）。
    """
    from pathlib import Path

    from app.rag.splitter import _default_strategy

    overrides = kb_store.get_strategies()
    files = [
        {
            "path": f.name,
            "default": _default_strategy(f.suffix.lower()),
            "override": overrides.get(f.name),
            "effective": overrides.get(f.name) or _default_strategy(f.suffix.lower()),
        }
        for f in sorted(Path(KB_DIR).iterdir())
        if f.is_file()
    ]
    return {"files": files, "stats": kb_store.stats()}

@router.post("/strategies")
async def set_strategy(body: StrategyRequest):
    """设置某文件的切分策略覆盖（下次重建生效）。

    只改配置不重建：避免调试时误触发全量重建；改完提示调用 /rebuild。
    """
    try:
        kb_store.set_strategy(body.path, body.strategy)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    clear_caches()
    return {
        "status": "ok",
        "path": body.path,
        "strategy": body.strategy,
        "hint": "策略已保存，需 POST /debug/kb/rebuild 重建后生效",
    }

@router.get("/split_compare")
async def split_compare(
    strategies: str = Query(default="header,paragraph,fixed", description="逗号分隔的切分策略"),
    q: str = Query(default="", description="对比用的查询串；为空只看切块统计"),
    top_k: int = Query(default=3, le=10),
):
    """切分策略对比工具（step-17）：策略效果难以凭直觉判断，用数据说话。

    对比模式下强制全目录用同一策略（force），与生产“按文件策略”口径独立。
    每个策略返回：切块统计（数量/均长/最长）+ 同一查询的内存检索 top_k。
    """
    wanted = [s.strip() for s in strategies.split(",") if s.strip()]
    unknown = [s for s in wanted if s not in _STRATEGIES]
    if unknown:
        raise HTTPException(status_code=422, detail=f"未知切分策略: {unknown}，可选: {sorted(_STRATEGIES)}")

    report = []
    for strategy in wanted:
        chunks = _load_chunks(strategy)  # 对比模式：强制全目录同一策略
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
    """触发知识库蓝绿全量重建（改了语料/切分逻辑/策略后调用，无需重启服务）。"""
    kb_store.ensure_table()
    count = store.rebuild_index(KB_DIR)
    clear_caches()
    return {"status": "ok", "chunks": count}

def register_debug_routes(app) -> None:
    """按配置开关注册调试路由（生产环境关闭）。"""
    if get_settings().debug:
        app.include_router(router)
