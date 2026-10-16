# AGENTS.md — 编码 Agent 工作指引

本项目是一个**面向生产的智能客服 Agent**（知识库 RAG + 人工转接），
采用小步迭代方式开发，每一步交付可运行、可验证的增量。

## 项目概览

- 技术栈：Python 3.12 + uv / FastAPI + LangGraph / Postgres(pgvector) + Milvus / Vite + React(Node 由 fnm 管理) / podman 管理中间件
- 核心链路：用户提问 → 关键词/LLM 双触发判断 → 单 Agent tool-calling（`search_kb` / `transfer_to_human`）→ 会话记忆存 Postgres（LangGraph Checkpointer）
- 演进路线：MVP(10 步) → 异常处理(6 项) → 测试 → 评估集 → Langfuse → 多 Agent 重构 → SSE 流式 → 模拟坐席端

## 硬性约束（每次修改代码都必须遵守）

1. **中文注释**：关键代码必须有中文注释，解释"为什么这么写"而非"做了什么"。
2. **小步前进**：每步只做当前步骤计划内的事，不顺手重构、不加未排期的功能。
3. **配置外置**：所有厂商相关配置（base_url/key/模型名）走 `.env`，代码不硬编码任何厂商。
4. **每步一份文档**：`docs/step-N.md` 记录本步目标、实现要点、验证方法、遗留问题。
5. **每步一次 commit**：完成验证后提交，commit message 格式 `step-N: <做了什么>`。
6. **异常处理显式化**：涉及外部调用（LLM/DB/向量库）的代码要有 try/except 并给出友好降级，不裸抛。

## 目录结构

```
app/
  main.py          # FastAPI 入口
  core/            # 配置、LLM 客户端等基础设施
  api/             # 路由（/chat /health 等）
  agents/          # LangGraph 图定义
  rag/             # 切块、embedding、检索
web/               # Vite + React 前端
docs/              # 每步学习笔记
data/knowledge_base/  # Markdown 知识库语料
tests/             # pytest
compose.yaml       # podman 中间件编排
```

## 常用命令

```bash
uv sync                                  # 安装 Python 依赖
uv run uvicorn app.main:app --reload     # 启动后端（开发模式，热重载）
uv run pytest                            # 跑测试
uv run ruff check .                      # lint
podman compose up -d                     # 启动中间件（Postgres + Milvus）
cd web && fnm use && npm run dev         # 启动前端
```
