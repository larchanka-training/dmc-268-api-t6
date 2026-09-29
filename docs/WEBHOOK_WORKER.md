# GitHub webhook receipt worker

The HTTP endpoint verifies each GitHub signature, commits the JSONB delivery receipt,
and returns `202`. The worker claims pending receipts and projects installation
and pull request events after the HTTP response. Expired claims and failed dispatches are retried.

Set `DATABASE_URL`, `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`,
and `GITHUB_APP_BOT_LOGIN` (the exact App bot login, such as `example[bot]`),
then run:

```bash
uv run alembic upgrade head
uv run python -m app.webhook_worker
```

For local Docker Compose, set `GITHUB_WEBHOOK_SECRET`, `GITHUB_APP_ID`,
`GITHUB_APP_PRIVATE_KEY`, and `GITHUB_APP_BOT_LOGIN` in `.env`, apply the migration,
and start the `webhooks` profile. A worker started outside Compose needs the
same database and GitHub App configuration.

```bash
docker compose up -d postgres
docker compose run --rm backend alembic upgrade head
docker compose --profile webhooks up --build
```

The worker serializes each PR's event projection with a PostgreSQL session advisory lock on an
autocommit connection. Synchronize, close, and reopen events fetch the current GitHub PR while
holding that lock. It stores the result in a separate short transaction, so the GitHub call never
spans a database transaction. Reviewer and close/reopen deliveries fetch the paginated issue
timeline under the same lock to resolve the latest explicit bot reviewer request, human removal,
and lifecycle barrier, including events in one timestamp second. GitHub timeline failures leave
the receipt pending for retry. It also projects installation events. Unknown repositories remain
retryable until onboarding.

This worker only projects durable GitHub receipts. It neither connects to a broker
nor enqueues `review.run/v1` messages. The application-level Run publisher port and
its fake-driven behavior tests remain in place; the concrete AMQP publisher, queue
topology, and consumer integration are deferred to #34. Missing database or GitHub App
configuration stops startup before any receipt is claimed. PostgreSQL integration
verification still requires a configured test environment.
