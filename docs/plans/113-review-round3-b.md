# PR #132 round-three proof B — schema and inactive origins

Refs #113. This test-only correction addresses the 2026-10-12 16:10 UTC review of
`83a36290192369197f6f72f5b84a65f1add6d1d1`. See the
[round-three plan](113-review-round3-plan.md) and [todo](113-review-round3-todo.md).
The migration and lifecycle implementation already satisfy these contracts.

## Behavior protected

- [Schema upgrade test](../../tests/test_app_reinstall_schema_postgres.py): legacy
  repository 108 has `enabled=false`, a selected-repository revocation at
  `2026-10-07T12:00:00Z`, and a fresh personal grant at `2026-10-08T12:00:00Z`.
  Upgrade from 0028 to head must persist literal `(108, False, True)`: the fresh
  grant restores availability while retaining the user's pause. Whole-installation
  deletion, selected removal without a fresh grant, legacy positive projection,
  unknown numeric identity, fresh schema constraints and data-bearing downgrade
  remain covered.
- [Origin guard tests](../../tests/test_run_origin_guard_postgres.py): `repo_removed`
  sets Repository availability false while the original installation remains live.
  It joins the provider-projection, reconciliation and worker-failure matrices.
  Two reconciliation passes terminalize stale queued/running/publishing work as
  `cancelled/installation_unavailable`, clear the lease, and emit no run/review
  messages. Worker failures cover retryable and nonretryable codes, and claimed
  attempts 2 and 3 (seeded attempts 1 and 2); the result is ACK, no retry, and the
  same cancelled state rather than failed/dead-lettered work.
- Direct owning-worker `finish(failed)` from running and publishing covers active,
  retired, rebound, unknown and repository-removed origins. Each inactive origin
  becomes `cancelled/installation_unavailable` with a cleared lease and error
  message, the supplied finish time, the same owner and immutable origin. Active
  controls retain `failed/diff_fetch_failed` and the provider error message. The
  lease is still live to distinguish owner authority from reconciler expiry.
  Other Run rows remain byte-for-byte equivalent as JSON; a second late finish
  returns false and leaves terminal history unchanged. Existing displaced-worker
  controls remain intact.
- Optional controls exercise repository availability restored before
  `retire_stale_origin`: queued/running/publishing work remains unchanged and the
  method returns false. A paused inactive origin is cancelled as installation
  unavailable; the active paused control is `skipped/repo_disabled`. Neither
  starts the pipeline or emits retry work, and the seeded attempt stays 1.

## Isolated mutation evidence

All mutations ran sequentially in an exclusive developer test window on disposable
PostgreSQL 17. Fixtures create isolated schemas; no shared public rows were changed.
Each mutation restored the exact original file bytes in `finally`; none is in the
final diff. No production behavior or dependency was changed.

Portable setup (the placeholders identify the local worktree and disposable DB):

```sh
cd <worktree>
export TEST_DATABASE_URL='<dev-db>'
SCHEMA_NODE=tests/test_app_reinstall_schema_postgres.py::test_existing_schema_upgrade_keeps_pause_and_unknown_identity
RECONCILE_NODE=tests/test_run_origin_guard_postgres.py::test_reconciliation_terminalizes_stale_inactive_origins_without_queue_messages
FINISH_NODE=tests/test_run_origin_guard_postgres.py::test_owning_worker_failure_terminalizes_inactive_origin_and_preserves_history
ACTIVE_NODE=tests/test_run_origin_guard_postgres.py::test_retire_stale_origin_preserves_work_when_origin_is_active_again
```

| Isolated temporary mutation | Actual command | Actual result |
| --- | --- | --- |
| Migration 0029: remove only the fresh-grant `NOT EXISTS` clause | `uv run pytest -q "$SCHEMA_NODE"` | **1 failed in 1.24s**; row 108 becomes `(108, False, False)` instead of `(108, False, True)` |
| Migration 0029: invert `access.created_at > revocation.revoked_at` to `<` | `uv run pytest -q "$SCHEMA_NODE"` | **1 failed in 0.78s**; same literal row-108 mismatch |
| Restore migration 0029 exactly | `uv run pytest -q tests/test_app_reinstall_schema_postgres.py` | **3 passed in 1.23s** |
| Lifecycle store: remove only `Repository.installation_available.is_(False)` from `stale_inactive_origins` | `uv run pytest -q "$RECONCILE_NODE" -k repo_removed` | **2 failed, 1 passed, 9 deselected, 2 warnings in 3.07s**; queued and publishing removal cases remain nonterminal, running control passes |
| Lifecycle store: remove only the inactive-origin outcome rewrite in `finish` | `uv run pytest -q "$FINISH_NODE"` | **8 failed, 2 passed, 2 warnings in 5.41s**; all inactive running/publishing outcomes are wrongly failed, both active controls pass |
| Lifecycle store: remove only active-origin recheck from `retire_stale_origin` | `uv run pytest -q "$ACTIVE_NODE"` | **3 failed, 2 warnings in 2.20s**; active-again queued/running/publishing work is wrongly cancelled |
| Restore lifecycle store exactly; verify both assigned files | `uv run pytest -q tests/test_app_reinstall_schema_postgres.py tests/test_run_origin_guard_postgres.py` | **82 passed, 2 warnings in 40.52s**, zero skips |

The first combined normal-code run exposed a test expectation mistake in the
optional pause control: the shared fixture seeds attempt 1, not 0. Its literal
expectation was corrected to 1; no production code changed. The final green result
above includes that correction. The warnings are existing Starlette/httpx and
AnyIO deprecations.

## Focused checks and handoff

- `uv run ruff check tests/test_app_reinstall_schema_postgres.py tests/test_run_origin_guard_postgres.py`
  — All checks passed.
- `uv run ruff format --check tests/test_app_reinstall_schema_postgres.py tests/test_run_origin_guard_postgres.py`
  — 2 files already formatted.
- `uv run mypy tests/test_app_reinstall_schema_postgres.py tests/test_run_origin_guard_postgres.py`
  — Success: no issues found in 2 source files.
- `git diff --check` for the two assigned test files passed.
- `git diff --quiet -- alembic/versions/20261010_0029_app_reinstall_identity.py app/modules/reviews/infrastructure/run_lifecycle_store.py`
  passed after restoration.

Blocking B1/B2 and both optional controls are complete. These are focused local
checks; the coordinator runs all four repository gates after every developer
mutation is restored. This proof does not claim the final full-suite result,
current-head approval or live GitHub reinstall acceptance.
