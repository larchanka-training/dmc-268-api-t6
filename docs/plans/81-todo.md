# Task List: remove reviewer intent / timeline (#81)

Plan: [81-plan.md](81-plan.md). Human approved the plan, public seams, coordination comments, and draft PR publication on 2026-10-14.

## Task 1: Pin retained product behavior with failing tests

**Description:** Specify ignored direct reviewer deliveries and preservation of
legacy values before removing the state machine. Existing dispatcher tests remain.

**Acceptance criteria:**

- [x] Both review-request actions return `IGNORED_UNRELATED` directly from the
  projector without UOW, lock, provider, save, commit, cancellation, or publication.
  Use fail-on-call fakes so bypassing the guard fails the test.
- [x] Literal non-default historical values in all seven reviewer fields survive
  metadata, synchronize, close/reopen, and label projection; assert active metadata,
  PR state, label state, and cancellation still have their documented outcomes.
- [x] Existing durable dispatcher tests keep `IGNORED_IRRELEVANT_EVENT` and no
  projector/trigger invocation for both actions.

**Verification:**

- [x] `uv run pytest tests/test_github_pr_projection.py tests/test_github_installation_dispatch.py`
  shows new tests fail for the intended missing behavior before implementation.
- [x] Record the failure and assertion; no tests derive expected values from helpers.

**Dependencies:** Approved plan. **Files likely touched:**

- `tests/test_github_pr_projection.py`
- `tests/test_github_installation_dispatch.py` (only if existing assertions need strengthening)

**Estimated scope:** Small: 1–2 files.

## Task 2: Remove the obsolete application path

**Description:** Remove reviewer/timeline dependencies and behavior while keeping
record fields and the active projection pipeline. Update mixed tests and remove
reviewer-only tests, adapter tests, and their imports/helpers.

**Acceptance criteria:**

- [x] No timeline protocol/types, parameter, HTTP adapter, reviewer apply/staleness,
  timeline fetch/reconciliation, or reviewer barrier writes remain in the application.
- [x] Task 1 tests pass; live metadata/head freshness, label reconciliation, state,
  projection serialization, current provider checkpoints, cancellation and
  post-commit signal tests remain; mixed migration test uses direct legacy seeding
  instead of invoking removed assignment behavior.
- [x] `models.py`, `github_pull_request_projection.py`, and migrations have no diff;
  `PullRequestRecord` reviewer fields and defaults remain.

**Verification:**

- [x] `uv run pytest tests/test_github_pr_projection.py tests/test_github_installation_dispatch.py tests/test_github_run_trigger.py`
- [x] Inspect new/edited tests and source for network calls outside transactions.
- [x] Check diff against base for model, mapper, migrations and record fields.

**Dependencies:** Task 1. **Files likely touched:**

- `app/modules/reviews/application/project_github_pull_request.py`
- `app/modules/integrations/webhooks/infrastructure/github_reviewer_timeline.py` (delete)
- `tests/test_github_pr_projection.py`

**Estimated scope:** Medium: 3 files.

## Checkpoint after Tasks 1–2

- [x] Red/green proof recorded; current projection behavior covered.
- [x] No removed symbols remain except pending sink/retirement test cleanup.
- [x] Historical reviewer fields preserved; database declarations unchanged.

## Task 3: Keep PostgreSQL retirement guarantees on the live provider

**Description:** Remove fake/HTTP timeline phases from DB tests and exercise the
remaining current provider and label path for source retirement and rebinding.

**Acceptance criteria:**

- [x] Active/retired source cases for synchronize, closed, and labeled still pass
  with current provider calls only; retirement before reads suppresses network.
- [x] Retain current-provider/label token checkpoint tests, no open transaction
  across provider calls, source revalidation after reads, and no writes/cancellation
  against rebound target history; remove timeline-page-only cases.
- [x] Replace any useful retirement-after-timeline coverage with retirement during
  the current read before database application, without adding a second fake phase.

**Verification:**

- [x] With configured `TEST_DATABASE_URL`, run
  `uv run pytest tests/test_retired_pr_projection_postgres.py tests/test_github_pr_projection.py tests/test_label_intent_migration.py`;
  explicitly record DB passes, not only skips.
- [x] Compare coverage intent before/after for retirement, locks, and cancellation.

**Dependencies:** Task 2. **Files likely touched:**

- `tests/test_retired_pr_projection_postgres.py`

**Estimated scope:** Small: 1 file.

## Task 4: Complete adapter cleanup and record the decision

**Description:** Remove the deleted adapter from path safety tests and update the
single SD legacy-decision row once ownership comments have been authorized/sent.

**Acceptance criteria:**

- [x] Remove only timeline import and sink registration from request-path tests;
  retain every remaining adapter/path case, including PR154 changes after rebase.
- [x] SD §16 records A/code removal, links #81, retains all seven columns by the
  tech-lead decision, and agrees with Р-10 / §6.1 label plus CI behavior.
- [x] `git grep -n "ReviewerTimelineProvider\|_apply_reviewer_intent" -- app tests`
  is empty (exit 1 expected); timeline-specific types and helpers have no residual
  imports. Reviewer references remain only for retained data/transport compatibility.

**Verification:**

- [x] `uv run pytest tests/test_github_repository_path.py`
- [x] Review diff scope and SD row against §6.1; check coordination comments on
  open PR154/148/124 exist or their ownership no longer applies.

**Dependencies:** Task 3; explicit authorization and posting of plan's PR comments.
**Files likely touched:**

- `tests/test_github_repository_path.py`
- `docs/SYSTEM_DESIGN.md`

**Estimated scope:** Small: 2 files.

## Checkpoint after Tasks 3–4

- [x] Changed DB tests pass with PostgreSQL; path tests retain all surviving sinks.
- [x] Issue A grep AC satisfied; model/mapper/migrations unchanged.
- [x] SD decision complete and active product trigger unaffected.

## Task 5: Verify and prepare review

**Description:** Run required gates, inspect final scope, and provide the result
for the authorized publishing/review workflow from the isolated worktree.

**Acceptance criteria:**

- [x] All four gates pass: `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy .`, `uv run pytest`; list integration skips and retain explicit
  PostgreSQL evidence from Task 3.
- [x] Branch is rebased on fresh main; diff contains only #81 work, no #111/#113/#117
  content; no dependencies or migrations added, and every issue A AC is proven.
- [x] PR uses conventional title ≤72 chars, What/Why/How to verify/Refs body, and
  `Refs #81`; current-head approving review and resolved review threads are needed
  before merge. Issue closure is left to the tech lead.

**Verification:**

- [x] Execute all four gates and capture results; review git status/diff and baseline.
- [x] Recheck ownership and current main before requesting review.

**Dependencies:** Tasks 1–4. **Files likely touched:** No new implementation files;
plan/todo status updates and PR metadata only as publishing workflow authorizes.

**Estimated scope:** Small verification/publishing task.

## Final checkpoint

- [x] Human plan approval recorded before implementation.
- [x] Required ownership comments sent only after authorization.
- [x] Issue AC, live behavior regression tests, DB evidence, and gates complete.
- [x] Independent standards/spec review completed under agent-loop.
