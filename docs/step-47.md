# step-47：全量代码 review —— 修复 3 个真 bug + 连接池优化 + 拆分执行层

## 目标

对全仓做一次系统性 review，找真实缺陷并修掉，顺带做有明确收益的重构。

**原则**：只改"能说清危害"的地方。没找到证据的"风格问题"不动——
否则 review 会变成大范围无收益改动，风险大于收益。

## 发现并修复的 3 个真 bug

### BUG 1：`/chat/{thread_id}/history` 丢掉 `id` 与 `sender_name`

**证据**（实测）：

```
DB 层 get_messages 返回: [{'id': 2680, 'role': 'user', ...},
                          {'id': 2681, 'role': 'human', 'sender_name': '坐席小王', ...}]
history 端点构造的:      [{'id': None, ...}, {'id': None, 'sender_name': None, ...}]
→ id 丢失: True    → sender_name 丢失: True
```

原因：端点手工挑字段 `SessionMessage(role=..., content=..., ts=...)`，
而 `handoff.get_messages` 明明返回了 `id` 和 `sender_name`。

**危害**：

1. `id` 是前端跨路径去重的唯一依据（step-40.1 修的"重复气泡"就靠它）。
   坐席端 `openThread` 用这个端点加载历史，拿到的是**没有 id 的消息**；
   WS 推来的同一条消息带 id → `mergeMessage` 找不到匹配 → 追加 → 重复。
2. `sender_name` 丢失 → 坐席端历史里人工消息显示"人工坐席"而不是
   "坐席小王"，看不出是谁回复的。

**修法**：整包透传 `SessionMessage(**r)`。

**回归测试**：新增 2 条，并验证过它们**在回退修复后确实失败**（不是摆设）。

### BUG 2：`set_thread_user` 打乱排队 FIFO，且让超时判断失真

**证据**（实测）：

```
队列顺序（应先 t1 后 t2）: ['t1', 't2']
补发消息后队列顺序:        ['t2', 't1']    ← t1 被挪到队尾
```

原因：`set_thread_user` 的 UPSERT 里有 `updated_at = now()`，而这个函数
**每条消息都会调用**（`_resolve_thread` 里补归属）。队列排序原先用
`ORDER BY updated_at ASC`。

**危害**：

1. 排队中的用户只要补发一句话就被挪到队尾 → **可能被无限饿死**
2. `updated_at` 同时兼职"排队超时判断" → 同一个字段被两个语义拉扯

根因是**一个字段承担了两个语义**：`updated_at` 既是"最后变化时间"
又是"队列排序依据"又是"超时依据"。

**修法**（迁移 v3）：拆出 `queued_at` 专管"何时进队列"

```sql
ALTER TABLE conversation_status ADD COLUMN IF NOT EXISTS queued_at TIMESTAMPTZ;
UPDATE conversation_status SET queued_at = updated_at
    WHERE status = 'queued' AND queued_at IS NULL;   -- 回填老数据
```

配套改动：

| 位置 | 改动 |
|---|---|
| `mark_pending_human` | 进队列时写 `queued_at = now()`；**已在排队中不刷新**（否则反复触发转接就能让超时永不生效） |
| `list_live_threads` | `ORDER BY COALESCE(queued_at, created_at) ASC` |
| `sweep_timed_out` | 用 `queued_at` 判超时，离开队列时置 NULL |
| `set_thread_user` | **不再碰 `updated_at`**（归属写入不是业务状态变化） |

**回归测试**：新增 2 条（FIFO 不被补发打乱、重复转接不重置排队时间）。

### BUG 3：流式中途失败 → 光标永久闪烁

**危害**：LLM 在流式**途中**报错时，后端发 `error` 事件；前端 `case "error"`
只追加了一条错误气泡，**没有给流式占位气泡收尾** → `streaming` 一直是
`true`，那个光标会永远闪下去，用户以为还在生成。

**修法**：

- 前端：收到 `error` 时先收尾占位气泡。有半截内容就保留并标 `aborted`，
  完全空白的直接丢掉
- 新增 `.aborted-tag`（"生成中断"）——告诉用户这段没生成完、刷新会消失，
  而不是让人对着半句话猜是不是系统坏了

## 顺带修的 4 个健壮性问题

| 问题 | 修法 |
|---|---|
| `bus.publish` 读 `asyncio.Queue` 的私有 `_loop` 属性 | `start()` 时显式保存 `self._loop` |
| 对话失败/被闸门拦下时 `CHAT_MESSAGES` 不计数 → 错误率不可见 | 加 `failed` / `busy` 两个 outcome |
| `main.py` 预热任务是"发射后不管"，关闭时无法取消 | 保存引用并在 shutdown 时 cancel |
| 坐席端快速连点工单时，慢响应会覆盖当前面板 | `loadUserHistory` / `openThread` 回写前校验 `selectedRef.current` |

## 重构 1：拆出 `app/agents/executor.py`

`app/api/chat.py` 原本 665 行，混了四类职责。把"怎么执行 Agent + 怎么翻译
LLM 异常"抽到 `app/agents/executor.py`（201 行）：

- `executor.invoke()` / `executor.stream()` —— 非流式 / 流式执行
- `executor.classify_llm_error()` —— 异常 → (指标标签, 用户文案)
- `executor.AgentError` —— 与 HTTP 无关的失败类型

**顺带消掉一处重复**：原先 `_friendly_llm_error` 和 `_error_kind` 各写了一遍
isinstance 判断链，顺序必须一致（`APITimeoutError` 是 `APIConnectionError`
的子类）。改一处忘另一处就会让"指标归类"和"用户文案"对不上。
现在合并成一张有序表 `_LLM_ERRORS`，顺序只存在一处。

`chat.py` 665 → 533 行，且只留业务编排（守卫 → 落库 → 调用 → 推送）。

**入口层保留薄包装**（`_invoke_agent` / `_stream_agent`）：把 `AgentError`
转成 `HTTPException`。执行层不该知道 HTTP 语义——WS 入口拿到的是事件不是状态码。

## 重构 2：同步 DB 连接池（性能优化，收益最大）

先量化再动手。统计一轮对话（不含 LLM）的同步 DB 调用：

```
一轮对话 14 次同步 DB 往返，累计阻塞事件循环 82.5 ms
  append_message        x2   26.34 ms
  get_status            x2   19.16 ms
  set_thread_user       x1   17.76 ms
  get_user_current_thread x1   9.75 ms
  get_thread_user       x1    9.45 ms
```

根因：三处 `_get_conn()` 都是 `psycopg.connect(...)` —— **每次 DB 调用
新建一条 TCP 连接**。

**两个后果**：

1. **连接数爆炸**：14 条新连接/轮次。Postgres 默认 `max_connections=100`，
   几十个用户同时对话就能把库连满（然后所有请求一起失败）
2. **事件循环被卡**：这些调用是同步的，在 `async def` 里直接跑，
   期间所有 WebSocket 连接与其他 HTTP 请求都停摆

**修法**：新增 `app/core/db.py`，进程级 `psycopg_pool.ConnectionPool`
（`psycopg[pool]` 已是现有依赖，不引新包）。三处 `_get_conn()` 改为从池借连接，
调用方代码一行没改（`with _get_conn() as conn:` 用法完全一致）。

**效果**：

```
改前：14 次调用，阻塞 82.5 ms
改后：14 次调用，阻塞 14.8 ms      ← 5.6x
```

另外在 `/health` 暴露池状态（`size` / `available` / `waiting`），
`available` 长期为 0 说明并发把池占满，该扩池或查慢查询。

## 验证

### 全量

`uv run pytest`：**250 passed**（246 + 4 条新回归）；`ruff check .` clean；
`oxlint` / `vite build` 通过。

### 回归测试的有效性

BUG 1 的两条新测试专门验证过：把修复回退掉，测试**确实失败**；
恢复修复后通过。这样保证它们不是"写了但测不出问题"的摆设。

### 真环境端到端

```
1) 流式 OK: 140 个 delta, reply_end id=3101
2) 历史 id 对齐 OK: 2 条, 全部带 id
3) FIFO: 补发前位置 [91, 92] → 补发后 [91, 92]  OK
4) 转人工后连发 OK: 3/3 条都有 sent 确认
/health db_pool: {open: true, size: 2, available: 2, waiting: 0}
```

## 变更文件

| 文件 | 变更 |
|---|---|
| `app/core/db.py` | **新增**：同步连接池 |
| `app/agents/executor.py` | **新增**：Agent 执行 + LLM 异常分类 |
| `app/core/migrations.py` | 迁移 v3：`conversation_status.queued_at` |
| `app/agents/handoff.py` | FIFO 改按 `queued_at`；`set_thread_user` 不再改时间戳；用连接池 |
| `app/api/chat.py` | 665→533 行；修 history 丢 id；补失败/繁忙指标；用连接池 |
| `app/api/routes.py` | `/health` 暴露连接池状态 |
| `app/core/bus.py` | 显式保存事件循环，去掉私有属性访问 |
| `app/core/users.py`、`app/rag/kb_store.py` | 用连接池 |
| `app/main.py` | 关闭时释放连接池；预热任务纳入生命周期 |
| `web/src/components/UserChat.jsx` | 错误时收尾流式气泡 |
| `web/src/components/MessageBubble.jsx` | 支持 `aborted` 标注 |
| `web/src/components/AgentConsole.jsx` | 历史加载加会话守卫 |
| `tests/test_api.py` | +2 条 history 回归；修打桩点（get_graph 已移到 executor） |
| `tests/test_handoff.py` | +2 条 FIFO 回归 |
| `tests/conftest.py` | 会话结束时关闭连接池 |

## 踩的坑

**打桩点又一次失效。** 把 `get_graph` 从 `chat.py` 移到 `executor.py` 后，
`tests/test_api.py` 里 `monkeypatch.setattr(chat_mod, "get_graph", boom)`
静默失效——测试不报错，而是**真的去连模型**，表现为超时而不是断言失败。

这是 AGENTS.md 里已经记过的坑（step-45 踩过一次），这次是同类问题重现。
说明那条纪律需要更靠前的检查手段，不能只靠记忆。

**连接池不关会在解释器退出时报错。** `psycopg_pool` 的 `__del__` 会 join
内部线程，而解释器退出阶段不允许 join：
`PythonFinalizationError: cannot join thread at interpreter shutdown`。
lifespan 里加了 `db.close()`；测试用 ASGITransport 不跑 lifespan，
所以在 conftest 加了 session 级清理。

## 遗留问题

- **同步 DB 仍在事件循环里跑**。连接池把 82ms 降到 15ms，但严格来说
  仍是阻塞调用。完整解法是全链路异步（`psycopg.AsyncConnection` +
  改所有调用方），改动面覆盖 `handoff` / `users` / `kb_store` 全部函数。
  当前规模下不值得，但并发再上一个量级时必须做。
- **`history` 端点的 `sender_name` 只对 human 消息有值**。agent 消息
  没有 sender（本来就该是"智能客服"），前端逻辑正确，但字段语义偏隐晦。
- **`chat.py` 仍有 533 行**。还能再拆（会话记录读写 / 三条分支 / 路由），
  但进一步拆会让"一轮对话的完整流程"需要跨三个文件才能读懂，
  可读性反而下降。当前切分点（执行 vs 编排）是最自然的。
- **`updated_at` 的语义仍然模糊**。现在它只表示"状态最后变化时间"，
  但没有任何地方依赖这个语义——将来若需要"最后活跃时间"，应再拆一列。
- **review 没有覆盖 `app/eval/`**（跑分器、指标）。它只在离线脚本里用，
  不进服务链路，优先级低。
- **没有静态类型检查**。`mypy`/`pyright` 能抓到"字段名拼错""返回值用错"
  这类问题（本步的 BUG 1 就属于这类），但引入需要大量类型标注工作。
