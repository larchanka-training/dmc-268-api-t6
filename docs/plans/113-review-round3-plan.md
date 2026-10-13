# PR #132 round-three review addendum (#113)

## Scope and authorization

This addendum handles axyi's 2026-10-12 16:10 UTC review of published head
`83a36290192369197f6f72f5b84a65f1add6d1d1`, based on remote main
`834dd24c554e62d725060ecbaf19901dc709542e`. #127 merged during publication
preparation; the branch was subsequently rebased onto main
`488475e4a3d48732d1d15860ee5718dfb83c10e5`, preserving the tested source/runtime
tree. [Final proof](113-review-round3-final.md) records exact validation provenance.
It supplements the approved
[original plan](113-plan.md), [original todo](113-todo.md),
[review plan](113-review-plan.md) and [review todo](113-review-todo.md).
User authorization to handle comments using agent-loop and only a worktree persists.
The three blocking P2 findings require tests; production behavior is already correct.
No production code, dependency, new PR, merge or author-resolved review thread is planned.

Read AGENTS.md, .agents/README.md, backend/git-workflow rules, planning, TDD,
code-review and pull-request skills. Fresh Codex agents receive brief-file paths
with no inherited history. The coordinator orchestrates; developers own edits.

## Ownership and partition

A fresh open-PR audit found no foreign owner of the three test files below.
Shared-doc coordination comments were posted before edits to #148, #137, #127,
#124, #121, #126 and #125. Limit edits to the documented sections and preserve
companion changes, especially #121's RunTriggerOutcome and keyword-only
expected_installation_external_id and #127's AuthScope contract.

| Owner | Final edit ownership | Temporary mutation ownership |
| --- | --- | --- |
| A | tests/test_app_reinstall_postgres.py; new 113-review-round3-a.md | installation_repository_unit_of_work.py and github_installation_links.py |
| B | tests/test_app_reinstall_schema_postgres.py; tests/test_run_origin_guard_postgres.py; new 113-review-round3-b.md | migration 0029 and run_lifecycle_store.py |
| C | SYSTEM_DESIGN §6.9, WEBHOOK_WORKER deferral paragraph, CICD first-origin rollout; personal-path cleanup in existing 113 plan/proof docs; new 113-review-round3-c.md | None |
| Coordinator/publisher | round-three plan/todo, full final proof, existing PR body/replies; UI draft presentation | None |

Mutation proofs temporarily edit an existing implementation seam, make the new
regression fail, restore the exact bytes, then rerun its focused green check.
A and B must coordinate before any mutation: other agents' test runs and final gates
must never overlap a temporary mutation. No mutation survives in the final diff.
Use isolated PostgreSQL fixture schemas; never mutate shared database/public state.

## Task A1 — existing clean target recovers after remove/add (blocking P2)

**Description:** Protect the reachable existing-is-None deferral branch using real
OAuth and synchronization/removal use cases. Place the test beside the existing-target
collision test. Seed archive/setup prerequisites but never hand-insert the clean Repository.

**Acceptance criteria:**
- T already has verified numeric identity and the archive's Workspace. A different user
  supplies a fresh OAuth snapshot with account identity absent and exact repository 101
  listed: no rebind, personal grant accepted, sync creates a distinct clean target row.
- Repository removal on T sets installation_available=false and removes its grants;
  repeated added/sync restores exactly one T row with the same clean UUID and True
  availability. S's archived row, history, pause, settings and ownership stay intact.
- Removing only `existing is None` makes the regression fail; exact source restored.

**Verification:** Focused PG test red under mutation, green on unchanged production code.
**Dependencies:** None. **Files:** A ownership above. **Scope:** Small.
[Review thread](https://github.com/larchanka-training/dmc-268-api-t6/pull/132#discussion_r4250090363).

## Task B1 — row 108 isolates fresher grant (blocking P2)

**Description:** Add legacy row 108 to the existing schema-upgrade test.
**Acceptance criteria:**
- Seed enabled=false, partial repository revocation, and a grant newer than revocation.
  Literal upgraded expectation is `(108, False, True)`; user pause remains intact.
- Removing the fresh-grant NOT EXISTS clause and separately reversing its freshness
  comparison each make this regression fail. Existing whole-deletion/partial/downgrade
  controls remain green; do not edit migration behavior in the final diff.
**Verification:** Real upgrade from 0028; focused mutation red/restore green.
**Dependencies:** None. **Files:** schema test and B proof. **Scope:** Small.
[Review thread](https://github.com/larchanka-training/dmc-268-api-t6/pull/132#discussion_r4250091187).

## Task B2 — repository removal and owning worker finish (blocking P2)

**Description:** Expand inactive-origin tests without changing lifecycle behavior.
**Acceptance criteria:**
- Add `repo_removed` to prepare and reconcile/retry matrices: installation remains live,
  Repository installation_available=false. queued/running/publishing reconcile twice
  leaves cancelled/installation_unavailable, no queue/review/retry/DLQ/provider calls.
- Direct owning-worker `finish(failed)` from running and publishing with inactive origin
  becomes cancelled/installation_unavailable. Preserve ownership/terminal history;
  keep displaced-worker controls. Exercise retired/rebound/NULL/repo_removed as useful.
- Removing Repository availability from stale_inactive_origins and separately removing
  finish's outcome rewrite each fail new tests; restore and rerun focused green checks.
**Verification:** PG matrices with literal state/reason, no-message assertions and active
controls. Optional small controls: newly active retire_stale_origin returns False unchanged;
inactive origin takes priority over user pause in guard.
**Dependencies:** None; B1/B2 share B's sequential mutation window.
**Files:** origin guard test and B proof. **Scope:** Small.
[Review thread](https://github.com/larchanka-training/dmc-268-api-t6/pull/132#discussion_r4250091604).

## Checkpoint 1 — blocking tests

All three P2 regressions green and their isolated mutations red; production files
restored. Evidence records actual commands/results, no predicted counts.

## Task A2 — sorted prelocks and late deletion without listing (P3)

**Acceptance criteria:**
- Deterministic overlapping callbacks with reversed installation snapshot order and NULL
  snapshot numeric account IDs exercise row prelocking without advisory-account
  serialization masking it. Use observed pg_blocking_pids/lock waits or barriers,
  bounded timeouts and cleanup; both complete. Removing callback's sorted prelock fails.
- Add a callback/sync arrangement that makes the sync sorted installation prelock
  necessary and prove its isolated removal fails. Distinguish snapshot NULL identity
  from stored installation identity; avoid a redundant test serialized solely by account
  advisory locks. No random stress or sleep-only scheduling as proof.
- Add c-prime: OAuth snapshot has no repository listing in the permanent late-source
  deletion test. Verify clean target remains usable across sync and repeat OAuth with
  no accepted personal repo grant shielding `archive == target.workspace_id`.
  Removing that Workspace equality must fail the new control.
**Verification:** Focused PG races and isolated mutation red/restore green evidence.
**Dependencies:** A1; production prelocks stay intact. **Files:** A ownership. **Scope:** Small.

## Task C1 — explicit accepted limits and accurate deployment/docs (P3)

**Acceptance criteria:**
- SD §6.9 explicitly accepts this race: successful POST retains review_id but checkpoint
  cancellation before comments GET gives empty/unknown comment IDs; remove → re-add
  before completion can produce succeeded/published=true without local Comment rows.
  External inline comments may exist. No HTTP lookup starts while origin is unavailable;
  this PR does not expand receipt identity/unknown-comment contract. Explain observable
  limitation without asserting complete receipt or inventing new production semantics.
- WEBHOOK_WORKER says deferred onboarding applies to PG only: tree GET and labels POST
  can already have occurred before deferral. CICD requires stopping every old API,
  webhook-worker, worker and independent Run writer BEFORE MERGE #132 into main,
  because Deploy staging automatically starts on main push; coordinate merge timing
  with tech lead, keep writers stopped through bootstrap, resume only new-image services.
- Replace personal worktree paths and local DSNs in existing plan/proof docs with
  `<worktree>`, `<dev-db>` and `<dev-broker>`; retain truthful historical commands/results.
  Prepare PR body draft naming original and review plan/todo plus round-three addendum;
  correct rebase account: #134 merge-base checkpoint integration, #108 SYSTEM_DESIGN
  resolution and #138 unified-diff/parser import integration, not import-only changes.
**Verification:** Diff/rg for private paths/DSNs, read contracts against source and actual
range-diff/history; no unsupported gate claims. **Dependencies:** Published doc ownership
comments (complete). **Files:** C ownership; publisher metadata stays temporary. **Scope:**
separate three small documentation edits followed by historical-reference cleanup.

## Task C2 — frontend follow-up preparation (P3)

UI #106 does not exist; the tracker is API #106. UI source hardcodes one misleading
409 message for active/closed, user pause and App-unavailable reruns. Fresh issue search
has no narrow existing duplicate. Prepare one Russian UI Task with linked API #106 and
#132, exact source/tests and bounded acceptance criteria. Use create-issue preflight and
body emitter, local-path sweep, full rendered preview. New issue creation requires the
user's explicit word `Create` after preview. The full draft was shown after
implementation and documentation were complete, while the final full-suite gates ran. Do not post the issue or tracker comment yet.
A frontend-only message can list all actual conflicts if no machine-readable reason
exists; do not require unsafe display of raw English detail or invent an API discriminator.
**Verification:** DoR/spec emitter succeeds, no personal references, no duplicate title.
**Dependencies:** None; independent draft preparation, no frontend code changes.

## Checkpoint 2 — current-head proof and review

Once source mutations stop, run `uv run ruff check .`,
`uv run ruff format --check .`, `uv run mypy .`, `uv run pytest -rs` with disposable
PostgreSQL `<dev-db>` and RabbitMQ `<dev-broker>`. Assert integration tests actually
run with zero skips. Collect real counts/timings. Fresh independent Standards and
Spec agents review full base-to-working-tree diff and exact issue/review requirements.
Resolve findings through owning developers; repeat necessary gates after any change.

## Publication and before-merge prerequisites

Commit/push only intended changes to existing PR #132, Conventional Commits + Refs #113,
normal hooks; no --no-verify/new PR. Verify remote branch head before push; fail on foreign
movement rather than overwriting it. Update body with actual base/head, fresh evidence,
accepted limitations and all plan/todo paths. Reply to all three open P2 threads with SHA
and mutation evidence; answer P3 with tests/docs or precise accepted-limit rationale.
Reviewer alone resolves threads. Monitor required CI; do not claim approval/merge.

#127 has merged and is included. #125/#126/#121 must land before #132. Then
rebase #132 last and run all gates on the
combined head; preserve #121 outcome + expected_installation_external_id, #127 AuthScope
and #125/#126 deployment work. Test waiting old-CI-receipt outcome at that integration.
Current-head approval and reviewer-resolved threads remain required. Stop old writers
BEFORE MERGE and agree timing with axyi because deployment is automatic. This plan
prepares changes now; it does not execute companion merges or stop production services.
