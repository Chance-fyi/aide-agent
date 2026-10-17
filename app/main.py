"""FastAPI 应用入口。

职责边界：main.py 只负责"组装"（注册路由、管理生命周期），
业务逻辑全部在 api/agents/rag 模块里——保持入口薄，方便测试。
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.agents import runtime
from app.api.chat import router as chat_router
from app.api.debug_routes import register_debug_routes
from app.api.routes import router

# 日志基础配置：生产部署时由 gunicorn/uvicorn 参数或环境变量接管更细粒度的配置，
# 这里保证"无论谁启动进程，异常都能以统一格式落到 stderr"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期钩子。

    启动时打印关键配置（不打印 Key！），方便确认 .env 是否生效。
    Milvus / Postgres 的连接采用"懒加载"：首次用到才建连，
    避免启动顺序耦合（比如先起后端再起中间件也不报错）。
    """
    from app.core.config import get_settings

    s = get_settings()
    logger.info("LLM: model=%s base_url=%s", s.llm_model, s.llm_base_url)
    logger.info("Milvus: %s | Postgres: %s:%s", s.milvus_uri, s.postgres_host, s.postgres_port)
    yield
    # 关闭：释放 Agent 的 Checkpointer 连接池
    runtime.close()


app = FastAPI(
    title="aide-agent",
    description="智能客服 Agent：知识库 RAG + 人工转接",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(router)
app.include_router(chat_router)
register_debug_routes(app)  # 调试路由：debug=False 时不注册

# CORS：前端(5173)与后端(8000)端口不同源，浏览器会拦截跨域 fetch。
# 开发环境放行本地前端端口；生产应改为具体域名列表（通过环境变量注入）
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)
