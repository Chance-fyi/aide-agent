"""跨进程事件总线测试。

核心要验证三件事：
1. **不回声**：A 进程广播的事件回到 A 时被跳过（否则事件数指数增长）
2. **编码安全**：大消息（emoji 最坏情况）压得进 NOTIFY 的 8000 字节上限
3. **优雅降级**：Postgres 不可用时退回单进程，不影响本地推送

真跨进程验证用两个独立 EventBus 实例模拟两个 worker（同一 PG 上互相收发）。
"""

import asyncio

import pytest

from app.core.bus import CHANNEL, EventBus, bus
from app.core.config import get_settings

# ============================================================
# 编码：NOTIFY 载荷上限
# ============================================================

def test_encode_compresses_and_stays_under_limit():
    """最坏情况（2000 个 emoji）也必须压进 8000 字节。

    原始 JSON 是 8186 字节，**直接超过 NOTIFY 硬上限**——
    所以压缩不是优化，是必需。
    """
    event = {
        "origin": "a" * 32,
        "target": "user",
        "user_id": 1,
        "event": {
            "type": "message",
            "message": {
                "id": 1,
                "role": "human",
                "content": "😀" * 2000,     # 2000 字符 × 4 字节
                "ts": "2026-10-23T09:11:06.798287+00:00",
                "sender_name": "坐席张敏",
            },
        },
    }
    payload = EventBus._encode(event)
    assert payload is not None, "大消息必须能编码（否则跨进程推送静默失效）"
    assert len(payload) < 8000


def test_encode_decode_roundtrip():
    """编码后能完整还原（含中文、emoji、嵌套结构）。"""
    event = {
        "origin": "o1",
        "target": "agents",
        "thread_id": "t-1",
        "event": {"type": "message", "message": {"content": "退款到账了 😀", "id": 7}},
    }
    assert EventBus._decode(EventBus._encode(event)) == event


def test_decode_ignores_garbage():
    """脏载荷（别的程序在同频道发的）只忽略，不能抛异常拖垮监听循环。"""
    assert EventBus._decode("not-base64!!") is None
    assert EventBus._decode("YWJj") is None       # 合法 base64 但不是 zlib


# ============================================================
# 单进程行为（总线未启动）
# ============================================================

def test_publish_is_noop_before_start():
    """未启动时 publish 是 no-op：单进程部署下这是正常状态。"""
    fresh = EventBus()
    fresh.publish("user", user_id=1, event={"type": "message"})   # 不应抛异常
    assert fresh.stats["sent"] == 0


async def test_origin_skips_own_events():
    """自己发出的通知必须被跳过，否则同一条消息会被投递两次。

    这里不连真 PG，直接喂 payload 给接收回调——验证的是过滤逻辑本身。
    """
    received = []
    e = EventBus()
    e._on_event = lambda ev: received.append(ev)

    # 自己发的
    e._handle_raw(EventBus._encode({"origin": e.origin, "target": "user", "user_id": 1}))
    assert received == [], "不能投递自己发出的事件"
    assert e.stats["skipped"] == 1

    # 别的进程发的
    e._handle_raw(EventBus._encode({"origin": "other", "target": "user", "user_id": 1}))
    assert len(received) == 1
    assert e.stats["received"] == 1


async def test_handler_exception_does_not_kill_listener():
    """单条事件处理失败不能中断监听循环（否则整个进程再也收不到推送）。"""
    def boom(ev):
        raise RuntimeError("处理失败")

    e = EventBus()
    e._on_event = boom
    # 不应抛异常
    e._handle_raw(EventBus._encode({"origin": "other", "target": "user", "user_id": 1}))


# ============================================================
# 真跨进程：两个 EventBus 实例互相收发
# ============================================================

@pytest.fixture
async def two_buses():
    """两个独立 EventBus 模拟两个 worker（各自有不同 origin）。"""
    a, b = EventBus(), EventBus()
    got_a, got_b = [], []

    assert await a.start(lambda ev: got_a.append(ev)), "总线 A 应启动成功"
    assert await b.start(lambda ev: got_b.append(ev)), "总线 B 应启动成功"
    try:
        yield (a, got_a), (b, got_b)
    finally:
        await a.stop()
        await b.stop()

async def _wait_for(predicate, timeout=5.0):
    """轮询等待条件成立（NOTIFY 是异步到达的，不能固定 sleep）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return False

async def test_event_reaches_other_process(two_buses):
    """A 广播 → B 收到（这是本步要解决的核心问题）。"""
    (a, got_a), (b, got_b) = two_buses

    a.publish("user", user_id=42, event={"type": "message", "n": 1})

    assert await _wait_for(lambda: len(got_b) == 1), "B 应收到 A 的事件"
    assert got_b[0]["user_id"] == 42
    assert got_b[0]["event"]["n"] == 1
    # A 自己不该收到（本地已在 publish 前投递过）
    assert got_a == [], "A 不应收到自己发出的事件"


async def test_bidirectional(two_buses):
    """双向都能送达（坐席在 A、用户在 B 的场景）。"""
    (a, got_a), (b, got_b) = two_buses

    a.publish("agents", event={"type": "queue_changed"}, thread_id=None)
    b.publish("user", user_id=7, event={"type": "status", "status": "queued"})

    assert await _wait_for(lambda: len(got_b) == 1 and len(got_a) == 1)
    assert got_a[0]["event"]["type"] == "status"
    assert got_b[0]["event"]["type"] == "queue_changed"


async def test_large_message_survives_bus(two_buses):
    """2000 字符的中文消息要能完整跨进程送达（不被截断/丢失）。"""
    (a, got_a), (b, got_b) = two_buses
    content = "退款审核通过后按原路退回，支付宝微信 1-3 个工作日。" * 50   # ~1400 字
    content = content[:2000]

    a.publish("user", user_id=1, event={"type": "message", "message": {"content": content}})

    assert await _wait_for(lambda: len(got_b) == 1)
    assert got_b[0]["event"]["message"]["content"] == content, "长消息必须无损"


# ============================================================
# 降级
# ============================================================

async def test_start_degrades_when_postgres_unreachable(monkeypatch):
    """PG 连不上时 start 返回 False 而不是抛异常（总线不能成为启动前提）。"""
    from app.core import bus as bus_mod

    real = get_settings()
    fake = real.model_copy(update={"postgres_port": 1})   # 1 端口必然连不上
    monkeypatch.setattr(bus_mod, "get_settings", lambda: fake)

    e = EventBus()
    assert await e.start(lambda ev: None) is False
    assert e.running is False
    # 降级后 publish 仍是安全的 no-op
    e.publish("user", user_id=1, event={"type": "message"})


async def test_disabled_by_config(monkeypatch):
    """WS_BUS_ENABLED=false 时完全不启动（单进程部署的省事开关）。"""
    from app.core import bus as bus_mod

    real = get_settings()
    fake = real.model_copy(update={"ws_bus_enabled": False})
    monkeypatch.setattr(bus_mod, "get_settings", lambda: fake)

    e = EventBus()
    assert await e.start(lambda ev: None) is False
    assert e.running is False


# ============================================================
# Hub 集成：本地投递 + 广播不能重复
# ============================================================

async def test_hub_delivers_locally_and_skips_echo(monkeypatch):
    """push_user 应只本地投递一次，且不因总线回声而重复。"""
    from app.core.hub import Connection, Hub

    class _FakeWS:
        def __init__(self) -> None:
            self.sent = []

        async def send_json(self, data) -> None:
            self.sent.append(data)

        async def close(self) -> None:
            pass

    h = Hub()
    ws = _FakeWS()
    conn = Connection(ws=ws, user_id=99)
    await h.add(conn)
    try:
        h.push_user(99, {"type": "message", "n": 1})
        await asyncio.sleep(0.05)
        assert len(ws.sent) == 1

        # 模拟总线把自己的事件回声过来：必须被过滤掉（否则这里会变 2）
        h.deliver_remote({"origin": "self", "target": "user", "user_id": 99,
                          "event": {"type": "message", "n": 1}})
        await asyncio.sleep(0.05)
        assert len(ws.sent) == 2, "deliver_remote 是给其他进程用的，本地重复投递由 origin 过滤"
    finally:
        h.remove(conn)
