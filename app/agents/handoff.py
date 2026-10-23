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
    """兼容入口：执行数据库迁移（建表逻辑已迁到 app/core/migrations.py）。

    保留这个函数名是因为测试与运维脚本仍在调用它；实现改为幂等迁移。
    **不要再往这里加 DDL** —— 改 schema 请到 migrations.py 追加一条 Migration。
    """
    from app.core import migrations

    migrations.run()

def append_message(
    thread_id: str, role: str, content: str,
    user_id: str | None = None, sender_id: int | None = None,
) -> dict | None:
    """追加一条用户可见的会话记录，返回 `{id, ts}`。

    role 取值见 app.api.chat 的 ROLE_* 常量：user / agent(智能客服) / human(人工坐席)。
    user_id 仅 user 角色消息传入（记录发送者，支持"按用户查消息"）；
    sender_id 仅 human 角色消息传入（坐席账号，展示昵称时 JOIN app_user）。
    失败由调用方决定是否降级（记日志不抛出场景居多）。

    为什么要返回 id：消息有**两条**到达前端的路径——REST 响应体与 WebSocket 推送。
    发送方既自己插一条、又作为订阅者收到同一条推送，不去重就会出现两条；
    而“按内容+时间匹配”不可靠（本地时钟 vs 服务端 created_at 必然不同）。
    主键 id 是唯一天然去重键，前端据此合并。

    返回 ts（created_at）的理由同理：前端用本地时间乐观插入会导致双端时间轴
    倒挂，以服务端时间为准。取不到时返回 None，前端回退本地时间。
    """
    with _get_conn() as conn:
        row = conn.execute(
            "INSERT INTO chat_message (thread_id, role, content, user_id, sender_id) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id, created_at",
            (thread_id, role, content, user_id, sender_id),
        ).fetchone()
    if row is None:
        return None
    return {"id": row[0], "ts": row[1].isoformat()}


def get_messages(thread_id: str) -> list[dict]:
    """按时间序读取会话记录；无记录返回空列表（调用方据此决定是否回退旧数据源）。"""
    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT m.id, m.role, m.content, m.created_at, u.display_name
            FROM chat_message m
            LEFT JOIN app_user u ON u.id = m.sender_id
            WHERE m.thread_id = %s
            ORDER BY m.id ASC
            """,
            (thread_id,),
        ).fetchall()
    return [
        {"id": r[0], "role": r[1], "content": r[2], "ts": r[3].isoformat(), "sender_name": r[4]}
        for r in rows
    ]


def list_live_threads(agent_id: int | None = None) -> list[dict]:
    """列出待处理/处理中的会话（坐席端队列数据源）。

    接入即锁定：in_service 会话只有接入者本人能看到，其他坐席
    队列里不该出现（防止误点进去回复被拒，界面语义也更清晰）；
    queued 排队会话所有坐席都可见（保持抢单语义）。
    agent_id 为 None 时不过滤（测试/管理场景用）。
    """
    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT cs.thread_id, cs.status, cs.reason, cs.agent_name, cs.updated_at, u.display_name
            FROM conversation_status cs
            LEFT JOIN app_user u ON u.id = cs.user_id
            WHERE cs.status = ANY(%s)
              -- 处理中的会话只归属接入者本人；排队的对所有坐席可见
              AND (cs.status = %s OR cs.agent_id = %s::bigint OR %s::bigint IS NULL)
            ORDER BY updated_at ASC           -- 先转接的排前面，先进先出
            """,
            (list(_LIVE_STATUSES), STATUS_QUEUED, agent_id, agent_id),
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

def _notify_status(thread_id: str, status: str) -> None:
    """状态变更后通知双端：用户端换 UI、坐席端刷队列。

    为什么放在数据层而不是路由层：状态机是"唯一事实源"，任何一次
    状态变更都必须让两端看到；靠每个调用方记得推送，早晚会漏一处
    （超时巡检这类后台任务尤其容易忘）。

    延迟导入：core.hub 需要 import handoff（按会话查归属用户），
    顶层导入会形成循环依赖。推送失败一律吞掉——实时性不能反向
    拖垮业务写库，客户端重连时靠 ready 快照自愈。
    """
    try:
        from app.core.hub import hub

        hub.push_thread(thread_id, {"type": "status", "thread_id": thread_id, "status": status})
        hub.push_agents({"type": "queue_changed"})
    except Exception:
        logger.debug("状态变更推送失败 thread=%s", thread_id, exc_info=True)


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
    _notify_status(thread_id, STATUS_QUEUED)


def agent_accept(thread_id: str, agent_name: str, agent_id: int) -> bool:
    """坐席接入：queued → in_service，同时锁定到该坐席账号。

    只允许从排队状态接入，防止双坐席重复接入或对 active 会话误操作；
    agent_id 落库是后续权限校验的依据（昵称可改、可重复，账号 ID 不会）。
    返回是否接入成功（False = 状态不是排队中，可能已被接走/已关闭/超时回退）。
    """
    with _get_conn() as conn:
        row = conn.execute(
            """
            UPDATE conversation_status
            SET status = %s, agent_name = %s, agent_id = %s, updated_at = now()
            WHERE thread_id = %s AND status = %s
            RETURNING thread_id
            """,
            (STATUS_IN_SERVICE, agent_name, agent_id, thread_id, STATUS_QUEUED),
        ).fetchone()
    if row:
        logger.info("坐席已接入: thread=%s agent=%s", thread_id, agent_name)
        _notify_status(thread_id, STATUS_IN_SERVICE)
        return True
    return False


def get_thread_agent_id(thread_id: str) -> int | None:
    """查会话当前接入坐席的账号 ID；未接入/无记录返回 None。

    供回复/关闭接口做"必须是接入者本人"校验。
    """
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT agent_id FROM conversation_status WHERE thread_id = %s",
            (thread_id,),
        ).fetchone()
    return row[0] if row else None

def agent_close(thread_id: str, resolution: str, agent_id: int | None = None) -> bool:
    """坐席结束处理：状态置 closed。

    close 是"处理完毕"而非"转回自助"：后续新消息视为新问题，会话回 active。
    排队中也可直接关闭（坐席看到排队工单后直接处理完）；
    但服务中的会话只有接入者本人能关（agent_id 传入时校验），
    防止其他坐席把别人的在办工单关掉。
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
              -- 服务中的会话：仅接入者本人可关闭；排队中：任何坐席可接单即关
              AND (status = %s OR %s::bigint IS NULL OR agent_id = %s::bigint OR agent_id IS NULL)
            RETURNING thread_id
            """,
            (
                STATUS_CLOSED,
                resolution,
                resolution,
                thread_id,
                STATUS_QUEUED,
                STATUS_IN_SERVICE,
                STATUS_QUEUED,
                agent_id,
                agent_id,
            ),
        ).fetchone()
    if row:
        logger.info("人工处理结束: thread=%s resolution=%s", thread_id, resolution)
        _notify_status(thread_id, STATUS_CLOSED)
        return True
    return False


def reset_active(thread_id: str) -> None:
    """复位为 active：closed 会话里来了新消息，视为新问题回自助服务。"""
    with _get_conn() as conn:
        conn.execute(
            "UPDATE conversation_status SET status = %s, updated_at = now() WHERE thread_id = %s",
            (STATUS_ACTIVE, thread_id),
        )
    _notify_status(thread_id, STATUS_ACTIVE)

def get_user_display_name(user_id: int) -> str | None:
    """查用户展示昵称；无此账号/异常返回 None（前端回退"游客"）。"""
    try:
        with _get_conn() as conn:
            row = conn.execute(
                "SELECT display_name FROM app_user WHERE id = %s", (user_id,)
            ).fetchone()
            return row[0] if row else None
    except Exception:
        logger.exception("用户昵称查询失败 user_id=%s", user_id)
        return None

def get_thread_user(thread_id: str) -> int | None:
    """查会话归属用户 ID；无记录/异常返回 None（不阻断对话主链路）。"""
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

def get_user_current_thread(user_id: int) -> str | None:
    """反查用户当前使用的聊天流 thread_id（取最近活跃的那条）。

    "一个用户一条聊天记录"语义：无论换设备/清缓存，登录后都能接上
    同一条流。取 updated_at 最新的一条（close 后再来消息会重置时间）。
    """
    try:
        with _get_conn() as conn:
            row = conn.execute(
                "SELECT thread_id FROM conversation_status "
                "WHERE user_id = %s ORDER BY updated_at DESC LIMIT 1",
                (user_id,),
            ).fetchone()
            return row[0] if row else None
    except Exception:
        logger.exception("用户当前会话查询失败 user_id=%s", user_id)
        return None

def set_thread_user(thread_id: str, user_id: int) -> None:
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
                (thread_id, STATUS_ACTIVE, user_id),
            )
    except Exception:
        logger.exception("会话归属更新失败 thread=%s user_id=%s", thread_id, user_id)

def get_user_messages(user_id: int, limit: int = 200) -> list[dict]:
    """按用户取全部聊天记录（跨 thread 合并、按时间序）。

    "一条聊天记录"的存储现实：用户历史上有多个 thread（每轮人工服务
    结束后 Agent 记忆清空开新段），但用户视角是一条流——
    查询层把所有归属该用户的 thread 的消息拉平合并。
    只取用户发过的消息所在的 thread（thread 归属即用户，双向一致）。
    """
    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT m.id, m.thread_id, m.role, m.content, m.created_at, u.display_name
            FROM chat_message m
            LEFT JOIN app_user u ON u.id = m.sender_id
            WHERE m.thread_id IN (
                SELECT thread_id FROM conversation_status WHERE user_id = %s
            )
            ORDER BY m.created_at DESC, m.id DESC
            LIMIT %s
            """,
            (user_id, limit),
        ).fetchall()
    # SQL 倒序取最新的 N 条，返回时转回正序（聊天流从上往下读）
    return [
        {
            "id": r[0], "thread_id": r[1], "role": r[2], "content": r[3],
            "ts": r[4].isoformat(), "sender_name": r[5],
        }
        for r in reversed(rows)
    ]

def list_user_threads(user_id: int) -> list[dict]:
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
            (user_id,),
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
        # 回退是后台任务触发的，没有请求上下文可以"顺手"通知前端，
        # 所以必须在这里显式推：用户端要从"排队中"切回"自助"，
        # 否则界面一直卡在"坐席接入中"等一个不会来的人
        for tid in thread_ids:
            _notify_status(tid, STATUS_ACTIVE)
    return thread_ids
