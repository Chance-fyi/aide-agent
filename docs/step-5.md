# Step 5 — Milvus 建集合 + 入库 + search_kb

## 目标

向量检索落地：知识库入 Milvus，提供正式检索函数 `store.search()`。

## 关键实现（`app/rag/store.py`）

- **用 pymilvus 原生 SDK 而非 langchain-milvus 封装**：
  schema 显式可控——`content/source/h1/h2` 元数据对检索结果的
  溯源与前端展示必不可少
- **metric 用 IP**：BGE 归一化向量下内积即余弦相似度（与内存版口径一致）
- **索引 AUTOINDEX**：standalone 下自动选索引类型，开发环境免调参
- **全量重建策略**：`rebuild_index()` = drop + 重建 + 插入 + flush。
  文档量小，全量重建比增量同步简单可靠，
  规避"文档改了旧块没删净"的一致性问题

## 踩坑记录

### 坑：insert 成功但检索不到数据

`client.insert()` 返回 `insert_count=31`，但
`get_collection_stats()` 显示 `row_count=0`，`search()` 返回空。

**根因**：Milvus 是最终一致性，新插入数据在 growing segment 中，
未 flush 前检索不可见。

**修复**：insert 后显式 `client.flush(COLLECTION_NAME)`。

## 验证

```python
from app.rag import store
print(store.rebuild_index("data/knowledge_base"))  # 31
store.search("怎么退货")  # 命中 returns.md/退货流程，score 0.7724
```

与步骤 4 的内存检索对照：同查询同分数（如 "发货慢怎么办" → 0.6348），
证明 Milvus 检索路径与内存实现口径完全一致。

## 有意思的边界案例

"发货慢怎么办" 命中的是 faq.md/如何修改订单地址（0.635），
因为知识库没有专门的"发货延迟"条目。这个案例保留：
它就是后续"检索低分降级转人工"迭代的真实素材。

## 启动入口

pyproject 已配置 `[tool.fastapi] entrypoint`，开发启动统一用：

```bash
uv run fastapi dev
```
