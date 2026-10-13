# Origin-lock refactor verification (#151 P3-5)

Base: `c1d8f59d9ff3f332a3595857dbe612115ad559e8` (refreshed origin/main).
Branch: `chore/151-origin-locks`.
Environment: dedicated disposable PostgreSQL 17 and RabbitMQ; fixture-created
random schemas, credentials not recorded. Full broker suites run serially.

## Before source changes

Baseline command: `uv run pytest tests/test_run_policy_context_origin_postgres.py
tests/test_run_policy_snapshot_origin_postgres.py tests/test_run_origin_guard_postgres.py -rs`.
Production behavior was already correct; this is regression-first refactoring,
not a claim of naturally failing old behavior. Existing locking regressions use
pg_blocking_pids/NOWAIT observations rather than elapsed time.


Result before any production edit: **140 passed, zero skipped**, 2 existing
third-party deprecation warnings, **180.94s** on the base above.

## Lock-order mutation proof

After extraction, temporarily move only the helper's final Run-lock execution
before its installation/Repository shared locks. Keep caller selects and all
origin/state/owner guards intact. Run:

```sh
uv run pytest tests/test_run_policy_snapshot_origin_postgres.py tests/test_run_origin_guard_postgres.py tests/test_run_diff_origin_postgres.py -k 'holds_provider_share_locks or claim_holds_source_and_repository or snapshot_writer_locks_origin_and_repository' -rs
```

Expected RED: **5 failed, 134 deselected, zero skipped**, **4.91s**. All five fail
`DID NOT RAISE DBAPIError` on the independent authority-row NOWAIT probes: policy
documents and snapshot writes, lifecycle claim, and two diff write regressions.
No constructor/type/provider error substitutes for this lock-order observation.
The exact helper source is restored in a `finally` block before acceptance.
This proves the existing regression safety net detects unsafe ordering; no new
test is needed or added solely to mirror the extracted implementation.

## Restored source static gates

- `uv run ruff check .`: all checks passed.
- `uv run ruff format --check .`: 434 files already formatted.
- `uv run mypy .`: no issues in 431 source files.
- `git diff --check`: clean.

The generic helper uses `Select[RowT]`, an explicitly typed `Result[RowT]` local,
and `Row[RowT] | None`; no casts or untyped return. Eight call sites preserve
caller-specific selects, outer/inner joins and refresh options. No dependency,
migration, transaction ownership, guard, public behavior or network change.

## Restored source acceptance

```sh
uv run pytest tests/test_run_policy_context_origin_postgres.py tests/test_run_policy_snapshot_origin_postgres.py tests/test_run_origin_guard_postgres.py tests/test_run_diff_origin_postgres.py tests/test_run_context_origin_postgres.py tests/test_run_publication_origin_postgres.py tests/test_run_origin_postgres.py tests/test_app_reinstall_postgres.py -rs
```


Acceptance result: **256 passed, zero skipped**, 2 existing third-party
warnings, **372.57s**. All five mutation-targeted lock regressions are green again.
This covers policy writes and cached-row refresh, lifecycle owner/stale checks,
diff revision/state/origin binding, context writes, publication, immutable
historical payloads, origin schema behavior and concurrent reinstall/rebind.

## External public-composition compatibility

First PR #152 head: `604423349f45bdc977cd770bd0414f22c9a97f41`.
Second refactor base/HEAD before publisher commit:
`c1d8f59d9ff3f332a3595857dbe612115ad559e8`, with the reviewed working diff.
Run `uv run python` from the second worktree, preimport `app.worker`, then invoke:

```python
pytest.main([
    "--import-mode=importlib",
    "--rootdir", str(second_root),
    str(first_root / "tests/test_run_policy_context_origin_postgres.py"),
    "-k", "worker_composition", "-rs",
])
```

Result: **2 passed, 24 deselected, zero skipped**, 2 existing deprecation
warnings, **49.91s**. Before and after pytest, `app.worker.__file__` resolves to
this second worktree's `app/worker.py`. A post-run check verifies all **196**
loaded `app` package/module files originate here. Both active and revoked cases
exercise the refactored app with the independent first PR's public composition
regression; no source or test from that branch is copied into this branch.
The first PR's test head and this app's working diff are separate evidence.

## Full required gate

`uv run pytest` ran with both dedicated test services configured and exclusive
broker ownership. The PostgreSQL-only acceptance run finished concurrently using
fixture-created isolated schemas. Result: **3425 passed, zero skipped**, 2 existing
third-party deprecation warnings, **753.16s**. Both test services remain available
for publication/verification. Post-suite remaining fixture schema count: **0**.

All four required gates pass on the unchanged reviewed source. The full test
count matches this independent main baseline; PR #152's two added composition
cases belong to that separate branch and were additionally checked externally.


## Fresh review

The coordinator's fresh full-diff Standards and Spec audits both report **zero
findings** on 2026-10-13. Exact comparison base:
`c1d8f59d9ff3f332a3595857dbe612115ad559e8`; reviewed the stable working diff plus
new helper. The coordinator also checked all eight call sites and the helper:
SQL sequence, typed row shape, joins, refresh options and caller guards match the
baseline. No subsequent production source changes are made before final gates.
