"""WebSocket 双端实时推送测试。

两类用例：
1. 推送缓冲策略单测（纯内存，不需要服务端）
2. 端点联调：真握手 + 真推送，验证"消息推给了谁、推的是什么"

为什么用 starlette 的 TestClient 而不是 httpx：ASGITransport 不支持
WebSocket 握手。且必须用 `with TestClient(...)` 让 portal（事件循环）
活到整个模块——用户端与坐席端两条连接要互相推送，跨事件循环操作同一个
asyncio.Queue 属于未定义行为。

LLM 一律打桩（monkeypatch `_invoke_agent`）：本文件验证的是推送链路，
"答得好不好"由评估集覆盖，不该在这里依赖真实模型。
"""

import asyncio
import uuid

import psycopg
import pytest
from pytest import MonkeyPatch
from starlette.testclient import TestClient

from app.agents import handoff
from app.api import chat as chat_mod
from app.core.config import get_settings
from app.core.hub import OUTBOX_MAX, Connection, hub
from app.core.security import ROLE_AGENT, create_token
from app.core.users import create_user, ensure_table
from app.main import app

# ============================================================
# 夹具与工具
# ============================================================

async def _noop_sweeper() -> None:
    """替代真实的排队超时巡检：不 sleep、不查库，保持测试环境干净。"""
    return

@pytest.fixture(scope="module")
def ws_client():
    """模块级共享一个 TestClient（详见文件头说明）。

    生命周期里顺手关掉两个与推送无关的副作用：
    - tracing.init/shutdown：.env 配了 Langfuse 时会在收尾阶段尝试网络 flush，
      测试不该依赖外部服务
    - 队列超时巡检：后台 UPDATE 会偷偷改掉别的用例造的数据
    """
    mp = MonkeyPatch()
    mp.setattr("app.core.tracing.init", lambda: False)
    mp.setattr("app.core.tracing.shutdown", lambda: None)
    mp.setattr("app.main._queue_sweeper", _noop_sweeper)
    ensure_table()
    with TestClient(app) as client:
        yield client
    mp.undo()

def _fake_invoke(reply: str):
    """替换 `_invoke_agent`：绕开 LLM 与 Checkpointer，只验证推送。"""

    async def _invoke(thread_id, message, user_id=None):
        return reply

    return _invoke

def _register(client: TestClient) -> dict:
    """注册一个用户端账号，返回 AuthResponse（含 token/username）。"""
    username = f"ws_user_{uuid.uuid4().hex[:8]}"
    resp = client.post(
        "/auth/register",
        json={"username": username, "password": "secret123", "display_name": "WS用户"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()

def _agent_token() -> str:
    """建一个坐席账号并签发 Token（坐席不开放注册，只能直接建号）。"""
    username = f"ws_agent_{uuid.uuid4().hex[:8]}"
    user = create_user(username, "test-password", ROLE_AGENT, "WS坐席")
    return create_token(user["id"], ROLE_AGENT)

def _scalar(sql: str, params: tuple) -> str:
    """查一个标量（测试里只读，直接用同步 psycopg 最省事）。"""
    with psycopg.connect(get_settings().postgres_dsn, autocommit=True) as conn:
        row = conn.execute(sql, params).fetchone()
    assert row, f"查询无结果: {sql}"
    return row[0]

def _user_id(username: str) -> int:
    return _scalar("SELECT id FROM app_user WHERE username = %s", (username,))

def _current_thread(username: str) -> str:
    return _scalar(
        "SELECT cs.thread_id FROM conversation_status cs "
        "JOIN app_user u ON u.id = cs.user_id "
        "WHERE u.username = %s ORDER BY cs.updated_at DESC LIMIT 1",
        (username,),
    )

def _recv_until(ws, wanted: str, limit: int = 10) -> dict:
    """读到指定类型的事件为止（中间可能夹着 status/pong 等无关事件）。

    用计数而非超时做上限：读不到就是协议没按约定推送，应该立刻失败而不是挂着。
    """
    for _ in range(limit):
        ev = ws.receive_json()
        if ev.get("type") == wanted:
            return ev
    raise AssertionError(f"没等到 {wanted} 事件")

# ============================================================
# 推送缓冲策略
# ============================================================

def test_outbox_drops_events_when_full():
    """慢连接只丢自己的事件，不阻塞推送方、不吃爆内存。

    这是"一个卡死的浏览器标签页不能拖垮所有人"的底线保证。
    """
    conn = Connection(ws=None)   # 只测缓冲策略，不碰真实 ws
    for i in range(OUTBOX_MAX + 20):
        hub.send(conn, {"type": "x", "i": i})
    assert conn.outbox.qsize() == OUTBOX_MAX
    # 保留的是最早的 OUTBOX_MAX 条：先到的业务事件（如 ready 快照）不能被后来的挤掉
    assert conn.outbox.get_nowait()["i"] == 0

async def test_hub_routes_by_user_and_watch():
    """推送寻址：用户端按账号、坐席端按"是否正在看这个会话"。"""
    class _FakeWS:
        def __init__(self) -> None:
            self.sent = []

        async def send_json(self, data) -> None:
            self.sent.append(data)

        async def close(self) -> None:
            pass

    user_ws, agent_a, agent_b = _FakeWS(), _FakeWS(), _FakeWS()
    user_conn = Connection(ws=user_ws, user_id=424242)
    watcher = Connection(ws=agent_a, agent_id=1)
    idle = Connection(ws=agent_b, agent_id=2)
    for conn in (user_conn, watcher, idle):
        await hub.add(conn)
    hub.watch(watcher, "thread-x")

    try:
        hub.push_user(424242, {"type": "message", "n": 1})
        hub.push_agents({"type": "queue_changed"})
        hub.push_agents({"type": "message", "n": 2}, thread_id="thread-x")
        await asyncio.sleep(0.05)   # 等写协程把 outbox 里的东西发出去

        assert [e["type"] for e in user_ws.sent] == ["message"]
        # 队列类事件所有坐席都收
        assert {"type": "queue_changed"} in agent_a.sent
        assert {"type": "queue_changed"} in agent_b.sent
        # 会话内消息只推给正在看该会话的坐席
        assert {"type": "message", "n": 2} in agent_a.sent
        assert {"type": "message", "n": 2} not in agent_b.sent
    finally:
        # 必须清理：写协程是任务，留着会污染后续用例的事件循环
        for conn in (user_conn, watcher, idle):
            hub.remove(conn)

# ============================================================
# 用户端端点
# ============================================================

def test_ws_chat_rejects_bad_token(ws_client):
    """无效 Token：发一条 error 事件后按 4401 关闭，前端据此回登录页。"""
    with ws_client.websocket_connect("/ws/chat?token=not-a-token") as ws:
        ev = ws.receive_json()
    assert ev["type"] == "error"
    assert ev["code"] == 4401

def test_ws_chat_rejects_foreign_origin(ws_client):
    """非白名单 Origin 拒绝握手：WebSocket 不受同源策略约束，必须服务端自己挡。"""
    auth = _register(ws_client)
    with ws_client.websocket_connect(
        f"/ws/chat?token={auth['token']}", headers={"origin": "http://evil.example"}
    ) as ws:
        ev = ws.receive_json()
    assert ev["type"] == "error"
    assert ev["code"] == 4403

def test_ws_chat_ready_then_reply(ws_client, monkeypatch):
    """连接即拿全量快照；发消息后回复由服务端推送回来（不再有 HTTP 响应体）。"""
    auth = _register(ws_client)
    monkeypatch.setattr(chat_mod, "_invoke_agent", _fake_invoke("这是测试回复"))

    with ws_client.websocket_connect(f"/ws/chat?token={auth['token']}") as ws:
        ready = _recv_until(ws, "ready")
        # 新用户：空历史 + 自助状态
        assert ready["messages"] == []
        assert ready["status"] == handoff.STATUS_ACTIVE

        ws.send_json({"type": "message", "content": "你好"})
        ev = _recv_until(ws, "message")
        assert ev["message"]["role"] == "agent"
        assert ev["message"]["content"] == "这是测试回复"
        # 服务端时间戳：前端时间轴不能因为双端时钟差异而倒挂
        assert ev["message"]["ts"]

def test_ws_chat_reports_validation_error(ws_client):
    """空消息：回一条 error 事件而不是断开连接（一条脏数据不该踢掉整条连接）。"""
    auth = _register(ws_client)
    with ws_client.websocket_connect(f"/ws/chat?token={auth['token']}") as ws:
        _recv_until(ws, "ready")
        ws.send_json({"type": "message", "content": "   "})
        ev = _recv_until(ws, "error")
        assert "不合法" in ev["message"]

        # 连接仍然可用：紧接着发一条合法消息应能正常走通
        ws.send_json({"type": "ping"})
        assert _recv_until(ws, "pong")["type"] == "pong"

# ============================================================
# 坐席端端点
# ============================================================

def test_ws_agent_requires_agent_role(ws_client):
    """用户端 Token 不能连坐席通道（角色门卫）。"""
    auth = _register(ws_client)
    with ws_client.websocket_connect(f"/ws/agent?token={auth['token']}") as ws:
        ev = ws.receive_json()
    assert ev["type"] == "error"
    assert ev["code"] == 4401

def test_ws_agent_receives_user_message(ws_client, monkeypatch):
    """用户发消息 → 正在看该会话的坐席立刻收到（替代 4 秒轮询的关键链路）。"""
    auth = _register(ws_client)
    monkeypatch.setattr(chat_mod, "_invoke_agent", _fake_invoke("好的"))
    agent_token = _agent_token()

    with ws_client.websocket_connect(f"/ws/chat?token={auth['token']}") as user_ws:
        _recv_until(user_ws, "ready")
        user_ws.send_json({"type": "message", "content": "第一句"})
        _recv_until(user_ws, "message")          # 等第一条回复，确保会话已建立
        thread_id = _current_thread(auth["username"])

        with ws_client.websocket_connect(f"/ws/agent?token={agent_token}") as agent_ws:
            ready = _recv_until(agent_ws, "ready")
            assert isinstance(ready["threads"], list)

            # watch 后用 ping/pong 确认订阅已生效（同一连接内事件按序处理）
            agent_ws.send_json({"type": "watch", "thread_id": thread_id})
            agent_ws.send_json({"type": "ping"})
            _recv_until(agent_ws, "pong")

            user_ws.send_json({"type": "message", "content": "第二句"})
            ev = _recv_until(agent_ws, "message")
            assert ev["thread_id"] == thread_id
            assert ev["message"]["role"] == "user"
            assert ev["message"]["content"] == "第二句"

def test_ws_user_receives_human_reply(ws_client):
    """坐席回复走 REST 落库，用户端通过 WS 立刻收到（含坐席昵称）。"""
    auth = _register(ws_client)
    agent_token = _agent_token()
    headers = {"Authorization": f"Bearer {agent_token}"}

    # 直接造一个排队会话：本用例只验证推送，不需要走 LLM 触发转接
    thread_id = str(uuid.uuid4())
    handoff.set_thread_user(thread_id, _user_id(auth["username"]))
    handoff.mark_pending_human(thread_id, "测试转接")

    with ws_client.websocket_connect(f"/ws/chat?token={auth['token']}") as user_ws:
        # ready 快照就带着转接状态：用户端首屏直接是"人工会话中"
        ready = _recv_until(user_ws, "ready")
        assert ready["status"] == handoff.STATUS_QUEUED
        assert ready["thread_id"] == thread_id

        assert ws_client.post(
            f"/agent/threads/{thread_id}/accept", json={}, headers=headers
        ).status_code == 200
        # 接入会推状态变更：用户端不用刷新就能切到"服务中"
        status_ev = _recv_until(user_ws, "status")
        assert status_ev["status"] == handoff.STATUS_IN_SERVICE

        resp = ws_client.post(
            f"/agent/threads/{thread_id}/reply",
            json={"content": "人工回复来了"}, headers=headers,
        )
        assert resp.status_code == 200

        ev = _recv_until(user_ws, "message")
        assert ev["message"]["role"] == "human"
        assert ev["message"]["content"] == "人工回复来了"
        # 用户端气泡要显示"是谁回复的"
        assert ev["message"]["sender_name"] == "WS坐席"
