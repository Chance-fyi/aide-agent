# step-27：消息归属用户——一条聊天记录跨设备延续

## 目标

回答并落实产品语义：**用户视角只有一条连续的聊天记录**（淘宝/京东客服模式），
人工服务片段是这条流的一部分；消息绑定到用户身上，退出重登/换设备不丢。

## 方案讨论：单 thread 清上下文 vs 独立会话列表

企业级客服（Intercom/Zendesk 等）实际是"一个客户多条会话 + 会话列表"，
因为客服指标（首次解决率/会话时长/转接率）以会话为单位。但本项目产品形态
是"一条聊天记录"，二者并不冲突：
- **用户视角**：一条流（跨 thread 合并展示）
- **Agent 视角**：每段人工服务独立上下文（close 后清记忆，step-26 已实现）
- **存储现实**：一个用户多个 thread（每段服务一个），查询层拉平

## 实现要点

### 后端

- **`chat_message.user_id` 新列**（幂等迁移 + 回填）：user 角色消息记录
  发送者；agent/human 消息留空（属于会话而非用户）
- **`handoff.get_user_messages(username)`**：按用户取全部消息——
  以 conversation_status.user_id 圈定该用户的 thread 集合，跨 thread
  按时间序拉平合并（最新 200 条），这是"一条聊天记录"的查询实现
- **`handoff.get_user_current_thread(username)`**：反查用户最近活跃的
  thread（updated_at 最新）
- **`/chat` 服务端解析 thread_id**（登录用户忽略前端传值）：
  `get_user_current_thread() or 新建`——thread_id 变成纯实现细节，
  前端无需持有；换设备发消息自动接回同一条流
- **`GET /chat/my/history`**（新增，必须登录）：返回该用户完整聊天流
  （跨 thread 合并）+ 当前转接状态；登录后无论什么设备都能找回全部消息
- `_save_messages` 链路透传 user_id（仅打在 user 角色消息上）

### 前端

- **删除 thread_id 的 localStorage 持久化**（STORAGE_KEY 引用清空）：
  恢复/轮询/续发全部走 `/chat/my/history`（按账号查，与设备无关）
- 退出登录只清登录态——聊天记录在服务端，重登即回

## 验证方法

- 端到端：登录发消息 → 全新连接"重登" → `/chat/my/history` 找回 2 条
  消息；续发消息 thread_id 与重登前一致（接回同一条流）✓
- `uv run ruff check app` 通过；`uv run pytest` 62 passed
- `cd web && npm run build` 通过
- 测试数据已清理（历史遗留 UUID 会话与孤儿消息，见 step-26.3 脚本）

## 遗留问题

- 坐席端队列仍以 thread 为单位（一次服务一单），与用户端"一条流"并存：
  同一用户第二次转人工会出现第二条队列项，属预期行为（不同工单）
- `/chat/sessions` 会话列表接口保留未接前端（未来若要做"按服务单查看"
  可直接复用）
