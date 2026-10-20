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
| `compose.yaml` | Langfuse 自建编排（`observability` profile，5 常驻 + 1 一次性） |
| `deploy/langfuse/clickhouse-config.xml` | 新增：限制 ClickHouse 内存 |
| `tests/test_tracing.py` | 新增 10 例 |

## 自建 Langfuse（podman）

### 为什么用 profile 而不是无条件启动

Langfuse 的 Python SDK 5.x 走 OTel 上报（`/api/public/otel/v1/traces`），
该端点**只在 server v3+ 存在**。而 v3 架构强制要求 ClickHouse + Redis + MinIO，
合计 **5 个常驻容器、实测常驻内存约 1.5G**。

大部分开发场景不需要可观测性，不该让所有人都承担这个启动成本。
所以放进 `compose.yaml` 但加 `profiles: [observability]`：

```bash
podman compose up -d                            # 只起业务中间件（4 个）
podman compose --profile observability up -d    # 连 Langfuse 一起（9 个）
```

一开始我把它拆成独立文件 `compose.langfuse.yaml`，理由是"不拖慢日常启动"。
但 profile 机制本来就能解决这个问题，拆文件反而带来两个实际麻烦：
两个 compose 各自管理网络与卷、环境变量要同步改两处。已合并回主文件。

### 组件与端口

| 容器 | 作用 | 宿主机端口 |
|---|---|---|
| `langfuse-web` | UI + Ingestion API | 3000 |
| `langfuse-worker` | 异步处理事件/评估/导出 | — |
| `langfuse-clickhouse` | 事件存储（列式，写入量大） | — |
| `langfuse-redis` | web → worker 任务队列 | — |
| `langfuse-minio` | 原始事件落盘（S3 兼容） | 9001（控制台） |
| `langfuse-minio-init` | 一次性：建 bucket 后退出 | — |

**只有 5 个常驻容器**（`minio-init` 建完桶就退出）。

### 元数据库：复用业务 Postgres 实例，但用独立 database

初版照抄了 Langfuse 官方 compose，单独起了一个 `langfuse-postgres`。
实测下来这个额外实例没有必要：

| 检查项 | 实测结果 |
|---|---|
| 表名冲突 | **零**（Langfuse 72 张表 vs 业务 10 张，无一重名） |
| schema 占用 | 只用 `public` |
| 扩展依赖 | 只需默认的 `plpgsql` |
| 连接数 | 业务 6 + Langfuse 11 = 17，`max_connections` 100 |
| 内存占用 | 独立实例常驻 32MB，为它多跑一个 PG 不划算 |

所以改成**同实例、独立 database**：

```
aide-postgres
├── aide_agent   ← 业务（app_user / chat_message / kb_* / checkpoints）
└── langfuse     ← Langfuse 元数据（72 张表）
```

**为什么是独立 database 而不是同一个库**——关键在爆炸半径：

- Langfuse 启动时会自动跑 **424 个 Prisma 迁移**。独立库 = 这些迁移
  物理上碰不到 `aide_agent` 的表
- 重置 Langfuse 只需 `DROP DATABASE langfuse`，业务数据零风险
- 备份粒度可分开（`pg_dump -d langfuse`）

**也不需要建库的 init 容器**：Prisma 迁移会自动 `CREATE DATABASE`。
实测流程——先 `DROP DATABASE langfuse`，再起 web：

```
Error: P1003  Database `langfuse` does not exist   ← 首次尝试
424 migrations found in prisma/migrations
Applying migration `20230518191501_init`
...
$ SELECT count(*) FROM pg_tables WHERE schemaname='public'  →  72
```

库自动创建、72 张表就绪。所以整个 Langfuse 只需要 `minio-init` 一个 init 容器。


### 踩到的四个坑

**1. ClickHouse 挂载配置 Permission denied（exit 233）**

rootless podman 下 SELinux 需要重标，`deploy/langfuse/clickhouse-config.xml`
的挂载必须加 `:z`：

```yaml
- ./deploy/langfuse/clickhouse-config.xml:/etc/clickhouse-server/config.d/langfuse.xml:ro,z
```

不加时容器启动即循环退出，日志只有一行 `Permission denied`。

**2. `localhost` 在容器内解析到 IPv6，healthcheck 永远失败**

Next.js standalone 在容器内监听容器网络接口，**不监听 loopback**。
`wget --spider http://localhost:3000` 会 `Connection refused`（`localhost` → `::1`），
`127.0.0.1` 同样失败。必须用 `$(hostname)`：

```yaml
test: ["CMD-SHELL", "wget -qO- http://$(hostname):3000/api/public/health || exit 1"]
```

**3. MinIO 官方镜像已下架，且 Langfuse 不会自己建 bucket**

- `docker.io/minio/minio` 与 `quay.io/minio/mc` 均已 403/401。
  改用 Milvus 项目维护的 `docker.io/milvusdb/minio`（同一上游 release，自带 `mc`）
- Langfuse 启动时**不会创建 bucket**，缺失时 ingestion 返回 500，
  日志只说 `The specified bucket does not exist`，很隐晦。

  所以用一个独立的一次性容器 `langfuse-minio-init` 建桶
  （复用 minio 镜像自带的 `mc`，不额外拉已下架的 `minio/mc`）。

  > 曾试过把建桶塞进 `langfuse-minio` 的 entrypoint（省一个容器），
  > 但那样要把「后台起 server → 轮询就绪 → 建桶 → `wait $SRV` 交回前台」
  > 写在一个脚本里。服务进程与一次性初始化混在一起，`wait`/信号处理
  > 一旦写错容器会静默退出，排查成本高于多一个容器。**拆开更清楚**：
  > minio 只跑服务，init 只建桶然后退出。

**4. podman-compose 1.6 只支持一个 `service_completed_successfully` 依赖**

这是本步最耗时的坑。当 web 同时依赖两个一次性容器（当时是 `db-init`
和 `minio-init`）时，`up` 报：

```
Error: the following dependencies of container <web-id> are not running:
       <db-init-id>: container state improper
Error: unable to start container <worker-id>
```

表现是 **web 能起来，但 worker 永远停在 `Created`**，Langfuse 后台任务
全部不执行（trace 能进但不出现在 UI）。逐个排除：

| 配置 | 结果 |
|---|---|
| web 依赖 1 个 completed 容器 | ✅ 正常 |
| web 依赖 2 个 completed 容器 | ❌ worker 起不来 |
| 链式（db-init → minio-init → web） | ❌ 一次性容器带 completed 依赖同样失败 |

根因在 `podman_compose.py:1317`：它把所有 `depends_on`（不分 condition）
统统翻成 podman 的 `--requires`，而 `--requires` 语义是"目标必须**正在运行**"，
已退出的一次性容器必然不满足。

结论：**每个服务最多只挂一个 completed 依赖**。正好 `db-init` 本身也是
多余的（见上文 Prisma 自动建库），去掉后 web 只剩 `minio-init` 一个，
问题消失。

### 实测验证

```
$ podman compose --profile observability up -d
aide-postgres             Up (healthy)      ← 业务库，langfuse 库也在这
aide-langfuse-clickhouse  Up (healthy)
aide-langfuse-redis       Up (healthy)
aide-langfuse-minio       Up (healthy)
aide-langfuse-minio-init  Exited (0)        ← 建完 bucket 即退出
aide-langfuse-web         Up (healthy)
aide-langfuse-worker      Up                ← 必须 Up，不能停在 Created
```

库结构确认：

```
$ podman exec aide-postgres psql -U aide -d aide_agent -tAc "SELECT datname FROM pg_database"
aide_agent
langfuse

$ podman exec aide-postgres psql -U aide -d langfuse -tAc "SELECT count(*) FROM pg_tables WHERE schemaname='public'"
72      ← 由 Prisma 迁移自动创建，无需 init 容器
```

上报验证：

```
trace: chat  session=final-check  tags=['final']
```

跑一次带 `search_kb` 工具调用的图，用 Langfuse API 查回：

```
trace: chat  session=e2e-verify-001  user=42

observation 数: 9
  CHAIN  LangGraph          22.219ms   ← 根 span
  CHAIN  __start__           0.015ms
  CHAIN  route_input         0.013ms
  AGENT  agent               0.002ms
  CHAIN  tools_condition     0.001ms
  CHAIN  tools              22.195ms
  TOOL   search_kb          22.194ms
  AGENT  agent               0.002ms
  CHAIN  tools_condition     0.001ms
```

内容捕获确认：

```
input : {'messages': [['user', '怎么退货']]}
output: {'messages': [{'content': '怎么退货', ...}]}
tags  : ['e2e', 'verify']
TOOL input : {'query': '退货'}
TOOL output: {'content': '【promo-seckill.md | 秒杀与限时抢购规则/退货说明 | 相关度 0.74】...'}
```

**输入、输出、工具参数、工具返回、tags 全部落库**。

### 预置的开发账号

compose 里通过 `LANGFUSE_INIT_*` 自动创建，无需手动注册：

| 项 | 值 |
|---|---|
| UI | http://localhost:3000 |
| 账号 | `dev@aide-agent.local` / `dev-password-123` |
| 项目 Public Key | `pk-lf-aide-agent-dev` |
| 项目 Secret Key | `sk-lf-aide-agent-dev` |

`.env.example` 默认就指向自建实例（本地开发零配置可用）。

## 遗留问题

- **自建实例的密钥是开发默认值**。`NEXTAUTH_SECRET` / `SALT` / `ENCRYPTION_KEY`
  以及预置的项目 Key（`pk-lf-aide-agent-dev` / `sk-lf-aide-agent-dev`）都写在
  compose 里，仅适合本地开发。对外暴露或生产部署前必须全部替换
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
