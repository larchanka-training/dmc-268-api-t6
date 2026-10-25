# #214 — verification evidence

PR: <https://github.com/larchanka-training/dmc-268-api-t6/pull/302>.
Remediation main base: `d08e6c9625ac5eb45eae45c229925fa776d82b80`;
rebased implementation parent: `a3fb8fd726dbd5f89f07fc78f1827879c2fd0dd4`.
**Current full gates passed: 6475 tests, zero skipped, two existing TestClient
deprecation warnings in 807.34 s (13:27); all four commands exited 0.**
Independent agent Standards/Spec checks covered the review-round changes and
reported no findings. This records that scoped checkpoint, not human approval
or a blanket correctness claim. Prior full success below is historical; no live
staging or durability result is inferred.

## Review-round test-first checks

- Port-zero and U+200B inputs before policy refinement: exit 1, 2 failed with
  DID NOT RAISE and 53 passed. After the minimal checks plus valid port-65535
  boundary case: settings 56 passed in 0.49 s.
- Restored settings/composition: 62 passed in 0.53 s. Relevant use-case/render
  regression selection including those files: 229 passed in 0.74 s.
- CI unit collection command:
  `uv run pytest tests/test_worker_portal_url.py tests/test_worker_portal_url_wiring.py -m "not integration" --strict-markers --collect-only -q`.
  All 62 collected, with no integration markers or exclusions added.
- Generic RuntimeError retained intentionally: fixed message names PORTAL_URL
  without input, avoiding additional diagnostic/privacy branches. Typed fakes
  remain local to avoid coupling independent feature tests.

## Independent mutation proofs

Commands below use prefix `uv run pytest`. Every mutation matched exactly one
site and restored app/worker.py byte for byte in finally before the next check.

| Mutation | Test selection | Exit and observed failure |
| --- | --- | --- |
| M1: remove HandleReviewRun run_url kwarg | `tests/test_worker_portal_url_wiring.py -k composed_handle_run_recovery -q` | 1; linked case at line 352: details_url=None versus literal portal Run URL; 1 failed, 2 passed, 3 deselected, 0.49 s |
| M2: remove PublishRunReview run_url kwarg | `tests/test_worker_portal_url_wiring.py -k composed_publish_review_success -q` | 1; linked case at line 386: same missing-link assertion; 1 failed, 2 passed, 3 deselected, 0.49 s |
| M12: remove only `from None` in parser-error handler | `tests/test_worker_portal_url.py -k do_not_echo -q` | 1; nfkc-netloc case at line 166 leaks do-not-log-this in formatted traceback; 1 failed, 1 passed, 54 deselected, 0.62 s |

NFKC input is literal `https://alice:do-not-log-this@ui.test／x` (U+FF0F).
The original ASCII bad-port input remains a separate case; it does not kill M12.
Restored privacy selection passed both cases; the intervening restored wiring
suite passed all 6 in 0.46 s. Full restored focused/regression results are above.
No mutation survives. These are behavioral assertion failures, not setup failures.

## Current full-gate status

| Command | Exit | Actual result |
| --- | --- | --- |
| `uv run ruff check .` | 0 | All checks passed |
| `uv run ruff format --check .` | 0 | 598 files already formatted |
| `uv run mypy .` | 0 | No issues in 588 source files |
| `uv run pytest` | 0 | 6475 passed, 0 skipped, 2 warnings, 807.34 s |

The frozen source used main base d08e6c9 above plus the review remediation.
All 1191 snapshot content hashes were verified; the Linux focused regression
selection passed 229 tests in 1.24 s and full collection confirmed 6475.
Immutable image:
`sha256:da60f35061775a31c738dd788958eec8a7fee7a293ec5cde5d08873ece8e08a0`.
Worker blob: `ea950ba905c6c793a620d4fc93a76764a5574bc2`.
Python 3.13.17 and uv 0.12.11 used locked all-groups dependencies, reusable
precompiled bytecode and runtime UV_COMPILE_BYTECODE=0. Tests ran without
exclusions against dedicated PostgreSQL 17/RabbitMQ 4. Disposable PostgreSQL
fsync was off for functional verification, providing no durability evidence.
Only the three plan/evidence documents were reconciled after the full result;
all six implementation/operator/test file hashes remained unchanged.

## Historical evidence

| Source/checkpoint | Actual outcome |
| --- | --- |
| Original pre-new-test publisher deletion, base `e6d3077cfcb5584f8af0524ce4a0c8a658b2c5fb` | Complete unexcluded pytest exit 1: 5685 passed, 1 leader timing failure, 0 skipped, 2 warnings in 2665.93 s (44:25). No URL assertion failed. Restored isolated leader retry passed in 2.79 s. Never a green baseline. |
| Intermediate TDD suites on `05a96bc422468a570bab3233b11b538eb4a87ff6` | 29 failed / 22 passed, then empty-port slice 2 failed / 50 passed. These were intermediate suites, not the final 56-test baseline. |
| Reviewer reproduction of final original 56 tests against pre-validator worker `f8b13103` | Review round 1 reports 33 failed / 23 passed; attributed reviewer observation, distinct from the earlier intermediate TDD runs. |
| Full implementation checkpoint on `4faa03dd7accfa161048f584b834b314fa1ab5b6` | All four gates exit 0: Ruff passed, format 591 files, mypy 581 source files; pytest 6437 passed, 0 skipped, 2 existing TestClient deprecations in 1532.28 s (25:32). Subsequent f8 commit was docs-only; runtime/test hashes matched and static checks passed again. This predates remediation. |

Earlier interrupted/environment-invalid attempts supplied no full-pass evidence.
Raw logs remain outside Git. The current result supersedes the historical
checkpoint for remediation. Deployment scripts were inspected:
ci-cd.yml bundle and rollback reporting, env-file.sh worker allowlist,
deploy.sh conditional write_app_env_files and rollback.sh ensure_app_env_files.
No deployment script, live setting, dependency or schema was changed.
