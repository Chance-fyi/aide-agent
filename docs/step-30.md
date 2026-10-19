# step-30：知识库存储重构（父块入 Postgres + 按文件切分策略）

## 目标

1. 文件路径与切分策略存 PostgreSQL（可按文件独立配置）
2. 父块从 PostgreSQL 检索（Milvus 只存向量 + 子块 + 引用）
3. 不再全局统一一种切分策略，不同文件按结构选合适策略

## 实现要点

### 存储分工（新模块 `app/rag/kb_store.py`）

| 存储 | 内容 | 理由 |
|---|---|---|
| Postgres `kb_document` | 文件路径、切分策略覆盖 | 文档管理元数据，关系库擅长 |
| Postgres `kb_chunk` | 子块、**父块全文**、h1/h2，`doc_path` 外键 CASCADE | 父块与小节正文大量重复，放 Milvus 浪费且难同步 |
| Milvus（schema **v3**） | embedding、content、`chunk_id` 引用、h1/h2 | 只做向量检索 |

### 检索链路（父子检索两段式）

`store.search`：Milvus 向量召回（带 chunk_id）→ 低分过滤 → `_attach_parents` 按 chunk_id **批量主键回查 Postgres** 取父块。回查是 top_k 级点查，毫秒级；chunk_id 查不到时回退用子块正文当父块（防御重建切换窗口的不一致）。

### 按文件切分策略

优先级：`kb_document.strategy` 覆盖 > 后缀默认（`.md → header`，其余 `→ paragraph`）。
`splitter.split_file(path, strategy)` 单文件入口供重建流程逐文件调用。

### 一致性保证（重建流程）

Postgres 写入（拿 chunk_id）与 Milvus staging 构建、冒烟测试绑在**同一个事务**：
Milvus 失败 → Postgres 回滚，两侧永远一致；冒烟通过 → 提交事务 → 切别名。
蓝绿语义与 v2 相同，旧版本在切换前始终在线。

## 新增/变更接口（debug=True 时）

- `GET /debug/kb/strategies` — 每个文件的默认/覆盖/生效策略
- `POST /debug/kb/strategies` — 按文件设置策略覆盖（`strategy: null` 恢复默认），重建后生效
- `/chunks`、`/memory_search`、`/split_compare` 支持 `strategy` 参数强制统一策略做对照
- `KB_SCHEMA_VERSION` 2 → 3（**需要重建**：`POST /debug/kb/rebuild`）

## 验证

- `uv run pytest`：71 passed（新增 `tests/test_kb_store.py` 5 例）
- 重建后检索验证：31 块 / 10 文档，「怎么退货」命中 returns.md/refund.md
- 策略覆盖端到端：faq.md 覆盖为 fixed 后 1 块（默认 header 4 块），恢复后回到默认

## 遗留问题

- `/split_compare` 对纯文本文件强制 fixed 时复用 markdown 的 fixed 路径，语义可再细分
- Milvus staging 构建在 Postgres 事务内进行，语料极大量时会拉长事务持锁时间（当前 31 块无感知，量大后可改为 staging 表方案）
- `kb_split_strategy` 全局配置项已不再是生产口径的唯一来源，可考虑在后续步骤移除
