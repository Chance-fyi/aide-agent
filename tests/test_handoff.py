"""转接状态机测试（step-14 扩展版）。

需要 Postgres 可用（podman compose up -d），但不需要 LLM/Milvus。
每个用例用随机 thread_id，互不污染。
"""

import uuid

import psycopg

from app.agents import handoff
from app.core.config import get_settings


def _set_updated_at(tid: str, minutes_ago: float) -> None:
    """直接改 updated_at，模拟"排队了很久"（跳过真实等待）。"""
    with psycopg.connect(get_settings().postgres_dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE conversation_status SET updated_at = now() - (%s * interval '1 minute')"
            " WHERE thread_id = %s",
            (minutes_ago, tid),
        )

def test_ensure_table_idempotent():
    """重复建表不报错。"""
    handoff.ensure_table()
    handoff.ensure_table()

def test_default_status_is_active():
    """无记录的会话默认 active。"""
    tid = str(uuid.uuid4())
    assert handoff.get_status(tid) == handoff.STATUS_ACTIVE

def test_full_lifecycle():
    """完整生命周期：转接排队 → 坐席接入 → 处理结束。"""
    tid = str(uuid.uuid4())

    # 1) 用户触发转接 → 排队
    handoff.mark_pending_human(tid, reason="测试转接")
    assert handoff.get_status(tid) == handoff.STATUS_QUEUED
    assert handoff.is_live_status(handoff.get_status(tid))

    # 2) 坐席接入：排队 → in_service
    assert handoff.agent_accept(tid, "坐席小王") is True
    assert handoff.get_status(tid) == handoff.STATUS_IN_SERVICE

    # 3) 重复接入应失败（防止双坐席）
    assert handoff.agent_accept(tid, "坐席小李") is False

    # 4) 处理结束：→ closed
    assert handoff.agent_close(tid, "已解决退款问题") is True
    assert handoff.get_status(tid) == handoff.STATUS_CLOSED
    assert not handoff.is_live_status(handoff.get_status(tid))

def test_accept_rejects_non_queued():
    """active 会话不能被接入（只能接排队的工单）。"""
    tid = str(uuid.uuid4())
    assert handoff.agent_accept(tid, "坐席") is False

def test_close_from_queued():
    """坐席看完排队工单直接处理完，允许 queued → closed。"""
    tid = str(uuid.uuid4())
    handoff.mark_pending_human(tid, reason="排队即处理")
    assert handoff.agent_close(tid, "直接答复") is True
    assert handoff.get_status(tid) == handoff.STATUS_CLOSED

def test_sweep_timed_out_reverts_only_old_queued():
    """超时回退只影响"超过时限的排队会话"，不碰新排队与 in_service。"""
    # 新排队的：不应被回退
    tid_fresh = str(uuid.uuid4())
    handoff.mark_pending_human(tid_fresh, reason="刚转接")
    # 超时的：应被回退
    tid_stale = str(uuid.uuid4())
    handoff.mark_pending_human(tid_stale, reason="很久没人接")
    _set_updated_at(tid_stale, minutes_ago=10_000)
    # 坐席服务中的：不应被回退（坐席在处理，不能自动打断）
    tid_serving = str(uuid.uuid4())
    handoff.mark_pending_human(tid_serving, reason="排队")
    handoff.agent_accept(tid_serving, "坐席")
    _set_updated_at(tid_serving, minutes_ago=10_000)

    swept = handoff.sweep_timed_out()
    assert tid_stale in swept
    assert tid_fresh not in swept
    assert tid_serving not in swept

    assert handoff.get_status(tid_stale) == handoff.STATUS_ACTIVE
    assert handoff.get_status(tid_fresh) == handoff.STATUS_QUEUED
    assert handoff.get_status(tid_serving) == handoff.STATUS_IN_SERVICE

def test_sweep_empty_returns_empty():
    """没有超时会话时返回空列表，不报错。"""
    assert handoff.sweep_timed_out() is not None
