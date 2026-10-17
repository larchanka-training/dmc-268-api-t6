# Task list: RAG indexer, `index.repo` queue and AST symbol extraction (#142)

See [142-plan.md](142-plan.md) for the design. That plan is the source of truth; this list
adds no scope. AC1–AC12 are the numbered issue acceptance criteria in the plan's table.
Baseline `origin/main` `7546e53`. Three stacked PRs:

- PR 1, branch `feat/142-rag-indexer-store` from `origin/main`: Tasks 1–6;
- PR 2, branch `feat/142-rag-indexer-build` on PR 1: Tasks 7–15;
- PR 3, branch `feat/142-rag-indexer-queue` on PR 2: Tasks 16–24.

Each task is one planned commit, with two exceptions:

- the review and publication tasks (6, 15, 24);
- Tasks 2–4 land as one commit. They stay separate here as a test-first work order, not
  as commit boundaries. Migration 0034's `profile_complete` CHECK rejects the existing
  adapter's `publish`, which writes only 4 of the 6 profile columns. The contract test
  requires the adapter to have exactly the port's methods
  (`tests/test_code_index_store_contract.py:48-49`). So neither the migration nor the
  port can be committed green without the adapter.

Every task is test-first: write the named tests, watch them fail, then write the code.

Dependency order: Task 1 → 2 → … → 24. PR 2 starts after PR 1 is opened, PR 3 after PR 2
is opened. Each task also lists its direct dependencies.

**Common verification.** Every task that adds a commit runs these before it is marked done:

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run pytest
TEST_DATABASE_URL=postgresql+psycopg://app:app@127.0.0.1:5432/dmc_142 \
  uv run pytest -m integration -rs tests/test_code_index_*_postgres.py \
  tests/test_initial_migration.py tests/test_alembic_heads.py
```

In PR 3 (Tasks 16–24), the focused run adds the broker:

```sh
TEST_DATABASE_URL=postgresql+psycopg://app:app@127.0.0.1:5432/dmc_142 \
TEST_RABBITMQ_URL=amqp://app:app@127.0.0.1:5672/dmc_142 \
  uv run pytest -m integration -rs tests/test_code_index_*_postgres.py \
  tests/test_initial_migration.py tests/test_alembic_heads.py \
  tests/test_code_index_queue_postgres.py tests/test_worker_queue_postgres.py
```

Until Task 21 creates `tests/test_code_index_queue_postgres.py`, leave that path out of the
command: pytest fails on a missing path argument.

- The focused integration run must report **zero skips**. A skipped PostgreSQL or
  RabbitMQ acceptance test never counts as passing.
- `tests/test_alembic_heads.py` carries no `integration` marker, so `-m integration`
  deselects it. It runs in the plain `uv run pytest` gate.
- Use only the dedicated database `dmc_142` and the dedicated vhost `dmc_142`, never
  another session's database or broker: parallel sessions pick up each other's NOTIFY
  traffic. The vhost must exist with permissions for `app` (recipe in TEST_PLAN:44). Do
  not print credentials.

## PR 1: `feat(code_index): extend the index store for the indexer`

### Task 1: Plan documents

**Description:** Write the English plan and task list from the approved plan as the first
commit of PR 1: `docs(plans): plan the rag indexer (#142)`.

**Acceptance criteria:**

- [x] `docs/plans/142-plan.md` and `docs/plans/142-todo.md` translate the approved plan for
  all three PRs. They keep every file:line reference, the three user decisions of
  2026-10-16, the planner decisions, the deviations and the AC1–AC12 table.
- [x] No new decision, column, method or scope is added.

**Verification:** the gates are unaffected; only the two files are committed.

**Dependencies:** None. **Files touched:** `docs/plans/142-plan.md`,
`docs/plans/142-todo.md`. **Estimated scope:** Small.

### Task 2: Migration `20261017_0034`, ORM columns and migration tests (commit with Task 4)

**Description:** Extend `code_index_states` as in the plan's "Migration 0034" section
(revision 0033 went to #145, which merged first; this migration follows it):

- the columns `indexed_default_branch`, `exclusions_digest`, `stored_bytes`,
  `pending_bytes`;
- the profile reset of existing rows (the 4 versions → NULL);
- the CHECK constraints `profile_complete`, `bytes`, `pending_leased`, `failed_code`;
- the partial index `ix_code_index_states_live_lease`.

The ORM model in `app/modules/code_index/infrastructure/models.py` gets the same columns,
so that `tests/test_initial_migration.py` still sees no metadata↔migration drift.

Tests first:

- edit the existing tests named by the plan: `_COLUMNS`, and head 0034 in the downgrade
  test;
- also edit `tests/test_app_reinstall_schema_postgres.py:67-70` (hard-coded head
  `20261015_0032`) and `tests/test_code_index_store_postgres.py:495-502` (inserts
  `failed` + `parse_failed`, which the new `failed_code` CHECK rejects — use a
  `limit_exceeded:*` code);
- round trip 0034 → 0033 → head;
- one rejection test per new CHECK.

**Acceptance criteria:**

- [ ] `uv run alembic heads` shows exactly one head, `20261017_0034`
  (`down_revision="20261016_0033"`). Upgrade → downgrade → upgrade passes on a clean
  database.
- [ ] The PG test asserts the exact `code_index_states` column set and rejects a row for
  each of the four CHECKs.
- [ ] AC8 (storage side): CHECK `failed_code` admits `failed` only with `archive_unsafe`
  or `limit_exceeded:%`.

**Verification:** in the Task 4 commit (no separate commit; see the header):

- run the common verification block;
- `uv run alembic heads`; upgrade → downgrade → upgrade on a clean database.

**Dependencies:** Task 1.

**Files likely touched:**

- `alembic/versions/20261017_0034_<slug>.py`;
- `app/modules/code_index/infrastructure/models.py`;
- `tests/test_code_index_store_postgres.py`.

**Estimated scope:** Medium.

### Task 3: Domain codes, VOs, port and the storage estimate (commit with Task 4)

**Description:** Add the pieces that need no database:

- `app/modules/code_index/domain/codes.py` with `FAILED_ERROR_CODES`;
- `app/modules/code_index/domain/storage.py`: the Python half of the row estimate
  `row_bytes = 160 + Σ w(c)·len(s.encode())`, with the weights table from the plan;
- in `app/modules/code_index/application/index_store.py`:
  - `IndexProfile` += `default_branch`, `exclusions_digest`;
  - `IndexState` += `stored_bytes`, `pending_bytes`;
  - new VOs `ClaimRefusal`, `IndexAdmission`, `LeaseClaim`, `ClaimRefused`,
    `IndexStorage`;
  - the new and changed port methods: `claim_lease(..., admission) -> LeaseClaim |
    ClaimRefused`, `mark_failed`, `release`, `replace_file(...) -> IndexStorage`,
    `iter_files`, `iter_imports`, `visible_counts`;
  - the softened port docstring (admission reads sums over other repositories' state
    rows, never their graph rows).

The plan's PR 2 layout lists `domain/storage.py`, but the PR 1 parity test (Task 4) needs
the Python half, so it lands here. This is the only place where the todo moves plan
content between PRs, and the dependency order forces it.

Tests first:

- `tests/test_code_index_store_contract.py`: the new methods in `_PORT_METHODS`; every
  method's first parameter is `repository_id`.

**Acceptance criteria:**

- [ ] The contract test covers every new Protocol method, each with `repository_id`
  first.
- [ ] The unit tests fail before the code exists and pass after.

**Verification:** in the Task 4 commit (no separate commit; see the header).

**Dependencies:** Task 2.

**Files likely touched:**

- `app/modules/code_index/domain/codes.py`;
- `app/modules/code_index/domain/storage.py`;
- `app/modules/code_index/application/index_store.py`;
- `tests/test_code_index_store_contract.py`.

**Estimated scope:** Medium.

### Task 4: Adapter: admission, failure, release, byte accounting and reads (PG tests first)

This task's commit also carries Tasks 2 and 3 (see the header).

**Description:** Write the behavioural PG tests in `tests/test_code_index_store_postgres.py`
first and watch them fail. Edit the `_claim` helpers for the new return type and the
claim-from-failed test (a claim now refuses FAILED). Then implement in
`app/modules/code_index/infrastructure/index_store.py`, as in the plan's "Port and
adapter" section:

- `CODE_INDEX_ADMISSION_LOCK = 3_468_003` in `app/common/infrastructure/db/leader.py`;
- `claim_lease` with the advisory transaction lock, then the state row `FOR UPDATE`, and
  the check order STALE → LEASED → QUOTA → BUSY → grant;
- `mark_failed` (fenced, `FOR UPDATE`, `ValueError` outside `FAILED_ERROR_CODES`,
  publication reset);
- `release` (fenced; READY or EMPTY; `None` keeps `last_error_code`);
- `replace_file` byte accounting with `DELETE … RETURNING`;
- `publish`: the new profile columns, one aggregate per table → `stored_bytes`,
  `pending_bytes = 0`, success clears `last_error_code`;
- the `pending_bytes` reset on a CAS miss, `revoke_expired_lease` and `purge`; `purge`
  clears the new columns;
- the reads `iter_files`, `iter_imports`, `visible_counts`;
- the SQL half of the estimate (`coalesce(octet_length(c),0)`).

The adapter constructor stays `(session)`; the store flushes and never commits.

**Acceptance criteria:**

- [ ] Claim:
  - BUSY for a third repository while 2 leases are live, and for a second repository of
    the same installation;
  - expired leases do not count; STALE and LEASED win over BUSY;
  - a concurrent claim waits for the advisory lock; a claim waits for purge without a
    deadlock;
  - the claim returns `target_commit_sha`.
- [ ] AC8 and AC12 (store side): `mark_failed` and `release` cover both status branches
  and `last_error_code`; a stale fence raises `IndexLeaseLost`; after `mark_failed`, GC
  removes everything.
- [ ] AC2 (store side): a job that ends without a publication records its metrics, and
  the publication counters stay: `release(stats=…)` with and without a publication
  (`test_release_records_the_job_metrics_and_keeps_the_publication_counters`) and a
  publication miss (`test_a_publication_miss_spends_the_lease_so_the_next_wake_up_claims_at_once`).
- [ ] AC10 (quotas) and AC7 (no double count):
  - a repeated replace is not counted twice;
  - full and incremental publications set `stored_bytes`;
  - Python = SQL parity of the estimate;
  - repository and Workspace quotas → QUOTA with the right code; another Workspace is
    unaffected; pending bytes of other builds count only while their lease is live;
  - `replace_file` returns the live Workspace bytes: another live build's committed
    batch counts and a rolled-back batch does not
    (`test_a_batch_counts_the_pending_bytes_of_other_builds_against_the_workspace_quota`);
  - a batch and a claim wait for an open batch on the admission lock, then count its
    bytes (`test_a_batch_and_a_claim_wait_for_an_open_batch_and_then_count_its_bytes`);
  - a batch waits for an open publication whose lease expired, then counts its stored
    bytes (`test_a_batch_waits_for_an_open_publication_and_counts_its_stored_bytes`);
  - a duplicate claim waits for an open publication without a deadlock, so the
    publication takes the admission lock before the state row
    (`test_a_duplicate_claim_waits_for_an_open_publication_without_a_deadlock`).
- [ ] Record the RED run and the GREEN run.

**Verification:**

- Run the common verification block with zero skips.
- `uv run alembic heads`; upgrade → downgrade → upgrade on a clean database (Task 2).

**Dependencies:** Task 3.

**Files likely touched:**

- `app/common/infrastructure/db/leader.py`;
- `app/modules/code_index/infrastructure/index_store.py`;
- `tests/test_code_index_store_postgres.py`;
- `tests/test_code_index_store_contract.py` (the adapter half).

**Estimated scope:** Large.

### Checkpoint: PR 1 store

- [ ] Gates are green; the focused PG run has zero skips; there is one Alembic head
  `20261017_0034`.
- [ ] Every PR 1 test named in the plan passes and asserts literal values.

### Task 5: Coordination comments, then PR 1 documentation

**Description:** Before the docs commit, the **coordinator** (not the development
subagent) re-checks the open PRs and their files, and posts a concise coordination
comment in each open PR that touches `docs/SYSTEM_DESIGN.md`. Planned: #124, #148 and
#155. Then update:

- SD §11, the `#141` row (`docs/SYSTEM_DESIGN.md:~1303`): the new columns;
- BACKEND_ARCHITECTURE, the `code_index` ownership paragraph (~:142).

**Acceptance criteria:**

- [ ] The coordination comments are posted before the docs commit; their links are
  recorded here:
  - #124: https://github.com/larchanka-training/dmc-268-api-t6/pull/124#issuecomment-6214950766
  - #155: https://github.com/larchanka-training/dmc-268-api-t6/pull/155#issuecomment-6214951136
  - #148: https://github.com/larchanka-training/dmc-268-api-t6/pull/148#issuecomment-6214951477
- [ ] Each documented column matches migration 0034.

**Verification:** Run the common verification block.

**Dependencies:** Task 4.

**Files likely touched:** `docs/SYSTEM_DESIGN.md`, `docs/BACKEND_ARCHITECTURE.md`.

**Estimated scope:** Small.

### Task 6: PR 1 review, verify and publish

**Description:**

- A clean `code-reviewer` reviews the PR 1 diff with only `git show`/`git diff` and no
  checkout, reporting under `## Standards` and `## Spec`. After the review, check that
  the branch is still `feat/142-rag-indexer-store`.
- Check the reviewer's advice against `.agents/rules/backend.md` before acting on it.
  Findings go back to the development subagent; the review then runs again.
- Run `/verify-run`.
- Rebase on `main` before requesting review. The publisher pushes and opens the PR.

**Acceptance criteria:**

- [ ] All four gates pass, the focused PG run has zero skips, and there is one head.
- [ ] The fresh review reports zero findings on both axes.
- [ ] The PR is opened with:
  - title `feat(code_index): extend the index store for the indexer`;
  - body What / Why / How to verify / `Refs #142`, with no closing keyword and no
    Development-panel link;
  - the deviations from the plan's list that PR 1 implements (AC8 versus the ADR, claim
    refuses FAILED, `mark_failed` resets the publication, the Workspace quota fails a
    READY repository, published-then-empty stays READY, `stored_bytes` is an estimate);
  - a statement that there are no new dependencies;
  - reviewer ndovnar.

**Verification:** Record the tested head SHA, the gate results, the zero-skip PG run, the
review result and the PR URL.

**Dependencies:** Tasks 1–5. **Files touched:** this checklist. **Estimated scope:** Small.

## PR 2: `feat(code_index): build the symbol graph from a repository archive`

Branch `feat/142-rag-indexer-build` on top of PR 1.

### Task 7: Public extractor helpers in `reviews`

**Description:** Expose the shared code without changing review behaviour:

- `app/modules/reviews/application/ast_resolution.py`: public `python_module_target`,
  `script_module_target`, `go_package_target`, `module_target`, `python_submodule_files`,
  `go_package_name`, `exported_symbols`; new `import_watch(importer, module, language,
  go_module)`;
- `app/modules/reviews/application/vcs_diff.py`: public `is_generated_path` and
  `is_binary_content`; the private names stay as aliases;
- `app/modules/reviews/infrastructure/subprocess_source_parser.py`: the parameter
  `attempt_cpu_seconds: float | None = MAX_PARSE_CPU_SECONDS_PER_ATTEMPT`.

Tests first: parity of the new public functions in `tests/test_ast_resolution.py`.

**Acceptance criteria:**

- [ ] The parity tests pass; every existing review test stays green unchanged.
- [ ] The default of `attempt_cpu_seconds` keeps the review behaviour; `None` disables the
  per-attempt CPU budget.

**Verification:** Run the common verification block.

**Dependencies:** Task 6 (PR 1 opened).

**Files likely touched:** the three `reviews` files above, `tests/test_ast_resolution.py`.

**Estimated scope:** Medium.

### Task 8: Limits and exclusions

**Description:** Add `app/modules/code_index/domain/limits.py` (`IndexLimits`) and
`app/modules/code_index/application/exclusions.py` with the plan's exclusion order of 11
classes, one counter per class, and `INDEX_EXCLUSIONS_VERSION`. The path classes make the
import target `EXCLUDED`.

Tests first: `tests/test_code_index_exclusions.py`, one test per class, including the
order (`is_supported_path` runs before `IgnoreMatcher`).

**Acceptance criteria:**

- [ ] AC4 (unit): one test per class, all 11 in the plan's order (`is_supported_path`,
  secret-like, vendored, generated and lock, unsupported language, ignore from `rules.md`
  at HEAD, symlink, over 1 MiB, over 256 KiB, binary, decode or parse error), each with
  its counter.

**Verification:** Run the common verification block.

**Dependencies:** Task 7.

**Files likely touched:** `app/modules/code_index/domain/limits.py`,
`app/modules/code_index/application/exclusions.py`, `tests/test_code_index_exclusions.py`.

**Estimated scope:** Medium.

### Task 9: Symbol rows

**Description:** Add `app/modules/code_index/application/symbol_rows.py`:

- `qualified_name` along the `enclosing` chain joined with `.`; a Go method is
  `Recv.Name` from `declaration_kind`;
- an unnamed symbol becomes `default` for a default export, otherwise it is dropped with
  its descendants;
- then `assign_disambiguators`;
- the row filter before `replace_file` (symbol over 512 characters or key over 2000 bytes
  dropped; a too-long specifier drops the import; a too-long edge is dropped).

Tests first: `tests/test_code_index_symbol_rows.py` (names, disambiguators, the row
filter).

**Acceptance criteria:**

- [ ] The tests assert literal names and disambiguators, and no row reaching
  `replace_file` can make `_validate` reject the whole file.

**Verification:** Run the common verification block.

**Dependencies:** Task 6.

**Files likely touched:** `app/modules/code_index/application/symbol_rows.py`,
`tests/test_code_index_symbol_rows.py`.

**Estimated scope:** Small.

### Task 10: Index resolution

**Description:** Add `app/modules/code_index/application/index_resolution.py`: import
records, edges and `affected_importers`, as in the plan's "Resolver: incremental = full"
section:

- its own per-specifier entry point with no per-run caps;
- Python/TS: target facts are not read; `uses_imported` stores (path, exported name);
  barrels are not followed;
- Go: package-member facts are read; the record points at the package directory;
- the rules W, I, G and F → full.

Tests first: `tests/test_code_index_resolution.py`.

**Acceptance criteria:**

- [ ] AC11: every branch of the resolution table is tested; only the ADR edge kinds come
  out resolved, others are `unsupported` or `ambiguous`.
- [ ] No caps: 101+ imported names all resolve.
- [ ] AC5 (unit): `affected_importers` covers W, I, G and F.

**Verification:** Run the common verification block.

**Dependencies:** Task 7.

**Files likely touched:** `app/modules/code_index/application/index_resolution.py`,
`tests/test_code_index_resolution.py`.

**Estimated scope:** Large.

### Task 11: VCS port and GitHub snapshot source

**Description:** Add the provider-neutral port `RepositorySnapshotSource` with its VOs and
errors in `app/modules/code_index/application/vcs.py`, and its GitHub implementation in
`app/modules/code_index/infrastructure/github_snapshot_source.py`, as in the plan's "VCS
port" and "GitHub adapter" sections (HEAD, compare, tarball without auto-redirect, error
mapping modelled on `vcs_failures.py:21-37`, request counter).

Tests first: `tests/test_github_snapshot_source.py` with `httpx.MockTransport`.

**Acceptance criteria:**

- [ ] AC2: a 302 to codeload is followed without auth; a foreign host is refused; the
  download limit raises `limit_exceeded:download_bytes`; 403/429/`Retry-After` and
  404/409/422 map as in the plan; request counts are literal.
- [ ] AC5: 300 or more files → `complete=False`.
- [ ] Before the adapter is final, verify against the GitHub docs (context7 or WebFetch)
  the compare file limit and pagination, and the tarball redirect. Record the doc
  snippets for the PR body; a mismatch goes back to the coordinator before coding on.

**Verification:** Run the common verification block.

**Dependencies:** Task 6.

**Files likely touched:** `app/modules/code_index/application/vcs.py`,
`app/modules/code_index/infrastructure/github_snapshot_source.py`,
`tests/test_github_snapshot_source.py`.

**Estimated scope:** Medium.

### Task 12: Tar archive scan

**Description:** Add `app/modules/code_index/infrastructure/tar_archive.py`, as in the
plan's "Archive" section: pass 1 (`tarfile` `r|gz` over `BytesIO`, entries and unpacked
limits, root stripped, unsafe entries → `archive_unsafe`, symlinks in the path set but not
read, local git blob SHA-1 for files ≤1 MiB, `.review/rules.md` and `go.mod` captured) and
pass 2 (filter and parse in batches). Parsing runs in `asyncio.to_thread`; a monotonic
deadline is passed into `scan`/`contents`.

Tests first: `tests/test_code_index_tar_archive.py`.

**Acceptance criteria:**

- [ ] AC10: unsafe entries (absolute path, `..`, hardlink, device, FIFO, sparse), symlink,
  the entries and unpacked-size limits, many small files, and a large archive of excluded
  paths each have a test.
- [ ] The deadline stops a pass in time.

**Verification:** Run the common verification block.

**Dependencies:** Tasks 8 and 11.

**Files likely touched:** `app/modules/code_index/infrastructure/tar_archive.py`,
`tests/test_code_index_tar_archive.py`.

**Estimated scope:** Medium.

### Task 13: Use case `BuildRepositoryIndex`

**Description:** Add `app/modules/code_index/application/build_index.py`,
`app/modules/code_index/application/ports.py` (`IndexRepositoryReader`, `IndexWakeups`)
and `app/modules/code_index/infrastructure/repository_reader.py`, as in the plan's
"Algorithm" section:

- steps 1–12, every store call in its own short transaction, no network call inside one;
- the error outcomes, the deadline inside threads, the skip counters of `BuildOutcome`,
  and one structured log line at the end with no content and no client paths (SD §13);
- the versions `extractor_version`, `resolver_version`, `parser_profile_version`,
  `limits_profile_version`.

Tests first:

- `tests/test_build_repository_index.py` with fakes;
- the layer-boundary test (AST or import check).

**Acceptance criteria:**

- [ ] AC5: every mode decision is tested.
- [ ] AC8: every code is tested, including `head()` = None → EMPTY without an error.
- [ ] AC7: no transaction is open during a VCS call.
- [ ] AC12: a CAS miss and `IndexLeaseLost` behave as in the plan; the soft cutoff and a
  target change wake the repository.
- [ ] AC3: the skip counters equal literals.
- [ ] AC9: no `code_index/application/*` module imports `integrations` or the GitHub
  adapters.
- [ ] AC10 (storage quotas at write): after each `replace_file` the build checks both
  quotas on the returned `IndexStorage` before the batch commit: the repository quota on
  `stored_bytes + pending_bytes` and the Workspace quota on the live `workspace_bytes`. A
  batch over either quota rolls back, and the job ends with
  `limit_exceeded:repository_storage` or `limit_exceeded:workspace_storage`. Fake-store
  tests cover both codes and show the batch is not committed.

**Verification:** Run the common verification block.

**Dependencies:** Tasks 8–12.

**Files likely touched:** the three files above, `tests/test_build_repository_index.py`,
the layer-boundary test.

**Estimated scope:** Large.

### Task 14: Indexer PG integration tests

**Description:** Add `tests/test_code_index_indexer_postgres.py`: PG, the real parser, a
fake VCS with a tarball per commit. Incremental on repository A is compared with full on
B through the normalized sets of symbol keys, edges and import records. A failure found
here is fixed in the code of the owning task, in this commit.

**Acceptance criteria:**

- [ ] AC6: the six scenarios match (declaration inserted before the target; deletion and
  rename; import resolution changed; target file deleted; missed direct push; two merges
  before the queue drains).
- [ ] Also: Python tie-break, Go move, `EXTERNAL` → `RESOLVED` when a file is added.
- [ ] AC4/AC5: a file that became >256 KiB, binary or a symlink loses its rows; deleted and
  renamed files lose their rows.
- [ ] AC7: a redelivery does not duplicate rows and does not move the sha backwards.
- [ ] AC12: purge between batches stops the job without writes.
- [ ] AC4/AC10: one test per exclusion class and per limit.
- [ ] AC10 (Workspace quota at write): two live builds of one Workspace; the batch that
  crosses the quota is rolled back, and the committed Workspace total never exceeds it.

**Verification:** Run the common verification block with zero skips.

**Dependencies:** Task 13.

**Files likely touched:** `tests/test_code_index_indexer_postgres.py`.

**Estimated scope:** Large.

### Checkpoint: PR 2 graph build

- [ ] Gates are green; the focused PG run has zero skips.
- [ ] Every PR 2 test named in the plan passes and asserts literal values.

### Task 15: PR 2 review, verify and publish

**Description:** As Task 6, for the PR 2 diff against the PR 1 branch; after the review,
check that the branch is still `feat/142-rag-indexer-build`.

**Acceptance criteria:**

- [ ] All four gates pass and the focused PG run has zero skips.
- [ ] The fresh review reports zero findings on both axes.
- [ ] The PR is opened on base `feat/142-rag-indexer-store` with:
  - title `feat(code_index): build the symbol graph from a repository archive`;
  - body What / Why / How to verify / `Refs #142`, with no closing keyword and no
    Development-panel link;
  - the planner decisions (wake-only message, incremental also downloads the archive,
    extractor code stays in `reviews`, re-wake from the target snapshot) and the
    deviations PR 2 implements (256 KiB–1 MiB skipped, Go `Recv.Name`, the #144
    hand-off);
  - the GitHub doc snippets from Task 11;
  - a statement that there are no new dependencies;
  - reviewer ndovnar.

**Verification:** Record the tested head SHA, the gate results, the zero-skip PG run, the
review result and the PR URL.

**Dependencies:** Tasks 7–14. **Files touched:** this checklist. **Estimated scope:** Small.

## PR 3: `feat(code_index): consume index.repo in the worker`

Branch `feat/142-rag-indexer-queue` on top of PR 2. From here on, the common verification
includes the broker run.

### Task 16: Message contract `index.repo/v1`

**Description:** Add `contracts/schemas/index.repo.v1.schema.json` and
`contracts/examples/index.repo.v1.json` with the fields `schema`, `message_id`,
`repository_id`, `generation ≥ 1`, `attempt 1..3`, `requested_at`.

Tests first: `tests/test_contract_schemas.py` with the mutations missing key, extra key,
generation 0, attempt 0 and 4, bad uuid, foreign `schema` constant.

**Acceptance criteria:**

- [ ] The example validates; every mutation is rejected.

**Verification:** Run the common verification block.

**Dependencies:** Task 15 (PR 2 opened).

**Files likely touched:** the two contract files, `tests/test_contract_schemas.py`.

**Estimated scope:** Small.

### Task 17: Reusable republish in `reviews/infrastructure/amqp.py`

**Description:** Move the body of `republish_after_error` into a module-level
`republish_with_attempt(exchange, message, attempts)` and make the method delegate to it.
Give `_requeue_after_error` a `dead_letter_queue` parameter and a public alias. Give the
local fallback store a TTL per entry (`local_ttl_seconds` on `_requeue_after_error` and
`count_unexpected_attempt`, `None` → `LOCAL_RETRY_TTL_SECONDS` at call time), a read that
skips an expired entry, and `current_unexpected_attempts`. `consume`, `validate_message`,
`consumer_queue` and `RetryDelays` stay as they are.

**Acceptance criteria:**

- [ ] Review behaviour is unchanged: the existing review queue tests stay green
  unchanged.

**Verification:** Run the common verification block.

**Dependencies:** Task 16.

**Files likely touched:** `app/modules/reviews/infrastructure/amqp.py`.

**Estimated scope:** Small.

### Task 18: Index queue topology, wake-ups and the delivery handler

**Description:** Add `app/modules/code_index/infrastructure/amqp.py` (the topology of the
plan's table, the delivery handler, and `AmqpIndexWakeups`, which implements the
`IndexWakeups` port defined in PR 2's `application/ports.py`; #143 reuses that port).

- The worker declares the topology at startup on its own confirm channel, not through the
  reviews `amqp_channels`.
- `message_id = uuid5(NS, f"{repository_id}:{generation}:{requested_at}")`.
- The outcome → action table of the plan; a republish waits for the confirm before the
  original is acked.

Tests first: unit tests with `FakeExchange`, one per outcome row.

**Acceptance criteria:**

- [ ] AC1: confirm before ack; `x-attempt` → DLQ on the 3rd unexpected exception; the
  retry key is chosen as in the table (TRANSIENT 30s/2m, rate limit ≥ 2m,
  `Retry-After` > 2 min → 10m, ack on attempt 3).
- [ ] AC10/AC12: FAILED and LEASE_LOST are acked with no retry and no DLQ; RELEASED is
  acked too.
- [ ] BUSY republishes to `retry.30s` with the same attempt; LEASED with `redelivered`
  (or already waiting, or an `x-attempt` copy) republishes to `10m` with
  `x-lease-wait ≤ ⌈lease / 600 s⌉` (2 at the default lease); REWAKE is only acked (the
  use case already woke the repository with attempt 1).

**Verification:** Run the common verification block.

**Dependencies:** Task 17.

**Files likely touched:** `app/modules/code_index/infrastructure/amqp.py`, a new unit
test file.

**Estimated scope:** Medium.

### Task 19: Settings and the indexer composition

**Description:** Add `app/bootstrap/code_index_worker.py`:
`CodeIndexSettings.from_environment` with the plan's variables and defaults (an invalid
value fails startup), the use case composition, its own httpx client and token provider,
and its own `SubprocessSourceParser` (not the one at `worker.py:529`), with
`attempt_cpu_seconds=None`.

Tests first: validation of `CodeIndexSettings` and the limits digest.

**Acceptance criteria:**

- [ ] Every default equals the plan's table; the bounds (deadline ≤1500, lease ≥ deadline
  + 60, lease ≤ 86400, soft cutoff < deadline) fail startup when violated.
- [ ] AC3/AC8: the indexer has its own `SubprocessSourceParser` instance (the same
  extractor), so reviews never queue behind index jobs.

**Verification:** Run the common verification block.

**Dependencies:** Task 18.

**Files likely touched:** `app/bootstrap/code_index_worker.py`, a new unit test file.

**Estimated scope:** Medium.

### Task 20: Worker wiring

**Description:** Before the edit, the coordinator re-checks the open PRs and comments in
#148 (it touches `app/worker.py` and `.env.example`). Then, in `app/worker.py`, about 12
lines:

- the import;
- the field `WorkerSettings.code_index`;
- inside the `amqp_channels` block in `run_worker`, the index consumer and one
  `create_task`, before the heartbeat task.

`github_adapters`, `compose_worker_process` and `WorkerProcess` are not touched.

Tests first: `run_worker` wires the index consumer.

**Acceptance criteria:**

- [ ] AC1: the index consumer runs in the `worker` process, separate from the review
  consumers.
- [ ] The #148 coordination comment is posted before the edit; its link is recorded here.
  - #148: https://github.com/larchanka-training/dmc-268-api-t6/pull/148#issuecomment-6225258841

**Verification:** Run the common verification block.

**Dependencies:** Task 19.

**Files likely touched:** `app/worker.py`, the worker unit tests.

**Estimated scope:** Small.

### Task 21: Queue integration tests

**Description:** Add `tests/test_code_index_queue_postgres.py` (falls back to
`RABBITMQ_URL`) and extend `_reset_topology` in `tests/test_worker_queue_postgres.py`
with the new names.

**Acceptance criteria:**

- [ ] AC1: the exact queue arguments (406 on others); TTL returns the message to
  `index.repo`; an invalid message goes to the DLQ; the BUSY loop; the heartbeat stays
  fresh with three consumers.

**Verification:** Run the common verification block, broker run included, with zero
skips.

**Dependencies:** Task 20.

**Files likely touched:** `tests/test_code_index_queue_postgres.py`,
`tests/test_worker_queue_postgres.py`.

**Estimated scope:** Medium.

### Task 22: Deploy notes and memory measurement

**Description:**

- `.env.example`: a commented-out block of the `CODE_INDEX_*` variables at the end of the
  file (after the #148 comment of Task 20);
- `deploy/compose/staging.yml`: only a comment at `worker`;
- `docker-compose.yml` and `env-file.sh` do not change.

Measure the peak RSS on the large-archive test.

**Acceptance criteria:**

- [ ] The peak RSS figure is recorded for the PR body. If it is noticeable on the shared
  VPS, a memory limit is proposed separately, not added here.

**Verification:** Run the common verification block.

**Dependencies:** Task 21.

**Files likely touched:** `.env.example`, `deploy/compose/staging.yml`.

**Estimated scope:** Extra small (XS).

### Task 23: Coordination comments, then PR 3 documentation

**Description:** Before the docs commit, the **coordinator** re-checks the open PRs and
their files and comments in each open PR that touches the edited docs (planned for SD:
#124, #148, #155). Then update, as in the plan's "PR 3 documents":

- SD §4, §7.1, §7.2, a new §7.4 «Очередь index.repo (#142)» (outside §7.3), §14;
- PIPELINE_SPEC §4.3, §4.4, a new §4.6, §5.2, §12;
- CICD §3;
- BACKEND_ARCHITECTURE «Worker и очередь (#34)»;
- INFRASTRUCTURE §8 (:263);
- TEST_PLAN:44: the new broker test file.

**Acceptance criteria:**

- [ ] The coordination comments are posted before the docs commit; their links are
  recorded here:
  - #148 (`app/worker.py`, `.env.example`, `staging.yml`, SD): https://github.com/larchanka-training/dmc-268-api-t6/pull/148#issuecomment-6225258841
  - #161 (SD, PIPELINE_SPEC, BACKEND_ARCHITECTURE, TEST_PLAN): https://github.com/larchanka-training/dmc-268-api-t6/pull/161#issuecomment-6225259061
  - #133 (TEST_PLAN:44): https://github.com/larchanka-training/dmc-268-api-t6/pull/133#issuecomment-6225259343
  - #124 (SD): https://github.com/larchanka-training/dmc-268-api-t6/pull/124#issuecomment-6225259591
  - #155 (SD): https://github.com/larchanka-training/dmc-268-api-t6/pull/155#issuecomment-6225259869
  - #180 (PIPELINE_SPEC, SD, TEST_PLAN; opened after the comments above): https://github.com/larchanka-training/dmc-268-api-t6/pull/180#issuecomment-6226804931
  - #181 (PIPELINE_SPEC; opened after the comments above): https://github.com/larchanka-training/dmc-268-api-t6/pull/181#issuecomment-6226805195
- [ ] Each documented queue argument, delay, limit and outcome matches the code of Tasks
  16–22.

**Verification:** Run the common verification block.

**Dependencies:** Task 22.

**Files likely touched:** `docs/SYSTEM_DESIGN.md`, `docs/PIPELINE_SPEC.md`,
`docs/CICD.md`, `docs/BACKEND_ARCHITECTURE.md`, `docs/INFRASTRUCTURE.md`,
`docs/TEST_PLAN.md`.

**Estimated scope:** Medium.

### Checkpoint: PR 3 queue

- [ ] Gates are green; the focused PG and broker run has zero skips.
- [ ] Every PR 3 test named in the plan passes.

### Task 24: PR 3 review, verify and publish

**Description:** As Task 6, for the PR 3 diff against the PR 2 branch; after the review,
check that the branch is still `feat/142-rag-indexer-queue`.

**Acceptance criteria:**

- [ ] All four gates pass and the focused PG and broker run has zero skips.
- [ ] The fresh review reports zero findings on both axes.
- [ ] The PR is opened on base `feat/142-rag-indexer-build` with:
  - title `feat(code_index): consume index.repo in the worker`;
  - body What / Why / How to verify / `Refs #142`, with no closing keyword and no
    Development-panel link;
  - the planner decision and deviation "the message only wakes the job" (the
    `index.repo/v1` contract);
  - the peak RSS figure from Task 22;
  - a statement that there are no new dependencies;
  - reviewer ndovnar.
- [ ] #142 moved to On Review.

**Verification:** Record the tested head SHA, the gate results, the zero-skip run, the
review result and the PR URL.

**Dependencies:** Tasks 16–23. **Files touched:** this checklist. **Estimated scope:** Small.

## External: before merge and issue acceptance

- [ ] Required CI passes on the latest head of each PR (`ci-cd.yml` runs on
  `pull_request` without a branch filter, so stacked PRs are gated too).
- [ ] Each PR is rebased on `main` before merge; PR 2 and PR 3 are rebased after the
  previous one merges, and `uv run alembic heads` still shows one head.
- [ ] One approving review on the current head of each PR; all review threads resolved.
- [ ] The PRs are merged under repository rules.
- [ ] Manual e2e after PR 3 (local compose, a sandbox repository
  `axyi/dmc268-t6-sandbox*` through the dev App): `enable` the row with SQL, publish
  `index.repo/v1` with a one-liner, the worker log shows `ready` and
  `github_request_count` 3–4 for a full build.
- [ ] Staging only after the user's explicit OK.
- [ ] Lab: report `docs/reports/142/` and its mirror in
  `base/projects/team-6/api-t6-paperwork/issue-142/`; a row in `economics.md`.
- [ ] After PR 3 merges: the tech lead checks AC1–AC12 and the DoD, closes #142, and the
  result and status are reflected in #139 and on board 12.
