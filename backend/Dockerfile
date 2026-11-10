# Build from the repository root:  docker build -f backend/Dockerfile -t katomaran-api .
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv UV_LINK_MODE=copy
RUN pip install --no-cache-dir uv
WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/ ./
COPY config/ /app/config/
ENV PATH="/opt/venv/bin:$PATH" KATOMARAN_ROOT=/app
RUN useradd --create-home app && mkdir -p /app/logs /app/storage && chown -R app /app
USER app
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=2).status==200 else 1)"
# Migrations are an explicit release step: `alembic upgrade head` (see docs/deployment.md).
# uvicorn handles SIGTERM with graceful shutdown; $PORT is honoured for PaaS platforms (Render).
CMD ["sh", "-c", "uvicorn app.main:app_factory --factory --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
