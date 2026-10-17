# Step 7 — 转接状态机 + 关键词兜底

## 目标

人工转接从"玩具"变"机制"：会话级状态落库、规则兜底、状态守卫。

## 状态机（`app/agents/handoff.py`）

```
active ──触发转接──> pending_human ──坐席接入(后续迭代)──> in_service
```

MVP 落地前两态。存储用独立 Postgres 表 `conversation_status`
（thread_id 主键 + status + reason + 时间戳）而非 Checkpointer 状态：
转接状态是**业务事实**（坐席端/管理后台要查），Checkpointer 状态是
**Agent 执行快照**（框架内部用），生命周期不同，不该混放。

## 图结构变化（`app/agents/graph.py`）

```
START ── route_input（入口条件路由）──┬─ transfer ─> END（统一话术）
                                      └─ agent ⇄ tools ─> END
```

`route_input` 三级判断（顺序即优先级）：

1. **状态守卫**：会话已 pending_human → 直接回复等待话术，
   不再进 agent（避免转接后机器人继续自说自话）
2. **关键词兜底**：命中 `.env` 的 `TRANSFER_KEYWORDS` → 跳过 LLM 直接转接，
   既防模型漏判，也省一次 LLM 调用
3. **正常消息** → agent

## 双保险设计

- LLM 自主判断：`transfer_to_human` 工具（模型按语义决定）
- 规则兜底：关键词路由（确定性行为）

工具里的 `config: RunnableConfig` 参数是关键细节：
ToolNode 自动注入运行时配置（不占 LLM 参数位），从中取 thread_id 落库。

## 验证（无需真实 LLM 即可测转接链路）

```
关键词兜底 -> 您的工单已提交，人工客服正在接入，请稍候。
DB 状态   -> pending_human
守卫回复   -> 您的工单已提交，人工客服正在接入，请稍候。
```

## 遗留问题

- 坐席端（查看转接会话/坐席回复）在后续迭代
- 超时回退（pending_human 超过 N 分钟无坐席接入 → 回退 active）后续迭代
