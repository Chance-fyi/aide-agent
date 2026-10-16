# Step 3 — 配置模块 + LLM 客户端 + FastAPI 骨架

## 目标

打通"配置 → LLM 客户端 → Web 服务"三层基础能力，为后续 Agent 接入做准备。

## 做了什么

### 1. `app/core/config.py` — pydantic-settings 配置中心

- 所有配置集中在 `Settings` 类，**厂商相关配置全部来自 .env**（零硬编码）
- `@lru_cache` 保证进程内单例，避免重复读文件
- `postgres_dsn` / `transfer_keyword_list` 用 property 派生，
  避免同一配置在多处手工拼接、格式不一致

### 2. `app/core/llm.py` — LLM 客户端

- `ChatOpenAI` 兼容任何 OpenAI 格式接口，换厂商只改 .env
- `temperature=0.1`：客服场景要确定性，不要创造性
- 提前设好 `timeout=30` 和 `max_retries=2`，防止 LLM 卡死拖垮请求

### 3. FastAPI 骨架

- `main.py` 只做组装（路由注册 + lifespan），保持入口薄
- `lifespan` 启动时打印配置（**绝不打印 API Key**）
- 中间件连接用懒加载，后端和中间件可以任意顺序启动

## 验证

```bash
# 1. 复制环境变量模板并填入真实值
cp .env.example .env

# 2. 启动
uv run uvicorn app.main:app --reload

# 3. 健康检查
curl http://localhost:8000/health
# 期望返回 {"status":"ok","llm_model":"...","milvus":"http://localhost:19530"}
```
