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

## step-28.2：消息显示坐席昵称与发送时间

- chat_message 新增 sender_name 列：human 消息落库时写入坐席昵称
  （routes.reply_thread 从登录态取 agent.display_name）
- get_user_messages / get_messages 返回 sender_name；SessionMessage 模型同步
- MessageBubble：human 消息标签显示具体坐席昵称（替代泛化的"人工"），
  所有带标签的消息显示时间（今天只显示时分，更早带日期 .msg-time）
- 端到端验证：坐席"坐席小王"回复后，用户端历史里 human 消息
  sender_name="坐席小王" ✓

## step-28.3：坐席署名改 sender_id 关联（不直接存昵称）

- chat_message.sender_name 改为 sender_id（关联 app_user.username）——
  与 user_id 同一关联模式；坐席改昵称后历史消息展示自动跟随
- get_messages / get_user_messages 查询 LEFT JOIN app_user 得 display_name，
  对外字段仍叫 sender_name（昵称语义），屏蔽存储细节；前端零改动
- routes.reply_thread 改传 sender_id=agent["username"]
- schema 变更走 DROP 重建；端到端验证：库中存 agent1，接口返回"坐席小王"

## step-28.4：app_user 增加代理主键 id，全链路 id 关联

- app_user 新增 BIGSERIAL id 主键，username 改 UNIQUE（登录名可变更）
- 关联全面切 id：
  - conversation_status.user_id / chat_message.user_id / chat_message.sender_id
    → BIGINT 关联 app_user.id
  - Token 的 sub 存 user id（改名后 Token 仍有效）；deps 按 id 反查用户
    （get_user_by_id），账号已删则按未认证处理
- users.py 返回体统一带 id；auth.py 签 Token 用 user["id"]
- 好处：改用户名/昵称不悬空、JOIN 稳定、未来支持改名的数据基础
- schema 三表 DROP 重建，正式账号重置（密码同前）
- 修复过程中发现的测试 fixture 问题：agent_headers 依赖 _agent_id 主键签 Token
