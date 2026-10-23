"""WebSocket 连接中心：把"谁在线、事件推给谁"从业务逻辑里剥离出来。

为什么需要这一层：双端实时消息（坐席回复、转接状态、队列变化）本质是
**服务端主动推送**。轮询把实时性交给客户端（4 秒延迟 + 大量空转请求），
推送则把节奏收回服务端。业务代码只说"这个会话有新消息"，
"该发给哪些连接"由这里决定。

部署约束（必须知道）：连接表在**进程内存**里，因此只在单进程部署下成立。
多 worker 时 A 进程的推送到不了 B 进程的连接，需要引入 Redis Pub/Sub 或
Postgres LISTEN/NOTIFY 做跨进程总线——本步不做，见 docs/step-39.md 的遗留项。

发送模型：每条连接一个 outbox 队列 + 一个写协程。
为什么不直接 await ws.send_json：
1. 推送是"广播"场景，一个慢/半死连接会把整条业务链路拖住；
2. 多个协程并发写同一个 WebSocket 会交错帧，协议层就坏了。
入队是同步非阻塞的，慢连接只会丢自己的事件（队列满即丢弃并记日志），
客户端重连时靠 ready 快照自愈。
"""

import asyncio
import logging
from dataclasses import dataclass, field

from starlette.websockets import WebSocket

from app.agents import handoff

logger = logging.getLogger(__name__)

# 单连接待发事件上限：满说明对端读得太慢（或已是半死连接）。
# 丢事件比无限吃内存好——重连时的 ready 快照会把状态重新对齐
OUTBOX_MAX = 100

@dataclass(eq=False)
class Connection:
    """一条 WebSocket 连接及其推送上下文。

    eq=False：连接按**对象身份**比较。默认 dataclass 会按字段比较，
    两个字段相同的连接会被 set 去重（比如同一用户的两个标签页），
    推送就会少发一路。
    """

    ws: WebSocket
    user_id: int | None = None          # 用户端连接：归属账号（推送寻址用）
    agent_id: int | None = None         # 坐席端连接：账号 ID
    # 坐席正在查看的会话：UI 一次只看一个会话，所以用"覆盖"而非"累加"语义
    watching: set[str] = field(default_factory=set)
    outbox: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=OUTBOX_MAX))
    writer: asyncio.Task | None = None
    # 连接所属事件循环：推送可能来自别的线程（见 Hub.send），
    # 跨线程投递必须回到这个循环里做
    loop: asyncio.AbstractEventLoop | None = None

    @property
    def is_agent(self) -> bool:
        return self.agent_id is not None

class Hub:
    """连接注册表 + 推送入口。所有 push 方法都是同步非阻塞的。"""

    def __init__(self) -> None:
        # 用户端按账号索引：一个账号可能多端在线（手机 + 电脑），都要收到
        self._users: dict[int, set[Connection]] = {}
        # 坐席端不按账号索引：推送是"广播给所有在线坐席"或"给正在看该会话的坐席"，
        # 不需要按账号寻址
        self._agents: set[Connection] = set()

    # ---------- 连接生命周期 ----------

    async def add(self, conn: Connection) -> None:
        """注册连接并启动写协程（写协程的生命周期 = 连接的生命周期）。"""
        conn.loop = asyncio.get_running_loop()
        conn.writer = asyncio.create_task(self._write_loop(conn))
        if conn.is_agent:
            self._agents.add(conn)
        elif conn.user_id is not None:
            self._users.setdefault(conn.user_id, set()).add(conn)

    def remove(self, conn: Connection) -> None:
        """注销连接并收掉写协程（连接断开时由端点的 finally 调用）。"""
        self._agents.discard(conn)
        if conn.user_id is not None:
            peers = self._users.get(conn.user_id)
            if peers is not None:
                peers.discard(conn)
                # 该账号最后一端下线就把字典项删掉，避免账号越多内存越涨
                if not peers:
                    self._users.pop(conn.user_id, None)
        if conn.writer is not None:
            # 不 cancel 会留下悬挂任务，测试里还会报 "Task was destroyed"
            conn.writer.cancel()
            conn.writer = None

    def watch(self, conn: Connection, thread_id: str) -> None:
        """设置坐席当前查看的会话（覆盖语义，见 Connection.watching 注释）。"""
        conn.watching = {thread_id}

    # ---------- 推送 ----------

    def send(self, conn: Connection, event: dict) -> None:
        """向指定连接投递一个事件（入队，不等待实际发送）。

        为什么统一走 call_soon_threadsafe 而不是直接入队：推送的调用方不一定
        在事件循环线程上。LangGraph 把同步节点丢进线程池执行，而转接状态变更
        （handoff.mark_pending_human）恰好就发生在那里；跨线程直接操作
        asyncio.Queue 会叫不醒正在等待的写协程，事件被拖到下一次循环唤醒才发出。
        """
        if conn.loop is None or conn.loop.is_closed():
            # 未注册的连接（单测里的裸 Connection）/ 应用已在关闭：直接入队
            self._enqueue(conn, event)
            return
        conn.loop.call_soon_threadsafe(self._enqueue, conn, event)

    def _enqueue(self, conn: Connection, event: dict) -> None:
        try:
            conn.outbox.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning(
                "推送队列已满，丢弃事件 type=%s user=%s agent=%s",
                event.get("type"), conn.user_id, conn.agent_id,
            )

    def push_user(self, user_id: int | None, event: dict) -> None:
        """推给某个用户端账号的所有在线连接。"""
        if user_id is None:
            return
        # tuple() 快照：遍历期间连接可能被移除（单事件循环下虽不会并发，
        # 但 remove 发生在别的协程里，快照能让语义不依赖这个隐含前提）
        for conn in tuple(self._users.get(user_id, ())):
            self.send(conn, event)

    def push_thread(self, thread_id: str, event: dict) -> None:
        """按会话归属推给对应用户端（匿名会话无归属，推不到，属预期）。

        这里要查一次归属：用户端的连接是按账号索引的，而推送方只知道
        thread_id。主键级查询，开销可忽略。
        """
        if not self._users:
            return  # 没有用户端在线，连查询都省了
        self.push_user(handoff.get_thread_user(thread_id), event)

    def push_agents(self, event: dict, thread_id: str | None = None) -> None:
        """推给坐席端。

        thread_id 为 None → 所有在线坐席（队列类事件）；
        否则只推给正在看该会话的坐席（会话内消息，避免无关坐席被打扰）。
        """
        for conn in tuple(self._agents):
            if thread_id is not None and thread_id not in conn.watching:
                continue
            self.send(conn, event)

    # ---------- 写协程 ----------

    async def _write_loop(self, conn: Connection) -> None:
        """把 outbox 里的事件逐条写出：单连接串行写，帧不会交错。"""
        try:
            while True:
                event = await conn.outbox.get()
                await conn.ws.send_json(event)
        except asyncio.CancelledError:
            raise
        # 这里必须兑下所有异常（BLE001 显式豁免）：推送失败的原因可能是断网、
        # 关页面、协议错乱，无论哪种处理方式都一样（关掉连接）；
        # 而且推送是旁路，不能把异常抛给业务链路
        except Exception as exc:  # noqa: BLE001
            # 对端已断开（或半开连接）：主动关 ws，让读协程立刻收到 disconnect
            # 走清理流程，而不是一直阻塞在 receive 上。
            # 只记 info：正常关页面/切网也会走到这里，报 error 会淹没真正的故障
            logger.info(
                "WebSocket 推送失败(%s)，关闭连接 user=%s agent=%s",
                exc, conn.user_id, conn.agent_id,
            )
            try:
                await conn.ws.close()
            except Exception:
                logger.debug("关闭失效连接时再次失败", exc_info=True)

# 进程级单例：连接表本来就是进程级状态，多实例没有意义
hub = Hub()
