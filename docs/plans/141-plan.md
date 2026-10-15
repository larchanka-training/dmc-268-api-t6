# Implementation plan: graph index data model and repository isolation (#141)

## Status and baseline

Approved by the user on 2026-10-15 under the agent-loop skill. This document is the
English task brief for the executor. It translates the approved plan and adds no new
decisions, columns, methods or scope. Where it says "match existing", the implementer
reads the named precedent in the code and copies it.
Issue: https://github.com/larchanka-training/dmc-268-api-t6/issues/141, epic #139.
Baseline: `origin/main` `7d3a16c5a9cbc329e71938c3b212bf6ca050001c`. The single Alembic
head is `20261014_0031`. Worktree `projects/dmc-268-api-t6-141`, branch
`feat/141-rag-index-schema`. Task checklist: [141-todo.md](141-todo.md).

## Context

This issue is part 2 of epic #139 (Graph RAG v1). ADR #140 (`docs/adr/0001-graph-rag-v1.md`,
PR #157 approved and merged 2026-10-15) is approved, so the blocker is lifted. The ADR
gives no DDL itself: «DDL — #141» (ADR:163, :581). #141 blocks #142 (indexer), #143
(lifecycle), #144 (retriever) and #145 (toggle/status).

Goal: index tables in PostgreSQL, a store port, and a SQLAlchemy adapter fenced per ADR
§6.2. Isolation is strictly by `repository_id`. PG tests cover the issue AC: cross-tenant
leak, stale token, purge, symbol identity and unresolved edge.

Facts verified on `origin/main`:

- #132/#136 are merged, so the "two heads" mine is gone. The single head is
  `20261014_0031`. The new revision is `20261015_0032_code_index_schema.py` with
  `down_revision="20261014_0031"`. The open PRs (#124/#133/#148/#155) carry no migrations.
- Removal from an installation sets `installation_available=False`, not `enabled` as the
  issue says. Event-driven purge belongs to #143 and is not touched here.
- `repositories` has no `workspace_id`; it reaches a Workspace through
  `provider_installations`. Scoping is an explicit `scope: AuthScope | None` plus
  `allow_unscoped`. There are no contextvars.
- No module has a `domain/` package yet. The BA tree (BA:241-244) prescribes
  `code_index/{domain,application,infrastructure}`.
- PostgreSQL 17 (CI, compose). There is no shared conftest. Each PG test has its own
  fixture, calls `pytest.skip` without `TEST_DATABASE_URL`, and uses a schema named
  `test_<x>_<uuid>`.
- `tests/test_initial_migration.py` already catches metadata↔migration drift and
  undropped ENUM types. It does so once the models are registered in
  `app/bootstrap/db_metadata.py`.
- The ADR header status still reads «Предложено» plus «до утверждения реализация
  #141–#147 заблокирована». Decision of 2026-10-15: a separate commit
  `docs(adr): mark graph RAG ADR accepted (#140)` in the #141 PR sets the status to
  «Принято, approve ndovnar в PR #157, 15.10».

## Issue acceptance criteria (numbered for cross-reference)

| AC | Requirement (issue #141) | Covered by |
| --- | --- | --- |
| AC1 | Migration creates the index tables per the ADR; upgrade and downgrade pass on a clean PostgreSQL; ORM metadata matches the migration (checked like existing migrations) | Task 2 |
| AC2 | Every index row carries `repository_id` NOT NULL with FK ON DELETE CASCADE; no code-text columns beyond what the ADR allows (signatures or nothing); no embeddings columns | Task 2 |
| AC3 | Index state stores a status from the ADR list, `indexed_commit_sha`, update time, file and symbol counts and the last error code | Task 2 (columns), Task 4 (publish literals) |
| AC4 | The store port has no method without `repository_id`; the adapter is not created with `allow_unscoped`; the adapter only `flush()`es, the use case commits | Task 3 (port), Task 4 (adapter) |
| AC5 | Atomic replacement of one file's rows and repository purge: after purge no rows of the repository remain, a repeated purge is a no-op, another repository's rows are untouched | Task 4 |
| AC6 | PG cross-tenant leak test: two Workspaces with the same paths and symbol names; no query for repository A returns rows of repository B | Task 4 |
| AC7 | SD §11, `docs/erd/erd.mmd` and BACKEND_ARCHITECTURE describe the new tables and the owner module | Task 5 |
| AC8 | A job with a stale fencing token (ADR #140), after purge or a toggle switch, writes no rows and does not publish; the write is rejected in the same transaction; PG test (review #139, F1) | Task 4 |
| AC9 | Index rows store no symbol identity bound to the revision or declaration order (`symbol_id` #136); identity per ADR #140 survives reordering and a commit change; test (review #139, F4) | Task 3 (unit), Task 4 (PG) |
| AC10 | Deleting the target file leaves no edge pointing at someone else's symbol; the edge becomes unresolved per ADR #140; test (review #139, F4) | Task 4 |

DoD (gates, docs, PR with Refs and no auto-close, approve on current head, threads
resolved, tech-lead acceptance) is covered by Task 7 and the external items in the todo.

## User decisions of 2026-10-15

1. **State columns:** the state row carries every field the ADR explicitly puts on it
   (versions, target, operational counters; see Schema).
2. **`defines` is not an edge:** `defines` (ADR §3) is stored as the symbol row itself
   (path → logical key), with no duplicate edge row. The PR states this explicitly.
3. **ADR status flip:** a separate `docs(adr): mark graph RAG ADR accepted (#140)` commit
   in this PR.

Earlier user decision (2026-10-14): the index keeps pointers only. There are no columns
for code text, signatures or embeddings.

## Scope

#141 delivers the whole **storage** side of ADR §6.2:

- logical purge (T1);
- toggle-on (T1');
- lease claim (T2, without the concurrency limit);
- cleanup of abandoned attempts (T3);
- batch file write (T4);
- publication CAS (T5);
- visible reads (T6);
- revocation of an expired lease, plus GC in portions (T7).

Out of scope:

- #142: the advisory-lock limit 2/1 per installation, the `index.repo` queue,
  extraction;
- #143: the leader-loop GC/TTL/HEAD tasks, `CodeIndexPurger` and calls from
  `repositories`;
- #145: the toggle flag, the status API, spend accounting;
- #147: vectors.

"Atomic file replacement" in the ADR is versioned, not DELETE+INSERT. Rows are written
invisibly (`valid_from` = the attempt's own number). The publication transaction closes
the old rows with `valid_to`. "File deletion" means the path is listed in `deleted_paths`
and no new rows are written for it.

## State invariants

The explicit purge marker is `purge_reason IS NOT NULL`.

- `purge`: no row, or already purged → no-op (generation does not grow). Otherwise:
  generation+1, status `empty`, `purge_reason` set, and publication, lease, counters and
  SHAs reset.
- `enable`:
  - no row → create it (generation 1);
  - purged → generation+1, `purge_reason` NULL, `last_used_at` set;
  - row exists and is not purged → no-op (a repeated toggle-on does not wipe a `ready`
    index).
- Reads see rows only when `status='ready'` (ADR:222-224: a `failed` publication is not
  used), plus the T6 predicate.
- A claim from `empty`/`failed` moves to `building` with `error_code` NULL (otherwise the
  CHECK fails).
- The state row is never deleted, except by FK cascade. Otherwise a re-enable would
  restart at generation 1 and an old job would match.
- The TTL re-check of `last_used_at` under FOR UPDATE, and the enumeration of repositories
  for the leader (without `repository_id`), are a separate seam in #143, not part of this
  port. Putting them here would break the contract test.

## Schema (migration `20261015_0032`)

### ENUM types

StrEnums live in `app/modules/code_index/domain/`:

- `code_index_status` {empty, building, ready, failed};
- `code_index_purge_reason` {ttl, removed, disabled};
- `code_index_edge_kind` {imports, uses_imported}. `defines` (ADR §3) is stored as the
  symbol row itself (path → logical key), with no duplicate in edges (decision of
  2026-10-15; stated explicitly in the PR).

Reuse, do not duplicate (BA:151):

- `SymbolKind` (`reviews/application/ast_symbols.py:51`) → `code_index_symbol_kind`;
- `ImportResolution` (`reviews/application/ast_resolution.py:54`) →
  `code_index_import_resolution`.

`pg_enum` (`app/common/infrastructure/db/columns.py:29`): widen the parameter type to
`type[StrEnum]`. Otherwise `common` would have to import the module.

### `code_index_states`

One row per repository. It is never deleted except by FK cascade. Otherwise a re-enable
would reset the generation and an old job would match.

- `repository_id` UUID PK, FK `repositories.id` ON DELETE CASCADE.
- `generation`, `reserved_publication`, `published_publication`, `floor_publication`:
  BIGINT NOT NULL default 0.
- `status` NOT NULL default `empty`; `purge_reason` NULL; `error_code` VARCHAR NULL;
  `last_error_code` VARCHAR NULL.
- `indexed_commit_sha` NULL; `published_at`, `last_used_at` TIMESTAMPTZ NULL.
- `lease_token` UUID NULL and `lease_expires_at` TIMESTAMPTZ NULL. The lease number is
  `reserved_publication` and the lease generation is `generation`; purge and enable always
  clear the lease.
- `file_count`, `symbol_count`, `edge_count`: INTEGER NOT NULL default 0. The database
  computes them at publication.
- Decision of 2026-10-15, "every field the ADR explicitly puts on the row":
  - versions (ADR:146-147): `extractor_version`, `resolver_version`,
    `parser_profile_version`, `limits_profile_version`, VARCHAR NULL. `publish` writes
    them (VO `IndexProfile`).
  - target (ADR:338-339): `target_commit_sha` NULL. Column only; its writer is the #143
    triggers. Purge resets it.
  - operational counters (ADR:515-516): `downloaded_bytes` BIGINT, `build_duration_ms`
    INTEGER, `github_request_count` INTEGER NOT NULL default 0. `publish` writes them
    (VO `IndexBuildStats`). Files/symbols/edges: see above.
  - [I] fields (default branch, ignore fingerprint, throttling of a full build from
    `empty`) are not added; #142/#143 introduce them. `published_at` is needed by GC (c)
    itself.
- `created_at`, `updated_at`: as in `repositories`.
- CHECK constraints:
  - `ck_code_index_states_failed_error`: `(status='failed') = (error_code IS NOT NULL)`;
  - `ck_code_index_states_lease_pair`;
  - `ck_code_index_states_purge_reason` (only with `empty`).

### Graph rows

Columns common to all graph tables:

- `repository_id` UUID NOT NULL, FK CASCADE;
- `generation` BIGINT;
- `valid_from` BIGINT NOT NULL;
- `valid_to` BIGINT NULL;
- `path` (type as in the existing path columns).

Keys are natural composite PKs. There are no surrogate ids, so edges have nothing to
reference by id.

- `code_index_files`: PK (repository_id, generation, valid_from, path); `blob_sha` (as
  `cached_file_blobs.blob_sha`).
- `code_index_symbols`: PK (repository_id, generation, valid_from, path, qualified_name,
  kind, disambiguator).
  - `disambiguator` INT NOT NULL: the 0-based ordinal among duplicates of
    (path, qualified_name, kind); a unique symbol gets 0.
  - `start_line`, `end_line`, plus a CHECK.
- `code_index_edges`: only `imports` and `uses_imported`.
  - PK (repository_id, generation, valid_from, path, kind, target_path, target_name).
  - `target_name` NOT NULL, `''` for `imports`, plus CHECK
    `(kind='imports') = (target_name='')`.
  - `ix_code_index_edges_target` (repository_id, generation, target_path, target_name).
  - `replace_file` collapses duplicates.
- `code_index_imports`: PK (repository_id, generation, valid_from, path, specifier,
  resolution).
  - `import x` plus `from x import y`, and a static plus a dynamic import of the same
    specifier, do not conflict.
  - `replace_file` collapses duplicates.
  - `target_path` NULL; `ix_code_index_imports_target`.

Server defaults (BIGINT 0, enum `'empty'`) are declared the same way in the ORM and the
migration as in 0031 (`compare_server_default=True`).

No columns for code text, signatures or embeddings (user, 2026-10-14: pointers only).

### Resolved details (coordinator, 2026-10-15)

Binding for the implementer; they close the planning hand-back's open points.

- Types: every `path`/`target_path` is `String(1024)` (precedent `findings.file_path`);
  `blob_sha`, `indexed_commit_sha`, `target_commit_sha` are `String(64)` (precedent
  `head_sha`/`base_sha`); `error_code`, `last_error_code` and the four `*_version` columns
  are `String(64)`; `qualified_name`, `target_name` are `String(512)`; `specifier` is
  `String(1024)`.
- `downloaded_bytes` (BIGINT), `build_duration_ms`, `github_request_count` (INTEGER) are
  all NOT NULL with server default 0, like the three entity counts.
- CHECK texts: `ck_code_index_states_lease_pair` = `(lease_token IS NULL) = (lease_expires_at
  IS NULL)`; `ck_code_index_states_purge_reason` = `purge_reason IS NULL OR status = 'empty'`
  (implication only); `ck_code_index_symbols_lines` = `start_line >= 1 AND end_line >=
  start_line`.
- `claim_lease` returns `None` for a purged row (`purge_reason IS NOT NULL`): only `enable`
  re-arms a purged repository, so a claim never violates the purge CHECK.
- `purge` resets: `status='empty'`, sets `purge_reason`, `generation+1`,
  `published_publication=0`, `floor_publication=0`, lease columns NULL, `indexed_commit_sha`,
  `target_commit_sha`, `published_at`, `error_code`, `last_error_code` and the four versions
  NULL, all six counters 0. It keeps `reserved_publication` (monotonic, never reset) and
  `last_used_at`.
- `enable` on a purged row: `generation+1`, `purge_reason` NULL, `status='empty'`,
  `last_used_at=now()`; on a new row: `generation=1`, `last_used_at=now()`.
- `ix_code_index_imports_target` = `(repository_id, generation, target_path)`.
- Why incremental publish carries `NOT EXISTS` (ADR §6.2, P2 of the PR #157 review): rows of
  an abandoned unpublished attempt of the same generation (`published < valid_from <
  mine`) would fall inside the visibility range once `published := mine`. The CAS refuses
  while any remain, so the caller must run `discard_abandoned` first. Full publication does
  not need it: `floor := mine` hides every lower number.
- mermaid-cli: pick the newest `@mermaid-js/mermaid-cli` release published at least 14 days
  ago (`npm view @mermaid-js/mermaid-cli time`) and pin that exact version in the PR body.

## Code

`app/modules/code_index/`:

- `domain/`: the StrEnums; `SymbolKey`; `assign_disambiguators(path, declarations)`, a
  pure function.
- `application/index_store.py`:
  - VOs: `IndexFence(token, generation, publication)`, `IndexedFile`, `IndexedSymbol`,
    `IndexedEdge`, `ImportRecord`, `IndexState`, `EdgeTarget`. `IndexProfile` and
    `IndexBuildStats` (see Schema) are the `publish` arguments.
  - `IndexLeaseLost(Exception)`.
  - `CodeIndexStore(Protocol)`, documented as "flushes, never commits".
  - `CodeIndexUnitOfWork(UnitOfWork, Protocol)`.
  - Template: `repositories/application/repository_settings.py:42-59`.
- `infrastructure/models.py` and `infrastructure/index_store.py`:
  - `SqlAlchemyCodeIndexStore(session)`, with no `scope`/`allow_unscoped`;
  - `SqlAlchemyCodeIndexUnitOfWork`, modelled on
    `repositories/infrastructure/repository_settings.py`.
- Registration in `app/bootstrap/db_metadata.py`.

### Port methods

**The first parameter is always `repository_id`, and every SQL statement filters by
it.** This includes `revoke_expired_lease` and `collect_garbage`. The signatures below
omit that leading parameter.

- **State:**
  - `get_state`.
  - `enable` (T1'): upsert with FOR UPDATE, generation+1, lease and publication cleared,
    `last_used_at` set.
  - `purge(reason)` (T1): FOR UPDATE. No row, or already purged → no-op; the generation
    does not grow.
- **Lease and writes:**
  - `claim_lease(generation, lease_seconds) -> IndexFence | None` (T2):
    - FOR UPDATE, `clock_timestamp()`;
    - a live lease is not taken over;
    - generation ≠ → `None`;
    - `empty` → `building`.
  - `discard_abandoned(fence, limit) -> int` (T3).
  - `replace_file(fence, file)` (T4): FOR SHARE plus a check of token, generation and
    expiry; delete the attempt's own rows for the path, then insert. Otherwise raise
    `IndexLeaseLost`.
- **Publication:**
  - `publish(fence, *, base_commit_sha, commit_sha, full, deleted_paths, profile, stats)
    -> bool` (T5): FOR UPDATE CAS.
    - Incremental: NOT EXISTS across the 4 tables, plus `valid_to := mine` on the visible
      rows of **all paths written with `valid_from = mine`, and of `deleted_paths`**
      (≤500).
    - Full: `floor := mine`.
    - The database computes the counters.
    - A fence failure raises `IndexLeaseLost`; any other CAS miss returns `False`.
- **Lease revocation and GC:**
  - `revoke_expired_lease`: a separate FOR UPDATE step.
  - `collect_garbage(limit) -> int` (T7 a/b/c): FOR SHARE; (c) runs no earlier than
    10 minutes after publication.
- **Reads** use the T6 predicate:
  - `get_file(path)`;
  - `file_symbols(path)`;
  - `outgoing_edges(path)`: the target is joined by name to visible symbols/files; an
    empty result means unresolved;
  - `incoming_edges(target_path, target_name)`;
  - `importers_of(target_path)`.

## Tests (test-first)

- `tests/test_code_index_symbol_key.py` (unit): the disambiguator; reordering
  declarations does not change the key.
- `tests/test_code_index_store_contract.py` (unit): every method of the Protocol and of
  the adapter has `repository_id` as its first parameter; the adapter constructor has no
  `scope`/`allow_unscoped`.
- `tests/test_code_index_store_postgres.py` (integration). It has its own fixture modelled
  on `test_initial_migration.py:28-48` and seeds, with raw SQL, two
  workspace/installation/repository sets.
  - **Schema:**
    - the exact column set of each table (literals);
    - FK `confdeltype='c'`;
    - `repository_id NOT NULL`.
  - **ENUM drift:** the exact PG ENUM values (`enum_range`) equal the StrEnum members,
    including `SymbolKind`/`ImportResolution` from #130, so extractor drift fails CI.
  - **Replace and flush-only:** replacing the same file twice in one attempt leaves only
    the second version visible; without `uow.commit()` nothing persists (flush-only).
  - **Two publications:** full c1 with `a.py`, then an incremental c2 that rewrites
    `a.py` → only the c2 rows are visible.
  - **State after publish** equals literals: `ready`, `indexed_commit_sha`,
    `published_at`, file/symbol/edge counts, the 4 versions, `downloaded_bytes`,
    `build_duration_ms`, `github_request_count` (AC3). `last_error_code` and
    `target_commit_sha` are columns only; their writers are #142/#143.
  - **Purge:**
    - after purge no visible rows remain;
    - `collect_garbage` run down to 0 → physically 0 rows of repository A;
    - a repeated purge is a no-op (generation unchanged);
    - repository B's rows are intact.
  - **Enable:** enable after enable → generation unchanged and the `ready` index intact;
    disable after enable → the old fence is rejected.
  - **Cross-tenant leak:** identical paths and names in A and B; every read method for A
    returns only A (literal `blob_sha` values and rows).
  - **Stale token:**
    - claim → purge → `replace_file`/`publish` with the old fence → `IndexLeaseLost`, no
      rows, state unchanged;
    - the same for disable+enable;
    - the same for an expired lease;
    - two sessions: purge waits for the batch's FOR SHARE (`pg_blocking_pids`); afterwards
      a write with the old fence is rejected.
  - **Identity (F4):** c1 [foo, bar] → c2 [bar, baz, foo]. The keys of foo/bar stay the
    same and the edge from the importer resolves.
  - **Unresolved edge (F4):** an incremental build deletes `a.py` and adds `c.py` with the
    same `foo` → the edge b.py→(a.py, foo) has no target.
- `test_initial_migration.py`: no edits expected. Check that it has no hard-coded lists.

## Documents

- **SD §11** (`docs/SYSTEM_DESIGN.md:1274-1311`):
  - add a `**#141:**` line with the 5 tables and their key fields;
  - add a storage paragraph: the index is repository-scoped, not Workspace-scoped; purge
    is explicit (logical, then GC within ≤1 h); the cascade is a safety net.
  - Ownership: SD is open in PRs #124, #148 and #155 (sections 985 / 645-1101 / 1445, not
    §11). The AGENTS rule is per file, so **before the docs commit, comment on each of
    the three PRs** (as task 1 did in #151).
- **ERD:** `docs/erd/erd.mmd` gets the 5 entities plus their relations from
  `REPOSITORIES`. Regenerate `erd.svg` with `npx -y @mermaid-js/mermaid-cli@<pinned>`. If
  that fails, flag it in the PR (the svg already lags: it has no `run_policy_snapshots`).
- **BA:**
  - «PostgreSQL и владение моделями» (`:408-415`): add a `code_index` row with its models;
  - «Миграции и проверка» (`:443-445`): add a sentence on revision 0032 and the new PG
    test.
- **TEST_PLAN §3.5** (`:336-356`): fill it in only if the rows reference tests.

## Process and commit order

- Worktree `projects/dmc-268-api-t6-141` from `origin/main`, branch
  `feat/141-rag-index-schema`; run `uv sync` and create `.env`. Use a dedicated database
  `dmc_141` on the local PG17, because of parallel NOTIFY traffic.
- Per `agent-loop`:
  - commit 1 is `docs/plans/141-plan.md` plus `141-todo.md` (English, the task brief for
    the executor);
  - implementation is done by a subagent from the brief file;
  - review is done by a clean `code-reviewer` (only `git show`/`git diff`, no checkout);
  - then `/verify-run`.
- Commits (Conventional, `(#141)`), each with green gates:
  1. plan;
  2. schema + migration;
  3. domain + port;
  4. adapter + PG tests;
  5. docs;
  6. `docs(adr)… (#140)`.
- Before the docs commit, comment on PRs #124, #148 and #155 (SD). Re-check the list of
  open PRs and their files at the time of the edit.
- PR:
  - title `feat(code_index): add graph index schema and fenced store (#141)`;
  - body What / Why / How to verify / `Refs #141`, with no closing keyword;
  - no new dependencies;
  - reviewer ndovnar;
  - #141 → On Review.

## Verification

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run pytest
TEST_DATABASE_URL=postgresql+psycopg://app:app@127.0.0.1:5432/dmc_141 \
  uv run pytest -m integration -rs tests/test_code_index_store_postgres.py \
  tests/test_initial_migration.py tests/test_alembic_heads.py   # 0 skips
uv run alembic heads   # exactly one head: 20261015_0032
```

- Upgrade → downgrade → upgrade on a clean database.
- `erd.svg` opens and contains `CODE_INDEX_STATES`.

Note: `tests/test_alembic_heads.py` has no `integration` marker. `-m integration`
therefore deselects it rather than skipping it. It runs in the plain `uv run pytest`
gate, and `uv run alembic heads` checks the same property.
