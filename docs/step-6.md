# Step 6 — LangGraph 单 Agent + Postgres Checkpointer

## 目标

Agent 核心落地：tool calling 检索知识库 + 会话记忆持久化。

## 图结构（`app/agents/graph.py`）

```
START → agent ⇄ tools → agent → ... → END
                │
                └─ transfer_to_human（步骤 7 细化状态机）
```

- **agent 节点**：LLM 绑定 `search_kb` / `transfer_to_human` 两个工具，
  每次注入系统提示词（不进历史，避免 Checkpointer 膨胀）
- **tools 节点**：`ToolNode` 按 tool_calls 自动分发执行
- **条件边**：`tools_condition` 判断 LLM 是否要调工具，形成 agent⇄tools 循环

## 关键设计决策

1. **工具 docstring 即提示词**：LLM 靠 docstring 理解何时调用，
   写工具等于在写一段"给模型看的文档"
2. **messages 用 `add_messages` reducer**：新消息追加而非覆盖，
   这是多轮记忆的基础机制
3. **Checkpointer = PostgresSaver**：每次图执行后完整状态入 Postgres，
   同一 `thread_id` 的请求自动恢复历史——**多轮记忆框架全托管**，
   不需要手工截断/拼接历史消息
4. **系统提示词约束幻觉**：强制"基于知识库回答、没有就转人工"，
   为异常处理迭代打底

## Postgres 侧变化

Checkpointer 首次 `setup()` 自动建 4 张表：
`checkpoints` / `checkpoint_blobs` / `checkpoint_writes` / `checkpoint_migrations`。

## 验证

```bash
uv run python scripts/chat_repl.py
# 测试多轮记忆：
#   你: 会员金卡有什么权益
#   小舒: ...92折会员价、生日礼券...
#   你: 上面说的第一项是什么意思
#   小舒: 应能解释"92折会员价"（依赖上一轮上下文）
```

## 遗留问题

- `transfer_to_human` 目前只返回确认文案，状态机在步骤 7 补全
- 端到端测试需要 .env 配置真实 LLM
