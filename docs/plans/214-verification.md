# #214 — verification evidence

**All four gates passed: 6437 tests, zero skipped, two existing deprecation
warnings in 1532.28 s (25:32), exit 0. Final review: Standards 0; Spec 0.**
Full verification base: `4faa03dd7accfa161048f584b834b314fa1ab5b6`.
Final branch base: `f8b13103f908b26e3e210c3f78b125f21bfbdaba`.
The latter adds exactly one documentation-only commit, touching 44 docs files
and none of the eight #214 paths. Every non-documentation path is unchanged;
app/tests/alembic/workflow/rules/review/scripts/dependency/config trees and blobs
match. All five #214 implementation/operator/test SHA256 hashes also match.
Static gates passed again after that rebase. The full result therefore verifies
the final executable/test source without an unnecessary full repeat.

No GitHub human approval, PR publication, live staging result or durability
verification is claimed here.

## Final gates and environment

| Command | Exit | Result |
| --- | --- | --- |
| `uv run ruff check .` | 0 | All checks passed |
| `uv run ruff format --check .` | 0 | 591 files already formatted |
| `uv run mypy .` | 0 | No issues in 581 source files |
| `uv run pytest` | 0 | 6437 passed, 0 skipped, 2 warnings, 1532.28 s |

All individual and aggregate statuses were 0. The exact frozen Linux source
snapshot had 1165 verified content hashes, including tests, rules, workflows and
review dataset, excluding host Git metadata, environments, secrets and caches.
The collection increase from 6032 at CF50 to 6437 reflects upstream additions.
Immutable image:
`sha256:68e00cb4da254479b1de690dd9d83dd83f017c4e9757c196f3bb6fb484919fb1`.
Final worker blob: `5ec99217c629a270edd577d495ba9bcd0966d5f2`.

Environment: official Python 3.13.17, uv 0.12.11, locked all-groups dependencies,
non-root UID 1000, writable precompiled caches, runtime UV_COMPILE_BYTECODE=0
and normal Python bytecode reuse. Dedicated PostgreSQL 17 and RabbitMQ 4 used
the internal Docker network and isolated test vhost. PostgreSQL fsync was off
only for disposable functional verification; this supplies no durability claim.
Warnings were existing FastAPI/Starlette TestClient deprecations. Raw logs and
individual statuses are retained outside Git. Only these three plan/evidence
documents were reconciled after completion.

## Behavioral and test-first proof

Initial settings RED on `05a96bc422468a570bab3233b11b538eb4a87ff6`:
29 failed with DID NOT RAISE, 22 passed. Additional empty-port RED: 2 failed,
50 passed; bracket-suffix junk was already rejected by accessing parsed port.
GREEN: 56 portal/settings tests passed, including the literal staging base,
valid path/host/port forms, absent configuration and credential-safe error text.
The CI unmarked selection passed all 56; original relevant use-case/render
regressions passed all 167.

After rebasing onto 4faa, no fake or implementation adjustment was needed.
Restored portal plus relevant use-case regressions passed 223 tests in 0.99 s;
CI unmarked portal selection passed 56 in 0.63 s. The exact Linux snapshot's
portal plus upstream parser CPU-cap checks passed all 71 in 6.92 s.

The composition tests execute real use cases, URL factory, renderer and
transactional trace. Typed fake gateways record emitted views, execute actual
checkpoints and assert closed UOWs. Linked and absent cases assert the literal
Run UUID, details URL and terminal summary without external services.

## Independent missing-link mutations

Both final mutations ran on base 4faa and worker blob `5ec99217...` above.
Each removed exactly one `run_url=run_url` constructor argument, and restored
app/worker.py byte for byte in finally. The final docs-only rebase leaves that
worker blob and both test files unchanged.

| Command (prefix `uv run pytest`) | Exit | Actual failure |
| --- | --- | --- |
| `tests/test_worker_portal_url_wiring.py -k composed_handle_run_recovery -q` | 1 | linked HandleReviewRun case, line 339; 1 failed, 1 passed, 2 deselected, 1.38 s |
| `tests/test_worker_portal_url_wiring.py -k composed_publish_review_success -q` | 1 | linked PublishRunReview case, line 371; 1 failed, 1 passed, 2 deselected, 1.03 s |

Both failures were the rendered gateway assertion:

```text
assert view.details_url == "https://portal.test/runs/11111111-2222-3333-4444-555555555555"
AssertionError: assert None == 'https://portal.test/runs/11111111-2222-3333-4444-555555555555'
```

The intervening restored wiring suite passed all 4 tests in 1.33 s; the final
restored focused suite and full gates passed above. These are missing-link
failures, not fixture, collection or type failures. No mutation survives.
Earlier proofs on base 05a worker blob `c44c0a47728ad6befa77e851cc19d9fef6124a92`
also produced the same independent failures (1.01/0.88 s), restoring exactly;
that #214 diff was unchanged by the subsequent CF50 rebase.

## Historical experiments, separate from the final pass

| Attempt | Actual result and interpretation |
| --- | --- |
| Original publisher deletion before new tests, base `e6d3077cfcb5584f8af0524ce4a0c8a658b2c5fb` | Complete unexcluded pytest: exit 1, 5685 passed, 1 failed, 0 skips, 2 warnings, 2665.93 s (44:25). Sole failure: leader timing test observed zero first-leader ticks in fixed 0.3/0.5 s waits; no URL assertion. Exact restored isolated retry: exit 0, 1 passed in 2.79 s. This baseline was not green and was not repeated after rebases. |
| Successful CF50 checkpoint, base `cf50ff359f0a5fdf0ac2aeac9ffdbfeaef4cc9b1` | All four gates exit 0; pytest 6032 passed, 0 skipped, 2 warnings in 1252.80 s (20:52). Standards 0; Spec 0. Superseded by the final result above. |
| Extra early clean run without services | Interrupted deliberately: exit 2, 943 passed, 272 skipped in 454.37 s; partial only. |
| Mac / earlier Linux partials | Mac session ended with the agent turn near 4%, without exit status. Linux base 05a stopped deliberately near 34% for rebase. Both partial only. |
| CF50 Linux with inherited UV_COMPILE_BYTECODE=1 | Stopped near 85% with one rollback-test failure. Isolated controls: flag 1 failed with a 20 s timeout and compilation stderr; flag 0 passed in 14.51 s. Only runtime flag changed for the successful CF50 full run; source/image unchanged. |

Partial attempts supply no full-pass evidence. No unrelated rollback test,
leader timing code, dependency, schema or deployment wiring was changed.

## Prepared publication body

Ready PR publication is next; the user assigns the reviewer. This is prepared
content, not a published PR or approval.

### What

Reject malformed nonempty PORTAL_URL during worker settings construction and
protect both real composed check-run publication paths with deterministic tests.
Document accepted bases and fail-start/rollback behavior in the operator guides.

### Why

A lost URL constructor argument silently removes portal links. Invalid configured
bases must fail startup before malformed links reach GitHub; absent configuration
continues to work without a link.

### How to verify

All four commands above passed, including unexcluded pytest with 6437 passed and
zero skips. Both independent URL-argument deletions fail on missing rendered
details_url and restore green. Settings RED/GREEN, credential privacy and the
literal staging base are covered above. Final documentation-only upstream rebase
preserves executable/test source; static gates pass again. No dependency added.

### Refs

Refs #214. Human current-head review, merge and tech-lead issue closure follow
publication; live deployment/acceptance are outside this implementation.
