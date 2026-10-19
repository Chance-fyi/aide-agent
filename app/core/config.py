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
    # 知识库检索别名：调用方永远只认别名，版本切换时别名原子改指向
    kb_alias: str = "kb_chunks"
    # 集合 schema 版本号：切分逻辑/表结构变化时手动 +1，触发新版本集合
    # v3：父块迁到 Postgres（父块不入 Milvus，改存 chunk_id 引用回查）
    kb_schema_version: int = 3
    # embedding 向量维度：换 embedding 模型时必须同步改（bge-small-zh=512，bge-base-zh=768）
    embedding_dim: int = 512
    # 知识库切分策略：header（按标题）/ paragraph（按段落聚合）/ fixed（固定窗口）。
    # 切换策略需同步 bump KB_SCHEMA_VERSION 后重建，否则新旧块口径不一致
    kb_split_strategy: str = "header"
    # 蓝绿重建的冒烟测试查询（逗号分隔）：新数据必须能命中这些核心业务问题才准上线
    rebuild_smoke_queries: str = "退货,退款,发货"

    # ----- 业务参数 -----
    history_max_rounds: int = 10
    retrieval_top_k: int = 3
    # 检索相似度阈值：低于此分数的命中视为"不相关"直接丢弃。
    # BGE-small-zh 余弦相似度：语义相关通常 >0.5，无关内容多在 0.2~0.3。
    # 低分片段喂给 LLM 是幻觉的主要诱因，宁可少检不可错检
    retrieval_min_score: float = 0.4
    # 强制转人工关键词：LLM 判断之外的第二道保险（步骤 7 使用）
    transfer_keywords: str = "转人工,人工客服,投诉"
    # 人工排队超时（分钟）：超过后排队会话自动回退 active，用户可继续自助问答。
    # 太久没人接的排队态若一直卡住，用户只能反复看"坐席接入中"死话术
    transfer_queue_timeout_min: float = 30
    # 排队超时巡检周期（秒）：多 worker 重复执行无害（幂等 UPDATE）
    queue_sweep_interval_sec: int = 60

    # 调试接口开关：生产环境置 false，调试路由不注册
    debug: bool = True

    # ----- 登录认证 -----
    # Token 签名密钥：生产环境必须通过 .env 覆盖为随机长字符串
    auth_secret: str = "dev-secret-change-me"

    # 输入防护开关：注入检测 + 违禁词（guardrails.py）。
    # 关闭后完全跳过规则检查——仅供本地调试，生产必须开启
    input_guardrail: bool = True
    # 违禁词追加表（逗号分隔），与内置默认表叠加，业务无需改代码即可增删
    blocked_words: str = ""

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
