"""登录/注册接口。

角色模型：app_user.role 区分 user（用户端）与 agent（坐席端），
登录后签发自包含 Token（HMAC 签名），前端带 Authorization: Bearer 访问。
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser
from app.core.security import ROLE_USER, create_token
from app.core.users import AuthError, authenticate, create_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=32, pattern=r"^[A-Za-z0-9_\-]+$",
                          description="用户名：字母/数字/下划线/连字符")
    password: str = Field(..., min_length=6, max_length=64, description="密码：至少 6 位")
    display_name: str = Field(default="", max_length=64, description="展示昵称，缺省用用户名")


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=32)
    password: str = Field(..., min_length=1, max_length=64)


class AuthResponse(BaseModel):
    token: str
    username: str
    role: str
    display_name: str


@router.post("/register", response_model=AuthResponse)
async def register(body: RegisterRequest):
    """注册普通用户账号并直接返回 Token（注册即登录）。

    坐席账号不开放自助注册：由管理员用 scripts/create_agent.py 创建，
    避免任意人注册成坐席接入用户会话。
    """
    try:
        user = create_user(
            body.username, body.password, ROLE_USER,
            body.display_name or None,
        )
    except AuthError as e:
        raise HTTPException(status_code=400, detail=e.message) from None
    except Exception:
        logger.exception("注册落库失败 username=%s", body.username)
        raise HTTPException(status_code=503, detail="注册暂时不可用，请稍后重试") from None
    return AuthResponse(
        token=create_token(user["username"], user["role"]),
        **user,
    )


@router.post("/login", response_model=AuthResponse)
async def login(body: LoginRequest):
    """登录：用户与坐席共用一个入口，角色由账号本身决定，
    前端据此决定进入哪个工作台——单表单少一次选择，也杜绝"角色选错"类问题。
    """
    try:
        user = authenticate(body.username, body.password)
    except AuthError as e:
        # 统一 401：不区分"账号不存在"与"密码错误"
        raise HTTPException(status_code=401, detail=e.message) from None
    except Exception:
        logger.exception("登录查询失败 username=%s", body.username)
        raise HTTPException(status_code=503, detail="登录暂时不可用，请稍后重试") from None
    return AuthResponse(
        token=create_token(user["username"], user["role"]),
        **user,
    )


@router.get("/me")
async def me(user: CurrentUser):
    """校验 Token 有效性并返回当前用户：前端启动恢复登录态时用。"""
    return user
