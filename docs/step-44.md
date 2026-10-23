# step-44：知识库管理 UI

## 目标

给知识库一套**生产可用**的管理界面：看状态、改切分策略、重建索引、验证检索。

## 问题：生产环境根本没有更新知识库的入口

改动前的现状：

```
重建唯一入口：POST /debug/kb/rebuild
                ↑ 只在 DEBUG=true 时注册，生产必须关掉
```

后果是生产上改语料只能：

1. 改文件 → **重启服务**（而重建逻辑根本没在启动时跑，重启也不生效）
2. 或者临时开 `DEBUG=true` → 把一整排内部调试接口暴露出去

也就是说：知识库事实上是"上线后就冻结"的。这属于功能缺口，不是体验问题。

## 方案：正式路由 `/kb` + 坐席鉴权

新建 `app/api/kb_admin.py`，与 `debug_routes` 明确分工：

| 路由 | 用途 | 可用性 | 鉴权 |
|---|---|---|---|
| `/kb/*` | 管理操作（状态、策略、重建、检索验证） | **始终可用** | 坐席登录 |
| `/debug/kb/*` | 开发期诊断（内存版对照检索、策略对比） | 仅 `DEBUG=true` | 无 |

为什么用坐席账号而不是新增"管理员"角色：项目只有 `user` / `agent` 两级，
知识库是所有坐席的公共资产。加第三级角色会牵动鉴权、Token、前端登录门卫
一整条链路，收益不足以支撑成本。

## 关键设计

### 1. 重建必须是异步任务（实测 24 秒）

```
全量重建实测：46 文件 → 199 块，耗时 24.0 秒
```

同步接口会：撞反向代理超时（常见 30s，边界极危险）、前端只能转圈、
用户可能重复点击。所以：

```
POST /kb/rebuild  → 202 + 任务记录（立刻返回）
GET  /kb/rebuild  → 轮询状态（前端每 1.5 秒）
```

### 2. 任务状态存 Postgres，不存进程内存

多 worker 下，**发起重建的请求和查询状态的请求可能落到不同进程**。
存内存会让另一个 worker 永远看到"空闲"，进而允许并发重建。

新增表（迁移 v2）：

```sql
CREATE TABLE kb_rebuild_run (
    id BIGSERIAL PRIMARY KEY,
    status TEXT NOT NULL,            -- running / succeeded / failed
    triggered_by TEXT,               -- 触发人（审计）
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    chunks INTEGER,
    error TEXT                       -- 用户可读的失败原因
);
-- 同一时刻只能有一个 running
CREATE UNIQUE INDEX ... ON kb_rebuild_run ((status)) WHERE status = 'running';
```

### 3. 互斥交给数据库，不靠应用层判断

`WHERE status='running'` 的部分唯一索引：并发插入直接报唯一冲突 → 409。
应用层"先查再插"有竞争窗口，两个请求可能都查到空闲然后都插入。

### 4. 僵尸任务必须能自愈

进程在重建中被 kill（部署、OOM）时，那条 `running` 会永远留着，
**之后所有重建都被永久拒绝**。所以启动新任务时先清理：

```python
UPDATE kb_rebuild_run SET status='failed', ...
WHERE status='running' AND started_at < now() - interval '10 minutes'
```

10 分钟远大于正常重建的 24 秒，留足慢机器/大语料余量。

### 5. 「是否需要重建」用文件 mtime 判断

语料是文件系统上的事实，索引是数据库里的快照。比较 `文件 mtime` 与
`kb_document.updated_at`，就能准确得出"哪些文件改过但没重建"。

这是管理台**最有价值的信号**——否则线上检索悄悄用着旧内容，没人知道。
界面把它放在最上方，并在文件行上打「待重建」徽章。

### 6. 路径穿越防护

`/kb/documents/chunks?path=` 直接拼文件路径，如果不校验，
`?path=../../.env` 就能读到 `LLM_API_KEY`。所以只接受纯文件名：

```python
if "/" in path or "\\" in path or path.startswith("."):
    raise HTTPException(422, "path 只能是文件名")
```

有 5 条参数化测试专门锁这个行为。

## 界面

新增 `/kb` 页面（顶栏第三个 tab），坐席登录后可见：

```
┌ 知识库 ────────────────────────────── [管理员] [退出] ┐
│ 语料 → 切块 → 向量索引。改完需要重建才生效。            │
├──────────────────────────────────────────────────────┤
│ ⚠ 3 个文件的语料比索引新，线上检索仍是旧内容            │
├──────────────────────────────────────────────────────┤
│ 文件 46 │ 切块 199 │ 父块 199 │ 向量库 199 │ 集合 …    │
├──────────────────────────────────────────────────────┤
│ [重建索引]  ✓成功  1 分钟前  199 块  耗时 20.0s  by …  │
├────────────────────────────┬─────────────────────────┤
│ 语料文件           46       │ 切块预览                 │
│ ┌──────────┬──────┬───┬────┐│ #19485 · 隐私与数据管理  │
│ │文件      │策略  │块 │状态││  / 收集范围 · 88 字      │
│ │account-… │[默认]│ 5 │已索引││ ## 收集范围            │
│ │refund.md │header│ 7 │待重建││ 账号信息（手机号…）     │
│ └──────────┴──────┴───┴────┘│                         │
│                             │ 检索验证  [生产链路]     │
│                             │ [退款多久到账]  [检索]   │
│                             │ 0.757 · refund.md        │
└────────────────────────────┴─────────────────────────┘
```

沿用 step-40 的 Instrument Minimal 设计系统：墨色阶、发丝线、等宽数字、
零装饰阴影。**重建按钮是整屏唯一的主按钮**（一屏最多一个实心色块）。

检索结果里分数低于阈值的标红——用颜色 + 数字本身双重表达，
色弱用户也能判断。

## 验证

### 后端测试（`tests/test_kb_admin.py`，18 例）

| 测试 | 锁住什么 |
|---|---|
| `test_requires_login` | 未登录一律 401（含只读 overview） |
| `test_rejects_normal_user` | 普通用户不能碰（重建影响所有用户） |
| `test_agent_can_read_overview` | 坐席能读，结构完整 |
| `test_chunks_rejects_path_traversal` ×4 | `../AGENTS.md`、`.env`、带目录的路径全部 422 |
| `test_strategy_rejects_path_traversal` | 写接口同样只接受纯文件名 |
| `test_chunks_404_for_missing_file` | 目录内不存在 → 404（不是空列表） |
| `test_rebuild_is_async_and_reports_status` | 202 + 轮询到 succeeded + 块数 |
| `test_concurrent_rebuild_returns_409` | 并发只成功一次（真实阻塞 0.6s 制造窗口） |
| `test_failed_rebuild_reports_friendly_error` | 失败给用户可读原因，堆栈只进日志 |
| `test_stale_running_is_cleared` | 20 分钟前的 running 被判定中断，不阻塞 |
| `test_friendly_reason_mapping` | 异常→文案映射，兜底不泄露 message |
| `test_status_response_is_self_consistent` | **响应不自相矛盾**（见下） |
| `test_search_reports_not_indexed_as_409` | 未建库 → 409 而非 500 |

### 真实环境（真后端 + 真浏览器）

```
1) 未登录应显示登录页: True
2) 首屏: 文件=46 切块=199 父块=199 向量库=199 集合=kb_chunks_v3_2e3f2f27
   文档行 46  重建按钮="重建索引"  横向溢出=false  阴影=0
3) 切块预览: account-privacy.md → 5 块
   #19485 · 隐私与数据管理 / 收集范围 · 88 字
4) 检索验证: 3 命中  0.757 / 0.731 / 0.701
5) 重建中: 按钮="重建进行中…" disabled=true 徽章="重建中"
6) 重建完成: 成功 · 199 块 · 耗时 20.0s · by 知识库管理员
```

视觉/无障碍断言（3 个视口：1440 / 820 / 375）全部 OK：
阴影 0、渐变 0、横向溢出 0、对比度全过、控件可访问名称齐全、
表格 `th` 带 `scope`、有 `h1`。

### 全量

`uv run pytest`：**214 passed**（196 + 18）；`ruff` clean；`oxlint` / `vite build` 通过。

## 变更文件

| 文件 | 变更 |
|---|---|
| `app/api/kb_admin.py` | **新增**：`/kb/overview` `/documents` `/documents/chunks` `/documents/strategy` `/rebuild` `/search` |
| `app/rag/rebuild.py` | **新增**：后台重建任务（DB 状态、部分唯一索引互斥、僵尸清理） |
| `app/rag/kb_store.py` | 新增 `file_stats()` / `recent_chunks()` |
| `app/rag/store.py` | 新增 `collection_info()`（别名/块数/残留旧集合） |
| `app/core/migrations.py` | 迁移 **v2**：`kb_rebuild_run` 表 + running 部分唯一索引 |
| `app/main.py` | 注册 `/kb` 路由 |
| `web/src/components/KbConsole.jsx` | **新增**：管理界面 |
| `web/src/App.jsx` | 新增 `/kb` 路由与顶栏 tab |
| `web/src/index.css` | 新增知识库页面样式（复用同一套 token） |
| `tests/test_kb_admin.py` | **新增**：18 例 |
| `tests/conftest.py` | `client` fixture 移到 conftest 共享 |

## 踩的坑

### 1. `GET /kb/rebuild` 会返回自相矛盾的响应

路由原先分两次查库：

```python
latest = job.latest()      # 查到 running
running = job.is_running() # 这期间后台完成了 → False
```

响应就变成 `job.status='running'` + `running=false`。前端据此停止轮询，
却仍显示"重建中"——最难解释的那类 bug。

修法：`snapshot()` 一次查询同时给出 `{job, running}`，两者必然一致。
并加了一条测试逐帧断言 `running == (job.status == 'running')`。

### 2. 迁移测试硬编码了版本号

新增 v2 后，`test_migrations.py` 里 `assert applied == [1]` 立刻挂掉；
更隐蔽的是 `test_failed_migration_rolls_back_and_raises` 用了固定版本号 2，
和真实 v2 撞号。已改为从 `migrations.MIGRATIONS` 推导
（`ALL_VERSIONS` / `LATEST`），以后加迁移不用再改测试。

### 3. 测试清理写成了全表 DELETE

`_clean_rebuild_rows` 最初 `DELETE FROM kb_rebuild_run`，会清掉开发库里
真实的重建历史。改成只删 `triggered_by = '知识库测试'` 的行——
测试不该破坏开发数据（和 step-41 的教训同类）。

## 遗留问题

- **没有"清理残留旧集合"的操作**。界面能显示"残留旧集合 N 个"，
  但没有按钮清理。蓝绿重建失败时旧集合会累积，白占内存。
- **不能编辑语料内容**。只能看切块、改策略、重建。真正的知识库管理
  还需要在线编辑 Markdown / 上传文件，那涉及版本控制与冲突处理，是另一个量级。
- **重建期间旧索引继续服务**（蓝绿切换保证），但**新语料在重建完成前不可见**。
  界面提示了这一点，但没有"预计完成时间"。
- **没有重建历史列表**。只有"最近一次"。排障时想看"上周三那次为什么失败"
  需要直接查库。
- **检索验证不支持批量/回归**。单条查询够日常自检，但改完语料想跑
  黄金集回归，还得用 `scripts/run_eval.py`。
- **`/kb` 与 `/agent` 共用坐席登录态**，同一浏览器登出坐席会同时影响两页。
  可接受（本来就是同一身份），但值得知道。
- **重建并发上限是 1**。语料很大时可以支持"分批重建"，当前全量 24 秒
  还不需要。
