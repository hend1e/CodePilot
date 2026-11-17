# 测试沙箱镜像：只装跑单测需要的依赖，不带项目代码（代码运行时挂载）。
# 为什么单独一个镜像：① 体积小、启动快 ② 与业务镜像隔离，LLM 生成的代码炸不到服务
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1

RUN pip install --no-cache-dir \
        pytest==8.3.4 \
        pytest-cov==6.0.0 \
        pytest-asyncio==0.24.0 \
        pydantic==2.11.4 \
        httpx==0.28.1 \
        requests==2.32.3 \
        sqlalchemy==2.0.36 \
        fastapi==0.117.1

# 非 root + 无特权
RUN useradd -m -u 10001 sandbox
USER sandbox
WORKDIR /workspace

CMD ["python", "-m", "pytest", "-q"]
