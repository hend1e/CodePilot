# backend/agents/rag/retrieval.py
# 混合召回：dense(Milvus) + sparse(PG 全文) → RRF 融合 → 精排 → Top-K。
# 关键点：任一路召回失败都能降级给出结果（degraded=True），绝不让检索挂掉整条链路。

import re
import time

from sqlalchemy import text

from backend.agents.rag.chunking import term_queries
from backend.agents.rag.embedding import get_embedder
from backend.agents.rag.store import MilvusStore
from backend.config import get_settings
from backend.core.exceptions import RetrievalError
from backend.core.logger import get_logger
from backend.db.session import AsyncSessionLocal

logger = get_logger(__name__)
_settings = get_settings()

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[\u4e00-\u9fff]{1,2}")
RRF_K = 60


def _rrf(rank: int) -> float:
    return 1.0 / (RRF_K + rank)


_CAMEL_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*")


def _split_identifier(tok: str) -> list[str]:
    """AuthMiddleware → auth middleware；user_id → user id；其余原样小写。"""
    out: list[str] = []
    for part in tok.split("_"):
        out.extend(m.group(0).lower() for m in _CAMEL_RE.finditer(part))
        if not any(c.isupper() for c in part) and part:
            out.append(part.lower())
    return out or [tok.lower()]


def _lexical_overlap(query_tokens: set[str], content: str) -> float:
    toks: set[str] = set()
    for t in _TOKEN_RE.findall(content):
        toks.update(_split_identifier(t))
    if not query_tokens or not toks:
        return 0.0
    return len(query_tokens & toks) / len(query_tokens)


async def _dense(query: str, repo_id: str | None, limit: int) -> tuple[list[tuple[str, float]], str | None]:
    try:
        vec = (await get_embedder().embed([query]))[0]
        hits = await MilvusStore().search(vec, repo_id, top_k=limit)
        return [(h["id"], h["score"]) for h in hits], None
    except Exception as e:      # noqa: BLE001 —— 向量库不可用不是致命错误
        logger.warning("retrieval.dense_degraded", error=str(e)[:200])
        return [], f"dense_unavailable:{type(e).__name__}"


def _tsquery(text: str) -> str | None:
    terms = term_queries(text)
    if not terms:
        return None
    return " | ".join(terms)


async def _sparse(query: str, repo_id: str | None, limit: int) -> list[tuple[str, float]]:
    tsq = _tsquery(query)
    if not tsq:
        return []
    sql = """
        SELECT id, ts_rank_cd(sparse_tsv, to_tsquery('simple', CAST(:q AS TEXT))) AS rank
        FROM code_chunks
        WHERE sparse_tsv @@ to_tsquery('simple', CAST(:q AS TEXT))
          AND (CAST(:rid AS TEXT) IS NULL OR repo_id = CAST(:rid AS UUID))
        ORDER BY rank DESC
        LIMIT :lim
    """
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(text(sql), {"q": tsq, "rid": repo_id, "lim": limit})).fetchall()
    return [(str(r.id), float(r.rank)) for r in rows]


async def _load_chunks(ids: list[str]) -> dict[str, dict]:
    if not ids:
        return {}
    sql = """
        SELECT id, repo_id, path, symbol, symbol_type, start_line, end_line, content
        FROM code_chunks WHERE id = ANY(:ids)
    """
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(text(sql), {"ids": ids})).fetchall()
    return {
        str(r.id): {"id": str(r.id), "path": r.path, "symbol": r.symbol,
                    "symbol_type": r.symbol_type, "start_line": r.start_line,
                    "end_line": r.end_line, "content": r.content}
        for r in rows
    }


async def _rerank(query: str, items: list[dict], top_k: int) -> tuple[list[dict], str]:
    backend = _settings.rerank_backend.lower()
    q_tokens = {t.lower() for t in _TOKEN_RE.findall(query)}

    if backend == "cross-encoder":
        try:
            from sentence_transformers import CrossEncoder

            model = _get_cross_encoder()
            pairs = [(query, it["content"][:2000]) for it in items]
            scores = model.predict(pairs)
            for it, s in zip(items, scores, strict=True):
                it["rerank_score"] = float(s)
            items.sort(key=lambda x: x["rerank_score"], reverse=True)
            return items[:top_k], "cross-encoder"
        except Exception as e:      # noqa: BLE001
            logger.warning("rerank.degraded", backend="cross-encoder", error=str(e)[:200])
            backend = "lexical"

    if backend == "none":
        return items[:top_k], "none"

    for it in items:
        it["rerank_score"] = round(
            0.6 * it.get("rrf", 0.0) * RRF_K + 0.4 * _lexical_overlap(q_tokens, it["content"]), 6
        )
    items.sort(key=lambda x: x["rerank_score"], reverse=True)
    return items[:top_k], "lexical"


_cross_encoder = None


def _get_cross_encoder():
    global _cross_encoder
    if _cross_encoder is None:
        from sentence_transformers import CrossEncoder

        _cross_encoder = CrossEncoder(_settings.rerank_model, device="cpu")
    return _cross_encoder


async def retrieve_context(query: str, repo_id: str | None = None,
                           tenant_id: str | None = None, top_k: int | None = None) -> dict:
    """返回 {"chunks": [...], "degraded": bool, "latency_ms": int, "fusion": "rrf"}。"""
    started = time.monotonic()
    top_k = top_k or _settings.rerank_top_k
    width = max(_settings.retrieve_top_k, top_k * 2)
    if not query.strip():
        raise RetrievalError("query 为空")

    dense, dense_error = await _dense(query, repo_id, width)
    try:
        sparse = await _sparse(query, repo_id, width)
    except Exception as e:      # noqa: BLE001
        logger.warning("retrieval.sparse_degraded", error=str(e)[:200])
        sparse = []

    fused: dict[str, dict] = {}
    for rank, (cid, score) in enumerate(dense):
        fused.setdefault(cid, {"rrf": 0.0})["rrf"] += _rrf(rank + 1)
        fused[cid]["dense_score"] = score
    for rank, (cid, score) in enumerate(sparse):
        fused.setdefault(cid, {"rrf": 0.0})["rrf"] += _rrf(rank + 1)
        fused[cid]["sparse_score"] = score

    details = await _load_chunks(list(fused.keys()))
    items: list[dict] = []
    seen: set[tuple] = set()
    for cid, meta in details.items():
        # window 子块与父块内容重复，优先保留语义块
        key = (meta["path"], meta["symbol"], meta["start_line"], meta["end_line"])
        if key in seen:
            continue
        seen.add(key)
        items.append({**meta, **fused[cid], "citation": f"{meta['path']}"
                      + (f"::{meta['symbol']}" if meta["symbol"] else "")
                      + f":{meta['start_line']}-{meta['end_line']}"})

    items, rerank_backend = await _rerank(query, items, top_k)
    latency = int((time.monotonic() - started) * 1000)
    degraded = bool(dense_error) or not dense

    logger.info("retrieval.done", query_len=len(query), dense=len(dense), sparse=len(sparse),
                kept=len(items), rerank=rerank_backend, degraded=degraded, latency_ms=latency)
    return {
        "chunks": items,
        "degraded": degraded,
        "dense_error": dense_error,
        "rerank_backend": rerank_backend,
        "latency_ms": latency,
        "fusion": "rrf",
    }
