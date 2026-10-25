"""pytest 全局配置。

**为什么关掉 Langfuse**

测试不该连外部服务。`.env` 里配了 Langfuse Key 时，SDK 会在导出时反复重试
`localhost:3000`（失败后指数退避），结果是：测试变慢、日志被
"Transient error ... Connection refused" 刷满，真正的问题反而看不见。

必须在任何 `app.*` 模块导入**之前**设置环境变量——`get_settings()` 是
`lru_cache` 的，一旦读过 `.env` 就固定了。conftest 由 pytest 最先导入，
正好是这个时机。
"""

import os

# 必须在导入 app.* 之前：pydantic-settings 优先读环境变量，覆盖 .env 的值
os.environ["LANGFUSE_ENABLED"] = "false"

# 测试里不要把开发库当成"可以随便改 schema"的库：
# 迁移由 tests/test_migrations.py 用临时库单独验证，
# 其余测试不应触发 schema 变更（见 docs/step-41.md 的踩坑记录）
os.environ.setdefault("AUTO_MIGRATE", "false")

import pytest
from pytest import MonkeyPatch
from starlette.testclient import TestClient

from app.core.users import ensure_table
from app.main import app


async def _noop_sweeper() -> None:
    """替代真实的排队超时巡检：不 sleep、不查库，保持测试环境干净。"""
    return

@pytest.fixture(scope="module")
def ws_client():
    """模块级共享一个 TestClient（WS 测试需要同一事件循环内的两条连接）。

    必须用 `with TestClient(...)` 让 portal（事件循环）活到整个模块：
    用户端与坐席端两条连接要互相推送，跨事件循环操作同一个 asyncio.Queue
    属于未定义行为。

    生命周期里顺手关掉两个与推送无关的副作用：
    - tracing：即使 LANGFUSE_ENABLED 已被关掉，也再兜一层
    - 队列超时巡检：后台 UPDATE 会偷偷改掉别的用例造的数据
    """
    mp = MonkeyPatch()
    mp.setattr("app.core.tracing.init", lambda: False)
    mp.setattr("app.core.tracing.shutdown", lambda: None)
    mp.setattr("app.main._queue_sweeper", _noop_sweeper)
    ensure_table()
    with TestClient(app) as client:
        yield client
    mp.undo()

@pytest.fixture
async def client():
    """REST 客户端：ASGITransport 直连 app，**不起 lifespan**。

    为什么不起 lifespan：lifespan 会跑数据库迁移与事件总线，
    测试不该改开发库 schema、也不该额外占用 PG 连接。
    """
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c

@pytest.fixture(scope="session", autouse=True)
def _close_db_pool_at_exit():
    """会话结束时关闭同步连接池。

    为什么需要：psycopg_pool 的 ConnectionPool 在 __del__ 里 join 内部线程，
    而解释器退出阶段不允许 join → 会刷出
    "PythonFinalizationError: cannot join thread at interpreter shutdown"。
    测试用 ASGITransport 不跑 lifespan，所以不会有人替我们关它。
    """
    yield
    from app.core import db

    db.close()
