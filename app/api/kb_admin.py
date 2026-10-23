"""知识库管理接口（正式路由，生产可用）。

**为什么单独建一套而不是复用 /debug/kb**

`debug_routes` 只在 `DEBUG=true` 时注册，而生产环境必须关掉它。
结果是：生产环境没有任何入口能更新知识库——只能改文件后重启服务，
或者临时开 debug（等于把一堆内部调试接口暴露出去）。

这套 `/kb` 路由是"正式管理能力"：要求坐席登录，始终可用。
两者分工：
- `/kb/*`   → 管理操作（看状态、改策略、重建、验证检索），需要登录
- `/debug/*`→ 开发期诊断工具（内存版对照检索、策略对比），仅 DEBUG

**重建为什么是异步任务**

全量重建实测 24 秒。同步接口会撞代理超时、前端无法显示进度、
用户可能重复点击。所以 `POST /kb/rebuild` 立刻返回任务，
前端轮询 `GET /kb/rebuild` 看进度。
"""

import asyncio
import logging
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.deps import AgentUser
from app.core.config import get_settings
from app.rag import kb_store, rebuild, store
from app.rag.splitter import _STRATEGIES, _default_strategy

logger = logging.getLogger(__name__)

KB_DIR = "data/knowledge_base"

router = APIRouter(prefix="/kb", tags=["kb"])

def _kb_path() -> Path:
    return Path(KB_DIR)

# ============================================================
# 概览
# ============================================================

@router.get("/overview")
async def overview(agent: AgentUser):
    """知识库整体状态：文件数、块数、向量集合、最近重建、是否需要重建。

    一次返回管理界面首屏需要的全部信息，避免前端串行发 4 个请求。
    """
    s = get_settings()
    stats = await asyncio.to_thread(kb_store.stats)
    files = await asyncio.to_thread(kb_store.file_stats)

    # 语料是否比索引新：决定要不要提示"建议重建"。
    # 用文件 mtime 与 kb_document.updated_at 比，是唯一可靠的判断依据
    # （语料是文件系统上的事实，索引是数据库里的快照）
    indexed = {f["path"]: f["indexed_at"] for f in files}
    pending: list[str] = []
    for f in sorted(_kb_path().iterdir()) if _kb_path().exists() else []:
        if not f.is_file():
            continue
        mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=None).astimezone()
        got = indexed.get(f.name)
        if got is None:
            pending.append(f.name)          # 新文件还没入库
            continue
        if mtime > datetime.fromisoformat(got):
            pending.append(f.name)          # 文件改过但没重建

    try:
        collection = await asyncio.to_thread(store.collection_info)
    except Exception:
        # Milvus 不可用不应让整个管理页打不开：把错误作为状态展示
        logger.warning("查询向量集合信息失败", exc_info=True)
        collection = {"error": "向量库不可用，请检查 Milvus 是否启动"}

    snap = await asyncio.to_thread(rebuild.job.snapshot)
    return {
        "stats": stats,
        "file_count": len(files),
        "pending_rebuild": pending,
        "collection": collection,
        "rebuild": snap["job"],
        "rebuild_running": snap["running"],
        "config": {
            "split_strategy": s.kb_split_strategy,
            "retrieval_top_k": s.retrieval_top_k,
            "min_score": s.retrieval_min_score,
            "smoke_queries": s.rebuild_smoke_queries,
        },
    }

# ============================================================
# 文档列表
# ============================================================

@router.get("/documents")
async def list_documents(agent: AgentUser):
    """文件清单：每个文件的切分策略、块数、是否落后于语料。"""
    indexed = {f["path"]: f for f in await asyncio.to_thread(kb_store.file_stats)}
    out = []
    for f in sorted(_kb_path().iterdir()) if _kb_path().exists() else []:
        if not f.is_file():
            continue
        info = indexed.get(f.name)
        mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=None).astimezone()
        out.append(
            {
                "path": f.name,
                "size": f.stat().st_size,
                "modified_at": mtime.isoformat(),
                "default_strategy": _default_strategy(f.suffix.lower()),
                "override_strategy": info["strategy"] if info else None,
                "effective_strategy": (info["strategy"] if info else None)
                or _default_strategy(f.suffix.lower()),
                "chunks": info["chunks"] if info else 0,
                "parents": info["parents"] if info else 0,
                "indexed_at": info["indexed_at"] if info else None,
                # 没有索引记录 / 语料比索引新 → 需要重建
                "stale": info is None or mtime > datetime.fromisoformat(info["indexed_at"]),
            }
        )
    return {"documents": out, "strategies": sorted(_STRATEGIES)}

@router.get("/documents/chunks")
async def document_chunks(
    agent: AgentUser,
    path: str = Query(..., description="文件名，如 returns.md"),
    limit: int = Query(default=5, ge=1, le=50),
):
    """查看某文件的切块内容：判断「切得对不对」的唯一可靠办法。"""
    if "/" in path or "\\" in path or path.startswith("."):
        # 纵深防御：只允许目录内的纯文件名，杜绝路径穿越读任意文件
        raise HTTPException(status_code=422, detail="path 只能是文件名")
    if not (_kb_path() / path).is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    chunks = await asyncio.to_thread(kb_store.recent_chunks, path, limit)
    return {"path": path, "chunks": chunks}

# ============================================================
# 切分策略
# ============================================================

class StrategyRequest(BaseModel):
    path: str = Field(min_length=1, max_length=256)
    strategy: str | None = Field(default=None, description="header/paragraph/fixed；空则恢复默认")

@router.post("/documents/strategy")
async def set_strategy(body: StrategyRequest, agent: AgentUser):
    """设置单个文件的切分策略覆盖（重建后生效）。

    只改配置不自动重建：策略调整往往是批量操作，
    自动重建会让每次点击都等 24 秒，且容易在中途重建出半成品。
    """
    if "/" in body.path or "\\" in body.path:
        raise HTTPException(status_code=422, detail="path 只能是文件名")
    if not (_kb_path() / body.path).is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    try:
        await asyncio.to_thread(kb_store.set_strategy, body.path, body.strategy)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except Exception:
        logger.exception("保存切分策略失败 path=%s", body.path)
        raise HTTPException(status_code=503, detail="保存失败，请稍后重试") from None
    logger.info("切分策略已更新 path=%s strategy=%s by=%s",
                body.path, body.strategy, agent["display_name"])
    return {"status": "ok", "hint": "策略已保存，需执行重建后生效"}

# ============================================================
# 重建
# ============================================================

@router.post("/rebuild", status_code=202)
async def start_rebuild(agent: AgentUser):
    """触发全量重建（异步）：立刻返回任务，前端轮询 /kb/rebuild 看进度。"""
    try:
        job = await rebuild.job.start(agent["display_name"])
    except rebuild.RebuildBusyError:
        # 409 而不是 400：请求本身没问题，是当前状态不允许
        raise HTTPException(status_code=409, detail="已有重建任务在进行中，请等待完成") from None
    return {"status": "accepted", "job": job}

@router.get("/rebuild")
async def rebuild_status(agent: AgentUser):
    """最近一次重建的状态（前端轮询，直到 running 结束）。

    用 snapshot() 一次取回两个字段：分两次查会让后台任务在中间完成，
    返回 `job.status=running` + `running=false` 的矛盾响应。
    """
    return await asyncio.to_thread(rebuild.job.snapshot)

# ============================================================
# 检索验证
# ============================================================

@router.post("/search")
async def test_search(
    agent: AgentUser,
    q: str = Query(..., min_length=1, max_length=200, description="查询串"),
    top_k: int = Query(default=3, ge=1, le=10),
    min_score: float | None = Query(
        default=None, ge=0, le=1,
        description="相似度阈值；不传用配置值，传 0 看全量分数分布",
    ),
):
    """用**生产同一条链路**验证检索效果（改完语料/策略后自检）。

    特意不提供"内存版检索"：那属于调试工具（/debug/kb/memory_search）。
    管理界面要回答的问题是"线上检索好不好"，不是"另一种实现好不好"。
    """
    try:
        results = await asyncio.to_thread(store.search, q, top_k, None, min_score)
    except RuntimeError as exc:
        # 知识库还没建：这是明确的状态，不是故障
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except Exception:
        logger.exception("检索验证失败 q=%s", q)
        raise HTTPException(status_code=503, detail="检索暂时不可用，请稍后重试") from None
    return {
        "query": q,
        "results": [
            {
                "score": round(r["score"], 4),
                "source": r["source"],
                "h1": r["h1"],
                "h2": r["h2"],
                "content": r["parent_content"],
            }
            for r in results
        ],
    }
