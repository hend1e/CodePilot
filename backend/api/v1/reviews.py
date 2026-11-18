# backend/api/v1/reviews.py
# 审查任务接口：创建（202）→ 查询 → 人工确认恢复。

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from backend.agents.review.state import summarize
from backend.dependencies import get_current_user
from backend.core.queue import get_queue
from backend.core import metrics
from backend.core.review_service import create_task, get_task, list_tasks, resume_task

router = APIRouter()


class CreateReviewRequest(BaseModel):
    repo: str = Field(..., description="仓库标识，如 owner/repo 或本地目录名")
    diff: str = Field("", description="unified diff 文本（与 pr_url 至少给一个）")
    pr_number: int | None = None
    pr_title: str = ""
    local_path: str | None = Field(None, description="本地仓库路径（沙箱执行与索引用，必须在白名单内）")
    provider: str = "local"
    source: str = "manual"


class ResumeRequest(BaseModel):
    action: str = Field(..., pattern="^(approve|reject|edit)$")
    comment: str = ""
    edited_patch: str | None = None


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def create_review(req: CreateReviewRequest, user: dict = Depends(get_current_user)):
    from backend.core.security import safe_repo_path

    local_path = str(safe_repo_path(req.local_path)) if req.local_path else None
    pr = {"number": req.pr_number, "title": req.pr_title} if req.pr_number else {}
    task_id, created = await create_task(
        tenant_id=user["tenant_id"], repo_full_name=req.repo, diff_text=req.diff, pr=pr,
        local_path=local_path, provider=req.provider, source=req.source,
        idempotency_key=(f"{req.repo}#{req.pr_number}" if req.pr_number and req.diff else None),
    )
    if created:
        await get_queue().enqueue({"task_id": task_id, "tenant_id": user["tenant_id"]})
        metrics.inc("codepilot_reviews_created_total", repo=req.repo)
    return {"task_id": task_id, "created": created,
            "status": "queued" if created else "duplicate",
            "poll": f"/api/v1/reviews/{task_id}"}


@router.get("/{task_id}")
async def read_review(task_id: str, user: dict = Depends(get_current_user)):
    task = await get_task(task_id, user["tenant_id"])
    if not task:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    if task.get("findings"):
        task["summary"] = summarize(_as_findings(task["findings"]))
    return task


def _as_findings(rows: list[dict]):
    from backend.agents.review.state import Finding

    out = []
    for r in rows:
        try:
            out.append(Finding(**{k: r[k] for k in (
                "dimension", "severity", "file_path", "line_start", "line_end", "rule_id",
                "evidence", "message", "suggestion", "confidence", "source")}))
        except Exception:       # noqa: BLE001
            continue
    return out


@router.get("")
async def list_review_tasks(
    limit: int = Query(20, ge=1, le=100),
    status_filter: str | None = Query(None, alias="status"),
    user: dict = Depends(get_current_user),
):
    return {"items": await list_tasks(user["tenant_id"], limit, status_filter)}


@router.post("/{task_id}/resume")
async def resume_review(task_id: str, req: ResumeRequest, user: dict = Depends(get_current_user)):
    final = await resume_task(task_id, req.model_dump(), decided_by=user["user_id"])
    metrics.inc("codepilot_hitl_resolved_total", action=req.action)
    return {"task_id": task_id, "final": final}
