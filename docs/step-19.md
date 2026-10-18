# Step 19 — 前端双端优化：用户端体验升级 + 坐席端控制台

## 目标

前端从单一用户聊天页升级为「用户端 / 坐席端」双视图：
用户端优化交互体验；坐席端补齐工作台 MVP（队列、查看历史、接入、结束）。

## 后端增量

- `handoff.list_live_threads()`：查询 queued/in_service 会话（只查业务表不碰 Checkpointer，
  队列刷新高频，消息由前端按需再拉 history）
- `GET /agent/threads`：坐席端队列数据源，按 updated_at 先进先出；失败返回 503

## 前端改动

- `App.jsx`：tab 双视图切换（不引路由依赖，切换即重挂载）
- `components/UserChat.jsx`：用户端
  - 转接状态横幅（脉冲动画）+ 空态欢迎页 + 快捷提问 chips
  - 机器人头像气泡布局、三点打字动画、后端 503 detail 文案透传
- `components/AgentConsole.jsx`：坐席端
  - 4s 轮询队列（MVP 够用，SSE 属后续流式迭代）
  - 点开会话拉 `/chat/{id}/history` 展示消息
  - 接入（queued → in_service）/ 结束处理（带备注 resolution），409 提示刷新

## 验证

```bash
npm run build        # 通过
npx oxlint src       # 仅 1 条 effect 内 setState 警告（轮询场景可接受）
uv run pytest        # 59 passed
uv run ruff check .  # 0 issues
```

手动验证：双终端分别起后端与 `npm run dev`，用户端发"转人工"→
坐席端队列 4s 内出现工单 → 接入 → 用户端刷新后状态不变（in_service 也算 transferred）→
坐席端结束处理 → 用户端再发消息回自助。

## 遗留问题

- 坐席端接入后无法直接以坐席身份发言（graph 无坐席消息通道），只能看历史 + 关闭；
  真正的坐席回复链路属后续"模拟坐席端"完整迭代
- 队列轮询非推送，实时性 4s 延迟
- 坐席端无鉴权（与后端约定一致，内网调试用）
