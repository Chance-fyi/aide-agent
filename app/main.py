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
from app.api.kb_admin import router as kb_router
from app.api.routes import agent_router, router, stats_router
from app.api.ws import router as ws_router
from app.core import metrics, migrations
from app.core.config import get_settings
from app.core.hub import hub
from app.core.metrics import MetricsMiddleware

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


async def prepare_schema(s) -> None:
    """启动阶段准备数据库 schema（迁移或版本校验）。

    为什么单独成一个函数而不是写在 lifespan 里：这样能直接单测"schema 落后
    必须打 CRITICAL"这条承诺，不用为了测一行日志去起整个 TestClient
    （起 TestClient 会连真实库，测试一不小心就会改到开发数据）。

    失败策略：**只告警不阻断启动**。Postgres 可能比后端起得晚（开发期常见），
    这里硬失败会让"先起后端再起中间件"这个习惯直接不可用；
    get_graph() 里还有一次迁移，首次真正用到图时会重试并抛出。
    """
    if s.auto_migrate:
        try:
            applied = await asyncio.to_thread(migrations.run)
            if applied:
                logger.info("数据库迁移已应用: %s", applied)
        except Exception:
            logger.exception(
                "启动时迁移失败（不阻断启动，首次对话时会重试）。"
                "生产环境请先执行 uv run python -m app.core.migrations"
            )
        return

    # 关掉自动迁移时只校验版本：落后就大声警告。
    # 为什么必须 CRITICAL：带着旧 schema 跑业务，报错会出现在离根因很远的
    # 业务代码里（"查询缺列"），运维很难联想到是迁移没跑
    try:
        st = await asyncio.to_thread(migrations.status)
        if st["pending"]:
            logger.critical(
                "schema 落后：当前 v%d，待应用 %s（AUTO_MIGRATE=false，"
                "请先执行 uv run python -m app.core.migrations）",
                st["current"], st["pending"],
            )
    except Exception:
        logger.exception("迁移状态检查失败")

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

    await prepare_schema(s)

    # 预热 embedding 模型：首次加载 torch 权重实测约 19 秒，
    # 不预热就会让第一个用户等 20 秒（后续请求只要 0.02 秒）。
    # 放后台任务，不阻塞启动——否则启动时间也会多 20 秒
    async def _warmup():
        from app.rag import embedder

        await asyncio.to_thread(embedder.warmup)

    asyncio.create_task(_warmup())

    # 指标定时落盘：多 worker 下让各进程的计数能被合并到同一个 /metrics
    metrics_flusher = asyncio.create_task(metrics.flush_loop())

    # 跨进程事件总线：多 worker 部署时推送能跨进程送达。
    # 启动失败只降级（退回单进程），不让总线成为启动前提
    from app.core.bus import bus

    await bus.start(hub.deliver_remote)

    # 可观测性初始化（未配置 Key 时完全 no-op，不影响启动）
    from app.core import tracing

    tracing.init()
    # 后台巡检：排队超时的会话自动回退 active（间隔可配）
    sweeper = asyncio.create_task(_queue_sweeper())
    yield
    # 关闭：先停指标落盘与巡检任务
    metrics_flusher.cancel()
    try:
        await metrics_flusher
    except asyncio.CancelledError:
        pass

    # 再停事件总线，再释放 Agent 的 Checkpointer 连接池。
    # 总线要先停：它自己持有两条 Postgres 连接，不关会拖慢进程退出
    await bus.stop()
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
app.include_router(stats_router)
app.include_router(chat_router)
# 知识库管理：正式路由（生产可用，要求坐席登录），与 /debug/kb 分工不同
app.include_router(kb_router)
# WebSocket：双端实时推送（/ws/chat 用户端、/ws/agent 坐席端）
app.include_router(ws_router)
register_debug_routes(app)  # 调试路由：debug=False 时不注册

# 指标中间件：统计请求数与耗时。
# 放在 CORS 之前注册（add_middleware 是后进先出，这里要让它包住业务路由）
app.add_middleware(MetricsMiddleware)

# CORS：前端(5173)与后端(8000)端口不同源，浏览器会拦截跨域 fetch。
# 白名单走配置（cors_origins）：WebSocket 握手也要校验 Origin，
# 两处共用同一份列表，避免"fetch 放行但 WS 被拒"这类不一致
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origin_list,
    allow_methods=["*"],
    allow_headers=["*"],
)
