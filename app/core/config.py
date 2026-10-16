"""全局配置模块。

用 pydantic-settings 统一管理配置：
- 所有配置项都可被 .env / 环境变量覆盖（厂商零硬编码的关键）
- 类型校验在启动时就完成，避免"跑一半才发现配置错"
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # 告诉 pydantic 去 .env 文件读配置；extra="ignore" 容忍 .env 里的多余项
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ----- LLM（OpenAI 兼容接口，任何厂商只需改 .env） -----
    llm_base_url: str
    llm_api_key: str
    llm_model: str

    # ----- Embedding（本地 BGE，无需 Key） -----
    embedding_model: str = "BAAI/bge-small-zh-v1.5"

    # ----- Postgres（会话记忆 Checkpointer） -----
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "aide"
    postgres_password: str = "aide_secret"
    postgres_db: str = "aide_agent"

    # ----- Milvus -----
    milvus_uri: str = "http://localhost:19530"

    # ----- 业务参数 -----
    history_max_rounds: int = 10
    retrieval_top_k: int = 3
    # 强制转人工关键词：LLM 判断之外的第二道保险（步骤 7 使用）
    transfer_keywords: str = "转人工,人工客服,投诉"

    # 调试接口开关：生产环境置 false，调试路由不注册
    debug: bool = True

    @property
    def postgres_dsn(self) -> str:
        """LangGraph Checkpointer 需要的连接串格式。"""
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def transfer_keyword_list(self) -> list[str]:
        """把逗号分隔的关键词拆成列表，方便直接做匹配。"""
        return [kw.strip() for kw in self.transfer_keywords.split(",") if kw.strip()]


# @lru_cache：整个进程只创建一次 Settings，
# 避免每次 import / 调用都重新读文件、做校验
@lru_cache
def get_settings() -> Settings:
    return Settings()
