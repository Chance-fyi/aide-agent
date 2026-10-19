# step-28：close 后开新会话（不再清记忆）——多 thread 架构下的正确闭环

## 设计修正

step-26 的"close 后清 Checkpointer 记忆"是**单 thread 复用架构下的补丁**：
一个用户永远用一个 thread，只能靠删记忆保证新问题上下文干净，代价是
排查/审计/回放数据被销毁。

step-27 改为多 thread 后，补丁失效为"多余且有害"：
- 新问题 → `/chat` 服务端发现当前 thread 已 closed → **分配新 thread**
  （上下文天然干净，无需删除任何数据）
- 旧 thread 的 Checkpointer 快照与 chat_message 全部保留 → 可排查、可审计
- 用户视角仍是"一条聊天记录"（`/chat/my/history` 跨 thread 合并）

## 实现

- **`handoff.reset_active_fresh` 删除**（连同 runtime.get_checkpointer
  的 Future 信箱——仅为删记忆而生）；graph.py 的 closed 分支回归
  `reset_active`（只复位状态行，且现在仅匿名会话会走到该分支）
- **`/chat` 登录用户 thread 解析规则**：当前 thread 状态为 closed →
  开新 thread；否则复用

## 语义终版

| 场景 | Agent 上下文 | Checkpointer 数据 | 用户聊天流 |
|---|---|---|---|
| 同一 thread 连续对话 | 延续 | 累积 | 连续 |
| 排队超时回退 | 保留 | 保留 | 连续 |
| close 后新消息 | **全新（新 thread）** | **保留（可排查）** | 连续（合并展示） |

## 验证

- 端到端：发消息 → 转接 → 坐席 close → 再发消息 → 确认
  开新 thread ✓、旧 thread Checkpointer 保留 ✓、
  `/chat/my/history` 合并两段内容 ✓
- ruff 通过；62 passed；build 通过；测试数据已清理

## step-28.1：chat_message 直接绑定 user_id，删除存量迁移代码

- ensure_table 重写：conversation_status / chat_message 建表语句直接含
  user_id（+索引）；删除 user_name 列迁移、chat_message 回填等全部
  存量兼容逻辑
- schema 变更策略改为"检测-重建"：访问 user_id 列失败（列缺失或表不存在）
  则 DROP 重建——开发期无历史负担，容忍丢数据；上生产前再引入正式迁移机制
- 旧 schema 表已 DROP 重建，正式账号（user1/user2/agent1/agent2）保留在
  app_user，未受影响
- ruff 通过；62 passed；端到端验证新 schema 下消息 user_id 直接落值
