"""跨进程事件总线（Postgres LISTEN/NOTIFY）。

**解决什么问题**

`core/hub.py` 的连接表在进程内存里，因此多 worker 部署时：
A 进程发出的推送到不了 B 进程持有的连接。表现为"坐席回复后，
用户端要刷新才看得到"——取决于两者是否恰好落在同一进程。

**为什么选 LISTEN/NOTIFY 而不是 Redis**

- Postgres 已经是硬依赖（会话记忆、业务表都在上面），不引入新中间件
- 推送量级很小（每条消息一个事件），NOTIFY 的开销完全够用
- 语义天然是"广播给所有监听者"，正好匹配需求

代价：**8000 字节载荷上限**。这在本项目里是真会撞到的——一条 2000 字符的
中文消息 JSON 后约 6200 字节，emoji 最坏情况 8186 字节，直接超限。
所以载荷一律 zlib 压缩 + base64（NOTIFY 载荷必须是字符串，不能有裸字节）。
实测：真实中文文本压到 1.6x，emoji 文本压到 49x，都远低于上限。

**为什么本地也立即投递，而不是等自己的通知回来**

如果只靠"发出→PG→收回"，本地推送就会依赖 PG 可用性：PG 抖一下，
连本进程内的实时消息都发不出去。所以策略是：
本地立即投递 + 广播给其他进程，接收端按 `origin` 跳过自己的消息。

**降级策略（重要）**

PG 不可用 / 未启用时，总线静默失效，`Hub` 退回单进程行为。
实时性在单进程内不受影响，跨进程的推送会丢——客户端重连时的
`ready` 全量快照会自愈。这比"因为总线挂了导致对话不可用"好得多。
"""

import asyncio
import base64
import json
import logging
import uuid
import zlib

import psycopg

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# 频道名：所有进程监听同一个，靠 payload 里的 target 字段区分投递范围
CHANNEL = "aide_events"

# Postgres NOTIFY 的载荷硬上限是 8000 字节；留出余量给信封字段
_MAX_PAYLOAD = 7000

class EventBus:
    """进程间事件广播。

    生命周期由 `main.lifespan` 管理（start/stop）。
    未启动时 `publish()` 是 no-op——单进程部署下这是完全正常的。
    """

    def __init__(self) -> None:
        # 本进程唯一标识：接收端据此跳过自己发出的消息，避免重复投递。
        # 用 uuid 而不是 pid：容器/重启后 pid 会复用，uuid 不会
        self.origin = uuid.uuid4().hex
        self._listen_task: asyncio.Task | None = None
        self._publish_task: asyncio.Task | None = None
        self._outbox: asyncio.Queue | None = None
        self._on_event = None
        self._running = False
        # LISTEN 真正生效的信号。为什么需要：start() 里 create_task 之后
        # 协程还没跑，`LISTEN` 也还没执行；此时 publish 出去的通知
        # 本进程收不到——事件会**永久丢失**（不是延迟）。
        # start() 必须等到它被 set 才能返回。
        #
        # 注意必须懒创建（在 start() 里），不能在 __init__ 建：
        # 模块级单例 `bus = EventBus()` 在 import 时构造，那时没有运行中的
        # 事件循环，Event 会被绑定到错误的 loop，start() 里一 await 就报
        # "is bound to a different event loop"（pytest 多事件循环下必现）
        self._listening: asyncio.Event | None = None
        # 统计：排障时能看出"到底有没有跨进程收发"（暴露在 /health）
        self.stats = {"sent": 0, "received": 0, "skipped": 0, "dropped": 0, "errors": 0}

    @property
    def running(self) -> bool:
        return self._running

    # ---------- 生命周期 ----------

    async def start(self, on_event) -> bool:
        """启动监听与发送协程；返回是否成功（失败时总线降级为不可用）。

        on_event(event: dict) 会在**每个**来自其他进程的事件上被调用，
        由调用方决定投递给本进程的哪些连接。
        """
        s = get_settings()
        if not s.ws_bus_enabled:
            logger.info("跨进程事件总线已关闭（WS_BUS_ENABLED=false），退回单进程模式")
            return False

        self._on_event = on_event
        self._outbox = asyncio.Queue(maxsize=1000)
        self._listening = asyncio.Event()   # 绑定当前事件循环（见 __init__ 注释）
        try:
            # 启动前先探一次连通性：连不上就明确降级，不要让后台任务反复报错
            await self._probe()
        except Exception:
            logger.warning(
                "跨进程事件总线不可用（Postgres 连接失败），退回单进程模式："
                "多 worker 部署时跨进程推送会丢，客户端重连靠 ready 快照自愈",
                exc_info=True,
            )
            self.stats["errors"] += 1
            return False

        self._running = True
        self._listening.clear()   # type: ignore[union-attr]
        self._listen_task = asyncio.create_task(self._listen_loop())
        self._publish_task = asyncio.create_task(self._publish_loop())
        try:
            # 等 LISTEN 生效再返回，否则启动瞬间的事件会丢（见 _listening 注释）
            await asyncio.wait_for(self._listening.wait(), timeout=5)
        except TimeoutError:
            logger.warning("事件总线监听未在 5 秒内就绪，跨进程推送可能丢早期事件")
        logger.info("跨进程事件总线已启动 channel=%s origin=%s", CHANNEL, self.origin[:8])
        return True

    async def stop(self) -> None:
        """停止总线。协程取消失败只记日志，不阻断进程退出。"""
        self._running = False
        for task in (self._publish_task, self._listen_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.debug("事件总线协程退出异常", exc_info=True)
        self._listen_task = None
        self._publish_task = None
        self._outbox = None
        if self._listening is not None:
            self._listening.clear()

    async def _probe(self) -> None:
        """确认 Postgres 可连（3 秒超时，别把启动拖太久）。"""
        s = get_settings()
        conn = await psycopg.AsyncConnection.connect(
            s.postgres_dsn, autocommit=True, connect_timeout=3
        )
        await conn.close()

    # ---------- 发送 ----------

    def publish(self, target: str, **fields) -> None:
        """广播一个事件给其他进程（同步非阻塞，可从任意线程调用）。

        target 取值：
        - "user"   → fields 需带 user_id
        - "agents" → fields 可带 thread_id（None 表示全体坐席）
        """
        if not self._running or self._outbox is None:
            return

        event = {"origin": self.origin, "target": target, **fields}
        loop = self._outbox._loop if hasattr(self._outbox, "_loop") else None
        # 推送可能来自工作线程（LangGraph 同步节点），必须回到事件循环入队
        if loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(self._enqueue, event)
                return
            except RuntimeError:
                pass   # 循环正在关闭，落到下面的直接入队
        self._enqueue(event)

    def _enqueue(self, event: dict) -> None:
        if self._outbox is None:
            return
        try:
            self._outbox.put_nowait(event)
        except asyncio.QueueFull:
            # 发送队列满 = PG 写入跟不上。丢事件而不是无限吃内存，
            # 客户端重连时靠 ready 快照自愈
            self.stats["dropped"] += 1
            logger.warning("事件总线发送队列已满，丢弃事件 target=%s", event.get("target"))

    async def _publish_loop(self) -> None:
        """把 outbox 里的事件逐条 NOTIFY。连接断了自动重连。"""
        s = get_settings()
        while self._running:
            conn = None
            try:
                conn = await psycopg.AsyncConnection.connect(s.postgres_dsn, autocommit=True)
                while self._running and self._outbox is not None:
                    event = await self._outbox.get()
                    payload = self._encode(event)
                    if payload is None:
                        continue
                    await conn.execute("SELECT pg_notify(%s, %s)", (CHANNEL, payload))
                    self.stats["sent"] += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                self.stats["errors"] += 1
                logger.warning("事件总线发送失败，2 秒后重连", exc_info=True)
                await asyncio.sleep(2)
            finally:
                if conn is not None:
                    try:
                        await conn.close()
                    except Exception:
                        logger.debug("关闭发送连接失败", exc_info=True)

    @staticmethod
    def _encode(event: dict) -> str | None:
        """压缩 + base64。超限返回 None（记日志后跳过）。"""
        raw = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        # level=6 是速度/压缩比的经验平衡点；实时链路不该为了省几十字节多花 CPU
        payload = base64.b64encode(zlib.compress(raw, 6)).decode("ascii")
        if len(payload) > _MAX_PAYLOAD:
            # 正常内容到不了这里（实测最坏 4KB）。真到了说明有异常大的消息，
            # 与其截断成错误的 JSON 让接收端崩，不如丢掉并大声记日志
            logger.error(
                "事件超过 NOTIFY 载荷上限（%d > %d），跳过跨进程投递 target=%s",
                len(payload), _MAX_PAYLOAD, event.get("target"),
            )
            return None
        return payload

    @staticmethod
    def _decode(payload: str) -> dict | None:
        try:
            raw = zlib.decompress(base64.b64decode(payload))
            return json.loads(raw)
        except Exception:
            # 可能是别的程序在同频道发的消息 / 版本不兼容：忽略即可
            logger.debug("无法解析的总线事件，已忽略", exc_info=True)
            return None

    # ---------- 接收 ----------

    async def _listen_loop(self) -> None:
        """LISTEN 并逐条分发；连接断了自动重连。"""
        s = get_settings()
        while self._running:
            conn = None
            try:
                conn = await psycopg.AsyncConnection.connect(s.postgres_dsn, autocommit=True)
                await conn.execute(f"LISTEN {CHANNEL}")
                if self._listening is not None:
                    self._listening.set()   # 就绪信号：start() 在等它
                logger.debug("事件总线已监听 %s", CHANNEL)
                # notifies() 会一直等到有通知为止（阻塞但可被 cancel）
                async for notify in conn.notifies():
                    self._handle_raw(notify.payload)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.stats["errors"] += 1
                logger.warning("事件总线监听中断，2 秒后重连", exc_info=True)
                await asyncio.sleep(2)
            finally:
                if conn is not None:
                    try:
                        await conn.close()
                    except Exception:
                        logger.debug("关闭监听连接失败", exc_info=True)

    def _handle_raw(self, payload: str) -> None:
        event = self._decode(payload)
        if event is None:
            return
        # 跳过自己发出的：本地已经在 publish 前投递过了，
        # 不跳过会造成同一条消息重复推送
        if event.get("origin") == self.origin:
            self.stats["skipped"] += 1
            return
        self.stats["received"] += 1
        try:
            self._on_event(event)
        except Exception:
            # 单条事件处理失败不能拖垮监听循环，否则整个进程都收不到推送了
            logger.exception("处理总线事件失败 event=%s", event.get("target"))

# 进程级单例
bus = EventBus()
