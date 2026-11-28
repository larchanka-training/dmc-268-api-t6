# syntax=docker/dockerfile:1.7
# Review bot of #601 (docs/INFRASTRUCTURE.md, section "Review bot"). Built on the host by
# review_bot/deploy.sh from this folder only; the application image does not include it.

FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.11 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/srv/.venv \
    PATH="/srv/.venv/bin:$PATH"

WORKDIR /srv

RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --uid 1000 --no-create-home --home-dir /srv bot \
    && mkdir -p /data \
    && chown bot /data

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY src ./src
RUN uv sync --locked --no-dev --no-editable \
    && rm -f /bin/uv /bin/uvx \
    && rm -rf /root/.cache/uv /srv/src \
    && pip uninstall -y pip

USER bot
VOLUME ["/data"]

# Alive while the poll loop runs: main touches the heartbeat file after every poll.
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD python -c "import os, sys, time; path = os.environ.get('REVIEW_BOT_HEARTBEAT_PATH', '/tmp/review-bot.alive'); sys.exit(time.time() - os.path.getmtime(path) > 300)"

CMD ["review-bot"]
