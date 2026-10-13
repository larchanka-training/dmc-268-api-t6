# PR #132 round-seven B proof (#113)

This addresses P3-2 in the [round-seven review](https://github.com/larchanka-training/dmc-268-api-t6/pull/132#pullrequestreview-5503707715). The starting checkpoint was `075004e996db907b19787c91322ff8b62e43825e`, based on main `2251b5936822827fc75b38cfc6b352c6a5e89a6b`. This proof records the focused test module; the [final proof](113-review-round7-final.md) records combined verification separately.

## Regression contract

`test_waiting_ci_receipt_keeps_its_installation_after_rebind` extends the existing `Pipeline`, `_wait_for_new_head` and `_finish_old_head` scenario in `tests/test_webhook_run_trigger_postgres.py`. Its four cases cover retry/redelivery against unchanged-installation controls and a repository rebound from retired GitHub installation 17 to active installation 29.

The test persists `delivery-3` while the old head is running with cancellation requested, then finishes that old Run. Literal assertions cover the waiting receipt's null projected/failed/deferred timestamps, 30-second retry time, zero attempt count, cleared claim/lease, and source installation 17 in both the receipt column and payload. A fixed public receiver clock also makes the terminal projected timestamp literal; failed/deferred/retry/claim/lease remain null and attempts remain zero.

Redelivery first repeats the same GUID before its retry is due: the receipt count, ID, original received timestamp, retry timestamp and all receipt fields remain unchanged, with no provider or token request. A different GUID then creates a separate terminal receipt with source 17 and the complete projected/failure/deferred/retry/attempt/claim/lease state asserted.

The old Run's origin UUID remains unchanged and resolves to installation 17. Each control creates exactly one new-head Run with that same origin and publishes exactly the old/new queued messages for installation 17. The expected GitHub CI endpoint calls and token requests are literal. Rebound cases create no new-head Run, no Run with origin 29, no extra publisher message, and no GitHub or installation-token request after retirement/rebind.

## Combined-fence mutation

The behavioral mutation changed only:

- `app/modules/reviews/infrastructure/webhook_run_targets.py`: remove the CI target's active-source-installation guard and source-installation external-ID predicate.
- `app/modules/reviews/application/try_enqueue_webhook_run.py`: discard `expected_installation_external_id` before eligibility and the locked candidate check.

This leaves valid target, eligibility, Run insert and publisher paths available to the replacement installation. The fake accepts those CI endpoints normally, so a forbidden replacement path reaches the persisted Run assertion.

With PostgreSQL/RabbitMQ configured through `TEST_DATABASE_URL` and `TEST_RABBITMQ_URL`, run:

```sh
uv run pytest -rs tests/test_webhook_run_trigger_postgres.py -k waiting_ci_receipt_keeps_its_installation_after_rebind
```

Baseline: **4 passed, 38 deselected, 0 skipped** in 7.51 seconds. Under the combined mutation: **2 failed, 2 passed, 38 deselected, 0 skipped** in 9.87 seconds. Both failures were the rebound cases (`retry-rebound` and `redelivery-rebound`), at the literal assertion that only the cancelled old-head Run exists. The database instead contained an extra `('dddddddddddddddddddddddddddddddddddddddd', 'queued', True)` Run. Neither failure was a signature, mock-request or return-policy error.

Both production files were restored from their exact saved bytes immediately afterward. The original and restored SHA-256 values matched:

| File | Original/restored SHA-256 |
| --- | --- |
| `webhook_run_targets.py` | `ed51cc35f828c946b69b85eab84fe5fcb88d9ef6103a9a174c26dac6cb34e040` |
| `try_enqueue_webhook_run.py` | `45abbfc04ba77a23af657ba927ae1a61f02ec9766c1332735524a6aad073214b` |

`git diff --exit-code -- app/modules/reviews/infrastructure/webhook_run_targets.py app/modules/reviews/application/try_enqueue_webhook_run.py` passed with no diff. No production changes or new dependencies remain.

## Restored verification

```sh
uv run pytest -rs tests/test_webhook_run_trigger_postgres.py
uv run ruff check tests/test_webhook_run_trigger_postgres.py
uv run ruff format --check tests/test_webhook_run_trigger_postgres.py
uv run mypy tests/test_webhook_run_trigger_postgres.py
git diff --check
```

The restored full module passed: **42 passed, 0 failed, 0 skipped** in 99.73 seconds. Focused lint, formatting and mypy passed (one source file), and `git diff --check` passed. The proof contains no local service URLs, credentials or absolute workspace paths.
