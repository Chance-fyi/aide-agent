# step-40.2：修复转人工后只能发一条消息

## 症状

用户转人工后，发一条消息，输入框就锁死了，再也发不出第二条。
排队中（还没接入）反而能发几条——这个差异正是定位问题的线索。

## 根因：客户端靠"收到回复"清状态，而人工会话没有机器人回复

```
send() → setLoading(true)          // 输入框 disabled={loading}
         ↓
     等服务端回一条 message 事件来清 loading
         ↓
  _handle_live() 在 in_service 下 ack = ""
  if push_reply and ack:  ← 条件不成立，什么都不推
         ↓
  loading 永远 true → 输入框永久禁用
```

`_handle_live` 里那句 `ack = _QUEUED_ACK if status == handoff.STATUS_QUEUED else ""`
原本是为了"坐席接入后别让机器人插话"，但它连带把**发送确认**也一起省掉了。

排队中 `ack` 非空 → 推一句话术 → 客户端收到 message → `loading` 清掉 → 能继续发。
坐席一接入 `ack` 为空 → 什么都不推 → 卡死。

**设计上的错误**：把"消息发送成功"和"机器人有回复"绑成了同一个信号。
这两件事本来无关——人跟人对话时，本来就不该有机器人回复。

## 修法

### 1. 服务端：每条已接收的用户消息都回一个 `sent` 事件

```python
hub.send(conn, {
    "type": "sent",
    "thread_id": outcome.thread_id,
    "status": outcome.status,   # 顺带同步转接状态
})
```

新增事件类型，与 `message`（对话内容）区分开：

| 事件 | 语义 |
|---|---|
| `message` | 有新对话内容要渲染成气泡 |
| `sent` | 你刚才那条消息已被服务端接收（可能没有回复） |

这样"发送成功"有了确定信号，不再靠"有没有回复"去猜。

### 2. 客户端：清 pending 改走 `sent`，并加超时兜底

```js
case "sent":
  clearPending();                              // 主信号
  setTransferred(isLiveStatus(ev.status));
  break;
```

同时给 pending 加 60 秒兜底定时器：

```js
const startPending = () => {
  setLoading(true);
  pendingTimer.current = setTimeout(() => setLoading(false), 60_000);
};
```

为什么需要兜底：`hub` 的 outbox 满时会丢事件（`OUTBOX_MAX=100`，慢连接只牺牲自己）。
一条丢失的确认就能让输入框永久禁用——这类"卡死"必须有一条超时兼容路径，
宁可偶尔提前解锁，也不要永久锁死。

### 3. 输入锁只作用于自助问答阶段

```js
// 发送
if (loading && !transferred) return;
// 输入框
disabled={loading && !transferred}
```

已转人工是人和人在对话，不能因为上一条的确认还没到就拦住下一条。

### 4. 顺手去掉"已收到"刷屏

排队中每条消息都回一句"您的消息已收到，人工客服会尽快回复您，请稍候。"
——用户连发三条就被刷三次。转接横幅（"已转接人工，坐席会尽快回复"）
已经表达了同样的语义，不需要逐条重复。

现在 `_handle_live` 完全不推气泡，`ChatOutcome.reply` 只给 REST 客户端用
（`/chat` 的响应体仍然返回这句话术，REST 行为不变）。

## 验证

### 回归测试（`tests/test_ws.py`，+2 例）

| 测试 | 锁住什么 |
|---|---|
| `test_user_can_send_repeatedly_after_agent_joined` | in_service 下连发 3 条，每条都有 `sent`；3 条都落库 |
| `test_user_can_send_repeatedly_while_queued` | 排队下连发 3 条都有 `sent`；不再推"已收到"气泡 |

第一条测试在修复前是**挂死**的（不是失败）——因为 `sent` 事件根本不存在，
`receive_json()` 会一直等下去。这个"挂住"本身就是 bug 的直接证据。

### 真浏览器验证（CDP + 真实后端）

**坐席已接入（bug 现场）：**
```
转接后状态: 人工坐席服务中
坐席接入后状态: 人工坐席服务中
连发 4 条全部成功，气泡总数 6
我发出的: ["我要投诉，转人工","补充第1句","补充第2句","补充第3句","补充第4句"]
刷新后我发出的: （同上，一条不少）
```

**排队中：**
```
状态: 人工坐席服务中
全部气泡: ["我要转人工","您的工单已提交，人工客服正在接入","排队中第1句","排队中第2句","排队中第3句"]
'已收到'话术条数: 0（期望 0）
```

> 中途踩了个坑：第一版验证脚本没先触发转接就直接连发，第 2 条被锁是**正确行为**
> （自助问答阶段在等 LLM 回复，本来就该锁输入）。改成先转接再连发才复现到真场景。

### 全量

`uv run pytest`：**162 passed**（160 + 2）；`ruff` clean；`npm run build` / `oxlint` 通过。

## 变更文件

| 文件 | 变更 |
|---|---|
| `app/api/ws.py` | 新增 `sent` 事件（每条已接收的用户消息都回） |
| `app/api/chat.py` | `_handle_live` 不再推气泡，去掉 `push_reply` 参数 |
| `web/src/components/UserChat.jsx` | 清 pending 改走 `sent`；60s 兜底定时器；输入锁仅限自助阶段 |
| `tests/test_ws.py` | 新增 2 条回归用例 |

## 遗留问题

- **`sent` 只保证"服务端收到了"**，不保证坐席看到了。真要可靠需要消息级 ACK
  （坐席端读到时回执），属于已读回执范畴，本步不做。
- **60 秒兜底是拍的值**。若将来接入更慢的模型，或网络更差，这个值要跟着调；
  更好的做法是按操作类型给不同超时。
- **客户端仍无离线队列**：断线期间发的消息会直接提示"连接已断开，请重发"，
  不做自动重投。真要做需要客户端消息 ID + 幂等去重。
- **`ChatOutcome.reply` 在 WS 路径已无人使用**（`sent` 取代了它的职责），
  但 REST 还在用，暂时保留。若将来 REST 也改成推送，这个字段可以删。
