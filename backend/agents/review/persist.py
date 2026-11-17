# backend/agents/review/persist.py
# 审查结果落库。所有写操作幂等（ON CONFLICT），失败只记日志不阻断图。

from sqlalchemy import text

from backend.agents.review.state import Finding
from backend.core.logger import get_logger
from backend.db.session import AsyncSessionLocal

logger = get_logger(__name__)


async def save_findings(task_id: str, findings: list[Finding]) -> None:
    if not findings:
        return
    sql = text("""
        INSERT INTO findings (task_id, fingerprint, dimension, severity, file_path,
                              line_start, line_end, rule_id, evidence, message, suggestion,
                              confidence, source)
        VALUES (:task_id, :fingerprint, :dimension, :severity, :file_path,
                :line_start, :line_end, :rule_id, :evidence, :message, :suggestion,
                :confidence, :source)
        ON CONFLICT (task_id, fingerprint) DO UPDATE
        SET confidence = GREATEST(findings.confidence, EXCLUDED.confidence),
            message = EXCLUDED.message,
            suggestion = EXCLUDED.suggestion
    """)
    async with AsyncSessionLocal() as session:
        for f in findings:
            await session.execute(sql, {"task_id": task_id, "fingerprint": f.fingerprint(), **f.model_dump()})
        await session.commit()


async def save_final(task_id: str, final: dict, *, degraded: bool, steps: int,
                     prompt_tokens: int = 0, completion_tokens: int = 0, cost_usd: float = 0.0,
                     status: str = "done") -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                UPDATE review_tasks
                SET final = CAST(:final AS JSONB), degraded = :degraded, steps = :steps,
                    prompt_tokens = :pt, completion_tokens = :ct, cost_usd = :cost,
                    status = :status, finished_at = NOW(), updated_at = NOW()
                WHERE id = :task_id
            """),
            {"final": __import__("json").dumps(final, ensure_ascii=False, default=str),
             "degraded": degraded, "steps": steps, "pt": prompt_tokens, "ct": completion_tokens,
             "cost": cost_usd, "status": status, "task_id": task_id},
        )
        await session.commit()


async def mark_status(task_id: str, status: str, error: str | None = None) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                UPDATE review_tasks
                SET status = CAST(:status AS VARCHAR),
                    error_msg = COALESCE(CAST(:error AS TEXT), error_msg),
                    started_at = CASE WHEN CAST(:status AS VARCHAR) = 'running'
                                      THEN COALESCE(started_at, NOW()) ELSE started_at END,
                    finished_at = CASE WHEN CAST(:status AS VARCHAR) IN
                                       ('done','failed','cancelled','superseded')
                                       THEN NOW() ELSE finished_at END,
                    updated_at = NOW()
                WHERE id = CAST(:task_id AS UUID)
            """),
            {"status": status, "error": error[:2000] if error else None, "task_id": task_id},
        )
        await session.commit()


async def save_test_run(task_id: str, patch: str, report: dict, targets: list[str]) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                INSERT INTO test_runs (task_id, patch, target_files, status, passed, failed,
                                       coverage, report, sandbox_image, duration_ms, error)
                VALUES (:task_id, :patch, CAST(:targets AS JSONB), :status, :passed, :failed,
                        :coverage, CAST(:report AS JSONB), :image, :duration, :error)
            """),
            {"task_id": task_id, "patch": patch, "targets": __import__("json").dumps(targets),
             "status": report.get("status", "skipped"), "passed": report.get("passed", 0),
             "failed": report.get("failed", 0), "coverage": report.get("coverage"),
             "report": __import__("json").dumps(report, ensure_ascii=False),
             "image": report.get("image", ""), "duration": report.get("duration_ms", 0),
             "error": report.get("error")},
        )
        await session.commit()


async def save_hitl(task_id: str, action: str, payload: dict, decided_by: str) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                INSERT INTO hitl_decisions (task_id, action, payload, decided_by)
                VALUES (:task_id, :action, CAST(:payload AS JSONB), :by)
            """),
            {"task_id": task_id, "action": action,
             "payload": __import__("json").dumps(payload, ensure_ascii=False), "by": decided_by},
        )
        await session.commit()
