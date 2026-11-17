# backend/dependencies.py
# FastAPI 依赖：从 Bearer Token 解析当前用户。角色校验用 require_role 工厂。

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from backend.core.exceptions import AuthenticationError
from backend.core.security import decode_access_token

bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> dict:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少认证凭证",
                            headers={"WWW-Authenticate": "Bearer"})
    try:
        return decode_access_token(credentials.credentials)
    except AuthenticationError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e),
                            headers={"WWW-Authenticate": "Bearer"}) from e


def require_role(*roles: str):
    async def _checker(user: dict = Depends(get_current_user)) -> dict:
        if roles and user.get("role") not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "权限不足")
        return user

    return _checker
