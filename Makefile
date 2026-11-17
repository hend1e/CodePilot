.PHONY: help venv install up down logs ps seed api worker test lint ingest demo sandbox-image fmt

PY := .venv/bin/python
PIP := .venv/bin/pip

help:
	@echo "make venv          创建 Python 3.11 虚拟环境"
	@echo "make install       安装依赖"
	@echo "make up            启动基础设施（PG / Redis / Milvus / Attu）"
	@echo "make sandbox-image 构建测试沙箱镜像"
	@echo "make seed          初始化演示账号"
	@echo "make api           本地启动 API（8000）"
	@echo "make worker        本地启动 worker"
	@echo "make ingest REPO=local/demo PATH=./workspace/demo  建代码索引"
	@echo "make demo          跑一次端到端演示审查"
	@echo "make test          跑单测"
	@echo "make down          停止并清理容器"

venv:
	python3.11 -m venv .venv || python3 -m venv .venv
	$(PIP) install -U pip

install:
	$(PIP) install -r requirements.txt

up:
	docker compose up -d
	@echo "等待 Milvus 就绪…（首次约 30-60s）"

up-all:
	DOCKER_GID=$$(stat -c '%g' /var/run/docker.sock) \
	  docker compose -f docker-compose.yml -f docker-compose.app.yml up -d --build

down:
	docker compose -f docker-compose.yml -f docker-compose.app.yml down

logs:
	docker compose logs -f --tail=100

ps:
	docker compose ps

sandbox-image:
	docker build -f docker/sandbox/Dockerfile -t codepilot/sandbox:py311 docker/sandbox

seed:
	$(PY) -m scripts.seed_data

api:
	$(PY) -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload

worker:
	$(PY) -m backend.infra.worker

ingest:
	$(PY) -m scripts.ingest_repo --repo $(REPO) --path $(PATH)

demo:
	$(PY) -m scripts.demo_review

test:
	$(PY) -m pytest -q

lint:
	.venv/bin/ruff check backend tests || true

fmt:
	.venv/bin/ruff format backend tests || true
