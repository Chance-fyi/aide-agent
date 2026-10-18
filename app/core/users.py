"""用户账户存储（app_user 表）与认证业务逻辑。

与 handoff.py 同一套连接约定：同步 psycopg、懒建连、ensure_table 幂等建表。
登录注册是低频操作，同步实现简单可靠优先。
"""

import logging

import psycopg

from app.core.config import get_settings
from app.core.security import ROLE_AGENT, ROLE_USER, hash_password, verify_password

logger = logging.getLogger(__name__)


def _get_conn() -> psycopg.Connection:
    s = get_settings()
    return psycopg.connect(s.postgres_dsn, autocommit=True)


def ensure_table() -> None:
    """幂等建表：用户账户独立于会话数据，账号生命周期与会话无关。"""
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS app_user (
                username      TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,          -- PBKDF2 哈希，绝不存明文
                role          TEXT NOT NULL,          -- user(用户端) / agent(坐席端)
                display_name  TEXT NOT NULL,          -- 展示昵称（坐席接入时显示给用户）
                created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)


class AuthError(Exception):
    """认证业务异常：message 为用户可读文案，由路由层翻译成 400/401。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def create_user(username: str, password: str, role: str, display_name: str | None = None) -> dict:
    """注册新账号。用户名即主键，冲突直接报业务异常。"""
    if role not in (ROLE_USER, ROLE_AGENT):
        raise AuthError("无效的角色")
    try:
        with _get_conn() as conn:
            conn.execute(
                "INSERT INTO app_user (username, password_hash, role, display_name) "
                "VALUES (%s, %s, %s, %s)",
                (username, hash_password(password), role, display_name or username),
            )
    except psycopg.errors.UniqueViolation:
        raise AuthError("该用户名已被注册") from None
    return {"username": username, "role": role, "display_name": display_name or username}


def authenticate(username: str, password: str) -> dict:
    """登录校验：用户不存在与密码错误返回同一文案，避免泄露账号是否存在。"""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT password_hash, role, display_name FROM app_user WHERE username = %s",
            (username,),
        ).fetchone()
    if row is None or not verify_password(password, row[0]):
        raise AuthError("用户名或密码错误")
    return {"username": username, "role": row[1], "display_name": row[2]}


def get_user(username: str) -> dict | None:
    """按用户名查询账号信息（Token 校验后反查用）。"""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT username, role, display_name FROM app_user WHERE username = %s",
            (username,),
        ).fetchone()
    if row is None:
        return None
    return {"username": row[0], "role": row[1], "display_name": row[2]}
