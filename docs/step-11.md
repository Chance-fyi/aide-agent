# Step 11 — 检索降级、低分过滤与拒答引导

## 目标

解决两个最影响生产可用性的问题：
1. **知识库故障拖垮对话**：Milvus 挂掉时整个 /chat 直接 503
2. **低相关片段诱发幻觉**：无论分数多低都返回 top_k，无关内容喂给 LLM 后被"硬答"

同时补强提示词，覆盖"无关/越权问题拒答 + 引导"。

## 实现要点

### 1. `search_kb` 三分支降级（app/agents/graph.py）

| 情况 | 工具返回 | LLM 行为 |
|---|---|---|
| store.search 抛异常（Milvus 故障/未建库） | "知识库暂时无法访问…请转接人工" | 致歉 + 引导转人工 |
| 无命中 / 全部低于阈值 | "没有相关内容…不要编造，可转人工" | 坦诚告知 + 转人工 |
| 正常命中 | 带来源标记的上下文 | 按知识回答 |

关键设计：**异常在工具层消化，以"系统提示"文本交还 LLM**。
ToolNode 收到字符串会作为 tool 结果回填，对话继续；
如果异常穿透 ToolNode，整个 graph 执行失败，用户只会看到 503。

### 2. 检索阈值过滤（app/rag/store.py + config）

- 新增配置 `RETRIEVAL_MIN_SCORE=0.4`（BGE-small-zh 余弦相似度：相关 >0.5，无关约 0.2~0.3）
- `store.search()` 增加 `min_score` 参数，返回前统一过滤；
  显式传 0 可看全量分数分布（调试用）
- 宁可"没检到"不可"检错了"——没检到会走转人工，检错了会编造

### 3. 提示词硬化（_SYSTEM_PROMPT 行为准则 6、7）

- 无关话题（闲聊/时事/医疗法律建议）：礼貌拒答 + 引导回售前售后或转人工
- 越权请求（刷单、站外交易、套取优惠）：明确拒绝，不展开讨论
- 强化依据声明：引用必须说依据，检索不到的细节一律不说

## 验证

```bash
uv run pytest          # 21 passed（新增 3 个 search_kb 降级用例 + 1 个阈值用例）
uv run ruff check .    # 0 issues
```

- `test_graph_tools.py`：monkeypatch 模拟故障/无命中/正常命中三分支（不依赖 Milvus）
- `test_store.py::test_search_min_score_filters_irrelevant`：真实 Milvus 上验证阈值行为

## 遗留问题

- 拒答目前靠提示词（LLM 自主判断），未做规则层前置拦截——输入防护在 step-13
- LLM 层的超时/重试异常分级在 step-12
