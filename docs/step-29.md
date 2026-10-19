# step-29：接入即锁定——人工服务单归属接入坐席

## 目标

一个坐席接入会话后，其他坐席不能再插手：不能回复、不能关闭、队列里也看不到。
（用户下一次转接产生的新会话不锁定，任何坐席都可以再接。）

## 实现要点

1. `conversation_status` 新增 `agent_id BIGINT` 列：接入时把坐席**账号 ID**（而非昵称）
   锁进会话。昵称可改可重复，账号 ID 才是权限校验的可靠依据。
   - schema 检测改查 `user_id, agent_id` 双列，缺列整表重建（沿用开发期约定）。
2. `handoff.agent_accept(thread_id, agent_name, agent_id)`：接入即锁定。
3. `handoff.list_live_threads(agent_id)`：队列按坐席过滤——
   `queued` 所有坐席可见（保持抢单语义），`in_service` 只有接入者本人可见。
4. `handoff.get_thread_agent_id()`：供 reply/close 接口校验身份。
5. `handoff.agent_close(..., agent_id)`：服务中的会话仅本人可关；
   排队中的仍允许任意坐席"接单即关"。
6. 路由层：
   - `POST /agent/threads/{id}/reply`：非接入者本人回复返回 **403**（身份不对）
     而非 409（状态不对），前端可区分提示。
   - `POST /agent/threads/{id}/close`：非本人关在办工单返回 409。
   - `GET /agent/threads`：按登录坐席过滤。
7. 前端 AgentConsole：会话从队列消失且不是自己关闭的 → 详情面板显示
   `locked`（"其他坐席处理中"），无操作按钮与回复栏；可继续看历史记录。
   顺手修了 `openThread` 里 `user_name`/`user_display_name` 字段名不一致的问题。

## 验证

## 追加（step-29.1）：坐席可查用户历史会话

- 新增 `GET /agent/threads/{id}/user_history`：按会话归属用户跨 thread
  拉平全部历史消息（复用 `handoff.get_user_messages`）；匿名会话返回空列表。
  仅要求坐席登录，不限定接入者——接入前也需要看历史决定是否抢单。
- 前端 AgentConsole：详情面板**主消息列表直接展示跨 thread 完整历史流**——
  当前会话消息正常显示，以前会话的消息置灰并带"历史"标记；
  匿名会话（无归属）回退展示当前 thread 历史。轮询同样优先拉历史流，
  坐席接入后回复能实时出现在完整流里。
- `SessionMessage` 增加可选 `thread_id` 字段，供前端区分旧会话/当前会话消息。
- 验证：30 passed（新增 `test_agent_user_history`）。

## 遗留问题

- 前端靠轮询感知会话被接走（最多 4s 延迟）；SSE/WebSocket 属于后续流式迭代范围。
- 坐席 A 中途掉线/请假时无"转派"操作，只能关掉让用户重新转接。
