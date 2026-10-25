"""用户账户存储（app_user 表）与认证业务逻辑。

与 handoff.py 同一套连接约定：同步 psycopg、懒建连、ensure_table 幂等建表。
登录注册是低频操作，同步实现简单可靠优先。
"""

import logging
from collections.abc import Iterator

import psycopg

from app.core.db import get_conn
from app.core.security import ROLE_AGENT, ROLE_USER, hash_password, verify_password

logger = logging.getLogger(__name__)


def _get_conn() -> Iterator[psycopg.Connection]:
    """借一条池化连接（不再每次新建 TCP 连接）。

    实测一轮对话会触发 14 次同步 DB 往返，原先每次都 `psycopg.connect()`
    ——既慢（建连约 1-2ms）又会把 Postgres 连接数打满。
    现在统一走 `app/core/db.py` 的进程级连接池。
    """
    return get_conn()


def ensure_table() -> None:
    """兼容入口：执行数据库迁移（建表逻辑已迁到 app/core/migrations.py）。

    保留函数名是因为 scripts/create_agent.py 与测试仍在调用；
    **不要再往这里加 DDL**，改 schema 请到 migrations.py 追加 Migration。
    """
    from app.core import migrations

    migrations.run()

def get_user_by_id(user_id: int) -> dict | None:
    """按主键查询账号信息（Token 校验后用；账号已删除返回 None 按未认证处理）。"""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT id, username, role, display_name FROM app_user WHERE id = %s",
            (user_id,),
        ).fetchone()
    if row is None:
        return None
    return {"id": row[0], "username": row[1], "role": row[2], "display_name": row[3]}

class AuthError(Exception):
    """认证业务异常：message 为用户可读文案，由路由层翻译成 400/401。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def create_user(username: str, password: str, role: str, display_name: str | None = None) -> dict:
    """注册新账号。用户名唯一，冲突直接报业务异常。"""
    if role not in (ROLE_USER, ROLE_AGENT):
        raise AuthError("无效的角色")
    try:
        with _get_conn() as conn:
            row = conn.execute(
                "INSERT INTO app_user (username, password_hash, role, display_name) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (username, hash_password(password), role, display_name or username),
            ).fetchone()
    except psycopg.errors.UniqueViolation:
        raise AuthError("该用户名已被注册") from None
    return {"id": row[0], "username": username, "role": role, "display_name": display_name or username}


def authenticate(username: str, password: str) -> dict:
    """登录校验：用户不存在与密码错误返回同一文案，避免泄露账号是否存在。"""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT id, password_hash, role, display_name FROM app_user WHERE username = %s",
            (username,),
        ).fetchone()
    if row is None or not verify_password(password, row[1]):
        raise AuthError("用户名或密码错误")
    return {"id": row[0], "username": username, "role": row[2], "display_name": row[3]}


def get_user(username: str) -> dict | None:
    """按用户名查询账号信息（Token 校验后反查用）。"""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT id, username, role, display_name FROM app_user WHERE username = %s",
            (username,),
        ).fetchone()
    if row is None:
        return None
    return {"id": row[0], "username": row[1], "role": row[2], "display_name": row[3]}
