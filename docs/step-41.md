# step-41：数据库迁移机制（消除 DROP TABLE 数据事故风险）

## 目标

把散落在三处的建表逻辑收成一套版本化迁移，并**彻底删掉 `DROP TABLE` 重建逻辑**。

## 为什么这是 P0

改动前，三个 `ensure_table()` 都带着同一句约定：

```python
# app/agents/handoff.py
try:
    conn.execute("SELECT user_id, agent_id FROM conversation_status LIMIT 1")
except (psycopg.errors.UndefinedColumn, psycopg.errors.UndefinedTable):
    conn.execute("DROP TABLE IF EXISTS conversation_status")   # ← 这里
```

后果很直接：**上线后只要加一列，用户聊天记录就全没了**。

而 `chat_message` 存的不只是聊天内容——它是：

- 用户端跨设备恢复历史的唯一数据源
- 坐席端查看"用户以前问过什么"的依据
- 审计与排障的事实来源

删一次就是不可恢复的数据事故。这属于"平时不报错、一上生产就出事"的典型，
所以排在上线加固的第一位。

## 方案

### 为什么不用 Alembic

Alembic 要 `alembic.ini` + `env.py` + autogenerate 一整套，还要接管连接管理。
本项目只有 6 张业务表，需要的只是"按序执行、只执行一次、可查当前版本"。
手写 runner（约 130 行）更贴合现状，也少一个依赖。

代价：没有 downgrade、没有自动 diff。真需要时再换——对外接口
（版本号 + 有序列表 + `run()`）不变，换实现不影响调用方。

### 结构

```
app/core/migrations.py
  Migration(version, name, sql)       # 一次迁移
  MIGRATIONS = (Migration(1, "baseline", _BASELINE_SQL), ...)   # 只追加
  run() -> list[int]                  # 应用待执行项，返回实际执行的版本
  status() -> dict                    # {current, latest, applied, pending}
  _main()                             # CLI：python -m app.core.migrations [--status]
```

`schema_migrations` 表记录已应用版本：

```sql
CREATE TABLE schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
```

### 基线写成幂等的（关键设计）

v1 基线不是纯 `CREATE TABLE`，而是 `CREATE TABLE IF NOT EXISTS` +
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS` 补齐每一列：

```sql
CREATE TABLE IF NOT EXISTS conversation_status (...);
ALTER TABLE conversation_status ADD COLUMN IF NOT EXISTS agent_id   BIGINT;
ALTER TABLE conversation_status ADD COLUMN IF NOT EXISTS resolution TEXT;
ALTER TABLE conversation_status ADD COLUMN IF NOT EXISTS user_id    BIGINT;
...
```

这样它能同时服务两种场景：

| 场景 | 行为 |
|---|---|
| 全新库 | 一路建出所有表 |
| **老开发库**（表已存在、缺列、无迁移记录） | 补齐缺列，**一行数据都不动** |

如果基线写成纯 `CREATE TABLE`，老库会被记成"已应用"但缺的列永远补不上——
那才是最难排查的状态（报错在业务代码里，根因在 schema 里）。

v2 起才是真正的增量 `ALTER`。

### 三个工程细节

**1. 每条迁移独立事务**

```python
with conn.transaction():
    conn.execute(m.sql)
    conn.execute("INSERT INTO schema_migrations ...")
```

DDL 与版本记录同生共死。否则会出现"表建好了但没记账"（下次重复执行）
或"记了账但没建表"（永久缺表）。

**2. advisory lock 串行化多 worker**

多副本同时启动时，两个进程都会读到"待应用"，然后一起 `ALTER`——
轻则报错，重则建出半截结构。

```python
conn.execute("SELECT pg_advisory_lock(%s)", (_LOCK_KEY,))
```

**3. 失败必须炸出来**

```python
except Exception:
    logger.critical("迁移失败 v%d %s —— 已回滚，服务不应继续启动", ...)
    raise
```

静默跳过 = 服务带着错误 schema 启动，之后报错的位置离真正原因非常远。

### 执行时机：启动时 + CLI

- **启动时**（`main.prepare_schema`）：保证"服务可访问 = schema 已就绪"
- **CLI**（`python -m app.core.migrations`）：生产发布流程先迁移再切流量

`AUTO_MIGRATE` 配置开关：

| 值 | 行为 |
|---|---|
| `true`（默认） | 启动时自动应用待执行迁移 |
| `false` | 不自动改 schema，只校验版本；落后打 **CRITICAL** 日志 |

多副本部署建议 `false` + 发布流程里跑 CLI，避免"新代码带着旧 schema 跑起来"的窗口期。

启动失败**不阻断启动**（只告警）：Postgres 可能比后端起得晚，
硬失败会让"先起后端再起中间件"这个开发习惯直接不可用。
`get_graph()` 里保留了一次迁移调用，首次真正用到图时会重试并抛出。

### 顺手做的一致性

`handoff.ensure_table` / `users.ensure_table` / `kb_store.ensure_table`
保留函数名（测试与 `scripts/create_agent.py` 仍在调用），但实现改为
`migrations.run()`。注释里写明"不要再往这里加 DDL"。

知识库三张表虽然是可从文件重建的派生数据，也一并纳入迁移——
**所有 DDL 只有一处来源**，才不会出现"某张表没人管"。

## 验证

### 迁移测试（`tests/test_migrations.py`，8 例）

全部用**临时库**（`temp_db` fixture 建库 → 测 → 删库），跑真实 DDL 而非 mock。

| 测试 | 锁住什么 |
|---|---|
| `test_fresh_database_creates_all_tables` | 空库一路建齐 7 张表 |
| `test_run_is_idempotent` | 重复执行不重复应用、不报错 |
| `test_adopts_existing_old_schema_without_data_loss` | **核心场景**：老库（缺 `agent_id`/`user_id` 列）被接管后列补齐、老数据完好、老状态不被重置 |
| `test_status_reports_pending_then_applied` | 部署流程能据此判断 |
| `test_migration_versions_are_unique_and_ascending` | 版本号契约自检 |
| `test_failed_migration_rolls_back_and_raises` | 失败回滚不留半截表、不记版本、修好后能继续 |
| `test_startup_warns_loudly_when_schema_behind` | `AUTO_MIGRATE=false` + 落后 → CRITICAL |
| `test_startup_does_not_block_when_db_unreachable` | 库连不上也不崩启动 |

`test_adopts_existing_old_schema_without_data_loss` 是最有价值的一条：
它直接复现了"旧实现会删库"的场景，并证明新实现不丢一行数据。

### 真实环境验证

```
$ uv run python -m app.core.migrations --status
当前版本 v0 / 最新 v1
待应用: [1]

$ uv run python -m app.core.migrations
已应用迁移: [1]

$ uv run python -m app.core.migrations --status
当前版本 v1 / 最新 v1
待应用: （无）

$ uv run python -m app.core.migrations
schema 已是最新，无需迁移
```

老数据完好（迁移前后 `chat_message` 1924 行，`schema_migrations` 只有 v1）：

```
业务表: [app_user, chat_message, checkpoint_*, conversation_status,
         kb_chunk, kb_document, kb_parent, schema_migrations]
chat_message 行数: 1924
无残留半截表: True
```

`uv run fastapi dev` 启动 → `/health` 200 → `POST /chat` 正常转接 →
`scripts/create_agent.py` 建号成功。`AUTO_MIGRATE=false` 启动同样正常。

### 相关测试

`test_migrations` + `test_handoff` + `test_kb_store` + `test_api` + `test_ws`：
**59 passed**；`ruff check .` clean。

## 变更文件

| 文件 | 变更 |
|---|---|
| `app/core/migrations.py` | **新增**：版本表、基线（幂等）、runner、advisory lock、CLI |
| `app/agents/handoff.py` | 删掉 `DROP TABLE` 与全部 DDL，`ensure_table` 转调迁移 |
| `app/core/users.py` | 同上 |
| `app/rag/kb_store.py` | 同上 |
| `app/agents/runtime.py` | 三处 `ensure_table` → 一次 `migrations.run()`（`to_thread`） |
| `app/main.py` | 新增 `prepare_schema()`，lifespan 调用 |
| `app/core/config.py` | 新增 `auto_migrate` |
| `tests/test_migrations.py` | **新增**：8 例，全部跑真实 DDL（临时库） |
| `.env.example` / `README.md` / `AGENTS.md` | 迁移命令、`AUTO_MIGRATE` 说明、新增第 8 条硬约束 |

## 踩的坑（值得记下来）

**测试污染了真实开发库。** 第一版 `test_startup_warns_loudly_when_schema_behind`
用 `TestClient(app)` 跑完整 lifespan，并 monkeypatch `app.core.config.get_settings`。
但 `main.py` 是 `from app.core.config import get_settings`——patch 源模块不影响
已绑定的引用，于是 lifespan 用了**真实配置**（`auto_migrate=True`），
把测试里那条假的 `Migration(999, "future", "SELECT 1;")` 应用到了开发库。

发现方式：`status()` 报出 `current: 999, latest: 1`。

处理：

1. `DELETE FROM schema_migrations WHERE version > 1` 清理
2. 把启动逻辑抽成 `main.prepare_schema(s)`——lifespan 保持薄，逻辑可单测
3. 测试改为直接调 `prepare_schema(fake)` + `temp_db`，**不再起 TestClient**

教训写进了 `AGENTS.md`：迁移类测试必须用临时库，且要 patch 使用方模块。

## 遗留问题

- **没有 downgrade**。误迁移只能手工写反向 SQL。表结构简单时够用，
  但复杂变更（如拆表）前应重新评估是否换 Alembic。
- **`chat_message.user_id` 是 `TEXT`，`conversation_status.user_id` 是 `BIGINT`**。
  这是本步发现的历史不一致——目前不影响功能（查询都按 `thread_id` 关联），
  但类型不统一迟早咬人。修正需要数据转换迁移，单独一步做。
- **基线幂等只对"加列"有效**。如果将来要改列类型/删列，`ADD COLUMN IF NOT EXISTS`
  帮不上，必须写真正的增量迁移并考虑数据回填。
- **`prepare_schema` 的启动失败只告警**。多副本 + `AUTO_MIGRATE=false` 是推荐姿势，
  但若有人两个都开着且 Postgres 恰好不可用，服务会带着旧 schema 起来。
  建议生产用 `AUTO_MIGRATE=false` 彻底消除这个窗口。
- **迁移没有"演练"模式**。理想是 `--dry-run` 打印将要执行的 SQL，
  让人在应用前看一眼。当前只能靠 `--status` 看版本。
