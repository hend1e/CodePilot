# backend/services/review_service.py
# 任务编排服务：建任务 → 入队 → 跑图 →（可中断）→ 恢复 → 落库。
# API / Worker 都只调这里，保证「命令行、webhook、聊天入口」行为一致。

import asyncio
import json
import uuid
from pathlib import Path

from sqlalchemy import text

from backend.agents.review.graph import build_review_graph
from backend.agents.review.persist import mark_status, save_final
from backend.core.exceptions import InvalidInput
from backend.core.logger import get_logger
from backend.db.session import AsyncSessionLocal

logger = get_logger(__name__)

_graph = None


def get_graph():
    global _graph
    if _graph is None:
        _graph = build_review_graph()
    return _graph


async def ensure_repo(tenant_id: str, full_name: str, *, provider: str = "local",
                      local_path: str | None = None, clone_url: str | None = None) -> dict:
    async with AsyncSessionLocal() as session:
        row = (await session.execute(
            text("SELECT id, full_name, local_path, provider FROM repos "
                 "WHERE tenant_id = :t AND full_name = :n"),
            {"t": tenant_id, "n": full_name},
        )).fetchone()
        if row:
            if local_path and row.local_path != local_path:
                await session.execute(text("UPDATE repos SET local_path = :p WHERE id = :id"),
                                      {"p": local_path, "id": row.id})
                await session.commit()
            return {"id": str(row.id), "full_name": row.full_name,
                    "local_path": local_path or row.local_path, "provider": row.provider}
        repo_id = str(uuid.uuid4())
        await session.execute(
            text("""INSERT INTO repos (id, tenant_id, provider, full_name, local_path, clone_url)
                    VALUES (:id, :t, :p, :n, :lp, :cu)"""),
            {"id": repo_id, "t": tenant_id, "p": provider, "n": full_name,
             "lp": local_path, "cu": clone_url},
        )
        await session.commit()
        return {"id": repo_id, "full_name": full_name, "local_path": local_path,
                "provider": provider}


async def create_task(
    *,
    tenant_id: str,
    repo_full_name: str,
    diff_text: str,
    pr: dict | None = None,
    local_path: str | None = None,
    provider: str = "local",
    clone_url: str | None = None,
    idempotency_key: str | None = None,
    source: str = "manual",
    kind: str = "pr_review",
) -> tuple[str, bool]:
    """返回 (task_id, created)。幂等键重复时返回已有任务。"""
    if not diff_text.strip():
        raise InvalidInput("diff 不能为空")

    repo = await ensure_repo(tenant_id, repo_full_name, provider=provider,
                             local_path=local_path, clone_url=clone_url)

    async with AsyncSessionLocal() as session:
        if idempotency_key:
            row = (await session.execute(
                text("SELECT id FROM review_tasks WHERE tenant_id = :t AND idempotency_key = :k"),
                {"t": tenant_id, "k": idempotency_key},
            )).fetchone()
            if row:
                logger.info("task.idempotent_hit", task_id=str(row.id), key=idempotency_key)
                return str(row.id), False

        # 同一个 PR 的新 commit：把旧任务标记 superseded（避免重复审查与成本浪费）
        if pr and pr.get("number"):
            await session.execute(
                text("""UPDATE review_tasks SET status = 'superseded', updated_at = NOW()
                        WHERE tenant_id = :t AND status IN ('queued','running')
                          AND trigger_payload->'pr'->>'number' = :num
                          AND trigger_payload->'repo'->>'full_name' = :repo"""),
                {"t": tenant_id, "num": str(pr["number"]), "repo": repo_full_name},
            )

        task_id = str(uuid.uuid4())
        payload = {"repo": repo, "pr": pr or {}, "diff_text": diff_text}
        await session.execute(
            text("""
                INSERT INTO review_tasks (id, tenant_id, repo_id, thread_id, kind, status,
                                          source, idempotency_key, trigger_payload)
                VALUES (:id, :t, :rid, :thread, :kind, 'queued', :src, :key, CAST(:payload AS JSONB))
            """),
            {"id": task_id, "t": tenant_id, "rid": repo["id"], "thread": task_id,
             "kind": kind, "src": source, "key": idempotency_key,
             "payload": json.dumps(payload, ensure_ascii=False)},
        )
        await session.commit()

    logger.info("task.created", task_id=task_id, repo=repo_full_name, source=source)
    return task_id, True


async def _load_payload(task_id: str) -> dict:
    async with AsyncSessionLocal() as session:
        row = (await session.execute(
            text("SELECT trigger_payload, status FROM review_tasks WHERE id = :id"),
            {"id": task_id},
        )).fetchone()
    if not row:
        raise InvalidInput(f"任务不存在: {task_id}")
    return row.trigger_payload or {}


async def run_review_task(task_id: str) -> dict:
    """执行审查图。被 interrupt 时返回 {"__interrupted__": payload}，状态置 waiting_human。"""
    payload = await _load_payload(task_id)
    pr = payload.get("pr") or {}
    initial = {
        "task_id": task_id,
        "tenant_id": payload.get("tenant_id", "tenant_default"),
        "thread_id": task_id,
        "repo": payload.get("repo", {}),
        "pr": pr,
        "diff_text": payload.get("diff_text", ""),
        "findings": [],
        "dim_status": {},
        "usage": [],
        "steps": 0,
        "no_progress": 0,
        "open_questions": [],
        "testgen_rounds": 0,
        "degraded": False,
    }
    config = {"configurable": {"thread_id": task_id}, "recursion_limit": 40}

    await mark_status(task_id, "running")
    try:
        result = await get_graph().ainvoke(initial, config=config)
    except Exception as e:      # noqa: BLE001
        logger.error("task.failed", task_id=task_id, error=str(e), exc_info=True)
        await mark_status(task_id, "failed", str(e))
        raise

    if result.get("__interrupt__"):
        await mark_status(task_id, "waiting_human")
        interrupt_payload = result["__interrupt__"][0].value
        logger.info("task.waiting_human", task_id=task_id)
        return {"__interrupted__": interrupt_payload, **(result.get("final") or {})}

    final = result.get("final") or {}
    await mark_status(task_id, "done")
    return final


async def resume_task(task_id: str, decision: dict, decided_by: str = "") -> dict:
    """人工确认后恢复图执行。decision = {"action": "approve|reject", "comment": "..."}"""
    from langgraph.types import Command

    from backend.agents.review.persist import save_hitl

    async with AsyncSessionLocal() as session:
        row = (await session.execute(
            text("SELECT status FROM review_tasks WHERE id = :id"), {"id": task_id}
        )).fetchone()
    if not row:
        raise InvalidInput(f"任务不存在: {task_id}")
    if row.status != "waiting_human":
        raise InvalidInput(f"任务当前状态为 {row.status}，不可恢复（需 waiting_human）")

    await save_hitl(task_id, decision.get("action", "reject"), decision, decided_by)
    config = {"configurable": {"thread_id": task_id}, "recursion_limit": 40}
    result = await get_graph().ainvoke(Command(resume=decision), config=config)
    final = result.get("final") or {}
    if result.get("__interrupt__"):
        await mark_status(task_id, "waiting_human")
        return {"__interrupted__": result["__interrupt__"][0].value, **final}
    await mark_status(task_id, "done")
    return final


async def get_task(task_id: str, tenant_id: str | None = None) -> dict | None:
    sql = """
        SELECT t.*, r.full_name AS repo_full_name
        FROM review_tasks t LEFT JOIN repos r ON r.id = t.repo_id
        WHERE t.id = :id
    """
    async with AsyncSessionLocal() as session:
        row = (await session.execute(text(sql), {"id": task_id})).mappings().fetchone()
        if not row:
            return None
        if tenant_id and row["tenant_id"] != tenant_id:
            return None
        findings = (await session.execute(
            text("""SELECT dimension, severity, file_path, line_start, line_end, rule_id,
                           evidence, message, suggestion, confidence, source
                    FROM findings WHERE task_id = :id
                    ORDER BY CASE severity WHEN 'block' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END,
                             file_path, line_start"""),
            {"id": task_id},
        )).mappings().all()
    return {**dict(row), "findings": [dict(f) for f in findings]}


async def list_tasks(tenant_id: str, limit: int = 20, status: str | None = None) -> list[dict]:
    sql = """
        SELECT id, kind, status, source, degraded, cost_usd, steps, created_at, finished_at,
               trigger_payload->'repo'->>'full_name' AS repo_full_name,
               trigger_payload->'pr'->>'number' AS pr_number
        FROM review_tasks WHERE tenant_id = :t
    """
    params: dict = {"t": tenant_id, "lim": limit}
    if status:
        sql += " AND status = :status"
        params["status"] = status
    sql += " ORDER BY created_at DESC LIMIT :lim"
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(text(sql), params)).mappings().all()
    return [dict(r) for r in rows]
