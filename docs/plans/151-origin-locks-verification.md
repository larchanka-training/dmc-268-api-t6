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

## Publication

Independent PR: [#153](https://github.com/larchanka-training/dmc-268-api-t6/pull/153).
Source/proof commit: `84641aa` (`refactor(reviews): centralize run origin lock order (#151)`).
Fresh publisher fetch found origin/main unchanged at the exact base above;
`git rebase origin/main` reported the branch up to date. Commit hooks passed,
including Ruff lint/format and mypy. Exactly four production files and these
three separate documents are committed; no first-PR diff, tests, dependency or
migration is included.

The PR was initially created as Draft against main, with no reviewers assigned as
requested. On 2026-10-14 it was Ready and all applicable CI checks were green on
historical head `205f1a6ab8ed405e0fe56df7bf63cb7be40519b9`. That observation does
not establish CI status for the review follow-up head. Human approval on the
current head, merge, and issue acceptance remain external Before merge steps.

## PR #153 review follow-up (2026-10-14)

The helper docstring now requires the caller's select to target the same
`Run.id` as `run_id` and contain no existing lock clause; the helper executes it
with `FOR UPDATE OF runs`. Executable SQL, caller selects and guards are unchanged.

Independent reviewer [ax yi (`axyi`)](https://github.com/larchanka-training/dmc-268-api-t6/pull/153#pullrequestreview-5516285256)
reported on historical head `205f1a6` that moving Repository before installation
produced genuine PostgreSQL `40P01` deadlocks in the claim and diff rebind tests,
plus two watcher timeouts. This supplements the original Run-first proof above.
It is reviewer evidence: no command, logs, timing or environment detail beyond
the review is inferred, and this follow-up did not rerun that mutation. No source
mutation was made in this round.

The same review reported 3425 full tests passed (613 integration), zero skips,
and 256 local eight-file PostgreSQL cases passed. These are separate reviewer
observations, not the developer's updated-run results.

Optional concurrency cases for `finish`, `requeue`, `complete_publication`, and
`save_context_payload` are deferred as a separate follow-up associated with
[#106](https://github.com/larchanka-training/dmc-268-api-t6/issues/106), as suggested
in the PR review; the exact gaps are tracked here, without changing #106.
This behavior-preserving refactor retains their selects/guards and existing
public behavior regressions; extending the concurrency matrix is a separate
focused task, beyond #151 P3-5's acceptance criteria.

Follow-up required gates on this working tree: `uv run ruff check .` passed;
`uv run ruff format --check .` reports 434 files formatted; `uv run mypy .`
reports no issues in 431 source files. Full `uv run pytest` with dedicated
PostgreSQL 17/RabbitMQ and exclusive broker use completed: **3425 passed, zero
skipped**, 2 existing third-party deprecation warnings, **728.41s**. This is the
follow-up developer's run, separate from the historical/reviewer results above.
`git diff --check` is clean; comparison against original PR head `205f1a6`
confirms identical executable helper AST and an empty tests diff. Post-suite
remaining non-system/non-public fixture schemas: **0**.

Fresh independent Standards/Spec reviews both report zero findings on 2026-10-14
against base `c1d8f59`, covering the original PR diff and stable docstring-only
production correction plus documentation. Subsequent evidence bookkeeping does
not change executable source. Published-head CI remains a separate observation.

Publication receipt: the publisher confirmed unchanged current main `c1d8f59`
and rebased the detached PR checkout before publication. The resulting SHA and
actual revised-head CI observation are recorded in the updated PR body and the
single four-item author reply; they are not inferred from historical CI or local
gates. Human approval, merge and issue acceptance remain external.
