"""认证安全原语：密码哈希与签名 Token。

为什么不用第三方库（passlib/pyjwt）：
- 密码哈希用标准库 pbkdf2_hmac（NIST 认可的 PBKDF2-SHA256）足够满足本项目需求，
  避免为单一功能引入重依赖；
- Token 用 HMAC-SHA256 自签名，格式仿 JWT（payload 独立可见 + 签名防篡改），
  自包含、无需服务端存会话，与"单进程自足"的架构约束一致。
"""

import base64
import hashlib
import hmac
import json
import time

from app.core.config import get_settings

# 密码哈希参数：60 万轮迭代（OWASP 2023 对 PBKDF2-SHA256 的推荐量级）
_PBKDF2_ITERATIONS = 600_000
_SALT_BYTES = 16

# Token 有效期（秒）：7 天，过期需重新登录
TOKEN_TTL_SEC = 7 * 24 * 3600


def hash_password(password: str) -> str:
    """生成密码哈希，格式：pbkdf2$iterations$salt_hex$hash_hex。"""
    import os

    salt = os.urandom(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS
    )
    return f"pbkdf2${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验密码：用存储串里的盐和轮数重新计算，恒定时间比较防时序侧信道。"""
    try:
        algo, iterations, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(digest.hex(), hash_hex)
    except (ValueError, TypeError):
        # 存储串格式损坏按"密码错误"处理，不裸抛
        return False


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def create_token(user_id: int, role: str) -> str:
    """签发自包含 Token：payload 明文可读（不存敏感信息），签名防篡改。

    sub 存 app_user.id（代理主键）而非 username：用户改名后 Token 仍有效，
    与"其他表用 id 关联用户"的策略一致。
    """
    payload = {
        "sub": user_id,
        "role": role,
        "exp": int(time.time()) + TOKEN_TTL_SEC,
    }
    body = _b64url(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    sig = _b64url(
        hmac.new(
            get_settings().auth_secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256
        ).digest()
    )
    return f"{body}.{sig}"


def decode_token(token: str) -> dict | None:
    """验签 + 校验过期；任何失败返回 None（调用方统一按未认证处理）。"""
    try:
        body, sig = token.split(".")
        expected = hmac.new(
            get_settings().auth_secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256
        ).digest()
        # compare_digest 防时序攻击：签名校验失败绝不能提前泄露"哪一段不对"
        if not hmac.compare_digest(sig.encode("ascii"), _b64url(expected).encode("ascii")):
            return None
        payload = json.loads(_b64url_decode(body))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


# 角色枚举（app_user.role 取值）：与前端登录视图约定一致
ROLE_USER = "user"      # 普通用户（用户端登录）
ROLE_AGENT = "agent"    # 人工坐席（坐席端登录）
