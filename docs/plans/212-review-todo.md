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
- Preserve main DiffMap and worker-comment versions; retain pins, 0038 parent
      and current CI sharding; add Refs #245 footer to follow-up commit.
- Ruff lint/format, mypy, full pytest and available real integration gates pass;
      independent Standards/Spec reviews have no outstanding actionable findings.
- Public warning appears BEFORE force-with-lease; pushed-head CI passes;
      reply to each P2 thread with SHA, summarize all P3, request axyi review.

**Verification:** saved commands/results and GitHub head/check/reply inspection;
never resolve author-owned threads or close issue. Refresh main/migration graph
before publishing and coordinate any newly advanced migration head.
**Dependencies:** Tasks 2–6. **Scope:** Verification plus concrete conflict fixes.
**Files:** only necessary rebase resolutions and review evidence updates.


## Local verification — 2026-10-25

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
