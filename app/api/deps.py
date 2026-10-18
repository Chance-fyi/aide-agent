"""FastAPI 鉴权依赖：从 Authorization: Bearer 头解析当前用户。

三级粒度，按接口敏感性选择：
- get_optional_user：尽力解析，失败不报错（对话接口未登录也能用，登录后可溯源）
- get_current_user：必须登录，否则 401
- require_agent：必须登录且角色为坐席，否则 401（坐席操作接口用）

用 Annotated 类型别名声明依赖（而非参数默认值调用 Depends）：
语义一致且对 ruff B008 规则友好。
"""

from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.security import ROLE_AGENT, decode_token
from app.core.users import get_user

# auto_error=False：没有 Authorization 头时不由库直接抛 403，
# 交给依赖函数自己决定是"降级为匿名"还是"返回 401"
_bearer = HTTPBearer(auto_error=False)


def _user_from_credentials(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> dict | None:
    """Token → 用户信息的公共解析：验签失败/账号已删除都按未认证处理。"""
    if creds is None:
        return None
    payload = decode_token(creds.credentials)
    if payload is None:
        return None
    return get_user(payload["sub"])


def get_optional_user(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> dict | None:
    return _user_from_credentials(creds)


def get_current_user(user: Annotated[dict | None, Depends(get_optional_user)]) -> dict:
    if user is None:
        raise HTTPException(status_code=401, detail="请先登录")
    return user


def require_agent(user: Annotated[dict, Depends(get_current_user)]) -> dict:
    if user["role"] != ROLE_AGENT:
        # 401 而非 403：不向前端区分"没登录"与"身份不够"，避免泄露接口存在感
        raise HTTPException(status_code=401, detail="需要坐席账号登录")
    return user


# 依赖注入的类型别名：路由函数签名直接用，保持可读
OptionalUser = Annotated[dict | None, Depends(get_optional_user)]
CurrentUser = Annotated[dict, Depends(get_current_user)]
AgentUser = Annotated[dict, Depends(require_agent)]
