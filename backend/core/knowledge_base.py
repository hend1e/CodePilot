# backend/core/knowledge_base.py
# 代码知识库（对应课件第 5 章）：
#   建索引：遍历仓库 → 函数级切分 → 向量化 → 写 Milvus + PG
#   检索：dense(Milvus) + sparse(PG 全文) → RRF 融合 → 精排 → Top-K
# Milvus 只存 id + vector（语义召回）；PG 存正文/行号（稀疏召回、引用、展示）。

import hashlib
import time
import uuid
from pathlib import Path

from sqlalchemy import text

from backend.core.chunking import CodeChunk, chunk_file, iter_repo_files, search_text, term_queries
from backend.core.embedding import get_embedder
from backend.core.reranker import rerank
from backend.config import get_settings
from backend.core.exceptions import MilvusUnavailable, RetrievalError
from backend.core.logger import get_logger
from backend.core.retry import estimate_tokens, with_retry
from backend.db.session import AsyncSessionLocal

logger = get_logger(__name__)
_settings = get_settings()


class MilvusStore:
    """懒加载的 Milvus 客户端。任何失败都转成 MilvusUnavailable，让上层降级到纯稀疏检索。"""

    def __init__(self) -> None:
        self._client = None
        self._ready = False
        self._down_until = 0.0          # 熔断：连续失败后 60s 内直接快速失败
        self.collection = _settings.milvus_collection
        self.dim = _settings.embedding_dim

    def _connect(self):
        from pymilvus import MilvusClient

        uri = f"http://{_settings.milvus_host}:{_settings.milvus_port}"
        return MilvusClient(uri=uri, timeout=3)

    def ensure(self, dim: int) -> None:
        import time

        if self._ready and self.dim == dim:
            return
        if time.monotonic() < self._down_until:
            raise MilvusUnavailable("Milvus 熔断中（近期连接失败）")
        try:
            if self._client is None:
                self._client = self._connect()
            if not self._client.has_collection(self.collection):
                self._client.create_collection(
                    collection_name=self.collection,
                    dimension=dim,
                    primary_field_name="id",
                    id_type="string",
                    max_length=64,
                    vector_field_name="vector",
                    metric_type="COSINE",
                    auto_id=False,
                )
                logger.info("milvus.collection_created", collection=self.collection, dim=dim)
            self.dim, self._ready = dim, True
        except Exception as e:      # noqa: BLE001
            import time

            self._down_until = time.monotonic() + 60
            self._ready = False
            raise MilvusUnavailable(f"Milvus 不可用: {e}") from e

    async def upsert(self, rows: list[dict]) -> None:
        if not rows:
            return
        self.ensure(len(rows[0]["vector"]))
        try:
            await with_retry(lambda: _run(self._client.upsert, self.collection, rows),
                             attempts=3, label="milvus.upsert")
            logger.info("milvus.upserted", count=len(rows))
        except Exception as e:      # noqa: BLE001
            raise MilvusUnavailable(f"Milvus 写入失败: {e}") from e

    async def search(self, vector: list[float], repo_id: str | None, top_k: int = 30) -> list[dict]:
        self.ensure(len(vector))
        expr = f'repo_id == "{repo_id}"' if repo_id else None
        try:
            hits = await with_retry(
                lambda: _run(
                    self._client.search,
                    collection_name=self.collection, data=[vector], limit=top_k,
                    filter=expr or "", output_fields=["repo_id", "path", "symbol"],
                    search_params={"metric_type": "COSINE", "params": {"nprobe": 16}},
                ),
                attempts=2, label="milvus.search",
            )
            return [{"id": h["id"], "score": float(h["distance"])} for h in (hits[0] if hits else [])]
        except Exception as e:      # noqa: BLE001
            raise MilvusUnavailable(f"Milvus 检索失败: {e}") from e

    async def delete_repo(self, repo_id: str) -> None:
        try:
            self.ensure(self.dim)
            await _run(self._client.delete, self.collection,
                       filter=f'repo_id == "{repo_id}"')
        except Exception as e:      # noqa: BLE001
            logger.warning("milvus.delete_failed", repo_id=repo_id, error=str(e))

    async def healthy(self) -> bool:
        try:
            self.ensure(self.dim)
            return True
        except MilvusUnavailable:
            return False


async def _run(fn, *args, **kwargs):
    """pymilvus 是同步 SDK，丢线程池避免阻塞事件循环。

    注意：fn 必须是「函数本身」，不能是已经调用过的结果 —— 之前把
    `client.delete(...)` 的返回值传进来，才会报 `OmitZeroDict is not callable`。
    """
    import asyncio

    return await asyncio.to_thread(fn, *args, **kwargs)


def _sha(text_value: str) -> str:
    return hashlib.sha1(text_value.encode("utf-8")).hexdigest()[:16]


def _windows(lines: list[str], start: int, end: int, size: int = 40, overlap: int = 8):
    """把长 chunk 切成滑动窗口，用于稀疏检索匹配（向量侧仍用整块）。"""
    out = []
    i = start - 1
    while i < end:
        j = min(i + size, end)
        out.append((i + 1, j, "\n".join(lines[i:j])))
        if j >= end:
            break
        i += size - overlap
    return out


async def ingest_repo(repo_id: str, root_path: str, *, reset: bool = True) -> dict:
    """把仓库灌入索引。幂等：reset=True 先清空该 repo 的 PG 与 Milvus 数据。"""
    root = Path(root_path).resolve()
    if not root.exists():
        raise RetrievalError(f"路径不存在: {root_path}")

    embedder = get_embedder()
    store = MilvusStore()

    files = list(iter_repo_files(str(root)))
    chunks_total = 0
    docs_total = 0
    milvus_ok = True
    inserted_vectors = 0
    batch: list[tuple[CodeChunk, str]] = []
    pending_rows: list[dict] = []

    async with AsyncSessionLocal() as session:
        if reset:
            await session.execute(text("DELETE FROM code_chunks WHERE repo_id = :rid"), {"rid": repo_id})
            await session.execute(text("DELETE FROM code_documents WHERE repo_id = :rid"), {"rid": repo_id})
            await session.commit()
            try:
                await store.delete_repo(repo_id)
            except MilvusUnavailable as e:
                logger.warning("rag.milvus_reset_skipped", error=str(e)[:200])

        for rel, abs_path in files:
            try:
                content = abs_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if not content.strip():
                continue
            sha = _sha(content)
            lang = rel.rsplit(".", 1)[-1].lower() if "." in rel else "text"
            result = await session.execute(
                text("""
                    INSERT INTO code_documents (id, repo_id, path, lang, sha, size_bytes)
                    VALUES (:id, :rid, :path, :lang, :sha, :size)
                    ON CONFLICT (repo_id, path, sha)
                    DO UPDATE SET indexed_at = NOW()
                    RETURNING id
                """),
                {"id": str(uuid.uuid4()), "rid": repo_id, "path": rel,
                 "lang": lang, "sha": sha, "size": len(content.encode("utf-8"))},
            )
            doc_id = str(result.scalar_one())
            docs_total += 1

            for chunk in chunk_file(rel, content):
                chunk_id = str(uuid.uuid4())
                await session.execute(
                    text("""
                        INSERT INTO code_chunks (id, doc_id, repo_id, path, symbol, symbol_type,
                                                 start_line, end_line, content, token_estimate,
                                                 sparse_tsv)
                        VALUES (:id, :doc, :rid, :path, :sym, :stype, :sl, :el, :content, :tok,
                                to_tsvector('simple', :search))
                    """),
                    {"id": chunk_id, "doc": doc_id, "rid": repo_id, "path": chunk.path,
                     "sym": chunk.symbol, "stype": chunk.symbol_type, "sl": chunk.start_line,
                     "el": chunk.end_line, "content": chunk.content,
                     "tok": estimate_tokens(chunk.content),
                     "search": search_text(chunk.content)},
                )
                # 稀疏检索窗口（子块），共享 chunk_id 便于回填
                for _, _, win in _windows(chunk.content.split("\n"), chunk.start_line, chunk.end_line):
                    await session.execute(
                        text("""
                            INSERT INTO code_chunks (id, doc_id, repo_id, path, symbol, symbol_type,
                                                     start_line, end_line, content, token_estimate,
                                                     sparse_tsv)
                            VALUES (:id, :doc, :rid, :path, :sym, 'window', :sl, :el, :content, :tok,
                                    to_tsvector('simple', :search))
                        """),
                        {"id": str(uuid.uuid4()), "doc": doc_id, "rid": repo_id, "path": chunk.path,
                         "sym": chunk.symbol, "sl": chunk.start_line, "el": chunk.end_line,
                         "content": win, "tok": estimate_tokens(win), "search": search_text(win)},
                    )
                batch.append((chunk, chunk_id))
                chunks_total += 1

            if len(batch) >= 32:
                pending_rows.extend(await _embed_batch(embedder, batch, repo_id))
                batch.clear()
            if pending_rows and len(pending_rows) >= 128:
                ok = await _safe_upsert(store, pending_rows)
                milvus_ok = ok and milvus_ok
                inserted_vectors += len(pending_rows) if ok else 0
                pending_rows.clear()

        if batch:
            pending_rows.extend(await _embed_batch(embedder, batch, repo_id))
        if pending_rows:
            ok = await _safe_upsert(store, pending_rows)
            milvus_ok = ok and milvus_ok
            inserted_vectors += len(pending_rows) if ok else 0
        await session.commit()

    logger.info("rag.ingested", repo_id=repo_id, files=docs_total, chunks=chunks_total,
                vectors=inserted_vectors, milvus=milvus_ok)
    return {"repo_id": repo_id, "files": docs_total, "chunks": chunks_total,
            "vectors": inserted_vectors,
            "milvus": milvus_ok, "degraded": not milvus_ok,
            "embedding_backend": _settings.embedding_backend}


async def _safe_upsert(store: "MilvusStore", rows: list[dict]) -> bool:
    """Milvus 写入失败不阻断建索引：PG 全文检索仍然可用，只是语义召回降级。"""
    try:
        await store.upsert(rows)
        return True
    except MilvusUnavailable as e:
        logger.warning("rag.milvus_upsert_degraded", error=str(e)[:200], rows=len(rows))
        return False


async def _embed_batch(embedder, batch: list[tuple[CodeChunk, str]], repo_id: str) -> list[dict]:
    texts = [f"{c.path}\n{c.symbol}\n{c.content}" for c, _ in batch]
    vectors = await embedder.embed(texts)
    rows = []
    for (chunk, chunk_id), vec in zip(batch, vectors, strict=True):
        rows.append({
            "id": chunk_id,
            "vector": vec,
            "repo_id": repo_id,
            "path": chunk.path,
            "symbol": chunk.symbol,
        })
    return rows


# ──────────────────────────────────────────────────────────────
# 检索（课件 5.6 混合召回 / 5.7 重排序）
# ──────────────────────────────────────────────────────────────

def _rrf(rank: int) -> float:
    return 1.0 / (60 + rank)


async def _dense(query: str, repo_id: str | None, limit: int) -> tuple[list[tuple[str, float]], str | None]:
    try:
        vec = (await get_embedder().embed([query]))[0]
        hits = await MilvusStore().search(vec, repo_id, top_k=limit)
        return [(h["id"], h["score"]) for h in hits], None
    except Exception as e:      # noqa: BLE001 —— 向量库不可用不是致命错误
        logger.warning("retrieval.dense_degraded", error=str(e)[:200])
        return [], f"dense_unavailable:{type(e).__name__}"


async def _sparse(query: str, repo_id: str | None, limit: int) -> list[tuple[str, float]]:
    terms = term_queries(query)
    if not terms:
        return []
    tsq = " | ".join(terms)
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


async def retrieve_context(query: str, repo_id: str | None = None,
                           tenant_id: str | None = None, top_k: int | None = None) -> dict:
    """返回 {"chunks": [...], "degraded": bool, "latency_ms": int, "fusion": "rrf"}。

    降级约定：Milvus 挂了只用 PG 全文，结果照常返回，degraded=True。
    """
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
        key = (meta["path"], meta["symbol"], meta["start_line"], meta["end_line"])
        if key in seen:         # window 子块与父块重复，优先保留语义块
            continue
        seen.add(key)
        items.append({**meta, **fused[cid], "citation": f"{meta['path']}"
                      + (f"::{meta['symbol']}" if meta["symbol"] else "")
                      + f":{meta['start_line']}-{meta['end_line']}"})

    items, rerank_backend = await rerank(query, items, top_k)
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
