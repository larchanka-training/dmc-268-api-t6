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
installation events. A delivery for an unknown installation or repository, or for an
installation event whose repository details GitHub cannot answer, is deferred instead, see
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
before it fetches the tree, outside any database transaction. A read that GitHub cannot
answer (the token request or `GET /repos` fails with a network error, a timeout, a 404, a
5xx or any other HTTP error status) defers the delivery like an unknown installation, see
"Deferred deliveries" below, and logs a WARNING with the installation id and the HTTP
status or exception class, never the URL or the token. A `GET /repos` answer that is HTTP
200 but is not JSON or lacks the default branch or web URL (never stored as an empty value)
and a malformed token response are not outages: they fail the dispatch (retry after 30 s,
three attempts in total, then `projection_failed_at`, never replayed). The tree request is
not part of this deferral; its failure still fails the dispatch, and so does the 240 s
dispatch timeout, which a large installation event can exceed because its repositories are
read one after another. The event is handled all or nothing: one repository that keeps
failing (a 404 after a rename or removal, for example) keeps every other repository of the
same event from being stored. `deleted` and `removed`
make no GitHub request. An installation payload that fails validation is logged at WARNING
(delivery, event, action, installation id, the total error count and the first ten failing
field names and messages, never their values) and its receipt is marked projected, so it is
not replayed.

Deferred deliveries. A delivery the dispatcher cannot handle yet (unknown installation or
repository, an event without a handler, or an installation event whose repository details
GitHub cannot answer) is retried after 5 minutes, at most three attempts in total, like a
failed dispatch. Deferrals and failed dispatches draw on the same three attempts: two
deferrals followed by one failed dispatch (a failing tree request, for example) mark the
receipt failed. After the third deferral it is deferred for good (`projection_deferred_at`)
and no longer retried. Linking the installation (`wake_receipts`, which runs at every GitHub
login of a user whose token lists the installation) clears that mark and gives the
deliveries of the installation a fresh attempt budget. Residual risk: only that login
revives a deferred receipt, and every login revives it again, so a receipt that keeps
failing (a repository that is gone) costs three more attempts per login; one nobody wakes is
deleted 30 days after `projection_deferred_at` (see Retention). Its repositories are then
stored only by a delivery with a new GUID, for example after removing and re-adding the
repository in the installation settings; a redelivery of the same GUID is ignored as a
duplicate. Each sweep logs how many deliveries it handled and deferred, and every final
deferral is logged with its reason.

Retention. Finished receipts (projected, failed or deferred for good) are deleted 30 days
after they finished; the worker runs the purge once an hour.
