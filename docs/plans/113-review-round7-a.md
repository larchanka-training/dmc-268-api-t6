# PR #132 round-seven A proof (#113)

P3-1 adds two PostgreSQL GitLab-origin regression cases to
`tests/test_run_publication_origin_postgres.py`. The starting checkpoint is
`075004e996db907b19787c91322ff8b62e43825e`, based on main
`2251b5936822827fc75b38cfc6b352c6a5e89a6b`. This proof records the focused GitLab-origin tests; combined gates and review
are recorded in the [final proof](113-review-round7-final.md).

## Public behavior

The existing `publication_env` uses the migrated `portal_schema`; the existing
`prepare` seeds a publishing Run with active or unknown origin. The test changes
the installation's provider to GitLab, then exercises the real
`SqlAlchemyRunLifecycleStore.publish_context`, `lock_for_claim` and
`PublishRunReview` with the real lifecycle UoW, `reviews=None` and a typed GitLab
fake. It asserts literal provider/project identity `gitlab/101`, context and claim
guard activity, persisted `succeeded/None` with gateway project calls `[101]`, or
`cancelled/installation_unavailable` with no gateway calls. Trace observations
show zero `github.publish_review` calls, and the typed check-run observer receives
zero calls. No new dependency, type ignore, unrelated helper refactor or persistent
production edit was needed. `run_lifecycle_store.py` remained read-only.

## Mutation proof

With the configured real PostgreSQL service, both behavioral mutation runs used:

```sh
uv run pytest -rs --tb=short tests/test_run_publication_origin_postgres.py -k gitlab
```

Only `app/modules/reviews/infrastructure/run_origin.py` was temporarily changed.
Each mutation inserted a GitLab-specific early return at the start of
`origin_active`:

```python
if installation.provider == "gitlab":
    return True  # always-active mutation; False in the always-inactive mutation
```

| Mutation | Actual result | Intended failure |
| --- | --- | --- |
| GitLab always active | 1 failed, 1 passed, 38 deselected, 0 skipped (2.13 s) | Unknown case: `assert context.origin_active is (origin == "active")` observed `True`, expected `False`. |
| GitLab always inactive | 1 failed, 1 passed, 38 deselected, 0 skipped (1.99 s) | Active case: the same assertion observed `False`, expected `True`. |

Each test run restored the original bytes in a `finally` block immediately after
execution and checked byte equality. Both restored SHA-256 values were
`22472ad2f02e5ed7196ffc7798b3e031473df5cc4f07950e8953c70a1e749506`;
`git diff -- app/modules/reviews/infrastructure/run_origin.py` was empty afterward.

An initial malformed probe used `installation.provider.value` and failed with
`AttributeError`, because this field is a string. Its bytes were restored and the
probe was corrected and rerun. That incidental failure is excluded from the two
behavioral mutation results above.

## Restored verification

`TEST_DATABASE_URL` was configured externally; no service URL or credential is
recorded in this proof. PostgreSQL tests ran against real migrated schemas, with
no skips.

| Command | Actual result |
| --- | --- |
| `uv run pytest -rs tests/test_run_publication_origin_postgres.py -k gitlab` (original-source baseline) | 2 passed, 38 deselected, 0 skipped (7.12 s). |
| `uv run pytest -rs --tb=short tests/test_run_publication_origin_postgres.py` (exact source restored) | 40 passed, 0 failed, 0 skipped (27.56 s). |
| `uv run ruff check tests/test_run_publication_origin_postgres.py` | All checks passed. |
| `uv run ruff format --check tests/test_run_publication_origin_postgres.py` | 1 file already formatted. |
| `uv run mypy tests/test_run_publication_origin_postgres.py` | No issues in 1 source file. |
| `git diff --check` | Clean. |

Pytest emitted two existing Starlette/httpx and anyio deprecation warnings. These
focused results are separate from the combined full-suite and final-head
verification recorded in the [final proof](113-review-round7-final.md).
