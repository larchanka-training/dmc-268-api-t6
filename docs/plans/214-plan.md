# Implementation Plan: #214 — worker run URL wiring and validation

## Overview

Issue: <https://github.com/larchanka-training/dmc-268-api-t6/issues/214>.
Planning baseline: fetched `origin/main` at
`e6d3077cfcb5584f8af0524ce4a0c8a658b2c5fb` on 2026-10-25.
The original checkout is the older `fix/111-worker-portal-url`; implementation
uses a separate worktree, preserving that checkout. Final branch base:
`f8b13103f908b26e3e210c3f78b125f21bfbdaba`. Full verification ran on
`4faa03dd7accfa161048f584b834b314fa1ab5b6`; the subsequent single commit
changed only 44 documentation files, none of the eight #214 paths. All non-doc
content and all five #214 implementation/operator/test hashes are unchanged.

Protect the composition of `run_url` into both worker use cases with deterministic
tests, then reject malformed nonempty `PORTAL_URL` during worker startup. This
changes worker configuration behavior, with no HTTP API/schema, DB, dependency,
GitLab publication, check-run title/text, or staging delivery changes.

**Status: implementation, verification and two-axis review complete.**
The human approved this plan before implementation. Work uses the isolated
`chore/214-portal-url-wiring` worktree; the original checkout and unrelated plans
remain untouched. Upstream RAG/verifier changes are preserved. All four full
gates exited 0, including **6437 tests, zero skips**; final review against the
actual issue found **Standards 0; Spec 0**. Ruff, format and mypy passed again
after the documentation-only rebase. Results and the prepared publication body
are in [214-verification.md](214-verification.md). Ready PR publication is next;
no PR is claimed here, and the user will assign its reviewer manually.

## Planning-baseline patterns and dependency graph

- At the planning baseline, `WorkerSettings.from_environment` stored `env.get("PORTAL_URL") or
  None`; required settings already raise `RuntimeError` with the variable name.
- `run_url_factory` preserves the base and removes trailing `/` before appending
  `/runs/{UUID}`. Keep that contract for valid inputs.
- `compose_worker_process` constructs one URL callable and passes it separately
  to real `HandleReviewRun` and `PublishRunReview` instances. Both constructors
  default silently to a callable returning `None`.
- `update_check_run` obtains a report inside a short UOW, then renders and calls
  the gateway after leaving it. `check_run_view` supplies both `details_url` and,
  for succeeded/failed runs, the summary link. Preserve this transaction boundary.
- `tests/test_worker.py` demonstrates monkeypatching worker-bound infrastructure
  constructors; `tests/test_review_run_worker.py` and
  `tests/test_publish_run_review.py` demonstrate synchronous pytest functions
  driving real use cases with `asyncio.run`, fake UOWs, and recording gateways.
- `tests/test_worker_portal_url.py` covers absent/empty settings and trailing
  slash normalization. The PG queue test sets a portal URL but records only
  run/status/conclusion; it cannot detect lost URLs.
- Main CI runs unmarked tests in `Python lint / type / unit` with zero skips;
  `Python lint / type / test` aggregates unit and integration results. A new
  unmarked test requires neither PostgreSQL nor RabbitMQ/Redis.

Dependency path: environment → validated WorkerSettings → run_url_factory →
compose_worker_process → two independent use cases → update_check_run →
CheckRunView → GitHub gateway. Configuration tests protect the first boundary;
composition tests execute both use cases and observe the rendered gateway view.

## Architecture decisions

### 1. Fail startup for invalid nonempty configuration

Choose issue option **(a)**: raise a clear `RuntimeError` mentioning `PORTAL_URL`
before starting worker resources. Optional configuration can be absent; an
explicit malformed value is an operator error and should not silently disable a
visible product feature. Do not echo the supplied URL in the error, since an
accidentally credential-bearing URL must not be copied into logs.

Validate at `WorkerSettings.from_environment`, using a small worker-local helper
and the standard library (`urllib.parse.urlsplit`); do not introduce a new
dependency or move configuration validation into application use cases. Existing
LLM URL parsing also uses `urlsplit`, but its provider/credential policy should
not be reused for portal links. This is a startup boundary, not a general URL
sanitizer or DNS/reachability check.

| Input | Planned result |
| --- | --- |
| Unset or exactly empty string | `None`; startup continues without a link |
| `https://staging-ui.dmc268-t6.axyi.ru` | Accepted unchanged |
| `https://ui.test`, `https://ui.test/`, `https://ui.test///` | Accepted; existing generated URL remains identical |
| `http://localhost:5173`, valid IPv4 or bracketed IPv6 host/port | Accepted; both HTTP and HTTPS are allowed |
| `https://ui.test/portal/` | Accepted; base path preserved, result `/portal/runs/{UUID}` |
| Scheme case variation such as `HTTPS://ui.test` | Accepted case-insensitively; do not otherwise rewrite the input |
| `ui.test`, `/portal`, `//ui.test`, `ftp://ui.test`, `https:///portal`, `https://` | Startup error: absent/unsupported scheme or absent host |
| Space-only, leading/trailing or embedded whitespace, tabs/newlines/control characters | Startup error; inspect raw input before urlsplit, which can strip characters |
| Query or fragment, including bare `?` or `#` | Startup error; suffix concatenation would place `/runs/...` inside the query/fragment |
| Userinfo (`https://user:pass@ui.test`) | Startup error; credentials do not belong in public summary/details links |
| Invalid brackets or port (`https://[::1`, `https://ui.test:bad`, port above 65535, or an explicitly empty port), backslashes | Startup error; catch parser errors, access parsed port, reject ambiguous raw separators |

Require a nonempty parsed hostname, not merely a nonempty netloc. Reject raw
whitespace/control characters, backslashes and query/fragment delimiters
explicitly; reject parsed userinfo. Preserve valid percent-encoded path content
and trailing-slash normalization. Test literal examples above rather than
computing expected values with the validator or URL factory.

Document fail-start behavior in the existing `PORTAL_URL` paragraph of
`docs/SECRETS.md` and `docs/CICD.md` §2.1, including allowed base paths, missing
values, invalid forms and trailing `/`. The known staging literal must pass a
settings test before deployment. Explain that invalid configuration can make
deploy health checks fail and trigger automatic rollback; rollback preserves
`worker.env`, so correcting the variable/env value may be necessary. Do not
change deployment wiring or perform live deployment in this task.

### 2. Execute the real composition without PostgreSQL

Add `tests/test_worker_portal_url_wiring.py`, unmarked and self-contained. Call
the real `compose_worker_process`; retain the real use-case constructors,
`run_url_factory`, `update_check_run`, `check_run_view`, and recording check-run
gateway. Replace only infrastructure-bound collaborators at the worker module
boundary with typed fakes: lifecycle UOW factory, branch selection, and trace
where the exercised path requires it. Other adapters/queue are inert fakes;
unexpected use should fail. No private `_run_url` assertions, source-code string
inspection, constructor-only spies, external HTTP, or service fixtures.

Use two separately named behavior tests with fresh fake state:

1. Execute `process.handle_run.execute` for an already terminal `failed` run with
   attempt >= 1. This public RunGuard recovery path updates its check-run without
   invoking the review pipeline, timers, LLM or diff providers.
2. Execute `process.publish_review.execute` for a valid GitHub publishing pointer
   and successful fake review submission; fake completion changes the state to
   `succeeded`, then the real publisher updates its check-run.

For both, assert the captured target UUID and the literal expected
`https://portal.test/runs/11111111-2222-3333-4444-555555555555` in `details_url`
and summary. Assert a view was actually emitted, so a swallowed gateway error
cannot turn a vacuous assertion green. Have recording gateways run the supplied
checkpoint and assert the fake UOW is closed during network-like calls. Cover
absent portal URL as a compatibility case, asserting `None` and no summary URL.
Use a trailing-slash base in at least one composition case. Do not modify
existing PG queue tests, which also avoids a known PR ownership overlap.

### 3. Prove the two regressions independently

On an isolated implementation checkout, first attempt the issue's original
baseline mutation of `PublishRunReview` wiring and record its actual result;
restore immediately. Never assume the current main suite is still green under
that mutation based on the older issue evidence.

After adding tests, remove only `run_url=run_url` from the `HandleReviewRun`
construction in `compose_worker_process`. The new handle test must fail because
the captured URL becomes `None`; restore it and verify green. Then independently
remove only the argument from `PublishRunReview`; its publisher test must fail
for the same missing URL reason. Restore it and verify green again. Guard the
mutation script/replacement against zero or multiple matches. Preserve logs,
test names, command, exit code, observed assertion and tested SHA in the PR body;
do not commit a mutation. A test collection/type/fixture failure is not proof.

## Tasks and checkpoints

The detailed checklist is [214-todo.md](214-todo.md).

1. Protect the two composition paths (one new test file; existing code is already
   wired). Establish baseline, implement tests, and run both independent mutations.
   **Complete:** both final mutations fail on the missing details URL;
   restored focused checks and all four final gates pass. See the verification record for the
   full baseline's unrelated leader timing failure and passing isolated retry.
2. **Complete:** validate portal settings test-first and update operator docs
   (worker, settings tests, SECRETS, CICD). No application layer edits were needed.
3. **Complete:** final gates and fresh two-axis review passed; the final
   documentation-only rebase preserves the exact verified feature source.

Checkpoint after tasks 1–2: both URL paths and optional behavior pass focused
tests, malformed settings fail before worker startup, documented policy matches
the test matrix, and both wiring mutations demonstrably fail. The final checkpoint is complete: all four gates pass, the new tests are unmarked
and pass CI's unit selection, and no mutation or unrelated work appears in the diff.
Remote CI will run after publication.

## Ownership coordination before implementation

Open-PR inventory inspected by the coordinator on 2026-10-25:

| Planned file | Open PRs touching it |
| --- | --- |
| `app/worker.py` | #220, #227, #254, #290 |
| `docs/SECRETS.md` | #220, #227, #279, #290 |
| `docs/CICD.md` | #254, #279 |
| `tests/test_worker_queue_postgres.py` (avoided) | #291 |

Ownership was refreshed before development. The coordinator posted narrow #214
coordination comments on #220, #227, #254, #279 and #290 after human approval
and before worker/docs edits. Repository rules require this coordination before
editing overlapping files. New test and
existing `tests/test_worker_portal_url.py` had no
reported overlap in this snapshot. Recheck after rebasing since ownership changes.

Latest-base ownership refresh before rebase/edit: existing #220/#227/#279
coordination remained applicable; the coordinator commented on new overlaps
#280 and #297 before development resumed.

## Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Testing only a constructor misses rendered links | Execute real composed use cases and assert gateway views |
| Broad monkeypatching hides wiring regression | Keep both constructors and URL/render functions real; prove both mutations |
| urlsplit accepts/strips malformed input | Raw checks plus scheme/hostname/port/userinfo checks and literal edge tests |
| Startup policy causes deploy rollback | Validate staging literal before rollout; document failure and preserved env behavior |
| Concurrent worker/docs work | Coordinate owning PRs before touching shared files; rebase from fresh main |
| Local PG skips obscure proof | New tests are unit tests with no service fixtures or skip paths; preserve full gate results including skips |

## Verification and review constraints

Focused: `uv run pytest tests/test_worker_portal_url.py tests/test_worker_portal_url_wiring.py`.
Then relevant rendering/use-case regression tests:
`uv run pytest tests/test_review_run_worker.py tests/test_publish_run_review.py`.
Final required gates (without exclusions):

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run pytest
```

Run the new tests without `integration` marking under the same unit selection as
CI; CI's integration shards remain the proof for external services if local
`TEST_DATABASE_URL`/`TEST_RABBITMQ_URL` are unavailable. Report actual skips and
failures, never silently omit `tests/test_deploy_monitoring.py` or claim live
staging verification from unit tests. Rebase on main before review. Use
`chore/214-portal-url-wiring`, conventional commits with `Refs #214`, and PR body
`What` / `Why` / `How to verify` / `Refs`, including both mutation results and no
closing keyword/Development link. No new dependencies are planned. Current-head
approval and resolved review threads precede merge; tech lead accepts/closes the
issue. Public coordination and PR publishing proceed within the invoked workflow
after plan approval; live deployment and live acceptance remain outside scope.

## Remaining workflow

Implementation, verification and review evidence are complete. Ready PR
publication follows; the user assigns the reviewer. GitHub current-head approval,
resolved threads, merge and tech-lead issue acceptance/closure remain outside this
implementation. No live deployment, staging acceptance or durability verification
was performed.
