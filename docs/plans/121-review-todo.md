# PR #121 review checklist

## Task 1: Rebase and establish baseline

**Acceptance criteria**

- [x] Rebase existing PR branch on `origin/main` (`46a9077`), preserving #131.
- [x] Use only `/private/tmp/pr121-fixes`; leave unrelated working copies alone.
- [x] Check overlapping ownership with #132 before touching additional owned files.

**Verification:** inspect rebased diff, record current head, run existing targeted
webhook regressions against disposable PostgreSQL.
**Dependencies:** none. **Scope:** no feature changes.

## Task 2: Make wait errors consecutive and waiting finite

**Acceptance criteria**

- [x] A successful supersession wait atomically resets failure attempts to zero;
  alternating failures/waits still let B queue after A completes.
- [x] A continued wait at `received_at + 1 hour` becomes failed with no retry or
  claim/lease; its post-commit WARNING identifies age expiry; it never replays.
- [x] Three consecutive dispatch failures still finalize; stale tokens raise;
  reset and expiry are flush-only and roll back without a use-case commit.

**Verification:** write and demonstrate red tests first, then green unit/PG
tests, including the exact one-hour boundary and original received timestamp.
Retain restart, concurrent replay, four-poll wait, one-active-Run, live eligibility,
and outbox regressions.
**Dependencies:** task 1.
**Files likely touched:**

- `app/modules/integrations/webhooks/application/receive_github_delivery.py`
- `app/modules/integrations/webhooks/infrastructure/github_webhook_receipts.py`
- `tests/test_github_webhook_delivery.py`
- `tests/test_webhook_run_trigger_postgres.py`

**Scope:** medium, four files; no migrations/dependencies.

## Task 3: Narrow the trigger outcome contract

**Acceptance criteria**

- [x] Handler methods and `_trigger_result` accept `RunTriggerOutcome` only.
- [x] All handler fakes return structured outcomes; final detail/action output
  and `DEFERRED_RUN_TRIGGER` intent remain unchanged.
- [x] CI multi-target aggregation keeps any waiting target retryable.

**Verification:** dispatcher and delivery/trigger tests plus mypy; new behavior
tests only if existing assertions do not verify the narrowed contract.
**Dependencies:** task 1; coordinate task 2's shared delivery-test fake.
**Files likely touched:** dispatcher, `tests/test_github_installation_dispatch.py`,
`tests/test_github_webhook_delivery.py`, and related full-path trigger fakes.
**Scope:** medium, at most five files.

## Task 4: Keep no-CI contention retryable

**Acceptance criteria**

- [x] `DEFERRED` does not exclude a selected no-CI candidate.
- [x] A later sweep queues it without a head or label change.
- [x] Existing final refusal/duplicate exclusion behavior stays covered.

**Verification:** fail a regression first by supplying a deferred enqueue
result, then pass after the sweep fix; run existing sweep integration scenarios.
**Dependencies:** task 1; independent of tasks 2/3.
**Files likely touched:** `app/modules/reviews/application/sweep_no_ci.py`,
`tests/test_sweep_no_ci.py` (the sweep application seam).
**Scope:** small, two files.

## Checkpoint: behavior fixes

- [x] Every new behavior has red/green evidence and the targeted suite passes.
- [x] GitHub calls remain outside transactions and repositories never commit.
- [x] No #132 installation-signature changes were incorporated.

## Task 5: Document, verify, and publish the review fixes

**Acceptance criteria**

- [x] Worker docs explain consecutive errors, one-hour continued-wait limit,
  post-expiry retention, recovery timing, and GitHub polling cost.
- [x] All four project gates pass with PostgreSQL/Rabbit integration enabled
  and zero integration skips on isolated services; final review finds no blockers.
- [x] Publication package includes current verification counts/rebase status and
  reviewer replies with commit-SHA evidence; publication evidence is recorded in
  existing PR #121, with thread resolution left to their owners.

**Verification:** `uv run ruff check .`, `uv run ruff format --check .`,
`uv run mypy .`, `uv run pytest`; use PostgreSQL `127.0.0.1:55640`, RabbitMQ
`127.0.0.1:55641`, and a disposable test vhost. Include #131 notification tests
on the isolated database. Recheck current main before review and before merge.
**Dependencies:** tasks 2–4.
**Files likely touched:** `docs/WEBHOOK_WORKER.md`, this plan/checklist, existing
PR metadata. **Scope:** small documentation and verification.

## Checkpoint: review-ready

- [x] PR #121 remains ahead of #132 in the planned merge order.
- [ ] Merge prerequisite: final push checks green; current-head approval obtained.
- [ ] Merge prerequisite: all review threads resolved by their owners; tech lead
  owns issue closure.

## Implementation evidence

- Rebased head: `0203fb2`, main base: `46a9077`. #132 coordination covers
  dispatcher/delivery full-path fakes; additional projection fake coordination:
  https://github.com/larchanka-training/dmc-268-api-t6/pull/132#issuecomment-6121927820.
- Counter RED: signed webhook → production composition → PostgreSQL failed with
  `attempt_count=1` after a successful wait (expected zero). GREEN: 1 passed,
  alternating three 502 errors/waits through restart queues B once after A completes,
  with the original three deliveries and one active Run.
- Age RED: exact `received_at + 1 hour` continued wait retained its retry instead
  of failing; recovered terminal-blocker control passed (1 failed, 1 passed).
  GREEN: reset/age/consecutive failures plus warning and persistence regressions
  8 passed. At 59:59 retry remains; at 1:00:00 failure clears retry/token/lease.
  A completed after expiry cannot enqueue B; already-terminal A still permits recovery.
  Stale replacement tokens fail both reset and expiry; uncommitted updates roll
  back including the previous counter/lease. Pending waits resist retention purge;
  expired receipts are purgeable. Failed finalize commit emits no expiry warning.
- Protocol GREEN: dispatcher, projection and receipt units 273 passed (10 PG tests
  deselected for this unit-only run); existing detail/action assertions retained.
- Sweep RED: `DEFERRED` excluded the selected candidate. GREEN: 7 passed; next
  sweep enqueues the same candidate without a head/label change.
- Targeted seven-file suite with PostgreSQL: 402 passed, 0 skipped in 53.06 s;
  explicit empty structured detail/action cases added afterward: 5 passed.
- Final gates: Ruff lint passed; format check 353 files unchanged; mypy 350
  source files clean; full PostgreSQL/Rabbit pytest **2494 passed, 0 skipped,
  2 warnings in 246.96 s**. Warnings are existing Starlette/httpx and anyio
  deprecations. Gate output summary: `/private/tmp/pr121-final-gates.log`.
- Parallel Standards and Spec reviews each returned zero findings on the final
  implementation. Publication updates existing PR #121 with the full description,
  P2 thread reply and P3 summary. GitHub records the published commit SHA and
  verification evidence. Reviewer thread resolution and current-head GitHub
  approval remain later reviewer/merge steps.
