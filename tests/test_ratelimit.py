"""限流与并发保护测试。

两类用例：
1. 单元：滑动窗口与并发闸门本身的行为（纯内存，快）
2. 集成：REST / WS 入口在超限时的实际响应（429 / error 事件）
"""

import asyncio

import pytest

from app.core import ratelimit
from app.core.ratelimit import (
    BusyError,
    ConcurrencyGuard,
    RateLimitExceeded,
    SlidingWindowLimiter,
    check_rate,
)

# ============================================================
# 滑动窗口
# ============================================================

def test_allows_up_to_limit_then_blocks():
    """配额内放行，超出拒绝。"""
    lim = SlidingWindowLimiter(max_events=3, window_sec=60)
    for _ in range(3):
        assert lim.allow("u1")[0] is True
    ok, retry_after = lim.allow("u1")
    assert ok is False
    assert retry_after >= 1


def test_keys_are_independent():
    """不同调用者互不影响——一个人刷爆不该牵连别人。"""
    lim = SlidingWindowLimiter(max_events=2, window_sec=60)
    lim.allow("a")
    lim.allow("a")
    assert lim.allow("a")[0] is False
    assert lim.allow("b")[0] is True, "b 的配额应与 a 无关"


def test_window_slides_and_frees_quota():
    """窗口滑过后配额恢复。"""
    now = [1000.0]
    lim = SlidingWindowLimiter(max_events=2, window_sec=10, clock=lambda: now[0])

    assert lim.allow("k")[0] is True
    assert lim.allow("k")[0] is True
    assert lim.allow("k")[0] is False

    now[0] += 11          # 两条记录都滑出窗口
    assert lim.allow("k")[0] is True


def test_retry_after_counts_down():
    """retry_after 应随时间递减，而不是恒定值（否则提示会误导用户）。

    注意时钟是**注入**的，不是 monkeypatch 全局 time.monotonic ——
    asyncio 的事件循环时钟就是 time.monotonic，冻结它会让所有
    `asyncio.wait_for` 永不超时（本步真踩过：并发闸门测试因此挂死）。
    """
    now = [1000.0]
    lim = SlidingWindowLimiter(max_events=1, window_sec=60, clock=lambda: now[0])

    lim.allow("k")
    _, first = lim.allow("k")
    now[0] += 30
    _, later = lim.allow("k")

    assert first > later, f"等待时间应递减：{first} -> {later}"


def test_disabled_when_max_events_zero():
    """配置为 0 = 关闭限流（本地调试用）。"""
    lim = SlidingWindowLimiter(max_events=0, window_sec=60)
    for _ in range(100):
        assert lim.allow("k")[0] is True


def test_prune_drops_stale_keys():
    """过期 key 会被清理，内存不随历史用户数无限增长。"""
    now = [1000.0]
    lim = SlidingWindowLimiter(max_events=5, window_sec=10, clock=lambda: now[0])
    for i in range(5):
        lim.allow(f"k{i}")
    assert len(lim._hits) == 5

    now[0] += 20          # 全部过期
    lim.prune(max_keys=1)
    assert lim._hits == {}


def test_check_rate_raises_with_retry_after(monkeypatch):
    """check_rate 超限时抛带 retry_after 的异常（路由层据此给 Retry-After 头）。"""
    ratelimit.reset()
    monkeypatch.setattr(
        ratelimit, "get_settings",
        lambda: type("S", (), {"rate_limit_per_minute": 1, "llm_max_concurrency": 0})(),
    )
    try:
        check_rate("user:1")
        with pytest.raises(RateLimitExceeded) as ei:
            check_rate("user:1")
        assert ei.value.retry_after >= 1
    finally:
        ratelimit.reset()


# ============================================================
# 并发闸门
# ============================================================

async def test_guard_limits_concurrency():
    """同时最多 N 个通过；超出者在等待超时后失败。"""
    guard = ConcurrencyGuard(max_concurrent=2, wait_sec=0.1)

    await guard.acquire()
    await guard.acquire()
    with pytest.raises(BusyError):
        await guard.acquire()   # 第三个必须失败（而不是无限等）

    guard.release()
    await guard.acquire()       # 释放后能再拿到
    guard.release()
    guard.release()


async def test_guard_releases_on_exception():
    """异常路径也必须归还名额，否则几个失败请求就永久占满闸门。"""
    guard = ConcurrencyGuard(max_concurrent=1, wait_sec=0.1)

    with pytest.raises(RuntimeError):
        await guard.acquire()
        try:
            raise RuntimeError("LLM 炸了")
        finally:
            guard.release()

    # 名额已归还，能再次拿到
    await guard.acquire()
    guard.release()


async def test_guard_waits_for_available_slot():
    """有人短暂占用时，后来者应该等待并成功，而不是立刻被拒。"""
    guard = ConcurrencyGuard(max_concurrent=1, wait_sec=1.0)

    async def holder():
        await guard.acquire()
        await asyncio.sleep(0.05)
        guard.release()

    task = asyncio.create_task(holder())
    await asyncio.sleep(0.01)
    await guard.acquire()     # 应等到 holder 释放
    guard.release()
    await task


def test_guard_disabled_when_zero():
    """配置为 0 = 关闭闸门。"""
    guard = ConcurrencyGuard(max_concurrent=0)
    # 不抛异常即可
    asyncio.run(guard.acquire())
    guard.release()


# ============================================================
# 集成：入口行为
# ============================================================

@pytest.fixture
def tight_limit(monkeypatch):
    """把限流收紧到 1 条/分钟，并重置单例让配置生效。"""
    from app.api import chat as chat_mod

    ratelimit.reset()
    fake = chat_mod.get_settings().model_copy(
        update={"rate_limit_per_minute": 1, "llm_max_concurrency": 0}
    )
    monkeypatch.setattr(chat_mod, "get_settings", lambda: fake)
    monkeypatch.setattr(ratelimit, "get_settings", lambda: fake)
    yield
    ratelimit.reset()


async def test_rest_chat_returns_429_with_retry_after(client, tight_limit):
    """REST 超限应返回 429 + Retry-After 头，而不是 500 或静默吞掉。

    用关键词转接路径（不依赖真实 LLM），第一次成功、第二次被限流。
    """
    r1 = await client.post("/chat", json={"message": "转人工"})
    assert r1.status_code == 200

    r2 = await client.post("/chat", json={"message": "转人工"})
    assert r2.status_code == 429
    assert "频繁" in r2.json()["detail"]
    assert int(r2.headers["Retry-After"]) >= 1


async def test_rest_limit_is_per_identity(client, tight_limit):
    """限流按账号隔离：A 被限不影响 B。"""
    from app.core.security import create_token
    from app.core.users import create_user

    u1 = create_user(f"rl_a_{__import__('uuid').uuid4().hex[:8]}", "secret123", "user", "A")
    u2 = create_user(f"rl_b_{__import__('uuid').uuid4().hex[:8]}", "secret123", "user", "B")
    h1 = {"Authorization": f"Bearer {create_token(u1['id'], 'user')}"}
    h2 = {"Authorization": f"Bearer {create_token(u2['id'], 'user')}"}

    assert (await client.post("/chat", json={"message": "转人工"}, headers=h1)).status_code == 200
    assert (await client.post("/chat", json={"message": "转人工"}, headers=h1)).status_code == 429
    # 换个账号应该还能发
    assert (await client.post("/chat", json={"message": "转人工"}, headers=h2)).status_code == 200


def test_ws_reports_rate_limit_as_error_event(ws_client, tight_limit, monkeypatch):
    """WS 超限应推一条 error 事件（WS 没有 HTTP 状态码可看）。

    不告知的话用户只会看到消息"消失"。
    """
    import uuid as _uuid

    from app.api import chat as chat_mod

    # 打桩 LLM：本用例验证的是限流，不该花真实模型调用（也会让测试变慢）
    async def _fake_invoke(thread_id, message, user_id=None):
        return "好的"

    monkeypatch.setattr(chat_mod, "_invoke_agent", _fake_invoke)

    username = f"rl_ws_{_uuid.uuid4().hex[:8]}"
    reg = ws_client.post(
        "/auth/register", json={"username": username, "password": "secret123"}
    ).json()

    def recv_until(ws, wanted, limit=10):
        for _ in range(limit):
            ev = ws.receive_json()
            if ev.get("type") == wanted:
                return ev
        raise AssertionError(f"没等到 {wanted}")

    with ws_client.websocket_connect(f"/ws/chat?token={reg['token']}") as user_ws:
        recv_until(user_ws, "ready")

        # 第一条：走关键词转接（不花 LLM），拿到 sent 确认
        user_ws.send_json({"type": "message", "content": "转人工"})
        assert recv_until(user_ws, "sent")["status"]

        # 第二条：超限，应收到 error 而不是被静默丢弃
        user_ws.send_json({"type": "message", "content": "再转人工"})
        ev = recv_until(user_ws, "error")
        assert "频繁" in ev["message"]
