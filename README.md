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

## Seed review prompts

Before deploying an application version that uses AI review, load the versioned
prompt artifacts into PostgreSQL from the repository checkout:

```bash
uv run python -m app.bootstrap.seed_prompts
```

The command requires `DATABASE_URL`. It is safe to run repeatedly; an existing
prompt version whose file content has changed causes the command to fail.

## LLM gateway

The review model is called through the LLM gateway (#33): one OpenAI-compatible adapter for
EUrouter and self-hosted servers, key rotation, retries, a fallback model, the run cost limit
and the attempt deadline of `docs/PIPELINE_SPEC.md` §3–§6. Configuration is `LLM_*` in
`.env` ([docs/SECRETS.md](docs/SECRETS.md)).

**Case → `ReviewOutput` + usage, without a database** — the entry point of eval #30 (live mode):

```python
from app.bootstrap.llm_gateway import ReviewCase, review_case
from app.modules.reviews.infrastructure.llm.settings import LlmSettings

result = await review_case(
    ReviewCase(diff=unified_diff, system=prompt_text, pr_meta=pr_meta),
    LlmSettings.from_env(os.environ),
)
result.output   # ReviewOutput (typed, schema- and Pydantic-checked)
result.usage    # one LlmUsage per provider call: provider, actual model, tokens, cost_usd
result.calls    # the llm.call records: kind (primary/retry/repair/fallback), model, duration
```

A failure raises `LlmCallFailed` with `error_code` (`llm_timeout`, `llm_rate_limited`,
`llm_unavailable`, `llm_invalid_output`, `llm_context_overflow`, `budget_exceeded`,
`deadline_exceeded`). Usage and the trace live in memory; `transport=` accepts a fake.

Manual live run (needs `LLM_MODEL` and `LLM_API_KEYS`; not part of CI) — prints provider,
model, tokens, cost and latency:

```bash
uv run python -m app.bootstrap.llm_gateway review/examples/sample.diff
```

For a self-hosted model in dev (LM Studio, Ollama, vLLM): `LLM_BASE_URL=http://localhost:1234/v1`,
`LLM_MODEL=<model>`, `LLM_CONTEXT_WINDOW=<tokens>` and, if the server has no strict JSON Schema
mode, `LLM_STRUCTURED_OUTPUT=prompt_json` with `LLM_ALLOW_PROMPT_JSON=1`.

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
