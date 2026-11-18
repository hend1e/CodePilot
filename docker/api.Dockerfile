# 多阶段构建：只把依赖装进 runtime 层，镜像更小、构建缓存更稳
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN python -m venv /opt/venv && /opt/venv/bin/pip install -r requirements.txt

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" TZ=Asia/Shanghai

RUN apt-get update && apt-get install -y --no-install-recommends curl docker.io \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 10001 appuser

COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY --chown=appuser:appuser backend ./backend
COPY --chown=appuser:appuser scripts ./scripts
# 前端构建产物（改前端后先在 frontend/ 里 npm run build）
COPY --chown=appuser:appuser frontend/dist ./frontend/dist
COPY --chown=appuser:appuser pyproject.toml ./

USER appuser
EXPOSE 8000
HEALTHCHECK --interval=20s --timeout=5s --retries=5 \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
