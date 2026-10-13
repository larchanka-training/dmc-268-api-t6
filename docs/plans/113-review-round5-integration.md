# PR #132 round-five integration proof (#113)

Verified source checkpoint: `b4fb4a24c4ff8cbcf6256c289565283c51b80851`.
Combined main: `11ddfabf31a3345e6d21cdf57586281cb62cee7c`, including merged
#121 receipt retries and #126 rollback revision admission. Results below include
the uncommitted adaptation of `tests/test_receipt_installation_guard_postgres.py`.

## Combined trigger contract

`TriggerFromDelivery` preserves `RunTriggerOutcome.detail` and typed
`retry_required`, while its enqueuer protocol requires keyword-only
`expected_installation_external_id`. PR, label and CI paths forward the original
event installation ID. The dispatcher continues to select `DEFERRED_RUN_TRIGGER`
from typed retry intent.

An existing source-rebind regression reproduced the integration mismatch before
its adaptation: `uv run pytest -x -rs tests/test_receipt_installation_guard_postgres.py`
failed with `TypeError: argument of type 'RunTriggerOutcome' is not iterable`
(1 failed in 1.50s). Its assertions now inspect `.detail` and literal
`retry_required is False`; the rebind enqueuer fake requires the original
installation ID. Existing source-refusal, fresh-installation success, provider-call
and persisted-run assertions remain. No production change or new test was needed.

## Focused verification

`TEST_DATABASE_URL` pointed to a disposable PostgreSQL 17 database. GitHub HTTP
and publication boundaries use the existing test transports/recorders. The tests
exercise real migrations, receipt persistence, production webhook composition,
trigger stores, run stores and worker-origin guards. These focused checks do not
claim a live RabbitMQ publication check; that belongs to the full repository gate.

```sh
uv run pytest -rs tests/test_webhook_run_trigger_postgres.py \
  tests/test_github_run_trigger.py tests/test_github_installation_dispatch.py \
  tests/test_webhook_full_path.py
```

185 passed, zero skipped, 2 dependency deprecation warnings in 69.39s.
54 tests carry the integration marker; 131 are unit/full-path checks.

The inherited PostgreSQL regressions passed:

- `test_successful_new_head_ci_survives_superseded_run_cancellation_without_another_event`:
  the old head cancels and exactly one new head queues without another delivery.
- `test_wait_survives_restart_redelivery_and_concurrent_replay_to_one_run`:
  receipt retry survives restart and duplicate delivery, concurrent replay creates
  one run, and failed publication recovers through the persisted outbox.
- `test_newer_head_during_wait_never_uses_obsolete_heads_successful_ci`:
  obsolete-head success does not create a run for that head; current-head eligibility
  is evaluated according to the existing CI mode contract.
- Waiting-receipt controls verify live CI, label removal, PR closure, repository
  disablement, literal pending/terminal receipt fields, one-hour wait age, transient
  failure reset and terminal consecutive failures.

```sh
uv run pytest -rs tests/test_receipt_installation_guard_postgres.py \
  tests/test_retired_enqueue_postgres.py tests/test_retired_pr_projection_postgres.py \
  tests/test_run_origin_postgres.py tests/test_run_origin_guard_postgres.py
```

148 passed, zero skipped, 2 dependency deprecation warnings in 84.23s.
Coverage includes original-receipt installation forwarding across PR/label/CI,
rebind during target lookup or CI observation, retired source refusal, installation
locks, both run writers, NULL-origin backfill, immutable historical origins,
source cancellation, worker ownership and reconciliation.

The adapted test file also passed `uv run ruff check`, `uv run ruff format --check`
and `uv run mypy` with that file as the explicit target. `git diff --check` passed.
The deployment scripts, compose configuration, CI workflows and dependency files
match combined main: `git diff --name-only origin/main -- deploy docker-compose.yml
scripts .github pyproject.toml uv.lock` returned no paths.

The [final proof](113-review-round5-final.md) records full repository lint, format,
typecheck, PostgreSQL/RabbitMQ tests and independent reviews. Publication hooks
and published-head CI remain separate verification steps.
