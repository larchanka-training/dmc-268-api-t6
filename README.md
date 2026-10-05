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

Run locally (`uv run` does not read `.env` by itself; outside Compose change the `postgres` host
in `DATABASE_URL` to `localhost`):

```bash
uv run --env-file .env uvicorn app.main:app --reload
```

## Seed review prompts

Before deploying an application version that uses AI review, load the versioned
prompt artifacts into PostgreSQL from the repository checkout:

```bash
uv run python -m app.bootstrap.seed_prompts
```

The command requires `DATABASE_URL`. It is safe to run repeatedly; an existing
prompt version whose file content has changed causes the command to fail.

## Webhook, webhook-worker and local auth

### Local auth keys

Sign-in issues an RS256 access JWT signed with `AUTH_JWT_PRIVATE_KEY` and verified with
`AUTH_JWT_PUBLIC_KEY`. Generate a local pair once:

```bash
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out jwt.pem
openssl rsa -in jwt.pem -pubout -out jwt.pub
```

Copy both PEMs into `.env` as real multi-line values in single quotes (the code does not
convert `\n` escapes), then delete `jwt.pem` and `jwt.pub`. Docker Compose and
`uv run --env-file .env` both read this form:

```dotenv
AUTH_JWT_PRIVATE_KEY='-----BEGIN PRIVATE KEY-----
replace-with-the-jwt.pem-body
-----END PRIVATE KEY-----'
AUTH_JWT_PUBLIC_KEY='-----BEGIN PUBLIC KEY-----
replace-with-the-jwt.pub-body
-----END PUBLIC KEY-----'
```

`AUTH_JWT_ISSUER` and `AUTH_JWT_AUDIENCE` default to `dmc-268-api` and `dmc-268-ui`
(`docker-compose.yml`). GitHub sign-in also needs `GITHUB_CLIENT_ID` and `GITHUB_CLIENT_SECRET`
of a dev GitHub App. Without the client ID, client secret, private key, issuer or audience
sign-in answers `503`; without `AUTH_JWT_PUBLIC_KEY` every authenticated endpoint does. Staging values and key rotation:
[docs/SECRETS.md](docs/SECRETS.md).

### Webhook

`POST /webhooks/github` checks the `X-Hub-Signature-256` HMAC with `GITHUB_WEBHOOK_SECRET` from
`.env` (empty → `503`, bad signature → `401`), stores the delivery in `webhook_events` and answers
`202` with `{"status": "pending"}`, or `"duplicate"` for a repeated `X-GitHub-Delivery`. Nothing
else happens in the request: webhook-worker projects the delivery later.

`docker compose up` does not run migrations (the image only starts uvicorn), and `/healthcheck`
answers `200` without touching the schema. Apply them, then smoke-test the endpoint:

```bash
docker compose up -d postgres
docker compose run --rm backend alembic upgrade head
docker compose up -d backend
uv run --env-file .env python scripts/webhook_smoke.py
```

The script signs test deliveries with `GITHUB_WEBHOOK_SECRET` from the environment and checks
`202 pending` → `202 duplicate` → `401` for a bad signature against `--url` (default
`http://localhost:8000/webhooks/github`); `--count N` also prints p50/p95 latency. CI runs the
same script against the built image (job `Webhook container smoke`). Stop webhook-worker during
the smoke (`docker compose stop webhook-worker`; it runs only with `--profile webhooks`) or expect
failed projections: the test deliveries name installation 17 and repository 101, so the worker's
GitHub calls fail and it marks each delivery failed after three tries.

### webhook-worker

webhook-worker replays stored deliveries every 30 s and projects installation and pull request
events. It runs in the `webhooks` profile and needs `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`
(multi-line PEM, quoted as above) and `GITHUB_APP_BOT_LOGIN` in `.env`, plus optional
`GITHUB_API_URL`:

```bash
docker compose --profile webhooks up -d webhook-worker
```

Details: [docs/WEBHOOK_WORKER.md](docs/WEBHOOK_WORKER.md).

## LLM gateway

The review worker (`python -m app.worker`) builds the gateway once per process from `LLM_*`
and binds the models to each claimed attempt. Without `LLM_MODEL` it starts with a warning
and every run fails with a message that names the missing configuration.

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

A failure raises `ReviewCaseFailed` — an `LlmCallFailed` with `error_code` (`llm_timeout`,
`llm_rate_limited`, `llm_unavailable`, `llm_invalid_output`, `llm_context_overflow`,
`budget_exceeded`, `deadline_exceeded`), `usage` and `trace` (every call with its error).
Usage and the trace live in memory; `transport=` accepts a fake.

Manual live run (needs `LLM_MODEL` and `LLM_API_KEYS`; not part of the required CI) — prints
provider, model, every call (with its error), tokens, cost and latency as JSON; exit 1 when the
gateway failed, 2 on a configuration error. `uv run` does not read `.env` by itself:

```bash
uv run --env-file .env python -m app.bootstrap.llm_gateway review/examples/sample.diff
```

On EUrouter the same run is the manual workflow `LLM live run` (`.github/workflows/llm-live-run.yml`,
Actions → Run workflow): it uses the organization secret `AI_DMC268_T6` and writes a summary
table per run.

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
| Webhook `POST /webhooks/github` | `https://staging-api.dmc268-t6.axyi.ru/webhooks/github` — обслуживает API; [staging-webhook.dmc268-t6.axyi.ru](https://staging-webhook.dmc268-t6.axyi.ru) зарезервирован под отдельный сервис и отвечает `502` | [webhook.dmc268-t6.axyi.ru](https://webhook.dmc268-t6.axyi.ru) — зарезервирован |
| Web UI ([dmc-268-ui-t6](https://github.com/larchanka-training/dmc-268-ui-t6)) | [staging-ui.dmc268-t6.axyi.ru](https://staging-ui.dmc268-t6.axyi.ru) | [ui.dmc268-t6.axyi.ru](https://ui.dmc268-t6.axyi.ru) |

`https://dmc268-t6.axyi.ru` — редирект на prod UI.

## Docs

| Документ | Содержание |
|---|---|
| [docs/INFRASTRUCTURE.md](docs/INFRASTRUCTURE.md) | Курсовой VPS и DNS, архитектура Hetzner, Terraform (`api-staging` + `ui-staging`), удаление стенда |
| [docs/CICD.md](docs/CICD.md) | Пайплайн, deployment, rollback, цели выката и edge-прокси |
| [docs/SECRETS.md](docs/SECRETS.md) | Перечень secrets, доставка на staging, git и логи |
| [docs/WEBHOOK_WORKER.md](docs/WEBHOOK_WORKER.md) | webhook-worker: запуск, проекция событий PR и лейбла `ai-review`, повторы квитанций |
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
