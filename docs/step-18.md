# Step 18 — 删除旧版本兼容代码

## 目标

项目处于开发期、未上线，历史遗留的兼容逻辑只有维护成本没有收益。
统一清除"兼容旧版本"代码，开发期约定：**升级 = 手动重建/迁移，不留过渡代码**。

## 删除内容

### 1. handoff.py：`pending_human` 遗留状态

- 删 `STATUS_LEGACY_PENDING` 常量及 `_LIVE_STATUSES` 中引用
- `agent_close` 的 `WHERE status IN (...)` 从三态减回两态
- `sweep_timed_out` 回退条件从 `IN (queued, pending_human)` 简化为 `= queued`

### 2. store.py：v0 裸集合名回落

- 删 `_legacy_collection_name()`（v0 单集合固定名）
- `_resolve_collection` 不再回落裸集合名：别名不存在 = 未入库，返回 None 报错

### 3. graph.py：父块兼容回退

- `h.get("parent_content") or h["content"]` → `h["parent_content"]`
  （schema v2 起该字段必存在，静默回退只会掩盖数据问题）

### 4. 测试配套

- 删 `test_legacy_pending_human_treated_as_live`
- test_store 的 schema fixture 从"缺字段自动重建"改为 fail-fast
  + 提示手动 `bump KB_SCHEMA_VERSION + POST /debug/kb/rebuild`
- `test_search_kb_normal_hits_include_source` 的 fake_hits 补 `parent_content`
  字段，并断言上下文取父块而非子块

### 5. 顺带修正：full_flow 测试的确定性

原 step-5 步骤用真实 LLM 全链路验证"closed 后新消息回自助"，
LLM 偶发调用转人工工具会让断言变 queued 而失败（本次全量跑实锤）。
改为关键词消息（"再给我转人工"）：
守卫放行 → 关键词兜底重新转接 → queued，全程不碰 LLM，100% 确定。

教训：**冒烟测试不要依赖真实 LLM 的决策结果**，LLM 是概率组件，
测试要么 mock 要么选不经过它的路径。

## 保留的"兼容"（非本步范围）

- `mark_pending_human` 函数名：语义仍是"标记等待人工"（动作名，非状态名）
- 蓝绿重建中的"旧版本集合"：指当前线上版本，是蓝绿机制本身
- OpenAI 兼容接口、`message.text()` 等：厂商兼容/框架 API，不是旧版本兼容

## 验证

```bash
uv run pytest          # 59 passed（删 1 个 legacy 用例）
uv run ruff check .    # 0 issues
```

## 遗留问题

- 无（本步为纯清理）
