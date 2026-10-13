# Task list: PR #132 follow-up (#151)

Approved by the user on 2026-10-13; see [151-plan.md](151-plan.md). Current branch
`fix/151-pr132-followup` addresses P3-1/2/3/4/6 in PR #152. P3-5 is delivered
separately in [PR #153](https://github.com/larchanka-training/dmc-268-api-t6/pull/153),
based independently on the same main. All technical implementation/publication
tasks below are complete; issue acceptance remains external.
Tasks 1-6 are verified; [151-verification.md](151-verification.md) records **3427
passed, zero skips in 1118.15s** and both independent reviews. The first PR is
published and attached as [#152](https://github.com/larchanka-training/dmc-268-api-t6/pull/152);
it was initially Draft, then marked Ready after all nine CI jobs passed on
`604423349f45bdc977cd770bd0414f22c9a97f41`. The latest metadata-head checks are
tracked separately under the external requirements below.

## Task 1: Establish baseline and ownership coordination

**Description:** Establish reproducible disposable infrastructure and satisfy the
repository ownership rule before implementation.

**Acceptance criteria:**

- [x] Human approves the saved plan under agent-loop; both branches start from main.
- [x] Coordinator rechecked open PR files and posted ownership coordination on
  [#124](https://github.com/larchanka-training/dmc-268-api-t6/pull/124#issuecomment-6153738352)
  and [#148](https://github.com/larchanka-training/dmc-268-api-t6/pull/148#issuecomment-6153738739).
- [x] Dedicated PostgreSQL 17 and random migrated schemas: exact three-file baseline
  passed **140 tests, zero skips in 98.55s** on `c1d8f59d9ff3f332a3595857dbe612115ad559e8`.

**Verification:** Inspect test output with `-rs`; verify schema isolation and cleanup.
Do not use an unrelated/shared DB or broker; do not print credentials.

**Dependencies:** None. **Files likely touched:** this checklist, optional #151
verification evidence document. **Estimated scope:** Small.

## Task 2: Add public production-composition regression (P3-1)

**Description:** Exercise the real worker claim and context-stage wiring through
`compose_worker_process(...).handle_run.execute(...)` with PostgreSQL.

**Acceptance criteria:**

- [x] Active origin claims the run and reaches real policy/context loading; observable
  imported-context or persisted-context evidence proves the active path succeeds.
- [x] Retirement during a policy HTTP response after claim prevents every subsequent
  policy/context request, avoids replacement credentials, and terminalizes cancelled
  with `installation_unavailable`; HTTP NOWAIT probes prove no open provider transaction.
- [x] No-op replacement of only the checkpoint passed into the stage makes the new
  request-boundary assertion fail; restored wiring passes. No private `_pipeline`
  access, permanent mutation, or signature-only RED evidence.

**Verification:** Run the focused PostgreSQL suite; record mutation failure and
restored GREEN. All four repo gates are required before the task is marked done.

**Dependencies:** Task 1 baseline; avoid source mutation unless worker ownership is
coordinated. **Files likely touched:** `tests/test_run_policy_context_origin_postgres.py`,
optional `docs/plans/151-verification.md`. **Estimated scope:** Small.

## Checkpoint: Composition proof

- [x] Three-file PostgreSQL baseline and new public composition regression are green
  without skips; mutation RED demonstrates the missing-checkpoint hazard.
- [x] Review verifies the test cannot pass on a path that never attempted context work.

## Task 3: State tested checkpoint guarantees (P3-2)

**Description:** Align round-eight request-fence documentation with its proof scope.

**Acceptance criteria:**

- [x] Initial pre-cache and upper `ReviewContextStage.begin/run` checks are explicitly
  described as defense in depth, without a separate zero-cache-call promise.
- [x] Preserve concrete post-cache-await/per-request guarantees and historical counts;
  link #151's new composition proof without presenting old results as current.

**Verification:** Compare wording to existing cache-await/context PostgreSQL tests
and source. Run four repo gates before marking done.

**Dependencies:** Task 2 evidence. **Files likely touched:**
`docs/plans/113-review-round8-context.md`. **Estimated scope:** Small.

## Task 4: Correct downgrade target losses (P3-3)

**Description:** Explain actual migration targets and irreversible preference loss.

**Acceptance criteria:**

- [x] CICD and SYSTEM_DESIGN identify target `20261012_0030` as already removing
  origin/account/deletion/availability and folding availability into enabled.
- [x] Both state `enabled=true/available=false` becomes false and re-upgrade does
  not restore the user preference or numeric identity; retained-state backfills
  do not guarantee former deletion/origin column values.
- [x] Deeper `20261007_0028` policy document/snapshot/convention losses are separate;
  frozen migrations remain untouched.

**Verification:** Read migration chain; use existing isolated data-bearing schema
regressions, optionally add the precise 0030/re-upgrade test if proof needs it.
Run required gates. Never downgrade an existing shared schema.

**Dependencies:** Task 1 ownership preparation. **Files likely touched:** `docs/CICD.md`,
`docs/SYSTEM_DESIGN.md`, optionally `tests/test_app_reinstall_schema_postgres.py`.
**Estimated scope:** Medium.

## Task 5: Document policy/AST origin fences (P3-4)

**Description:** Extend SYSTEM_DESIGN §8.4's explicit protected-write/request scope.

**Acceptance criteria:**

- [x] Protected writes explicitly include policy documents and snapshots.
- [x] Protected requests explicitly include policy/AST context tree and blob reads;
  active-origin/RUNNING and short-transaction rules remain accurate.

**Verification:** Compare docs with policy UOW/source adapters and existing
PostgreSQL request/persistence guards; run required gates.

**Dependencies:** Task 1 ownership preparation; coordinate sequentially with Task 4's same
file. **Files likely touched:** `docs/SYSTEM_DESIGN.md`. **Estimated scope:** Small.

## Checkpoint: Runtime contract documentation

- [x] Exact downgrade targets, boolean example, and protected paths match source.
- [x] Documentation claims do not exceed test evidence; no migration/source scope creep.

## Task 6: Label round-six evidence Historical (P3-6)

**Description:** Preserve prior evidence while making its historical scope explicit.

**Acceptance criteria:**

- [x] All four round-six final/todo/plan/integration docs explicitly say Historical,
  with checkpoint `d887257` based on `2251b59` (full SHAs in the notices).
- [x] Historical counts remain intact and wording does not claim current-main or
  current-head approval/gate status.

**Verification:** Search all four files for Historical, both SHAs, and misleading
present-tense claims; run required gates.

**Dependencies:** None after plan approval. **Files likely touched:**
`docs/plans/113-review-round6-{final,todo,plan,integration}.md`.
**Estimated scope:** Medium (four independent short notices).

## Task 7: Verify, review, and publish the first PR

**Description:** Publish a narrow reviewed test/docs follow-up for P3-1/2/3/4/6.

**Acceptance criteria:**

- [x] `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy .`,
  and `uv run pytest` pass; focused PostgreSQL AC tests run without skips.
- [x] Fresh independent Standards/Spec review has zero actionable findings on
  both axes for the complete first-PR diff based on `c1d8f59`.
- [x] Publisher commits with issue reference and opens/attaches
  [PR #152](https://github.com/larchanka-training/dmc-268-api-t6/pull/152) with
  What / Why / How to verify / Refs and explicit P3-5 follow-up scope.
- [x] Refresh main before publication: base remains `c1d8f59`; no rebase is needed.

**Verification:** Record exact tested head and relevant infrastructure/skips,
mutation proof, gates, reviews, and PR URL. No closing keywords or issue Development
link; tech lead owns issue closure.

**Dependencies:** Tasks 2-6. **Files likely touched:** this checklist, verification
proof if added. **Estimated scope:** Small.

## Separate Task F1: Create origin-lock refactor worktree (P3-5)

**Description:** After the first PR, create a separate managed worktree from refreshed
main with `chore/151-origin-locks`, and save its detailed implementation plan.
Approval of this combined plan authorizes both PRs; no fresh approval is required
unless the scope materially changes.
Publish independently without waiting for the first PR merge or importing its diff.

**Acceptance criteria:**

- [x] Refactor has its own worktree/branch/PR; no P3-5 implementation enters this branch.
- [x] Real PostgreSQL three-file baseline passes without skips before the refactor.
- [x] Caller inventory and SQL/guard/transaction invariants in 151-plan.md are retained.

**Verification:** Independent [PR #153](https://github.com/larchanka-training/dmc-268-api-t6/pull/153)
records a baseline of **140 passed, zero skips in 180.94s**, branch
`chore/151-origin-locks`, base `c1d8f59`, and unchanged caller invariants.

**Dependencies:** Task 7 (published; merge is not a prerequisite). **Files likely touched:** future
plan/todo documents. **Estimated scope:** Small.

## Separate Task F2: Consolidate origin lock ordering

**Description:** Extract shared installation FOR SHARE -> Repository FOR SHARE ->
Run FOR UPDATE sequencing; retain caller-specific selects and guards.

**Acceptance criteria:**

- [x] Policy documents/snapshot, diff storage, five lifecycle paths and the trace-store
  context writer use a shared
  typed helper without changing origin/running/revision checks or row refresh.
- [x] Transaction boundaries, exception codes, repository flush-only behavior and
  network-outside-transaction invariant remain unchanged.
- [x] Real PostgreSQL retirement and competing rebind/lock-wait regressions listed
  in the plan pass, including row refresh after blocking and NOWAIT probes.

**Verification:** PR #153 records **256 focused passed, zero skips in 372.57s**,
**3425 full-suite passed, zero skips in 753.16s**, all static gates and fresh
Standards/Spec reviews with zero findings. A Run-first mutation caused five
meaningful NOWAIT failures; exact restored source passes. External first-PR
composition control passes **2 tests, zero skips in 49.91s** while all 196 loaded
app files originate in the refactor worktree. No first-PR test/source is copied
into the refactor branch.

**Dependencies:** F1. **Files likely touched:** new `run_origin_locks.py`,
`policy_snapshot_unit_of_work.py`, `run_lifecycle_store.py`, `run_repository.py`
in reviews infrastructure; modify at most one focused regression file if needed.
**Estimated scope:** Medium (four primary files).

## Separate Task F3: Publish refactor PR and finish issue evidence

- [x] Publisher opens and attaches independent `refactor(reviews)`
  [PR #153](https://github.com/larchanka-training/dmc-268-api-t6/pull/153), referencing #151.
- [x] This combined technical ledger cross-references both PRs and all six P3 outcomes;
  external review, CI, merge, board transition and issue acceptance remain below.

**Dependencies:** F2. **Verification:** Inspect both PR diffs/evidence and issue AC.

## External Before merge and issue acceptance

These items are intentionally separate from completed implementation/publication.

- [ ] Required CI passes on the latest published heads of both PRs; coordinator
  verifies Ready status without assigning reviewers, as requested by the user.
- [ ] Board moves to On Review after both PRs are ready.
- [ ] Each PR is refreshed/rebased on current main before merge.
- [ ] Each PR has an approving human review on its current head.
- [ ] Reviewer-owned review threads are all resolved on each PR.
- [ ] Both PRs are merged under repository rules.
- [ ] Tech lead checks issue AC/DoD evidence and closes #151.
