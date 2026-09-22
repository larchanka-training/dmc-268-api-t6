FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.11 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/srv/.venv/bin:$PATH"

WORKDIR /srv

COPY . .
RUN uv sync --locked --no-dev --package database-migrator

CMD ["/srv/.venv/bin/alembic", "-c", "/srv/alembic.ini", "upgrade", "head"]
