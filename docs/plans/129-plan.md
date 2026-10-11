# Implementation plan: PostgreSQL wake-ups for webhook-worker (#129)

## Status and scope

User approved the plan and PR ownership comments on 2026-10-10.
Implementation completed in reviewed slices under `agent-loop`; fresh final review
cleared Standards 0 / Spec 0. Latest-main gates passed (2444 tests, zero skips)
on `8cf3608d80d0d1cf79e56e333b50d6a4d2052ab8`; see `129-todo.md` for evidence.
Publishing is authorized; human review, merge and tech-lead acceptance remain pending.
Specification: https://github.com/larchanka-training/dmc-268-api-t6/issues/129.
Inspected on 2026-10-10 against freshly fetched `origin/main`:
`7325fae976801ca33ef3999c8cbe5aa2aa37ea05`. The relevant worker, listener and
stores are identical in the current checkout and this main revision.

New receipts and OAuth reactivation should wake an idle webhook worker before its
existing 30-second polling pause ends. PostgreSQL remains the durable queue; the
notification only asks the worker to inspect that queue. Preserve claims, leases,
retry eligibility, batch size 100, hourly revival/purge, heartbeat and shutdown.
No new public API, UI, SSE, broker migration, or onboarding completion-time promise.

## Current behavior

- `ReceiveGitHubDelivery.execute` saves a receipt and commits through its use case.
  `SqlAlchemyGitHubWebhookReceiptStore.save` uses insert-on-conflict and returns a
  boolean; duplicates must remain duplicates.
- `LinkGitHubInstallations.execute` calls `wake_receipts` in its final reconciliation
  transaction, after GitHub reads have finished. This resets attempt/defer/retry
  state without changing the permanent failure semantics.
- `sweep_forever` scans immediately on startup, runs maintenance on the first pass
  and hourly thereafter, and sleeps 30 seconds unless 100 deliveries were handled.
- `run_update_listener.py` demonstrates dedicated psycopg autocommit connections,
  URL conversion, reconnect logging and cancellation. Use its pattern without
  modifying the portal listener or turning this issue into a generic listener rewrite.
- `psycopg[binary]` is already declared; no dependency or migration is needed.

## Architecture decisions

1. Add a webhook notification helper in
   `app/modules/integrations/webhooks/infrastructure/webhook_notifications.py`.
   Own one constant channel, provisionally `webhook_work_available`, and issue
   `SELECT pg_notify(channel, '')` using the caller's `AsyncSession`. Send no
   receipt data, repository information or secrets in the payload.
2. Call that helper in `save` only after a successful insertion, in the same
   transaction. Call it in `wake_receipts` when the existing update matches receipts;
   use statement rowcount without loading IDs or expanding update eligibility.
   Repositories never commit. PostgreSQL defers delivery until commit and drops
   notifications on rollback; do not emit on a separate autocommit connection.
3. Add a focused composition-layer listener in
   `app/bootstrap/webhook_work_listener.py`, modeled on the existing listener.
   A dedicated autocommit psycopg connection runs `LISTEN` and sets a shared
   `asyncio.Event` for each signal. Successfully installing LISTEN, including on
   every reconnect, also sets the event to force a durable-queue scan. Listener
   failures are logged and retried with a bounded delay; cancellation propagates
   and closes the connection. The sweep remains operational during connection loss.
4. The worker owns one event and one listener task. At the beginning of each sweep
   clear the event, then inspect the database. A signal received during processing
   remains set until the next pass. After processing, wait on the event with the
   existing 30-second timeout, or yield immediately when a full batch was handled.
   Never clear between the scan and wait: that loses arrivals during processing.
   Repeated notifications coalesce; they do not spawn per-notification dispatches.
5. Start the sweep immediately without waiting indefinitely for LISTEN readiness.
   The listener's post-LISTEN event closes the startup/reconnect observation gap.
   Every pass still goes through `replay_pending` and its existing claim/lease
   checks, so multiple workers or repeated signals cannot bypass idempotency.
6. Compose listener and sweep lifecycle with the existing worker task lifecycle;
   cancellation must await listener cleanup before resource disposal. Preserve
   publisher, database and heartbeat cleanup paths. Keep maintenance on monotonic
   time, including startup maintenance, under frequent notifications.

The database remains the source of truth: reconnect scans recover backlog while
the 30-second idle fallback recovers missed signals and future-due retries.
An in-progress GitHub dispatch still determines actual processing latency.

PostgreSQL documents transactional notification delivery and the required
LISTEN-then-inspect sequence:
[NOTIFY](https://www.postgresql.org/docs/current/sql-notify.html),
[LISTEN](https://www.postgresql.org/docs/current/sql-listen.html).
The local stack uses PostgreSQL 17; validate against that real stack, not only fakes.

## Work isolation and ownership

The current branch is `fix/111-worker-portal-url`; it belongs to unrelated PR #125.
Preserve its commits and all existing untracked 113/117 planning documents.
After approval, refresh `origin/main` and create an isolated managed checkout for
`fix/129-webhook-worker-notify` from current main; do not implement on this branch.
Copy only these two #129 planning files into that checkout when needed.

Open PR ownership was inspected read-only with `gh pr list` on 2026-10-10:

| Planned file | Open PR | Required action before editing |
| --- | --- | --- |
| `app/modules/integrations/webhooks/infrastructure/github_webhook_receipts.py` | [#121](https://github.com/larchanka-training/dmc-268-api-t6/pull/121) | Comment there and coordinate the small `save` change; refresh ownership first. |
| `docs/WEBHOOK_WORKER.md` | [#121](https://github.com/larchanka-training/dmc-268-api-t6/pull/121), [#124](https://github.com/larchanka-training/dmc-268-api-t6/pull/124) | Comment on both before editing; preserve each PR's receipt/evaluation content. |

No PR comments were sent during planning. The rule is explicit in AGENTS.md:
"Never edit files owned by another open PR without a comment there." Posting
comments is an external message and needs the user's explicit authorization;
if these PRs remain open, obtain it before those edits or wait for their merge.
Recheck all planned paths before development because ownership can change.

A database trigger in a new migration could avoid editing the receipt store, and
a separate notification document could avoid the shared worker document. Neither
is preferred: the trigger introduces schema lifecycle complexity for two known
application write paths, and a separate document leaves the existing polling
description stale. Coordinate the minimal changes instead.

## Ordered tasks

Use the detailed acceptance criteria, TDD steps and file lists in `129-todo.md`.
Each development slice touches at most five implementation/test/document files;
updating this task checklist is bookkeeping, not an additional feature slice.

1. Transactional new-receipt notification with real PostgreSQL commit/rollback tests.
2. OAuth receipt reactivation notification through real reconciliation transactions.
3. Dedicated reconnecting listener with readiness, cancellation and cleanup tests.
4. Worker event wait preserving scan races, polling, full batches and maintenance.
5. Full PostgreSQL wake-up/recovery/claim verification and worker documentation.

Dependency order: 1 → 2; 1 → 3; 3 → 4; 2 + 4 → 5.
Review every completed slice. After 2 and 4, run verification checkpoints before
continuing. Shared notification contract, worker and test file edits are sequential;
independent Standards and Spec reviews can run in parallel under the review skill.

## Verification approach

Before implementation, establish baseline in the isolated checkout:

```bash
uv run pytest tests/test_webhook_worker_revival.py tests/test_webhook_worker_publication.py tests/test_installation_linking.py
uv run pytest tests/test_run_stream_postgres.py tests/test_webhook_deferred_receipts_postgres.py -rs
```

Provide `TEST_DATABASE_URL` for the second command; skipped PostgreSQL tests do
not establish a baseline. Follow `docs/TEST_PLAN.md` and README for the local
PostgreSQL/RabbitMQ stack. Each DB test gets a random migrated schema and drops
only that schema. PostgreSQL channels are database-wide even across schemas:
use unique test delivery IDs and assert their persisted processing state rather
than assuming a notification count belongs exclusively to one test.

New tests use plain pytest/`asyncio.run`, `@pytest.mark.integration`, real receipt
and link UoWs, real listener connections, and fake GitHub dispatch at its public
port. Synchronize startup/reconnect using explicit readiness/barriers, not an
arbitrary sleep. Assert processing within a bounded deadline substantially below
30 seconds (e.g. 5 seconds on the local stack), and prove no dispatch before commit.
Short injectable timing in unit tests must not change the production 30-second
default. Fallback tests should establish that a sweep, not another notification,
recovered the target receipt.

Integration evidence must cover insertion, OAuth reconciliation, both rollback
paths, startup backlog, actual listener connection interruption/reconnection,
insertion while disconnected, missed notification fallback, arrival during dispatch,
multiple workers/duplicate hints, existing retries, maintenance, and cancellation.
For reconnect fault injection terminate only the identified test listener's backend
PID; use an isolated database role permitted to terminate its own sessions. Never
terminate unrelated sessions or reset shared queues/databases.

Mandatory completion gates, after targeted tests:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run pytest
uv run pytest -m integration -rs
```

Set both `TEST_DATABASE_URL` and `TEST_RABBITMQ_URL` for the integration suite,
using the dedicated RabbitMQ `test` vhost described in `docs/TEST_PLAN.md`.
Require zero integration skips and record outcomes before claiming completion.
No baseline gates were run as part of this planning-only phase.

## Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Signal lost after scan or during long dispatch | Clear the event before scanning; verify with controlled dispatch barriers. |
| LISTEN setup/reconnect misses a committed receipt | Force a scan after LISTEN becomes active, retain startup scan and polling fallback. |
| Signals incorrectly treated as work or duplicate onboarding | Inspect existing durable queue; retain leases/claims, test two workers and repeated hints. |
| Notification storm delays maintenance | Single event, sequential scans, monotonic maintenance tests under continuous signals. |
| Permanent failure/defer semantics inadvertently change | Keep existing update predicates and replay eligibility; run existing receipt/linking tests. |
| Test connection fault injection affects others | Random schemas/IDs, identify own listener PID, terminate only that PID. |
| PR overlap causes conflicts or violates ownership | Comment with authorization before edits; rebase current main and repeat ownership inspection. |
| PostgreSQL unavailable locally | Prepare isolated services; report unavailable validation explicitly, never treat skipped tests as proof. |

## Approval and publishing

The coordinator presents these saved files for user approval before development,
as required by the invoked `agent-loop`. Resolve the external-comment permission
constraint if the overlapping PRs still remain open.

After all tasks and both review axes have zero findings, publisher follows the
pull-request skill: review status, stage explicit new paths and tracked intended
changes, Conventional Commit referencing #129, push branch and create PR.
PR title is conventional and ≤72 characters; body has `What`, `Why`,
`How to verify`, `Refs`, with real PostgreSQL evidence and `Refs #129`.
No closing keywords or Development-panel link. Rebase before review; attach the
created PR to this chat. Merge/issue closure stays with the established review
and tech-lead acceptance workflow; a published PR alone is not issue acceptance.
