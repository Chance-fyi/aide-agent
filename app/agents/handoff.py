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
    """幂等建表。

    schema 变更时直接 DROP 旧表重建（开发期无历史负担，不做存量迁移）；
    上生产前再引入正式的迁移机制。
    """
    with _get_conn() as conn:
        # 开发期：schema 与代码不一致时整表重建，容忍丢数据
        # （检测方式：尝试访问 user_id 列，失败则重建）
        try:
            conn.execute("SELECT user_id FROM conversation_status LIMIT 1")
        except (psycopg.errors.UndefinedColumn, psycopg.errors.UndefinedTable):
            conn.execute("DROP TABLE IF EXISTS conversation_status")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS conversation_status (
                thread_id  TEXT PRIMARY KEY,          -- 会话 ID（与 Checkpointer thread_id 同源）
                status     TEXT NOT NULL DEFAULT 'active',
                reason     TEXT,                       -- 转接原因（阈值/关键词/用户诉求）
                agent_name TEXT,                       -- 接入坐席标识（坐席端后续迭代使用）
                resolution TEXT,                       -- 坐席处理结果
                user_id    TEXT,                       -- 会话归属用户（关联 app_user.username；匿名会话为空）
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_conversation_user ON conversation_status (user_id)")
        # 用户可见的会话记录表：用户/机器人/人工坐席的消息都落在这里。
        # 为什么不用 Checkpointer 当历史源：Checkpointer 是 Agent 执行快照，
        # 坐席回复、纯转达类消息不经过图执行，写不进去；
        # 且跨天恢复、坐席端展示都需要按时间序读纯消息流，独立表查询模型更简单
        try:
            conn.execute("SELECT user_id FROM chat_message LIMIT 1")
        except (psycopg.errors.UndefinedColumn, psycopg.errors.UndefinedTable):
            conn.execute("DROP TABLE IF EXISTS chat_message")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_message (
                id         BIGSERIAL PRIMARY KEY,
                thread_id  TEXT NOT NULL,
                role       TEXT NOT NULL,              -- user / agent(智能客服) / human(人工坐席)
                content    TEXT NOT NULL,
                user_id    TEXT,                       -- user 角色消息的发送者（agent/human 消息为空）
                sender_id  TEXT,                       -- human 消息的坐席（关联 app_user.username；昵称 JOIN 得到）
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_message_thread ON chat_message (thread_id, id)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_message_user ON chat_message (user_id)")


def append_message(
    thread_id: str, role: str, content: str,
    user_id: str | None = None, sender_id: str | None = None,
) -> None:
    """追加一条用户可见的会话记录。

    role 取值见 app.api.chat 的 ROLE_* 常量：user / agent(智能客服) / human(人工坐席)。
    user_id 仅 user 角色消息传入（记录发送者，支持"按用户查消息"）；
    sender_id 仅 human 角色消息传入（坐席账号，展示昵称时 JOIN app_user）。
    失败由调用方决定是否降级（记日志不抛出场景居多）。
    """
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO chat_message (thread_id, role, content, user_id, sender_id) "
            "VALUES (%s, %s, %s, %s, %s)",
            (thread_id, role, content, user_id, sender_id),
        )


def get_messages(thread_id: str) -> list[dict]:
    """按时间序读取会话记录；无记录返回空列表（调用方据此决定是否回退旧数据源）。"""
    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT m.role, m.content, m.created_at, u.display_name
            FROM chat_message m
            LEFT JOIN app_user u ON u.username = m.sender_id
            WHERE m.thread_id = %s
            ORDER BY m.id ASC
            """,
            (thread_id,),
        ).fetchall()
    return [
        {"role": r[0], "content": r[1], "ts": r[2].isoformat(), "sender_name": r[3]}
        for r in rows
    ]


def list_live_threads() -> list[dict]:
    """列出所有待处理/处理中的会话（坐席端队列数据源）。

    只查业务表，不碰 Checkpointer：队列刷新是高频操作，
    轻量查询优先，消息内容由前端按需再调 /chat/{id}/history 获取。
    """
    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT cs.thread_id, cs.status, cs.reason, cs.agent_name, cs.updated_at, u.display_name
            FROM conversation_status cs
            LEFT JOIN app_user u ON u.username = cs.user_id
            WHERE cs.status = ANY(%s)
            ORDER BY updated_at ASC           -- 先转接的排前面，先进先出
            """,
            (list(_LIVE_STATUSES),),
        ).fetchall()
    return [
        {
            "thread_id": r[0],
            "status": r[1],
            "reason": r[2],
            "agent_name": r[3],
            "updated_at": r[4].isoformat(),
            # 展示昵称（display_name），匿名/已删号用户为 None，前端回退"游客"
            "user_display_name": r[5],
        }
        for r in rows
    ]

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

def get_thread_user(thread_id: str) -> str | None:
    """查会话归属用户；无记录/异常返回 None（不阻断对话主链路）。"""
    try:
        with _get_conn() as conn:
            row = conn.execute(
                "SELECT user_id FROM conversation_status WHERE thread_id = %s",
                (thread_id,),
            ).fetchone()
            return row[0] if row else None
    except Exception:
        logger.exception("会话归属查询失败 thread=%s", thread_id)
        return None

def get_user_current_thread(username: str) -> str | None:
    """反查用户当前使用的聊天流 thread_id（取最近活跃的那条）。

    "一个用户一条聊天记录"语义：无论换设备/清缓存，登录后都能接上
    同一条流。取 updated_at 最新的一条（close 后再来消息会重置时间）。
    """
    try:
        with _get_conn() as conn:
            row = conn.execute(
                "SELECT thread_id FROM conversation_status "
                "WHERE user_id = %s ORDER BY updated_at DESC LIMIT 1",
                (username,),
            ).fetchone()
            return row[0] if row else None
    except Exception:
        logger.exception("用户当前会话查询失败 user=%s", username)
        return None

def set_thread_user(thread_id: str, username: str) -> None:
    """记录/修正会话归属用户（UPSERT）。

    为什么用 UPSERT 而非纯 UPDATE：登录用户发首条消息时，
    conversation_status 可能还没有该会话的状态行（状态行只在转接时才 INSERT），
    纯 UPDATE 会匹配 0 行静默无效（首条消息时状态行可能尚未存在）。
    无行则插入 active 状态行，与 get_status 的"无记录视为 active"语义一致；
    已有行则只补归属，不覆盖现有转接状态。
    """
    try:
        with _get_conn() as conn:
            conn.execute(
                """
                INSERT INTO conversation_status (thread_id, status, user_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (thread_id)
                DO UPDATE SET user_id = EXCLUDED.user_id, updated_at = now()
                """,
                (thread_id, STATUS_ACTIVE, username),
            )
    except Exception:
        logger.exception("会话归属更新失败 thread=%s user=%s", thread_id, username)

def get_user_messages(username: str, limit: int = 200) -> list[dict]:
    """按用户取全部聊天记录（跨 thread 合并、按时间序）。

    "一条聊天记录"的存储现实：用户历史上有多个 thread（每轮人工服务
    结束后 Agent 记忆清空开新段），但用户视角是一条流——
    查询层把所有归属该用户的 thread 的消息拉平合并。
    只取用户发过的消息所在的 thread（thread 归属即用户，双向一致）。
    """
    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT m.thread_id, m.role, m.content, m.created_at, u.display_name
            FROM chat_message m
            LEFT JOIN app_user u ON u.username = m.sender_id
            WHERE m.thread_id IN (
                SELECT thread_id FROM conversation_status WHERE user_id = %s
            )
            ORDER BY m.created_at DESC, m.id DESC
            LIMIT %s
            """,
            (username, limit),
        ).fetchall()
    # SQL 倒序取最新的 N 条，返回时转回正序（聊天流从上往下读）
    return [
        {
            "thread_id": r[0], "role": r[1], "content": r[2],
            "ts": r[3].isoformat(), "sender_name": r[4],
        }
        for r in reversed(rows)
    ]

def list_user_threads(username: str) -> list[dict]:
    """列出某用户的全部会话（用户端"历史会话"列表数据源）。

    会话骨架取 conversation_status（有状态/归属），LEFT JOIN 每会话最后一条
    消息做预览与排序。无消息记录的会话也展示（如刚转接还没聊的）。
    """
    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT cs.thread_id, cs.status, cs.updated_at,
                   lm.content, lm.created_at, lm.role
            FROM conversation_status cs
            LEFT JOIN LATERAL (
                SELECT content, created_at, role
                FROM chat_message m
                WHERE m.thread_id = cs.thread_id
                ORDER BY m.id DESC LIMIT 1
            ) lm ON TRUE
            WHERE cs.user_id = %s
            ORDER BY COALESCE(lm.created_at, cs.updated_at) DESC
            """,
            (username,),
        ).fetchall()
    return [
        {
            "thread_id": r[0],
            "status": r[1],
            "updated_at": r[2].isoformat(),
            "last_message": r[3],                      # 最后一条消息预览（可能 None）
            "last_message_at": r[4].isoformat() if r[4] else None,
            "last_role": r[5],
        }
        for r in rows
    ]

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
