# scripts/seed_data.py
# 初始化演示用户 + 一个本地仓库记录。
# 用法：python -m scripts.seed_data
# 默认账号：codepilot / codepilot123（生产环境请立刻改掉）

import asyncio
import os
import uuid

from sqlalchemy import text

from backend.core.logger import configure_logging, get_logger
from backend.core.security import hash_password
from backend.db.session import AsyncSessionLocal

logger = get_logger(__name__)


async def main() -> None:
    configure_logging()
    username = os.getenv("SEED_USERNAME", "codepilot")
    password = os.getenv("SEED_PASSWORD", "codepilot123")
    email = os.getenv("SEED_EMAIL", "codepilot@example.com")

    async with AsyncSessionLocal() as session:
        exists = (await session.execute(
            text("SELECT id FROM users WHERE tenant_id = :t AND email = :e"),
            {"t": "tenant_default", "e": email},
        )).fetchone()
        if exists:
            logger.info("seed.user_exists", email=email)
        else:
            await session.execute(
                text("""
                    INSERT INTO users (id, tenant_id, username, email, password_hash, role)
                    VALUES (:id, 'tenant_default', :u, :e, :p, 'admin')
                """),
                {"id": str(uuid.uuid4()), "u": username, "e": email,
                 "p": hash_password(password)},
            )
            logger.info("seed.user_created", username=username, email=email)

        repo_path = os.getenv("SEED_REPO_PATH", "")
        if repo_path:
            repo = (await session.execute(
                text("SELECT id FROM repos WHERE tenant_id = 'tenant_default' AND full_name = 'local/demo'")
            )).fetchone()
            if not repo:
                await session.execute(
                    text("""INSERT INTO repos (id, tenant_id, provider, full_name, local_path)
                            VALUES (:id, 'tenant_default', 'local', 'local/demo', :p)"""),
                    {"id": str(uuid.uuid4()), "p": repo_path},
                )
                logger.info("seed.repo_created", path=repo_path)
        await session.commit()

    print(f"\n✅ 初始化完成\n   账号: {username}\n   密码: {password}")


if __name__ == "__main__":
    asyncio.run(main())
