# Task list: RAG toggle, index status and spend accounting (#145)

See [145-plan.md](145-plan.md) for the design. That plan is the source of truth; this list
adds no scope. AC1–AC7 are the numbered issue acceptance criteria in the plan's table.
Baseline `origin/main` `7546e53`; branch `feat/145-rag-toggle-status`. Each task below is
one planned commit, except Task 9 (review and publication). C3 is dropped (D-E), so there
is no task for it.

Dependency order: Task 1 → 2 → 3 → 4 → 5 → 6 (ui repository) → 7 → 8 → 9. U1 (Task 6)
must land before C5 (Task 7), because C5 pins `UI_CONTRACT_COMMIT` to the U1 SHA.

**Common verification.** Every task that adds an api commit runs these before it is
marked done:

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy .
TEST_DATABASE_URL=postgresql+psycopg://app:app@127.0.0.1:5432/dmc_145 uv run pytest
TEST_DATABASE_URL=postgresql+psycopg://app:app@127.0.0.1:5432/dmc_145 \
  uv run pytest -m integration -rs tests/test_repository_rag_postgres.py \
  tests/test_code_index_store_postgres.py tests/test_repositories_api_postgres.py \
  tests/test_app_reinstall_schema_postgres.py tests/test_app_reinstall_postgres.py \
  tests/test_initial_migration.py tests/test_llm_gateway_postgres.py
```

- The focused integration run must report **zero skips**. A skipped PostgreSQL acceptance
  test never counts as passing. `tests/test_repository_rag_postgres.py` exists from Task 3
  on; leave it out of the focused run before that.
- The tests without a database (`test_alembic_heads.py`, `test_openapi_contract.py`,
  `test_ui_zod_contracts.py`, `test_rag_index_status.py`, `test_context_provenance.py`,
  `test_review_context_stage.py`, `test_llm_gateway.py`) run in the plain pytest gate.
- Use only the dedicated database `dmc_145`, never another session's database or broker.
  Do not print credentials.

## Task 1: Plan documents (C1)

**Description:** Write the English plan and task list from the approved plan as commit 1:
`docs(plans): plan the RAG toggle, index status and spend accounting (#145)`.

**Acceptance criteria:**

- [x] `docs/plans/145-plan.md` and `docs/plans/145-todo.md` translate the approved plan.
  They keep every file:line reference, the user decisions D-A..D-E of 2026-10-16, the
  commit order and the dropped C3 with its reason.
- [x] No new decision, column, method or scope is added.

**Verification:** `uv run ruff check .` and `uv run ruff format --check .` are
unaffected; only the two files are committed.

**Dependencies:** None. **Files touched:** `docs/plans/145-plan.md`,
`docs/plans/145-todo.md`. **Estimated scope:** Small.

## Task 2: Derived RAG index status (C2)

**Description:** `feat(code_index): derive the RAG index status readers see (#145)`.
Test-first: write `tests/test_rag_index_status.py` and watch it fail, then add
`RagIndexStatus` to `app/modules/code_index/domain/enums.py` and the frozen `RagIndex` plus
`rag_index(...)` in `app/modules/code_index/domain/rag_index.py`, as in plan step 2.

**Acceptance criteria:**

- [x] `RagIndexStatus` members, in this order: disabled, empty, building, ready, stale,
  failed (D-B). The order is the contract order for OpenAPI and Zod.
- [x] The literal table covers: flag off → `disabled` with null/0 fields; no state row →
  `empty`; READY with target ≠ indexed → `stale`; READY without a target or with
  target = indexed → `ready`; EMPTY/BUILDING/FAILED map 1:1; `last_error_code` comes from
  `error_code` when failed, otherwise from `last_error_code`; the publication fields are
  filled only for ready/stale.
- [x] AC4: disabled → not usable and no reason; empty/building/failed → not usable with a
  reason; ready/stale → usable.
- [x] The unit test fails before the code exists and passes after.

**Verification:** Run the common verification block.

**Dependencies:** Task 1.

**Files likely touched:**

- `app/modules/code_index/domain/enums.py`;
- `app/modules/code_index/domain/rag_index.py`;
- `tests/test_rag_index_status.py`.

**Estimated scope:** Small.

## Task 3: The RAG toggle column, off by default (C4)

**Description:** `feat(repositories): store the RAG toggle, off by default (#145)`.
Test-first: write `tests/test_repository_rag_postgres.py` and the extra reinstall assert,
watch them fail, then add the migration
`alembic/versions/20261016_0033_repository_rag_enabled.py`
(`down_revision="20261015_0032"`) and the ORM column next to `installation_available`;
update both heads pins.

**Acceptance criteria:**

- [x] `rag_enabled BOOLEAN NOT NULL server_default false`; downgrade drops it;
  `tests/test_initial_migration.py` stays green (metadata matches the migration).
- [x] AC1: a repository inserted at 0032 reads `rag_enabled = false` after the upgrade to
  0033.
- [x] AC1/AC7: a new repository onboarded through `SyncInstallationRepositories` has
  `rag_enabled = false`; a repeated onboarding of a repository with `rag_enabled = true`
  keeps true.
- [x] Reinstall keeps `rag_enabled` (+1 assert in `tests/test_app_reinstall_postgres.py`).
- [x] The heads pins name `20261016_0033` in
  `tests/test_app_reinstall_schema_postgres.py` and `tests/test_code_index_store_postgres.py`.

**Verification:** Run the common verification block with zero skips; `uv run alembic
heads` shows one head, `20261016_0033`.

**Dependencies:** Task 1.

**Files likely touched:**

- `alembic/versions/20261016_0033_repository_rag_enabled.py`;
- `app/modules/repositories/infrastructure/models.py`;
- `tests/test_repository_rag_postgres.py`;
- `tests/test_app_reinstall_postgres.py`;
- `tests/test_app_reinstall_schema_postgres.py`;
- `tests/test_code_index_store_postgres.py`.

**Estimated scope:** Small.

## Task 4: Retrieved excerpt role and context tokens by role (C6)

**Description:** `feat(reviews): add the retrieved excerpt role and context tokens by role (#145)`.
Add `SourceRole.RETRIEVED_EXCERPT` and `tokens_by_role` in `context_summary`, as in plan
step 7.

**Acceptance criteria:**

- [x] `tests/test_context_provenance.py` pins the role list with `retrieved_excerpt`.
- [x] `tests/test_review_context_stage.py` asserts literal `tokens_by_role` for a payload
  with a `retrieved_excerpt` block.
- [x] The tests fail before the code exists and pass after.

**Verification:** Run the common verification block.

**Dependencies:** Task 1.

**Files likely touched:**

- `app/modules/reviews/application/context_provenance.py`;
- `app/modules/reviews/application/review_context_stage.py`;
- `tests/test_context_provenance.py`;
- `tests/test_review_context_stage.py`.

**Estimated scope:** Small.

## Task 5: Gateway pin for every prompt block, and the run of every usage event (C7)

**Description:** `test(reviews): pin that every prompt block is estimated and charged to its run (#145)`.
Tests only, as in plan step 8, plus the test moved from C3 (D-E).

**Acceptance criteria:**

- [x] AC5: for a block in the system message, in the user message and in an extra
  message, 300 + 600 + 1200 characters give `input_tokens_estimate` = 700.
- [x] AC5: an answer without usage → exactly one ledger event with `tokens_in == 700` on
  this run_id and workspace_id.
- [x] AC5: spending up to the run limit minus the next call's cost passes; 0.000001 more
  → `BUDGET_EXCEEDED` with no request.
- [x] AC6 (moved from C3): `usage_events.run_id` is NOT NULL with an FK to `runs`.

**Verification:** Run the common verification block. The tests pin existing behaviour, so
there is no failing run before them.

**Dependencies:** Task 1.

**Files likely touched:** `tests/test_llm_gateway.py`; the PostgreSQL inspection needs a
database, so it lives in `tests/test_llm_gateway_postgres.py`.

**Estimated scope:** Small.

## Task 6: Zod schemas in the ui repository (U1)

**Description:** In `projects/dmc-268-ui-t6-102` (branch `feat/102-rag-toggle` from
`e8d47b5`): `feat(repository): add the RAG toggle and index status to zod schemas (#102)`,
footer `Refs larchanka-training/dmc-268-api-t6#145`, as in plan step 5.

**Acceptance criteria:**

- [ ] `RagIndexStatusSchema` in the same order as `RagIndexStatus`; `RagIndexSchema`;
  `RepositorySchema` += `ragEnabled`, `ragIndex`; `UpdateRepositorySchema` += optional
  `ragEnabled`.
- [ ] `schemas.test.ts` cases; mocks in `app-state.ts` and `mockTransport.ts`; test
  literals use `disabledRagIndex`.
- [ ] `pnpm lint`, `pnpm format:check`, `pnpm check-types`, `pnpm test` pass.
- [ ] Pushed; draft PR ui#102 with base `feat/api-149-repo-review-settings`.

**Dependencies:** Task 2 (the status order).

## Task 7: The settings contract (C5)

**Description:** `feat(repositories): expose the RAG toggle and index status in repository settings (#145)`,
the whole contract in one commit, as in plan step 6.

**Acceptance criteria:**

- [x] AC2: GET returns `ragEnabled` and `ragIndex`; PATCH changes `ragEnabled`; another
  tenant's repository → 404 with the row intact; OpenAPI is updated.
- [x] AC1: default false in GET and in the list.
- [x] AC3: the status matrix from SQL rows of `code_index_states` gives literal
  `ragIndex` values; an unavailable repository → false + `disabled` with the stored value
  kept.
- [x] The contract tests reject `{"ragEnabled": null}` and `{"ragIndex": {...}}` with 422.
- [x] The Zod fixture is regenerated and `UI_CONTRACT_COMMIT` = the U1 SHA.

**Verification:** Run the common verification block with zero skips.

**Dependencies:** Tasks 2, 3 and 6.

## Task 8: Coordination comments, then documentation (C8)

**Description:** Before the docs commit, the coordinator comments in each open PR that
touches `docs/SYSTEM_DESIGN.md` (planned: #124, #148, #155). Then
`docs: describe the RAG toggle, index status and spend accounting (#145)`, as in plan
step 9.

**Acceptance criteria:**

- [ ] The coordination comments are posted before the docs commit.
- [ ] SD, PIPELINE_SPEC, TEST_PLAN §3.5, BACKEND_ARCHITECTURE and the ERD match the code
  of Tasks 2–7.

**Dependencies:** Task 7.

## Task 9: Review, verify and publish

**Description:** A clean `code-reviewer` reviews the full branch diff with only
`git show`/`git diff` and no checkout; findings go back to the development subagent. Then
`/verify-run`, a rebase on `main`, and the PR.

**Acceptance criteria:**

- [ ] All four gates pass and the focused PG run has zero skips.
- [ ] The fresh review reports zero findings on both axes.
- [ ] The PR body is What / Why / How to verify / `Refs #145`, with no closing keyword,
  and states the items of the plan's "What the PR states"; reviewer ndovnar; #145 → On
  Review.

**Dependencies:** Tasks 1–8.

## External: before merge and issue acceptance

- [ ] Required CI passes on the latest PR head.
- [ ] The PR is rebased on `main` before merge, and `uv run alembic heads` still shows
  one head (re-chain C4 if another `_0033` merged first).
- [ ] One approving review on the current head; all review threads resolved.
- [ ] The tech lead checks AC1–AC7 and the DoD, then closes #145. The result and status
  are reflected in #139 and on board 12.
