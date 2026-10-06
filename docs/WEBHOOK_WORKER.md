# GitHub webhook receipt worker

The HTTP endpoint verifies each GitHub signature, commits the JSONB delivery receipt,
and returns `202`. The worker claims pending receipts and projects installation
and pull request events after the HTTP response. Expired claims and failed dispatches are retried.

Set `DATABASE_URL`, `RABBITMQ_URL` (the worker publishes Runs to the review queue and does not
start without it), `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`, and `GITHUB_APP_BOT_LOGIN` (the
exact App bot login, such as `example[bot]`). Apply the migrations and seed the prompts: without
an active `review.system` prompt no Run is created. Then run:

```bash
uv run alembic upgrade head
uv run python -m app.bootstrap.seed_prompts
uv run python -m app.webhook_worker
```

For local Docker Compose, set `GITHUB_WEBHOOK_SECRET`, `GITHUB_APP_ID`,
`GITHUB_APP_PRIVATE_KEY`, and `GITHUB_APP_BOT_LOGIN` in `.env`, apply the migrations, seed the
prompts and start the `webhooks` profile (the compose file passes `RABBITMQ_URL` itself). A
worker started outside Compose needs the same database, broker and GitHub App configuration.
Values of the dev GitHub App and its smee.io delivery path: [README](../README.md#local-dev-app).

```bash
docker compose up -d postgres rabbitmq
docker compose run --rm backend alembic upgrade head
docker compose run --rm backend python -m app.bootstrap.seed_prompts
docker compose --profile webhooks up --build
```

## Local recipe: a signed `labeled` delivery creates a queued Run

`scripts/webhook_smoke.py` alone only checks that the API accepts a delivery (202). It sends
installation 17 and repository 101: against the real GitHub the worker's calls fail (GitHub does
not know installation 17), so the delivery is marked failed after three tries and no Run is
created; with the stub below but without the seed it is deferred with
`ignored_unknown_repository`. To get a Run, seed that installation and repository and point the
worker at `scripts/github_stub.py`, which answers the installation token and PR 7 with the
`ai-review` label. Start from a clean database with only PostgreSQL and RabbitMQ up: the
compose `backend` would hold port 8000, the compose `worker` would take the Run from the
queue, and the compose `webhook-worker` could claim the delivery against the real GitHub. The
SQL seed (the `psql` block of `INSERT`s below) fails on a second run, unlike `seed_prompts`,
which is safe to rerun; to repeat the recipe, start again from `down -v`, which deletes the
local database and broker volumes:

```bash
docker compose --profile webhooks down -v
docker compose up -d --wait postgres rabbitmq
```

One-time setup of that database, from the repository root:

```bash
# .env.local: the API and the worker read the same values
cat > .env.local <<'ENV'
DATABASE_URL=postgresql+psycopg://app:app@127.0.0.1:5432/app
RABBITMQ_URL=amqp://app:app@127.0.0.1:5672/
GITHUB_WEBHOOK_SECRET=local-secret
GITHUB_APP_ID=1
GITHUB_APP_BOT_LOGIN=reviewer[bot]
GITHUB_API_URL=http://127.0.0.1:9999
ENV
# any RSA key: the stub does not check the App JWT
echo "GITHUB_APP_PRIVATE_KEY=\"$(openssl genrsa 2048 2>/dev/null)\"" >> .env.local

uv run --env-file .env.local alembic upgrade head
uv run --env-file .env.local python -m app.bootstrap.seed_prompts
docker compose exec -T postgres psql -U app -d app -v ON_ERROR_STOP=1 <<'SQL'
INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (gen_random_uuid(), 'Local', 0);
INSERT INTO provider_installations (id, workspace_id, provider, external_id, metadata)
  SELECT gen_random_uuid(), id, 'github', 17, '{}'::jsonb FROM workspaces WHERE name = 'Local';
INSERT INTO repositories (id, provider_installation_id, external_id, full_name,
                          default_branch, web_url, wait_for_ci)
  SELECT gen_random_uuid(), id, 101, 'smoke/webhook-smoke', 'main',
         'https://github.com/smoke/webhook-smoke', 'never'
  FROM provider_installations WHERE external_id = 17;
INSERT INTO rule_versions (id, repository_id, version, rules, checksum, is_active)
  SELECT gen_random_uuid(), id, 1, '[]'::jsonb, repeat('a', 64), true
  FROM repositories WHERE external_id = 101;
SQL
```

Then start each long-lived command in its own terminal and leave it running:

```bash
uv run python scripts/github_stub.py                                      # terminal 1
uv run --env-file .env.local uvicorn app.main:app --port 8000             # terminal 2
uv run --env-file .env.local python -m app.webhook_worker                 # terminal 3
```

In a fourth terminal, send the delivery and read the Run:

```bash
GITHUB_WEBHOOK_SECRET=local-secret uv run python scripts/webhook_smoke.py
sleep 40                                 # the worker sweeps every 30 s
docker compose exec -T postgres psql -U app -d app -c \
  "SELECT state, head_sha, trigger, engine, message_published_at IS NOT NULL FROM runs"
```

After the sweep the query shows one Run: `queued`, head `aaaa…`, trigger
`webhook`, engine `fast`, published `t`. In the worker terminal, after the `INFO:<logger>:`
prefix, the delivery line reads (ids vary):

```text
GitHub webhook delivery <delivery_id> event=pull_request status=projected_pr detail=action=labeled pr=<pull request id> head=aaaaaaa: enqueued run=<run id>
```

`enqueued` has no reason, so no parentheses follow it (see "Outcome log" below). A deferred
delivery prints the same prefix with another status, such as
`status=ignored_unknown_repository`, and ends with `retry_at=`. The repository waits for no CI
(`wait_for_ci = never`), so the label alone starts the Run.

The review worker is not needed for this check: started with the same `.env.local`, it would
claim the Run and fail at its first GitHub read of the PR, which the stub answers only in the
shape the webhook worker needs. Against a real PR it
reviews only with `LLM_*` set: without `LLM_MODEL` the Run takes all three attempts and ends
`failed` / `llm_unavailable`, and local models have a fixed 90 s call timeout and may need
`prompt_json` (README, "LLM gateway").

## How the worker handles deliveries

The worker serializes each PR's event projection with a PostgreSQL session advisory lock on an
autocommit connection and stores the result in a separate short transaction, so the GitHub call
never spans a database transaction. `labeled` / `unlabeled` of the `ai-review` label (events sent
by the App's own bot are ignored) and `synchronize`, `closed`, `reopened`, and `edited` fetch the
current GitHub PR once while holding that lock; `opened` is applied from the payload. Label
events and `synchronize`, `closed`, and `reopened` reconcile `ai_review_labeled` from that PR's
current labels, so a closed PR keeps its label state; a new Run needs an open PR, since CI
eligibility and the no-CI sweep consider only open PRs (`docs/PIPELINE_SPEC.md` §8.2);
`edited` updates metadata only. The issue timeline is never fetched:
`review_requested` and `review_request_removed` are dropped as irrelevant. A pull request payload
whose repository name is outside `owner/repo` (the same shape as for installation events, below)
is invalid: the outcome line reports `invalid_payload fields=repository.full_name` (see "Outcome
log") and the receipt is acknowledged. A failed dispatch (for
example a GitHub error or the 240 s dispatch timeout) releases the receipt for a retry after
30 s; the third failure marks it failed and it is no longer replayed (if the release itself fails,
the receipt keeps its claim until the 5-minute lease lapses and the attempt counter does not
advance). Once that mark is committed, the worker logs a WARNING after the failure line,
`GitHub webhook delivery <delivery_id> failed after its last attempt: <ExceptionClass>`
(see "Failure log" below). It also projects
installation events. A delivery for an unknown installation or repository, or for an
installation event whose repository details or installation token GitHub cannot answer, is
deferred instead, see "Deferred deliveries" below.

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
rest; `default_branch` and `html_url` are used only when both are present. `full_name` must
have the form `owner/repo`, because it goes into the path of GitHub requests that carry the
installation token: exactly one slash, no dots in the owner (underscores are allowed, as in
Enterprise Managed User logins), and a repo that does not consist of dots only (`.github`
and `repo.js` pass, `.` and `..` do not). Every adapter that puts a repository name into a
request path builds the path with the shared builder in
`app/common/infrastructure/github_repository_path.py`, which rejects a name of another shape
before a token is minted or a request is sent, so it also protects rows stored before the
parser checked the name. The tree adapter encodes the default branch with it (`.` and `..` are
rejected), and the check-run and run-source reads pass their commit SHAs through it;
`get_blob` and the CI adapter keep their own SHA checks, and the installation id in a path is
an integer. When either of `default_branch` and `html_url` is
missing, the worker reads both with `GET /repos/{full_name}` using the installation token
before it fetches the tree, outside any database transaction. A read that GitHub cannot
answer (the token request or `GET /repos` fails with a network error, a timeout, a 404, a
5xx or any other HTTP error status) is an outage and defers the delivery, see "Installation
event failures" and "Installation token failures" below. A `GET /repos` answer that is HTTP
200 but is not JSON or lacks the default branch or web URL (never stored as an empty value),
a malformed token response and an App key that cannot sign are not outages: they fail the
dispatch (retry after 30 s, three attempts in total, then `projection_failed_at`, never
replayed). `deleted` and `removed` make no GitHub
request. An installation payload that fails validation is logged at WARNING (delivery,
event, action, installation id, the total error count and the first ten failing field names
and messages, never their values) and its receipt is marked projected, so it is not
replayed. One invalid `full_name` among N repositories therefore drops the whole event at
the parser, the valid repositories included.

Installation event failures. Repositories are processed independently, except that a token
failure skips the repositories that have not started (see "Installation token failures"). One
that cannot be read (a 404 right after the repository was created, a rename or a removal, for
example) does not discard the others: the readable ones are saved in one transaction, each failure
is logged at WARNING (installation id, repository id, full name, error type and, for an
HTTP error, its status code, for unreadable details or a token failure those of the error
behind them; never the message or URL), and the first error in event order is then raised.
That error decides the path of the receipt. Unreadable details and a token request GitHub
could not answer defer the delivery like an unknown installation (`deferred_repository_details`:
retried after 5 minutes, then revived once an hour, see "Deferred deliveries" below). Despite
their names, that status and the dispatcher's WARNING `Repository details unavailable for
installation <id>: <reason>` cover both causes: the reason is the HTTP status or exception
class of unreadable details, or `GitHub installation access token unavailable`, never the URL
or the token. A failed tree request, a malformed token response or an App key that cannot
sign, the 240 s dispatch timeout and any other error take the failed-dispatch path above
(retry after 30 s, three attempts in total, then `projection_failed_at`). So a failed tree
request of an earlier repository fails the delivery even when a later repository's details
are only unavailable, and unavailable details of an earlier repository defer it even when a
later tree request failed. Every retry or revival replays the whole event, which is safe to
repeat: every repository that was read is upserted again, and the upsert sets `enabled` to
true, so a repository disabled between two attempts is enabled again. (The at-least-once
replay of a delivery already did that; with partial persistence, replaying repositories that
are already saved is now the normal path of a partly failed event.) The label request is sent
again and a 422 (label exists) counts as success. A 404 caused by replication lag therefore
heals on a later attempt. A repository whose tree keeps failing is a residual risk: a receipt
that failed for good is not replayed, and neither the hourly revival nor `wake_receipts`
clears that mark.

Installation token failures. The cache of installation tokens is cold after a restart or an
expiry, and the parallel repositories then share one token mint:
`GitHubAppInstallationAccessTokenProvider` keeps one exchange in flight per installation, and
every caller that waits for it receives its token or its error. A failed exchange is not
remembered, so the next call after it exchanges again, and a waiter that is cancelled (by the
dispatch timeout, for one) leaves the exchange running for the others. A failure to obtain
the token, such as a suspended installation or a revoked or invalid App key, concerns the
whole installation, not one repository. The details, tree and label adapters report it as
`InstallationAccessTokenError`, and the projector then skips the repositories that have not
started, while the running ones finish, the readable ones are saved and the first failure in
event order is raised as before. When GitHub could not answer the token request (a transport
error, a timeout or an error status, such as the 401 of a revoked App key), the error is
transient and the dispatcher defers the delivery like unreadable details (see "Installation
event failures"). A malformed token response or an App key that cannot sign is permanent, as
for details: the repositories that have not started are still skipped, but the delivery takes
the failed-dispatch path. One
WARNING with the installation id and the count covers the skipped repositories, which get no
WARNING of their own, and the WARNING of a failed repository names the type and status of the
original error. A failed attempt therefore costs about one mint, not N: about three per cycle
of three attempts, and for a transient failure a cycle about once an hour for up to 7 days,
plus a cycle per login. A
failed label request is still only logged, unless the token behind it failed: a token failure
at the label request is a failure of that repository like any token failure. The repository
is not saved in this attempt, the repositories that have not started are skipped, and the
first failure in event order decides the path (transient: deferred and retried; permanent:
failed). So a transient token failure at the label request of the last repository alone
defers the delivery instead of onboarding it without that label. The pull request and CI
adapters, and the review worker that shares the provider class, still see the original HTTP
errors, so a token outage fails their dispatch instead of deferring it.

Installation event budget. The dispatch of a receipt is limited to 240 s. Up to four
repositories are processed at a time (details, tree, label), and the label requests are
spaced at least 0.8 s apart, 75 a minute, on top of that. GitHub allows 80
content-generating requests and 900 points (a POST costs 5) a minute, and the labels are
the bottleneck. A slot stays held while its repository waits for the label pacer, so the
throughput is min(1 / 0.8 s, 4 / (G + P)) repositories a second, where G + P is the
details, tree and label-request latency of one repository. While G + P stays below about
3.2 s the pacer is the limit: 200 repositories take about 160 s. The label lane alone
allows 240 s / 0.8 s = 300 repositories; the ceiling of about 280 leaves about 16 s of the
budget for the details and tree requests of the first repositories and for the commit.
Slow large trees raise G + P and lower that ceiling, so 160 s is the best case, not a
guarantee. (The 10 s httpx timeout applies to each phase of a call, connect, read, write
and pool, not as a total deadline, so one call can take longer than 10 s.) The pace
belongs to the label adapter, which the worker creates once, so it is shared by every
event of the process. When the timeout fires it cancels the whole event before the
commit: nothing is stored and the receipt takes the failed path above. The worker's sweep
handles receipts one at a time, so an onboarding of about 160 s delays the other
deliveries queued behind it.
`GET /installation/repositories` is not used: it would replace one of the three calls per
repository, an `added` event needs the ids filtered out of it, `repository_selection=all`
needs every page, and it does not touch the label requests, which are the bottleneck.

Empty repositories. The tree request of a repository without a commit answers 409
("Git Repository is empty."). A 409 whose JSON `message` contains `repository is empty`,
compared case-insensitively, is read as an empty tree, so the repository is connected with no
recognised languages and `select_default_rule_set` picks `backend`, as for any tie. Any other
409 is an error like any other status, so a retry can heal it and an unrelated 409 does not
freeze a repository as empty. The default rule set is chosen once, at the first connection:
`OnboardRepository` returns the existing active rule version before it reads the languages,
and connecting the repository again re-enables the same row without touching its rules.
Nothing recomputes the languages, so an empty repository keeps the `backend` rules, even if
it later gets frontend code, until its rules are replaced by another path; there is no
automatic path.

Residual risks of the budget. GitHub also caps content-generating requests at 500 an hour.
A retry or a revival sends the label request again for every repository of the event that
was read (a 422 is a success, but it still counts as a request). A repository that failed
never reaches its label POST, so three attempts at N repositories make at most 3N - 2 POSTs
(one repository failing twice and healing on the third attempt) or 3 × (N - 1) (one that
keeps failing). Both pass 500 from N = 168, so about 170 repositories; label POSTs made in
the same hour for other events share the same 500 budget. A repository whose details stay
unreadable keeps the delivery deferred, and the hourly revival runs another cycle of three
attempts about once an hour for up to 7 days after the delivery was received (see "Deferred
deliveries"), so an event of about 170 or more repositories with one such repository can
hit the cap every hour for up to 7 days. Once the cap is reached the label POSTs answer 403
(or 429); each is logged and is not fatal, and the repositories are connected without the
label. The other residual risks are described above: a receipt that failed for good is not
replayed, a replay enables a repository again, the sweep handles receipts one at a time, and
one invalid name drops the whole event at the parser.

Outcome log. The process calls `logging.basicConfig(level=INFO)` and, once a delivery has been
processed and its receipt updated, writes one INFO line to the `webhook-worker` log, so the log
shows why a label produced no Run:

```text
GitHub webhook delivery <delivery_id> event=<event> status=<status> detail=<detail> [retry_at=<time|none>]
```

`status` is the dispatch status: `projected_pr`, `processed_ci`, `onboarded`, the final
`ignored_irrelevant_event` and `ignored_invalid_event`, or a deferral, retried as described in
"Deferred deliveries" below (`ignored_unknown_installation`, `ignored_unknown_repository`,
`deferred_known_event`, `deferred_repository_details`; a deferral carries `retry_at`).
`detail` is `-` or the reason. For a `pull_request` event it starts with `action=<action>`, so
`labeled` and `synchronize` can be told apart (an ignored action, such as `ready_for_review`,
shows only that). For a Run trigger (label, `synchronize`, `reopened`, CI events) the rest is
`pr=<pull request id> head=<first 7 of the head sha>: <status>[ (<reason>[: <detail>])][ run=<run id>]`:
`enqueued` and `publication_pending` have no reason and end with `run=`, every other status has
a reason in parentheses and no `run=`. A CI event joins one such outcome per open PR on that
head with `; ` (and has no action):

| status | reason | meaning |
| --- | --- | --- |
| `enqueued` | | the Run was inserted and published (`run=<id>`) |
| `publication_pending` | | the Run was inserted (`run=<id>`), the broker publish failed; the review worker's leader loop replays it |
| `ineligible` | the CI gate reason: `ci_blocked` (a foreign check suite or the commit status is not green), `waiting_for_ci` (no CI evidence yet: with `wait_for_ci = always`, with `auto` while the label or head time is unknown, or with `auto` still inside the 2-minute window), `label_not_active`, `stale_head`, `stale_state`, `closed_pr`, `disabled_repository`, `unknown_pr` | the gate decided not to start a Run |
| `unconfigured` | `missing_installation`, `missing_rules` (no active rule version), `missing_prompt` (no active `review.system` prompt and none pinned) | the repository lacks what a Run needs; a new label or CI event does not change that |
| `stale` | `pull_request_gone`, `repository_gone`, `state_changed` (the PR changed between the gate and the locked read) | the decision no longer matches the current PR |
| `duplicate` | `active_run` (a queued, running or publishing Run exists), `head_already_reviewed` (this head already has a webhook Run) | no second Run is created |

`ci_blocked` and `waiting_for_ci` carry a detail that says what blocks or delays the gate,
for example `ineligible (ci_blocked: check suite app=5111174 queued)`:

| reason | detail | meaning |
| --- | --- | --- |
| `ci_blocked` | `check suite app=<app id> <status>`, or `<status>/<conclusion>` once the suite has a conclusion, then ` (+<n> more)` when `n` more suites block | the first blocking foreign check suite in GitHub's order |
| `ci_blocked` | `commit status <state>` | the combined commit status of the head is not `success` |
| `waiting_for_ci` | `no CI yet` | `wait_for_ci = always`, and the head has no CI evidence yet |
| `waiting_for_ci` | `no CI yet, label or head time unknown` | `wait_for_ci = auto`, but the label or head time is missing, so the 2-minute window cannot start; only CI starts the Run |
| `waiting_for_ci` | `no CI yet, auto start at <time>` | `wait_for_ci = auto` inside the window; `<time>` (ISO 8601, UTC) is 2 minutes after the later of the label and the first sight of the head |

The status, conclusion and state come from GitHub and are logged like `event` below: as is when
they are `[a-z_]`, 1 to 40 characters, and as `?` otherwise.

No-CI sweep. Under `wait_for_ci = auto`, no delivery resolves `waiting_for_ci` when the head
gets no CI: the no-CI sweep in `worker` does (its leader loop, every 30 s, once 2 minutes have
passed since the later of the label and the first sight of the head; a PR without either time
is never picked). It calls the same `try_enqueue` and writes one INFO line per candidate to the
`worker` log, not the `webhook-worker` log, with the same outcome as above:

```text
No-CI sweep pr=<pull request id> head=<first 7 of the head sha>: <status>[ (<reason>[: <detail>])][ run=<run id>][; excluded until the head or label changes]
```

A candidate whose result is not `enqueued` gets the suffix: the sweep excludes it and does not
pick it again until its head or label changes, for example
`No-CI sweep pr=<id> head=aaaaaaa: unconfigured (missing_rules); excluded until the head or label changes`.

Other details: `no open pull request` (a PR event or label whose PR is closed or unknown,
nothing to enqueue), `no open pull request at this head` (a CI event for a head that no open PR
has, for example CI of an old head that finished after a push),
`not an ai-review labeled action`, `label is not ai-review` (a foreign label), and, when the
trigger did not run, the projection result: `projected`, `ignored_stale`, why the repository
cannot take the event (the second table below), or why the projection was ignored, for
example `action=labeled ignored_own_bot` (status `ignored_irrelevant_event`, the receipt is
acknowledged):

| projection result | meaning |
| --- | --- |
| `ignored_own_bot` | the label event was sent by the App's own bot |
| `ignored_identity_mismatch` | the current GitHub PR is not the PR the event names (its id, number, repository or installation differ) |
| `ignored_identity_conflict` | the event's PR id and number belong to different stored PRs |
| `ignored_external_id_mismatch` | the stored PR with this number has another GitHub PR id |
| `ignored_other_label` | a label other than `ai-review`; the dispatcher answers `label is not ai-review` before the projection, so a delivered label does not show it |
| `ignored_unrelated` | remains only for paths the dispatcher already filters (review requests, actions the projection does not handle), so it does not appear for a delivered label |

A pull request or label event whose repository cannot take it is not acknowledged: its status is
`ignored_unknown_repository`, it is deferred like any deferral below (retried after 5 minutes,
three attempts in total, then `retry_at=none`), and the detail names the case, for example
`action=labeled disabled_repository`. The projection tells the cases apart with one extra
database query after the repository lookup misses, without a GitHub call (api#80):

| projection result | meaning |
| --- | --- |
| `unknown_repository` | no installation stores the repository: it was never onboarded |
| `disabled_repository` | the event's installation stores the repository, but disabled (removed from the installation, or turned off in its settings); this wins when another installation stores it too. It is not the CI gate reason of the same name, `ineligible (disabled_repository)` above, which the Run trigger reports for a pull request already stored |
| `other_installation_repository` | the repository is stored, but only under installations other than the event's; pull request events are not checked against the linked installations, so the event's installation may also be unlinked |

GitHub before the repository, one budget (api#80, kept by decision). A label delivery
(`labeled`, `unlabeled`) asks GitHub first, for the installation token and the current pull
request, and looks the repository up only after that; a pull request event that reads the
current pull request (`synchronize`, `closed`, `reopened`, `edited`) does the same. So while
GitHub cannot be reached, even a repository that was never onboarded fails the dispatch
(`failed stage=dispatch category=github_request`) instead of being deferred as
`unknown_repository`. Failed dispatches and deferrals spend the same three attempts
(`projection_attempt_count`), in either order: two deferrals and then a failed dispatch mark
the receipt failed (see "Deferred deliveries"), and two failed dispatches (a `ConnectError`,
for example) and then one repository lookup that misses defer it for good (`retry_at=none`)
after a single real check of the repository. A receipt deferred for good waits for linking
(`wake_receipts`); a receipt marked failed is never revived: neither the hourly revival nor
linking clears `projection_failed_at`, and the sweep claims only receipts without it. Its pull
request gets a Run only from a later delivery, such as a push, a reopen, or the label removed
and added again.

`invalid_payload` (status `ignored_invalid_event`) means the payload could not be parsed into the event's shape,
for example a `labeled` event with an empty `label`; for a `pull_request` event
`action=<action>` precedes it. For a `pull_request` or CI event it is followed by
`fields=<path>,...`, the schema paths of the failing fields (`label.name`,
`pull_request.head.sha`; `label` for a label event without a label, `number` when the top-level
number differs from the pull request's), each once, at most 10, then `+<n>` for the rest:
`action=labeled invalid_payload fields=label.name`. A path segment that is not an identifier
(`[A-Za-z_][A-Za-z0-9_]*`, at most 64 characters) is logged as `?`, a list index as its
number, and an empty path as `?`. A payload that is not a JSON object, or any other rejection,
is plain `invalid_payload`.
The receipt is acknowledged and not retried, and no payload value or error message is logged
with this reason, only field paths; for an installation event the reason stays plain and a
separate WARNING names the failing fields. The line never carries the payload, an installation
token or the webhook secret.

A deferred delivery's outcome line ends with `retry_at=<time>`, or `retry_at=none` after the
third attempt, and that final deferral is also logged as a WARNING with its reason, once the
receipt is committed. `deferred_repository_details` is an installation event whose repository
details or installation token GitHub cannot answer; like the other deferrals it is retried
after 5 minutes, at most
three attempts in total. `retry_at=none` means no retry is scheduled: the receipt waits until
something revives it, the hourly revival for installation events or linking the installation
(`wake_receipts`), see "Deferred deliveries" below.

Logged fields. In this line and in the failure line below, `event` and `action` are plain
tokens: a value is logged only when it is `[a-z_]`, 1 to 40 characters, and as `-` otherwise.
`event` is the `X-GitHub-Event` header, which the signature does not cover; the receipt stores
and dispatches it as received, only the log is restricted. The delivery id is the stored
receipt id, the `X-GitHub-Delivery` header (GitHub sends a GUID). Intake answers `400` and
stores nothing unless it is 1 to 255 ASCII letters, digits and `-` (#80), so the id of a
delivery stored since then is logged as received and cannot forge a field of the line.
Receipts stored before that check are not re-validated: their id is logged as stored.

Failure log. When processing a delivery fails, the worker first writes one WARNING line with the
action and a failure category, then the sweep writes its ERROR record
`GitHub webhook projection failed for delivery <delivery_id>` with the traceback. On the last
failed attempt of a dispatch, the final WARNING
`GitHub webhook delivery <delivery_id> failed after its last attempt: <ExceptionClass>` comes
between them. The first line is the greppable outcome, the ERROR record the diagnosis:

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
| `dispatch` | the projection or the Run trigger failed (a GitHub request, the database, the 240 s timeout); a Run may already exist, see above. The receipt is released for a retry after 30 s and the third failure marks it failed; once that is committed, a second WARNING follows the line, `GitHub webhook delivery <delivery_id> failed after its last attempt: <ExceptionClass>`, so the last attempt is told apart from one that is retried; if the release itself fails, the line still names the dispatch error, but the receipt waits for the 5-minute claim lease and its attempt counter does not advance |
| `finalize` | the dispatch finished but updating the receipt failed; `outcome` and `detail` repeat the dispatch result, so a Run may already exist. The claim lapses after 5 minutes and the delivery is replayed; the trigger answers `duplicate` instead of creating a second Run |

The use case maps only `timeout` itself: any builtin `TimeoutError`, which needs no library
knowledge. The other categories come from a classifier that the composition root supplies
(`classify_failure`, in the webhooks infrastructure), because only that layer knows the HTTP
and database libraries; without a classifier every other failure is `internal`.

| category | meaning |
| --- | --- |
| `timeout` | the dispatch exceeded its timeout (240 s), or the dispatch raised another builtin `TimeoutError` |
| `github_request` | a GitHub request failed: a refused connection, a request timeout, or an error status of any kind (401, 404, 5xx); the status is in the traceback record, not in the line |
| `database` | a database error: the connection, a statement or the connection pool |
| `internal` | anything else: our own bugs and failed invariants on a GitHub response (a `ValueError` for an unexpected response, such as inconsistent check-suite pagination or a changed pull request number, a pydantic `ValidationError`, a JSON error); the traceback record tells them apart |

Deferred deliveries. A delivery the dispatcher cannot handle yet (unknown installation or
repository, an event without a handler, or an installation event whose repository details
or installation token GitHub cannot answer) is retried after 5 minutes, at most three
attempts in total, like a
failed dispatch. Deferrals and failed dispatches draw on the same three attempts: two
deferrals followed by one failed dispatch (a failing tree request, for example) mark the
receipt failed. After the third deferral it is deferred (`projection_deferred_at`) and the
sweep no longer selects it until something revives it with a fresh attempt budget. Once an
hour, in the same tick as the purge, the worker revives the installation-event receipts
(`installation` and `installation_repositories`) of linked installations that have been
deferred for at least 45 minutes and were received within the last 7 days
(`ReviveDeferredInstallationDeliveries`). The revival covers installation events only:
pull-request and label deliveries deferred with `ignored_unknown_repository` during the same
outage are still revived only by linking (`wake_receipts`, below). CI deliveries
(`check_suite`, `workflow_run`, `status`) are not deferred by an outage: only a dispatcher
composed without a Run trigger (no broker publisher or no App id) defers them
(`deferred_known_event`), and `webhook-worker` does not start without `GITHUB_APP_ID` and
`RABBITMQ_URL`, so it never defers them.

An `installation_repositories.added` event that later brings their repository in does not
wake them either, by decision (#70). `webhook_events` has no repository column, so waking
them on `added` would mean waking every deferred delivery of the installation. Re-enabling
the repository in its settings (`PATCH /api/repos/{repo_id}`) does not wake them either: a
receipt with attempts left is retried every 5 minutes anyway, and one deferred for good is
revived only by `wake_receipts` at a GitHub login, since the hourly revival takes installation
events only. The cost: a PR labeled during the outage gets no Run until its next push
(`synchronize`), a reopen, or the label removed and added again; a CI event alone does not
create one, since it only matches a PR already stored with that head.

An installation-event receipt (`installation`, `installation_repositories`) is revived at the
first hourly tick that comes at least 45 minutes after its deferral. A revived receipt
spends its three attempts within about ten minutes, so that is usually the next tick and the
receipt is retried about once an hour; when it was deferred less than 45 minutes before a
tick (its first deferral, a cycle started by a login, a worker restart that shifts the tick,
or a slow sweep), it is the tick after that, up to about two hours later. An outage of the
details read or of the token request therefore heals on its own, with no login, about an
hour after it ends, unless the last attempt of a cycle fails the dispatch instead (a failing
tree request, a malformed token response or an App key that cannot sign, the 240 s dispatch
timeout or a database error; a failed label request is only logged, unless the token behind
it is permanently unusable, while a transient token failure defers the delivery again): the
receipt is then marked failed (`projection_failed_at`), and neither the revival
nor a login brings it back. The 45-minute delay and the 7-day window are the parameters the
tech lead approved
(api#71): a GitHub outage longer than a week is not a transient failure, and a `GET /repos`
404 for a week means the repository is gone. Linking the installation (`wake_receipts`,
which runs at every GitHub login of a user whose token lists the installation) clears the
mark of every deferred delivery of the installation, whatever its event or age. Residual
risks: every attempt runs the event again for all of its repositories, those already saved
included (see "Installation event failures"), and each readable repository costs a details
read, a tree read and a label request, the unreadable one its details read, so an event of
N repositories with one that stays unreadable costs about 3 × (N - 1) + 1 GitHub requests
per attempt, three attempts an hour for 7 days after it was received, plus three attempts
per login; the label POSTs of the saved repositories repeat on every attempt (see "Residual
risks of the budget"). A revived
`installation_repositories.added` is applied hours or days late without an ordering check
against a later `removed` event of the same repository, so a public repository can come back
enabled after it was removed from the installation (a private one answers 404 and stays
deferred); the 5-minute retries and `wake_receipts` already had this gap. Outside the 7-day
window only a login or a delivery with a new GUID revives a receipt, and one nobody revives
is deleted 30 days after its last `projection_deferred_at` (see Retention). Its repositories
that no earlier attempt saved are then stored only by a delivery with a new GUID, for example
after removing and re-adding
the repository in the installation settings; a redelivery of the same GUID is ignored as a
duplicate. Each sweep that selects at least one delivery logs how many it handled, deferred
and deferred for good (`GitHub webhook sweep: N handled, M deferred, K deferred for good`); a
sweep with nothing due logs nothing. K counts the final deferrals and is part of M; each of
them also logs `GitHub webhook delivery … deferred after its last attempt`. A dispatch that
fails on its last attempt is not in any of the three numbers: it logs its own `… failed after
its last attempt` WARNING. A revival that resets receipts logs how many.

Retention. Finished receipts (projected, failed, or deferred and not revived since) are deleted
30 days after they finished; the worker runs the purge once an hour. The purge is one `DELETE`
without a batch limit and without an index on its condition, by decision (#70): with a few
installations the table stays small and the hourly scan is short. It counts the deleted rows
from the statement's row count and returns no ids (no `RETURNING`), so a large purge does not
load its ids into the worker. Add a batch limit and an index on the finished timestamps once
the table holds about one million rows or one purge takes longer than 10 seconds. Neither is
logged: check the size with `SELECT count(*) FROM webhook_events` and the duration with
`EXPLAIN ANALYZE` of the purge query inside a transaction that is rolled back, since
`EXPLAIN ANALYZE` executes the `DELETE`:

```sql
BEGIN;
EXPLAIN ANALYZE DELETE FROM webhook_events
WHERE projected_at < now() - interval '30 days'
   OR projection_failed_at < now() - interval '30 days'
   OR projection_deferred_at < now() - interval '30 days';
ROLLBACK;
```

The `Purged N finished GitHub webhook receipts` line (written only when N > 0) gives the rows
removed per run.
