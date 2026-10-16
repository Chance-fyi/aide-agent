"""FastAPI 应用入口。

职责边界：main.py 只负责"组装"（注册路由、管理生命周期），
业务逻辑全部在 api/agents/rag 模块里——保持入口薄，方便测试。
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期钩子。

    启动时打印关键配置（不打印 Key！），方便确认 .env 是否生效。
    Milvus / Postgres 的连接采用"懒加载"：首次用到才建连，
    避免启动顺序耦合（比如先起后端再起中间件也不报错）。
    """
    from app.core.config import get_settings

    s = get_settings()
    print(f"[startup] LLM: model={s.llm_model} base_url={s.llm_base_url}")
    print(f"[startup] Milvus: {s.milvus_uri} | Postgres: {s.postgres_host}:{s.postgres_port}")
    yield
    # 关闭阶段的清理动作（Checkpointer 连接池等）在步骤 6 接入后补充


app = FastAPI(
    title="aide-agent",
    description="智能客服 Agent：知识库 RAG + 人工转接",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(router)
