"""数据库迁移测试。

为什么这些用例要用**临时库**而不是主库：迁移的本质就是"改变 schema"，
在主库上跑等于拿真实数据做实验。临时库用完即删，测的是真实 DDL 行为
（不是 mock），又不会污染开发数据。

三个必须验证的场景：
1. 全新库：一路建出所有表
2. 老库采纳（adoption）：表已存在但缺列/无迁移记录 → 补齐缺列，**不删数据**
3. 幂等：重复执行不重复应用
"""

import uuid

import psycopg
import pytest

from app.core import migrations
from app.core.config import get_settings

# 业务表清单：迁移跑完后必须全部存在
BUSINESS_TABLES = {
    "app_user",
    "chat_message",
    "conversation_status",
    "kb_document",
    "kb_parent",
    "kb_chunk",
    "schema_migrations",
}


def _admin_dsn() -> str:
    """连到 postgres 维护库（建/删库用）。"""
    return get_settings().postgres_dsn.rsplit("/", 1)[0] + "/postgres"


def _dsn_for(dbname: str) -> str:
    return get_settings().postgres_dsn.rsplit("/", 1)[0] + "/" + dbname


@pytest.fixture
def temp_db(monkeypatch):
    """建一个空库，把 migrations 的连接指向它；用完删掉。"""
    name = f"aide_mig_test_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')

    dsn = _dsn_for(name)
    # migrations.run() 内部用 get_settings()，这里替换成指向临时库的副本
    fake = get_settings().model_copy(update={"postgres_db": name})
    monkeypatch.setattr(migrations, "get_settings", lambda: fake)

    try:
        yield dsn
    finally:
        # 强制断开可能残留的连接再删库，否则 DROP DATABASE 会报"正在被访问"
        with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            conn.execute(f'DROP DATABASE IF EXISTS "{name}"')


def _tables(dsn: str) -> set[str]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        ).fetchall()
    return {r[0] for r in rows}


def _columns(dsn: str, table: str) -> set[str]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
            (table,),
        ).fetchall()
    return {r[0] for r in rows}


# 期望的"全部版本"从 MIGRATIONS 推导，而不是硬编码 ——
# 否则每加一条迁移就要改测试，且容易漏改（本步真的踩到）
ALL_VERSIONS = [m.version for m in migrations.MIGRATIONS]
LATEST = max(ALL_VERSIONS)

def test_fresh_database_creates_all_tables(temp_db):
    """空库跑迁移：所有业务表一次建齐。"""
    applied = migrations.run()
    assert applied == ALL_VERSIONS, "空库应一次应用全部迁移"
    assert BUSINESS_TABLES <= _tables(temp_db)


def test_run_is_idempotent(temp_db):
    """重复执行不重复应用、不报错。"""
    assert migrations.run() == ALL_VERSIONS
    assert migrations.run() == [], "第二次不应再应用任何迁移"

    with psycopg.connect(temp_db, autocommit=True) as conn:
        n = conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
    assert n == len(ALL_VERSIONS)


def test_adopts_existing_old_schema_without_data_loss(temp_db):
    """老库采纳：表已存在、缺列、没有迁移记录 → 补齐缺列且保留数据。

    这是本步要解决的核心风险场景。旧实现遇到这种情况会 `DROP TABLE` 重建，
    把用户聊天记录全删掉。基线写成幂等（CREATE IF NOT EXISTS +
    ADD COLUMN IF NOT EXISTS）之后，同样的库能被"接管"而不丢一行数据。
    """
    # 造一个 step-29 之前的 conversation_status（没有 agent_id/user_id 等列）
    # 和一条 chat_message 老数据
    with psycopg.connect(temp_db, autocommit=True) as conn:
        conn.execute("""
            CREATE TABLE conversation_status (
                thread_id  TEXT PRIMARY KEY,
                status     TEXT NOT NULL DEFAULT 'active',
                reason     TEXT,
                agent_name TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        conn.execute("""
            CREATE TABLE chat_message (
                id         BIGSERIAL PRIMARY KEY,
                thread_id  TEXT NOT NULL,
                role       TEXT NOT NULL,
                content    TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        conn.execute(
            "INSERT INTO chat_message (thread_id, role, content) VALUES (%s, %s, %s)",
            ("old-thread", "user", "这条老消息不能丢"),
        )
        conn.execute(
            "INSERT INTO conversation_status (thread_id, status) VALUES (%s, %s)",
            ("old-thread", "queued"),
        )

    assert migrations.run() == ALL_VERSIONS

    # 缺的列被补上
    assert {"agent_id", "user_id", "resolution"} <= _columns(temp_db, "conversation_status")
    assert {"user_id", "sender_id"} <= _columns(temp_db, "chat_message")

    # 老数据完好，且老状态没被重置
    with psycopg.connect(temp_db, autocommit=True) as conn:
        content = conn.execute("SELECT content FROM chat_message").fetchone()[0]
        status = conn.execute("SELECT status FROM conversation_status").fetchone()[0]
    assert content == "这条老消息不能丢"
    assert status == "queued"


def test_status_reports_pending_then_applied(temp_db):
    """status() 能准确报出待应用/已应用，供部署流程判断。"""
    before = migrations.status()
    assert before == {"current": 0, "latest": LATEST, "applied": [], "pending": ALL_VERSIONS}

    migrations.run()

    after = migrations.status()
    assert after == {"current": LATEST, "latest": LATEST, "applied": ALL_VERSIONS, "pending": []}


def test_migration_versions_are_unique_and_ascending():
    """迁移列表本身的自检：版本号唯一且严格递增。

    版本号是对外契约（schema_migrations 里存的就是它），
    重复或乱序会让"哪些还没跑"的判断出错。
    """
    versions = [m.version for m in migrations.MIGRATIONS]
    assert versions == sorted(versions)
    assert len(versions) == len(set(versions))
    assert versions[0] == 1, "基线必须是 v1"


def test_failed_migration_rolls_back_and_raises(temp_db, monkeypatch):
    """迁移失败必须回滚并抛出，不能静默跳过。

    静默跳过 = 服务带着错误 schema 启动，之后报错的位置离真正原因非常远
    （比如"查询缺列"报在业务代码里，而根因是迁移没跑）。所以这里要求：
    - 异常向上抛（阻断启动）
    - 该版本的 DDL 回滚（不留半截结构）
    - 不写 schema_migrations 记录（下次还会重试）
    """
    # 版本号取 LATEST+1：用固定值会和真实迁移撞号（本步新增 v2 时撞过）
    broken = migrations.Migration(
        version=LATEST + 1, name="broken", sql="CREATE TABLE half_made (id INT); SELECT 1/0;"
    )
    # 先存下原始列表：后面要再 patch 一次，不能用已 patch 过的 MIGRATIONS
    # 去拼（那样会把 broken 再追加一遍，导致第二次仍然失败）
    original = migrations.MIGRATIONS
    monkeypatch.setattr(migrations, "MIGRATIONS", (*original, broken))

    with pytest.raises(psycopg.errors.DivisionByZero):
        migrations.run()

    tables = _tables(temp_db)
    assert "half_made" not in tables, "失败的迁移必须整条回滚，不能留半截表"

    with psycopg.connect(temp_db, autocommit=True) as conn:
        applied = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    assert [r[0] for r in applied] == ALL_VERSIONS, "失败的那条不能记成已应用"

    # 修好之后应该能继续应用（说明失败没把库搞坏）
    monkeypatch.setattr(
        migrations, "MIGRATIONS",
        (*original, migrations.Migration(LATEST + 1, "fixed", "CREATE TABLE ok_table (id INT);")),
    )
    assert migrations.run() == [LATEST + 1]
    assert "ok_table" in _tables(temp_db)


async def test_startup_warns_loudly_when_schema_behind(temp_db, monkeypatch, caplog):
    """AUTO_MIGRATE=false 且 schema 落后时，启动必须打 CRITICAL。

    为什么要求"大声"：带着旧 schema 跑业务，报错会出现在离根因很远的
    业务代码里（"查询缺列"），运维很难联想到是迁移没跑。

    这里直接调 `main.prepare_schema`（temp_db 已把 migrations 的连接指向临时库），
    不走 TestClient —— 起 TestClient 会跑完整 lifespan 连真实库，
    测试一旦写错就会把假迁移应用到开发数据上（本步真的踩到过）。
    """
    import logging

    from app.main import prepare_schema

    # 先让临时库到最新版本，再塞一条永远"待应用"的迁移
    migrations.run()
    monkeypatch.setattr(
        migrations, "MIGRATIONS",
        (*migrations.MIGRATIONS, migrations.Migration(LATEST + 100, "future", "SELECT 1;")),
    )

    fake = get_settings().model_copy(update={"auto_migrate": False})
    with caplog.at_level(logging.CRITICAL):
        await prepare_schema(fake)

    assert any("schema 落后" in r.message and r.levelno == logging.CRITICAL
               for r in caplog.records), "schema 落后时启动日志必须有 CRITICAL 警告"


async def test_startup_does_not_block_when_db_unreachable(monkeypatch, caplog):
    """数据库连不上时启动不应崩：只记日志，交给首次请求时重试。

    这是"先起后端再起中间件"这个开发习惯的保障；也是生产上
    Postgres 短暂不可用时不让整个服务起不来的底线。
    """
    import logging

    from app.main import prepare_schema

    fake = get_settings().model_copy(
        update={"auto_migrate": True, "postgres_port": 1}  # 1 端口必然连不上
    )
    # 必须同时改 migrations 的连接配置：它内部自己调 get_settings()，
    # 只改传给 prepare_schema 的那份，run() 仍会连真实库并成功
    monkeypatch.setattr(migrations, "get_settings", lambda: fake)
    with caplog.at_level(logging.ERROR):
        await prepare_schema(fake)   # 不应抛异常

    assert any("启动时迁移失败" in r.message for r in caplog.records)
