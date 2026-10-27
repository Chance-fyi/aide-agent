# step-47.1：修复转人工时出现"抱歉，刚才的回复没有生成成功"

## 症状

用户触发转人工后：

- **坐席端**看到 `抱歉，刚才的回复没有生成成功，请您再试一次。`
- **用户端**什么都看不到

两个现象看似矛盾（同一条消息，一端有一条没有），实际是同一个 bug 的两面。

## 根因

关键词兜底转接走的是 `transfer` 节点，它**直接构造 AIMessage、完全不经过 LLM**：

```python
def transfer_node(state, config):
    ...
    return {"messages": [AIMessage(content=_PENDING_HUMAN_REPLY)]}
```

而流式路径 `executor.stream()` 只从 `astream_events` 里捞
`on_chat_model_stream` 的 token。**没有 LLM 调用 → 一个 token 都没有**：

```
delta 数: 0
stream() 返回: '抱歉，刚才的回复没有生成成功，请您再试一次。'   ← 误判
图最终消息:    '您的工单已提交，人工客服正在接入，请稍候。'      ← 真实结果
```

于是 `parts` 为空 → 被当成"模型生成失败" → 返回 `EMPTY_REPLY`。

**为什么用户端看不到**：两个条件同时成立

1. `process_message` 里 `if push_reply and on_delta is None` —— 流式分支不整条推送
   （这是 step-45 的设计：内容已边生成边推过，再推会重复）
2. `ws.py` 的 `reply_end` 只在 `stream_state["started"]` 为真时发
   —— 而 `started` 只在收到第一个 delta 时置位

一个 delta 都没有 → 既不整条推、也不发 `reply_end` → **用户端完全静默**。

而坐席端走的是另一条路（`_notify_agents_message`，不区分流式），
所以它拿到了那条错误的兜底文案。

## 修法

### 1. 流式无 token 时，改用图的最终输出

`astream_events` 里顶层链的 `on_chain_end`（name=`LangGraph`）带完整最终 state：

```
on_chain_end name='transfer'   output_type=dict has_messages=True
   → 最后一条: '您的工单已提交，人工客服正在接入，请稍候。'
on_chain_end name='LangGraph'  output_type=dict has_messages=True
   → 最后一条: '您的工单已提交，人工客服正在接入，请稍候。'
```

所以：

```python
if full.strip():
    return full
# 一个 token 都没有：可能是"不经过 LLM 的分支"（转接节点等），
# 也可能模型真的生成了空内容。先看图的最终输出，那才是权威结果。
if final_state:
    reply = final_state["messages"][-1]
    text = reply.text if isinstance(reply, AIMessage) else str(reply.content)
    if text.strip():
        return text
return EMPTY_REPLY
```

`EMPTY_REPLY` 只保留给"图里也确实是空"的真正异常情况。

### 2. 流式但没产出时，整条推给用户

在 `process_message` 里包一层 `on_delta` 跟踪"是否真的有内容流出去"：

```python
streamed = False
def _on_delta(chunk):
    nonlocal streamed
    streamed = True
    on_delta(chunk)

...
if push_reply and not streamed:
    _push_user_message(uid, ROLE_AGENT, text, saved_agent)
```

推送策略变成三种情况，语义清晰：

| 情况 | 是否整条推 | 原因 |
|---|---|---|
| 非流式（REST） | ✅ | 响应体本来就要完整文本 |
| 流式且有 delta | ❌ | 已边生成边推过；再推会重复（id 不同，`mergeMessage` 拦不住） |
| **流式但无 delta**（转接等） | ✅ | 不推的话用户端**什么都收不到** |

这个跟踪放在 `process_message` 而不是 `ws.py`，因为"该不该推送"是业务编排
的一部分，不该让传输层去推断。

## 验证

### 回归测试（`tests/test_ws.py`，+2 例）

| 测试 | 锁住什么 |
|---|---|
| `test_keyword_transfer_streams_no_delta_but_user_gets_reply` | 话术是真正的转接提示（不是兜底文案），且用户端确实收到（带 id） |
| `test_agent_side_does_not_see_empty_reply_fallback` | 落库/坐席端也看不到兜底文案 |

两条都验证过**回退修复则失败、恢复则通过**，不是摆设。

### 真环境端到端

```
用户端收到 agent 消息 1 条:
  '您的工单已提交，人工客服正在接入，请稍候。客服会尽快与您联系。'

坐席端看到 agent 消息 1 条:
  '您的工单已提交，人工客服正在接入，请稍候。客服会尽快与您联系。'

用户端无兜底文案: True
坐席端无兜底文案: True
用户端确实收到回复: True
PASS
```

### 相关路径回归（确认没改坏别处）

```
1) 正常问答:      delta=136 reply_end=True 整条消息=0  → OK 仍流式
2) 关键词转接:    delta=0   reply_end=False 整条消息=1  → OK 无流式但整条送达
3) 已转人工补发:  delta=0 整条消息=0                   → OK 不刷屏
4) 安检拦截:      delta=0 整条消息=1                   → OK
```

### 全量

`uv run pytest`：**252 passed**（250 + 2）；`ruff check .` clean。

## 变更文件

| 文件 | 变更 |
|---|---|
| `app/agents/executor.py` | 捕获 `on_chain_end`(LangGraph) 的最终 state，无 token 时兜底 |
| `app/api/chat.py` | 跟踪 `streamed`，流式无产出时整条推送 |
| `tests/test_ws.py` | +2 条回归 |

## 这个 bug 暴露的深层问题

**"流式"与"生成回复"被绑成了同一件事**。实际上：

- 有的分支产出 token（正常问答）
- 有的分支直接给结果（转接节点、安检拦截）
- 有的分支根本没有回复（已转人工）

step-45 引入流式时，只考虑了第一种，把"没有 token"等同于"生成失败"。
修法不是打补丁，而是把三件事分开：

1. **回复内容从哪来** —— 图最终 state 才是权威，token 流只是其中一种呈现
2. **要不要流式呈现** —— 取决于有没有 token，而不是取决于分支类型
3. **要不要整条推送** —— 取决于有没有真的流过

## 遗留问题

- **`EMPTY_REPLY` 仍可能与真实回复混淆**。现在只有"图里也空"才会走到它，
  但若模型真的返回空内容，用户看到的文案无法区分"模型抽风"和"系统故障"。
- **没有"生成中断"的显式事件**。若 LLM 在流式**中途**失败，前端靠 `error`
  事件收尾（step-47 修过），但服务端没有 `reply_abort` 之类的事件区分
  "正常结束"与"异常中断"。
- **转接时坐席端会看到机器人的转接话术**。这是刻意的（坐席需要知道机器人
  对用户说了什么），但会让坐席的历史里混入机器人消息，与"人工对话"混排。
- **`_handle_guarded` / `_handle_live` 的推送逻辑与主路径不一致**：
  前者在分支内部自己推，后者完全不推。三条分支的推送时机分散在三个地方，
  将来加分支容易漏。可以考虑统一成"返回 outcome + 统一的推送出口"。
