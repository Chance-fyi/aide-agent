# step-35：上下文窗口管理（让 history_max_rounds 真正生效）

## 目标

`history_max_rounds` 从 step-1 就写在 `config.py` 和 `.env.example` 里，
但**全项目零引用**——`agent_node` 把 `state["messages"]` 原封不动全塞给 LLM。

后果是确定性的，不是"可能有问题"：

- Checkpointer 的 `messages` 只增不减，第 N 轮的 token 成本 ∝ N
- 长会话（几十轮）必然撞上模型上下文上限，届时整个会话直接报错
- 而配置项写着 10，看起来"已经控制了"——这种"假配置"比没有更危险

本步把窗口管理落地，并加上"接线测试"防止它再次变成死配置。

## 实现要点

### 1. 在「human 消息边界」切窗口（`graph._window_messages`）

```python
def _window_messages(messages, max_rounds) -> list:
    # 从后往前数 human 消息，第 max_rounds 个的位置即窗口起点
    seen = 0
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].type == "human":
            seen += 1
            if seen == max_rounds:
                return list(messages[i:])
    return list(messages)   # 轮数不足上限：全量送出
```

**为什么按 human 消息切，而不是简单取最后 N 条**——这是本步最关键的设计点：

1. **绝不能出现孤立的 `ToolMessage`**。tool-calling 链路是
   `HumanMessage → AIMessage(tool_calls) → ToolMessage → AIMessage`，
   `tool_calls` 与 `ToolMessage` 必须成对。如果按条数硬切，
   很可能切出"没有对应 tool_calls 的 ToolMessage"，
   [OI] 兼容接口会直接返回 400，整个会话挂掉。
2. **窗口首条必须是 HumanMessage**。模型看到"从助手回答开始"的对话会混乱，
   不知道在回应什么。
3. 因为切点永远落在 human 消息上，**一轮内部的工具往返天然完整**。

### 2. 只裁"送给模型的窗口"，不动 state

```python
def agent_node(state, config):
    window = _window_messages(state["messages"], get_settings().history_max_rounds)
    if len(window) < len(state["messages"]):
        logger.info("上下文已裁剪 thread=%s %d→%d 条消息", ...)
    messages = [SystemMessage(content=_SYSTEM_PROMPT), *window]
    return {"messages": [llm_with_tools.invoke(messages)]}
```

裁剪结果**不写回 state**，所以：

- Checkpointer 保留完整历史 → 只有送给 LLM 的那一份是窗口
- 线上排查时可以重放“LLM 当时到底看到了什么”

这是"上下文管理"与"模型输入可追溯性"两件事的解耦。

> **重要区分（本步初稿曾写错）**：**坐席端看到的历史不来自 Checkpointer**。
> 坐席端与用户端读的是 `chat_message` 表（`app/api/chat.py` → `handoff.get_messages`），
> 由 API 层 `_save_messages` 写入，与 LangGraph state 无关。
> Checkpointer 只是 `chat_message` 为空时的**存量会话兜底**
> （`_messages_from_checkpointer`，step-20 之前的老会话，实测现有 167 个 thread 走这条路径）。
> 所以"删 messages 会让坐席看不到历史"是不成立的——产品侧历史与模型侧状态是两套数据。

### 3. 裁剪要留日志

不记日志就只能靠看账单猜它有没有生效——而这正是本步要修的 bug 的成因
（配置存在、没人读、也没人发现没人读）。

### 4. `<= 0` 表示不限制

调试时想看全量历史，不该被配置卡住。0 和负数都按"不限制"处理。

## 验证

### 接线测试（比纯函数测试更重要）

只测 `_window_messages` 是不够的——**本步的 bug 恰恰是"函数/配置都在，但没接上"**。
所以用假 LLM 验证构建出来的 agent 节点真的裁剪了：

```python
class _RecordingLLM:          # 记录每次收到的消息
    def bind_tools(self, tools): return self
    def invoke(self, messages, **kwargs):
        self.calls.append(list(messages)); return AIMessage(content="好的")

def test_agent_node_sends_only_recent_rounds(monkeypatch):
    monkeypatch.setenv("HISTORY_MAX_ROUNDS", "2")
    get_settings.cache_clear()          # lru_cache，改环境变量后必须清
    node({"messages": _rounds(5)}, {...})
    assert len(llm.calls[0]) == 5       # 1 条 system + 最近 2 轮
```

`tests/test_context_window.py` 共 10 例，覆盖：
轮数不足/恰好等于/超过上限、窗口首条必为 human、
**tool_calls 与 ToolMessage 不被拆散**、0/负数不限制、空列表、
不修改入参、agent 节点真的用了窗口、裁剪不裁掉系统提示。

### 真实 12 轮会话实测

```
[ 1/12] 怎么退货？        →  4 条消息
...
[10/12] 能换货吗？        → 40 条消息
INFO 上下文已裁剪 thread=ctxcheck-... 41→37 条消息
INFO 上下文已裁剪 thread=ctxcheck-... 43→39 条消息
[11/12] 怎么取消订单？    → 44 条消息
INFO 上下文已裁剪 thread=ctxcheck-... 45→37 条消息
INFO 上下文已裁剪 thread=ctxcheck-... 47→39 条消息
[12/12] 运费谁出？        → 48 条消息
```

关键观察：

- **第 11 轮起开始裁剪**（`HISTORY_MAX_ROUNDS=10`，前 10 轮不裁）
- **送给 LLM 的窗口稳定在 37/39 条，不再增长**（成本被钉住）
- **Checkpointer 里的消息继续增长**（40 → 44 → 48），完整历史保留

窗口在 37/39 之间交替，是因为部分轮次多一次工具往返（一轮 4 条 vs 6 条），
按 human 边界切的结果是"最近 10 个用户提问及其后续"，属预期。

### 其他

- `uv run pytest`：**116 passed**（新增 10 例）
- `uv run ruff check .`：clean
- 配置注释同步更新（`config.py` 与 `.env.example` 都说明"只影响送给模型的窗口"）

## 变更文件

| 文件 | 变更 |
|---|---|
| `app/agents/graph.py` | `_window_messages()` + `agent_node` 应用窗口 + 裁剪日志 |
| `app/core/config.py` | `history_max_rounds` 注释说明真实语义 |
| `.env.example` | 同上（原注释"由 Checkpointer 状态管理截断"是错的） |
| `tests/test_context_window.py` | 新增 10 例 |

## 遗留问题

- **只有窗口截断，没有摘要压缩**。被裁掉的历史彻底消失，模型无法知道
  "用户前面提过什么"。多轮指代问题（"那它能退吗"）在第 11 轮之后会失效。
  摘要压缩需要额外一次 LLM 调用，属于独立一步
- **按"轮"计数而非按 token 计数**。10 轮里如果都是长回答（如知识库检索
  带回大段父块），仍可能超出上下文。理想做法是按 token 预算裁剪
- **Checkpointer 存储无限增长**：本步只控制了 token 成本，没控制存储。
  需要归档/清理策略（生产跑一年 `checkpoints` 表会很大）
- 裁剪日志是 INFO 级，长会话会刷日志；生产可降为 DEBUG 或采样
- 未测量裁剪对回答质量的影响（第 11 轮之后是否开始"忘事"）。
  这需要长会话评估集，当前黄金集都是单轮/2 轮，测不到
