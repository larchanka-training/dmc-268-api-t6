# DMC-268 API (Team 6)

FastAPI backend with PostgreSQL.

## Setup

Requirements: [uv](https://docs.astral.sh/uv/) and Docker.

```bash
cp .env.example .env
uv sync
```

## Run

Docker:

```bash
docker compose up
```

Backend: `http://localhost:8000`
Healthcheck: `http://localhost:8000/healthcheck`

Stop:

```bash
docker compose down
docker compose down -v  # also remove PostgreSQL data
```

Run locally:

```bash
uv run uvicorn app.main:app --reload
```

## Tests

```bash
uv run pytest
```

## Lint & format

```bash
uv run ruff check .
uv run ruff format --check .
uv run ruff format .
```

## Type check

```bash
uv run mypy .
```

## CI/CD

GitHub Actions собирает Docker-образ, сканирует его, пушит в GHCR и выкатывает FastAPI + PostgreSQL на Hetzner staging. После выката проверяется `GET /healthcheck`; при ошибке выполняется автоматический rollback. Дизайн, jobs и секреты: [docs/CICD.md](docs/CICD.md).

## Pre-commit

Install hooks:

```bash
uv run pre-commit install --config .pre-commit-config.yaml
```

Run manually:

```bash
uv run pre-commit run --config .pre-commit-config.yaml --all-files
```
