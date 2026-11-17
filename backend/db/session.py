# backend/db/session.py
# 数据库连接与 LangGraph Checkpointer 生命周期。
# 注意：AsyncPostgresSaver 需要 psycopg 连接池（异步），与业务用的 asyncpg 并存。

from contextlib import AsyncExitStack
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.config import get_settings
from backend.core.logger import get_logger

logger = get_logger(__name__)
settings = get_settings()

engine = create_async_engine(
    settings.database_url,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    echo=False,
)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖：请求级会话，自动提交/回滚。"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ── Checkpointer（进程级单例）──
_checkpointer = None
_exit_stack: AsyncExitStack | None = None


async def init_checkpointer():
    """启动时调用一次：PostgresSaver 常驻连接池；失败则降级 InMemorySaver。"""
    global _checkpointer, _exit_stack
    if _checkpointer is not None:
        return _checkpointer

    _exit_stack = AsyncExitStack()
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg_pool import AsyncConnectionPool

        pool = AsyncConnectionPool(
            conninfo=settings.psycopg_dsn,
            max_size=20,
            open=False,
            kwargs={"autocommit": True, "prepare_threshold": 0},
        )
        await _exit_stack.enter_async_context(pool)
        saver = AsyncPostgresSaver(pool)
        await saver.setup()
        _checkpointer = saver
        logger.info("checkpointer.ready", backend="postgres")
    except Exception as e:
        await _exit_stack.aclose()
        _exit_stack = None
        if settings.app_env == "prod":
            raise
        from langgraph.checkpoint.memory import InMemorySaver

        _checkpointer = InMemorySaver()
        logger.warning("checkpointer.degraded", backend="memory", error=str(e))
    return _checkpointer


async def close_checkpointer() -> None:
    global _checkpointer, _exit_stack
    if _exit_stack is not None:
        await _exit_stack.aclose()
    _checkpointer, _exit_stack = None, None


def get_checkpointer():
    """图编译时取用；未初始化则退回内存（单测场景）。"""
    if _checkpointer is None:
        from langgraph.checkpoint.memory import InMemorySaver

        return InMemorySaver()
    return _checkpointer
