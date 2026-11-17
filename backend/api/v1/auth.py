# backend/api/v1/auth.py
# 登录与鉴权。bcrypt 校验放线程池，避免阻塞事件循环。

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.core.logger import get_logger
from backend.core.security import create_access_token, verify_password
from backend.db.session import get_db
from backend.dependencies import get_current_user

router = APIRouter()
logger = get_logger(__name__)


class LoginRequest(BaseModel):
    username: str = Field(..., description="用户名或邮箱")
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    role: str
    user_id: str


@router.post("/login", response_model=TokenResponse)
async def login(req: LoginRequest, db: AsyncSession = Depends(get_db)):
    row = (await db.execute(
        text("""SELECT id, password_hash, role, tenant_id, is_active FROM users
                WHERE username = :v OR email = :v LIMIT 1"""),
        {"v": req.username},
    )).fetchone()

    if not row or not row.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")
    if not await verify_password(req.password, row.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")

    token = create_access_token(str(row.id), row.role, row.tenant_id)
    logger.info("auth.login", user_id=str(row.id), role=row.role)
    return TokenResponse(
        access_token=token,
        expires_in=get_settings().jwt_access_token_expire_minutes * 60,
        role=row.role,
        user_id=str(row.id),
    )


@router.get("/me")
async def me(user: dict = Depends(get_current_user)):
    return user
