"""FastAPI 应用入口。

职责边界：main.py 只负责"组装"（注册路由、管理生命周期），
业务逻辑全部在 api/agents/rag 模块里——保持入口薄，方便测试。
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.agents import runtime
from app.api.auth import router as auth_router
from app.api.chat import router as chat_router
from app.api.debug_routes import register_debug_routes
from app.api.routes import agent_router, router
from app.api.ws import router as ws_router
from app.core.config import get_settings

# 日志基础配置：生产部署时由 gunicorn/uvicorn 参数或环境变量接管更细粒度的配置，
# 这里保证"无论谁启动进程，异常都能以统一格式落到 stderr"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


async def _queue_sweeper():
    """排队超时回退的周期任务。

    为什么不用 DB 定时器/独立进程：MVP 保持"单进程自足"，
    多 worker 部署时每个进程都跑这个任务是幂等 UPDATE，重复执行无害。
    同步 psycopg 放 to_thread 执行，避免阻塞事件循环。
    """
    from app.agents import handoff
    from app.core.config import get_settings

    while True:
        try:
            await asyncio.to_thread(handoff.sweep_timed_out)
        except asyncio.CancelledError:
            raise  # 关闭信号必须继续向上传播
        except Exception:
            # DB 抖动不能杀死巡检循环：记日志，下个周期重试
            logger.exception("排队超时回退任务失败，下个周期重试")
        await asyncio.sleep(get_settings().queue_sweep_interval_sec)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期钩子。

    启动时打印关键配置（不打印 Key！），方便确认 .env 是否生效。
    Milvus / Postgres 的连接采用"懒加载"：首次用到才建连，
    避免启动顺序耦合（比如先起后端再起中间件也不报错）。
    """
    s = get_settings()
    logger.info("LLM: model=%s base_url=%s", s.llm_model, s.llm_base_url)
    logger.info("Milvus: %s | Postgres: %s:%s", s.milvus_uri, s.postgres_host, s.postgres_port)
    # 可观测性初始化（未配置 Key 时完全 no-op，不影响启动）
    from app.core import tracing

    tracing.init()
    # 后台巡检：排队超时的会话自动回退 active（间隔可配）
    sweeper = asyncio.create_task(_queue_sweeper())
    yield
    # 关闭：先停巡检任务，再释放 Agent 的 Checkpointer 连接池
    sweeper.cancel()
    try:
        await sweeper
    except asyncio.CancelledError:
        pass
    # close() 是协程，必须 await：早先漏了 await，连接池从未被真正关闭
    await runtime.close()
    # Langfuse 是批量异步上报，不 flush 会丢掉缓冲区里的 span
    tracing.shutdown()


app = FastAPI(
    title="aide-agent",
    description="智能客服 Agent：知识库 RAG + 人工转接",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(router)
app.include_router(auth_router)
app.include_router(agent_router)
app.include_router(chat_router)
# WebSocket：双端实时推送（/ws/chat 用户端、/ws/agent 坐席端）
app.include_router(ws_router)
register_debug_routes(app)  # 调试路由：debug=False 时不注册

# CORS：前端(5173)与后端(8000)端口不同源，浏览器会拦截跨域 fetch。
# 白名单走配置（cors_origins）：WebSocket 握手也要校验 Origin，
# 两处共用同一份列表，避免"fetch 放行但 WS 被拒"这类不一致
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origin_list,
    allow_methods=["*"],
    allow_headers=["*"],
)
