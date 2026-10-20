# step-38：接入 Langfuse 可观测性

## 目标

此前排障全靠翻日志：一次对话里 LLM 调了几次、检索命中了什么、哪个节点慢、
哪次是限流重试——都看不到。本步把 Agent 执行链路（LLM 调用、工具、LangGraph 节点）
上报到 Langfuse，形成可检索的 trace。

## 三条硬约束

按 AGENTS.md 的"异常处理显式化"与"配置外置"，接入必须满足：

1. **默认关闭、零侵入**：没配 Key 时完全 no-op，连 handler 都不创建
2. **失败只记日志**：初始化/上报异常一律吞掉，绝不变成用户侧 500
3. **配置全走 .env**：Key/host 不硬编码

第 1 条是硬性的：可观测性是"锦上添花"，不能成为启动前提或运行依赖。
线上 Langfuse 挂了，客服对话必须照常工作。

## 为什么用 CallbackHandler 而不是手动 OTel 埋点

LangGraph 的 `graph.ainvoke(config=...)` 会把 `callbacks` 传给内部**所有**
LangChain 组件（LLM、ToolNode、Runnable）。一处接线就能自动拿到完整的父子 span 树。

手动埋点要自己维护 span 层级与上下文传播，易错且冗余。

实测确认 LangGraph 节点名与工具名都被自动捕获（见验证章节）。

## 实现

### `app/core/tracing.py`

```
init()          幂等初始化（区分"没试过"与"试过但失败"，避免每请求重试）
get_handler()   取回调器；未启用返回 None
build_config()  在现有 LangGraph config 上附加 callbacks + metadata
flush() / shutdown()  应用关闭时推缓冲区
```

关键点：

- **`build_config` 未启用时原样返回 base**（拷贝一份，不返回同一对象）。
  调用方无需写 if 分支，这条路径与接入前**完全等价**
- **`configurable` 与 `metadata` 严格分离**：`configurable` 是业务运行时参数通道
  （thread_id 等，节点会读），`metadata` 是观测元数据通道。
  把 `langfuse_*` 塞进 `configurable` 会被节点当业务参数读走
- **callbacks 追加而非覆盖**：调用方可能已挂别的回调（测试、其他观测），
  覆盖会静默丢掉别人的数据
- **延迟导入 SDK**：没启用 Langfuse 的部署不必付 import 开销

### metadata 键名

SDK 靠 `langfuse_*` 约定取上下文，写错**不会报错、只会静默丢聚合维度**：

| 键 | 作用 |
|---|---|
| `langfuse_session_id` | 会话聚合（同一 thread 的多轮归到一个 session） |
| `langfuse_user_id` | 用户聚合（定位"某个用户总遇到问题"） |
| `langfuse_tags` | 标签筛选（区分 `chat` / `eval` / 分类） |
| `langfuse_trace_name` | trace 名称 |

匿名会话不写 `langfuse_user_id`，否则 UI 里会多出一个空用户。

### 接线点

| 位置 | 说明 |
|---|---|
| `app/api/chat.py::_invoke_agent` | 真实对话，带 `user_id`（匿名则不带） |
| `app/eval/runner.py::run_full` | 评估跑分，打 `eval` + 分类标签 |
| `app/main.py::lifespan` | 启动 `tracing.init()`，关闭 `tracing.shutdown()` |
| `scripts/run_eval.py` | 脚本退出前 flush（否则缓冲区全丢） |

评估流量用 `eval` 标签隔开，UI 里能筛出评估、不与真实用户对话混在一起。

### 为什么必须 flush

Langfuse 是**批量异步上报**（攒批减少网络请求）。进程退出时缓冲区里的 span 会丢——
本地脚本跑完就退、uvicorn 收到 SIGTERM 时都是这种情况。所以 `flush()`
挂在 lifespan 关闭阶段与 eval 脚本的 `finally` 里。

## 配置

```bash
LANGFUSE_ENABLED=true
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_HOST=https://cloud.langfuse.com   # 自建填 http://localhost:3000
LANGFUSE_ENVIRONMENT=development
```

两个 Key 都非空才真正接入。`LANGFUSE_ENABLED=false` 可临时关闭而不删 Key。

显式传参而不是依赖 SDK 读环境变量：本项目所有配置统一走 `Settings`，
两套来源并存会导致"改了 .env 不生效"。

## 验证

### 1. 未配置 = 零行为差异（实测）

```
INFO app.core.tracing Langfuse 未配置 Key，跳过接入
INFO httpx2 GET http://testserver/health "HTTP/1.1 200 OK"
INFO httpx2 POST http://testserver/chat "HTTP/1.1 200 OK"
```

启动、健康检查、对话全部正常，只有一行说明性日志。

### 2. 真上报验证（假 Langfuse 服务抓包）

起一个本地 HTTP 服务接住 SDK 的 POST，验证 payload 内容：

```
POST /api/public/otel/v1/traces  解压后 1070 bytes
   包含 'thread-demo-1': True     ← session id
   包含 'chat': True              ← trace name
   包含 'test': True              ← tag
   包含 'session.id': True        ← OTel 属性键
   包含 'user.id': True
```

走的是 **OTel + gzip**（Langfuse 5.x 的实现），不是旧的 JSON batch API。

### 3. LangGraph 图 + 工具链

用假 LLM 触发一次 `search_kb` 工具调用：

```
POST /api/public/otel/v1/traces  4916B / 15623B
   'thread-graph-1': True    ← session
   'LangGraph': True         ← LangGraph 节点 span
   'search_kb': True         ← 工具 span
   转义后的提问存在: True      ← 输入内容（JSON 转义为 \uXXXX）
   转义后的回答存在: True      ← 输出内容
```

输入输出内容确实被捕获（payload 里是 `ensure_ascii` 转义形式），
这是观测的核心价值——能回放"用户问了什么、模型答了什么"。

### 4. 单元测试（`tests/test_tracing.py`，10 例）

不依赖真实 Langfuse 服务，锁住三条硬约束：

| 测试 | 锁住什么 |
|---|---|
| `test_build_config_passthrough_when_disabled` | 未配置时 config **原样**返回，不加 callbacks/metadata |
| `test_master_switch_disables_even_with_keys` | `ENABLED=false` 时即使有 Key 也不接入 |
| `test_flush_and_shutdown_are_noop_when_disabled` | 未启用时 flush/shutdown 安全且不抛（lifespan 无条件调用） |
| `test_init_failure_degrades_silently` | SDK 初始化抛异常时降级为关闭，不向上传播 |
| `test_init_is_idempotent_and_cached` | 重复 init 不重复尝试注定失败的连接 |
| `test_build_config_metadata_keys` | `langfuse_*` 键名正确（写错静默丢数据） |
| `test_build_config_omits_user_id_when_anonymous` | 匿名不写 user_id |
| `test_build_config_appends_to_existing_callbacks` | 追加而非覆盖已有回调 |
| `test_build_config_does_not_mutate_input` | 不修改入参（LangGraph 会复用 config 对象） |

- `uv run pytest`：**149 passed**（新增 10 例）
- `uv run ruff check .`：clean

## 变更文件

| 文件 | 变更 |
|---|---|
| `pyproject.toml` / `uv.lock` | 新增 `langfuse==5.0.3`（依赖仅 backoff + wrapt + otel） |
| `app/core/tracing.py` | 新增 |
| `app/core/config.py` | 5 个 Langfuse 配置项 |
| `.env.example` | 同上 |
| `app/api/chat.py` | `_invoke_agent` 接入追踪 |
| `app/eval/runner.py` | 评估跑分接入，打 `eval` 标签 |
| `app/main.py` | lifespan 初始化与关闭 |
| `scripts/run_eval.py` | 退出前 flush |
| `tests/test_tracing.py` | 新增 10 例 |

## 遗留问题

- **未接自建 Langfuse**。自建需要 Clickhouse + Redis + MinIO + Postgres 四件套，
  体量远超本项目现有中间件。默认指向 Langfuse Cloud；
  自建时把 `LANGFUSE_HOST` 改成实例地址即可，代码无需改动
- **敏感信息未脱敏**。用户输入原样上报（含手机号、订单号等）。
  生产必须加 `mask` 函数过滤 PII——SDK 支持 `Langfuse(mask=...)`，
  本步未实现，属于合规必需项
- **未接入 Langfuse 的评分/数据集功能**。现在只做 trace 上报，
  `langfuse` 的 dataset + score 能力与 step-32~34 的黄金集有重叠，
  可以考虑把评估结果回传为 score（目前是本地 JSON）
- **采样率未配置**。高流量下全量上报成本高，SDK 支持 `sample_rate`，
  当前未暴露配置项
- **未验证 OTel 上下文在 SSE 流式场景的传播**（尚未实现流式）
- 评估跑分会产生大量 trace，注意 Cloud 免费额度
