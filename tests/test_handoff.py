"""转接状态机测试（step-14 扩展版）。

需要 Postgres 可用（podman compose up -d），但不需要 LLM/Milvus。
每个用例用随机 thread_id，互不污染。
"""

import time
import uuid

import psycopg

from app.agents import handoff
from app.core.config import get_settings


def _set_updated_at(tid: str, minutes_ago: float) -> None:
    """把时间戳回拨，模拟"排队了很久"（跳过真实等待）。

    同时改 queued_at 与 updated_at：排队超时现在以 **queued_at**（进队列时刻）
    为准，updated_at 只是"最后变化时间"。两个都拨回才符合"很久之前就排队了"。
    """
    with psycopg.connect(get_settings().postgres_dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE conversation_status"
            " SET updated_at = now() - (%s * interval '1 minute'),"
            "     queued_at  = now() - (%s * interval '1 minute')"
            " WHERE thread_id = %s",
            (minutes_ago, minutes_ago, tid),
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

    # 2) 坐席接入：排队 → in_service（传入账号 ID，锁定到该坐席）
    assert handoff.agent_accept(tid, "坐席小王", agent_id=1001) is True
    assert handoff.get_status(tid) == handoff.STATUS_IN_SERVICE
    # 接入者 ID 已落库，供回复/关闭接口校验
    assert handoff.get_thread_agent_id(tid) == 1001

    # 3) 重复接入应失败（防止双坐席）
    assert handoff.agent_accept(tid, "坐席小李", agent_id=1002) is False

    # 4) 处理结束：→ closed
    assert handoff.agent_close(tid, "已解决退款问题") is True
    assert handoff.get_status(tid) == handoff.STATUS_CLOSED
    assert not handoff.is_live_status(handoff.get_status(tid))

def test_accept_rejects_non_queued():
    """active 会话不能被接入（只能接排队的工单）。"""
    tid = str(uuid.uuid4())
    assert handoff.agent_accept(tid, "坐席", agent_id=1) is False

def test_in_service_thread_visible_only_to_owner():
    """接入即锁定：in_service 会话只出现在接入者本人的队列里，
    其他坐席看不到，排队中的对所有坐席可见。"""
    tid = str(uuid.uuid4())
    handoff.mark_pending_human(tid, reason="队列可见性测试")
    # 排队中：任何坐席都能看到
    assert tid in [t["thread_id"] for t in handoff.list_live_threads(agent_id=1)]
    assert tid in [t["thread_id"] for t in handoff.list_live_threads(agent_id=2)]
    # 坐席 1 接入后：只有坐席 1 能看到
    assert handoff.agent_accept(tid, "坐席甲", agent_id=1) is True
    assert tid in [t["thread_id"] for t in handoff.list_live_threads(agent_id=1)]
    assert tid not in [t["thread_id"] for t in handoff.list_live_threads(agent_id=2)]

def test_close_in_service_rejected_for_other_agent():
    """服务中的会话只有接入者本人能关闭，其他坐席关闭应失败。"""
    tid = str(uuid.uuid4())
    handoff.mark_pending_human(tid, reason="关闭权限测试")
    assert handoff.agent_accept(tid, "坐席甲", agent_id=1) is True
    # 其他坐席不能关别人的在办工单
    assert handoff.agent_close(tid, "越权关闭", agent_id=2) is False
    assert handoff.get_status(tid) == handoff.STATUS_IN_SERVICE
    # 本人可以关
    assert handoff.agent_close(tid, "本人关闭", agent_id=1) is True
    assert handoff.get_status(tid) == handoff.STATUS_CLOSED

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
    handoff.agent_accept(tid_serving, "坐席", agent_id=999)
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

def test_queue_fifo_not_broken_by_user_followup_messages():
    """排队 FIFO 不能被"用户补发消息"打乱。

    回归一个真实 bug：`set_thread_user`（每条消息都会调用）顺手刷新了
    `updated_at`，而队列原先按 `updated_at ASC` 排序 —— 于是排队中的用户
    只要补发一句话就被挪到队尾，可能被无限饿死。

    修法：排队顺序改按 `queued_at`（进队列时刻），且归属写入不再碰时间戳。
    """
    tid_first = str(uuid.uuid4())
    tid_second = str(uuid.uuid4())
    handoff.mark_pending_human(tid_first, reason="先转接")
    time.sleep(0.05)
    handoff.mark_pending_human(tid_second, reason="后转接")

    def order():
        ids = [t["thread_id"] for t in handoff.list_live_threads()]
        return [x for x in ids if x in (tid_first, tid_second)]

    assert order() == [tid_first, tid_second], "初始应为先进先出"

    # 模拟先排队那位补发消息（process_message 会调 set_thread_user 补归属）
    handoff.set_thread_user(tid_first, 12345)
    time.sleep(0.05)

    assert order() == [tid_first, tid_second], (
        "补发消息不应改变排队顺序（否则该用户会被挤到队尾）"
    )

def test_queued_at_not_reset_by_repeated_transfer():
    """重复触发转接不重置排队时间。

    否则用户反复触发转接就能让排队超时永不生效（一直"刚进队列"）。
    """
    tid = str(uuid.uuid4())
    handoff.mark_pending_human(tid, reason="第一次")
    first = _get_queued_at(tid)

    time.sleep(0.05)
    handoff.mark_pending_human(tid, reason="又触发一次")
    assert _get_queued_at(tid) == first, "已在排队中时不应刷新 queued_at"

    # 离开队列后再进来，应拿到新的排队时间
    handoff.agent_accept(tid, "坐席", agent_id=1)
    assert _get_queued_at(tid) is None, "接入后应清空排队时间"
    handoff.mark_pending_human(tid, reason="重新转接")
    assert _get_queued_at(tid) > first, "重新进队列应刷新排队时间"

def _get_queued_at(tid: str):
    with psycopg.connect(get_settings().postgres_dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT queued_at FROM conversation_status WHERE thread_id = %s", (tid,)
        ).fetchone()
    return row[0] if row else None
