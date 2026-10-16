# Step 1 — 项目脚手架

## 目标

搭建可运行的最小项目骨架：uv 环境、目录结构、环境变量模板、AGENTS.md。

## 做了什么

1. `uv init --bare --name aide-agent --python 3.12` 初始化项目
   - `--bare` 不生成示例代码和 README，保持干净
2. 创建目录结构：
   - `app/core`：配置、LLM 客户端等基础设施
   - `app/api`：FastAPI 路由
   - `app/agents`：LangGraph 图定义
   - `app/rag`：切块、embedding、检索
   - `data/knowledge_base`：知识库语料
   - `docs` / `tests` / `web`
3. 一次性声明全部依赖到 `pyproject.toml`（uv.lock 锁定版本）：
   - torch 走 **CPU 专用源**（`tool.uv.sources` + `[[tool.uv.index]]`），
     否则会拉取数 GB 的 CUDA 版 torch——纯推理场景完全不需要
4. `.env.example`：所有厂商相关配置全部外置，代码零硬编码
5. `AGENTS.md`：给编码 Agent 的项目级工作指引

## 验证

```bash
uv sync
uv run python -c "print('ok')"
```

## 遇到的坑

- **torch CPU 源是关键**：默认源会下载 CUDA 版 torch（约 3GB+），国内网络下极慢。
  pyproject 里通过 `explicit = true` 的独立 index 指定 CPU 源解决。
- 国内网络下 uv 下载慢属正常，耐心等待或临时加 `UV_DEFAULT_INDEX` 环境变量指向国内镜像。

## 遗留问题

- 依赖未实际安装完成（网络原因），步骤 2/3 开始前必须先 `uv sync` 成功。
