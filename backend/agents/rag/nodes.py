# backend/agents/rag/nodes.py
# 代码库 RAG 子图：改写 → 多路召回 → 融合 → 生成 → 引用校验 → 落审计。
# 没有 LLM 时退化为「抽取式回答」：直接给最相关的片段 + 引用，不编造。

import asyncio
import json
import re
from typing import TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel
from sqlalchemy import text

from backend.agents.rag.prompts import (
    QUERY_REWRITE_SYSTEM, RAG_SYSTEM, RAG_USER, build_context,
)
from backend.agents.rag.retrieval import retrieve_context
from backend.config import get_settings
from backend.core.llm_factory import get_llm, get_structured_llm
from backend.core.logger import get_logger
from backend.db.session import AsyncSessionLocal

logger = get_logger(__name__)
settings = get_settings()
_CITE_RE = re.compile(r"\[(\d{1,2})\]")

MAX_MERGED = 12


class RAGState(TypedDict, total=False):
    question: str
    repo_id: str | None
    tenant_id: str
    task_id: str | None
    queries: list[str]
    chunks: list[dict]
    answer: str
    citations: list[dict]
    degraded: bool
    invalid_citations: int


class _QueriesOut(BaseModel):
    queries: list[str] = []


async def rewrite_query(state: RAGState) -> dict:
    q = state["question"].strip()
    queries = [q]
    if settings.llm_enabled and len(q) > 6:
        llm = get_structured_llm("rag_hyde", _QueriesOut)
        if llm is not None:
            try:
                async with asyncio.timeout(25):
                    out = await llm.ainvoke([
                        SystemMessage(content=QUERY_REWRITE_SYSTEM),
                        HumanMessage(content=q),
                    ])
                queries = [q, *[x for x in out.queries if x.strip() and x != q][:2]]
            except Exception as e:      # noqa: BLE001
                logger.warning("rag.rewrite_degraded", error=str(e)[:200])
    return {"queries": queries[:3], "degraded": False}


async def retrieve(state: RAGState) -> dict:
    merged: dict[str, dict] = {}
    degraded = False
    for query in state.get("queries") or [state["question"]]:
        try:
            ctx = await retrieve_context(query, state.get("repo_id"),
                                         state.get("tenant_id"), top_k=settings.rerank_top_k)
            degraded = degraded or ctx["degraded"]
            for c in ctx["chunks"]:
                key = c["citation"]
                cur = merged.get(key)
                if cur is None or c.get("rerank_score", 0) > cur.get("rerank_score", 0):
                    merged[key] = c
        except Exception as e:      # noqa: BLE001
            logger.warning("rag.retrieve_degraded", error=str(e)[:200])
            degraded = True

    chunks = sorted(merged.values(), key=lambda x: x.get("rerank_score", 0), reverse=True)
    return {"chunks": chunks[:MAX_MERGED], "degraded": degraded}


def _extractive(chunks: list[dict]) -> str:
    if not chunks:
        return "知识库中没有找到相关实现。建议先执行 `python scripts/ingest_repo.py` 建立索引。"
    lines = ["未启用大模型，以下是检索到的最相关代码片段（抽取式回答）：", ""]
    for i, c in enumerate(chunks[:5], start=1):
        lines.append(f"[{i}] `{c['path']}`"
                     + (f" :: `{c['symbol']}`" if c.get("symbol") else "")
                     + f"（行 {c['start_line']}-{c['end_line']}）")
        lines.append("```")
        lines.append("\n".join(c["content"].split("\n")[:20]))
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


async def answer(state: RAGState) -> dict:
    chunks = state.get("chunks", [])
    context = build_context(chunks)

    if not settings.llm_enabled:
        text_out = _extractive(chunks)
        citations, invalid = _citations(text_out, chunks)
        return {"answer": text_out, "citations": citations,
                "degraded": True, "invalid_citations": invalid}

    llm = get_llm("rag_answer", temperature=0.1)
    try:
        async with asyncio.timeout(90):
            resp = await llm.ainvoke([
                SystemMessage(content=RAG_SYSTEM),
                HumanMessage(content=RAG_USER.format(question=state["question"], context=context)),
            ])
        text_out = resp.content.strip()
    except Exception as e:      # noqa: BLE001
        logger.warning("rag.answer_degraded", error=str(e)[:200])
        text_out = _extractive(chunks)
        citations, invalid = _citations(text_out, chunks)
        return {"answer": text_out, "citations": citations,
                "degraded": True, "invalid_citations": invalid}

    citations, invalid = _citations(text_out, chunks)
    return {"answer": text_out, "citations": citations,
            "degraded": state.get("degraded", False) or invalid > 0,
            "invalid_citations": invalid}


def _citations(answer_text: str, chunks: list[dict]) -> tuple[list[dict], int]:
    """只保留真实存在的编号；引用了不存在的编号计入 invalid（用于评估幻觉率）。"""
    used: list[int] = []
    invalid = 0
    for m in _CITE_RE.finditer(answer_text):
        n = int(m.group(1))
        if 1 <= n <= len(chunks):
            if n not in used:
                used.append(n)
        else:
            invalid += 1
    out = [{
        "index": n, "path": chunks[n - 1]["path"], "symbol": chunks[n - 1].get("symbol", ""),
        "start_line": chunks[n - 1]["start_line"], "end_line": chunks[n - 1]["end_line"],
        "citation": chunks[n - 1]["citation"],
        "score": round(chunks[n - 1].get("rerank_score", 0.0), 4),
    } for n in used]
    return out, invalid


async def persist(state: RAGState) -> dict:
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(
                text("""
                    INSERT INTO rag_queries (tenant_id, task_id, repo_id, question, answer,
                                             citations, degraded, latency_ms)
                    VALUES (:tid, :task, :rid, :q, :a, CAST(:c AS JSONB), :d, :l)
                """),
                {"tid": state.get("tenant_id", settings.default_tenant_id),
                 "task": state.get("task_id"), "rid": state.get("repo_id"),
                 "q": state["question"], "a": state.get("answer", ""),
                 "c": json.dumps(state.get("citations", []), ensure_ascii=False),
                 "d": state.get("degraded", False), "l": 0},
            )
            await session.commit()
    except Exception as e:      # noqa: BLE001 —— 审计失败不影响回答
        logger.warning("rag.persist_failed", error=str(e)[:200])
    return {}
