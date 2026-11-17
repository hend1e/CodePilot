# backend/infra/worker.py
# 消费者：从 Redis Stream 取任务 → 跑审查图。可多副本水平扩展（消费组自动分片）。
# 优雅退出：收到 SIGTERM 处理完当前任务再退出。

import asyncio
import signal

from backend.core.logger import get_logger
from backend.infra.queue import get_queue
from backend.services.review_service import run_review_task

logger = get_logger(__name__)


class Worker:
    def __init__(self, concurrency: int = 2):
        self.queue = get_queue()
        self.concurrency = concurrency
        self._stopping = False
        self._sem = asyncio.Semaphore(concurrency)

    async def _handle(self, payload: dict) -> None:
        task_id = payload.get("task_id")
        async with self._sem:
            try:
                logger.info("worker.task_start", task_id=task_id,
                            attempt=payload.get("attempt", 0))
                await run_review_task(task_id)
                await self.queue.ack(payload)
            except Exception as e:      # noqa: BLE001
                logger.error("worker.task_failed", task_id=task_id, error=str(e)[:500])
                await self.queue.retry(payload, str(e))

    async def run_forever(self) -> None:
        logger.info("worker.started", concurrency=self.concurrency)
        tasks: set[asyncio.Task] = set()
        failures = 0
        while not self._stopping:
            try:
                payload = await asyncio.wait_for(self.queue.consume(), timeout=5)
            except asyncio.TimeoutError:
                continue
            except Exception as e:      # noqa: BLE001
                failures += 1
                if failures <= 3:
                    logger.error("worker.consume_failed", error=str(e)[:300], failures=failures)
                await asyncio.sleep(min(30, 2 * failures))
                continue
            task = asyncio.create_task(self._handle(payload))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        if tasks:
            logger.info("worker.draining", pending=len(tasks))
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.queue.close()
        logger.info("worker.stopped")

    def stop(self) -> None:
        self._stopping = True


async def main() -> None:
    from backend.config import get_settings
    from backend.core.logger import configure_logging
    from backend.db.session import close_checkpointer, init_checkpointer

    configure_logging()
    await init_queue()
    await init_checkpointer()
    worker = Worker(concurrency=2)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, worker.stop)
    try:
        await worker.run_forever()
    finally:
        await close_checkpointer()


if __name__ == "__main__":
    asyncio.run(main())
