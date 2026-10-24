# aide-agent — 智能客服 Agent

面向生产的智能客服系统：知识库 RAG 问答 + 人工转接 + 双端实时会话。
技术栈 **FastAPI + LangGraph + Postgres(pgvector) + Milvus + React**。

## 能力

| 能力 | 实现要点 |
|---|---|
| 知识库问答 | Milvus 向量检索（父子块切分），低分片段直接丢弃以防幻觉 |
| 多轮记忆 | LangGraph Postgres Checkpointer；窗口锚定水位线 + 摘要压缩，保住前缀缓存 |
| 人工转接 | LLM 工具调用自主判断 + 关键词规则兜底；排队超时自动回退 |
| 坐席工作台 | 队列抢单、接入锁定、跨会话用户历史、处理结果备注 |
| 实时推送 | 双端 WebSocket（消息 / 转接状态 / 队列变化），断线指数退避重连 |
| 流式输出 | 回答边生成边显示（逐 token 推送），首字延迟约 1 秒级 |
| 多实例 | Postgres LISTEN/NOTIFY 跨进程总线，支持 `--workers N`；不可用时自动降级单进程 |
| 输入防护 | 提示注入检测 + 违禁词拦截，命中即友好拒答并留痕 |
| 限流保护 | 按账号/ IP 滑动窗口限流 + LLM 并发闸门（超限快速失败而非无限排队） |
| 可观测 | Langfuse trace（LLM / 工具 / LangGraph 节点 span），未配置时零侵入 no-op |
| 知识库管理 | `/kb` 页面：状态读数、按文件切分策略、异步重建、检索验证（坐席登录） |
| 评估 | 黄金问答集 + 检索/生成两层跑分（`scripts/run_eval.py`） |

## 快速开始

### 1. 依赖与配置

```bash
uv sync
cp .env.example .env     # 必填：LLM_BASE_URL / LLM_API_KEY / LLM_MODEL
```

任何兼容 OpenAI 接口的服务都能用，改 `.env` 即可，代码里没有厂商硬编码。

### 2. 启动中间件

```bash
podman compose up -d     # Postgres + Milvus + Attu
```

需要可观测性时叠加 profile（额外 6 个容器，UI 在 `:3000`）：

```bash
podman compose --profile observability up -d
```

### 3. 启动后端

```bash
uv run fastapi dev
```

默认监听 `http://127.0.0.1:8000`，自动热重载，接口文档在 `/docs`。
需要局域网内其他设备访问时加 `--host 0.0.0.0`。

启动时会自动应用数据库迁移（`AUTO_MIGRATE=true`）。生产环境建议关掉自动迁移，
改为发布流程里先执行一次：

```bash
uv run python -m app.core.migrations            # 应用待执行的迁移
uv run python -m app.core.migrations --status   # 只查看当前版本
```

### 4. 启动前端

```bash
cd web && fnm use && npm install && npm run dev
```

访问 `http://localhost:5173` 是用户端，`/agent` 是坐席端。

### 5. 准备账号

用户端可自助注册。坐席账号不开放注册，由管理员创建：

```bash
uv run python scripts/create_agent.py agent1 agent123 坐席小王
```

### 6. 导入知识库

语料在 `data/knowledge_base/`。用坐席账号登录后打开 `http://localhost:5173/kb`，
点「重建索引」即可（蓝绿重建，旧索引全程在线）。

也可以用命令行触发（需坐席 Token）：

```bash
curl -X POST http://127.0.0.1:8000/kb/rebuild -H "Authorization: Bearer <坐席Token>"
```

改了语料或切分策略后重建一次生效；管理页会提示哪些文件"语料比索引新"。

## 常用命令

```bash
uv run fastapi dev                       # 后端开发模式（热重载）
uv run pytest                            # 测试
uv run pytest tests/test_ws.py -q        # 只跑某个模块
uv run ruff check .                      # lint
cd web && npm run lint && npm run build  # 前端 lint / 构建

uv run python -m app.core.migrations      # 应用数据库迁移
uv run python -m app.core.migrations --status  # 查看迁移版本
uv run python scripts/create_agent.py    # 创建坐席账号
uv run python scripts/run_eval.py        # 评估：只跑检索层（不花 LLM 费用）
uv run python scripts/run_eval.py --full # 评估：完整链路（检索 + 生成 + 转人工判断）
```

## 项目结构

```
app/
  main.py            # FastAPI 入口：只做组装（路由、生命周期）
  core/              # 配置、LLM、鉴权、输入防护、WS 连接中心、事件总线、限流、迁移
  api/               # 路由：/chat /auth /agent /ws /kb，以及业务编排 process_message
  agents/            # LangGraph 图、工具、转接状态机、上下文摘要
  rag/               # 切块、embedding、Milvus 与 Postgres 存储
  eval/              # 黄金集、指标、跑分器
web/src/             # Vite + React：用户端 / 坐席端 / 知识库管理 / WS 客户端
data/knowledge_base/ # Markdown 知识库语料
tests/               # pytest（API / WS / 检索 / 摘要 / 评估）
compose.yaml         # 中间件编排（含 observability profile）
```

## 设计取舍

几条贯穿全项目的约束，改动代码时需一并遵守：

- **配置外置**：厂商地址/Key/模型名全走 `.env`，代码不硬编码
- **异常显式化**：外部调用（LLM / DB / 向量库）都有 try/except 与友好降级，
  对外只说"发生了什么类别的问题 + 该怎么办"，内部细节只进日志
- **实时性不拖垮业务**：推送失败只记日志，客户端靠重连时的全量快照自愈
- **可观测性非必需**：Langfuse 未配置时完全 no-op，不成为启动前提

## 文档

`docs/step-N.md` 按迭代顺序记录每次改动的目标、实现要点、验证方法与遗留问题。
排障与设计决策的原因都写在那里。
