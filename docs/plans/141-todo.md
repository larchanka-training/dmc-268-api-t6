# Task list: graph index data model and repository isolation (#141)

See [141-plan.md](141-plan.md) for the design. That plan is the source of truth; this list
adds no scope. AC1–AC10 are the numbered issue acceptance criteria in the plan's table.
Baseline `origin/main` `7d3a16c`; branch `feat/141-rag-index-schema`. Each task below
is one planned commit, except Task 7 (review and publication).

Dependency order: Task 1 → 2 → 3 → 4 → 5 → 6 → 7. Schema changes are sequential and
nothing here runs in parallel.

**Common verification.** Every task that adds a commit runs these before it is marked done:

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run pytest
TEST_DATABASE_URL=postgresql+psycopg://app:app@127.0.0.1:5432/dmc_141 \
  uv run pytest -m integration -rs tests/test_code_index_store_postgres.py \
  tests/test_initial_migration.py tests/test_alembic_heads.py
```

- The focused integration run must report **zero skips**. A skipped PostgreSQL acceptance
  test never counts as passing.
- `tests/test_alembic_heads.py` carries no `integration` marker, so `-m integration`
  deselects it. It runs in the plain `uv run pytest` gate.
- Use only the dedicated database `dmc_141`, never another session's database or broker.
  Do not print credentials.

## Task 1: Plan documents

**Description:** Write the English plan and task list from the approved plan as commit 1:
`docs(plans): plan the graph index data model (#141)`.

**Acceptance criteria:**

- [x] `docs/plans/141-plan.md` and `docs/plans/141-todo.md` translate the approved plan.
  They keep every file:line reference, the three user decisions of 2026-10-15 and the
  state invariants.
- [x] No new decision, column, method or scope is added.

**Verification:** `uv run ruff check .` and `uv run ruff format --check .` are
unaffected; only the two files are committed.

**Dependencies:** None. **Files touched:** `docs/plans/141-plan.md`,
`docs/plans/141-todo.md`. **Estimated scope:** Small.

## Task 2: Schema, migration `20261015_0032` and schema PG tests

**Description:** Add the five tables (`code_index_states`, `code_index_files`,
`code_index_symbols`, `code_index_edges`, `code_index_imports`) and their PG ENUM types,
as in the plan's Schema section. The order is test-first:

1. Write the schema assertions in `tests/test_code_index_store_postgres.py` and watch
   them fail.
2. Add the ORM models and the migration
   `alembic/versions/20261015_0032_code_index_schema.py`
   (`down_revision="20261014_0031"`).

The ORM models and `test_initial_migration.py` cannot build without some items that the
plan lists under Code and that the commit order places in "domain + port". These items
land in this task:

- the three StrEnums `code_index_status`, `code_index_purge_reason` and
  `code_index_edge_kind`, in `app/modules/code_index/domain/`;
- the `pg_enum` widening to `type[StrEnum]` (`app/common/infrastructure/db/columns.py:29`);
- `app/modules/code_index/infrastructure/models.py`;
- registration in `app/bootstrap/db_metadata.py`.

This is the only place where the todo moves plan content between commits, and the
dependency order forces it. `SymbolKey`, `assign_disambiguators` and the port stay in
Task 3.

Implementer instructions carried over from the plan:

- `path` columns use the type of the existing path columns.
- `blob_sha` matches `cached_file_blobs.blob_sha`.
- `created_at`/`updated_at` follow `repositories`.
- Server defaults (BIGINT 0, enum `'empty'`) are declared the same way in the ORM and the
  migration as in 0031 (`compare_server_default=True`).
- `SymbolKind` (`reviews/application/ast_symbols.py:51`) and `ImportResolution`
  (`reviews/application/ast_resolution.py:54`) are reused as `code_index_symbol_kind` and
  `code_index_import_resolution`, not duplicated (BA:151).

**Acceptance criteria:**

- [ ] AC1: `uv run alembic heads` shows exactly one head, `20261015_0032`. Upgrade →
  downgrade → upgrade passes on a clean database. `tests/test_initial_migration.py` stays
  green without edits; first check that it holds no hard-coded table or enum lists. It
  confirms that metadata matches the migration and that no ENUM types are left behind.
- [ ] AC2: the PG test asserts:
  - the exact column set of each of the five tables (literals);
  - `repository_id` NOT NULL with FK `confdeltype='c'` on every table;
  - no columns for code text, signatures or embeddings.
- [ ] AC3 (columns): `code_index_states` has every column in the plan, including:
  - the four versions;
  - `target_commit_sha`;
  - `downloaded_bytes`/`build_duration_ms`/`github_request_count`;
  - the CHECK constraints `ck_code_index_states_failed_error`,
    `ck_code_index_states_lease_pair` and `ck_code_index_states_purge_reason`.
- [ ] Each PG ENUM's `enum_range` equals its StrEnum members, including
  `SymbolKind`/`ImportResolution` from #130. Extractor drift fails CI.

**Verification:**

- Run the common verification block.
- `uv run alembic heads`.
- Upgrade → downgrade → upgrade on a clean database.

**Dependencies:** Task 1; plan approval.

**Files likely touched:**

- `app/modules/code_index/` (package, `domain/` enums, `infrastructure/models.py`);
- `app/common/infrastructure/db/columns.py`;
- `app/bootstrap/db_metadata.py`;
- `alembic/versions/20261015_0032_code_index_schema.py`;
- `tests/test_code_index_store_postgres.py`.

**Estimated scope:** Large. One migration commit cannot be split further.

## Task 3: Domain key and store port, with unit tests

**Description:** Add the pieces that need no database:

- `SymbolKey` and the pure function `assign_disambiguators(path, declarations)` in
  `app/modules/code_index/domain/`;
- `app/modules/code_index/application/index_store.py` with:
  - the VOs `IndexFence(token, generation, publication)`, `IndexedFile`,
    `IndexedSymbol`, `IndexedEdge`, `ImportRecord`, `IndexState`, `EdgeTarget`, and the
    `publish` arguments `IndexProfile` and `IndexBuildStats`;
  - `IndexLeaseLost(Exception)`;
  - `CodeIndexStore(Protocol)`, documented as "flushes, never commits";
  - `CodeIndexUnitOfWork(UnitOfWork, Protocol)`.

The template is `repositories/application/repository_settings.py:42-59`. The port
methods and their T1–T7 labels are listed in the plan; the first parameter of every
method is `repository_id`.

Test-first:

- `tests/test_code_index_symbol_key.py`: disambiguator values; reordering declarations
  does not change the key.
- `tests/test_code_index_store_contract.py`: the Protocol half. Every Protocol method's
  first parameter is `repository_id`. The adapter half needs the adapter, so it is added
  in Task 4.

**Acceptance criteria:**

- [ ] AC9 (unit): the key does not depend on revision or declaration order.
  - `disambiguator` is the 0-based ordinal among duplicates of
    (path, qualified_name, kind), and a unique symbol gets 0.
  - c1 [foo, bar] → c2 [bar, baz, foo] keeps the keys of foo and bar.
- [ ] AC4 (port): every `CodeIndexStore` method has `repository_id` as its first
  parameter. That includes `revoke_expired_lease` and `collect_garbage`. There is no
  leader enumeration or TTL seam; those belong to #143.
- [ ] The unit tests fail before the code exists and pass after.

**Verification:** Run the common verification block.

**Dependencies:** Task 2 (the StrEnums).

**Files likely touched:**

- `app/modules/code_index/domain/`;
- `app/modules/code_index/application/index_store.py`;
- `tests/test_code_index_symbol_key.py`;
- `tests/test_code_index_store_contract.py`.

**Estimated scope:** Medium.

## Task 4: SQLAlchemy adapter, UoW and behavioural PG tests (test-first)

**Description:** Write the behavioural PostgreSQL tests in
`tests/test_code_index_store_postgres.py` first and watch them fail.

- The fixture is modelled on `test_initial_migration.py:28-48`.
- It seeds, with raw SQL, two workspace/installation/repository sets.

Then implement `SqlAlchemyCodeIndexStore(session)` and `SqlAlchemyCodeIndexUnitOfWork` in
`app/modules/code_index/infrastructure/index_store.py`.

- `SqlAlchemyCodeIndexStore` takes no `scope`/`allow_unscoped`.
- The UoW is modelled on `repositories/infrastructure/repository_settings.py`.
- The adapter follows the plan's method list and state invariants:
  - `enable`/`purge` (T1'/T1), `claim_lease` (T2), `discard_abandoned` (T3),
    `replace_file` (T4), `publish` (T5), the T6 reads, and `revoke_expired_lease` plus
    `collect_garbage` (T7);
  - the store flushes and never commits.

Also extend `tests/test_code_index_store_contract.py` with the adapter half:

- every adapter method's first parameter is `repository_id`;
- the constructor has no `scope`/`allow_unscoped`.

**Acceptance criteria:**

- [ ] AC4 (adapter):
  - the contract test covers the adapter's methods and constructor;
  - replacing the same file twice in one attempt leaves only the second version visible;
  - without `uow.commit()` nothing persists (flush-only).
- [ ] AC3 and AC5:
  - full c1 with `a.py`, then an incremental c2 that rewrites `a.py` → only the c2 rows
    are visible;
  - the state after publish equals literals (`ready`, `indexed_commit_sha`,
    `published_at`, file/symbol/edge counts, the 4 versions, `downloaded_bytes`,
    `build_duration_ms`, `github_request_count`);
  - purge → no visible rows; `collect_garbage` run down to 0 → physically 0 rows of
    repository A; a repeated purge is a no-op (generation unchanged); repository B's
    rows are intact;
  - enable after enable keeps the generation and the `ready` index; disable after enable
    rejects the old fence.
- [ ] AC6: A and B have identical paths and names. Every read method for A returns only
  A's rows, checked against literal `blob_sha` values and rows.
- [ ] AC8 (F1):
  - claim → purge → `replace_file`/`publish` with the old fence raises `IndexLeaseLost`,
    leaves no rows and leaves the state unchanged;
  - the same holds for disable+enable and for an expired lease;
  - two sessions: purge waits for the batch's FOR SHARE (shown with `pg_blocking_pids`),
    and a write with the old fence is rejected afterwards.
- [ ] AC9 and AC10 (F4):
  - c1 [foo, bar] → c2 [bar, baz, foo] keeps the foo/bar keys, and the importer's edge
    resolves;
  - an incremental build that deletes `a.py` and adds `c.py` with the same `foo` leaves
    the edge b.py→(a.py, foo) without a target.

**Verification:**

- Run the common verification block with zero skips.
- Record the RED run (tests failing before the adapter exists) and the GREEN run.

**Dependencies:** Tasks 2 and 3.

**Files likely touched:**

- `app/modules/code_index/infrastructure/index_store.py`;
- `tests/test_code_index_store_postgres.py`;
- `tests/test_code_index_store_contract.py`.

**Estimated scope:** Medium. The test file is large.

## Checkpoint: Schema and store

- [ ] Gates are green; the focused PG run has zero skips; there is one Alembic head.
- [ ] AC1–AC6 and AC8–AC10 have passing tests that assert literal values.
- [ ] A clean `code-reviewer` reviews the diff so far with only `git show`/`git diff` and
  no checkout.

## Task 5: Coordination comments, then documentation

**Description:** Before the docs commit, the **coordinator** (not the development
subagent) does the ownership step:

- re-check the open PRs and their files;
- post a concise coordination comment in each open PR that touches
  `docs/SYSTEM_DESIGN.md`. Planned: #124, #148 and #155, which touch sections 985 /
  645-1101 / 1445, not §11.

The AGENTS rule is per file. Then update the documentation:

- **SD §11** (`docs/SYSTEM_DESIGN.md:1274-1311`):
  - add a `**#141:**` line with the 5 tables and their key fields;
  - add a storage paragraph: the index is repository-scoped, not Workspace-scoped; purge
    is explicit (logical, then GC within ≤1 h); the cascade is a safety net.
- **`docs/erd/erd.mmd`:**
  - add the 5 entities and their relations from `REPOSITORIES`;
  - regenerate `docs/erd/erd.svg` with `npx -y @mermaid-js/mermaid-cli@<pinned>`. If that
    fails, flag it in the PR (the svg already lags: it has no `run_policy_snapshots`).
- **BA:**
  - «PostgreSQL и владение моделями» (`:408-415`): add a `code_index` row with its
    models;
  - «Миграции и проверка» (`:443-445`): add a sentence on revision 0032 and the new PG
    test.
- **TEST_PLAN §3.5** (`:336-356`): fill it in only if the rows reference tests.

**Acceptance criteria:**

- [ ] The coordination comments are posted on every open PR that touches
  `docs/SYSTEM_DESIGN.md` before the docs commit. The comment links are recorded here.
- [ ] AC7: SD §11, `docs/erd/erd.mmd` and BACKEND_ARCHITECTURE describe the five tables
  and the owner module `code_index`. Each claim matches the code from Tasks 2–4.
- [ ] `erd.svg` opens and contains `CODE_INDEX_STATES`, or the regeneration failure is
  flagged for the PR body.

**Verification:**

- Run the common verification block.
- Compare the documented columns and keys with the migration.

**Dependencies:** Task 4.

**Files likely touched:**

- `docs/SYSTEM_DESIGN.md`;
- `docs/erd/erd.mmd`;
- `docs/erd/erd.svg`;
- `docs/BACKEND_ARCHITECTURE.md`;
- `docs/TEST_PLAN.md`, only if its rows reference tests.

**Estimated scope:** Medium.

## Task 6: Mark the ADR accepted

**Description:** Add a separate commit, `docs(adr): mark graph RAG ADR accepted (#140)`,
in this PR (user decision of 2026-10-15).

- Replace the header status in `docs/adr/0001-graph-rag-v1.md` («Предложено» plus «до
  утверждения реализация #141–#147 заблокирована») with «Принято, approve ndovnar в PR
  #157, 15.10».

**Acceptance criteria:**

- [ ] One commit changes only the ADR header status, with the exact subject above.
- [ ] No other ADR content changes.

**Verification:**

- Run the common verification block.
- `git show --stat HEAD` lists only `docs/adr/0001-graph-rag-v1.md`.

**Dependencies:** Task 5. **Files touched:** `docs/adr/0001-graph-rag-v1.md`.
**Estimated scope:** Extra small (XS).

## Task 7: Review, verify and publish

**Description:**

- A clean `code-reviewer` reviews the full branch diff with only `git show`/`git diff`
  and no checkout. It reports under `## Standards` and `## Spec`.
- Any findings go back to the development subagent; the review then runs again.
- Then run `/verify-run`.
- Rebase on `main` before requesting review.
- The publisher opens the PR.

**Acceptance criteria:**

- [ ] All four gates pass and the focused PG run has zero skips. The Checkpoint
  verification also holds: one head `20261015_0032`, and upgrade → downgrade → upgrade on
  a clean database.
- [ ] The fresh review reports zero findings on both axes.
- [ ] The PR is opened with:
  - title `feat(code_index): add graph index schema and fenced store (#141)`;
  - body What / Why / How to verify / `Refs #141`, with no closing keyword and no
    Development-panel link;
  - an explicit statement that `defines` is the symbol row, not an edge row;
  - a statement that there are no new dependencies;
  - any `erd.svg` flag;
  - reviewer ndovnar;
  - #141 moved to On Review.

**Verification:**

- Record the tested head SHA, the gate results, the zero-skip PG run, the review result
  and the PR URL.

**Dependencies:** Tasks 1–6. **Files touched:** this checklist.
**Estimated scope:** Small.

## External: before merge and issue acceptance

- [ ] Required CI passes on the latest PR head.
- [ ] The PR is rebased on `main` before merge, and `uv run alembic heads` still shows
  one head.
- [ ] One approving review on the current head; all review threads resolved.
- [ ] The PR is merged under repository rules.
- [ ] The tech lead checks AC1–AC10 and the DoD, then closes #141. The result and status
  are reflected in #139 and on board 12.
