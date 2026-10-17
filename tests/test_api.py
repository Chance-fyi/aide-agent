"""API 集成测试。

用 ASGITransport 直连 FastAPI app（不起真实服务进程）。
只测不依赖真实 LLM 的路径：
- 转接链路（关键词兜底 / 状态守卫 / 历史查询）
- 参数校验 / 404 / 健康检查

agent 路径需要真实 LLM，不进 CI 冒烟（后面评估集迭代覆盖）。
"""

import uuid

import httpx
import openai
import pytest
from httpx import ASGITransport, AsyncClient

from app.api import chat as chat_mod
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

# ---------- step-12：LLM 异常分级与友好错误 ----------

def _timeout_error() -> openai.APITimeoutError:
    """构造真实的 openai 超时异常（校验异常映射的继承顺序是否正确）。"""
    return openai.APITimeoutError(request=httpx.Request("POST", "http://test"))

async def test_chat_llm_timeout_friendly_error(client, monkeypatch):
    """LLM 超时应转为 503 + 可操作文案，而不是裸 500 或泄露内部细节。"""
    async def boom():
        raise _timeout_error()

    monkeypatch.setattr(chat_mod, "get_graph", boom)
    resp = await client.post("/chat", json={"message": "你好"})
    assert resp.status_code == 503
    assert "超时" in resp.json()["detail"]

async def test_chat_llm_rate_limit_friendly_error(client, monkeypatch):
    """限流错误应给出"人数较多"文案（与普通故障区分，引导稍后再试）。"""
    request = httpx.Request("POST", "http://test")

    async def boom():
        raise openai.RateLimitError(
            "rate limited", response=httpx.Response(429, request=request), body=None
        )

    monkeypatch.setattr(chat_mod, "get_graph", boom)
    resp = await client.post("/chat", json={"message": "你好"})
    assert resp.status_code == 503
    assert "人数较多" in resp.json()["detail"]

async def test_chat_status_query_failure_degrades(client, monkeypatch):
    """转接状态查询失败时不应拖垮已生成的回复：降级为未转接 + 200。"""
    def boom(thread_id):
        raise RuntimeError("postgres down")

    # 走关键词转接链路（不依赖真实 LLM），再让状态查询炸掉
    monkeypatch.setattr(chat_mod.handoff, "get_status", boom)
    resp = await client.post("/chat", json={"message": "转人工"})
    assert resp.status_code == 200
    assert resp.json()["transferred"] is False

async def test_history_invalid_thread_id_rejected(client):
    """非法字符/超长 thread_id 应 422（纵深防御，不进 Checkpointer）。"""
    resp = await client.get("/chat/not%20allowed%20id/history")
    assert resp.status_code == 422
    resp = await client.get(f"/chat/{'a' * 65}/history")
    assert resp.status_code == 422
