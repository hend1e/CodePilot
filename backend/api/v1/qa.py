# backend/api/v1/rag_api.py
# 代码库 RAG 接口：建索引 / 问答 / 统计。

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from backend.agents.qa.graph import ask
from backend.core.knowledge_base import ingest_repo
from backend.core.security import safe_repo_path
from backend.db.session import AsyncSessionLocal
from backend.dependencies import get_current_user
from backend.core.review_service import ensure_repo

router = APIRouter()


class IngestRequest(BaseModel):
    repo: str = Field(..., description="owner/repo 或本地目录名")
    path: str = Field(..., description="本地仓库绝对/相对路径（白名单内）")
    reset: bool = True


class QueryRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=4000)
    repo_id: str | None = None
    thread_id: str | None = Field(None, description="传入则启用多轮记忆（课件 5.9）")


@router.post("/ingest", status_code=status.HTTP_202_ACCEPTED)
async def ingest(req: IngestRequest, background: BackgroundTasks,
                 user: dict = Depends(get_current_user)):
    root = safe_repo_path(req.path)
    repo = await ensure_repo(user["tenant_id"], req.repo, provider="local",
                             local_path=str(root))
    background.add_task(_ingest_bg, repo["id"], str(root), req.reset)
    return {"repo_id": repo["id"], "status": "indexing"}


async def _ingest_bg(repo_id: str, path: str, reset: bool) -> None:
    from backend.core.logger import get_logger

    try:
        stats = await ingest_repo(repo_id, path, reset=reset)
        get_logger(__name__).info("rag.ingest_done", **stats)
    except Exception as e:      # noqa: BLE001
        get_logger(__name__).error("rag.ingest_failed", error=str(e))


@router.post("/query")
async def query(req: QueryRequest, user: dict = Depends(get_current_user)):
    memory = ""
    if req.thread_id:
        from backend.core.memory import ConversationMemory

        memory = await ConversationMemory(req.thread_id, user["tenant_id"]).render()
    out = await ask(req.question, req.repo_id, user["tenant_id"],
                    thread_id=req.thread_id, memory=memory)
    if req.thread_id and out.get("answer"):
        from backend.core.memory import ConversationMemory

        await ConversationMemory(req.thread_id, user["tenant_id"]).append(
            req.question, out["answer"])
    return {
        "answer": out.get("answer", ""),
        "citations": out.get("citations", []),
        "degraded": out.get("degraded", False),
        "queries": out.get("queries", []),
    }


@router.get("/repos")
async def repos(user: dict = Depends(get_current_user)):
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            text("""
                SELECT r.id, r.full_name, r.local_path,
                       (SELECT COUNT(*) FROM code_chunks c WHERE c.repo_id = r.id) AS chunks
                FROM repos r WHERE r.tenant_id = :t ORDER BY r.created_at DESC
            """),
            {"t": user["tenant_id"]},
        )).mappings().all()
    return {"items": [dict(r) for r in rows]}


@router.get("/{repo_id}/status")
async def index_status(repo_id: str, user: dict = Depends(get_current_user)):
    async with AsyncSessionLocal() as session:
        row = (await session.execute(
            text("""SELECT COUNT(*) AS chunks, COUNT(DISTINCT path) AS files,
                           MAX(indexed_at) AS last_indexed
                    FROM code_chunks c JOIN code_documents d ON d.id = c.doc_id
                    WHERE c.repo_id = :r"""),
            {"r": repo_id},
        )).mappings().fetchone()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "仓库不存在")
    return dict(row)
