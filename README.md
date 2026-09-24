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

## Environments

Staging выкатывается автоматически при каждом push в `main` ([docs/CICD.md](docs/CICD.md)). Prod-адреса зарезервированы на edge-прокси и отвечают `502`, пока prod не задеплоен.

| Сервис | Staging | Prod |
|---|---|---|
| API | [staging-api.dmc268-t6.axyi.ru](https://staging-api.dmc268-t6.axyi.ru/healthcheck) · [Swagger](https://staging-api.dmc268-t6.axyi.ru/docs) | [api.dmc268-t6.axyi.ru](https://api.dmc268-t6.axyi.ru) |
| Webhook (будущий сервис за gateway) | [staging-webhook.dmc268-t6.axyi.ru](https://staging-webhook.dmc268-t6.axyi.ru) | [webhook.dmc268-t6.axyi.ru](https://webhook.dmc268-t6.axyi.ru) |
| Web UI ([dmc-268-ui-t6](https://github.com/larchanka-training/dmc-268-ui-t6)) | [staging-ui.dmc268-t6.axyi.ru](https://staging-ui.dmc268-t6.axyi.ru) | [ui.dmc268-t6.axyi.ru](https://ui.dmc268-t6.axyi.ru) |

`https://dmc268-t6.axyi.ru` — редирект на prod UI.

## Docs

| Документ | Содержание |
|---|---|
| [docs/INFRASTRUCTURE.md](docs/INFRASTRUCTURE.md) | Курсовой VPS и DNS, архитектура Hetzner, Terraform (`api-staging` + `ui-staging`), удаление стенда |
| [docs/CICD.md](docs/CICD.md) | Пайплайн, deployment, rollback, цели выката и edge-прокси |
| [docs/SECRETS.md](docs/SECRETS.md) | Перечень secrets, доставка на staging, git и логи |
| [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) | Процесс работы: роли, путь задачи, треды ревью, вердикты, споры, мерж |

## Pre-commit

Install hooks:

```bash
uv run pre-commit install --config .pre-commit-config.yaml
```

Run manually:

```bash
uv run pre-commit run --config .pre-commit-config.yaml --all-files
```
