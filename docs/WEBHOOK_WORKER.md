# GitHub webhook receipt worker

The HTTP endpoint verifies each GitHub signature, commits the JSONB delivery receipt,
and returns `202`. The worker claims pending receipts and projects installation
and pull request events after the HTTP response. Expired claims and failed dispatches are retried.

Set `DATABASE_URL`, `RABBITMQ_URL`, `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`,
and `GITHUB_APP_BOT_LOGIN` (the exact App bot login, such as `example[bot]`),
then run:

```bash
uv run alembic upgrade head
uv run python -m app.webhook_worker
```

For local Docker Compose, set `GITHUB_WEBHOOK_SECRET`, `GITHUB_APP_ID`,
`GITHUB_APP_PRIVATE_KEY`, and `GITHUB_APP_BOT_LOGIN` in `.env`, apply the migration,
and start the `webhooks` profile (the compose file passes `RABBITMQ_URL` itself). A worker started outside Compose needs the
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
current labels, so a closed PR keeps its label state; a new Run needs an open PR, since CI
eligibility and the no-CI sweep consider only open PRs (`docs/PIPELINE_SPEC.md` §8.2);
`edited` updates metadata only. The issue timeline is never fetched:
`review_requested` and `review_request_removed` are dropped as irrelevant. A failed dispatch (for
example a GitHub error or the 240 s dispatch timeout) releases the receipt for a retry after
30 s; the third failure marks it failed and it is no longer replayed. It also projects
installation events. A delivery for an unknown installation or repository is deferred, see
"Deferred deliveries" below.

Runs (T1, T6). Label, `synchronize`, `reopened`, `check_suite`, `workflow_run` and
`status` deliveries go through `TriggerFromDelivery` and `try_enqueue`: a Run is inserted with
its outbox mark in one transaction and its `review.run/v1` pointer is published to RabbitMQ
after commit with publisher confirms. The broker connection opens on the first Run; a
publication that fails stays in the outbox and the review worker's leader loop replays it.
Cancelling an attempted Run (new head, closed PR) publishes the check-run close signal with
the same delivery. Missing database, broker or GitHub App configuration stops startup before
any receipt is claimed.

Installation events. GitHub sends each repository of `installation.created`,
`installation_repositories.added` and the removal events as `id`, `node_id`, `name`,
`full_name` and `private` only. The parser requires `id` and `full_name` and ignores the
rest; `default_branch` and `html_url` are used only when both are present. When either is
missing, the worker reads both with `GET /repos/{full_name}` using the installation token
before it fetches the tree, outside any database transaction; a response without a default
branch or web URL is an error, never an empty value. A failed read follows the
failed-dispatch path above (retry after 30 s, three attempts in total, then
`projection_failed_at`). That is a residual risk: a receipt that failed for good is not
replayed, and `wake_receipts` does not clear that mark. The event is handled all or
nothing: one repository that keeps failing (a 404 after a rename or removal, for example)
keeps every other repository of the same event from being stored. `deleted` and `removed`
make no GitHub request. An installation payload that fails validation is logged at WARNING
(delivery, event, action, installation id, the total error count and the first ten failing
field names and messages, never their values) and its receipt is marked projected, so it is
not replayed.

Deferred deliveries. A delivery the dispatcher cannot handle yet (unknown installation or
repository, or an event without a handler) is retried after 5 minutes, at most three
attempts in total, like a failed dispatch. After the third it is deferred for good
(`projection_deferred_at`) and no longer retried. Linking the installation
(`wake_receipts`) clears that mark and gives the deliveries of the installation a fresh
attempt budget. Each sweep logs how many deliveries it handled and deferred, and every
final deferral is logged with its reason.

Retention. Finished receipts (projected, failed or deferred for good) are deleted 30 days
after they finished; the worker runs the purge once an hour.
