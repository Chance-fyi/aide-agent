# step-42：限流与并发保护

## 目标

给 `/chat` 加两道闸：**按调用者限流**（防成本）和 **LLM 并发上限**（防拖垮）。

## 先看清风险到底在哪

改动前的事实：

| 事实 | 出处 |
|---|---|
| 每条用户消息触发一次 LLM 调用 | `_invoke_agent` |
| 单次调用 `timeout=30` + `max_retries=2` → 最坏占用 **约 90 秒** | `core/llm.py` |
| LangGraph 连接池 `max_size=10` | `agents/runtime.py` |
| 全仓无任何 rate limit / semaphore | 搜索确认 |

由此得到两个**性质不同**的风险：

1. **成本**：任何人写个脚本就能把 LLM 账单刷到任意额度
2. **可用性（更致命）**：10 个并发请求就能占满连接池，之后**所有用户**的对话
   一起排队。一个人可以拖垮整个服务——这比超支更难接受

所以要做两件事，而不是一件：

| 机制 | 防什么 | 粒度 |
|---|---|---|
| 滑动窗口限流 | 持续刷（成本、DB 写入量） | 按调用者身份，单位时间次数 |
| 并发闸门 | 瞬时打满（所有人的响应时间） | 全局，同时在跑的 LLM 调用数 |

## 实现

### 为什么不用 slowapi / limits

这类库围绕 HTTP 中间件设计（按 path + IP）。本项目有两点不匹配：

- 限流要按**登录账号**做（匿名才退化成 IP），中间件拿不到"业务身份"
- **WebSocket 消息不是 HTTP 请求**，中间件管不到

而业务逻辑统一收在 `process_message`（AGENTS.md 约束 7）。在那一处做限流，
比给两条通道各配一套中间件更不容易走偏。

### 滑动窗口，不是固定窗口

固定窗口在边界处允许 2 倍突发（59 秒发 20 条 + 61 秒再发 20 条 = 2 秒内 40 条）。
滑动窗口语义也更贴近用户直觉（"每分钟最多 N 条"），好写测试断言。

```python
class SlidingWindowLimiter:
    def allow(self, key: str) -> tuple[bool, int]:
        # 丢掉窗口外的记录，再判断是否还有名额
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if len(hits) >= self.max_events:
            retry_after = max(1, int(hits[0] - cutoff) + 1)   # 最早那条滑出才有名额
            return False, retry_after
        hits.append(now)
        return True, 0
```

`retry_after` 是**动态算的**（随时间递减），不是恒定值——否则提示会误导用户。

### 并发闸门：带超时等待，不是直接拒绝

```python
await asyncio.wait_for(self._semaphore().acquire(), timeout=self.wait_sec)  # 5s
```

- 直接拒绝 → 几个用户同时提问就被拒，体验差
- 无限等待 → 用户干等 90 秒，比拒绝更糟

折中：等 5 秒，拿不到就快速失败给"稍后再试"。

释放放在 `finally`：**LLM 超时/抛异常也必须归还名额**，否则几个失败请求就能把闸门永久占满。

闸门只包住 `_invoke_agent`（真正的昂贵资源），**不包落库**——落库是毫秒级，
限它反而会在繁忙时丢掉用户消息。

### 限流键：匿名必须按 IP

```python
def _rate_key(user, thread_id, client_ip) -> str:
    if user is not None:
        return f"user:{user['id']}"      # 登录：按账号
    if client_ip:
        return f"ip:{client_ip}"         # 匿名：按 IP
    return f"anon:{thread_id or 'unknown'}"
```

**为什么匿名不能用 thread_id**：它是客户端自己传的（localStorage 里的值），
每次换个新 ID 就是新的计数桶——等于没限流。

IP 能通过 `X-Forwarded-For` 伪造，所以它只用于"提高绕过成本"，
**不当成安全边界**。真要精确限流需要网关层配合，写在遗留问题里。

### 错误语义：429 与 503 分开

| 情况 | 状态码 | 文案 | 客户端该做什么 |
|---|---|---|---|
| 发太快 | **429** + `Retry-After` | "发送太频繁，请 N 秒后再试" | 退避 |
| 系统繁忙 | **503** + `Retry-After` | "当前咨询人数较多，请稍后重试" | 换时间重试 |

这两件事对用户的含义不同，混成一个码会让前端没法给正确提示。

WS 没有 HTTP 状态码，所以限流也要**推一条 `error` 事件**——
不告知的话用户只会看到消息"消失"。

## 验证

### 单元测试（`tests/test_ratelimit.py`，14 例）

| 测试 | 锁住什么 |
|---|---|
| `test_allows_up_to_limit_then_blocks` | 配额内放行、超出拒绝 |
| `test_keys_are_independent` | 一个人刷爆不牵连别人 |
| `test_window_slides_and_frees_quota` | 窗口滑过后配额恢复 |
| `test_retry_after_counts_down` | 等待提示随时间递减（不是恒定值） |
| `test_disabled_when_max_events_zero` | 配置 0 = 关闭 |
| `test_prune_drops_stale_keys` | 内存不随历史用户数无限增长 |
| `test_check_rate_raises_with_retry_after` | 异常带 `retry_after` |
| `test_guard_limits_concurrency` | 并发上限生效，超出者**失败而非无限等** |
| `test_guard_releases_on_exception` | 异常路径归还名额 |
| `test_guard_waits_for_available_slot` | 短暂占用时等待并成功 |
| `test_guard_disabled_when_zero` | 0 = 不限 |
| `test_rest_chat_returns_429_with_retry_after` | REST 返回 429 + `Retry-After` 头 |
| `test_rest_limit_is_per_identity` | 按账号隔离 |
| `test_ws_reports_rate_limit_as_error_event` | WS 推 `error` 事件而不是静默丢弃 |

### 真实压测

**限流**（同一账号连发 25 条关键词转接消息，不花 LLM）：

```
第 21 条被限流, Retry-After=59, detail=发送太频繁，请 59 秒后再试
...
状态码分布: 200×20  429×5
```

精确命中 `RATE_LIMIT_PER_MINUTE=20`。

**并发闸门**（8 个不同账号同时打 LLM 请求，闸门配置 4）：

```
并发 8 个请求，总耗时 37.9s
  #0: 503   5.4s  当前咨询人数较多，请稍后重试
  #1: 200  36.4s  ok
  #2: 200  37.9s  ok
  #3: 200  34.7s  ok
  #4: 200  37.4s  ok
  #5: 503   5.3s  当前咨询人数较多，请稍后重试
  #6: 503   5.3s  当前咨询人数较多，请稍后重试
  #7: 503   5.4s  当前咨询人数较多，请稍后重试

200×4  503(繁忙)×4  异常×0
```

恰好 4 个通过（= `LLM_MAX_CONCURRENCY`），其余 4 个在 5.4 秒后快速失败
（= `wait_sec`），而不是一起排队 90 秒。

### 全量

`uv run pytest`：**184 passed**；`ruff check .` clean。

## 变更文件

| 文件 | 变更 |
|---|---|
| `app/core/ratelimit.py` | **新增**：`SlidingWindowLimiter` / `ConcurrencyGuard` / `check_rate` |
| `app/core/config.py` | 新增 `rate_limit_per_minute`、`llm_max_concurrency` |
| `app/api/chat.py` | `process_message` 加限流 + 闸门；`_rate_key`；REST 翻译 429/503 |
| `app/api/ws.py` | 传 `client_ip`；限流/繁忙翻译成 `error` 事件 |
| `tests/test_ratelimit.py` | **新增**：14 例（单元 + 集成） |
| `tests/conftest.py` | **新增**：关掉 Langfuse、默认不自动迁移；共享 `ws_client` |
| `.env.example` / `README.md` / `AGENTS.md` | 配置项、能力表、新增踩坑记录 |

## 踩的坑

**测试把全局时钟冻住了。** 第一版用 `monkeypatch.setattr(ratelimit.time, "monotonic", ...)`
控制时间，但 `ratelimit.time` 就是标准库 `time` 模块——**asyncio 的事件循环时钟
就是 `time.monotonic`**。冻结之后所有 `asyncio.wait_for` 永不超时，
并发闸门测试直接挂死（跑到 300 秒超时）。

修法：给 `SlidingWindowLimiter` 加 `clock` 参数**注入时钟**，测试传假时钟，
不碰全局。这条写进了 AGENTS.md。

**顺手治了测试噪音。** 之前跑测试会刷满
`Transient error HTTPConnectionPool(host='localhost', port=3000)` ——
`.env` 里配了 Langfuse Key，SDK 一直在重试导出。新增的 `tests/conftest.py`
在任何 `app.*` 导入前设 `LANGFUSE_ENABLED=false`（必须在此之前，
因为 `get_settings()` 是 `lru_cache`），测试快了不少，日志也干净了。

**顺带修了 fixture 组织。** `ws_client` 原本定义在 `test_ws.py`，想在
`test_ratelimit.py` 里复用时只能 `from tests.test_ws import ws_client`，
触发 ruff 的 F811（参数名与导入名重定义）。已移到 `conftest.py`——
这才是 pytest 的标准做法。

## 遗留问题

- **计数器在进程内存，只对单进程精确**。多 worker 时实际限额 = 配置 × worker 数。
  和 `core/hub.py` 的连接表是同一个约束。跨进程精确限流需要 Redis
  （`INCR` + `EXPIRE`），届时换 `SlidingWindowLimiter` 的实现即可，调用点不动。
- **`X-Forwarded-For` 可伪造**。它只提高绕过成本，不是安全边界。
  生产应该在网关（nginx/envoy）层做真正的 IP 限流，应用层这份是"兜底"。
- **没有按租户/账号等级分级**。现在所有账号同一配额。将来若有 VIP 用户，
  需要按角色给不同限额（`_rate_key` 里可以带上角色）。
- **`wait_sec=5` 是拍的值**。若接入更慢的模型，这个值要跟着调；
  更好的是按模型 P95 延迟动态算。
- **限流被触发时用户消息不会重投**。前端收到 429/error 后只是提示，
  用户得手动重发。要做"自动排队重试"需要客户端消息 ID + 幂等去重。
- **没有限流指标上报**。触发次数、被拒率这些应该进 Langfuse 或 Prometheus
  才能调参。当前只有 WARNING 日志。
