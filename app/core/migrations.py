"""数据库 schema 迁移。

**为什么需要这一层**

在此之前，建表逻辑散落在 `handoff.ensure_table` / `users.ensure_table` /
`kb_store.ensure_table` 三处，且都带一句"schema 不一致就 DROP TABLE 重建"。
那在开发期无所谓，但 `chat_message` 存的是用户真实聊天记录、坐席看的历史、
审计依据——**上线后一次加列就会把用户数据全删掉**。这属于"不报错但一上生产
就是事故"的那类问题。

**为什么不用 Alembic**

Alembic 要 alembic.ini + env.py + autogenerate 一整套，还要接管连接管理；
本项目的表结构很小（6 张业务表），需要的只是"按序执行、只执行一次、可查当前版本"。
手写一个 100 行的 runner 更贴合现状，也少一个依赖。
代价是没有 downgrade 和自动 diff——真需要时再换，接口（版本号 + 有序列表）不变。

**LangGraph 的 checkpoint_* 表不在这里管**：`AsyncPostgresSaver.setup()` 自带
`checkpoint_migrations` 表，框架自己负责，我们不要重复管。

**基线（v1）刻意写成幂等的**

`CREATE TABLE IF NOT EXISTS` + `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`。
这样它能同时服务两种场景：
1. 全新库：一路建出来
2. 老开发库（表已存在、但没有 schema_migrations 记录）：补齐缺的列，不删任何东西

如果基线用纯 `CREATE TABLE`，老库会被记成"已应用"但缺列永远不会补——
那才是最难排查的状态。后续迁移（v2 起）才是真正的增量 ALTER。
"""

import logging
from dataclasses import dataclass

import psycopg

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# 迁移期间使用的 advisory lock key（任意固定值）。
# 为什么要锁：多 worker 同时启动时会并发跑迁移，两个进程都读到"待应用"，
# 然后一起执行 ALTER —— 轻则报错，重则建出半截结构。
# pg_advisory_lock 是会话级锁，拿不到就阻塞等待，天然串行化。
_LOCK_KEY = 0x6169_6465  # "aide"

# ---- 基线 v1：当前全部业务表（幂等）----

_BASELINE_SQL = """
-- 会话转接状态：状态机的唯一事实源（坐席端队列、用户端状态都查它）
CREATE TABLE IF NOT EXISTS conversation_status (
    thread_id  TEXT PRIMARY KEY,          -- 会话 ID（与 Checkpointer thread_id 同源）
    status     TEXT NOT NULL DEFAULT 'active',
    reason     TEXT,                      -- 转接原因（阈值/关键词/用户诉求）
    agent_name TEXT,                      -- 接入坐席昵称（展示用）
    agent_id   BIGINT,                    -- 接入坐席账号 ID（权限校验的唯一依据）
    resolution TEXT,                      -- 坐席处理结果
    user_id    BIGINT,                    -- 会话归属用户（匿名会话为空）
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- 老库补列（新库上是 no-op）：IF NOT EXISTS 让基线可重复执行
ALTER TABLE conversation_status ADD COLUMN IF NOT EXISTS reason     TEXT;
ALTER TABLE conversation_status ADD COLUMN IF NOT EXISTS agent_name TEXT;
ALTER TABLE conversation_status ADD COLUMN IF NOT EXISTS agent_id   BIGINT;
ALTER TABLE conversation_status ADD COLUMN IF NOT EXISTS resolution TEXT;
ALTER TABLE conversation_status ADD COLUMN IF NOT EXISTS user_id    BIGINT;
CREATE INDEX IF NOT EXISTS idx_conversation_user ON conversation_status (user_id);

-- 用户可见的会话记录：用户/智能客服/人工坐席的消息都落这里。
-- 不用 Checkpointer 当历史源的原因：坐席回复、纯转达消息不经过图执行写不进去
CREATE TABLE IF NOT EXISTS chat_message (
    id         BIGSERIAL PRIMARY KEY,
    thread_id  TEXT NOT NULL,
    role       TEXT NOT NULL,              -- user / agent(智能客服) / human(人工坐席)
    content    TEXT NOT NULL,
    user_id    TEXT,                       -- user 角色消息的发送者
    sender_id  BIGINT,                     -- human 消息的坐席（昵称 JOIN 得到）
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE chat_message ADD COLUMN IF NOT EXISTS user_id   TEXT;
ALTER TABLE chat_message ADD COLUMN IF NOT EXISTS sender_id BIGINT;
CREATE INDEX IF NOT EXISTS idx_chat_message_thread ON chat_message (thread_id, id);
CREATE INDEX IF NOT EXISTS idx_chat_message_user   ON chat_message (user_id);

-- 账号：role 区分用户端/坐席端
CREATE TABLE IF NOT EXISTS app_user (
    id            BIGSERIAL PRIMARY KEY,  -- 代理主键：其他表用它关联（改用户名不悬空）
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,          -- PBKDF2 哈希，绝不存明文
    role          TEXT NOT NULL,          -- user / agent
    display_name  TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 知识库：文档 → 父块（小节全文）→ 子块（检索单元）
CREATE TABLE IF NOT EXISTS kb_document (
    id         BIGSERIAL PRIMARY KEY,
    path       TEXT NOT NULL UNIQUE,      -- 相对语料目录的文件路径
    strategy   TEXT,                      -- 切分策略覆盖；NULL = 按后缀默认
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE kb_document ADD COLUMN IF NOT EXISTS strategy TEXT;

CREATE TABLE IF NOT EXISTS kb_parent (
    id       BIGSERIAL PRIMARY KEY,
    doc_path TEXT NOT NULL REFERENCES kb_document(path) ON DELETE CASCADE,
    content  TEXT NOT NULL,               -- 小节全文（父子检索的返回单元）
    h1       TEXT NOT NULL DEFAULT '',
    h2       TEXT NOT NULL DEFAULT ''
);
ALTER TABLE kb_parent ADD COLUMN IF NOT EXISTS h1 TEXT NOT NULL DEFAULT '';
ALTER TABLE kb_parent ADD COLUMN IF NOT EXISTS h2 TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS kb_chunk (
    id        BIGSERIAL PRIMARY KEY,
    parent_id BIGINT NOT NULL REFERENCES kb_parent(id) ON DELETE CASCADE,
    content   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kb_chunk_parent ON kb_chunk (parent_id);
"""

@dataclass(frozen=True)
class Migration:
    """一次迁移。

    version 必须严格递增且不复用——它是对外契约（schema_migrations 里存的就是它）。
    加迁移时**只追加**，不要改已发布的条目：线上库已经执行过旧版本了。
    """

    version: int
    name: str
    sql: str

# 迁移列表：只追加，不改历史。
# 改 schema 的流程：在末尾加一条 Migration(N+1, "...", "ALTER TABLE ...")
MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "baseline", _BASELINE_SQL),
)

def _ensure_version_table(conn: psycopg.Connection) -> None:
    """建迁移记录表（本身也必须幂等，否则第一次跑就没法记账）。"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)

def current_version(conn: psycopg.Connection) -> int:
    """当前库已应用到哪一版；空库/无记录返回 0。"""
    _ensure_version_table(conn)
    row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
    return row[0] if row else 0

def applied_versions(conn: psycopg.Connection) -> list[int]:
    """已应用的版本号列表（排障时看"到底跑过哪些"）。"""
    _ensure_version_table(conn)
    rows = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    return [r[0] for r in rows]

def run() -> list[int]:
    """应用所有未执行的迁移，返回本次实际执行的版本号。

    幂等：没有待应用的迁移时直接返回空列表。
    每条迁移在**独立事务**里执行，版本记录与 DDL 同生共死——
    避免"表建好了但没记账"导致下次重复执行（或反过来记了账但没建表）。
    """
    s = get_settings()
    applied: list[int] = []
    # autocommit=False：需要显式事务让 DDL 与版本记录原子提交
    with psycopg.connect(s.postgres_dsn, autocommit=False) as conn:
        # 会话级 advisory lock：多 worker 同时启动时串行化，谁先拿到谁先跑
        conn.execute("SELECT pg_advisory_lock(%s)", (_LOCK_KEY,))
        try:
            done = set(applied_versions(conn))
            conn.commit()

            for m in MIGRATIONS:
                if m.version in done:
                    continue
                try:
                    with conn.transaction():
                        conn.execute(m.sql)
                        conn.execute(
                            "INSERT INTO schema_migrations (version, name) VALUES (%s, %s)",
                            (m.version, m.name),
                        )
                except Exception:
                    # 迁移失败必须炸出来：静默跳过会让服务带着错误 schema 启动，
                    # 后续报错点会离真正原因非常远
                    logger.critical(
                        "迁移失败 v%d %s —— 已回滚，服务不应继续启动", m.version, m.name
                    )
                    raise
                applied.append(m.version)
                logger.info("已应用迁移 v%d %s", m.version, m.name)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))
            conn.commit()

    if not applied:
        logger.debug("schema 已是最新，无需迁移")
    return applied

def status() -> dict:
    """迁移状态快照（CLI / 排障用）。"""
    with psycopg.connect(get_settings().postgres_dsn, autocommit=True) as conn:
        done = applied_versions(conn)
    pending = [m.version for m in MIGRATIONS if m.version not in done]
    return {
        "current": max(done) if done else 0,
        "latest": max((m.version for m in MIGRATIONS), default=0),
        "applied": done,
        "pending": pending,
    }

def _main() -> None:
    """CLI 入口：`uv run python -m app.core.migrations [--status]`。

    为什么需要 CLI：生产部署应该"先迁移、再切流量"，而不是让每个 worker
    启动时各自去改 schema。配合 AUTO_MIGRATE=false 使用。
    """
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if "--status" in sys.argv:
        st = status()
        print(f"当前版本 v{st['current']} / 最新 v{st['latest']}")
        print(f"已应用: {st['applied'] or '（无）'}")
        print(f"待应用: {st['pending'] or '（无）'}")
        return

    applied = run()
    if applied:
        print(f"已应用迁移: {applied}")
    else:
        print("schema 已是最新，无需迁移")

if __name__ == "__main__":
    _main()
