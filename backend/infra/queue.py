# backend/infra/queue.py
# 任务队列：Redis Streams（消费组 + ACK + 重投），Redis 不可用时退化为进程内队列。
# 为什么用 Streams 而不是 Celery：少一个中间件、支持消费组、天然可观测。

import asyncio
import json
import uuid
from typing import Any

from backend.config import get_settings
from backend.core.logger import get_logger

logger = get_logger(__name__)


class InMemoryQueue:
    """单进程队列，仅用于本地调试 / 单测。"""

    def __init__(self) -> None:
        self._q: asyncio.Queue[dict] = asyncio.Queue()

    async def enqueue(self, payload: dict) -> str:
        msg_id = f"mem-{uuid.uuid4().hex[:12]}"
        payload = {**payload, "queue_msg_id": msg_id, "attempt": payload.get("attempt", 0)}
        await self._q.put(payload)
        return msg_id

    async def consume(self) -> dict:
        return await self._q.get()

    async def ack(self, payload: dict) -> None:
        return None

    async def retry(self, payload: dict, error: str) -> None:
        attempt = int(payload.get("attempt", 0)) + 1
        if attempt <= 3:
            await self._q.put({**payload, "attempt": attempt, "last_error": error[:500]})

    async def close(self) -> None:
        return None

    async def ping(self) -> bool:
        return True


class RedisStreamQueue:
    """XADD 入队；XREADGROUP 消费；XACK + 失败重投。"""

    def __init__(self) -> None:
        import redis.asyncio as aioredis

        self.s = get_settings()
        self.redis = aioredis.from_url(self.s.redis_url, decode_responses=True)
        self.stream = self.s.queue_name
        self.group = self.s.queue_group
        self._consumer = f"worker-{uuid.uuid4().hex[:8]}"
        self._group_ready = False

    async def _ensure_group(self) -> None:
        if self._group_ready:
            return
        try:
            await self.redis.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except Exception as e:      # BUSYGROUP = 已存在
            if "BUSYGROUP" not in str(e):
                raise
        self._group_ready = True

    async def enqueue(self, payload: dict) -> str:
        await self._ensure_group()
        body = {**payload, "attempt": payload.get("attempt", 0)}
        msg_id = await self.redis.xadd(
            self.stream, {"payload": json.dumps(body, ensure_ascii=False)}, maxlen=10_000
        )
        logger.info("queue.enqueued", msg_id=msg_id, task_id=payload.get("task_id"))
        return str(msg_id)

    async def consume(self) -> dict:
        await self._ensure_group()
        while True:
            resp = await self.redis.xreadgroup(
                self.group, self._consumer, {self.stream: ">"}, count=1, block=3000
            )
            if not resp:
                continue
            _, entries = resp[0]
            msg_id, fields = entries[0]
            payload = json.loads(fields["payload"])
            payload["queue_msg_id"] = msg_id
            return payload

    async def ack(self, payload: dict) -> None:
        msg_id = payload.get("queue_msg_id")
        if msg_id:
            await self.redis.xack(self.stream, self.group, msg_id)

    async def retry(self, payload: dict, error: str) -> None:
        await self.ack(payload)
        attempt = int(payload.get("attempt", 0)) + 1
        if attempt <= 3:
            await self.enqueue({**payload, "attempt": attempt, "last_error": error[:500]})
        else:
            await self.redis.xadd(
                f"{self.stream}:dead",
                {"payload": json.dumps({**payload, "last_error": error[:500]}, ensure_ascii=False)},
                maxlen=2_000,
            )
            logger.error("queue.dead_letter", task_id=payload.get("task_id"), error=error[:200])

    async def close(self) -> None:
        await self.redis.aclose()

    async def ping(self) -> bool:
        try:
            await self.redis.ping()
            return True
        except Exception:
            return False


_queue: Any = None


def get_queue():
    """全局队列单例：Redis 配置为空 → 内存队列。"""
    global _queue
    if _queue is not None:
        return _queue
    s = get_settings()
    if s.redis_url.strip():
        _queue = RedisStreamQueue()
    else:
        _queue = InMemoryQueue()
        logger.warning("queue.degraded", backend="memory")
    return _queue


async def init_queue() -> None:
    """启动时探测 Redis：不可用则立刻降级到内存队列（避免 worker 空转刷日志）。"""
    global _queue
    s = get_settings()
    if not s.redis_url.strip():
        get_queue()
        return
    candidate = RedisStreamQueue()
    if await candidate.ping():
        _queue = candidate
        logger.info("queue.ready", backend="redis", stream=s.queue_name)
        return
    await candidate.close()
    _queue = InMemoryQueue()
    logger.warning("queue.degraded", backend="memory", reason="redis_unreachable")


async def reset_queue() -> None:
    global _queue
    if _queue is not None:
        await _queue.close()
    _queue = None
