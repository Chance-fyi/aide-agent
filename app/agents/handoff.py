"""人工转接状态机（会话维度）。

状态流转：

    active ──触发转接──> queued ──坐席接入──> in_service ──结束/解决──> closed
                ▲                        │
                └── 排队超时自动回退 ─────┘

- queued：已转接，排队等待坐席；超过 transfer_queue_timeout 后自动回退 active
- in_service：坐席已接入；由坐席端主动结束，不自动回退
- closed：坐席处理完成；用户再来新消息视为新问题，回自助服务

存储选型：独立 Postgres 表而非 Checkpointer 状态——
转接状态是"业务事实"（要被坐席端/管理后台查询），而 Checkpointer
的状态是"Agent 执行快照"（框架内部用途），两者生命周期和查询模式不同。

MVP 用同步 psycopg：转接是低频操作，简单可靠优先。
"""

import logging

import psycopg

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# 会话转接状态枚举
STATUS_ACTIVE = "active"                # 正常自助服务中
STATUS_QUEUED = "queued"                # 已转接，排队等待坐席接入
STATUS_IN_SERVICE = "in_service"        # 坐席已接入，处理中
STATUS_CLOSED = "closed"                # 坐席处理完成

# "会话受转接影响"的状态集合：route_input 守卫与 transferred 标记按此判断
_LIVE_STATUSES = {STATUS_QUEUED, STATUS_IN_SERVICE}


def _get_conn() -> psycopg.Connection:
    s = get_settings()
    return psycopg.connect(s.postgres_dsn, autocommit=True)


def ensure_table() -> None:
    """幂等建表 + 幂等加列（step-14 新增 agent_name/resolution）。

    CREATE TABLE IF NOT EXISTS 不会给已存在的表加新列，
    存量库必须靠 ALTER TABLE 迁移；ADD COLUMN IF NOT EXISTS 可重复执行。
    """
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS conversation_status (
                thread_id  TEXT PRIMARY KEY,          -- 会话 ID（与 Checkpointer thread_id 同源）
                status     TEXT NOT NULL DEFAULT 'active',
                reason     TEXT,                       -- 转接原因（阈值/关键词/用户诉求）
                agent_name TEXT,                       -- 接入坐席标识（坐席端后续迭代使用）
                resolution TEXT,                       -- 坐席处理结果
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        conn.execute("ALTER TABLE conversation_status ADD COLUMN IF NOT EXISTS agent_name TEXT")
        conn.execute("ALTER TABLE conversation_status ADD COLUMN IF NOT EXISTS resolution TEXT")


def get_status(thread_id: str) -> str:
    """查询会话当前状态；无记录视为 active。"""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT status FROM conversation_status WHERE thread_id = %s",
            (thread_id,),
        ).fetchone()
    return row[0] if row else STATUS_ACTIVE


def is_live_status(status: str) -> bool:
    """是否处于"转接待处理或处理中"状态（守卫/transferred 标记的统一口径）。"""
    return status in _LIVE_STATUSES


def mark_pending_human(thread_id: str, reason: str) -> None:
    """标记会话进入人工排队（幂等：重复转接只更新原因）。"""
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO conversation_status (thread_id, status, reason)
            VALUES (%s, %s, %s)
            ON CONFLICT (thread_id)
            DO UPDATE SET status = %s, reason = %s, updated_at = now()
            """,
            (thread_id, STATUS_QUEUED, reason, STATUS_QUEUED, reason),
        )
    logger.info("会话已转接(排队): thread=%s reason=%s", thread_id, reason)


def agent_accept(thread_id: str, agent_name: str) -> bool:
    """坐席接入：queued → in_service。

    只允许从排队状态接入，防止双坐席重复接入或对 active 会话误操作。
    返回是否接入成功（False = 状态不是排队中，可能已被接走/已关闭/超时回退）。
    """
    with _get_conn() as conn:
        row = conn.execute(
            """
            UPDATE conversation_status
            SET status = %s, agent_name = %s, updated_at = now()
            WHERE thread_id = %s AND status = %s
            RETURNING thread_id
            """,
            (STATUS_IN_SERVICE, agent_name, thread_id, STATUS_QUEUED),
        ).fetchone()
    if row:
        logger.info("坐席已接入: thread=%s agent=%s", thread_id, agent_name)
        return True
    return False


def agent_close(thread_id: str, resolution: str) -> bool:
    """坐席结束处理：状态置 closed。

    close 是"处理完毕"而非"转回自助"：后续新消息视为新问题，会话回 active。
    排队中也可直接关闭（坐席看到排队工单后直接处理完）。
    """
    with _get_conn() as conn:
        row = conn.execute(
            """
            UPDATE conversation_status
            SET status = %s,
                resolution = %s,
                reason = COALESCE(reason, '') || ' | 结束: ' || %s,
                updated_at = now()
            WHERE thread_id = %s AND status IN (%s, %s)
            RETURNING thread_id
            """,
            (
                STATUS_CLOSED,
                resolution,
                resolution,
                thread_id,
                STATUS_QUEUED,
                STATUS_IN_SERVICE,
            ),
        ).fetchone()
    if row:
        logger.info("人工处理结束: thread=%s resolution=%s", thread_id, resolution)
        return True
    return False


def reset_active(thread_id: str) -> None:
    """复位为 active：closed 会话里来了新消息，视为新问题回自助服务。"""
    with _get_conn() as conn:
        conn.execute(
            "UPDATE conversation_status SET status = %s, updated_at = now() WHERE thread_id = %s",
            (STATUS_ACTIVE, thread_id),
        )


def sweep_timed_out() -> list[str]:
    """排队超时回退：太久没人接的排队会话回到 active（用户可继续自助问答）。

    只回退 queued：in_service 表示坐席正在处理，更不应自动打断；
    回退而非关闭，用户再次求助还能重新转接。
    """
    timeout_min = get_settings().transfer_queue_timeout_min
    with _get_conn() as conn:
        rows = conn.execute(
            """
            UPDATE conversation_status
            SET status = %s,
                reason = COALESCE(reason, '') || ' [排队超时自动回退]',
                updated_at = now()
            WHERE status = %s
              AND updated_at < now() - (%s * interval '1 minute')
            RETURNING thread_id
            """,
            (STATUS_ACTIVE, STATUS_QUEUED, timeout_min),
        ).fetchall()
    thread_ids = [r[0] for r in rows]
    if thread_ids:
        logger.info("排队超时回退 %d 个会话: %s", len(thread_ids), thread_ids[:5])
    return thread_ids
