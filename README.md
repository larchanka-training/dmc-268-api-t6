# DMC-268 API (Team 6)

Monorepo AI Code Reviewer: independently packaged `portal-api`, `auth-api`, `webhook-api`,
`worker` and `publisher` services, with PostgreSQL, RabbitMQ and Redis.

## Setup

Requirements: [uv](https://docs.astral.sh/uv/) and Docker.

```bash
uv sync --all-packages
```

## Run

Docker:

```bash
make up
```

Portal API: `http://localhost:8000/healthcheck`

Webhook API: `http://localhost:8001/healthcheck`

Auth API: `http://localhost:8002/healthcheck`

Stop:

```bash
make down
docker compose down -v  # also remove PostgreSQL data
```

Run locally:

```bash
cd services/portal-api && uv run --package portal-api uvicorn app.main:app --reload
```

Apply the database schema before starting services that need it:

```bash
make migrate
```

## Tests

```bash
make test
```

## Lint & format

```bash
make lint
uv run ruff format --check .
make format
```

## Type check

```bash
make typecheck
```

## Pre-commit

Install hooks:

```bash
uv run pre-commit install --config .pre-commit-config.yaml
```

Run manually:

```bash
uv run pre-commit run --config .pre-commit-config.yaml --all-files
```

## Structure

See [the backend architecture](docs/BACKEND_ARCHITECTURE.md). The current PostgreSQL
schema lives in `packages/database`; the shared Alembic history is in the root
`migrations/` directory. Neither is copied into workers.
