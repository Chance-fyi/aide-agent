# Step 10 — pytest 测试

## 目标

MVP 收官：核心链路自动化测试，`uv run pytest` 一键回归。

## 测试矩阵（17 个用例，4 个文件）

| 文件 | 覆盖 | 依赖 |
|---|---|---|
| `test_splitter.py` | 标题切块：标题保留、h2 分块、超长兜底二次切、真实语料加载 | 无 |
| `test_handoff.py` | 状态机：幂等建表、默认状态、标记转接、重复标记更新原因 | Postgres |
| `test_store.py` | 检索：语义命中冒烟（退货→returns.md）、分数降序、schema 完整性、集合不存在报错 | Milvus + BGE |
| `test_api.py` | 接口集成：参数校验 422（空/超长）、关键词转接全流程、历史过滤空消息、404 | Postgres |

分层原则：**纯逻辑不碰外部服务，集成测试只测确定路径**。
agent 的 LLM 路径不确定性高，不进冒烟测试（留给评估集迭代）。

## 测试驱动出的真实改进

`test_search_empty_collection_raises` 需要 `store.search()` 支持
`collection_name` 参数——补上了（测试倒逼 API 设计的例子）。

## 顺带修复

- `ruff check` 全绿：import 排序、未使用导入、`try/except/pass` 改为记日志
- pytest 命令行输出确认：`17 passed`

## 运行

```bash
podman compose up -d          # 测试前确保中间件在线
uv run pytest                 # 17 passed
uv run ruff check .           # 0 issues
```
