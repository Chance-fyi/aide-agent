# step-39：双端消息改 WebSocket 实时推送

## 目标

双端的"收到消息"体验一直靠轮询：用户端和坐席端各自每 4 秒拉一次 history
（`POLL_INTERVAL_MS`）。本步把**收**这一侧整体换成 WebSocket 推送。

## 轮询在双端场景下到底糟在哪

| 问题 | 具体表现 |
|---|---|
| 延迟不可控 | 坐席回复后用户最多 4 秒才看到；坐席看用户补发的消息同理 |
| 空转请求 | 两个端常开就是每分钟 30 次查询，99% 返回"没有新消息" |
| 语义是错的 | "消息到达"被实现成"客户端碰巧拉了一次"，多端/断线时容易漏 |
| 最隐蔽的一条 | 原实现里用户消息是在 **LLM 跑完之后**才落库的，所以坐席端即便轮询也要等整轮 LLM 结束才看得见"用户在等" |

最后一条是这次改动顺带修掉的真问题：消息先落库、再调 Agent，
坐席端的感知延迟从"LLM 耗时"降到"一次 INSERT"。

## 方案：收走 WS，发走 REST

不是"全链路 WS"，而是按语义分：

| 动作 | 通道 | 理由 |
|---|---|---|
| 用户发消息 | WS `{"type":"message"}` | 回复本来就要异步推回来，用一条连接闭环最自然 |
| 坐席发消息/接入/结束 | REST POST | 已有完整状态机校验与 409/403 语义，为"实时"重写一遍权限逻辑不划算 |
| 收消息 / 收状态 | WS 推送 | 本次要解决的问题 |
| 历史 / 队列快照 | 连接建立时 `ready` 全量给 | 断线重连即自愈，客户端不用自己补缺口 |

## 协议

### 用户端 `/ws/chat?token=...`

| 方向 | 事件 | 说明 |
|---|---|---|
| C→S | `{"type":"message","content":"..."}` | 发消息（长度/空白校验与 REST 同一套 `ChatRequest`） |
| C→S | `{"type":"ping"}` | 应用层心跳（浏览器发不了 ping 帧） |
| S→C | `{"type":"ready","thread_id","status","messages"}` | 连接建立/重连的全量快照 |
| S→C | `{"type":"message","message":{role,content,ts,sender_name}}` | 客服/坐席回复、排队确认话术 |
| S→C | `{"type":"status","status"}` | 转接状态变化（排队/服务中/结束/超时回退） |
| S→C | `{"type":"error","message"}` | 友好错误（校验失败 / LLM 故障），**不断连接** |
| S→C | `{"type":"pong"}` | 心跳应答 |

### 坐席端 `/ws/agent?token=...`

| 方向 | 事件 | 说明 |
|---|---|---|
| C→S | `{"type":"watch","thread_id":"..."}` | 订阅当前查看的会话（覆盖语义） |
| S→C | `{"type":"ready","threads":[...]}` | 队列快照（按坐席过滤后的） |
| S→C | `{"type":"queue_changed"}` | 队列有变，客户端重拉 `GET /agent/threads` |
| S→C | `{"type":"message","thread_id","user_id","message":{...}}` | 正在看的会话有新消息 |

## 六个关键设计决策

### 1. 每条连接一个 outbox 队列 + 一个写协程

不能直接 `await ws.send_json(...)`：

- 推送是**广播**场景，一个卡死的标签页会把整条业务链路（包括别人的推送）拖住
- 多个协程并发写同一个 WebSocket 会**交错帧**，协议层直接坏掉

所以 `Hub.send()` 只做同步入队（`put_nowait`），实际发送由每条连接自己的
写协程串行完成。队列满（`OUTBOX_MAX=100`）就丢弃并记日志——慢连接只牺牲自己，
重连时 `ready` 快照会把状态重新对齐。

### 2. 全量快照 + 增量推送，而不是纯增量

纯增量流在断线、切网、多端在线时必然出现缺口，客户端要自己写补洞逻辑。
现在每次连接都给一份 `ready` 全量真相，客户端只负责"替换 + 追加"。

### 3. 队列推"信号"，不推列表

队列是**按坐席过滤**的（`in_service` 只对接入者可见）。如果服务端推列表，
就得给每条连接算一份；推 `queue_changed` 让客户端用同一个
`GET /agent/threads` 取自己那份，过滤口径永远只有一处。

### 4. 状态变更通知放在数据层

`handoff._notify_status()` 被 `mark_pending_human` / `agent_accept` /
`agent_close` / `reset_active` / `sweep_timed_out` 统一调用。

为什么不放在路由层：状态机是唯一事实源，**任何**一次状态变更都必须让两端看到。
靠每个调用方记得推送，早晚会漏——尤其是后台巡检（`sweep_timed_out`）
这种没有请求上下文的地方。这一步同时修掉一个真实缺陷：排队超时回退时，
用户端原本会一直卡在"坐席接入中"等一个不会来的人。

（`handoff` 用函数内延迟导入 `core.hub` 来打破循环依赖：hub 需要
`handoff.get_thread_user` 做会话→用户寻址。）

### 5. 跨线程投递必须回到事件循环

LangGraph 会把**同步节点丢进线程池**执行，而 `route_input` 里的
`mark_pending_human`（转接状态变更）就发生在那里。从工作线程直接
`put_nowait` 到事件循环线程的 `asyncio.Queue`，会叫不醒正在等待的写协程，
事件被拖到下一次循环唤醒才发出去。

所以 `Hub.send()` 统一走 `loop.call_soon_threadsafe`，连接注册时记下自己的 loop。

### 6. 鉴权与安全

- **Token 走查询参数**：浏览器 WebSocket API 不允许自定义请求头，
  `Authorization` 传不进去。代价是 uvicorn 访问日志可能记录 URL。
- **Origin 白名单**：WebSocket 不受同源策略约束，浏览器不会替我们拦，
  必须服务端校验。白名单与 HTTP CORS 共用 `CORS_ORIGINS` 一份配置
  （两处各配一份，迟早出现"fetch 能过、WS 被拒"）。
- **关闭码分语义**：4401/4403 表示"别重连，回登录页"，
  其他关闭码才触发指数退避重连——否则 Token 失效时前端会无限重试。
- **先 accept 再 close**：ASGI 里未 accept 就 close 会被转成 HTTP 403，
  前端拿不到任何原因。现在是 accept → 发 `error` 事件 → 带自定义码关闭。

## 验证

### 单元/集成测试 `tests/test_ws.py`（9 例）

| 测试 | 锁住什么 |
|---|---|
| `test_outbox_drops_events_when_full` | 慢连接只丢自己的事件，且保留最早的（`ready` 不能被后来的挤掉） |
| `test_hub_routes_by_user_and_watch` | 用户按账号寻址、坐席按"是否正在看该会话"寻址 |
| `test_ws_chat_rejects_bad_token` | 无效 Token → error + 4401 |
| `test_ws_chat_rejects_foreign_origin` | 非白名单 Origin → error + 4403 |
| `test_ws_chat_ready_then_reply` | 连接即快照；回复由推送回来且带服务端时间戳 |
| `test_ws_chat_reports_validation_error` | 脏消息回 error 但**不断连接** |
| `test_ws_agent_requires_agent_role` | 用户 Token 连不上坐席通道 |
| `test_ws_agent_receives_user_message` | 用户发消息 → 正在看的坐席立刻收到 |
| `test_ws_user_receives_human_reply` | 坐席 REST 回复 → 用户端立刻收到（含坐席昵称） |

测试必须用 `with TestClient(app)` 而不是裸 `TestClient(app)`：
后者每次 `websocket_connect` 会新建一个 portal（= 新事件循环），
而两条连接要互相推送，跨事件循环操作同一个 `asyncio.Queue` 是未定义行为。

### 真实 uvicorn 冒烟（`websockets` 客户端）

```
1) 用户端 ready OK
2) 转接状态 + 回复推送 OK（status: queued → 客服话术）
3) 坐席端 ready OK（队列快照）
4) 用户消息实时到坐席 OK
5) 坐席回复实时到用户 OK: 人工在此 / 冒烟坐席
```

另外单独跑了一轮真实 LLM（非转接）路径：`退款多久到账？` 经 WS 发出，
RAG 回答完整推回，确认 `process_message` 重构没有破坏正常问答。

### 全量

- `uv run pytest`：**158 passed**（原 149 + 新增 9）
- `uv run ruff check .`：clean
- `cd web && npm run lint` / `npm run build`：通过

## 变更文件

| 文件 | 变更 |
|---|---|
| `app/core/hub.py` | **新增**：连接注册表 + 每连接 outbox/写协程 + 寻址推送 |
| `app/api/ws.py` | **新增**：`/ws/chat`、`/ws/agent` 端点，鉴权/Origin/协议处理 |
| `app/api/chat.py` | 抽出 `process_message`（REST 与 WS 共用）、`build_user_snapshot`；用户消息改为先落库再调 Agent；新增推送 |
| `app/agents/handoff.py` | `append_message` 返回入库时间；新增 `_notify_status` 并在 5 处状态变更点调用 |
| `app/api/routes.py` | 坐席回复落库后推送（用户端 + 其他在看该会话的坐席） |
| `app/core/config.py` | 新增 `cors_origins`（HTTP CORS 与 WS 握手共用） |
| `app/main.py` | 注册 WS 路由；CORS 白名单改为读配置 |
| `web/src/ws.js` | **新增**：带指数退避重连、心跳/假死检测、鉴权失败停止重连的客户端 |
| `web/src/components/UserChat.jsx` | 轮询 → WS（ready 快照 + 消息/状态推送） |
| `web/src/components/AgentConsole.jsx` | 队列轮询 + 消息轮询 → WS（queue_changed 信号 + watch 订阅） |
| `web/src/constants.js` | 新增 `API_WS_BASE`（由 `API_BASE` 推导）；删除 `POLL_INTERVAL_MS` |
| `.env.example` | 新增 `CORS_ORIGINS` |

## 遗留问题

- **连接表在进程内存里，只支持单进程**。多 worker 部署时 A 进程的推送到不了
  B 进程的连接。上生产前需要跨进程总线（Postgres `LISTEN/NOTIFY` 或 Redis Pub/Sub），
  接法是把 `Hub.send` 的实现换掉，业务侧调用点不用动。
- **Token 会进访问日志**（查询参数）。更严格的做法是握手后第一帧再发 Token，
  或改用短期一次性 ticket 换取连接。
- **同一坐席开多个标签页**：自己发的回复只在本标签页乐观追加，
  另一个标签页收不到（REST 动作不回推给发起者）。多坐席协作不受影响。
- **断线期间发送的消息会丢**：前端只提示"连接已断开，请稍后重发"，
  没有离线队列/自动重发。真要做得可靠需要客户端消息 ID + 幂等去重。
- **`ready` 快照只给最近 200 条**（沿用 `get_user_messages` 的 limit），
  超长历史的首屏不全，需要分页加载。
- **无"对方正在输入"、已读回执、在线状态**——这些是 WS 之后才有条件做的东西，
  不在本步范围。
- **前端没有 WS 不可用时的降级**：当前 WS 连不上就只有错误提示，
  不会自动回退到 REST 轮询。企业内网有 WS 被拦的情况，届时需要降级策略。
