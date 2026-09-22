FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.11 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /srv

COPY . .
RUN uv sync --locked --no-dev --package auth-api

WORKDIR /srv/services/auth-api

EXPOSE 8002

CMD ["/srv/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8002"]
