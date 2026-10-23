"""知识库管理接口测试。

重点验证三件容易出错的事：
1. **鉴权**：未登录 / 普通用户不能碰知识库（它能重建线上索引）
2. **路径穿越**：path 参数只接受纯文件名，不能读到语料目录之外
3. **重建互斥**：并发触发只能成功一次，且状态能查到

重建本身耗时 20+ 秒，测试里一律打桩（`store.rebuild_index`），
只验证任务编排与状态机，不真的重建。
"""

import uuid

import psycopg
import pytest

from app.core.config import get_settings
from app.core.security import ROLE_AGENT, create_token
from app.core.users import create_user
from app.rag import rebuild as rebuild_mod


@pytest.fixture
def agent_headers():
    """坐席 Token：知识库管理要求坐席登录。"""
    user = create_user(f"kbt_{uuid.uuid4().hex[:8]}", "secret123", ROLE_AGENT, "知识库测试")
    yield {"Authorization": f"Bearer {create_token(user['id'], ROLE_AGENT)}"}
    # 清理账号，避免测试数据堆积
    with psycopg.connect(get_settings().postgres_dsn, autocommit=True) as conn:
        conn.execute("DELETE FROM app_user WHERE id = %s", (user["id"],))


@pytest.fixture
def user_headers():
    """普通用户 Token：应被拒（知识库是运维能力，不是用户能力）。"""
    from app.core.security import ROLE_USER

    user = create_user(f"kbu_{uuid.uuid4().hex[:8]}", "secret123", ROLE_USER, "普通用户")
    yield {"Authorization": f"Bearer {create_token(user['id'], ROLE_USER)}"}
    with psycopg.connect(get_settings().postgres_dsn, autocommit=True) as conn:
        conn.execute("DELETE FROM app_user WHERE id = %s", (user["id"],))


@pytest.fixture(autouse=True)
def _clean_rebuild_rows():
    """每个用例前后清掉**本测试产生的**重建记录。

    只删 triggered_by 以「知识库测试」开头的行：`running` 的部分唯一索引
    要求同一时刻只能有一条，残留会污染后续用例；但全表 DELETE 会把开发库里
    真实的重建历史一起清掉，那属于破坏开发数据。
    """
    def _clean():
        with psycopg.connect(get_settings().postgres_dsn, autocommit=True) as conn:
            conn.execute("DELETE FROM kb_rebuild_run WHERE triggered_by = %s", ("知识库测试",))

    _clean()
    yield
    _clean()


# ============================================================
# 鉴权
# ============================================================

async def test_requires_login(client):
    """未登录一律 401（包括只读的 overview）。"""
    assert (await client.get("/kb/overview")).status_code == 401
    assert (await client.get("/kb/documents")).status_code == 401
    assert (await client.post("/kb/rebuild")).status_code == 401


async def test_rejects_normal_user(client, user_headers):
    """普通用户不能访问知识库管理：重建会影响所有用户的检索结果。"""
    assert (await client.get("/kb/overview", headers=user_headers)).status_code == 401


async def test_agent_can_read_overview(client, agent_headers):
    """坐席能读到概览，且结构完整。"""
    resp = await client.get("/kb/overview", headers=agent_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) >= {
        "stats", "file_count", "pending_rebuild", "collection", "rebuild", "config"
    }
    assert body["stats"]["documents"] >= 0


# ============================================================
# 路径穿越
# ============================================================

@pytest.mark.parametrize("bad", ["../AGENTS.md", "..\\AGENTS.md", ".env", "data/knowledge_base/x.md"])
async def test_chunks_rejects_path_traversal(client, agent_headers, bad):
    """切块预览的 path 只能是语料目录内的纯文件名。

    否则 `/kb/documents/chunks?path=../../.env` 就能读到任意文件
    （包括 LLM_API_KEY）。这是本接口最需要防的一类问题。
    """
    resp = await client.get("/kb/documents/chunks", params={"path": bad}, headers=agent_headers)
    assert resp.status_code == 422


async def test_strategy_rejects_path_traversal(client, agent_headers):
    """写接口同样只接受纯文件名。"""
    resp = await client.post(
        "/kb/documents/strategy",
        json={"path": "../AGENTS.md", "strategy": "header"},
        headers=agent_headers,
    )
    assert resp.status_code == 422


async def test_chunks_404_for_missing_file(client, agent_headers):
    """目录内不存在的文件返回 404（而不是空列表，便于前端区分）。"""
    resp = await client.get(
        "/kb/documents/chunks", params={"path": "no-such-file.md"}, headers=agent_headers
    )
    assert resp.status_code == 404


async def test_chunks_returns_content_for_real_file(client, agent_headers):
    """真实文件能取到切块（语料目录里一定有 .md）。"""
    docs = (await client.get("/kb/documents", headers=agent_headers)).json()
    if not docs["documents"]:
        pytest.skip("语料目录为空")
    path = docs["documents"][0]["path"]
    resp = await client.get(
        "/kb/documents/chunks", params={"path": path, "limit": 2}, headers=agent_headers
    )
    assert resp.status_code == 200
    assert resp.json()["path"] == path


# ============================================================
# 重建任务
# ============================================================

async def test_rebuild_is_async_and_reports_status(client, agent_headers, monkeypatch):
    """POST /kb/rebuild 立刻返回 202 + 任务；随后能从 GET 查到结果。

    重建耗时 20+ 秒，必须异步——同步接口会撞代理超时且前端无法显示进度。
    这里打桩成瞬时完成，验证的是编排而不是重建本身。
    """
    from app.rag import store

    monkeypatch.setattr(store, "rebuild_index", lambda kb_dir: 123)

    resp = await client.post("/kb/rebuild", headers=agent_headers)
    assert resp.status_code == 202
    job = resp.json()["job"]
    assert job["status"] in ("running", "succeeded")
    assert job["triggered_by"] == "知识库测试"

    # 后台任务在工作线程里跑，轮询等它结束（打桩后应该是瞬间）
    import asyncio
    for _ in range(50):
        status = (await client.get("/kb/rebuild", headers=agent_headers)).json()
        if not status["running"]:
            break
        await asyncio.sleep(0.05)

    assert status["job"]["status"] == "succeeded"
    assert status["job"]["chunks"] == 123
    assert status["job"]["finished_at"]


async def test_concurrent_rebuild_returns_409(client, agent_headers, monkeypatch):
    """并发触发重建：第二个必须 409，不能真的跑两次。

    互斥由部分唯一索引保证（`WHERE status='running'`），
    而不是应用层"先查再插"——后者在并发下有竞争窗口。
    """
    import threading
    import time

    from app.rag import store

    # 让重建卡住一会儿，制造"正在运行"的窗口
    def slow_rebuild(kb_dir):
        time.sleep(0.6)
        return 1

    monkeypatch.setattr(store, "rebuild_index", slow_rebuild)

    first = await client.post("/kb/rebuild", headers=agent_headers)
    assert first.status_code == 202
    await asyncio_sleep(0.1)   # 等后台线程真正开始

    second = await client.post("/kb/rebuild", headers=agent_headers)
    assert second.status_code == 409
    assert "进行中" in second.json()["detail"]

    # 等第一次结束，避免影响后续用例（清理 fixture 也会删记录）
    for _ in range(60):
        status = (await client.get("/kb/rebuild", headers=agent_headers)).json()
        if not status["running"]:
            break
        await asyncio_sleep(0.05)
    _ = threading  # 保持 import 显式，说明并发是用线程模拟的真实阻塞


async def asyncio_sleep(sec: float) -> None:
    import asyncio

    await asyncio.sleep(sec)


async def test_failed_rebuild_reports_friendly_error(client, agent_headers, monkeypatch):
    """重建失败：状态记为 failed 且给出用户可读原因（堆栈只进日志）。"""
    from app.rag import store

    def boom(kb_dir):
        raise RuntimeError("知识库语料为空，拒绝重建（请检查语料目录）")

    monkeypatch.setattr(store, "rebuild_index", boom)

    await client.post("/kb/rebuild", headers=agent_headers)
    for _ in range(50):
        status = (await client.get("/kb/rebuild", headers=agent_headers)).json()
        if not status["running"]:
            break
        await asyncio_sleep(0.05)

    job = status["job"]
    assert job["status"] == "failed"
    assert "语料" in job["error"], f"应给出可操作原因，实际: {job['error']}"


def test_stale_running_is_cleared():
    """僵尸 running（进程被杀留下的）必须被清理，否则重建永久被拒。

    直接把 started_at 改到 20 分钟前模拟进程中断。
    """
    with psycopg.connect(get_settings().postgres_dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO kb_rebuild_run (status, triggered_by, started_at) "
            "VALUES ('running', 'ghost', now() - interval '20 minutes')"
        )

    job = rebuild_mod.RebuildJob()
    assert job.is_running() is False, "僵尸任务不应阻塞新重建"

    job._clear_stale()
    latest = job.latest()
    assert latest["status"] == "failed"
    assert "中断" in latest["error"]


def test_friendly_reason_mapping():
    """异常 → 用户可读文案的映射（对外不泄露堆栈）。"""
    assert "语料" in rebuild_mod._friendly_reason(RuntimeError("知识库语料为空，拒绝重建"))
    assert "向量库" in rebuild_mod._friendly_reason(RuntimeError("Milvus 连接失败"))
    assert "冒烟" in rebuild_mod._friendly_reason(RuntimeError("冒烟测试失败：查询 '退货' 无命中"))
    # 兜底只给类型名，不泄露 message
    out = rebuild_mod._friendly_reason(ValueError("secret internal detail"))
    assert "ValueError" in out and "secret" not in out


# ============================================================
# 检索验证
# ============================================================

async def test_search_requires_login(client):
    assert (await client.post("/kb/search", params={"q": "退款"})).status_code == 401


async def test_search_reports_not_indexed_as_409(client, agent_headers, monkeypatch):
    """知识库还没建：返回 409 + 明确提示，而不是 500。

    调用方要能区分"没建库"和"服务挂了"。
    """
    from app.rag import store

    def not_indexed(*args, **kwargs):
        raise RuntimeError("知识库集合不存在，请先执行入库")

    monkeypatch.setattr(store, "search", not_indexed)
    resp = await client.post("/kb/search", params={"q": "退款"}, headers=agent_headers)
    assert resp.status_code == 409
    assert "知识库" in resp.json()["detail"]


async def test_status_response_is_self_consistent(client, agent_headers, monkeypatch):
    """`job.status` 与 `running` 必须来自同一次快照，不能自相矛盾。

    这条测试锁住一个真实 bug：路由原先分两次查库（latest + is_running），
    后台任务恰好在两次查询之间完成时，响应会变成
    `job.status='running'` 但 `running=false`。
    前端据此停止轮询，却仍显示"重建中"——最难解释的那类 bug。
    """
    import asyncio

    from app.rag import store

    # 让重建有一个短暂窗口，制造"查询时可能刚好完成"的时机
    def slow(kb_dir):
        import time

        time.sleep(0.05)
        return 7

    monkeypatch.setattr(store, "rebuild_index", slow)
    await client.post("/kb/rebuild", headers=agent_headers)

    seen = []
    for _ in range(60):
        body = (await client.get("/kb/rebuild", headers=agent_headers)).json()
        seen.append((body["job"]["status"], body["running"]))
        # 核心断言：两者必须一致
        assert body["running"] == (body["job"]["status"] == "running"), (
            f"响应自相矛盾: status={body['job']['status']} running={body['running']}"
        )
        if not body["running"]:
            break
        await asyncio.sleep(0.02)

    assert any(running for _, running in seen), "应至少观察到一次 running 状态"
    assert seen[-1][0] == "succeeded"
