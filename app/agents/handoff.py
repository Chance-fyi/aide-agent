"""人工转接状态机（会话维度）。

状态流转：
    active ──触发转接──> pending_human ──坐席接入(步骤后续)──> in_service ──结束──> closed
                ▲
                │ 超时回退(后续迭代)回到 active

存储选型：独立 Postgres 表而非 Checkpointer 状态——
转接状态是"业务事实"（要被坐席端/管理后台查询），而 Checkpointer
的状态是"Agent 执行快照"（框架内部用途），两者生命周期和查询模式不同。

MVP 用同步 psycopg：转接是低频操作，简单可靠优先。
"""

import logging
from datetime import datetime

import psycopg

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# 会话转接状态枚举
STATUS_ACTIVE = "active"            # 正常自助服务中
STATUS_PENDING_HUMAN = "pending_human"  # 已转接，等待坐席接入


def _get_conn() -> psycopg.Connection:
    s = get_settings()
    return psycopg.connect(s.postgres_dsn, autocommit=True)


def ensure_table() -> None:
    """幂等建表。"""
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS conversation_status (
                thread_id  TEXT PRIMARY KEY,          -- 会话 ID（与 Checkpointer thread_id 同源）
                status     TEXT NOT NULL DEFAULT 'active',
                reason     TEXT,                       -- 转接原因（阈值/关键词/用户诉求）
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)


def get_status(thread_id: str) -> str:
    """查询会话当前状态；无记录视为 active。"""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT status FROM conversation_status WHERE thread_id = %s",
            (thread_id,),
        ).fetchone()
    return row[0] if row else STATUS_ACTIVE


def mark_pending_human(thread_id: str, reason: str) -> None:
    """标记会话为待人工接入（幂等：记录转接原因）。"""
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO conversation_status (thread_id, status, reason)
            VALUES (%s, %s, %s)
            ON CONFLICT (thread_id)
            DO UPDATE SET status = %s, reason = %s, updated_at = now()
            """,
            (thread_id, STATUS_PENDING_HUMAN, reason, STATUS_PENDING_HUMAN, reason),
        )
    logger.info("会话已转接: thread=%s reason=%s", thread_id, reason)
