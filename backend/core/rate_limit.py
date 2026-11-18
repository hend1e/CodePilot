# backend/core/rate_limit.py
# 轻量限流 + 幂等辅助：Redis 计数器（INCR + EXPIRE），Redis 不可用时放行（不阻断业务）。

import time

from backend.config import get_settings
from backend.core.logger import get_logger

logger = get_logger(__name__)

WINDOW_SECONDS = 60
MAX_PER_WINDOW = 30

_client = None


def _redis():
    global _client
    if _client is None:
        import redis.asyncio as aioredis

        _client = aioredis.from_url(get_settings().redis_url, decode_responses=True)
    return _client


async def check_rate_limit(key: str) -> tuple[bool, int]:
    """按 key 做 60s 窗口限流，返回 (是否放行, 剩余次数)。"""
    if not get_settings().redis_url.strip():
        return True, MAX_PER_WINDOW
    bucket = f"codepilot:rl:{key}:{int(time.time() // WINDOW_SECONDS)}"
    try:
        client = _redis()
        count = await client.incr(bucket)
        if count == 1:
            await client.expire(bucket, WINDOW_SECONDS + 5)
        return count <= MAX_PER_WINDOW, max(0, MAX_PER_WINDOW - count)
    except Exception as e:      # noqa: BLE001
        logger.warning("guard.redis_unavailable", error=str(e)[:200])
        return True, MAX_PER_WINDOW
