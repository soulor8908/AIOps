# AIOps Console Backend — 极简镜像
# 多阶段构建，最终镜像仅含运行时依赖，并以非 root 用户运行（deployment.spec.md§安全）。

FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# 安装构建依赖（asyncpg / pgvector 需要 gcc + pg_config）
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# 依赖装入独立 venv（/opt/venv），便于整目录拷贝到运行时镜像且与 root 解耦。
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml ./
RUN pip install --no-cache-dir \
    "fastapi>=0.115" \
    "uvicorn[standard]>=0.32" \
    "pydantic>=2.9" \
    "pydantic-settings>=2.5" \
    "sqlalchemy>=2.0" \
    "asyncpg>=0.30" \
    "pgvector>=0.3" \
    "httpx>=0.27" \
    "redis>=5.2" \
    "python-jose[cryptography]>=3.3" \
    "passlib[bcrypt]>=1.7" \
    "python-multipart>=0.0.12"

# ---- 运行时镜像 ----
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# 仅运行时需要的共享库
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 从 builder 拷贝独立 venv
COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY . /app

# 非 root 用户运行（UID/GID 1000，与 K8s runAsUser 对齐）
RUN groupadd -g 1000 -r app && useradd -u 1000 -r -g app -s /sbin/nologin app \
    && chown -R app:app /app
USER 1000

EXPOSE 8000

# 健康检查打到 /health 端点（curl 无 Python 启动开销）
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=10s \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
