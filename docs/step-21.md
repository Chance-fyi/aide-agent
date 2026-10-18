# step-21：代码重构——可读性提升（行为不变）

## 目标

在 59 个测试守护下做纯结构性重构，不改变任何行为：
消除重复模式、拆分过长函数、统一角色命名、抽取前后端共享常量。

## 实现要点

### 后端

- **`app/api/chat.py`**：
  - 110 行的 `chat()` 拆为语义化小函数：
    `_get_thread_status`（安全状态查询+降级）、`_save_messages`（批量落库）、
    `_handle_guarded`（安检命中）、`_handle_live`（已转接直达人工）、
    `_invoke_agent`（执行图+异常翻译）
  - `history()` 的 Checkpointer 回退逻辑抽出为 `_messages_from_checkpointer`
  - `ROLE_USER/AGENT/HUMAN` 常量集中定义，成为角色命名的单一事实来源
- **`app/agents/graph.py`**：
  - 抽取 `_get_thread_status(thread_id, fallback)` 统一三处"查询失败降级"模式
  - 抽取 `_last_text_message(messages, msg_type)` 统一两处"倒序找最近文本消息"

### 前端

- **`web/src/constants.js`**（新增）：API_BASE、ROLE、THREAD_STATUS、
  isLiveStatus、STORAGE_KEY、POLL_INTERVAL_MS——前后端协议字段集中一处，
  消除组件里的魔法字符串与重复定义
- **`web/src/components/MessageBubble.jsx`**（新增）：消息气泡渲染 + 角色展示
  配置（头像文字/标签）双端共用；`agentViewBubbleClass` 封装坐席端视角映射。
  各端只决定对齐方向与是否带头像/标签，角色视觉双端一致
- `UserChat.jsx` / `AgentConsole.jsx` 改用共享模块，消息渲染逻辑减半

### 角色命名统一（前后端对齐）

`user`=用户、`agent`=智能客服、`human`=人工坐席；
CSS 类名同步语义化（`.msg.agent-view`/`.msg.human`/`.avatar.bot.human`）。

## 验证方法

- `uv run ruff check app` 通过；`uv run pytest` 59 passed（行为不变）
- `cd web && npm run build` 通过

## 遗留问题

- AgentConsole 仍用 alert 提示操作失败，可换成非阻断 toast（后续步骤）
- 两端各自维护轮询逻辑，可在后续迭代抽 usePolling hook
