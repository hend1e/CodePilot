# backend/api/v1/chat.py
# 统一入口（SSE）：一条消息 → 路由 → 子 Agent → 流式事件。
# 事件类型：route / citations / token / stage / result / done / error

import asyncio
import json

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from backend.core.orchestrator import classify
from backend.agents.qa.nodes import persist as rag_persist
from backend.agents.qa.prompts import RAG_SYSTEM, RAG_USER, build_context
from backend.core.knowledge_base import retrieve_context
from backend.config import get_settings
from backend.core.llm_factory import get_llm
from backend.core.logger import get_logger
from backend.dependencies import get_current_user
from backend.core import metrics
from backend.core.review_service import create_task, run_review_task

router = APIRouter()
logger = get_logger(__name__)
settings = get_settings()


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=20_000)
    repo: str = "local/repo"
    repo_id: str | None = None
    pr_number: int | None = None
    thread_id: str | None = Field(None, description="传入则启用多轮记忆（课件 5.9）")


def _sse(event: str, data: dict) -> dict:
    return {"event": event, "data": json.dumps(data, ensure_ascii=False, default=str)}


@router.post("/chat")
async def chat(req: ChatRequest, user: dict = Depends(get_current_user)):
    return EventSourceResponse(_stream(req, user))


async def _stream(req: ChatRequest, user: dict):
    try:
        decision = await classify(req.message)
        metrics.inc("codepilot_chat_total", intent=decision.intent)
        yield _sse("route", decision.as_dict())

        if decision.intent == "qa":
            async for chunk in _qa_stream(req, user):
                yield chunk
            return

        yield _sse("stage", {"stage": "accepted", "intent": decision.intent})
        task_id, created = await create_task(
            tenant_id=user["tenant_id"], repo_full_name=req.repo,
            diff_text=req.message if decision.intent in ("review", "test") else "",
            pr={"number": req.pr_number} if req.pr_number else {},
            source="chat",
        )
        if created:
            # 聊天场景直接内联执行（低延迟），任务照常落库以便审计
            yield _sse("stage", {"stage": "running", "task_id": task_id})
            try:
                final = await run_review_task(task_id)
            except Exception as e:      # noqa: BLE001
                yield _sse("error", {"message": str(e)[:500], "task_id": task_id})
                return
            if final.get("__interrupted__"):
                yield _sse("result", {"kind": "review", "task_id": task_id,
                                      "waiting_human": final["__interrupted__"]})
            else:
                yield _sse("result", {"kind": decision.intent, "task_id": task_id,
                                      "summary": final.get("summary"),
                                      "test_report": final.get("test_report")})
        else:
            yield _sse("result", {"kind": decision.intent, "task_id": task_id, "duplicate": True})
        yield _sse("done", {"task_id": task_id})
    except Exception as e:      # noqa: BLE001
        logger.error("chat.stream_failed", error=str(e), exc_info=True)
        yield _sse("error", {"message": str(e)[:500]})


async def _qa_stream(req: ChatRequest, user: dict):
    from backend.core.memory import ConversationMemory

    memory = ConversationMemory(req.thread_id, user["tenant_id"]) if req.thread_id else None
    try:
        ctx = await retrieve_context(req.message, req.repo_id, user["tenant_id"],
                                     top_k=settings.rerank_top_k)
    except Exception as e:      # noqa: BLE001
        yield _sse("error", {"message": f"检索失败: {e}"[:300]})
        return

    chunks = ctx.get("chunks", [])
    yield _sse("citations", {"chunks": [{k: c[k] for k in (
        "citation", "path", "symbol", "start_line", "end_line")} for c in chunks],
        "degraded": ctx.get("degraded", False)})

    mem_text = await memory.render() if memory else ""
    llm = get_llm("rag_answer", temperature=0.1, streaming=True)
    answer_parts: list[str] = []

    if llm is None:
        from backend.agents.qa.nodes import _citations, _extractive

        text_out = _extractive(chunks)
        answer_parts.append(text_out)
        yield _sse("token", {"text": text_out})
        citations, invalid = _citations(text_out, chunks)
    else:
        messages = [
            {"role": "system", "content": RAG_SYSTEM},
            {"role": "user", "content": RAG_USER.format(
                question=req.message,
                context=(f"{mem_text}\n\n" if mem_text else "") + build_context(chunks))},
        ]
        invalid = 0
        try:
            async for event in llm.astream(messages):
                piece = event.content or ""
                if piece:
                    answer_parts.append(piece)
                    yield _sse("token", {"text": piece})
        except Exception as e:      # noqa: BLE001
            yield _sse("error", {"message": f"生成失败: {e}"[:300]})
            return
        from backend.agents.qa.nodes import _citations

        citations, invalid = _citations("".join(answer_parts), chunks)

    await rag_persist({
        "question": req.message, "repo_id": req.repo_id, "tenant_id": user["tenant_id"],
        "answer": "".join(answer_parts), "citations": citations,
        "degraded": ctx.get("degraded", False) or invalid > 0,
    })
    if memory:
        await memory.append(req.message, "".join(answer_parts))
    metrics.inc("codepilot_rag_queries_total", degraded=str(ctx.get("degraded", False)))
    yield _sse("done", {"citations": len(citations), "invalid_citations": invalid})
