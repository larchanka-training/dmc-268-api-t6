# PR #121 review fixes: acceptance checklist

Scope, architecture, risks, and review references are recorded in
[the implementation plan](121-review-plan.md). This checklist records implemented
acceptance criteria and their verification; current execution results belong in
[PR #121](https://github.com/larchanka-training/dmc-268-api-t6/pull/121).

## Receipt recovery

- [x] Successful supersession waiting resets consecutive failures to zero;
  alternating failures/waits allow the new head to queue after its predecessor.
- [x] Three consecutive genuine dispatch failures still finalize without replay.
- [x] Continued waiting expires at the inclusive one-hour boundary measured from
  original persisted receipt time, including downtime and restart.
- [x] Expiry marks the receipt failed and clears retry, claim, and lease; it never
  replays or enqueues after the predecessor completes. An already-terminal
  blocker still permits recovery after the deadline.
- [x] Token-guarded reset/expiry only flush; stale owners raise and uncommitted
  updates roll back. The age-expiry WARNING appears only after successful commit.
- [x] Pending waits resist finished-receipt purge; expired receipts are eligible
  for retention.

**Verification:** `tests/test_github_webhook_delivery.py` and
`tests/test_webhook_run_trigger_postgres.py` cover exact age boundaries,
consecutive failures, persisted retry state, rollback, replacement tokens,
retention, and post-commit WARNING. Preserve signed-webhook production composition,
restart/redelivery/concurrent replay, waits beyond three polls, one-active-Run,
current-head/label/lifecycle/repository eligibility, live CI, and outbox coverage.

## Trigger contract and no-CI contention

- [x] Handler methods and `_trigger_result` accept `RunTriggerOutcome` only;
  fakes return structured outcomes and retain detail/action rendering.
- [x] Any temporarily waiting CI target keeps a multi-target receipt retryable.
- [x] No-CI `DEFERRED` contention remains selectable and later enqueues without
  a head or label change; terminal non-enqueue exclusions remain intact.
- [x] GitHub calls stay outside DB transactions; repositories only flush.
- [x] No dependencies, migrations, added provider calls, or #132 feature changes.

**Verification:** `tests/test_github_installation_dispatch.py`,
`tests/test_github_pr_projection.py`, `tests/test_sweep_no_ci.py`, trigger
regressions, and mypy verify the contract, rendering, contention, and boundaries.

## Sweep accounting and portable documentation

- [x] Expired waits contribute to handled/deferred counts but never ordinary
  `deferred for good`; ordinary final deferrals still contribute to K.
- [x] A sweep with one expired wait and two ordinary final deferrals returns
  literal `3` and logs `GitHub webhook sweep: 3 handled, 3 deferred, 2 deferred for good`.
  It asserts the wait failed, both ordinary receipts finalized as deferred, and
  all three cleared retry/claim/lease and cannot replay.
- [x] The asymmetric regression independently rejects returning the expiry flag
  instead of the ordinary-final flag, without a surviving production change.
- [x] Worker/pipeline docs describe downtime, cached CI/no-CI exclusion, a new
  push/label launch opportunity, the predecessor's check-run promise, and
  WARNING-only expiry visibility, retaining the existing mechanism.
- [x] Plan/checklist preserve architecture, scenarios, review/issue links, and
  portable verification commands.

**Verification:** run the mixed-sweep regression and existing delivery suite;
compare [WEBHOOK_WORKER.md](../WEBHOOK_WORKER.md) and
[PIPELINE_SPEC.md](../PIPELINE_SPEC.md) with trigger/sweep behavior. Read both
planning records independently and check portability. Mutation proof and execution
logs stay outside these records.

## Final verification and later merge prerequisites

Run `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy .`, and
`uv run pytest` against disposable isolated PostgreSQL/RabbitMQ configured via
`TEST_DATABASE_URL` and `TEST_RABBITMQ_URL`. Require zero integration skips,
including #131 notification coverage, and independent Standards and Spec reviews.
See [TEST_PLAN.md](../TEST_PLAN.md) for the test strategy. Record current results
in the PR's `What` / `Why` / `How to verify` / `Refs` description.

- [ ] Merge prerequisite: synchronize with current `main` and pass final checks.
- [ ] Merge prerequisite: obtain an approving review on the current head.
- [ ] Merge prerequisite: review owners resolve all threads; the tech lead owns
  issue closure.
- [ ] Integration prerequisite: [PR #132](https://github.com/larchanka-training/dmc-268-api-t6/pull/132)
  integrates last and preserves structured retry intent.
