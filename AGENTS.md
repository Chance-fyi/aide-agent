# AGENTS.md — 编码 Agent 工作指引

本项目是一个**面向生产的智能客服 Agent**：知识库 RAG 问答 + 人工转接 + 双端实时会话。
采用小步迭代方式开发，每一步交付可运行、可验证的增量。

## 项目概览

- **技术栈**：Python 3.12+ / uv · FastAPI + LangGraph · Postgres(pgvector) + Milvus ·
  Vite + React（Node 由 fnm 管理，版本见 `web/.nvmrc`）· podman 管理中间件
- **核心链路**：用户提问 → 输入安检 → 关键词/LLM 双触发判断 → 单 Agent tool-calling
  （`search_kb` / `transfer_to_human`）→ 会话记忆存 Postgres（LangGraph Checkpointer）
  → 消息落 `chat_message` 表 → 经 WebSocket 推给双端
- **已完成**：MVP → 异常处理 → 测试 → 评估集 → 上下文窗口/摘要 → Langfuse →
  坐席工作台 → WebSocket 实时推送 → 前端视觉重做
- **未开始**：多 Agent 重构、SSE 流式输出、知识库管理 UI（见各步文档的遗留问题）

## 硬性约束（每次修改代码都必须遵守）

1. **中文注释**：关键代码必须有中文注释，解释"为什么这么写"而非"做了什么"。
2. **小步前进**：每步只做当前步骤计划内的事，不顺手重构、不加未排期的功能。
3. **配置外置**：所有厂商相关配置（base_url/key/模型名）走 `.env`，代码不硬编码任何厂商。
4. **每步一份文档**：`docs/step-N.md` 记录本步目标、实现要点、验证方法、遗留问题。
5. **每步一次 commit**：完成验证后提交，commit message 格式 `step-N: <做了什么>`
   （修订用 `step-N.M`，如 `step-40.1`）。
6. **异常处理显式化**：涉及外部调用（LLM/DB/向量库/推送）的代码要有 try/except
   并给出友好降级，不裸抛。对外只说"什么问题 + 该怎么办"，细节进日志。
7. **业务逻辑只写一处**：REST 与 WebSocket 两条入口共用 `app/api/chat.py` 的
   `process_message`，不要为某条通道另写一套编排。
8. **改 schema 只走迁移**：`app/core/migrations.py` 里**追加**一条 `Migration`，
   不要写 `CREATE TABLE` / `ALTER TABLE` 到业务模块，更不允许 `DROP TABLE`
   （`chat_message` 存的是用户真实聊天记录，删一次就是数据事故）。

## 领域约定（改动时不要破坏）

这些是跨前后端的契约，散落在多处代码里，改之前先全局搜一遍引用。

| 概念 | 取值 | 定义位置 |
|---|---|---|
| 消息角色 `chat_message.role` | `user` / `agent`(智能客服) / `human`(人工坐席) | `app/api/chat.py` 的 `ROLE_*` |
| 会话状态 `conversation_status.status` | `active` / `queued` / `in_service` / `closed` | `app/agents/handoff.py` 的 `STATUS_*` |
| 账号角色 `app_user.role` | `user` / `agent` | `app/core/security.py` 的 `ROLE_*` |
| WS 事件类型 | `ready` `message` `sent` `status` `queue_changed` `error` `pong` | `app/api/ws.py` 顶部文档字符串 |

状态机（唯一事实源在 `handoff.py`，状态变更会通过 `_notify_status` 自动推送双端）：

```
active ──触发转接──> queued ──坐席接入──> in_service ──结束──> closed
          ▲                    │
          └── 排队超时回退 ─────┘
```

## 目录结构

```
app/
  main.py            # FastAPI 入口：只做组装（注册路由、生命周期）
  core/
    config.py        # 全部配置（pydantic-settings，读 .env）
    migrations.py    # 数据库迁移：版本表 + 有序迁移列表（DDL 的唯一来源）
    ratelimit.py     # 限流（滑动窗口）+ 并发闸门（保护 LLM 与连接池）
    hub.py           # WebSocket 连接中心：寻址推送、每连接 outbox
    bus.py           # 跨进程事件总线（Postgres LISTEN/NOTIFY，zlib+base64 编码）
    llm.py guardrails.py security.py users.py tracing.py
  api/
    chat.py          # 业务编排 process_message（REST 与 WS 共用）
    ws.py            # /ws/chat、/ws/agent 端点
    routes.py        # 坐席操作 /agent/*
    auth.py deps.py schemas.py debug_routes.py
  agents/
    graph.py         # LangGraph 图（单 Agent + tool calling）
    handoff.py       # 转接状态机 + 会话记录表（业务事实的唯一源）
    summarize.py     # 上下文摘要
    runtime.py       # 图与 Checkpointer 生命周期
  rag/               # 切块、embedding、Milvus 与 Postgres 存储
  eval/              # 黄金集、指标、跑分器
web/src/
  components/        # UserChat / AgentConsole / LoginView / MessageBubble
  ws.js              # WS 客户端：重连、心跳、假死检测
  messages.js        # 消息合并（按服务端 id 去重）
  index.css          # 设计系统 token（墨色阶 + 等宽数据 + 发丝线）
data/knowledge_base/ # Markdown 知识库语料
docs/                # 每步的设计与排障记录
scripts/             # create_agent / run_eval / cleanup_test_data / chat_repl
tests/               # pytest
compose.yaml         # 中间件编排（含 observability profile）
```

## 常用命令

```bash
uv sync                                  # 安装 Python 依赖
uv run fastapi dev                       # 启动后端（热重载，127.0.0.1:8000）
uv run pytest                            # 跑测试
uv run pytest tests/test_ws.py -q        # 只跑相关模块（全量较慢）
uv run ruff check .                      # lint
podman compose up -d                     # 启动中间件（Postgres + Milvus）
cd web && fnm use && npm run dev         # 启动前端（5173）
```

运维/数据命令：

```bash
uv run python -m app.core.migrations [--status]                      # 数据库迁移 / 看版本
uv run python scripts/create_agent.py <username> <password> [昵称]   # 建坐席账号
curl -X POST http://127.0.0.1:8000/debug/kb/rebuild                  # 重建知识库索引
uv run python scripts/run_eval.py [--full]                           # 跑评估集
```

## 踩过的坑（改相关代码前必读）

这些都是已经修过、但很容易被重新引入的问题：

- **消息会经多条路径到达同一界面**（REST 响应体 / WS 推送 / 重连快照）。
  消息体必须带服务端主键 `id`，前端一律用 `mergeMessage` 合并，不要直接 `[...prev, msg]`。
  曾导致"坐席发出一条显示两条，刷新变一条"。
- **不要让 UI 状态依赖"有没有收到回复"**。已转人工时不会有机器人回复，
  客户端靠回复事件清 `loading` 会让输入框永久锁死。服务端对每条已接收消息都回 `sent`。
- **推送可能来自非事件循环线程**。LangGraph 把同步节点丢进线程池，
  转接状态变更就发生在那里；`hub.send` 必须走 `call_soon_threadsafe`。
- **WebSocket 不受同源策略保护**，必须服务端校验 `Origin`（白名单与 CORS 共用 `CORS_ORIGINS`）。
- **前端视觉有硬约束**：零装饰阴影、零背景渐变、无 emoji 图标、颜色只做信号不做装饰、
  小号文字对比度 ≥4.5:1。改样式前先看 `index.css` 顶部说明。
  （`box-shadow` 只允许用于键盘焦点环 `0 0 0 3px var(--ring)`，不要拿去画卡片投影。）
- **改 schema 必须走迁移，不要 `DROP TABLE`**。旧实现"schema 不一致就整表重建"
  会把用户聊天记录全删。现在 `schema_migrations` 记版本，基线是幂等的
  （`CREATE IF NOT EXISTS` + `ADD COLUMN IF NOT EXISTS`），老库能被接管而不丢数据。
- **测试不要连真实库**。迁移类测试用临时库（见 `tests/test_migrations.py` 的
  `temp_db` fixture），并注意 `from ... import get_settings` 绑定的是名字，
  要 patch **使用方模块**才生效——踩过一次，假迁移被应用到开发库上。
- **限流键不能只用 thread_id**。匿名会话的 thread_id 是客户端传的，换一个
  就是新的计数桶（等于没限流）。匿名必须按 IP，登录按账号 ID。
- **限流/闸门的计数器在进程内存里**，多 worker 时实际限额 = 配置 × worker 数。
  与 `core/hub.py` 的连接表是同一个约束，跨进程要 Redis。
- **`Hub.push_*` 会同时做本地投递和总线广播**，`Hub.deliver_*` 只做本地投递。
  总线接收端必须走 `deliver_remote` → `deliver_*`，绝不能调 `push_*`——
  否则两个进程互相回声，事件数指数增长。
- **`asyncio.Event`/`Queue` 不能在模块级单例的 `__init__` 里创建**。
  `bus = EventBus()` 在 import 时构造，那时没有运行中的事件循环，
  对象会被绑到错误的 loop，之后一 await 就报 "bound to a different event loop"。
  必须在 `start()`（运行在 app 循环里）懒创建。
- **不要 monkeypatch 全局 `time.monotonic`**。asyncio 的事件循环时钟就是它，
  冻结后所有 `asyncio.wait_for` 永不超时（踩过一次：并发闸门测试挂死）。
  需要控制时间就注入时钟（见 `SlidingWindowLimiter(clock=...)`）。
- **历史数据源是 `chat_message` 表，不是 Checkpointer**。Checkpointer 只在
  记录表为空时兜底（存量会话）。有测试锁住这一点。
