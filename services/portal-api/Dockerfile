FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.11 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/srv/.venv/bin:$PATH"

WORKDIR /srv

COPY . .
RUN uv sync --locked --no-dev --package portal-api

WORKDIR /srv/services/portal-api

EXPOSE 8000

CMD ["/srv/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
