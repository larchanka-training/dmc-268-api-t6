# Final review and gate evidence — PR #132 / #113

Tested base: `9aedfa8e25185a8c3daf00fe37de7a4236f9a70c` (current fetched main,
including #134). Reviewed working tree: HEAD
`ed62aad4154ed65409b4ae1ab65d76fbc4afe297` plus intended correction delta.
The correction commit adds the reviewed source/tests and documentation evidence.
Historical base/counts in `113-plan.md`, `113-todo.md` and per-developer proof
are historical execution records, not claims about the final base.

## Final configured gates

- `uv run ruff check .`: passed.
- `uv run ruff format --check .`: passed, 383 files already formatted.
- `uv run mypy .`: passed, 380 source files.
- `uv run pytest -rs`: 2,846 passed, zero failed/skipped, two existing dependency
  deprecation warnings, 443.46 seconds (7:23).
- `uv run pytest --collect-only -m integration -q`: 469/2,846 collected,
  2,377 deselected, 2.36 seconds. All 469 integration cases were included in the
  same full execution; no standalone integration-subset execution is claimed.

Dedicated real PostgreSQL and RabbitMQ were configured for that full execution.
The full suite completed successfully before publication/commit. Coordinator logs:
`/tmp/pr132-pytest-main9aed.log` and `/tmp/pr132-integration-main9aed.log`.

The initial full correction run exposed two stale worker eligibility assertions:
unavailable origin now requires `cancelled/installation_unavailable`, while active
user pause retains `skipped/repo_disabled`. Those expectations were corrected.
Independent Spec review then found the real NULL-to-numeric callback/sync deadlock;
its synchronized PG red reproduction, rollback-before-bounded-retry correction and
62-test affected green proof are recorded in developer A evidence. The final run
above includes both corrections and supersedes prior counts/interrupted runs.

The #134 rebase checkpoint passed 133 focused tests, including mandatory
merge-base token and GET retirement guards. The new `test_merge_base_fences_token_and_http_after_retirement` has three
parameter cases: cancellation before token, cancellation before comparison GET,
and an active control. Removing either of the two checkpoints makes its mutation
fail; both were restored and the adapter suite passed 16 tests. No mutation timing
is claimed. All source changes were included in the subsequent final gates and
independent review.

## Independent review

The following is the independent read-only review captured before publication;
its statements that execution/publication are pending describe reviewer ownership
and are superseded only by actual gate evidence above.

## Standards

0 findings — no actionable documented-standard violations or Fowler-smell findings in the current tracked diff against `9aedfa8e25185a8c3daf00fe37de7a4236f9a70c`, including uncommitted corrections and the explicitly reviewed untracked account-lock helper and correction-plan/evidence files. HEAD: `ed62aad4154ed65409b4ae1ab65d76fbc4afe297`.

Checked AGENTS.md, backend/git rules, CONTRIBUTING, BACKEND_ARCHITECTURE and TEST_PLAN. Application ports remain independent of infrastructure, stores flush rather than commit, and provider/checkpoint units of work close before network entry. Account locking precedes ordered installation locks. The NULL-to-numeric identity transition raises inside the short UOW, whose exit rolls back/releases row locks before one bounded application retry. Lifecycle changes retain state/worker fences and separate inactive-origin cancellation from pause; reconciliation releases each origin before acquiring the next. Provider fences use SHARE while Run writes remain exclusive.

Inspected test code rather than accepting developer summaries: synchronized callback/sync and identity-transition regressions, retired-origin claim/retry/reconciliation controls, concurrent-reader/FK compatibility, migration literal upgrade/downgrade expectations, POST receipt audit with no later HTTP, local HTTP PostgreSQL skip fixture, blob cancellation, and mandatory merge-base token/GET checkpoints. Docs describe the resulting behavior and explicitly retain future companion-head validation as pending.

Full gates were not rerun by this read-only reviewer; the coordinator owns execution and final gate evidence. No source or Git mutations performed.

## Spec

Spec review: **0 actionable findings**.

Reviewed the complete tracked working-tree delta against `9aedfa8e25185a8c3daf00fe37de7a4236f9a70c` (HEAD `ed62aad4154ed65409b4ae1ab65d76fbc4afe297`), including uncommitted corrections and the untracked account-lock helper. Sources were the approved correction plan/checklist, issue #113, original review and historical feature plans. This is an independent code/test inspection; full gates belong to the coordinator.

Verified:
- Positive sync defers the unique same-account archive without target/default-rule/grant creation; fresh exact OAuth alone transfers archive identity/history. Permanent late-deletion clean targets and missing-account clean-grant controls match documented decisions.
- Callback/sync account fences precede globally ordered installation locks. A NULL→numeric identity change raises before archive access; the failed UOW rolls back/closes before exactly one retry. Both public paths have synchronized PG regression coverage and bounded-retry unit controls.
- Migration 0029 distinguishes partial legacy revocation from permanent installation deletion and folds availability into enabled on downgrade.
- Claim, retry/failure and stale reconciliation terminalize inactive origins while respecting state/worker/lease ownership. Installation/repository SHARE fences preserve ordering; terminal history remains fenced.
- Successful review POST preserves its returned review ID in append-only audit, starts no comment GET after retirement, and leaves findings/comments protected. RUNNING context tests cover absent/existing payloads and active controls.
- Request checkpoints remain mandatory, including merge-base token and comparison GET boundaries. Current-main DiffMap, anchor, provenance and policy modules are unchanged; current-main design terminology remains preserved.

Publication/gates are still pending coordinator work. Companion #127/#125/#126/#121 combined-head integration and frontend 409 wording remain explicitly disclosed external limitations, not missing implementation in this approved scope.

Summary: Standards 0 findings (no worst issue); Spec 0 actionable findings (no worst issue). Full gates and publication remain coordinator-owned.

## Remaining external requirements

Companion #127/#125/#126/#121 remain open. Their required merge order, subsequent
rebase and full combined-head validation are not complete. Preserve #121 outcome
and mandatory origin kwarg, #127 fail-closed AuthScope text, and check the old
waiting CI receipt outcome at that integration. Current-head human approval,
reviewer-resolved threads and required CI remain before-merge requirements.
The frontend 409 wording follow-up and original #106 legacy-history limitation
are disclosed; no frontend edit or identity backfill is included.

## Pre-commit verification

Configured `uv run pre-commit run` on all staged correction files passed:
trailing whitespace, EOF, merge conflicts, full-repository ruff lint/format and
mypy. YAML/TOML had no staged files; the all-files run separately passed their
checks, along with ruff/format/mypy. The initial all-files EOF fixer touched six
pre-existing prompt golden `.txt` fixtures outside this correction. Their bytes
were restored from the unchanged index before the successful staged run.
No unrelated fixture change is included. No new dependency was added.

There is no installed shared Git `pre-commit` hook. The actual configured hooks
were invoked explicitly through uv; the normal commit does not use `--no-verify`
and does not install or alter hooks in the original checkout. SHA-256 comparison
of all 354 app/alembic/test Python files before and after hooks proved no source
or test-code mutation. Full pytest evidence remains applicable to that source.
