"""转接状态机测试。

需要 Postgres 可用（podman compose up -d），但不需要 LLM/Milvus。
使用独立测试表空间隔离？MVP 从简：直接操作真实表，
但每个用例用随机 thread_id，互不污染。
"""

import uuid

from app.agents import handoff


def test_ensure_table_idempotent():
    """重复建表不报错。"""
    handoff.ensure_table()
    handoff.ensure_table()


def test_default_status_is_active():
    """无记录的会话默认 active。"""
    tid = str(uuid.uuid4())
    assert handoff.get_status(tid) == handoff.STATUS_ACTIVE


def test_mark_pending_human():
    """标记转接后状态变为 pending_human，且记录原因。"""
    tid = str(uuid.uuid4())
    handoff.mark_pending_human(tid, reason="测试转接")
    assert handoff.get_status(tid) == handoff.STATUS_PENDING_HUMAN


def test_mark_pending_human_idempotent_update():
    """重复标记更新原因而非报错（ON CONFLICT 路径）。"""
    tid = str(uuid.uuid4())
    handoff.mark_pending_human(tid, reason="第一次")
    handoff.mark_pending_human(tid, reason="第二次")
    assert handoff.get_status(tid) == handoff.STATUS_PENDING_HUMAN
    # 验证原因被更新
    import psycopg

    from app.core.config import get_settings

    with psycopg.connect(get_settings().postgres_dsn) as conn:
        row = conn.execute(
            "SELECT reason FROM conversation_status WHERE thread_id = %s", (tid,)
        ).fetchone()
    assert row[0] == "第二次"
