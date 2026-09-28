# GitHub webhook receipt worker

The HTTP endpoint verifies each GitHub signature, commits the JSONB delivery receipt,
and returns `202`. The worker claims pending receipts and projects installation
and pull request events after the HTTP response. Expired claims and failed dispatches are retried.

Set `DATABASE_URL`, `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`,
`GITHUB_APP_BOT_LOGIN` (the exact App bot login, such as `example[bot]`), and
`RABBITMQ_URL`, then run:

```bash
uv run alembic upgrade head
uv run python -m app.webhook_worker
```

For local Docker Compose, set `GITHUB_WEBHOOK_SECRET`, `GITHUB_APP_ID`,
`GITHUB_APP_PRIVATE_KEY`, and `GITHUB_APP_BOT_LOGIN` in `.env`, apply the migration,
and start the override. It starts a RabbitMQ service and defaults `RABBITMQ_URL` to
that service; set the URL explicitly for an external broker.

```bash
docker compose -f docker-compose.yml -f docker-compose.webhooks.yml up -d postgres
docker compose -f docker-compose.yml -f docker-compose.webhooks.yml run --rm backend alembic upgrade head
docker compose -f docker-compose.yml -f docker-compose.webhooks.yml --profile webhooks up --build
```

The worker serializes each PR's event projection with a PostgreSQL session advisory lock on an
autocommit connection. Synchronize, close, and reopen events fetch the current GitHub PR while
holding that lock. It stores the result in a separate short transaction, so the GitHub call never
spans a database transaction. Reviewer and close/reopen deliveries fetch the paginated issue
timeline under the same lock to resolve the latest explicit bot reviewer request, human removal,
and lifecycle barrier, including events in one timestamp second. GitHub timeline failures leave
the receipt pending for retry. It also projects installation events. Unknown repositories remain
retryable until onboarding.

The worker connects to RabbitMQ before claiming receipts. It declares durable
`review.run.fast` and `review.run.deep` queues on the `reviews` direct exchange,
publishes persistent `review.run/v1` messages with mandatory routing and broker
confirms, and treats a returned or unconfirmed message as a failure. The Run and
`run_updated` notification commit before publication; a failed confirm leaves a queued
Run with `message_published_at` unset. Every worker sweep retries these publications,
skipping superseded, closed, unassigned, or disabled PRs. Missing broker or App
configuration stops startup before any receipt is claimed. PostgreSQL and live RabbitMQ
integration verification still require a configured test environment.
