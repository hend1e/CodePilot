# backend/core/memory.py
# 多轮记忆（对应课件 5.9）：按 thread_id 保存最近几轮问答 + 滚动摘要。
# 存储用 Redis List（LTRIM 截断），Redis 不可用时内存兜底；摘要只在超过阈值时才压缩。

import json
import time

from backend.config import get_settings
from backend.core.logger import get_logger

logger = get_logger(__name__)
settings = get_settings()

MAX_TURNS = 6                 # 保留最近 6 轮原文
SUMMARY_TRIGGER = 4           # 超过 4 轮就尝试压缩成摘要
_TTL_SECONDS = 7 * 24 * 3600


class ConversationMemory:
    """一个 thread 一份记忆。key = codepilot:mem:{tenant}:{thread_id}。"""

    def __init__(self, thread_id: str, tenant_id: str | None = None,
                 max_turns: int = MAX_TURNS):
        self.thread_id = thread_id
        self.tenant_id = tenant_id or settings.default_tenant_id
        self.max_turns = max_turns
        self.key = f"codepilot:mem:{self.tenant_id}:{thread_id}"
        self._client = None
        self._fallback: list[dict] = []

    # ── 存储 ──
    def _redis(self):
        if self._client is None:
            import redis.asyncio as aioredis

            self._client = aioredis.from_url(settings.redis_url, decode_responses=True)
        return self._client

    async def _read(self) -> list[dict]:
        if not settings.redis_url.strip():
            return list(self._fallback)
        try:
            raw = await self._redis().lrange(self.key, 0, -1)
            return [json.loads(x) for x in raw]
        except Exception as e:      # noqa: BLE001
            logger.warning("memory.read_degraded", error=str(e)[:200])
            return list(self._fallback)

    async def append(self, question: str, answer: str) -> None:
        turn = {"q": question[:800], "a": answer[:1200], "ts": int(time.time())}
        self._fallback.append(turn)
        self._fallback = self._fallback[-self.max_turns:]
        if not settings.redis_url.strip():
            return
        try:
            client = self._redis()
            await client.rpush(self.key, json.dumps(turn, ensure_ascii=False))
            await client.ltrim(self.key, -self.max_turns, -1)
            await client.expire(self.key, _TTL_SECONDS)
        except Exception as e:      # noqa: BLE001
            logger.warning("memory.write_degraded", error=str(e)[:200])

    async def clear(self) -> None:
        self._fallback.clear()
        if settings.redis_url.strip():
            try:
                await self._redis().delete(self.key)
            except Exception:       # noqa: BLE001
                pass

    # ── 读取给 prompt 用 ──
    async def render(self, max_chars: int = 1500) -> str:
        turns = await self._read()
        if not turns:
            return ""
        lines: list[str] = ["## 历史对话（供理解指代，不要直接复述）"]
        for t in turns[-3:]:
            lines.append(f"用户：{t['q'][:200]}")
            lines.append(f"助手：{t['a'][:400]}")
        text = "\n".join(lines)
        return text[:max_chars]

    async def summarize_if_needed(self) -> str:
        """超过阈值时把旧轮次压成一句话（无 LLM 时退化为截断拼接）。"""
        turns = await self._read()
        if len(turns) < SUMMARY_TRIGGER:
            return ""
        joined = "；".join(f"{t['q'][:60]}" for t in turns[:-2])
        return f"此前用户问过：{joined}"[:600]
