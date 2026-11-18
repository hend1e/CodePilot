# backend/core/reranker.py
# 精排（对应课件 5.7 重排序）：cross-encoder / lexical / none 三种后端。
# 契约：失败一律降级到 lexical，不抛异常打断检索链路。

import asyncio
import re

from backend.config import get_settings
from backend.core.logger import get_logger

logger = get_logger(__name__)
_settings = get_settings()

RRF_K = 60
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[\u4e00-\u9fff]{1,2}")
_CAMEL_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*")


def split_identifier(tok: str) -> list[str]:
    """AuthMiddleware → auth middleware；user_id → user id。"""
    out: list[str] = []
    for part in tok.split("_"):
        out.extend(m.group(0).lower() for m in _CAMEL_RE.finditer(part))
        if part and not any(c.isupper() for c in part):
            out.append(part.lower())
    return out or [tok.lower()]


def query_tokens(query: str) -> set[str]:
    toks: set[str] = set()
    for t in _TOKEN_RE.findall(query):
        toks.update(split_identifier(t))
    return toks


def lexical_overlap(tokens: set[str], content: str) -> float:
    toks: set[str] = set()
    for t in _TOKEN_RE.findall(content):
        toks.update(split_identifier(t))
    if not tokens or not toks:
        return 0.0
    return len(tokens & toks) / len(tokens)


_cross_encoder = None


def _get_cross_encoder():
    global _cross_encoder
    if _cross_encoder is None:
        from sentence_transformers import CrossEncoder

        _cross_encoder = CrossEncoder(_settings.rerank_model, device="cpu")
    return _cross_encoder


async def rerank(query: str, items: list[dict], top_k: int) -> tuple[list[dict], str]:
    """返回 (排序后的 items, 实际使用的后端名)。"""
    backend = _settings.rerank_backend.lower()
    tokens = query_tokens(query)

    if backend == "cross-encoder":
        try:
            model = _get_cross_encoder()
            pairs = [(query, it["content"][:2000]) for it in items]
            scores = await asyncio.to_thread(model.predict, pairs)
            for it, score in zip(items, scores, strict=True):
                it["rerank_score"] = float(score)
            items.sort(key=lambda x: x["rerank_score"], reverse=True)
            return items[:top_k], "cross-encoder"
        except Exception as e:      # noqa: BLE001 —— 模型没装/加载失败都退回词面精排
            logger.warning("rerank.degraded", backend="cross-encoder", error=str(e)[:200])
            backend = "lexical"

    if backend == "none":
        return items[:top_k], "none"

    for it in items:
        it["rerank_score"] = round(
            0.6 * it.get("rrf", 0.0) * RRF_K + 0.4 * lexical_overlap(tokens, it["content"]), 6
        )
    items.sort(key=lambda x: x["rerank_score"], reverse=True)
    return items[:top_k], "lexical"
