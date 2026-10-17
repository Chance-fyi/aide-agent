# Step 9 — Vite + React 聊天前端

## 目标

可视化的客服对话界面：聊天消息流、多轮会话、转人工状态提示。

## 技术与运行

- Vite + React（Node 由 fnm 管理，v22）
- `web/.env` → `VITE_API_BASE=http://localhost:8000`（后端地址外置）

```bash
cd web
fnm use 22           # 或 eval "$(fnm env)" 后用默认版本
npm install
npm run dev          # http://localhost:5173
```

## 与后端的协议（`src/App.jsx`）

- `POST /chat { message, thread_id? }` → `{ message, thread_id, transferred }`
- **thread_id 是多轮对话的唯一凭据**：首轮不传，后端生成后随响应返回，
  前端存入 state，之后每轮原样携带——多轮记忆全部由后端 Checkpointer 负责，
  前端不存任何对话历史（刷新页面即"新会话"是符合协议的简化设计）
- `transferred=true` → 禁用输入框，顶栏显示"已转接人工"

## 交互细节

- 乐观更新：用户消息立即上屏，不等后端
- 加载态："正在输入…"占位
- 请求失败：友好错误气泡，不白屏
- "新会话"按钮：清空本地状态，thread_id 置空即开新会话

## 端到端验证（真实 LLM）

- 前端 dev server 200
- "退货要自己出运费吗" → 正确检索知识库并分情况回答（质量/无理由），
  `transferred: false`

## 遗留

- Markdown 渲染（回复含 **加粗** 与列表，当前按纯文本展示）后续迭代
- SSE 流式输出后续迭代
