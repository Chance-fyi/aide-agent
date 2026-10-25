"""同步 Postgres 连接池（业务表用）。

**为什么需要这个模块**

原先 `handoff` / `users` / `kb_store` 三处各自 `psycopg.connect(...)`，
**每次 DB 调用都新建一条 TCP 连接**。实测一轮对话（不含 LLM）会触发
**14 次**同步 DB 往返，累计阻塞事件循环约 82 ms。

两个后果：

1. **连接数爆炸**：14 条新连接/轮次。Postgres 默认 `max_connections=100`，
   几十个用户同时对话就能把库连满（然后所有请求一起失败）。
2. **事件循环被卡住**：这些调用是同步的，在 `async def` 里直接跑，
   期间**所有** WebSocket 连接与其他 HTTP 请求都停摆。

本模块解决第 1 点（复用连接，把建连开销摊到一次）。第 2 点的完整解法是
全链路异步，改动面很大，见 docs/step-47.md 的遗留项；用上连接池后单次
往返降到百微秒级，阻塞已经不再是主要瓶颈。

**为什么与 LangGraph 的异步池分开**

`app/agents/runtime.py` 里的 `AsyncConnectionPool` 是给 Checkpointer 用的
（异步链路）。这里的同步池服务的是业务表读写（转接状态、会话记录、
知识库元数据），两者生命周期和调用模型不同，硬合并会让"异步池绑定的
事件循环"污染同步调用方。

**`open=False` + 懒打开**

不在 import 时建连：模块导入可能发生在没有数据库的环境（如只跑
`ruff`、或单测只测纯函数），建连会直接炸。首次真正用到时才打开。
"""

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg_pool import ConnectionPool

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_pool: ConnectionPool | None = None
# 保护懒初始化：多个工作线程可能同时首次取连接
_lock = threading.Lock()

# 池大小：业务表操作都是毫秒级短查询，不需要大池。
# 取 8 与 LangGraph 异步池的 max_size=10 同量级——
# 两个池合计不超过 Postgres 默认 max_connections(100) 的零头
_POOL_MIN = 1
_POOL_MAX = 8
# 等连接超时：拿不到就说明并发真的打满了，快速失败比无限等好
_POOL_TIMEOUT = 5.0


def _build_pool() -> ConnectionPool:
    """创建连接池（不打开）。

    `autocommit=True` 与原先 `psycopg.connect(..., autocommit=True)` 保持一致：
    业务表的写操作都是单语句，不需要显式事务；需要事务的地方
    （如迁移、蓝绿重建）自己用 `conn.transaction()` 显式声明。
    """
    s = get_settings()
    return ConnectionPool(
        conninfo=s.postgres_dsn,
        min_size=_POOL_MIN,
        max_size=_POOL_MAX,
        # 不在构造时建连：见模块文档
        open=False,
        kwargs={"autocommit": True},
        # 池内连接空闲超过该时长会被回收，避免被数据库/中间设备静默断开后复用死连接
        max_idle=300,
    )


def get_pool() -> ConnectionPool:
    """取进程级连接池（懒创建 + 懒打开，线程安全）。"""
    global _pool
    if _pool is None:
        with _lock:
            if _pool is None:
                pool = _build_pool()
                try:
                    pool.open(wait=False)
                except Exception:
                    # 打不开时仍然返回池对象：后续 acquire 会给出真实错误，
                    # 这样"数据库不可用"的报错点落在业务调用处（带上下文），
                    # 而不是 import 期间一个莫名其妙的异常
                    logger.warning("同步连接池打开失败，首次使用时会重试", exc_info=True)
                _pool = pool
    return _pool


@contextmanager
def get_conn() -> Iterator[psycopg.Connection]:
    """借一条连接，用完归还（替代原先的 `psycopg.connect(...)`）。

    用法与原先完全一致：

        with _get_conn() as conn:
            conn.execute(...)

    出池时 `with` 块正常结束或抛异常都会归还连接；异常时 psycopg_pool
    会检查连接是否还能用，坏连接直接丢弃而不是塞回池里。
    """
    pool = get_pool()
    with pool.connection(timeout=_POOL_TIMEOUT) as conn:
        yield conn


def close() -> None:
    """关闭连接池（应用退出时调用）。失败只记日志，不阻断退出。"""
    global _pool
    if _pool is not None:
        try:
            _pool.close()
        except Exception:
            logger.warning("同步连接池关闭异常", exc_info=True)
        _pool = None


def stats() -> dict:
    """池状态快照（/health 用，排查"连接是不是被用光了"）。"""
    pool = _pool
    if pool is None:
        return {"open": False}
    try:
        return {
            "open": True,
            "size": pool.get_stats().get("pool_size"),
            "available": pool.get_stats().get("pool_available"),
            "waiting": pool.get_stats().get("requests_waiting"),
        }
    # 统计接口只是排障辅助，任何异常都不该让 /health 变红
    except Exception:  # noqa: BLE001
        return {"open": True, "stats_error": True}
