# step-40.1：修复坐席端回复出现两条重复气泡

## 症状

坐席发出一条回复，界面上出现**两条一模一样的消息**；刷新页面后变回一条。

"刷新就好了"是最能定位问题的线索：说明 **DB 里本来就只有一条**，
重复完全发生在渲染层。

## 根因：一条消息有两条到达路径

坐席发送回复时，这条消息会走两条独立的路回到他自己的界面：

```
                    ┌─→ REST POST /agent/threads/{id}/reply
                    │     响应体（发送方自己的 ack）
POST /reply ────────┤
                    └─→ hub.push_agents(..., thread_id=...)
                          WS 推送（发送方自己正在 watch 这个会话，
                          服务端广播给所有 watcher —— 包含他自己）
```

前端两处都直接 `[...prev, msg]`：

```js
// sendReply()：本地拼一条乐观消息
setHistory((prev) => [...prev, humanMessage]);

// WS onEvent：服务端把同一条推回来
setHistory((prev) => [...prev, msg]);
```

→ 两个气泡。刷新时 `openThread()` 从 DB 重载历史，只有一行，于是变回一条。

顺带还有第二个隐性问题：本地乐观消息用 `new Date().toISOString()`，
与服务端 `created_at` 不同，双端时间轴会出现"回复时间早于提问"的倒挂。

## 为什么不能"前端按内容去重"

最省事的修法是在追加前判断"列表里有没有相同 role + content 的消息"。
这个方案有三个问题：

1. **时间戳不同**，严格比较永远不相等；放宽到只比内容又会误杀
   ——坐席连发两条"好的"是合法操作，第二条会被吞掉
2. 消息体里没有稳定标识，重连补发（`ready` 快照 + 增量）无法对齐
3. 判定逻辑会散落在多处，将来加第三条推送路径又会漏

## 修法：给每条消息一个服务端主键，按 id 收敛

### 1. 落库时返回主键

`handoff.append_message` 从返回 `ts` 改为返回 `{id, ts}`：

```python
INSERT INTO chat_message (...) VALUES (...) RETURNING id, created_at
```

`chat_message.id` 是 `BIGSERIAL`，天然唯一、单调递增，正好当去重键。

### 2. 所有出口都带上 id

| 出口 | 变更 |
|---|---|
| `POST /reply` 响应体 | 返回权威 `message: {id, role, content, ts, sender_name}` |
| WS `message` 事件（推给用户端） | `message.id` |
| WS `message` 事件（推给坐席端） | `message.id` |
| `GET /chat/{id}/history` | `SessionMessage.id` |
| `GET /chat/my/history`、`/agent/threads/{id}/user_history` | 同上 |

### 3. 前端按 id 合并，不再盲目追加

新增 `web/src/messages.js`：

```js
export function mergeMessage(list, incoming) {
  if (incoming?.id != null) {
    const i = list.findIndex((m) => m.id === incoming.id);
    if (i !== -1) {
      const next = list.slice();
      next[i] = { ...next[i], ...incoming };  // 服务端版本更权威
      return next;                            // 保留原位置
    }
  }
  return [...list, incoming];
}
```

两个要点：

- **同 id 已存在时保留原位置**，只覆盖字段。否则重连补发会让消息跳到列表末尾
- `id == null` 的消息不参与去重（如排队确认话术，它本就不落库）

### 4. 发送方不再自己拼消息

`sendReply()` 改为使用响应体里的权威消息：

```js
const data = await resp.json();
if (data?.message) {
  const sent = { ...data.message, thread_id: selected.thread_id };
  setHistory((prev) => mergeMessage(prev, sent));
}
```

时间戳直接用服务端的 `created_at`，倒挂问题一并解决。
而且这个顺序是**无关的**：WS 推送先到 → 列表里有 id X → 响应体到达时按 id 命中；
响应体先到 → 推送到达时按 id 命中。两条路径谁先谁后都只有一条。

用户端也换成 `mergeMessage`（防御重连补发与双通道）。

## 验证

### 新增回归测试（`tests/test_ws.py`，2 例）

| 测试 | 锁住什么 |
|---|---|
| `test_agent_reply_is_delivered_once_with_same_id` | REST 响应与 WS 推送的 **id 必须相同**——这是前端能去重的唯一依据。同时断言响应体带 `id` 与 `ts` |
| `test_reply_duplicate_is_one_row_in_history` | DB 里确实只有一条 human 消息；历史接口每条都带 `id` |

### 真浏览器复现（修复前后对比）

CDP 驱动真实 Chrome + 真实后端，坐席发一条回复：

```
target thread: 17825424 -> in_service
发送前气泡数: 2
发送后气泡数: 3   （期望 before+1 = 3）
气泡内容: ["我要投诉，转人工","您的工单已提交，人工客服正在","这是一条测试回复"]
刷新后气泡数: 3
结果: PASS 无重复
```

### 证明"推送确实到了"（排除"碰巧没推"的假通过）

如果推送根本没到达坐席端，去重逻辑等于没被执行，测试会假绿。所以单独验证：

```
REST 响应 id: 1730 ts: 2026-10-23T09:11:06.798287+00:00
WS 推送  id: 1730 ts: 2026-10-23T09:11:06.798287+00:00
推送确实到达: True
两路 id 相同: True
→ 前端 mergeMessage 命中同 id，只保留一条
```

### 用户端回归

```
用户端 收到前 2 → 收到后 3（期望 +1）
内容: ["我要投诉，转人工","您的工单已提交，人工客服","用户端只应看到一条"]
PASS
```

（首次跑这个用例时误报了 FAIL，原因是用例自己的 bug：`/agent/threads` 按
`updated_at` 升序返回，队列里堆着大量历史测试工单，取第一条拿到的是别人的会话。
改成用该用户的 `/chat/my/history` 取自己的 thread_id 后通过。）

### 全量

- `uv run pytest`：**160 passed**（158 + 2）
- `uv run ruff check .`：clean
- `npm run lint` / `npm run build`：通过

## 变更文件

| 文件 | 变更 |
|---|---|
| `app/agents/handoff.py` | `append_message` 返回 `{id, ts}`；`get_messages` / `get_user_messages` 补 `id` 列 |
| `app/api/schemas.py` | `SessionMessage` 新增 `id` |
| `app/api/chat.py` | `_append` / `_append_safe` 返回 `{id, ts}`；推送事件带 `id` |
| `app/api/routes.py` | `POST /reply` 响应体回传权威 `message`（含 `id` / `ts`） |
| `web/src/messages.js` | **新增**：`mergeMessage` 按主键合并 |
| `web/src/components/AgentConsole.jsx` | 去掉本地拼消息；两条流都走 `mergeMessage` |
| `web/src/components/UserChat.jsx` | 推送合并改走 `mergeMessage` |
| `tests/test_ws.py` | 新增 2 条回归用例 |

## 遗留问题

- **`_QUEUED_ACK` 仍会重复**：用户在排队中每发一条消息都会收到一句
  "您的消息已收到…"。这是产品层面的刷屏，不是 bug，但坐席接入后就该停止
  （已实现）。若要去掉，可以改成"只在第一条时提示一次"。
- **`openThread` 仍是整表替换**：`setHistory(data.messages ?? [])`。
  如果 WS 推送在 history 请求返回前到达，那条推送会被替换掉——但因为它
  已落库，一定在 history 里，所以不丢。若将来改成增量加载，这里需要改成 merge。
- **消息 id 是全局单调的 `BIGSERIAL`**，跨会话可比。若将来做多库分片，
  这个去重键要改成 `(thread_id, id)` 或 UUID。
- 前端仍无 JS 单元测试框架，`mergeMessage` 的正确性靠 CDP 端到端用例兜底。
  纯函数本应可单测，后续可考虑引入 vitest。
