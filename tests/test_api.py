"""API 集成测试。

用 ASGITransport 直连 FastAPI app（不起真实服务进程）。
只测不依赖真实 LLM 的路径：
- 转接链路（关键词兜底 / 状态守卫 / 历史查询）
- 参数校验 / 404 / 健康检查

agent 路径需要真实 LLM，不进 CI 冒烟（后面评估集迭代覆盖）。
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture
async def client():
    """异步测试客户端：生命周期随 fixture 起止。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_chat_validation_error(client):
    """空消息应 422（pydantic min_length=1）。"""
    resp = await client.post("/chat", json={"message": ""})
    assert resp.status_code == 422


async def test_chat_overlong_message_rejected(client):
    """超长消息应 422（max_length=2000，输入防护第一道闸）。"""
    resp = await client.post("/chat", json={"message": "a" * 2001})
    assert resp.status_code == 422


async def test_chat_keyword_transfer_full_flow(client):
    """关键词兜底全流程：转接 → 守卫 → 历史。"""
    # 1) 首条消息命中关键词，触发转接
    r1 = await client.post("/chat", json={"message": "太差劲了，给我转人工"})
    assert r1.status_code == 200
    data = r1.json()
    assert data["transferred"] is True
    assert data["thread_id"]
    tid = data["thread_id"]

    # 2) 同会话第二条消息：状态守卫生效，不进 agent
    r2 = await client.post(
        "/chat", json={"message": "退款多久到账", "thread_id": tid}
    )
    assert r2.status_code == 200
    assert r2.json()["transferred"] is True

    # 3) 历史接口：消息应为 user/assistant 交替，且无空 assistant
    h = await client.get(f"/chat/{tid}/history")
    assert h.status_code == 200
    body = h.json()
    assert body["status"] == "pending_human"
    roles = [m["role"] for m in body["messages"]]
    assert roles[0] == "user"
    for m in body["messages"]:
        assert m["content"].strip(), "历史里不应有空消息（tool_calls 已过滤）"


async def test_history_404(client):
    """不存在会话应 404。"""
    resp = await client.get(f"/chat/{uuid.uuid4()}/history")
    assert resp.status_code == 404
