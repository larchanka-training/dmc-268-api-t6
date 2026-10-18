# Verification: reviewer-intent / timeline removal (#81)

Baseline: `362f7586f797ae29b4f367a3bcbd745657924976`. All implementation is
in the isolated `81-reviewer-intent-path` worktree. The original checkout was
not edited. Human approval covered the complete plan, public test seams,
ownership coordination comments, and draft PR publication on 2026-10-14.

Coordination was posted before shared-file edits: PR154
`issuecomment-6176814769`, PR148 `issuecomment-6176815788`, PR124
`issuecomment-6176816780`.

## Red / green proof

- `uv run pytest tests/test_github_pr_projection.py tests/test_github_installation_dispatch.py -k 'reviewer_deliveries_are_ignored or review_request'`:
  RED: 2 failed / 2 passed; both new projector cases raised
  `reviewer delivery acquired the projection lock`. Dispatcher cases passed.
  After the early ignore guard: GREEN, 4 passed.
- `uv run pytest tests/test_github_pr_projection.py -k live_projection_preserves`:
  RED: 3 failed / 2 passed; synchronize-to-closed, closed, and reopened changed
  literal historical reviewer values (`False != True`). After removing the legacy
  barrier writes/state machine: GREEN as part of the targeted suite below.
- `uv run pytest tests/test_github_pr_projection.py tests/test_github_installation_dispatch.py tests/test_github_run_trigger.py`:
  171 passed, 20 skipped (opt-in PostgreSQL). Existing durable dispatcher ignore,
  live projection, trigger, cancellation, and post-commit signal coverage retained.

## PostgreSQL proof

A fresh disposable `postgres:17-alpine` container named `dmc268-issue81-proof`
used loopback random port 63945 and database `issue81`. Existing local containers
were inspected and left running. Fixtures create UUID-named schemas, migrate
explicitly with Alembic, then drop only their own schemas. No shared database was
used. The dedicated container was stopped and automatically removed afterward.
This was repository integration verification, so the full HTTP smoke stack from
`e2e-test` was not needed.

```sh
TEST_DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:63945/issue81 uv run pytest tests/test_retired_pr_projection_postgres.py tests/test_github_pr_projection.py tests/test_label_intent_migration.py
```

Result: **78 passed, zero skipped**, 2 existing dependency deprecation warnings.
This includes 24 retirement/current-provider cases, 53 PR projection cases
(including PostgreSQL advisory-lock serialization, label persistence, metadata
replay, and legacy mapping), and 1 label migration case.

Retirement coverage now exercises only live current-provider paths. It retains
source refusal before reads, token checkpoints before current HTTP, installation
lock release across network work (a concurrent `FOR UPDATE NOWAIT` succeeds),
source revalidation after reads, write locking, and no projection or cancellation
of retired/rebound history. Synchronize, closed, and label paths cover retirement
during a read and source rebinding. Timeline-only pagination coverage was retired.

## Scope and static checks

- `uv run pytest tests/test_github_repository_path.py`: 219 passed. The only
  edits remove the deleted timeline adapter import and sink registration; all
  surviving adapters retain every request-path case.
- `git grep -n 'ReviewerTimelineProvider\|_apply_reviewer_intent' -- app tests`:
  empty, expected exit 1. Removed timeline types/helpers have no residual imports.
- Model, mapper, and migrations have no diff against the baseline. All seven
  `PullRequestRecord` reviewer fields and defaults are unchanged. No dependencies
  were added.
- GitHub current-provider calls remain outside UOW contexts; checkpoint UOWs
  release installation locks before the next external call. Projection lock,
  current identity/head freshness, labels, lifecycle, transaction cancellation,
  and post-commit signaling remain in place.
- SD changes are confined to the single §16 reviewer row: option A, retained
  historical columns/values, and the existing `ai-review` plus successful CI trigger.
- `git diff --check`: passed.

## Required gates

- `uv run ruff check .`: passed.
- `uv run ruff format --check .`: passed; 433 files already formatted.
- `uv run mypy .`: passed; 430 source files.
- `uv run pytest`: **2796 passed, 617 skipped**, 2 existing dependency
  deprecation warnings, 217.96 seconds. No failures. `TEST_DATABASE_URL` and
  `TEST_RABBITMQ_URL` were unset for this default gate; the affected PostgreSQL
  fixtures were explicitly exercised in the 78-pass zero-skip run above.

Default integration skips are the opt-in database/broker cases in
`tests/*_postgres.py`, plus the integration fixtures in:

- `tests/test_cancellation_signal_store.py`
- `tests/test_github_pr_projection.py`
- `tests/test_github_run_trigger.py`
- `tests/test_github_webhook_delivery.py`
- `tests/test_initial_migration.py`
- `tests/test_installation_linking.py`
- `tests/test_installation_onboarding_composition.py`
- `tests/test_label_intent_migration.py`
- `tests/test_prompt_seeding.py`
- `tests/test_repo_conventions.py`
- `tests/test_run_invariants_integration.py`

Independent final review reported **Standards: 0; Spec: 0** actionable findings.
No source changes followed successful gates.

## Publishing preflight

- The user authorized draft publication; no reviewer assignment was requested.
- Fresh `git fetch origin main` and `git rebase --autostash origin/main`:
  branch `chore/81-reviewer-intent-path` was already current on baseline
  `362f7586f797ae29b4f367a3bcbd745657924976`; source was unchanged.
- Open-PR ownership inventory still contained #154, #148, #133, and #124,
  with only the already documented overlaps. All three authorized coordination
  comments were read back and verified before publication.
- Final independent review reports are in [81-review.md](81-review.md).
  All plan tasks and final checkpoints are complete.
- Publication uses the repository PR template, `Refs #81`, and draft status.
  Current-head GitHub approval and resolved threads remain merge requirements.
