# backend/api/v1/health.py
# 健康检查与指标：/healthz 存活、/readyz 依赖就绪、/metrics Prometheus 文本。

from fastapi import APIRouter, Response
from sqlalchemy import text

from backend.core.knowledge_base import MilvusStore
from backend.config import get_settings
from backend.db.session import AsyncSessionLocal
from backend.core.queue import get_queue
from backend.core import metrics

router = APIRouter()
settings = get_settings()


@router.get("/healthz")
async def healthz():
    return {"status": "ok", "env": settings.app_env, "llm": settings.llm_provider,
            "llm_enabled": settings.llm_enabled,
            "models": settings.llm_model_map}


@router.get("/readyz")
async def readyz():
    checks: dict[str, bool] = {}

    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        checks["postgres"] = True
    except Exception:       # noqa: BLE001
        checks["postgres"] = False

    try:
        checks["redis"] = await get_queue().ping()
    except Exception:       # noqa: BLE001
        checks["redis"] = False

    checks["milvus"] = await MilvusStore().healthy()

    critical = ["postgres"]
    ready = all(checks[k] for k in critical)
    return {"ready": ready, "checks": checks,
            "degraded": [k for k, v in checks.items() if not v and k not in critical]}


@router.get("/metrics")
async def prometheus_metrics():
    return Response(content=metrics.render(), media_type="text/plain; version=0.0.4")
