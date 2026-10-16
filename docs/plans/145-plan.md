# Implementation plan: RAG toggle, index status and spend accounting (#145)

## Status and baseline

Approved by the user on 2026-10-16. This document is the English task brief for the
executor. It translates the approved plan and adds no new decisions, columns, methods or
scope. Where it says "match existing", the implementer reads the named precedent in the
code and copies it.
Issue: https://github.com/larchanka-training/dmc-268-api-t6/issues/145, epic #139.
Baseline: `origin/main` `7546e539e1c1f565f65b9a5ca4cdc5dd1fbfb3fa`. The single Alembic
head is `20261015_0032`. Worktree `projects/dmc-268-api-t6-145`, branch
`feat/145-rag-toggle-status`. Task checklist: [145-todo.md](145-todo.md).

## Context

This issue is part 6 of epic #139 (Graph RAG v1). ADR #140
(`docs/adr/0001-graph-rag-v1.md`, accepted) and the data model of #141 (PR #158 →
`app/modules/code_index`) are on `main` `7546e53`, so the blockers are lifted. #145 blocks
ui#102 (the API contract) and #143 (the reaction to a flag change).

Goal: a `rag_enabled` flag on the repository (default false), the index status in the
repository settings GET/PATCH, RAG spend accounting, and the record of an indexing job's
outcome — the full scope of AC1–AC7.

## Issue acceptance criteria (numbered for cross-reference)

| AC | Requirement (issue #145) | Covered by |
| --- | --- | --- |
| AC1 | The RAG flag is false by default for new and existing repositories; the `installation.created` and `installation_repositories.added` projections do not turn it on | C4 (column, onboarding), C5 (GET default) |
| AC2 | The repository settings GET returns the flag and the index status, PATCH changes the flag; access follows the rules of the other settings; OpenAPI is updated | C5 |
| AC3 | The index status in the response: a value from the ADR list, `indexed_commit_sha`, update time, file and symbol counts, last error code; no code text | C2 (derivation), C5 (API and contract); deviation D-B |
| AC4 | After disabling, the next Run does not touch the index; after enabling, a Run goes without RAG until the index is ready | C2 (`usable` / `not_ready_reason`); see "What the PR states" |
| AC5 | RAG block tokens enter the pre-call cost estimate and the `usage_events` of their own run; the Workspace daily budget and the run limit work as before; the RAG share is visible by source role in the manifest | C6, C7; see "What the PR states" |
| AC6 | Every indexing job records per repository the number of GitHub requests, files and symbols, the duration and the outcome; `usage_events` gets no rows with a substitute `run_id` | #142 (D-E); in #145 only the `usage_events.run_id` test (C7); counters per D-D |
| AC7 | Tests: default false, onboarding does not turn the flag on, PATCH and access rights, the status contract | C2, C4, C5 |

DoD (gates, docs, PR with Refs and no auto-close, approve on the current head, threads
resolved, tech-lead acceptance) is covered by C8 and the publication steps.

## User decisions of 2026-10-16

- **D-A** — the full scope of AC5–AC6 stays in #145; nothing moves to #144 or #142.
- **D-B** — `status ∈ {disabled, empty, building, ready, stale, failed}`. `disabled` goes
  beyond the ADR list (a deviation from the letter of AC3, stated in the PR and the
  issue). The "empty repository" of ui#102 is `ready` with `fileCount = 0` (the ui#102
  wording is to be corrected).
- **D-C** — the Zod commit in the ui repository is made in this session (a branch on top
  of the ui#106 head), the fixture is regenerated, and `UI_CONTRACT_COMMIT` gets a new
  value.
- **D-D** — the outcome of a failed job uses no new columns: status plus
  `error_code`/`last_error_code`; `publish` clears `last_error_code`; the file and symbol
  counts are the counters of the visible publication.
- **D-E** — C3 is dropped: the parallel #142 writes the job outcome (`mark_failed` /
  `release`). AC6 is closed through #142; #145 keeps only a test on
  `usage_events.run_id`.

## Key facts (`origin/main` `7546e53`)

- Settings are flat columns of `repositories`
  (`app/modules/repositories/infrastructure/models.py:71-117`). Read model, change and use
  cases: `application/repository_settings.py:13-91`. Adapter
  `infrastructure/repository_settings.py`: `_to_settings` :21-36; GET
  `enabled = enabled AND installation_available` :27; `update_repository` :69-93
  (`UPDATE…RETURNING`). DTO `api/dtos.py:14-58`. Routes `app/main.py:829/844/864-893`.
  A repository outside the scope → 404.
- Onboarding does not force `enabled` (the issue's "mine" is inaccurate):
  `upsert_repository` inserts 5 columns. Reinstall #132 moves the row, so a column with
  `server_default false` stays false.
- Precedent: #149 (`a07c10c`, `f27d4c2`):
  - migration 0031;
  - hand-written `contracts/openapi.yaml`;
  - `tests/test_openapi_contract.py` (fake `REPOSITORY` :259, store :276-310);
  - the Zod mirror :715-773 (recursive over properties/required/enum lists; a union of
    several non-null types is not compared);
  - fixture `tests/fixtures/ui_zod_contracts.json`, pin `tests/test_ui_zod_contracts.py:27`.
- Alembic head `20261015_0032`. Heads pins: `tests/test_app_reinstall_schema_postgres.py:65-73`,
  `tests/test_code_index_store_postgres.py:564`.
- `code_index`:
  - `IndexStatus` (`domain/enums.py:8-12`);
  - `CodeIndexState` (`infrastructure/models.py:35-97`) already carries the counts,
    `downloaded_bytes`, `build_duration_ms`, `github_request_count`, `error_code`,
    `last_error_code` and `target_commit_sha`;
  - `publish` (`infrastructure/index_store.py:529-549`) does not clear `last_error_code`;
  - there is no transition to FAILED and no record of a failure; `stale` is not computed.
- Accounting:
  - `_estimate` (`reviews/infrastructure/llm/gateway.py:311-313`) counts every message;
  - the run limit is checked at :342-380;
  - the daily budget is checked at claim (`handle_review_run.py:352-357`) over
    `usage_events`.
  - L1–L4 do not reach the model on `main` (`process_run.py:243`); the manifest exists
    only in the open PR #133.
  - `SourceRole` (`reviews/application/context_provenance.py:22-31`) has no
    `retrieved_excerpt`.
  - `context_summary` (`review_context_stage.py:128-181`) writes `tokens_by_level` into
    the `context.build` trace.

## Preparation

- Worktree `projects/dmc-268-api-t6-145`, branch `feat/145-rag-toggle-status` from
  `origin/main` `7546e53`. A dedicated database `dmc_145` on the local PG17
  (`postgresql+psycopg://app:app@127.0.0.1:5432/dmc_145`).
- UI worktree `projects/dmc-268-ui-t6-102`, branch `feat/102-rag-toggle` from `e8d47b5`
  (the ui#106 head), `pnpm install`. Not `-106`: the generator needs a clean tree.
- Implementation is done by a subagent from the brief file (`docs/plans/145-plan.md` plus
  the todo); the main context coordinates and verifies.

## Commits

Gates run on every commit. The operative order is **C1, C2, C4, C6, C7, U1 (ui
repository), C5, C8**: U1 must land before C5, and C3 is dropped. The numbering below keeps
the approved plan's labels.

1. **C1** `docs(plans): plan the RAG toggle, index status and spend accounting (#145)` —
   `docs/plans/145-plan.md` and `145-todo.md` (modelled on 141).
2. **C2** `feat(code_index): derive the RAG index status readers see (#145)` —
   `app/modules/code_index/domain/rag_index.py`:
   - `RagIndexStatus` (a StrEnum in `domain/enums.py`);
   - a frozen `RagIndex` (status, indexed_commit_sha, indexed_at, file_count,
     symbol_count, last_error_code) with `usable` (ready/stale) and `not_ready_reason`
     (empty/building/failed);
   - `rag_index(*, rag_enabled, status: IndexStatus | None, indexed_commit_sha,
     target_commit_sha, published_at, file_count, symbol_count, error_code,
     last_error_code)` over primitives.

   Rules:
   - flag off → `disabled`, null/0;
   - no state row → `empty`;
   - READY with a target set and target ≠ indexed → `stale`, otherwise `ready`;
   - the other statuses map 1:1;
   - `last_error_code` = `error_code` when failed, otherwise `last_error_code`;
   - the publication fields are filled only for ready/stale.

   Test `tests/test_rag_index_status.py`: a table of literals, including AC4 (disabled →
   no access; empty/building/failed → not ready with a reason; ready/stale → usable).
3. ~~C3~~ **dropped (decision of 2026-10-16, D-E).** The parallel branch #142
   (`feat/142-rag-indexer-build`, worktree `-142`, not pushed at planning time) already
   carries:
   - `mark_failed` (failed + `FAILED_ERROR_CODES` + stats);
   - `release` (a soft refusal, `last_error_code`);
   - `publish` that clears `last_error_code`.

   AC6 is closed by #142. A comment in #142: `release` must write the stats (GitHub
   requests, duration, bytes). Migration 0033 is taken there too; whoever merges second
   re-chains. The test "usage_events without a substitute run_id" (inspection of
   `usage_events.run_id` NOT NULL + FK to `runs`) moves to C7.
4. **C4** `feat(repositories): store the RAG toggle, off by default (#145)`:
   - `alembic/versions/20261016_0033_repository_rag_enabled.py`
     (`down_revision="20261015_0032"`, `rag_enabled BOOLEAN NOT NULL server_default
     false`, downgrade drops it);
   - the ORM column next to `installation_available`;
   - the heads pins (both files).

   New `tests/test_repository_rag_postgres.py`:
   - upgrade 0032 → insert → 0033 → false (the `_upgrade` pattern,
     `test_app_reinstall_schema_postgres.py:41`);
   - a new repository through `SyncInstallationRepositories` → false;
   - a repeated onboarding keeps true;
   - +1 assert in `tests/test_app_reinstall_postgres.py:379` (reinstall keeps
     `rag_enabled`).
5. **U1** (ui) `feat(repository): add the RAG toggle and index status to zod schemas (#102)`,
   footer `Refs larchanka-training/dmc-268-api-t6#145` —
   `src/entities/repository/model/schemas.ts`:
   - `RagIndexStatusSchema` (the same order), `RagIndexSchema`;
   - `RepositorySchema` += `ragEnabled`, `ragIndex`;
   - `UpdateRepositorySchema` += optional `ragEnabled`;
   - cases in `schemas.test.ts`;
   - mocks `src/app/mocks/app-state.ts` (different statuses) and `mockTransport.ts`
     (`ragEnabled` masked by `installationAvailable`; PATCH false → disabled, true →
     empty);
   - about 49 `Repository` literals in tests go through a shared constant
     `disabledRagIndex`.

   Push, draft PR ui#102 with base `feat/api-149-repo-review-settings`.
6. **C5** `feat(repositories): expose the RAG toggle and index status in repository settings (#145)`
   — the whole contract in one commit:
   - read model += `rag_enabled`, `rag_index: RagIndex`; change += `rag_enabled: bool | None`;
   - adapter:
     - a shared select of `Repository` plus 9 columns of `CodeIndexState` through
       `outerjoin` (no N+1, no new port — the infra-join precedent; the port appears in
       #143 as `CodeIndexPurger`);
     - `_to_settings` calls `rag_index(...)`;
     - GET `ragEnabled = rag_enabled AND installation_available` (like `enabled`), and
       the status comes from the same effective value;
     - PATCH writes the raw value, then re-reads through the shared select;
   - DTO `RagIndexDto`; `RepositoryDto` += `rag_enabled`, `rag_index`;
     `RepositoryUpdateDto` += `rag_enabled`; the mapping in `app/main.py:881-889`;
   - `contracts/openapi.yaml`:
     - enum `RagIndexStatus` [disabled, empty, building, ready, stale, failed];
     - object `RagIndex`: all 6 required, `additionalProperties: false`, nullable as
       `[string, "null"]`, counts `integer minimum 0`, description "publication fields
       only for ready/stale";
     - `Repository` += required `ragEnabled`, `ragIndex` (readOnly);
     - `RepositoryUpdate` += `ragEnabled`;
   - `tests/test_openapi_contract.py`: fakes, the PATCH round trip,
     `INVALID_REVIEW_SETTINGS` += `{"ragEnabled": null}`, `{"ragIndex": {...}}` → 422;
   - fixture: `DMC_268_UI_DIR=…/dmc-268-ui-t6-102 node tests/generate_ui_zod_contracts.mjs`,
     `UI_CONTRACT_COMMIT` = the U1 SHA (the diff touches only
     repository/repositoryUpdate/provenance);
   - PG tests in `tests/test_repository_rag_postgres.py`:
     - default false in GET and in the list;
     - PATCH true/false reaches the database;
     - another tenant's repository → 404, the row is intact;
     - an unavailable repository → false + `disabled`, the stored value kept;
     - a status matrix from SQL rows of `code_index_states`, as literals: no row,
       building, ready without a target, ready with target ≠ indexed → stale, failed
       `limit_exceeded:symbols`, ready with a soft code, flag false while ready →
       disabled;
     - the list gives the same.
7. **C6** `feat(reviews): add the retrieved excerpt role and context tokens by role (#145)`:
   - `SourceRole.RETRIEVED_EXCERPT` + the pin in `tests/test_context_provenance.py:163-174`;
   - `tokens_by_role` in `context_summary` + a test in
     `tests/test_review_context_stage.py` with a `retrieved_excerpt` block (literals per
     role).
8. **C7** `test(reviews): pin that every prompt block is estimated and charged to its run (#145)`
   — `tests/test_llm_gateway.py`:
   - parametrized by the block position (system / user / an extra message);
   - literals from `chars_per_token = 3.0` (300 + 600 + 1200 characters → 700):
     `input_tokens_estimate` = 700;
   - an answer without usage → 1 ledger event with `tokens_in == 700` on this
     run_id/workspace_id;
   - spending up to the run limit minus the cost of the next call passes; +0.000001 →
     `BUDGET_EXCEEDED` with no request.

   Plus the test moved from C3: `usage_events.run_id` is NOT NULL with an FK to `runs`.
9. **C8** `docs: describe the RAG toggle, index status and spend accounting (#145)`:
   - SD §6.9 (:598-613), §11 (`Repository` :1286, `code_index_states` :1303,
     :1318-1340), §12 (:1366), §13 «Стоимость» (:1413);
   - PIPELINE_SPEC :131 (`tokens_by_role`) and §4.5 (the estimate covers any prompt
     block);
   - TEST_PLAN §3.5 (:336-356);
   - BACKEND_ARCHITECTURE ~:417-482;
   - `docs/erd/erd.mmd` + re-render `erd.svg`.

   Before the SD edit: comments in #124, #148 and #155.

## What the PR states (body: What / Why / How to verify / Refs, no closing keyword)

- AC3: `disabled` goes beyond the ADR list (D-B); "empty repository" = ready + fileCount 0.
- AC4: on `main` nobody reads the index, so it holds trivially; #145 gives `usable` /
  `not_ready_reason` for #144.
- AC5: the `retrieved_excerpt` role, `tokens_by_role` in the trace, the gateway invariant.
  "The share in the manifest" waits for the manifest (#120 / PR #133; `ManifestBlock` has
  no tokens) and for the block producer (#144).
- AC6: closed by #142 (`mark_failed`/`release`, D-E); #145 has the NOT NULL + FK test on
  `usage_events.run_id`; the counters are read per D-D.
- `stale` does not occur on `main` until #143 (nothing writes the target); enabling
  creates no state row → `empty` until #143.
- Hand-offs:
  - #142 — `release` writes the stats;
  - #143 — read the raw `rag_enabled` `FOR UPDATE` and call purge(`disabled`)/enable
    through `CodeIndexPurger` before the use case commits;
  - #144 — `usable`/`not_ready_reason`, and render the blocks into the messages
    (otherwise they miss the estimate).

## Coordination

- Comments:
  - #124/#148/#155 (SD) before C8;
  - #133 after C6 (its second `SourceRole` has no `retrieved_excerpt`);
  - ui#106 (both edit `schemas.ts`);
  - ui#102 (`empty` with `ragEnabled: true` is normal until #143; the "empty repository"
    wording).
- Issue #145: record the D-B deviation and the D-D reading (marked as the tech lead's).
- Risks: a rebase/squash of ui#106/ui#102 orphans the pin → regenerate; a foreign `_0033`
  → re-chain C4.

## Verification

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy .
TEST_DATABASE_URL=postgresql+psycopg://app:app@127.0.0.1:5432/dmc_145 uv run pytest
```

- Focused, zero skips: `-m integration -rs` over `tests/test_repository_rag_postgres.py`,
  `test_code_index_store_postgres.py`, `test_repositories_api_postgres.py`,
  `test_app_reinstall_schema_postgres.py`, `test_app_reinstall_postgres.py`,
  `test_initial_migration.py`.
- Without a database: `test_alembic_heads.py`, `test_openapi_contract.py`,
  `test_ui_zod_contracts.py`, `test_rag_index_status.py`, `test_context_provenance.py`,
  `test_review_context_stage.py`, `test_llm_gateway.py`.
- UI (`-102`): `pnpm lint`, `pnpm format:check`, `pnpm check-types`, `pnpm test`.
- Review: a clean `code-reviewer` subagent (only `git show`/`git diff`, no checkout); then
  check the clones' branches; check review fixes against `.agents/rules/backend.md` before
  pushing.
- Manual check: `uv run alembic upgrade head` on `dmc_145`, the dev server, GET/PATCH
  `/api/repos/{id}` with a token — `ragEnabled`, and `ragIndex.status` = `empty` after
  enabling.

## Publication

- Draft PR → CI green → Ready, reviewer ndovnar, board 12: #145 On Review; ui#102 stays
  draft until the screen exists.
- `/verify-run`, report in `docs/reports/145/`.
- After the merge: the tech lead's AC check, the status in #139, drop the `dmc_145`
  database and the worktree.
