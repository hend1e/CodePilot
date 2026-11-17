# backend/db/migrations.py
# 启动时执行幂等 DDL 补丁。规则：只允许 ADD COLUMN IF NOT EXISTS / CREATE INDEX IF NOT EXISTS，
# 禁止 DROP / TRUNCATE。init_db.sql 改了结构，就在这里补一条。

from sqlalchemy import text

from backend.core.logger import get_logger
from backend.db.session import AsyncSessionLocal

logger = get_logger(__name__)

_MIGRATIONS: list[tuple[str, str]] = [
    (
        "review_tasks.degraded",
        "ALTER TABLE review_tasks ADD COLUMN IF NOT EXISTS degraded BOOLEAN NOT NULL DEFAULT FALSE",
    ),
    (
        "review_tasks.cost_usd",
        "ALTER TABLE review_tasks ADD COLUMN IF NOT EXISTS cost_usd NUMERIC(10,6) NOT NULL DEFAULT 0",
    ),
    (
        "findings.source",
        "ALTER TABLE findings ADD COLUMN IF NOT EXISTS source VARCHAR(8) NOT NULL DEFAULT 'rule'",
    ),
    (
        "code_chunks.sparse_tsv",
        "ALTER TABLE code_chunks ADD COLUMN IF NOT EXISTS sparse_tsv tsvector",
    ),
    (
        "idx_chunks_sparse_tsv",
        "CREATE INDEX IF NOT EXISTS idx_chunks_sparse_tsv ON code_chunks USING GIN (sparse_tsv)",
    ),
    (
        "idx_chunks_repo_path",
        "CREATE INDEX IF NOT EXISTS idx_chunks_repo_path ON code_chunks (repo_id, path)",
    ),
]


async def run_migrations() -> None:
    async with AsyncSessionLocal() as session:
        for desc, sql in _MIGRATIONS:
            try:
                await session.execute(text(sql))
                await session.commit()
            except Exception as e:  # 单条失败不阻断启动
                await session.rollback()
                if "already exists" not in str(e):
                    logger.warning("db.migration_failed", column=desc, error=str(e))
    logger.info("db.migrations_done", count=len(_MIGRATIONS))
