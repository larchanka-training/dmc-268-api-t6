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
30 s; the third failure marks it failed and it is no longer replayed (if the release itself fails,
the receipt keeps its claim until the 5-minute lease lapses and the attempt counter does not
advance). It also projects
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

Outcome log. The process calls `logging.basicConfig(level=INFO)` and, once a delivery has been
processed and its receipt updated, writes one INFO line to the `webhook-worker` log, so the log
shows why a label produced no Run:

```text
GitHub webhook delivery <delivery_id> event=<event> status=<status> detail=<detail> [retry_at=<time|none>]
```

`status` is the dispatch status (`projected_pr`, `processed_ci`, `onboarded`, `ignored_*`,
`deferred_known_event`, `deferred_repository_details`). `detail` is `-` or the reason. For a
`pull_request` event it starts with `action=<action>`, so `labeled` and `synchronize` can be
told apart (an ignored action, such as `ready_for_review`, shows only that). For a Run trigger
(label, `synchronize`, `reopened`, CI events) the rest is
`pr=<pull request id> head=<first 7 of the head sha>: <status> (<reason>) run=<run id>`, and a
CI event joins one such outcome per open PR on that head with `; ` (and has no action):

| status | reason | meaning |
| --- | --- | --- |
| `enqueued` | | the Run was inserted and published (`run=<id>`) |
| `publication_pending` | | the Run was inserted, the broker publish failed; the review worker's leader loop replays it |
| `ineligible` | the CI gate reason: `ci_blocked` (a foreign check suite or the commit status is not green), `waiting_for_ci` (no CI evidence yet with `wait_for_ci = always`, or still inside the 2-minute `auto` window), `label_not_active`, `stale_head`, `stale_state`, `closed_pr`, `disabled_repository`, `unknown_pr` | the gate decided not to start a Run |
| `unconfigured` | `missing_installation`, `missing_rules` (no active rule version), `missing_prompt` (no active `review.system` prompt and none pinned) | the repository lacks what a Run needs; a new label or CI event does not change that |
| `stale` | `pull_request_gone`, `repository_gone`, `state_changed` (the PR changed between the gate and the locked read) | the decision no longer matches the current PR |
| `duplicate` | `active_run` (a queued, running or publishing Run exists), `head_already_reviewed` (this head already has a webhook Run) | no second Run is created |

Other details: `no open pull request` (a PR event or label whose PR is closed or unknown,
nothing to enqueue), `no open pull request at this head` (a CI event for a head that no open PR
has, for example CI of an old head that finished after a push),
`not an ai-review labeled action`, `label is not ai-review` (a foreign label), and, when the
trigger did not run, the projection result: `projected`, `ignored_stale`, `ignored_unrelated`
(for example a label set by the App's own bot) or `unknown_repository`. The line never carries
the payload, an installation token or the webhook secret.

A deferred delivery's outcome line ends with `retry_at=<time>`, or `retry_at=none` after the
third attempt, and that final deferral is also logged as a WARNING with its reason, once the
receipt is committed. `deferred_repository_details` is an installation event whose repository
details GitHub cannot answer; like the other deferrals it is retried after 5 minutes, at most
three attempts in total. `retry_at=none` means no retry is scheduled: the receipt waits until
something revives it, the hourly revival for installation events or linking the installation
(`wake_receipts`), see "Deferred deliveries" below.

Failure log. When processing a delivery fails, the worker first writes one WARNING line with the
action and a failure category, then the sweep writes its ERROR record
`GitHub webhook projection failed for delivery <delivery_id>` with the traceback. The first line
is the greppable outcome, the second the diagnosis:

```text
GitHub webhook delivery <delivery_id> event=<event|-> action=<action|-> failed stage=<stage> category=<category> error=<ExceptionClass> [outcome=<status> detail=<detail>]
```

For a failed `labeled` delivery the line therefore carries `action=labeled` and the category of
the failure. A `stage=dispatch` line does not prove that no Run exists: the Run may have been
committed and published before the failure (the publish was confirmed and then `mark_published`
failed), and a CI event loops over several pull requests, so a later one can fail after an
earlier one was enqueued. The replayed delivery then reports `duplicate (active_run)`. `error` is
the class name only: an exception message can hold a URL or an identifier, so it is left to the
traceback record, and the line never carries the payload, an installation token or the webhook
secret. The receipt is opaque to the receipt layer, so the action comes from a
reader that the transport adapter supplies (`action_of`): it decodes the payload and returns the
`action` only when it is a plain token (`[a-z_]`, 1 to 40 characters), so free text or a line
break in a payload cannot reach the log. The reader runs for the `dispatch` and `finalize`
stages, including a timeout, and a reader that fails never hides the failure. `action=-` without
a diagnostic record means no error happened: the payload has no action (`status`) or its action
is not a plain token; it also marks a failure while claiming, where the receipt has not been read.
A payload that cannot be decoded is an error: the reader raises, and `action=-` is preceded by
the diagnostic record described below. The same token rule applies to the `action=` that starts
the `detail` of an ignored event in the outcome line.

The line is written even when one of its fields cannot be computed: if the action reader, the
failure classifier or the outcome rendering raises, the field falls back to `action=-`,
`category=internal` or `outcome=-`, and a diagnostic WARNING comes first,
`GitHub webhook delivery <delivery_id> failure line: <field> fell back to <default> after <ExceptionClass>`.
It names the field and the error class only, never the message or a traceback. The original
error, the receipt release and the sweep's ERROR record are unchanged.

| stage | meaning |
| --- | --- |
| `claim` | the receipt could not be claimed; `event` and `action` are `-` |
| `dispatch` | the projection or the Run trigger failed (a GitHub request, the database, the 240 s timeout); a Run may already exist, see above. The receipt is released for a retry after 30 s and the third failure marks it failed; if the release itself fails, the line still names the dispatch error, but the receipt waits for the 5-minute claim lease and its attempt counter does not advance |
| `finalize` | the dispatch finished but updating the receipt failed; `outcome` and `detail` repeat the dispatch result, so a Run may already exist. The claim lapses after 5 minutes and the delivery is replayed; the trigger answers `duplicate` instead of creating a second Run |

| category | meaning |
| --- | --- |
| `timeout` | the dispatch exceeded its timeout, or a builtin `TimeoutError` |
| `github_request` | an `httpx` / `httpcore` error: a refused connection, an `httpx` timeout, or an `HTTPStatusError` of any status (401, 404, 5xx); the status is in the traceback record, not in the line |
| `database` | a SQLAlchemy or `psycopg` error |
| `internal` | anything else: our own bugs and failed invariants on a GitHub response (a `ValueError` for an unexpected response, such as inconsistent check-suite pagination or a changed pull request number, a pydantic `ValidationError`, a JSON error); the traceback record tells them apart |

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
