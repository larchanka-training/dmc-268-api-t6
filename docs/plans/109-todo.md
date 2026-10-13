# Task List: durable superseded-head enqueue (#109)

Tasks 1–6 and implementation checkpoints are complete. Publication, GitHub review/merge,
tech-lead verification, and resource cleanup remain pending with their owners. Follow red → green at public seams; do not batch every test
before implementation. Plan and proposed seams: [109-plan.md](109-plan.md).

## Task 1: demonstrate the PostgreSQL race

**Description:** Extend the production-composition PG webhook test harness with a
replay-without-delivery path and permanent A→B race/control tests. Prefer signed
HTTP webhook receipt ingress, while retaining actual PG/application composition.

**Acceptance criteria:**

- [x] A is genuinely `running` when synchronize B requests its cancellation; B CI
      succeeds and is received before A becomes `cancelled/superseded`.
- [x] No additional receipt/event arrives after cancellation; replay must eventually
      produce exactly one queued/published B Run. This assertion fails on base main.
- [x] The identical control with A already terminal before B CI yields `enqueued`
      and one B Run; the test reads literal head/state/count/publication assertions.

**Verification:** Run `uv run pytest tests/test_webhook_run_trigger_postgres.py -v -rs`
against the disposable database. Save the expected RED and passing control evidence;
zero skips. Check formatting/types of test changes. No production code in this task.

**Evidence:** PostgreSQL 17 disposable database `dmc268_issue109_tests`; signed
HTTP ingress through the real app and production receiver composition. Command
`TEST_DATABASE_URL=<disposable-db-url> uv run pytest tests/test_webhook_run_trigger_postgres.py -v -rs`
produced **1 expected failure, 13 passed, zero skips**. Race fails at the literal
B Run assertion after cancellation/replay; terminal control logs `processed_ci`
and `enqueued`. RED log: `/private/tmp/issue109-task1-red.log`. No production code
changed during Task 1. The Run model names its supersession field `error_code`;
the fixture finalization uses `error_code='superseded'` (no `cancel_reason` column).

**Dependencies:** plan/seam approval; disposable PostgreSQL 17; ownership check.

**Files likely touched:** `tests/test_webhook_run_trigger_postgres.py`;
`docs/plans/109-todo.md` (evidence only).

**Estimated scope:** Small, 1–2 files.

## Task 2: expose temporary supersession through typed outcomes

**Description:** Test and implement a typed enqueue result for an active old head
whose cancellation is pending, then carry that retry intent to delivery dispatch.
Keep final log strings compatible while retry is represented by typed data.

**Acceptance criteria:**

- [x] Old different-head running/publishing Run with cancellation requested yields
      a temporary outcome, without inserting B or relaxing active uniqueness;
      same-head active and `head_already_reviewed` remain final duplicates.
- [x] Trigger/dispatcher receive structured retry intent and produce a dedicated
      retryable dispatch result; existing action/outcome detail formatting survives.
- [x] A CI event with several PR targets waits if any target is temporarily blocked,
      including when another target was enqueued or is already reviewed.

**Verification:** Add one failing case at a time in `tests/test_github_run_trigger.py`,
then minimal production behavior. Run that file and existing dispatcher/full-path
tests. If structured contract assertions require more files, split adapter test
updates into a small sequential subtask rather than broad test cleanup.

**Evidence:** Both PG running/publishing blocker cases were RED (`duplicate`
instead of `deferred`), then GREEN after returning typed blocker data under the
existing PR lock. Extended PG controls cover same head with/without cancellation
and old head without cancellation; all remain final `duplicate (active_run)`.
Multi-target aggregation was RED (plain string result), then GREEN; both target
orders preserve retry beside enqueued and head-already-reviewed targets.
Dispatcher PR/label/CI retry tests were RED (final status), then GREEN.

`TEST_DATABASE_URL=<disposable-db-url> uv run pytest tests/test_github_run_trigger.py tests/test_github_installation_dispatch.py tests/test_github_webhook_delivery.py tests/test_webhook_full_path.py -q -rs`
passed **292 tests, zero skips**. Ruff lint, format check (346 files), and mypy
(343 source files) pass. Contract adaptation was sequential: blocker/store,
trigger with existing log assertions, then dispatcher tests/adapter; legacy
`str | None` doubles remain final outcomes. No index, migration, active-state,
worker-finalization, or PR #108 owned document changes.

**Dependencies:** Task 1's observed RED.

**Files likely touched:**

- `app/modules/reviews/application/try_enqueue_webhook_run.py`
- `app/modules/reviews/infrastructure/webhook_runs.py`
- `app/modules/reviews/application/trigger_from_delivery.py`
- `app/modules/integrations/webhooks/application/github_installation_dispatch.py`
- `tests/test_github_run_trigger.py`

**Estimated scope:** Medium, up to 5 files per focused subtask.

## Checkpoint A

- [x] Control PG scenario passes, race failure is recorded.
- [x] Typed supersession behavior and existing duplicate/state-change tests pass.
- [x] No change to Run indexes or cancellation finalization semantics.

Checkpoint A deliberately retains the original race RED until Task 3:
`/private/tmp/issue109-checkpoint-a-pg.log` records **1 expected failure, 13 passed,
zero skips**, now logging `deferred_run_trigger` with `deferred (active_run)`.
Full pytest gates are due at final completion; the four-file focused suite and
static checks above are the checkpoint evidence.

## Task 3: persist retryable waiting in the receipt

**Description:** Add a receipt release for temporary Run contention and let the
receiver choose it from the typed dispatch result. Reuse the existing worker poll,
lease recovery, and pending selection; no new table/migration or broker consumer.

**Acceptance criteria:**

- [x] A deferred receipt stores `retry_after`, clears lease/token, stays unprojected
      and nonterminal, and remains retryable after more than three waiting polls.
- [x] Receipt UoW commits this state; repository only flushes. Lost claim raises;
      current generic deferral and dispatch-failure attempt limits still pass.
- [x] The Task 1 race turns GREEN through replay alone after A becomes terminal;
      GitHub/queue network calls remain outside receipt/enqueue DB transactions.

**Verification:** RED/GREEN receiver unit and PG persistence tests, then run the
Task 1 regression and control. Test the release through public receipt methods and
persisted rows; use fake clock/due retry state instead of long wall-clock sleeps.

**Evidence:** Receiver RED showed the deferred receipt incorrectly marked projected;
PG store RED showed missing release. After minimal implementation, command
`TEST_DATABASE_URL=<disposable-db-url> uv run pytest tests/test_github_webhook_delivery.py tests/test_webhook_run_trigger_postgres.py tests/test_webhook_full_path.py -q -rs`
passed **172 tests, zero skips** (`/private/tmp/issue109-task3-green.log`). Public
receipt operation PG checks prove rollback without commit, lost-token rejection,
lease-expiry replacement, persisted 30-second due retry, zero generic attempts,
four retries, no terminal markers and exclusion from purge. The original signed
HTTP race/control are both GREEN. Existing three-attempt generic/failure cases pass.

**Dependencies:** Task 2.

**Files likely touched:**

- `app/modules/integrations/webhooks/application/receive_github_delivery.py`
- `app/modules/integrations/webhooks/infrastructure/github_webhook_receipts.py`
- `tests/test_github_webhook_delivery.py`
- `tests/test_webhook_full_path.py` (receipt fake gains the port operation)
- `tests/test_webhook_run_trigger_postgres.py`

**Estimated scope:** Medium, 3–5 files.

## Task 4: prove restart and redelivery durability

**Description:** Test recovery across reconstructed worker resources, repeated CI
delivery IDs, distinct duplicate CI receipts, and concurrent receipt processing.

**Acceptance criteria:**

- [x] Waiting survives at least four polls and rebuilding receiver/resources from
      the same DB, with no new event after A's terminal transition.
- [x] Same-ID redelivery and distinct equivalent CI receipts yield one B Run and
      at most one active Run on the PR; completed B replay reports head reviewed.
- [x] Claim expiry/lost-token and crash/publication-pending recovery preserve a
      durable path to B. Confirmed publisher behavior is checked by Run ID; repeat
      publication may be idempotent, but repeat Run insertion is forbidden.

**Verification:** Add PG scenarios through the production composed receiver and
existing public outbox replay. Run focused PG/receipt/run-trigger suites with zero
PG skips. Reuse existing concurrency and publication tests where they already prove
the required invariant rather than creating implementation-mirroring duplicates.

**Evidence:**
`TEST_DATABASE_URL=<disposable-db-url> uv run pytest tests/test_webhook_run_trigger_postgres.py -k survives_restart -v -rs`
passed **2 PG scenarios, zero skips** (`/private/tmp/issue109-task4-pg.log`). Both
confirmed-publication and broker-failure/outbox-recovery paths rebuild engine,
HTTP client, resources, and composed receiver, retain due retry across restart,
wait four polls, deduplicate same-ID intake, persist distinct duplicate CI receipts,
and concurrently replay to exactly one B Run/one active Run. The latter path
reconstructs again and recovers the same Run through public
`replay_pending_publications`; a second publication replay has no pending work.
Completed B redelivery reports `head_already_reviewed`. Existing PG exclusive
claim/lease-expiry test and Task 3 token/rollback test cover crash-before-finalize
reclaim; existing publication test covers failed confirm with repeated same Run ID.
No additional production changes were needed for these already-preserved invariants.

**Dependencies:** Task 3.

**Files likely touched:** `tests/test_webhook_run_trigger_postgres.py`;
`tests/test_github_webhook_delivery.py`; `tests/test_github_run_trigger.py`;
`tests/test_webhook_worker_publication.py` only if existing recovery coverage has a gap.

**Estimated scope:** Medium, 2–4 files.

## Checkpoint B

- [x] Genuine PG race/control tests pass with no extra webhook event.
- [x] Restart/redelivery and >3 waiting polls pass; active uniqueness is retained.
- [x] Existing generic retry/failure and Run-publication behavior remains green.

## Task 5: prevent stale deferred launches

**Description:** Reproduce state changes while B is durably waiting, then release A
and replay. Verify current candidate and CI govern every deferred insertion.

**Acceptance criteria:**

- [x] Removing `ai-review` or closing PR through real webhook projection, or
      disabling the repository via its persisted setting, prevents B's launch.
- [x] Synchronize to head C while B waits cannot enqueue obsolete B. A C launch,
      if eligible, uses C's own current CI; B's completed suite cannot approve C.
- [x] CI becoming red/pending before retry blocks B; stale/removed targets retire
      the old receipt, and the tested modes include `always`, `auto`, and `never`
      according to their existing semantics (never deliberately ignores CI).

**Verification:** RED/GREEN PG parameterized invalidation cases with current
GitHub HTTP fixture state. Include an intervening candidate change during CI I/O
via the existing eligibility/locked-recheck tests. Run PG and CI eligibility suites;
assert literal heads/counts, not outcomes calculated from implementation constants.

**Evidence:**
`TEST_DATABASE_URL=<disposable-db-url> uv run pytest tests/test_webhook_run_trigger_postgres.py -k 'waiting_ci_receipt or newer_head' -v -rs`
passed **18 PG cases, zero skips** (`/private/tmp/issue109-task5-pg.log`). Literal
head/count/state assertions cover `always`, `auto`, and `never`, removed label and
closed PR through real webhook projection, persisted repository disabling, fresh
red/pending CI, and C push while B waits. Only C's own CI success enables it in
modes that consult CI; `never` intentionally ignores CI. Strengthened cases replay
old synchronize and labeled B receipts against authoritative current PR/label
state, ensuring they cannot restore B, removed label, or an open PR. No production
eligibility changes were needed. Existing
`test_pending_ci_never_times_out_and_stale_or_disabled_state_blocks` proves head
and enabled change during CI I/O; `test_a_candidate_that_moved_since_the_decision_is_stale_with_state_changed`
proves the enqueue lock recheck. Final focused rerun includes these suites and strengthened PR/label replay cases:
`TEST_DATABASE_URL=<disposable-db-url> uv run pytest tests/test_webhook_run_trigger_postgres.py tests/test_github_run_trigger.py tests/test_github_webhook_delivery.py tests/test_ci_eligibility.py -v -rs`
passed **304 tests, zero skips** (`/private/tmp/issue109-final-focused.log`), including
all **34** production-composition PG webhook cases.

**Dependencies:** Tasks 3–4.

**Files likely touched:** `tests/test_webhook_run_trigger_postgres.py`;
`tests/test_github_run_trigger.py`; `tests/test_ci_eligibility.py`;
the existing target/eligibility adapter only if a failing test exposes a genuine gap
(split any fix with more than 5 touched files).

**Estimated scope:** Medium, 2–5 files.

## Task 6: document and validate the final behavior

**Description:** Document the retry outcome and timing in the unowned webhook
worker guide, record AC evidence, and complete the repository gates before review.

**Acceptance criteria:**

- [x] `docs/WEBHOOK_WORKER.md` explains temporary active-run waiting, retry timing,
      durable restart behavior, current-state checks, and the new log outcome.
- [x] All four mandatory API gates pass and the issue's PG cases run with zero skips.
      Review examines security/correctness/transaction boundaries and all issue AC.
- [x] Evidence records test commands/results and any runtime limitations; no PR #108
      owned docs are modified. Publication remains the coordinator/publisher phase.

**Verification:**

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run pytest
```

Keep the disposable PG URL set; use an isolated RabbitMQ test vhost for the full
integration suite where available. Record focused PG `-v -rs` output explicitly.

**Dependencies:** Tasks 1–5; current ownership check of `docs/WEBHOOK_WORKER.md`.

**Files likely touched:** `docs/WEBHOOK_WORKER.md`; `docs/plans/109-plan.md`;
`docs/plans/109-todo.md`.

**Estimated scope:** Medium, 3 files.

Task 6 static gates passed with their exact commands: `uv run ruff check .`,
`uv run ruff format --check .` (**346 files**) and `uv run mypy .` (**343 source
files**). Full `uv run pytest` with `TEST_DATABASE_URL=<disposable-db-url>` and
`TEST_RABBITMQ_URL=<isolated-task-vhost-url>` passed **2426 tests, zero skips** in
**226.85 seconds**, with two existing Starlette/httpx/anyio deprecation warnings.
Log: `/private/tmp/issue109-full-gates.log`. No new dependency or migration was
introduced. Both final independent Standards and Spec reviews returned **zero
findings**; the coordinator rechecked main (`92250a1`) and PR ownership (#108 still
has no overlap). No code changed after the focused run/review freeze; plan/checklist
evidence was finalized after verification. `git diff --check` is clean.

## Checkpoint C

- [x] AC1: PostgreSQL real webhook race and terminal-A control proven.
- [x] AC2: restart/redelivery, one active Run, head-reviewed semantics proven.
- [x] AC3: current head/PR/label/enabled/CI invalidation proven.
- [x] Gates and independent Standards/Spec review green; ready for publisher.
- [x] Disposable resources cleaned up without touching pre-existing stack data.
      Coordinator dropped only database `dmc268_issue109_tests` in existing
      `dmc268-webhook-local-postgres-1` (`DROP DATABASE`, exit 0), and removed only
      task-created `dmc268-issue109-rabbitmq`/vhost `issue109` and its anonymous volume
      (`docker rm -fv`, exit 0). The pre-existing PostgreSQL stack remains untouched.

## Publication follow-through (coordinator / publisher / tech lead)

- [x] Independent reviews passed; publisher fetched/rebased on current `main`
      (`92250a1`), unchanged base/implementation, and rechecked PR #108 ownership.
- [ ] Conventional Commit with `Refs #109` and push the issue branch.
- [ ] PR title ≤72 characters; body What / Why / How to verify / Refs; no closing
      keywords or Development-panel link; attach created PR to the chat.
- [ ] GitHub current-head approving review and all review threads resolved before merge
      (independent implementation reviews above do not replace this external approval).
- [ ] Tech lead verifies acceptance and updates tracker #106 / board 12; no automatic
      issue closure by this implementation loop.


Handoff: implementation, reviews, gates, rebase and disposable-resource cleanup are
complete. The publisher prepared the Conventional Commit and draft PR body. Commit,
push and PR creation are the remaining publication operations at this snapshot;
external approval/merge and tech-lead acceptance/board updates remain pending above.
No issue was closed.
