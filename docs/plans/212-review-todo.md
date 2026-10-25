# PR #280 review round 1 — task checklist

User authorization permits immediate execution; no plan-approval pause.

## 1. Coordinate and establish the red regressions

**Description:** Protect ownership/stack boundaries and demonstrate the prompt bypass.

**Acceptance criteria:**
- [x] Post overlap comments for the exact open-PR owners listed in review plan.
- [x] Prompt-repository PG cases fail on pinned `warning` versus changed live
      `critical`; historical NULL case returns a changed nondefault live threshold.
- [x] Postprocessing unit cases cover distinct pinned/live pairs and nondefault fallback.

**Verification:** focused PostgreSQL and unit runs; failures must assert the settings.
**Dependencies:** None. **Scope:** Small.
**Files:** `tests/test_review_prompt_pins_postgres.py` (or existing publication PG
suite), `tests/test_sqlalchemy_run_repository.py`.

## 2. Make threshold conversion explicit

**Description:** Replace casts with the existing exhaustive enum mapping shared by adapters.

**Acceptance criteria:**
- [x] Shared infrastructure converter maps all enum members; NULL stays explicit.
- [x] Snapshot and postprocessing adapters use it; snapshot casts are removed.
- [x] Webhook/rerun writes use the same enum conversion convention.

**Verification:** mypy plus existing snapshot and creation suites.
**Dependencies:** Task 1 coordination. **Scope:** Medium (5 files).
**Files:** new `app/modules/reviews/infrastructure/severity_threshold.py`,
`run_repository.py`, `policy_snapshot_unit_of_work.py`, `webhook_runs.py`, `rerun_store.py`
(the last four in the same infrastructure directory).

## 3. Close the reflection input bypass

**Description:** Select Run threshold with historical NULL live fallback at its source.

**Acceptance criteria:**
- [x] Prompt repository returns pinned warning after Repository changes to critical.
- [x] NULL/missing-setting compatibility and missing-run behavior are preserved.
- [x] No change to execute_review.py or store_review_output.py is needed.

**Verification:** Task 1 PG regressions green plus existing prompt tests.
**Dependencies:** Tasks 1–2. **Scope:** Small.
**Files:** `app/modules/reviews/infrastructure/review_prompt_repository.py`, selected PG test.

## Checkpoint: correctness

- [x] P2-1 and P3-1/P3-4 pass meaningful real-DB/unit checks.
- [x] Snapshot/digest contracts and legacy fallback remain unchanged.

## 4. Strengthen baseline compatibility oracles

**Description:** Lock baseline identity independently and remove redundant test typing/noise.

**Acceptance criteria:**
- [x] Digest/cache literals come from unchanged baseline 1ae74b1 fixture inputs.
- [x] Both historical and pin variants equal literals; existing formula oracle stays.
- [x] Remove redundant identity-adjacent assert and type threshold without ignore.

**Verification:** focused snapshot/context-stage tests and mypy.
**Dependencies:** Task 1. **Scope:** Small.
**Files:** `tests/test_prepare_policy_snapshot.py`, `tests/test_review_context_stage.py`.

## 5. Document migration loss precisely

**Description:** Register revision 0038 and its actual schema/downgrade behavior.

**Acceptance criteria:**
- [x] CICD first registry row lists four columns, two checks, retained enum, NULL on re-upgrade.
- [x] CICD/architecture/SD inventories include 0038 and match actual migration order.
- [x] SD Run/snapshot schemas describe implemented pins and historical NULL pairs.

**Verification:** review migration test against each documented claim; diff check.
**Dependencies:** Task 1 coordination. **Scope:** Medium (3 files).
**Files:** `docs/CICD.md`, `docs/BACKEND_ARCHITECTURE.md`, `docs/SYSTEM_DESIGN.md`.

## 6. Align remaining current-state prose

**Description:** Correct RFS/TEST_PLAN and remove transient implementation notes.

**Acceptance criteria:**
- [x] RFS §7.2/introduction and TEST_PLAN describe current retry/rerun pins with regression link.
- [x] Historical fallback and remaining future prompt/profile work stay explicit.
- [x] Existing plan/todo contain no local DSN, temporary evidence paths or agent narrative.

**Verification:** search changed docs for obsolete live-read claims and transient paths.
**Dependencies:** Task 5 for shared SD wording. **Scope:** Medium (4 files).
**Files:** `docs/RULES_FORMAT_SPEC.md`, `docs/TEST_PLAN.md`,
`docs/plans/212-plan.md`, `docs/plans/212-todo.md`.

## Checkpoint: full review coverage

- [x] All three P2 and P3-1 through P3-6 have concrete changes/tests.
- [x] P3-7/8 handled by preserving main broker comment and migration compatibility.

## 7. Rebase, verify, publish responses

**Description:** Integrate current main and deliver a current-head reviewable patch.

**Acceptance criteria:**
- [x] Preserve main DiffMap and worker-comment versions, pins and current CI
      sharding; keep Refs #245 footer. Final migration parents follow Task 8.
- [x] Ruff lint/format, mypy and focused real-service gates pass after Task 8;
      independent Standards/Spec reviews have no outstanding actionable findings.
- Final-head Linux CI must prove the complete suite with zero integration skips;
  current-head evidence is tracked in PR #280 rather than asserted by old local results.
- Publication warns BEFORE force-with-lease, verifies the remote head and explicit
  lease, waits for pushed-head CI, replies with SHA to each P2 thread, summarizes P3,
  and requests axyi review. PR #280 records live completion evidence.


**Verification:** saved commands/results and GitHub head/check/reply inspection;
never resolve author-owned threads or close issue. Refresh main/migration graph
before publishing and coordinate any newly advanced migration head.
**Dependencies:** Tasks 2–6. **Scope:** Verification plus concrete conflict fixes.
**Files:** only necessary rebase resolutions and review evidence updates.


## Historical local verification — 2026-10-25 (05a96bc)

The review patch was rebased onto `05a96bc` before verification. Main's DiffMap
publication path and broker polling explanation were preserved; migration
`20261024_0038` still follows `20261020_0037`. The implementation does not change
`execute_review.py` or `store_review_output.py` relative to that base.

- Required static gates passed: `uv run ruff check .`,
  `uv run ruff format --check .` (568 files), `uv run mypy .` (558 source files).
- Focused real-service and compatibility suite: **210 passed, 0 skipped**.
- Full `uv run pytest` with disposable PostgreSQL and RabbitMQ:
  **5988 passed, 2 failed, 0 skipped**, including **999 integration tests passed**;
  2 existing deprecation warnings; 1944.15 seconds.
- Both full-suite failures are the unchanged workflow shell cases
  `test_validate_uses_the_profile_of_the_selected_corpus[main-legacy]` and
  `[main-none]`. macOS `/bin/bash` 3.2 rejects an empty `profile` array under
  `nounset`; the same workflow/test blobs reproduce on main baselines `05a96bc`
  and `74b42a7`. The exact workflow step succeeds for both cases under Linux.
  No unrelated workflow change was made.
- Independent Standards and Spec reviews reported **0 findings**.

The local full suite is not claimed green. Subsequent main-only changes require
a final rebase with focused checks; current-head Linux CI and publication evidence
are tracked in [PR #280](https://github.com/larchanka-training/dmc-268-api-t6/pull/280).

## 8. Preserve merged #130 D while integrating current main

**Description:** Rebuild safely from the now-combined PR head, preserving foreign
commits, then apply the already-committed review fixes on the new main base.
This task supersedes Task 7's original 0037-parent and separate-stack assumptions.

**Acceptance criteria:**
- [x] Back up local HEAD; start from combined remote `3f8d1da71f6266f44928fa8fc6374073d64ac730`,
      rebase #212 plus all six #130 D commits onto main
      `48636f2cb456ecc07b7195cea3c98e36a280c376`, then cherry-pick ONLY `3c4d32c`.
      Preserve all foreign work and every P2/P3 fix; do not replay `ab5f468`.
- [x] Preserve IDs with one chain 0037 → `20261023_0038` → `20261024_0038` →
      `20261024_0039`; final head assertions are 0039. Integrate registry/schema
      documentation for both carried revisions and main's RAG migration. RFS says
      complete prompt bindings are pinned for new runs by #130 D, while execution
      profile is fixed at the first snapshot. No execute/store or F wiring scope.
- [x] Static and focused real-PG #212/#130 D/migration/RAG/context/cache checks pass;
      independent baseline identity fixtures remain valid and all six foreign
      commits are preserved. Standards and Spec reviews report zero findings.
- Final-head Linux CI supplies complete-suite/zero-skip evidence. Publication
  warns first, verifies the remote still equals `3f8d1da`, uses that explicit lease,
  and describes all combined work in PR #280 before replies/review request.


**Verification:** Implementation agent checks commit ancestry/content preservation,
new migration graph and literal head tests; compare reviewed source changes and
foreign behavior after semantic conflict resolution. Rerun focused gates on the
new base; old 210 focused/999 integration passes are historical evidence only.
Do not repeat the known 32-minute macOS full-suite platform failures. Linux CI's
full tests and zero-skip integration remain required on the final pushed head.

**Dependencies:** Tasks 1–6 implemented; Task 7 publication waits for this task.
**Scope:** Bounded integration pass in sequential checkpoints: history preservation,
then schema/documentation alignment, then focused checks and final CI. Delegate
source conflict resolutions only to the implementation agent; this planning task
changes no source, history or GitHub state.
**Files:** necessary semantic conflict files from main/#212/#130 D only; prioritize
migration parent/head tests, CICD/BACKEND_ARCHITECTURE/SD/RFS and affected context
regressions. Preserve main CI and RAG work. No new feature or migration ID.


## Combined-branch verification — 2026-10-25

The combined remote head `3f8d1da` was preserved and rebased onto `48636f2`.
Only review fix `3c4d32c` was then applied; the rewritten feature was not replayed.
The integration also includes main `bb09caeefcd6b4a18d7cd4e3d6e517d6848ae0db`
(policy-tree handling). All six merged #130 D commits survive; their critical
model, candidate, rerun, migration and regression files match the combined remote.
The only D commit range difference is the migration graph's additional main row.

The single migration chain is `20261020_0037` → `20261023_0038` →
`20261024_0038` → `20261024_0039`. The #212 downgrade test now targets its actual
main parent and proves main's RAG throttle columns survive removal of #212 pins.
The new main throttle test's obsolete head assertion failed first, then passed
with the literal final head 0039. Existing baseline digest/cache literals passed
unchanged. Main RAG/context, execution/store paths and CI configuration are retained;
no #130 F wiring was added.

Current-base local checks:

- `uv run ruff check .`, `uv run ruff format --check .` (589 files),
  `uv run mypy .` (579 source files), and `git diff --check`: passed.
- Publication/prompt pins, snapshots, origin, context, creation and schema checks:
  **270 passed, 0 skipped**.
- Migration-head compatibility cases: **4 passed**, 117 unrelated cases deselected.
- RAG/context/graph, policy-source, prompt-seeding and worker compatibility:
  **463 passed, 0 skipped**.
- Independent Standards and Spec integration reviews: **0 findings**.

These focused checks describe the combined branch on `bb09caee`. The earlier
210/999 results are historical, not current-head full-suite evidence. Definitive
complete verification is the final pushed head's Linux CI in PR #280.
