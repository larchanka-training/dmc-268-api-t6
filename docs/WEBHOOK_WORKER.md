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
autocommit connection and stores the result in a separate short transaction, so the GitHub call
never spans a database transaction. `labeled` / `unlabeled` of the `ai-review` label (events sent
by the App's own bot are ignored) and `synchronize`, `closed`, `reopened`, and `edited` fetch the
current GitHub PR once while holding that lock; `opened` is applied from the payload. Label
events and `synchronize`, `closed`, and `reopened` reconcile `ai_review_labeled` from that PR's
current labels, so a closed PR keeps its label state (`docs/PIPELINE_SPEC.md` §8.2 lists `false`
for `closed`; the effect is the same, since CI eligibility and the no-CI sweep consider only open
PRs for a new Run); `edited` updates metadata only. The issue timeline is never fetched:
`review_requested` and `review_request_removed` are dropped as irrelevant. A failed dispatch (for
example a GitHub error or the 240 s dispatch timeout) releases the receipt for a retry after
30 s; the third failure marks it failed and it is no longer replayed. It also projects
installation events. Unknown repositories remain retryable until onboarding. Until #52 wires Run
creation, a projected `labeled`, `synchronize`, or `reopened` delivery and every CI event
(`status`, completed `check_suite` and `workflow_run`) are deferred and replayed every 5 minutes;
each replay of a PR delivery fetches the current PR again.

This worker only projects durable GitHub receipts. It neither connects to a broker
nor enqueues `review.run/v1` messages. The application-level Run publisher port and
its fake-driven behavior tests remain in place; the concrete AMQP publisher, queue
topology, and consumer integration are deferred to #34. Missing database or GitHub App
configuration stops startup before any receipt is claimed. PostgreSQL integration
verification still requires a configured test environment.
