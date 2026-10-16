# aide-agent — 智能客服 Agent

基于 **FastAPI + LangGraph** 的智能客服系统，支持知识库 RAG 问答与人工转接。

## 功能

- 🤖 智能问答：基于电商售后知识库的 RAG 检索问答
- 🧠 多轮记忆：LangGraph Postgres Checkpointer 持久化会话
- 🙋 人工转接：LLM 自主判断 + 关键词规则兜底的双触发机制
- ⚠️ 异常处理：检索降级、拒答、重试、输入防护（迭代中）
- 📊 评估与可观测：黄金问答集 + Langfuse（排期中）

## 快速开始

```bash
# 1. 安装依赖
uv sync

# 2. 配置环境变量
cp .env.example .env   # 填入你的 LLM base_url / key / model

# 3. 启动中间件（Postgres + Milvus）
podman compose up -d

# 4. 启动后端
uv run uvicorn app.main:app --reload

# 5. 启动前端（另开终端）
cd web && fnm use && npm install && npm run dev
```

## 学习路线

见 `docs/` 目录，每步一篇笔记：`step-1.md` 起，按序阅读即可复现整个项目。
