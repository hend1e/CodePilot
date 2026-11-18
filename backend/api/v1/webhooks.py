# backend/api/v1/webhooks.py
# GitHub / GitLab webhook 入口：验签 → 解析 → 幂等 → 落库 → 入队 → 202。
# 注意：必须读 raw body 验签，不能用 request.json()（会破坏签名）。

import json

from fastapi import APIRouter, Header, HTTPException, Request, status

from backend.core.security import verify_github_signature
from backend.core.logger import get_logger
from backend.core.rate_limit import check_rate_limit
from backend.core.github import parse_webhook
from backend.core.queue import get_queue
from backend.core import metrics
from backend.core.review_service import create_task

router = APIRouter()
logger = get_logger(__name__)


@router.post("/github", status_code=status.HTTP_202_ACCEPTED)
async def github_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(None),
    x_github_event: str | None = Header(None),
):
    body = await request.body()
    if not verify_github_signature(body, x_hub_signature_256):
        logger.warning("webhook.invalid_signature", event=x_github_event)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "签名校验失败")

    if x_github_event == "ping":
        return {"ok": True, "message": "pong"}

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "非法 JSON") from e

    parsed = parse_webhook(payload)
    if not parsed:
        return {"ok": True, "skipped": True, "reason": "非 PR 事件或 action 不在处理列表"}

    # 按仓库限流（防止一次推送触发大量 PR）
    allowed, remaining = await check_rate_limit(parsed["repo_full_name"])
    if not allowed:
        logger.warning("webhook.rate_limited", repo=parsed["repo_full_name"])
        return {"ok": True, "skipped": True, "reason": "仓库触发频率超限", "remaining": 0}

    task_id, created = await create_task(
        tenant_id="tenant_default",
        repo_full_name=parsed["repo_full_name"],
        diff_text="",                       # 由 ingest 节点用 GitHub API 拉取
        pr={"number": parsed["pr_number"], "title": parsed["title"],
            "author": parsed["author"], "head_sha": parsed["head_sha"],
            "html_url": parsed["html_url"]},
        provider="github",
        clone_url=parsed.get("clone_url"),
        idempotency_key=parsed["idempotency_key"],
        source="webhook",
    )
    if created:
        await get_queue().enqueue({"task_id": task_id})
        metrics.inc("codepilot_reviews_created_total", repo=parsed["repo_full_name"])

    return {"ok": True, "task_id": task_id, "created": created, "remaining": remaining}
