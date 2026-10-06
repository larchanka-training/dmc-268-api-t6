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
receipt failed. After the third deferral it is deferred (`projection_deferred_at`) and the
sweep no longer selects it until something revives it with a fresh attempt budget. Once an
hour, in the same tick as the purge, the worker revives the installation-event receipts
(`installation` and `installation_repositories`) of linked installations that have been
deferred for at least 45 minutes and were received within the last 7 days
(`ReviveDeferredInstallationDeliveries`). The revival covers installation events only:
pull-request, label and CI deliveries deferred as an unknown repository during the same
outage are still revived only by linking (`wake_receipts`, below). A receipt is revived at
the first hourly tick that comes at least 45 minutes after its deferral. A revived receipt
spends its three attempts within about ten minutes, so that is usually the next tick and the
receipt is retried about once an hour; when it was deferred less than 45 minutes before a
tick (its first deferral, a cycle started by a login, a worker restart that shifts the tick,
or a slow sweep), it is the tick after that, up to about two hours later. An outage of the
details read therefore heals on its own, with no login, about an hour after it ends, unless
the last attempt of a cycle fails the dispatch instead (a failing tree request, the 240 s
dispatch timeout or a database error; a failed label request is only logged): the receipt is
then marked failed (`projection_failed_at`), and neither the revival nor a login brings it
back. The 45-minute delay and the 7-day window are the parameters the tech lead approved
(api#71): a GitHub outage longer than a week is not a transient failure, and a `GET /repos`
404 for a week means the repository is gone. Linking the installation (`wake_receipts`,
which runs at every GitHub login of a user whose token lists the installation) clears the
mark of every deferred delivery of the installation, whatever its event or age. Residual
risks: every attempt runs the event again from its first repository, and each repository
before the unreadable one costs a details read, a tree read and a label request, so an event
whose k-th repository stays unreadable costs about 3 × (k - 1) + 1 GitHub requests per
attempt, three attempts an hour for 7 days after it was received, plus three attempts per
login; per-repository isolation, which would stop this, is the follow-up api#73. A revived
`installation_repositories.added` is applied hours or days late without an ordering check
against a later `removed` event of the same repository, so a public repository can come back
enabled after it was removed from the installation (a private one answers 404 and stays
deferred); the 5-minute retries and `wake_receipts` already had this gap. Outside the 7-day
window only a login or a delivery with a new GUID revives a receipt, and one nobody revives
is deleted 30 days after its last `projection_deferred_at` (see Retention). Its repositories
are then stored only by a delivery with a new GUID, for example after removing and re-adding
the repository in the installation settings; a redelivery of the same GUID is ignored as a
duplicate. Each sweep logs how many deliveries it handled and deferred, every final deferral
is logged with its reason, and a revival that resets receipts logs how many.

Retention. Finished receipts (projected, failed, or deferred and not revived since) are
deleted 30 days after they finished; the worker runs the purge once an hour.
