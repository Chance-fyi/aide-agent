"""Agent 运行时管理：图与 Checkpointer 的生命周期。

设计：
- 异步连接池（AsyncConnectionPool）+ AsyncPostgresSaver：
  FastAPI 是异步框架，整条链路（连接池 → Checkpointer → ainvoke）
  保持异步才不会阻塞事件循环。同步 PostgresSaver 搭配 ainvoke
  会在 aget_tuple 处直接 NotImplementedError
- 图实例全局唯一（懒加载单例），避免 /chat 每请求重建
- FastAPI lifespan 关闭时释放连接池（main.py 调用 close()）
"""

import asyncio
import logging

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.state import CompiledStateGraph
from psycopg_pool import AsyncConnectionPool

from app.agents import handoff
from app.agents.graph import build_graph_with_memory
from app.core.config import get_settings

logger = logging.getLogger(__name__)

_lock = asyncio.Lock()
_pool: AsyncConnectionPool | None = None
_graph: CompiledStateGraph | None = None

async def get_graph() -> CompiledStateGraph:
    """懒加载单例：首次调用时建连接池、Checkpointer、建表、编译图。

    加锁防并发初始化。连接池 open=False + 显式 wait()：
    等 Postgres 真正可用再继续，失败会带明确报错。
    """
    global _pool, _graph, _checkpointer
    if _graph is None:
        async with _lock:
            if _graph is None:  # 双重检查
                s = get_settings()
                _pool = AsyncConnectionPool(
                    conninfo=s.postgres_dsn,
                    max_size=10,
                    open=False,
                )
                await _pool.open(wait=True, timeout=10)  # 显式打开并等到至少一个连接就绪
                _checkpointer = AsyncPostgresSaver(_pool)
                await _checkpointer.setup()   # 幂等建 Checkpointer 表
                handoff.ensure_table()        # 幂等建转接状态表
                from app.core.users import ensure_table as ensure_user_table
                ensure_user_table()           # 幂等建用户账号表（登录体系）
                from app.rag import kb_store
                kb_store.ensure_table()       # 幂等建知识库文档/切块表（父块存储）
                _graph = build_graph_with_memory(_checkpointer)
                logger.info("Agent 图已初始化（AsyncPostgresSaver）")
    return _graph


async def close() -> None:
    """应用关闭时释放连接池。"""
    global _pool, _graph, _checkpointer
    if _pool is not None:
        try:
            await _pool.close()
        except Exception:
            # 关闭失败只记日志，不阻断进程退出
            logger.warning("连接池关闭异常", exc_info=True)
    _pool = None
    _graph = None
    _checkpointer = None
