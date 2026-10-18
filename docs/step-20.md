# step-20：会话连续性与人工对话闭环

## 目标

1. 用户会话跨天/刷新可恢复（同一客户会话记录全量保存）
2. 转人工后用户可继续发消息（直达坐席，不消耗 LLM）
3. 坐席可回复消息，用户能看到
4. 前端全面视觉升级（设计系统落地）+ 坐席端三角色消息区分

## 实现要点

### 后端

- **`app/agents/handoff.py`**：新增用户可见会话记录表 `chat_message`
  (thread_id, role, content, created_at)，附 `append_message` / `get_messages`。
  独立表而非 Checkpointer：坐席回复不经过图执行写不进快照，
  且跨天恢复/坐席端展示需要按时间序读纯消息流
- **`app/api/chat.py`**：
  - `POST /chat` 用户消息+机器人回复落记录表
  - 已转接（queued/in_service）bypass LLM，消息直达坐席工作台；
    排队中回确认话术，in_service 返回空 message（不刷屏）
  - `GET /chat/{id}/history` 优先读记录表（role: user/bot/agent），
    存量老会话回退 Checkpointer 快照，升级不丢历史
- **`app/api/routes.py` / `schemas.py`**：新增 `POST /agent/threads/{id}/reply`，
  坐席回复写记录表，仅排队中/处理中允许（否则 409）
- **`app/agents/graph.py`**：`transfer_node` 去重——in_service 下
  上一条 assistant 已是接入提示则本轮不追加，避免重复刷屏

### 前端

- **会话连续性**：thread_id 存 localStorage，重进自动拉历史恢复
  消息与转接状态；404 则静默按新会话
- **用户端**：转人工后输入不再禁用；4s 轮询收人工回复；
  空消息不渲染气泡；用户/智能客服/人工客服三角色气泡+头像+标签区分
- **坐席端**：客户靠左（灰），智能客服靠右（蓝），人工坐席靠右（绿）；
  回复栏（回车/纸飞机按钮）+ 4s 轮询用户补发消息；备注栏弱化分层
- **视觉升级**：Minimalism & Swiss 设计系统（海军蓝 + 蓝 accent 语义 token）、
  卡片浮层布局、分段式 tab、SVG 图标替代 emoji、focus-visible 焦点环、
  prefers-reduced-motion 适配、胶囊输入框 + 圆形发送按钮、≤720px 响应式

## 验证方法

- `uv run ruff check .` 通过；`cd web && npm run build` 通过
- Postgres 冒烟：建表、三角色写入读取、状态流转正常（数据已清理）
- 手动链路：用户提问 → 转人工 → 坐席接入 → 双方互发消息可见；
  刷新/隔天重进自动恢复会话

## 遗留问题

- 轮询（4s）为过渡方案，实时性升级走 SSE（已列入后续流式迭代）
- 坐席接口无鉴权（内网调试），待坐席体系迭代统一接入
- 存量 Checkpointer 会话的角色映射为 bot，无法还原当时是否有转接
