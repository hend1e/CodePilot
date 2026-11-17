# backend/core/security.py
# 认证与验签：JWT 签发/校验、GitHub webhook HMAC 验签、路径白名单防目录穿越。

import asyncio
import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jose import JWTError, jwt
from passlib.context import CryptContext

from backend.config import get_settings
from backend.core.exceptions import AuthenticationError, InvalidInput

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(raw: str) -> str:
    return pwd_context.hash(raw)


async def verify_password(raw: str, hashed: str) -> bool:
    """bcrypt 是 CPU 密集操作，放线程池避免阻塞事件循环。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, pwd_context.verify, raw, hashed)


def create_access_token(subject: str, role: str, tenant_id: str) -> str:
    s = get_settings()
    payload = {
        "sub": subject,
        "role": role,
        "tenant_id": tenant_id,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=s.jwt_access_token_expire_minutes),
    }
    return jwt.encode(payload, s.jwt_secret_key, algorithm=s.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    s = get_settings()
    try:
        payload = jwt.decode(token, s.jwt_secret_key, algorithms=[s.jwt_algorithm])
    except JWTError as e:
        raise AuthenticationError("无效或过期的令牌") from e
    if not payload.get("sub"):
        raise AuthenticationError("令牌缺少 subject")
    return {
        "user_id": payload["sub"],
        "role": payload.get("role", "developer"),
        "tenant_id": payload.get("tenant_id", s.default_tenant_id),
    }


def verify_github_signature(body: bytes, signature_header: str | None) -> bool:
    """GitHub: X-Hub-Signature-256 = sha256=<hex>。用 compare_digest 防时序攻击。"""
    secret = get_settings().github_webhook_secret
    if not secret:
        # 未配置密钥时只允许非生产环境（本地联调）
        return get_settings().app_env != "prod"
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header.removeprefix("sha256="))


def safe_repo_path(candidate: str) -> Path:
    """只允许访问 REPO_LOCAL_ROOTS 白名单内的路径（webhook 参数不可信）。"""
    path = Path(candidate).resolve()
    roots = [Path(r).resolve() for r in get_settings().repo_local_root_list]
    if not any(path == root or root in path.parents for root in roots):
        raise InvalidInput(f"仓库路径不在白名单内: {candidate}")
    return path
