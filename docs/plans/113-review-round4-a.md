# PR #132 round-four guard-order proof (#113)

## Scope

At rebased head `e417c2ec5af2f7e5556614c38474743e31ebaa98` on
main `620c81d927cebf3deb0721e5dacb7b70b721ad7e`, strengthen
`test_inactive_origin_cancellation_takes_priority_over_user_pause` in
`tests/test_run_origin_guard_postgres.py`. Production behavior is unchanged.

The existing five literal origin cases retain their real PostgreSQL outcomes:
active plus user pause yields `(skipped, repo_disabled, 1, None)`; retired,
rebound, unknown and repository-removed origins plus user pause each yield
`(cancelled, installation_unavailable, 1, None)`.

A recording wrapper awaits the original public `update_check_run` helper.
The active paused control expects no invocation; every inactive case expects
exactly `[RUN_ATTEMPTED]`. A recording check-run gateway also verifies that
no provider invocation occurs. Pipeline and retry publication remain empty.
Real lifecycle stores and units of work are retained.

The observation matters because `finish` independently rewrites inactive-origin
Runs as cancelled: persisted state alone does not detect a reversed guard order.
The test checks its literal persisted tuple before checking helper invocation.

## RED: reversed guard order

Temporarily move the `repository_enabled` decision before `origin_active` in
`HandleReviewRun._guard`, leaving the rest of the production source unchanged.
With `TEST_DATABASE_URL` configured for a disposable PostgreSQL database, run:

```sh
uv run pytest tests/test_run_origin_guard_postgres.py::test_inactive_origin_cancellation_takes_priority_over_user_pause -q --tb=short
```

Result: **4 failed, 1 passed**, zero skips, 2 deprecation warnings, **5.85s**;
pytest exited 1. Retired, rebound, unknown and repository-removed origins each
failed on `check_updates == [RUN_ATTEMPTED]` because the observed list was empty.
Their preceding persisted cancellation assertions and zero-work assertions passed.
The active paused control passed.

The temporary mutation was restored in a `finally` block from the original
source bytes, and exact byte equality was asserted. The subsequent
`git diff --exit-code -- app/modules/reviews/application/handle_review_run.py`
exited 0 with no diff.

## GREEN: original production implementation

Using the same configured disposable PostgreSQL database:

```sh
uv run pytest tests/test_run_origin_guard_postgres.py::test_inactive_origin_cancellation_takes_priority_over_user_pause -q --tb=short
uv run pytest tests/test_run_origin_guard_postgres.py -q --tb=short
```

Results: selected test **5 passed in 4.34s**; complete target file
**79 passed in 47.37s**. Both runs had zero skips and the same 2 existing
Starlette/httpx and anyio deprecation warnings.

Changed-file checks passed:

```sh
uv run ruff check tests/test_run_origin_guard_postgres.py
uv run ruff format --check tests/test_run_origin_guard_postgres.py
uv run mypy tests/test_run_origin_guard_postgres.py
git diff --check
```

Ruff reported all checks passed and one file already formatted; mypy reported
no issues in one source file; whitespace validation was clean.
These focused checks do not replace the full four repository gates recorded
at the final round-four integration checkpoint.
