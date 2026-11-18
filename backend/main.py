# backend/main.py
# FastAPI 应用入口：日志 → 配置自检 → 数据库补丁 → Checkpointer → 路由。
# 开发环境可内嵌 worker（INLINE_WORKER=true），生产用独立 worker 容器。

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.api.v1 import auth, chat, health, rag_api, reviews, webhooks
from backend.config import get_settings
from backend.core.exceptions import CodePilotError, InvalidInput
from backend.core.logger import configure_logging, get_logger
from backend.db.migrations import run_migrations
from backend.db.session import close_checkpointer, init_checkpointer
from backend.infra.queue import get_queue, init_queue

settings = get_settings()
logger = get_logger(__name__)

_inline_worker_task: asyncio.Task | None = None


async def _start_inline_worker() -> None:
    from backend.infra.worker import Worker

    worker = Worker(concurrency=2)
    global _inline_worker_task
    _inline_worker_task = asyncio.create_task(worker.run_forever())
    logger.info("worker.inline_started")


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    problems = settings.validate_production()
    if problems:
        for p in problems:
            logger.error("config.problem", problem=p)
        if settings.app_env == "prod":
            raise RuntimeError(f"生产配置不完整: {problems}")

    logger.info("app.starting", env=settings.app_env, llm=settings.llm_provider,
                sandbox=settings.sandbox_mode, embedding=settings.embedding_backend)
    try:
        await run_migrations()
    except Exception as e:      # noqa: BLE001 —— 数据库没起来不阻止进程启动（/readyz 会暴露）
        logger.warning("db.migrations_skipped", error=str(e)[:300])

    await init_queue()
    await init_checkpointer()
    if settings.inline_worker:
        await _start_inline_worker()

    yield

    global _inline_worker_task
    if _inline_worker_task:
        _inline_worker_task.cancel()
        try:
            await _inline_worker_task
        except asyncio.CancelledError:
            pass
    await get_queue().close()
    await close_checkpointer()
    logger.info("app.stopped")


app = FastAPI(
    title="CodePilot API",
    description="多 Agent 研发协作与代码审查平台",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.app_env != "prod" else [],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router, tags=["health"])
app.include_router(auth.router, prefix="/api/v1/auth", tags=["auth"])
app.include_router(reviews.router, prefix="/api/v1/reviews", tags=["reviews"])
app.include_router(webhooks.router, prefix="/api/v1/webhooks", tags=["webhooks"])
app.include_router(rag_api.router, prefix="/api/v1/rag", tags=["rag"])
app.include_router(chat.router, prefix="/api/v1", tags=["chat"])


@app.exception_handler(CodePilotError)
async def handle_domain_error(request: Request, exc: CodePilotError):
    status_code = 400 if isinstance(exc, InvalidInput) else 500
    logger.warning("api.domain_error", path=request.url.path, code=exc.code,
                   message=exc.message[:300])
    return JSONResponse(status_code=status_code, content={"error": exc.as_dict()})


@app.exception_handler(Exception)
async def handle_unexpected(request: Request, exc: Exception):
    logger.error("api.unhandled", path=request.url.path, error=str(exc), exc_info=True)
    return JSONResponse(status_code=500, content={"error": {"code": "internal_error",
                                                           "message": "服务器内部错误"}})


# ── 前端 SPA（Vite 构建产物，同源托管，无需额外 nginx 容器）──
# 改前端后：cd frontend && npm run build
_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"

if _DIST.is_dir():
    if (_DIST / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    async def spa_index():
        return FileResponse(_DIST / "index.html")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa_fallback(path: str):
        """SPA 兜底：真实文件直接返回，其余交给前端路由；API 路径保持 404 JSON。"""
        if path.startswith(("api/", "docs", "redoc", "openapi.json",
                            "healthz", "readyz", "metrics")):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        target = (_DIST / path).resolve()
        if _DIST.resolve() in target.parents and target.is_file():
            return FileResponse(target)
        return FileResponse(_DIST / "index.html")
