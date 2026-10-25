# Task List: #212 publication settings pinned to run

Every implementation task follows red → green → refactor with literal spec-based
assertions.

## Task 1: Immutable snapshot contract regressions

**Description:** Add failing tests for carrying pins without changing policy/cache identity.

**Acceptance criteria:**

- [x] Snapshot preserves both publication fields, including a historical NULL pair.
- [x] Changing only either publication setting leaves existing digest and conventions
      cache key unchanged; the existing digest oracle is untouched.
- [x] Repeated snapshot preparation returns the first stored pair.

**Verification:** `uv run pytest tests/test_prepare_policy_snapshot.py tests/test_policy_snapshot.py tests/test_repo_conventions.py` (capture expected red failure, then green later).

**Dependencies:** None.

**Files likely touched:** `tests/test_prepare_policy_snapshot.py`, `tests/test_policy_snapshot.py`, `tests/test_repo_conventions.py`.

**Estimated scope:** Medium (3 files).

## Task 2: PostgreSQL acceptance regressions

**Description:** Add a dedicated migrated-database scenario that exposes live-read behavior.

**Acceptance criteria:**

- [x] Actual webhook store creation, settings mutation, postprocessing and retry
      prove the original pair controls output before any snapshot exists.
- [x] Actual rerun store creates a new pair from current Repository values while
      original Run pins are unchanged, including stale identity-map coverage.
- [x] Historical NULL Run/snapshot cases prove explicit live fallback; all tests
      need only disposable PostgreSQL and existing test helpers.

**Verification:** `TEST_DATABASE_URL=... uv run pytest tests/test_run_publication_settings_postgres.py -q -rs`; record red failures against unmodified behavior and check they assert outcomes rather than mirror SQL.

**Dependencies:** None; write after agreeing the contract in Task 1.

**Files likely touched:** new `tests/test_run_publication_settings_postgres.py`.

**Estimated scope:** Small (1 file).

## Task 3: Nullable persistence schema

**Description:** Add Run/snapshot publication columns without inventing historical values.

**Acceptance criteria:**

- [x] Both tables store nullable enum/cap pairs with named validity/pair constraints;
      migration leaves old rows NULL and adds no server default/backfill.
- [x] New revision chains from current main, reuses the native enum, and downgrade
      removes only new columns/constraints without dropping Repository's enum.
- [x] A migrated PostgreSQL schema rejects invalid pairs and survives
      upgrade/downgrade/re-upgrade with historical rows preserved.

**Verification:** focused new migration integration test plus `uv run ruff check .`, `uv run mypy .`; use existing per-file TEST_DATABASE_URL skip convention.

**Dependencies:** Task 2 (regression definitions).

**Files likely touched:** `app/modules/reviews/infrastructure/models.py`, new `alembic/versions/20261024_0038_run_publication_settings.py`, new `tests/test_run_publication_settings_migration.py`, `tests/test_app_reinstall_schema_postgres.py` (append the literal new graph head; actual revision suffix must match refreshed head).

**Estimated scope:** Medium (4 files).

## Checkpoint: Foundation

- [x] Digest compatibility tests remain unchanged in meaning.
- [x] Migration/constraints pass on real PostgreSQL, historical pairs remain NULL.
- [x] Acceptance failures now identify missing creation/read behavior only.

## Task 4: Run creation pins

**Description:** Capture a fresh settings pair in the transaction shared by webhook/rerun creation.

**Acceptance criteria:**

- [x] RunInsertCandidate requires both settings; lock_candidate reads fresh
      Repository state in the creation UoW, including an already-loaded ORM entity.
- [x] Webhook insert and rerun insert populate both fields; queue payloads do not
      become the settings authority and existing lock ordering is preserved.
- [x] Both creation branches and rollback behavior pass real PostgreSQL assertions.

**Verification:** focused Task 2 creation/rollback cases and existing webhook/rerun suites; keep unrelated transient failures separate from assertions.

**Dependencies:** Task 3.

**Files likely touched:** `app/modules/reviews/application/try_enqueue_webhook_run.py`, `app/modules/reviews/infrastructure/webhook_runs.py`, `app/modules/reviews/infrastructure/rerun_store.py`; Task 2 regression file. Update the two explicit candidate constructors in `tests/test_github_run_trigger.py` and `tests/test_webhook_full_path.py` as a small fixture-only follow-up before the full suite.

**Estimated scope:** Medium (4 implementation/test files, plus a separate 2-file fixture follow-up).

## Task 5: Snapshot propagation

**Description:** Carry stored Run publication settings through the immutable policy snapshot.

**Acceptance criteria:**

- [x] Frozen application contracts use optional literal threshold/cap fields;
      store get_pins, snapshot insert/load and builder preserve their exact values.
- [x] First snapshot prepared after Repository changes still carries Run pins;
      historical NULL snapshots remain NULL on repeat reads/preparation.
- [x] Canonical digest payload, schema_version and cache-key builder are unchanged.

**Verification:** Task 1 tests, `tests/test_policy_snapshot_postgres.py`, Task 2 snapshot cases, and relevant origin snapshot tests.

**Dependencies:** Task 3; sequential with Task 4 due shared model/regressions.

**Files likely touched:** `app/modules/reviews/application/policy_snapshot.py`, `app/modules/reviews/application/prepare_policy_snapshot.py`, `app/modules/reviews/infrastructure/policy_snapshot_unit_of_work.py`, `tests/test_policy_snapshot_postgres.py`, Task 2 regression file.

**Estimated scope:** Medium (5 files).

## Task 6: Postprocessing consumes Run pins

**Description:** Apply pinned settings in the currently running legacy pipeline.

**Acceptance criteria:**

- [x] New runs use Run values before a snapshot exists and after Repository settings
      mutate; actual processed output demonstrates threshold and cap independently.
- [x] Retry preserves the same pinned result; only fully unpinned historical runs
      use the live Repository pair on each processing attempt.
- [x] Existing rule names, verifier guards, drop counts and inline disposition stay
      unchanged; the fake SQL result shape remains accurate.

**Verification:** Task 2 end-to-end store/postprocessor scenarios, `tests/test_sqlalchemy_run_repository.py`, existing severity/output suites; final integration run must execute the DB regressions without skips.

**Dependencies:** Tasks 4 and 5.

**Files likely touched:** `app/modules/reviews/infrastructure/run_repository.py`, `tests/test_sqlalchemy_run_repository.py`, Task 2 regression file.

**Estimated scope:** Medium (3 files).

## Checkpoint: Acceptance behavior

- [x] Create → mutate → postprocess → retry holds original settings.
- [x] Rerun uses current settings; historical runs keep documented live fallback.
- [x] Snapshot round trip preserves pins; digest/cache identity is unchanged.

## Task 7: Specification alignment

**Description:** Describe the implemented legacy behavior and unchanged target work.

**Acceptance criteria:**

- [x] PS §9 and severity decision table state immediate legacy pin adoption and
      pre-migration NULL fallback, retry versus rerun, and unchanged legacy count.
- [x] CVS invariant 1 and RFS §7.1 describe actual Run/snapshot storage rather than
      marking the fields wholly future; digest/cache formulas remain intact.
- [x] Future verifier/final-result/result_digest scope is not claimed implemented.

**Verification:** review documentation against Task 2 asserted histories; `git diff --check`.

**Dependencies:** Task 6.

**Files likely touched:** `docs/PIPELINE_SPEC.md`, `docs/CONTEXT_AND_VERIFICATION_SPEC.md`, `docs/RULES_FORMAT_SPEC.md`.

**Estimated scope:** Medium (3 files).

## Task 8: Final gates and review handoff

**Description:** Produce a concrete verified change ready for repository review.

**Acceptance criteria:**

- [x] All baseline gates pass: `uv run ruff check .`, `uv run ruff format --check .`,
      `uv run mypy .`, `uv run pytest`.
- [x] Focused real PostgreSQL and relevant/full integration checks run with services;
      report counts and skips accurately, and fix any change-related failures.
- [x] Independent standards/spec review findings are addressed; PR body records
      immediate legacy migration, no-backfill fallback, testing, and `Refs #212`.

**Verification:** record exact commands/results and checks current-main
revision compatibility before requesting review. Keep issue closure with tech lead;
no closing keyword or Development-panel link. No dependency additions are planned.

**Dependencies:** Task 7.

**Files likely touched:** only files requiring fixes to concrete gate/review findings;
update this checklist to record completion, without claiming unavailable checks.

**Estimated scope:** Small verification/handoff; split any discovered implementation work.

## Checkpoint: Complete

- [x] Every issue AC has a test or explicit documentation change.
- [x] Coordinated open-PR overlap comments precede implementation edits.
- [x] Required gates and available integration checks are green.
- [ ] GitHub approving review on the current head and resolution of review threads
      by their authors are required before merge. The issue remains open for tech-lead acceptance.


## Verification evidence (2026-10-24)

Completed test-first vertical slices cover snapshot contract/digest/cache,
nullable migration, webhook and rerun creation, snapshot persistence, and legacy
postprocessing. All issue acceptance criteria have regression coverage or an
explicit specification change.

Required gates passed on the final implementation:

- `uv run ruff check .`
- `uv run ruff format --check .` (545 files)
- `uv run mypy .` (542 source files)
- `uv run pytest` with isolated PostgreSQL
  and RabbitMQ: **5679 passed, 0 failed, 0 skipped**, including **993 integration**
  tests; 2 existing deprecation warnings; 1025.85 seconds.
- `git diff --check`

Dedicated PostgreSQL publication/migration regressions: **8 passed, 0 skipped**.
Independent Standards and Spec reviews reported **0 findings**, including
final migration-graph and historical-origin regression updates. These internal
reviews do not replace the required GitHub approving review on the current head.

No dependencies were added. The draft PR records
immediate legacy adoption, unchanged policy/cache identity and historical NULL
live fallback without backfill; merge and issue acceptance remain pending.
