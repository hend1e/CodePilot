# backend/core/retry.py
# 三层兜底：① 节点内重试（指数退避 + 抖动）② 单维度超时/降级 ③ 图级降级不阻断。
# 只对「可重试」异常重试，业务错误（参数非法等）立即上抛。

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from backend.core.exceptions import CodePilotError
from backend.core.logger import get_logger

logger = get_logger(__name__)
T = TypeVar("T")


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 0.4,
    max_delay: float = 6.0,
    label: str = "call",
) -> T:
    """指数退避重试。attempts 含首次执行。"""
    last: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            return await fn()
        except CodePilotError as e:
            last = e
            if not e.retryable or i == attempts:
                raise
        except (asyncio.TimeoutError, ConnectionError) as e:
            last = e
            if i == attempts:
                raise
        delay = min(max_delay, base_delay * 2 ** (i - 1)) * (0.7 + random.random() * 0.6)
        logger.warning("retry.backoff", label=label, attempt=i, delay=round(delay, 2),
                       error=str(last))
        await asyncio.sleep(delay)
    raise last  # type: ignore[misc]


def estimate_tokens(text: str) -> int:
    """粗略 token 估算：中文按字符、英文按 4 字符 ≈ 1 token，够用于预算控制。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return int(cjk * 1.0 + other / 3.8) + 1
