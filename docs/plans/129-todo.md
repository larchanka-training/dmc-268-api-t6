# Tasks: webhook-worker PostgreSQL wake-ups (#129)

Status: user approved implementation on 2026-10-10; All implementation tasks complete; fresh final review cleared Standards 0 / Spec 0; latest-main gates passed. Publishing follows below; human review/merge/issue acceptance remain pending.
Design and ownership constraints: [129-plan.md](129-plan.md).
Every task uses RED → GREEN → refactor and a fresh Standards/Spec review.

## Task 1: Notify after committing a new receipt

**Description:** Add one channel/helper and call it after the receipt store actually
inserts a delivery, using the same session/transaction as the receipt insert.

**Acceptance criteria:**

- [x] A real LISTEN connection receives a work signal only after a new receipt commits;
  another session observes that receipt once signaled.
- [x] Uncommitted or rolled-back inserts produce no delivered signal/visible receipt.
- [x] Duplicate delivery insertion preserves `False`/duplicate behavior and produces
  no fresh work notification; the store does not commit.

**Verification / TDD:**

- [x] Recheck file ownership; coordinate/comment on PR #121 with user authorization
  before editing its receipt store if still open.
- [x] RED: real PostgreSQL tests fail on current missing notification; add explicit
  listener readiness and bounded waits, not fixed startup sleeps.
- [x] GREEN: implement minimal transaction-bound helper/call; verify rollback and
  duplicate controls alongside commit, then run all mandatory gates.

**Dependencies:** None; approved plan, isolated `fix/129-webhook-worker-notify`
checkout from current main and PostgreSQL baseline are prerequisites.

**Files likely touched (3):**

- `app/modules/integrations/webhooks/infrastructure/webhook_notifications.py` (new)
- `app/modules/integrations/webhooks/infrastructure/github_webhook_receipts.py`
- `tests/test_webhook_work_notifications_postgres.py` (new)

**Estimated scope:** Medium.

**Execution evidence — 2026-10-10:**

- Branch `fix/129-webhook-worker-notify`, isolated managed checkout from
  `7325fae976801ca33ef3999c8cbe5aa2aa37ea05`; original checkout unchanged.
- Ownership comments in PR #121/#124 were posted by the coordinator with user approval.
- Disposable compose project `dmc268-129-tests`: PostgreSQL 17 on port 5434,
  RabbitMQ on port 5673 with dedicated `/test` vhost; random migrated schemas.
- With `TEST_DATABASE_URL=postgresql+psycopg://app:app@localhost:5434/app`, baseline
  `uv run pytest tests/test_webhook_worker_revival.py tests/test_webhook_worker_publication.py
  tests/test_installation_linking.py tests/test_run_stream_postgres.py
  tests/test_webhook_deferred_receipts_postgres.py -rs`: **37 passed, zero skipped**.
- RED: first `test_new_receipt_signals_only_after_commit` failed with `TimeoutError`
  waiting five seconds for the absent signal; listener was ready and uncommitted
  receipt visibility/absence assertions passed.
- GREEN: `uv run pytest tests/test_webhook_work_notifications_postgres.py -q`:
  **3 passed, zero skipped** (commit visibility, rollback, duplicate negative control).
- `uv run ruff check .`: passed; `uv run ruff format --check .`: 348 files formatted;
  `uv run mypy .`: passed, 345 source files; `git diff --check`: clean.
- `TEST_DATABASE_URL=postgresql+psycopg://app:app@localhost:5434/app
  TEST_RABBITMQ_URL=amqp://app:app@localhost:5673/test uv run pytest`:
  **2395 passed, zero skipped**, 165.28 seconds; two existing dependency
  deprecation warnings from Starlette/FastAPI test client.
- Fresh independent Task 1 review: **Standards 0 / Spec 0**; coordinator authorized Task 2.
- No dependency, migration, commit or push added during Task 1.

## Task 2: Notify OAuth reactivation within reconciliation

**Description:** Signal available work when `wake_receipts` updates receipts, still
inside the use case's final installation reconciliation transaction.

**Acceptance criteria:**

- [x] A deferred receipt is revived by `LinkGitHubInstallations` with real UoWs and
  fake GitHub reads, and a signal becomes visible only after reconciliation commits.
- [x] Rolled-back reactivation retains original defer/attempt/retry state and emits
  no notification; a no-match reactivation produces no work signal.
- [x] Current reactivation eligibility, permanent failure behavior, claim ownership
  and GitHub-outside-transaction boundary remain intact.

**Verification / TDD:**

- [x] RED: assert receipt state and commit-bound signal through the application use
  case; add explicit rollback and no-match controls.
- [x] GREEN: reuse the helper after matched update, using rowcount without retrieving
  IDs, expanding predicates or adding commit calls.
- [x] Run new PostgreSQL notification tests, existing `test_installation_linking.py`
  and `test_webhook_deferred_receipts_postgres.py`, plus mandatory gates.

**Dependencies:** Task 1.

**Files likely touched (2):**

- `app/modules/workspaces/infrastructure/github_installation_links.py`
- `tests/test_webhook_work_notifications_postgres.py`

**Estimated scope:** Small.

**Execution evidence — 2026-10-10:**

- RED: `TEST_DATABASE_URL=postgresql+psycopg://app:app@localhost:5434/app uv run
  pytest tests/test_webhook_work_notifications_postgres.py -k oauth -q -x` failed
  with `TimeoutError` waiting five seconds for the missing reactivation signal.
  Actual `LinkGitHubInstallations` committed reconciliation and revived the receipt;
  precommit barrier proved its retry/defer state remained invisible beforehand.
- GREEN first OAuth commit test: **1 passed**. Expanded tests use real reconciliation
  UoWs and a fake GitHub boundary that requires each UoW to be closed during reads.
  Final-commit barrier proves no precommit signal; rollback preserves original
  attempt/defer/retry values and rolls back workspace/application state. No-match
  reconciliation commits without signaling; permanent failure and existing
  claim-token/lease values remain intact and cannot be replayed.
- `TEST_DATABASE_URL=postgresql+psycopg://app:app@localhost:5434/app uv run pytest
  tests/test_webhook_work_notifications_postgres.py tests/test_installation_linking.py
  tests/test_webhook_deferred_receipts_postgres.py -rs`: **32 passed, zero skipped**.
- `uv run ruff check .`: passed (one import-order issue corrected);
  `uv run ruff format --check .`: 348 files formatted;
  `uv run mypy .`: passed, 345 source files; `git diff --check`: clean.
- `TEST_DATABASE_URL=postgresql+psycopg://app:app@localhost:5434/app
  TEST_RABBITMQ_URL=amqp://app:app@localhost:5673/test uv run pytest`:
  **2400 passed, zero skipped**, 149.49 seconds; two existing dependency warnings.
  Full output retained at `/private/tmp/129-task2-pytest.log`.
- Implementation only observes existing UPDATE rowcount and sends the shared hint
  for matched receipts. No predicate, transaction boundary, dependency or migration
  changed; no Task 3–5 implementation, commit or push performed.

- Fresh independent Task 2 review: **Standards 0 / Spec 0**; reviewer approved the
  transaction checkpoint, coordinator authorized Task 3.

## Checkpoint: transaction guarantees

- [x] Task 1 and 2 tests pass against real PostgreSQL with zero skips.
- [x] Existing receipt/linking behavior and mandatory gates pass.
- [x] Fresh review has zero Standards/Spec findings; coordinator can proceed within
  the approved plan without another routine approval gate.

## Task 3: Reconnecting listener sets a bounded wake signal

**Description:** Add a dedicated worker LISTEN lifecycle following the existing
psycopg autocommit listener pattern and force a scan after successful registration.

**Acceptance criteria:**

- [x] Initial LISTEN, each reconnect and received notifications set a shared event;
  repeated notifications do not create per-message processing tasks.
- [x] A lost connection is logged and retried; cancellation propagates and the
  connection/task closes on notification waiting or reconnect backoff.
- [x] URL conversion preserves connection options; listener performs no GitHub calls
  and holds no queue processing transaction.

**Verification / TDD:**

- [x] RED: deterministic tests with a controlled connection boundary expose missing
  readiness, reconnect, cancellation and cleanup behavior.
- [x] GREEN: implement dedicated connection/task lifecycle; use real PostgreSQL
  connectivity in Task 5 instead of relying on connection mocks alone.
- [x] Run focused listener tests and all mandatory gates.

**Dependencies:** Task 1 (shared channel contract).

**Files likely touched (2):**

- `app/bootstrap/webhook_work_listener.py` (new)
- `tests/test_webhook_work_listener.py` (new)

**Estimated scope:** Small.

**Execution evidence — 2026-10-10:**

- RED initial lifecycle test failed on the absent `webhook_work_listener` module.
  First GREEN slice registered a dedicated autocommit connection, set the event
  only after LISTEN, coalesced hints, preserved URL options and closed on cancel:
  **1 passed**.
- RED reconnect test then failed because a synthetic psycopg `OperationalError`
  escaped and no second LISTEN became ready within the two-second bound.
  GREEN adds logging, a fixed five-second production reconnect pause and the same
  post-LISTEN event on each connection. Portal listener unchanged.
- `uv run pytest tests/test_webhook_work_listener.py -q`: **7 passed** (0.30 seconds).
  Controlled third-party psycopg boundary covers readiness before/after LISTEN,
  repeated hints, initial connect/LISTEN errors, successful reconnect, and
  cancellation during notification wait, registration, setup and reconnect delay.
  Real PostgreSQL interruption/recovery remains the Task 5 integration seam.
- `uv run ruff check .`: passed; `uv run ruff format --check .`: 350 files formatted;
  `uv run mypy .`: passed, 347 source files; `git diff --check`: clean.
- `TEST_DATABASE_URL=postgresql+psycopg://app:app@localhost:5434/app
  TEST_RABBITMQ_URL=amqp://app:app@localhost:5673/test uv run pytest`:
  **2407 passed, zero skipped**, 162.03 seconds; two existing dependency warnings.
  Full output retained at `/private/tmp/129-task3-pytest.log`.
- Only dedicated listener and its boundary tests added, plus checklist evidence.
  No Task 4–5 code, dependency, migration, commit or push added.

- Fresh independent Task 3 review: **Standards 0 / Spec 0**; coordinator authorized
  Task 4 worker integration.

## Task 4: Wake the durable sweep without losing in-flight arrivals

**Description:** Wire a shared event/listener into the worker, replacing idle sleep
with an event wait while preserving production polling and maintenance behavior.

**Acceptance criteria:**

- [x] Startup scans immediately, a signal wakes an idle scan before its 30-second
  timeout, and a signal during processing survives the next transition to waiting.
- [x] Timeout still scans future-due retries and missed work; full batch 100 still
  causes immediate continuation; noisy notifications do not skip hourly maintenance.
- [x] Cancellation closes listener, publisher and database resources while heartbeat
  remains live during normal processing and recoverable listener outages.

**Verification / TDD:**

- [x] RED: controlled receiver and event barriers assert idle wake, arrival during
  scan, timeout, exact full-batch behavior, monotonic maintenance and cancellation.
- [x] GREEN: clear event before the database scan, wait with bounded timeout after
  processing, compose listener lifecycle and adapt existing worker tests as needed.
- [x] Run `tests/test_webhook_worker_notifications.py`, listener tests, revival and
  publication regressions; run all mandatory gates.

**Dependencies:** Task 3.

**Files likely touched (4):**

- `app/webhook_worker.py`
- `tests/test_webhook_worker_notifications.py` (new)
- `tests/test_webhook_worker_revival.py`
- `tests/test_webhook_worker_publication.py`

**Estimated scope:** Medium.

**Execution evidence — 2026-10-10:**

- RED: with the new event argument established, existing fixed-sleep loop never
  reached the event wait within the one-second bound. GREEN starts scanning at
  once, clears the event before scanning and waits after processing with the
  unchanged **30-second** production timeout. Exact batch 100 yields before its
  next immediate scan.
- Worker composition/lifecycle RED: no LISTEN task became ready and the old
  composition lacked the shared-event argument. GREEN composes one event and
  one listener with sweep/heartbeat in the existing `TaskGroup`.
- `uv run pytest tests/test_webhook_worker_notifications.py -q`: **8 passed**.
  Covers prompt idle wake, hints during blocked dispatch, timeout without hints,
  exact 99/100/101 behavior and fairness, startup/hourly monotonic maintenance
  under frequent signals, startup scan/heartbeat before LISTEN readiness, and
  shutdown waiting for connection closure before publisher/database disposal.
- Existing startup-maintenance regression now uses public receiver/reviver/event
  boundaries instead of patching the loop's helpers or replacing asyncio.
- `uv run pytest tests/test_webhook_worker_notifications.py
  tests/test_webhook_work_listener.py tests/test_webhook_worker_revival.py
  tests/test_webhook_worker_publication.py -q`: **27 passed**.
- `uv run ruff check .`: passed; `uv run ruff format --check .`: 351 files formatted;
  `uv run mypy .`: passed, 348 source files; `git diff --check`: clean.
- `TEST_DATABASE_URL=postgresql+psycopg://app:app@localhost:5434/app
  TEST_RABBITMQ_URL=amqp://app:app@localhost:5673/test uv run pytest`:
  **2415 passed, zero skipped**, 159.57 seconds; two existing dependency warnings.
  Full output retained at `/private/tmp/129-task4-pytest.log`.
- No Task 5 code, dependency, migration, commit or push added. Actual PostgreSQL
  notification interruption/recovery and worker guidance remain Task 5.

- Fresh independent Task 4 review: **Standards 0 / Spec 0**; coordinator authorized
  Task 5 real PostgreSQL recovery verification and documentation.

## Checkpoint: resilient worker loop

- [x] Transaction, listener and worker behavior verified; all mandatory gates pass.
- [x] Review confirms no clear-after-scan race, unbounded notification tasks, or
  transactional network calls; zero Standards/Spec findings.

## Task 5: Prove recovery and update worker guidance

**Description:** Exercise actual listener, actual receipt/link UoWs and worker sweep
against PostgreSQL; update the existing worker description and polling smoke recipe.

**Acceptance criteria:**

- [x] New receipt/OAuth commits process within a bounded deadline below 30 seconds;
  both rollback paths never dispatch, and a receipt arriving during a blocked
  dispatch processes without waiting for the fallback timeout.
- [x] Startup backlog, insertion while the listener is disconnected, forced reconnect
  and deliberately missed notification recover from the durable queue. Repeated
  signals/two workers retain existing claim/lease and idempotent onboarding behavior.
- [x] Retry/maintenance/shutdown regressions pass; documentation explains transient
  signals, post-LISTEN scan, 30-second fallback and latency limits accurately.

**Verification / TDD:**

- [x] Recheck docs ownership; obtain authorization and comment in PR #121 and #124
  before editing `WEBHOOK_WORKER.md` if those PRs still own it.
- [x] Coverage: add missing recovery/race integration cases for the already reviewed behavior; use migrated random
  schemas and distinct delivery IDs, real LISTEN readiness and barriers.
- [x] Fault injection terminates only this test listener's backend PID; wait for
  confirmed disconnect, insert during outage, then confirm successful LISTEN and
  processing. For fallback suppress notification delivery and verify timeout scan.
- [x] Assert persisted processing state and dispatcher calls, not global signal counts;
  verify claim contention and repeat sweep causes no duplicate onboarding.
- [x] Run focused PostgreSQL suites and `uv run pytest -m integration -rs` with
  `TEST_DATABASE_URL`/dedicated `TEST_RABBITMQ_URL`, require zero skips, and complete
  all four mandatory gates. Record command results for the PR.

**Dependencies:** Tasks 2 and 4.

**Files likely touched (2):**

- `tests/test_webhook_worker_notifications_postgres.py` (new)
- `docs/WEBHOOK_WORKER.md`

**Estimated scope:** Small-to-medium; any uncovered implementation fix returns to
the owning earlier task and fresh review rather than expanding this slice unchecked.

**Execution evidence — 2026-10-10:**

- Added recovery coverage for the already implemented/reviewed behavior; no
  artificial RED or production regression was introduced. Initial run passed
  eight real PostgreSQL cases, with one harness failure from assuming which
  worker won a claim. The harness now observes the actual claimant. An atomic
  future-retry fixture was corrected to bind its JSON payload explicitly.
- **Nine real worker/PG cases** verify insertion commit and uncommitted visibility,
  insertion rollback, OAuth commit and rollback, startup backlog and actual
  listener closure, real listener backend termination/offline insertion/reconnect,
  ignored wake hints plus future-due retry fallback, arrivals during blocked
  dispatch, and two workers/repeated hints retaining one claim/dispatch.
- Production LISTEN completion and explicit event/dispatch/commit barriers supply
  synchronization; no fixed startup sleep. State is read from durable receipt
  rows. Normal processing uses a five-second bound; real reconnect uses the
  production five-second backoff and an eight-second reconnect bound. Fault
  injection terminates only the UUID-named test listener's identified backend PID.
- Ownership comments in PR #121/#124 were already authorized and posted; their
  receipt/evaluation documentation content is preserved. Worker guidance now
  explains transient hints, post-LISTEN scans, polling/retry/maintenance/shutdown
  and latency limits; local smoke polls for a published Run instead of sleeping
  for the former worker interval.
- Refreshed main and safely rebased before final checks using named `stash -u`
  backup, then applied it with no conflicts. Final branch/base is
  **`08d56c41f1a3f841afbc5950fc01b8277920e21f`** (`origin/main`), replacing the
  historical planning snapshot. All intended tracked/untracked changes restored;
  backup `codex-129-before-main-08d56c4` retained until publisher verification.
- `TEST_DATABASE_URL=postgresql+psycopg://app:app@localhost:5434/app uv run pytest
  tests/test_webhook_worker_notifications_postgres.py
  tests/test_webhook_work_notifications_postgres.py
  tests/test_webhook_deferred_receipts_postgres.py tests/test_installation_linking.py -rs`:
  **41 passed, zero skipped**, 22.47 seconds.
- `uv run ruff check .`: passed; `uv run ruff format --check .`: 352 files formatted;
  `uv run mypy .`: passed, 349 source files; `git diff --check`: clean.
- With the same database URL and dedicated
  `TEST_RABBITMQ_URL=amqp://app:app@localhost:5673/test`,
  `uv run pytest -m integration -rs`: **145 passed, zero skipped**, 2294 deselected,
  93.97 seconds; log `/private/tmp/129-task5-integration.log`.
- Same isolated URLs, `uv run pytest`: **2439 passed, zero skipped**, 169.77 seconds;
  log `/private/tmp/129-task5-pytest.log`. Both suites emitted only the two existing
  Starlette/FastAPI dependency deprecation warnings.
- No additional production code, dependency or migration in Task 5. No commits
  or pushes made. Final independent review/publishing remain for the coordinator.


**Final review correction — 2026-10-10:**

- Fresh final review reported **Standards 1 (P3) / Spec 0**; all issue acceptance
  criteria satisfied. The fixture import violated the documented requirement that
  each DB test file owns its environment check and skip.
- Extracted an undecorated `migrated_database` context manager for schema setup,
  migration and cleanup. Both PostgreSQL notification test files now have their
  own fixture that checks `TEST_DATABASE_URL` and calls `pytest.skip` locally.
  No new dependency, shared conftest, production change or unrelated test added.
- `env -u TEST_DATABASE_URL uv run pytest tests/test_webhook_work_notifications_postgres.py
  tests/test_webhook_worker_notifications_postgres.py -rs`:
  **17 expected skips**, 0.79 seconds, confirming the local optional-DB behavior.
- With the isolated PostgreSQL URL, the same two files:
  **17 passed, zero skipped**, 18.35 seconds.
- `uv run ruff check .`: passed; `uv run ruff format --check .`: 352 files formatted;
  `uv run mypy .`: passed, 349 source files; `git diff --check`: clean.
- The interrupted full correction run ended without a summary; it is not counted
  as passing evidence. A fresh independent re-review cleared **Standards 0 / Spec 0**.
- Publisher refreshed and rebased onto **`8cf3608d80d0d1cf79e56e333b50d6a4d2052ab8`**
  (`origin/main`). The tracked feature patch is byte-identical and all eight
  untracked feature files have identical SHA-256 hashes before/after the rebase.
- On this latest main: `uv run ruff check .` passed;
  `uv run ruff format --check .`: **353 files formatted**;
  `uv run mypy .`: **350 source files, no issues**; `git diff --check`: clean.
- Fresh full `uv run pytest`, with the same isolated PostgreSQL/RabbitMQ URLs:
  **2444 passed, zero skipped**, exit 0, **217.25 seconds**;
  log `/private/tmp/129-publisher-latest-main-pytest.log`. Only the two existing
  dependency warnings remain. The full suite includes all integration tests;
  prior dedicated integration checkpoint was **145 passed, zero skipped**.

## Checkpoint: publishing readiness

- [x] Every issue acceptance criterion has passing evidence, including real PostgreSQL.
- [x] All implementation task checkboxes completed; fresh Standards and Spec review has zero findings.
- [x] `uv run ruff check .` passed.
- [x] `uv run ruff format --check .` passed.
- [x] `uv run mypy .` passed.
- [x] `uv run pytest` passed.
- [x] Full integration suite passed with zero skips and isolated RabbitMQ vhost.
- [x] Refresh/rebase main and repeat gates if the diff changes.
- [ ] Publisher stages only #129 paths, makes an issue-referencing Conventional Commit,
  pushes and creates the PR via pull-request skill; no closing keywords/linkage.
- [ ] PR has What / Why / How to verify / Refs and validation evidence; attach its URL.
- [ ] One approve on current head and resolved review threads are prerequisites for
  merge; tech lead checks AC and closes the issue through the course workflow.
