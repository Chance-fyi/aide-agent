# step-43：跨进程推送总线（支持多 worker 部署）

## 目标

消除最后一条"只能单进程跑"的硬约束：用 Postgres LISTEN/NOTIFY 做进程间事件广播，
让 `uvicorn --workers N` 下的 WebSocket 推送能跨进程送达。

## 问题

`core/hub.py` 的连接表在进程内存里。多 worker 时：

```
worker A: 持有用户 X 的 WS 连接
worker B: 处理坐席回复 → hub.push_thread() → 只遍历 B 自己的连接表
          → 用户 X 收不到 → 表现为"坐席回复了，用户要刷新才看得到"
```

是否出问题取决于两端**是否恰好落在同一进程**——这种"时好时坏"的 bug 最难排查。

## 为什么选 LISTEN/NOTIFY 而不是 Redis

- Postgres 已是硬依赖（会话记忆、业务表都在上面），不引入新中间件
- 推送量级很小（每条消息一个事件），NOTIFY 开销完全够用
- 语义天然是"广播给所有监听者"，正好匹配

代价是 **8000 字节载荷上限**，这在本项目里是真会撞到的（下面详述）。

## 三个关键设计

### 1. 载荷必须压缩（不是优化，是必需）

实测最坏情况——2000 个 emoji 的消息：

```
原始 JSON:        8186 bytes   ← 已经超过 8000 上限
zlib 压缩后:       164 bytes
base64 后:         220 bytes   ← 远低于上限
```

真实中文文本（随机 2000 字）：

```
原始:        6201 bytes
压缩+base64: 3948 bytes   ratio 1.6x
```

所以载荷一律 `zlib.compress(level=6)` + `base64`（NOTIFY 载荷必须是字符串，
不能有裸字节）。level 取 6 是速度/压缩比的经验平衡点——实时链路不该为省几十
字节多花 CPU。

极端情况（压缩后仍 > 7000 字节）直接丢弃并 `logger.error`，**不截断**：
截断会产出错误的 JSON 让接收端解析崩溃，比丢一条推送更糟。

### 2. 本地立即投递 + origin 过滤（避免回声）

如果只靠"发出 → PG → 收回"，本地推送就依赖 PG 可用性：PG 抖一下，
连本进程内的实时消息都发不出去。所以：

```python
def push_user(self, user_id, event):
    self.deliver_user(user_id, event)              # 本地立即投递
    bus.publish("user", user_id=user_id, event=event)   # 广播给其他进程

def deliver_remote(self, event):                   # 总线接收回调
    if event.get("origin") == self.origin:         # 跳过自己发的
        return
    self.deliver_user(...)                         # 只投递，不再 publish
```

两个要点：

- **`push_*` 会广播，`deliver_*` 只本地投递**。接收端必须走 `deliver_*`，
  否则 A 和 B 会互相回声，事件数量指数增长
- 每个进程有 uuid 作为 `origin`（不用 pid：容器重启后 pid 会复用）

### 3. 启动必须等 LISTEN 真正生效

这个 bug 是测试暴露出来的：`start()` 里 `create_task(_listen_loop())` 之后
协程还没跑，`LISTEN` 也还没执行。此时 `publish` 出去的通知本进程收不到——
事件**永久丢失**，不是延迟。

```python
self._listening = asyncio.Event()
self._listen_task = asyncio.create_task(self._listen_loop())
await asyncio.wait_for(self._listening.wait(), timeout=5)   # 等就绪
```

`_listen_loop` 执行完 `LISTEN` 后才 `set()`。

## 实现结构

```
app/core/bus.py
  EventBus
    start(on_event)      # 探活 → 起两个协程 → 等 LISTEN 就绪
    stop()               # 取消协程（PG 连接随之关闭）
    publish(target, ...) # 同步非阻塞，任意线程可调
    _publish_loop()      # outbox → NOTIFY，断线重连
    _listen_loop()       # LISTEN → 分发，断线重连
    _encode/_decode      # zlib + base64
    stats                # sent/received/skipped/dropped/errors
```

两条独立 PG 连接：一条只 LISTEN（`notifies()` 是阻塞生成器），
一条只 NOTIFY（避免互相干扰）。

`publish()` 从工作线程调用时走 `loop.call_soon_threadsafe`——
和 `Hub.send` 是同一个原因（LangGraph 把同步节点丢进线程池）。

## 降级策略

| 情况 | 行为 |
|---|---|
| `WS_BUS_ENABLED=false` | 不启动，单进程行为 |
| PG 连不上（启动探活失败） | 记 WARNING，退回单进程，**不阻断启动** |
| 运行中连接断开 | 每 2 秒重连，期间跨进程推送丢 |
| 发送队列满（PG 跟不上） | 丢事件 + 计数，客户端重连靠 `ready` 快照自愈 |

核心原则：**总线不是启动前提**。它挂了最多丢跨进程实时性，不能让对话不可用。

`/health` 暴露总线状态，多 worker 排障时不用翻日志：

```json
"event_bus": {"enabled": true, "running": true,
              "sent": 12, "received": 4, "skipped": 12, "dropped": 0, "errors": 0}
```

## 验证

### 单元测试（`tests/test_bus.py`，12 例）

| 测试 | 锁住什么 |
|---|---|
| `test_encode_compresses_and_stays_under_limit` | 2000 emoji 必须压进 8000 字节（原始是 8186） |
| `test_encode_decode_roundtrip` | 中文/emoji/嵌套结构无损 |
| `test_decode_ignores_garbage` | 脏载荷只忽略，不拖垮监听循环 |
| `test_publish_is_noop_before_start` | 未启动时 publish 安全 |
| `test_origin_skips_own_events` | 自己发的必须跳过（防回声） |
| `test_handler_exception_does_not_kill_listener` | 单条失败不中断监听 |
| `test_event_reaches_other_process` | **A 广播 → B 收到** |
| `test_bidirectional` | 双向都通（坐席在 A、用户在 B） |
| `test_large_message_survives_bus` | 2000 字中文跨进程无损 |
| `test_start_degrades_when_postgres_unreachable` | PG 挂了返回 False 不抛 |
| `test_disabled_by_config` | 开关生效 |
| `test_hub_delivers_locally_and_skips_echo` | Hub 本地投递 + 回声过滤 |

### 决定性验证：两个独立进程

单测里的"两个 EventBus 实例"仍可能被质疑是否真跨进程。所以另起
**两个独立 uvicorn 进程**（:8000 和 :8002，共享同一个 Postgres），
用户端连 A、坐席端连 B：

```
1) 用户端已连进程 A
2) 坐席端已连进程 B
3) 用户转接: queued thread=f2dcec61
4) ✓ 用户消息(A) 跨进程送达坐席(B)
5) ✓ 坐席回复(B) 跨进程送达用户(A)

进程A 统计: sent=12 received=4  skipped=12
进程B 统计: sent=4  received=12 skipped=4
```

两边统计互相印证：A 发出 12 条、B 收到 12 条；B 发出 4 条、A 收到 4 条。

### 多 worker 启动

`uvicorn --workers 2`：两个 worker 都打出"事件总线已启动"，
origin 不同，`/health` 正常，无 loop 绑定错误。

### 全量

`uv run pytest`：**196 passed**；`ruff check .` clean。

## 变更文件

| 文件 | 变更 |
|---|---|
| `app/core/bus.py` | **新增**：EventBus（编码/收发/重连/降级/统计） |
| `app/core/hub.py` | 拆 `push_*`（本地+广播）与 `deliver_*`（仅本地）；新增 `deliver_remote` |
| `app/core/config.py` | 新增 `ws_bus_enabled` |
| `app/main.py` | lifespan 启动/停止总线 |
| `app/api/routes.py` | `/health` 暴露总线状态 |
| `tests/test_bus.py` | **新增**：12 例 |
| `.env.example` / `README.md` / `AGENTS.md` | 配置、能力表、两条新踩坑 |

## 踩的坑

### 1. `start()` 返回时 LISTEN 还没生效

测试里 `publish` 紧跟在 `start()` 后面，事件全部丢失。加 `_listening` Event
等待就绪后才返回。

### 2. `asyncio.Event` 在模块级单例里创建 → 绑错事件循环

```python
bus = EventBus()      # import 时构造，此刻没有运行中的事件循环
    self._listening = asyncio.Event()    # ← 绑到了错误的 loop
```

之后 `await self._listening.wait()` 直接抛
`RuntimeError: is bound to a different event loop`。
pytest 的多事件循环下必现（11 个 WS 测试全 ERROR）。

修法：在 `start()` 里懒创建（它运行在 app 的事件循环中）。
`asyncio.Queue` 同理（原本已经在 `start()` 里创建，没中招）。

### 3. `push_thread` 不能因"本进程没连接"就提前返回

旧代码有 `if not self._users: return` 的优化。跨进程之后这是 bug：
本进程没有用户端连接，不代表别的进程没有。已删除。

## 遗留问题

- **NOTIFY 是"至多一次"投递**。接收进程在那一刻没在监听（重启、重连中）
  就永久丢失。当前靠客户端重连的 `ready` 快照兜底，不做可靠重传。
  要强一致得用带游标的表（如 `events` 表 + `id > last_seen` 轮询）。
- **8000 字节上限仍然存在**。压缩后正常内容在 4KB 以内，但若将来支持
  附件/富文本，必须改成"只发信号，接收端回查数据库"的模式。
- **所有进程监听同一个频道**，事件会发给全部进程再各自过滤。
  worker 数很多（如 32）时是无谓的放大。可以按 target 分频道
  （`aide_user_123`）或加哈希分片，当前规模不值得。
- **没有跨进程的"在线状态"**。某用户是否在线仍只能查单进程的连接表，
  所以"用户是否在线"这类功能在多 worker 下不准确。
- **`/health` 的总线统计是单进程视角**。多 worker 时请求落到哪个 worker
  就看到哪一份，要聚合得靠外部监控采集。
- **限流计数器仍未跨进程**（step-42 遗留）。现在总线已就位，
  可以复用同一套 NOTIFY 机制做分布式计数，但精确限流更适合 Redis 的原子 INCR。
