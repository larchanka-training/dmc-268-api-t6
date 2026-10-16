# Implementation plan: RAG indexer, `index.repo` queue and AST symbol extraction (#142)

## Status and baseline

Approved by the user on 2026-10-16 under the agent-loop skill. This document is the
English task brief for the executor of all three stacked PRs. It translates the approved
plan and adds no new decisions, columns, methods or scope. Where the plan says "verify
against the GitHub docs at implementation", that stays an implementation-time check, not
a settled fact.
Issue: https://github.com/larchanka-training/dmc-268-api-t6/issues/142, epic #139.
Baseline: `origin/main` `7546e539e1c1f565f65b9a5ca4cdc5dd1fbfb3fa` (#141 merged). The
single Alembic head is `20261015_0032`. Worktree `projects/dmc-268-api-t6-142`.
Branches, stacked: `feat/142-rag-indexer-store` (PR 1, from `origin/main`) →
`feat/142-rag-indexer-build` (PR 2, on PR 1) → `feat/142-rag-indexer-queue` (PR 3, on
PR 2). Task checklist: [142-todo.md](142-todo.md).

## Context

This issue is part 3 of epic #139 (Graph RAG v1). The blockers are lifted:

- ADR #140 is accepted (`docs/adr/0001-graph-rag-v1.md`);
- #141 merged on 2026-10-16 (main `7546e53`: the `code_index_*` tables, the
  `CodeIndexStore` port, fencing);
- #130/#136 are merged (the AST extractor and resolver in `reviews`).

#142 blocks #143 (lifecycle) and #144 (retriever).

Goal: a background consumer of `index.repo` in the `worker` process builds the symbol
graph of the default branch and writes it through the #141 port, with fencing, the ADR §7
limits and the exclusions. A full build reads one tar.gz archive; an incremental build
uses a compare from `indexed_commit_sha`. Webhooks and reviews are not touched.

## Issue acceptance criteria (numbered for cross-reference)

AC numbers follow the order of the checkboxes in issue #142. "PR n / Task m" points at
[142-todo.md](142-todo.md).

| AC | Requirement (issue #142) | Covered by |
| --- | --- | --- |
| AC1 | Indexing runs in a separate consumer of `index.repo`, not in webhook delivery and not in the review consumer; retry and DLQ work as for reviews | PR 3: topology, outcome table, wiring (Tasks 18–21). Tests: unit outcome rows with `FakeExchange` (confirm before ack, `x-attempt` → DLQ on the 3rd, retry-key choice); `run_worker` wires the index consumer; `tests/test_code_index_queue_postgres.py` (exact queue arguments, TTL returns to `index.repo`, invalid message → DLQ, BUSY loop, heartbeat fresh with three consumers) |
| AC2 | Full index reads the default-branch HEAD tree with one archive request through the VCS port; the number of GitHub requests per index is recorded; 403 and 429 with `Retry-After` are handled per PIPELINE_SPEC §5.2 | PR 2: GitHub adapter (HEAD, tarball, error mapping, request counter → `github_request_count`; Task 11); failed builds record it too (Task 13). Tests: `test_github_snapshot_source.py` (302 to codeload without auth, foreign host refused, download limit, 403/429/`Retry-After`, 404/409/422, literal request counts); manual e2e: 3–4 requests for a full build |
| AC3 | Symbols, references, imports and edges are extracted by the #130 extractor with the same grammar and profile versions; files in unsupported languages are skipped with a counter | PR 2: the extractor profile is the review's (user decision 3); `parser_profile_version` from `profile_version(lang)`; counter `skipped_unsupported_language` (Tasks 7, 13). PR 3: the indexer's own `SubprocessSourceParser` (Task 19). Tests: skip-counter literals in `test_build_repository_index.py`; real parser in `test_code_index_indexer_postgres.py` |
| AC4 | Excluded paths (generated, review-ignore, binary, lock, vendored, over the size limit, secret-like) do not enter the index; one test per class | PR 2: the exclusion order (Task 8). Tests: `test_code_index_exclusions.py`, one test per class; in `test_code_index_indexer_postgres.py`, one test per exclusion class, and a file that became >256 KiB, binary or a symlink loses its rows |
| AC5 | Incremental takes the change set per ADR #140 from `indexed_commit_sha` to the target commit, not the last PR's paths; deleted and renamed files lose their rows; the ADR fallback cases (no index, incomplete compare, target not a descendant of the indexed commit, default-branch change, too wide a set) run a full build (review #139, F2) | PR 2: algorithm steps 6–8, compare, rules W/I/G/F (Tasks 10, 11, 13). Tests: every mode decision in `test_build_repository_index.py`; `affected_importers` W/I/G/F in `test_code_index_resolution.py`; the 300-file threshold in `test_github_snapshot_source.py`; deletion and rename in `test_code_index_indexer_postgres.py` |
| AC6 | Normalized edge sets (not counts) of incremental and full on the same commit are equal for: declaration inserted before the target, declaration deleted and renamed, import resolution changed, target file deleted, missed direct push, two merges before the queue drains (review #139, F2, F4) | PR 2: resolver "incremental = full" (Task 10). Test: `test_code_index_indexer_postgres.py` (Task 14), incremental on repository A against full on B; the normalized sets of symbol keys, edges and import records are compared for the six scenarios |
| AC7 | No DB transaction spans a GitHub call; a redelivered message does not duplicate rows and does not move `indexed_commit_sha` backwards | PR 2: every store call is its own short transaction and no network call runs inside one; the message only wakes the job (Task 13); PR 1: no double count on a repeated `replace_file` (Task 4). Tests: "no transaction during a VCS call" in `test_build_repository_index.py`; "redelivery does not duplicate rows and does not move the sha backwards" in `test_code_index_indexer_postgres.py` |
| AC8 | Empty repository or no default branch → status `empty` without an error; failure → status `failed` with a code; neither blocks reviews | PR 1: `release`, `mark_failed`, CHECK `failed_code` (Tasks 2, 4); PR 2: step 4 and the error outcomes (Task 13). **Deviation:** `failed` only for `limit_exceeded:*` and `archive_unsafe` per ADR:222-225 and :382-391 (see Deviations). Reviews are unaffected: separate consumer, own parser. Tests: both status branches of `mark_failed`/`release` and `last_error_code` (PG, PR 1); every code in `test_build_repository_index.py`; 404/409/422 in `test_github_snapshot_source.py` |
| AC9 | The indexer use case depends only on the VCS port: the GitHub implementation is the only one, and the use case has no provider-specific code (compatible with #97) | PR 2: provider-neutral `RepositorySnapshotSource`; provider-neutral error codes carried by the port exceptions (Tasks 11, 13). Test: layer boundary, `code_index/application/*` imports neither `integrations` nor the GitHub adapters (AST or import check) |
| AC10 | ADR #140 limits: streaming download with a total-size check; entries, files, symbols, edges and the job deadline are bounded; exceeding one → status with the ADR code, no retry and no DLQ, review runs without RAG; when writing the archive to disk, absolute paths, `..`, symlinks, hardlinks and devices are refused. Tests: large archive of excluded paths, many small files, abort on a total limit, path-traversal and symlink entries (review #139, F3) | PR 2: download cap, archive pass 1, per-batch limits, deadline inside threads, `mark_failed("limit_exceeded:<limit>")` (Tasks 11–13); PR 3: FAILED → ack, no retry and no DLQ (Task 18); PR 1: storage quotas (Task 4). The archive is parsed from an in-memory stream and is never written to disk: unsafe entries → `archive_unsafe`, symlinks join the path set and are never read (exclusion class 7). Tests: `test_code_index_tar_archive.py` (unsafe entries, symlink, entries and unpacked limits, many small files, large archive of excluded paths); download limit in `test_github_snapshot_source.py`; one test per limit in `test_code_index_indexer_postgres.py`; quota tests (PG, PR 1) |
| AC11 | Only the ADR #140 edge kinds are stored as resolved; other relations are marked `unsupported` or `ambiguous` (review #139, F6) | PR 2: the resolution table (Task 10) on top of the #141 edge kinds {`imports`, `uses_imported`}. Test: every branch of the table in `test_code_index_resolution.py` |
| AC12 | Every write batch and the index publication re-check the fencing token per ADR #140 inside the write transaction; a mismatch stops the job without a write and without retry; network calls run outside the transaction (review #139, F1) | PR 1: fenced `mark_failed`, `release`, `replace_file`, `publish` (Task 4); PR 2: `IndexLeaseLost` → stop without write and retry (Task 13); PR 3: LEASE_LOST → ack (Task 18). Tests: stale fence → `IndexLeaseLost` for `mark_failed`/`release` (PG, PR 1); CAS miss in `test_build_repository_index.py`; purge between batches in `test_code_index_indexer_postgres.py` |

DoD (gates, docs, PRs with `Refs #142` and no auto-close, approve on the current head,
threads resolved, tech-lead acceptance) is covered by the review and publication tasks
and the external items in the todo.

## User decisions of 2026-10-16

1. **Three stacked PRs**, each with `Refs #142` and no closing keyword.
2. The ADR §7 storage quotas are **estimated in #142**: columns `stored_bytes` and
   `pending_bytes`.
3. Files of 256 KiB–1 MiB are **skipped with a counter**. The extractor profile is the
   review's; 1 MiB is a filter applied before the content is read.

## Planner decisions (stated explicitly in the PR body)

- **The message only wakes the job:** `{repository_id, generation}` per ADR:338-340, not
  "commit + mode" from the issue Scope.
  - The target is the default-branch HEAD that the job reads at its start. The archive is
    downloaded at that SHA.
  - A redelivery does not move `indexed_commit_sha` backwards (AC7). Jobs merge, so "two
    merges before the queue drains" is covered.
  - HEAD == indexed with the same versions and branch → release without publication.
- **Incremental also downloads the archive.** The archive gives the full path set and
  `.review/rules.md` at HEAD. The compare gives only the change set and the fallback
  signals. That is one request instead of up to 500 blob GETs under the 5000/h limit.
- **The shared extractor code stays in `reviews`**, and `code_index` imports it.
  Precedent: `code_index/domain/keys.py:8`; `.agents/rules/backend.md` §3 regulates only
  the direction of dependencies.
- **Re-wake per ADR:301-303** works from the `target_commit_sha` snapshot taken at claim,
  with no extra GitHub requests. The #143 triggers write the target.

## Out of scope

- **#143:** who enqueues `index.repo` jobs and when, including the triggers that write
  `target_commit_sha`. Future #143 publishers use the PR 3 topology constants and the
  `IndexWakeups` port. No kill-switch is added: there are no publishers before #143, so
  the consumer idles.
- **#144:** retrieval. Hand-off for #144: TS edges store the exported name, and Go records
  point at the package directory.
- **#145:** the toggle and the status in the API.
- **#97** (related, not blocking): GitLab. The indexer sits behind the provider-neutral VCS
  port.

## Coordination (before the edit)

- `docs/SYSTEM_DESIGN.md` (PR 1, PR 3): comments in #124, #148 and #155.
- `app/worker.py` and `.env.example` (PR 3): comment in #148.
- `github_installation_tree_provider.py`: PR #148 edits it, so PR 2 only imports
  `_is_empty_repository_answer` from it.
- `app/worker.py` wiring keeps the overlap with #148 minimal (see PR 3 Wiring). After #148
  merges, switching to the Redis token cache is a one-line change.
- The new SD §7.4 sits outside §7.3, which #148 edits.
- Before every docs commit, re-check the open PRs and their files.

## PR 1: `feat(code_index): extend the index store for the indexer`

Branch `feat/142-rag-indexer-store` from `origin/main`. Migration `20261016_0033`,
`down_revision="20261015_0032"`.

### Migration 0033 (`code_index_states`)

- Columns:
  - `indexed_default_branch varchar(255) NULL` and `exclusions_digest varchar(64) NULL`
    (= `IgnorePolicy.digest` of `rules.md` at HEAD);
  - `stored_bytes`, `pending_bytes bigint NOT NULL DEFAULT 0`.
- Profile reset of existing rows: the 4 versions → NULL, so the next build is full. No
  code on main writes such rows.
- CHECK constraints:
  - `profile_complete`: `num_nulls(<the 6 profile columns>) IN (0,6)`;
  - `bytes`: both counters ≥ 0;
  - `pending_leased`: `pending_bytes = 0 OR lease_token IS NOT NULL`;
  - `failed_code`: `status <> 'failed' OR error_code = 'archive_unsafe' OR error_code LIKE
    'limit_exceeded:%'`.
- Partial index `ix_code_index_states_live_lease (repository_id) WHERE lease_token IS NOT
  NULL`.

### Port and adapter (`app/modules/code_index/`)

The first parameter of every method is `repository_id`; the adapter constructor stays
`(session)`.

- **VOs:**
  - `IndexProfile` += `default_branch`, `exclusions_digest`;
  - `IndexState` += `stored_bytes`, `pending_bytes`;
  - new: `ClaimRefusal{STALE, LEASED, BUSY, QUOTA}`, `IndexAdmission(max_leases,
    max_installation_leases, repository_storage_bytes, workspace_storage_bytes)`,
    `LeaseClaim(fence, target_commit_sha, workspace_bytes)`, `ClaimRefused(reason,
    error_code)`, `IndexStorage`;
  - `domain/codes.py` with `FAILED_ERROR_CODES`.
- **`claim_lease(..., admission) -> LeaseClaim | ClaimRefused`.** Locks: the advisory
  transaction lock `CODE_INDEX_ADMISSION_LOCK = 3_468_003` (in
  `app/common/infrastructure/db/leader.py`), then the state row `FOR UPDATE`. Check order:
  1. STALE: no row, purged, another generation, or **FAILED**. This changes #141
     behaviour per ADR §7: the claim-from-failed test is edited.
  2. LEASED: this repository has a live lease.
  3. QUOTA: the repository or Workspace quota is exceeded (join `repositories →
     provider_installations`). Writes `failed` + `limit_exceeded:{repository,workspace}_storage`.
  4. BUSY: live leases ≥2 in total or ≥1 per installation, by `clock_timestamp()`.
  5. Lease granted: `pending_bytes = 0`, EMPTY → BUILDING.
- **`mark_failed(fence, error_code, *, stats=None)`**, fenced, `FOR UPDATE`:
  - the code must be in `FAILED_ERROR_CODES`, otherwise `ValueError`;
  - status FAILED; lease and pending are reset;
  - the publication is not used: published and floor → 0, profile and sha → NULL,
    counters → 0. GC removes the rows.
- **`release(fence, *, last_error_code=None) -> IndexStatus`**, fenced:
  - status READY if there was a publication, otherwise EMPTY; lease and pending are reset;
  - `None` keeps the previous `last_error_code`;
  - used for: the soft cutoff, an empty repository or no default branch, a transient
    error after retry, cleanup after an unexpected error;
  - limitation (stated in the PR): a repository that was published and later became empty
    stays READY (stale).
- **`replace_file(...) -> IndexStorage`.** `pending_bytes += added − removed`; rewriting a
  path within the same attempt is not counted twice (`DELETE … RETURNING`).
- **`publish`:**
  - writes the new profile columns;
  - one aggregate per table (count + bytes) → `stored_bytes`; `pending_bytes = 0`;
  - success clears `last_error_code`.
- **`pending_bytes` reset.** A CAS miss, `revoke_expired_lease` and `purge` also reset
  `pending_bytes`; `purge` also clears the new columns.
- **New reads for incremental builds** (visible rows, in portions): `iter_files(after,
  limit)`, `iter_imports(after, limit)`, `visible_counts(paths)`.
- **Port docstring:** soften "only the repository's rows". Admission reads sums over other
  repositories' state rows, but never their graph rows.

### Storage estimate

`row_bytes = 160 + Σ w(c)·octet_length(c)`, where `w(c)` = 1 + the number of indexes,
including the PK, that contain column `c`:

| Table | Weights |
| --- | --- |
| files | path 2, blob_sha 1 |
| symbols | path 2, qualified_name 2 |
| edges | path 2, target_path 3, target_name 3 |
| imports | path 2, specifier 2, target_path 2 |

The formula exists twice, in Python (`len(s.encode())`) and in SQL
(`coalesce(octet_length(c),0)`); a parity test keeps them equal. The PR 2 layout names the
Python half `domain/storage.py`; the PR 1 parity test needs it (see the todo, Task 3).

### PR 1 tests

Files: `tests/test_code_index_store_postgres.py`, the port contract test, the migration
test.

- **Edits to existing tests:** `_COLUMNS`, head 0033 in the downgrade test, the `_claim`
  helpers for the new return type, the new methods in `_PORT_METHODS`.
- **Migration:** round trip 0033 → 0032 → head; rejection by each CHECK.
- **Claim:**
  - BUSY: a third repository while 2 leases are live; a second repository of the same
    installation;
  - expired leases do not count; STALE and LEASED win over BUSY;
  - a concurrent claim waits for the advisory lock; a claim waits for purge without a
    deadlock.
- **`mark_failed` / `release`:** both status branches, `last_error_code`, stale fence →
  `IndexLeaseLost`; after `mark_failed`, GC removes everything.
- **Quotas:** no double count on a repeated replace; full and incremental publications;
  Python = SQL parity; repository and Workspace → QUOTA with the right code; another
  Workspace is unaffected; pending bytes of other builds count only while their lease
  is live.
- **Target snapshot:** the claim returns `target_commit_sha`.

### PR 1 documents

- SD §11, the `#141` row (`docs/SYSTEM_DESIGN.md:~1303`): the new columns.
- BACKEND_ARCHITECTURE, the `code_index` ownership paragraph (~:142).

## PR 2: `feat(code_index): build the symbol graph from a repository archive`

Branch `feat/142-rag-indexer-build` on top of PR 1.

### Layout (`app/modules/code_index/`)

- **`application/`:**
  - `vcs.py`: the port `RepositorySnapshotSource`, provider-neutral for #97;
  - `exclusions.py`;
  - `index_resolution.py`: import records, edges, `affected_importers`;
  - `symbol_rows.py`;
  - `build_index.py`: the use case `BuildRepositoryIndex`;
  - `ports.py`: `IndexRepositoryReader`, `IndexWakeups`.
- **`domain/`:** `limits.py` (`IndexLimits`), `storage.py` (the row estimate).
- **`infrastructure/`:** `github_snapshot_source.py`, `tar_archive.py`,
  `repository_reader.py`.

### VCS port

```python
class RepositorySnapshotSource(Protocol):
    request_count: int  # property
    async def head(self) -> BranchHead | None: ...              # None = empty repo / no default branch
    async def compare(self, base_sha, head_sha) -> Comparison | None: ...  # None = base unknown
    async def archive(self, commit_sha, *, max_bytes) -> SourceArchive: ...
```

- VOs:
  - `BranchHead(branch, commit_sha)`;
  - `Comparison(ancestry: AHEAD|IDENTICAL|BEHIND|DIVERGED, files: ChangedPath(path,
    status, previous_path), complete)`;
  - `ArchiveScan(commit_sha, entries, documents, counters)`.
- Errors: `VcsUnavailable(retry_after, rate_limited)`, `VcsAccessDenied`, `ArchiveUnsafe`,
  `IndexLimitExceeded(limit)`.

### GitHub adapter

- **HEAD:** `GET /repos/{full}` → `default_branch`; `GET /commits/{ref_segment(branch)}`
  with `Accept: application/vnd.github.sha`. A 409 "empty" answer (import
  `_is_empty_repository_answer`; PR #148 edits that file, so import only), or a 404 or 422
  on the branch → `None`.
- **Compare:** `/compare/{base}...{head}`, with a bounded body. 300 files or more, or an
  overflow → `complete=False`.
- **Tarball:** `/tarball/{sha}` without auto-redirect.
  - A 302 is accepted only to `https://codeload.github.com`; that GET goes without
    `Authorization`.
  - First `Content-Length`, then the running total, up to 100 MiB →
    `limit_exceeded:download_bytes`.
- **Errors** (PIPELINE_SPEC §5.2, model `vcs_failures.py:21-37`):
  - 403/429 with `Retry-After` or `X-RateLimit-Remaining: 0` → rate limited;
  - any other 403, or 404 → access denied;
  - 5xx, timeout → unavailable.
- **Request counter:** API and codeload requests, excluding the token exchange →
  `github_request_count`.
- **Verify at implementation against the GitHub docs** (context7 or WebFetch): the compare
  file limit and pagination, and the tarball redirect.

### Archive

The compressed bytes are buffered in memory under the limit. Parsing runs in
`asyncio.to_thread`, because the event loop is shared with reviews and the heartbeat.

- **Pass 1** (`tarfile` `r|gz` over `BytesIO`):
  - limits: every header, pax included, counts toward entries (100,000); unpacked bytes
    up to 1 GiB;
  - the root directory is stripped;
  - an absolute path, `..`, a hardlink, a device, a FIFO or a sparse entry →
    `archive_unsafe`;
  - a symlink joins the path set but is not read;
  - files ≤1 MiB get a local git blob SHA-1 (`blob <len>\0…`);
  - `.review/rules.md` and `go.mod` are captured.
- **Pass 2:** filter and parse in batches.
- **Exclusion order** (one counter per class):
  1. `is_supported_path` (before `IgnoreMatcher`, which otherwise raises);
  2. secret-like: `.env*`, `*.pem|key|p12|pfx|jks|kdbx`, `id_rsa*` and the like,
     `credentials.*`, `secret(s).*`, the directories `.ssh/.aws/.gnupg`;
  3. vendored: `vendor`, `node_modules`, `third_party`, `bower_components`,
     `site-packages`, `.venv`, `venv`, `Pods`;
  4. generated and lock: the public `is_generated_path` from `vcs_diff.py`;
  5. unsupported language;
  6. ignore from `rules.md` at HEAD;
  7. symlink;
  8. over 1 MiB;
  9. over 256 KiB;
  10. binary: the public `is_binary_content`;
  11. decode or parse error.

  The path classes make the import target `EXCLUDED`.
- **Row filter before `replace_file`** (`_validate` rejects the whole file): a symbol
  longer than 512 characters or a key over 2000 bytes is dropped; a specifier that is too
  long drops the whole import; an edge that is too long is dropped.

### Symbols

- `qualified_name` follows the `enclosing` chain joined with `.`. A Go method is
  `Recv.Name` from `declaration_kind`.
- An unnamed symbol becomes `default` if it is a default export; otherwise it is dropped
  together with its descendants.
- Then `assign_disambiguators`.

### Resolver: incremental = full

Its own per-specifier entry point, with no per-run caps. An edge depends on the path set
and the importer's content, not on the target's facts.

- **Python/TS:** target facts are not read. `uses_imported` stores (path, exported name)
  and is resolved by a join at read time (ADR:131-136); barrels are not followed.
- **Go:** package-member facts are read (alias, build constraints, package name); the
  record points at the package directory.
- **Importer re-extraction rules** (over all visible import records, unresolved ones
  included):
  - **W:** the `import_watch(...)` candidates intersect Δ (added, removed, renamed with
    both paths, copied, changed), or Δ is a child module of a watched package;
  - **I:** a changed `__init__.py` or `index.*` matching `target_path`;
  - **G:** a changed `.go` file in the `target_path` directory;
  - **F → full:** `go.mod` or `go.work` in the list, or C ∪ R > 500.
- **Edits in `reviews`** (review behaviour does not change; the existing tests stay):
  - `ast_resolution.py`: public `python_module_target`, `script_module_target`,
    `go_package_target`, `module_target`, `python_submodule_files`, `go_package_name`,
    `exported_symbols`; new `import_watch(importer, module, language, go_module)`;
  - `vcs_diff.py`: public `is_generated_path` and `is_binary_content`; the private names
    stay as aliases;
  - `subprocess_source_parser.py`: parameter `attempt_cpu_seconds: float | None =
    MAX_PARSE_CPU_SECONDS_PER_ATTEMPT`. The indexer passes `None`: a 1 s limit per file
    plus the job deadline. Otherwise, after 5 s of total CPU everything goes to
    `AST_TIMEOUT`.
- **Versions:**
  - `extractor_version` = `facts/v1`;
  - `resolver_version` = `resolver/v1+index/v1`;
  - `parser_profile_version` = sha256 of the sorted `profile_version(lang)` values.
    `profile_version` lives in `reviews/infrastructure/tree_sitter_facts.py:61`, so the
    whole `IndexProfile` is computed in the composition root
    (`app/bootstrap/code_index_worker.py`) and passed to the use case as a value;
    `code_index/application` never imports an infrastructure module;
  - `limits_profile_version` = `limits/v1:` + a digest of the limits that change content
    (1 MiB, 256 KiB) and of the exclusion versions (`BUILTIN_EXCLUSIONS_VERSION` and
    `INDEX_EXCLUSIONS_VERSION`).

  The "HEAD == indexed" check needs no download.

### Algorithm `BuildRepositoryIndex.execute(repository_id, generation)`

Every store call is its own short transaction; no network call runs inside a
transaction.

1. **No repository** → STALE.
2. **Claim the lease:** lease = deadline + 5 min. A refusal → the outcome (STALE, LEASED,
   BUSY or QUOTA).
3. **`discard_abandoned` in portions.** If it does not fit in the soft cutoff → release,
   wake, REWAKE.
4. **`head()` = None** → release → EMPTY. Empty repository or no default branch, without
   an error (AC8).
5. **HEAD == indexed** with the same versions and branch → release → UNCHANGED.
6. **Mode choice.**
   - Full: no index, the profile changed, or the default branch changed.
   - Otherwise compare. A `None` result, ancestry other than AHEAD, an incomplete list, or
     a touched `go.mod`/`go.work` → full.
7. **Archive, pass 1, policy.** `PolicyError` → release with
   `last_error_code="policy_<code>"`. A different `exclusions_digest` → full.
8. **Incremental only:** C = the compare paths ∪ the blob-SHA diff against `iter_files`;
   R = the importers by rules W/I/G. More than 500 → full.
9. **Parse, pass 2.** The needed Go and `__init__` facts are read separately; memory holds
   fact summaries, not content.
10. **Limits on every batch:** `parsed_files`, `symbols`, `edges` (for incremental: the
    state counters − `visible_counts(closed)` + the new ones), storage, deadline.
11. **`replace_file` in batches** of ~50 files per transaction.
12. **`publish(deleted_paths = (stored ∩ (C ∪ R)) − written)`.**
    - `False` → wake.
    - `True` → `get_state`; if `target_commit_sha` differs from the snapshot taken at
      claim → wake.

**Error outcomes.** The codes are provider-neutral and carried by the port exceptions; the
use case only passes the code on (AC9).

- a limit → `mark_failed("limit_exceeded:<limit>", stats=…)`: a failed build records
  `github_request_count` too;
- `ArchiveUnsafe` → `mark_failed("archive_unsafe", stats=…)`;
- `VcsUnavailable` → release with `vcs_rate_limited` or `vcs_unavailable` →
  TRANSIENT(retry_after);
- `VcsAccessDenied` → release with `vcs_access_denied`;
- `IndexLeaseLost` → stop without a write and without retry;
- deadline → `mark_failed("limit_exceeded:deadline")`;
- any other exception → release (best effort, fresh transaction, shield) → re-raise.

**Deadline inside threads.** `asyncio.timeout` does not interrupt work in `to_thread`.
So a monotonic deadline is passed into `scan`/`contents`, and a pass over 1 GiB stops in
time. The deadline `mark_failed` is written in a fresh shielded transaction.

**Skip counters.** Fields of the `BuildOutcome` result (per exclusion class,
`skipped_unsupported_language`, `skipped_too_large`, `parse_failed`); tests compare them
against literals. At the end of the job, one structured log line with no content and no
client paths (SD §13).

**Own parser.** `code_index_worker.py` creates its own `SubprocessSourceParser`, not the
one at `worker.py:529`. That one is sequential and stateful: sharing it would queue
reviews behind 15-minute jobs.

### PR 2 tests

**Unit:**

- `test_code_index_resolution.py`: every branch of the table, no caps on 101+ names,
  `affected_importers` W/I/G/F;
- parity of the new public functions in `test_ast_resolution.py`;
- `test_code_index_symbol_rows.py`: names, disambiguators, the row filter;
- `test_code_index_exclusions.py`: one test per class;
- `test_code_index_tar_archive.py`: unsafe entries, symlink, entries and unpacked-size
  limits, many small files, a large archive of excluded paths;
- `test_github_snapshot_source.py` (`httpx.MockTransport`): 302 to codeload without auth,
  foreign host refused, download limit, 403/429/`Retry-After`, 404/409/422, literal
  request counts, the 300-file threshold;
- `test_build_repository_index.py` (fakes): every mode decision, every code, the soft
  cutoff, a CAS miss, wake on a target change, no transaction during a VCS call,
  skip-counter literals;
- layer boundary (AC9): the `code_index/application/*` modules import neither
  `integrations` nor the GitHub adapters (AST or import check).

**Integration, `test_code_index_indexer_postgres.py`:** PG, the real parser, a fake VCS
with a tarball per commit. Incremental on repository A is compared with full on B; the
normalized sets of symbol keys, edges and import records are compared.

- the 6 AC scenarios: declaration inserted before the target; deletion and rename;
  import resolution changed; target file deleted; missed direct push; two merges before
  the queue drains;
- additionally: Python tie-break, Go move, `EXTERNAL` → `RESOLVED` when a file is added;
- a file that became >256 KiB, binary or a symlink loses its rows;
- a redelivery does not duplicate rows and does not move the sha backwards;
- purge between batches;
- one test per exclusion class and per limit.

## PR 3: `feat(code_index): consume index.repo in the worker`

Branch `feat/142-rag-indexer-queue` on top of PR 2.

### Topology

The queue arguments are fixed before the first staging deploy.

| Exchange | Type | Key / queue | Arguments |
| --- | --- | --- | --- |
| `code_index` | direct | `index.repo` | durable, `x-dead-letter-exchange=code_index.dlx` |
| `code_index.retry` | direct | `index.repo.retry.{30s,2m,10m}` | TTL from `RetryDelays`, DLX `code_index` → `index.repo` |
| `code_index.dlx` | fanout | `code_index.dlq` | TTL 7 days |

The worker declares it at startup on its own confirm channel, not through the reviews
`amqp_channels`. Future #143 publishers use the same constants.

### Message `index.repo/v1`

- Fields: `schema`, `message_id`, `repository_id`, `generation ≥ 1`, `attempt 1..3`,
  `requested_at`.
- `message_id = uuid5(NS, f"{repository_id}:{generation}:{requested_at}")`: unique per
  wake-up and deterministic from the fields (SD §7.2). A per-generation id does not work:
  every wake-up of a generation would share one key in the local unexpected-error counter
  (`amqp.py:243-273`, TTL 300 s for an unconfirmed handoff), and a healthy repository
  could land in the DLQ.
- Files: `contracts/schemas/index.repo.v1.schema.json` and
  `contracts/examples/index.repo.v1.json`.

### Outcomes → AMQP action

A republish always waits for the confirm, and only then acks the original.

| Outcome | Action |
| --- | --- |
| decode or schema error, foreign `message_id` | `nack(requeue=False)` → DLQ |
| PUBLISHED, UNCHANGED, EMPTY, STALE, FAILED, LEASE_LOST | ack |
| LEASED | ack; if `redelivered`, republish to `10m` with `x-lease-wait ≤ 2` (holder SIGKILLed) |
| BUSY | republish to `retry.30s` with the same attempt, ack (no nack-requeue spin) |
| REWAKE | `wake()` to `index.repo` with attempt 1, ack |
| TRANSIENT | attempt 1 → 30s, 2 → 2m; rate limit ≥ 2m; `Retry-After` > 2 min → 10m; on attempt 3, ack (`last_error_code` is already written) |
| unexpected exception | `x-attempt` republish; on the 3rd → DLQ |

### Reuse of `reviews/infrastructure/amqp.py`

- The body of `republish_after_error` moves into a module-level
  `republish_with_attempt(exchange, message, attempts)`; the method delegates to it.
- `_requeue_after_error` gets a `dead_letter_queue` parameter and a public alias.
- `consume`, `validate_message`, `consumer_queue` and `RetryDelays` are used as they are.
- New file: `code_index/infrastructure/amqp.py` (topology, `AmqpIndexWakeups`, the
  delivery handler). It implements the `IndexWakeups` port that PR 2 defines in
  `code_index/application/ports.py`; #143 reuses the same port.

### Wiring

Minimal overlap with #148.

- New `app/bootstrap/code_index_worker.py`: `CodeIndexSettings.from_environment`, the use
  case composition, its own httpx client and token provider.
- `app/worker.py`, about 12 lines:
  - the import;
  - the field `WorkerSettings.code_index`;
  - inside the `amqp_channels` block in `run_worker`, the index consumer and one
    `create_task`, before the heartbeat task.
- `github_adapters`, `compose_worker_process` and `WorkerProcess` are not touched. After
  #148 merges, a one-line switch to the Redis token cache.

### Environment variables

The defaults live in code; an invalid value fails startup.

| Variable | Default |
| --- | --- |
| `CODE_INDEX_MAX_CONCURRENT` / `_PER_INSTALLATION` | 2 / 1 |
| `CODE_INDEX_DEADLINE_SECONDS` | 900 (≤1500, below `consumer_timeout`) |
| `CODE_INDEX_LEASE_SECONDS` | 1200 (≥ deadline + 60) |
| `CODE_INDEX_SOFT_CUTOFF_SECONDS` | 300 (< deadline) |
| `CODE_INDEX_DISCARD_BATCH_ROWS` | 10000 |
| `CODE_INDEX_MAX_DOWNLOAD_BYTES` | 104857600 |
| `CODE_INDEX_MAX_UNPACKED_BYTES` | 1073741824 |
| `CODE_INDEX_MAX_ARCHIVE_ENTRIES` | 100000 |
| `CODE_INDEX_MAX_FILE_BYTES` | 1048576 |
| `CODE_INDEX_MAX_PARSED_FILES` / `_SYMBOLS` / `_EDGES` | 20000 / 500000 / 2000000 |
| `CODE_INDEX_MAX_INCREMENTAL_FILES` | 500 |
| `CODE_INDEX_MAX_REPOSITORY_STORAGE_BYTES` | 536870912 |
| `CODE_INDEX_MAX_WORKSPACE_STORAGE_BYTES` | 2147483648 |

No kill-switch is added: there are no publishers before #143, so the consumer idles.

### Deploy

- `.env.example`: a commented-out block at the end of the file.
- `deploy/compose/staging.yml`: only a comment at `worker`.
- `docker-compose.yml` and `env-file.sh` do not change: the service is the same and the
  variables are not secrets.
- Memory. `worker` has no `mem_limit` and no `deploy.resources` in either
  `docker-compose.yml` or `staging.yml` (checked). The peak of one job is the ≤100 MiB
  buffer, the tar state and the fact summaries, plus the parser subprocess with RLIMIT_AS
  128 MiB. The peak RSS is measured on the large-archive test, and the number goes into
  the PR body. If the peak is noticeable on the shared VPS, propose a limit separately.

### PR 3 documents

- SD:
  - §4: worker and RabbitMQ;
  - §7.1: three rows and the parameters;
  - §7.2: the `index.repo/v1` row;
  - a new §7.4 «Очередь index.repo (#142)»: the outcomes and who declares the topology;
    outside §7.3, which #148 edits;
  - §14: one sentence.
- PIPELINE_SPEC: §4.3, §4.4, a new §4.6 (delays, concurrency limit), §5.2 (the indexer's
  GitHub errors), §12.
- CICD §3: services and heartbeat.
- BACKEND_ARCHITECTURE «Worker и очередь (#34)».
- INFRASTRUCTURE §8 (:263).
- TEST_PLAN:44: the new broker test file.

### PR 3 tests

- `test_contract_schemas.py`: `index.repo.v1` with mutations (missing key, extra key,
  generation 0, attempt 0 and 4, bad uuid, foreign `schema` constant).
- Unit with `FakeExchange`, one test per outcome row: confirm before ack, `x-attempt` →
  DLQ on the 3rd, choice of the retry key.
- Validation of `CodeIndexSettings` and the limits digest.
- `run_worker` wires the index consumer.
- `tests/test_code_index_queue_postgres.py` (falls back to `RABBITMQ_URL`):
  - the exact queue arguments (406 on others);
  - TTL returns the message to `index.repo`;
  - an invalid message goes to the DLQ;
  - the BUSY loop;
  - the heartbeat stays fresh with three consumers.
- Extend `_reset_topology` in `test_worker_queue_postgres.py` with the new names.

## Process

**Environment:**

- Worktree `projects/dmc-268-api-t6-142` from `origin/main`; branches PR 1 → PR 2 → PR 3,
  stacked;
- `uv sync`, `.env`;
- a dedicated database `dmc_142` on the local PG17 and a dedicated RabbitMQ vhost
  `dmc_142`: parallel sessions pick up each other's NOTIFY traffic.

**Loop per `agent-loop`:**

- The first commit of PR 1 is `docs/plans/142-plan.md` and `142-todo.md` (English, the
  executor brief for all three PRs).
- Implementation is done by a subagent from the brief file.
- Review is done by a clean `code-reviewer`: only `git show`/`git diff`, no checkout;
  after the review, check the branch.
- Then `/verify-run`.
- Check the reviewer's advice against `.agents/rules/backend.md` before pushing.
- Conventional commits with `(#142)`; gates green on every commit.

**PRs:**

- body What / Why / How to verify / `Refs #142`, no closing keyword; no new dependencies
  (stdlib `tarfile`/`hashlib`; httpx is already a dependency);
- reviewer ndovnar;
- #142 → On Review once PR 3 is ready;
- PR 2 and PR 3 are rebased after the previous one merges.

**CI on the stack.** `ci-cd.yml` runs on `pull_request` without a branch filter, so the
gates also run on PRs whose base is a branch.

## Deviations (stated in the PR body)

- **AC8 versus the ADR.** AC8 asks for "failure → status `failed` with a code". The plan
  follows ADR:222-225 and :382-391:
  - `failed` is only `limit_exceeded:*` and `archive_unsafe`; CHECK `failed_code` pins
    this;
  - any other terminal failure (VCS after retry, policy) → release with `last_error_code`;
    the last publication stays (`stale` or `empty`);
  - reviews are not blocked in either case.
- **AC10 "when the archive is written to disk".** The archive is never written to disk:
  it is parsed in memory (ADR:377-378). A symlink is skipped and counted, not rejected
  (ADR:378-379); an absolute path, `..`, a hardlink, a device or a FIFO stops the job
  with `archive_unsafe` (ADR:379-380).
- The message only wakes the job (not commit + mode from the Scope).
- A claim refuses FAILED (a change of #141 behaviour).
- `mark_failed` resets the publication.
- Exceeding the Workspace quota moves a READY repository to `failed` too.
- A repository that was published and later became empty stays READY.
- `stored_bytes` is an estimate, not the real on-disk size.
- Files of 256 KiB–1 MiB are skipped.
- The `qualified_name` of a Go method is `Recv.Name`.
- For #144: TS edges store the exported name, and Go records point at the package
  directory.

## Lab-side work

- Report `docs/reports/142/` (lab-local) and its mirror in
  `base/projects/team-6/api-t6-paperwork/issue-142/`;
- a row in `economics.md`;
- after PR 3 merges, check the #142 AC and add a note in #139.

## Verification

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run pytest
TEST_DATABASE_URL=postgresql+psycopg://app:app@127.0.0.1:5432/dmc_142 \
  uv run pytest -m integration -rs tests/test_code_index_*_postgres.py \
  tests/test_initial_migration.py tests/test_alembic_heads.py   # 0 skips
uv run alembic heads   # exactly one head: 20261016_0033
```

- PR 3 adds `TEST_RABBITMQ_URL=amqp://…/dmc_142` and
  `tests/test_code_index_queue_postgres.py tests/test_worker_queue_postgres.py` to the
  focused run.
- Upgrade → downgrade → upgrade on a clean database.
- Manual e2e after PR 3 (local compose, a sandbox repository `axyi/dmc268-t6-sandbox*`
  through the dev App):
  1. `enable` the row with SQL;
  2. publish `index.repo/v1` with a one-liner;
  3. the worker log shows `ready`, and `github_request_count` is 3–4 requests for a full
     build.
- Staging only after the user's explicit OK.

Note: `tests/test_alembic_heads.py` has no `integration` marker. `-m integration`
therefore deselects it rather than skipping it. It runs in the plain `uv run pytest`
gate, and `uv run alembic heads` checks the same property.
