# Task list: RAG context provider, review-side index refresh (#144)

See [144-plan.md](144-plan.md) for the design. That plan is the source of truth; this list
adds no scope. AC1–AC13 are the numbered issue acceptance criteria in the plan's table;
U1–U4 and M1–M10 are the decisions named there. Baseline `origin/main` `22c621ab`, Alembic
head `20261020_0037`. Two stacked PRs here; PR 3 gets its own plan:

- PR 1, branch `feat/144-rag-index-review-refresh` from `origin/main`: Task 0, then Tasks 1–7;
- PR 2, branch `feat/144-rag-context-provider` on PR 1: Tasks 1–8.

Each task is one Conventional commit whose subject ends with `(#144)`; scopes are
`code_index`, `reviews`, `worker`, `plans` (underscore, the repository's convention), or an
unscoped `docs:`. ⚑ marks a task with more than 10 edits across the files it touches. Every
task that writes code is delegated to a subagent through a brief file
(`standards/workflow.md` §5.1). The coordinator keeps coordination, verification and the
commands. **Every brief says: never read `.env` of any clone.**

Every task is test-first: write the named tests, watch them fail, record the RED run, then
write the code and record the GREEN run. Docs tasks instead keep the existing tests green.

Dependency order: PR 1 Task 0 → 1 → … → 7; PR 2 Task 1 → … → 8. PR 2 starts after PR 1 is
opened. Each task lists its direct dependencies. One change from the approved commit order:
the port and use case (PR 1 Task 3) come before the adapter (PR 1 Task 4), because the
adapter returns the port's `RagReadiness`.

**Common verification.** Every task that adds a commit runs these before it is marked done:

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy . --exclude '^docs/reports/'
uv run pytest
TEST_DATABASE_URL=postgresql+psycopg://app:app@127.0.0.1:5432/dmc_144 \
TEST_RABBITMQ_URL=amqp://app:app@127.0.0.1:5672/dmc_144 \
  uv run pytest -m integration -rs tests/test_code_index_*_postgres.py tests/test_rag_*.py \
  tests/test_app_reinstall_schema_postgres.py tests/test_worker_queue_postgres.py \
  tests/test_initial_migration.py
```

- The focused integration run must report **zero skips**. A skipped PostgreSQL or RabbitMQ
  test never counts as passing.
- `tests/test_alembic_heads.py` carries no `integration` marker, so `-m integration`
  deselects it; it runs in the plain `uv run pytest` gate.
- `uv run pytest` locally: the 7 failures of `test_deploy_monitoring` come from the
  `~/.local/bin/sh` wrapper; run the gate without it in `PATH`.
- Use only the dedicated database `dmc_144` and the vhost `dmc_144`, never another
  session's: parallel sessions pick up each other's NOTIFY traffic. The vhost must exist
  with permissions for `app` (recipe in `docs/TEST_PLAN.md:44`). Do not print credentials.
- `df /tmp` before long runs.

## PR 1: `feat(code_index): refresh and keep the index alive from reviews`

### Task 0: Plan documents

**Commit:** `docs(plans): add the #144 plan (#144)`

**Files:** `docs/plans/144-plan.md`, `docs/plans/144-todo.md`.

**What to do:** commit the English plan and task list translated from the approved plan.

**Tests to add:** none.

**Done when:**

- [x] both files translate the approved plan for PR 1 and PR 2 and outline PR 3: U1–U4,
  M1–M10, the PR designs, the AC1–AC13 table, the deviations and the verification;
- [x] no new decision, column, method or scope is added;
- [x] only the two files are committed; the gates are unaffected.

**Dependencies:** none.

### Task 1: Migration for the review throttle ⚑

**Commit:** `feat(code_index): store the review full-build throttle (#144)`

The ⚑ comes from the derived test and doc edits the migration forces (plan, "Migration").

**Files:**

- new `alembic/versions/20261023_0038_code_index_review_trigger.py` (suggested slug; the
  next free number after `main`'s head at commit time; check #220/#169 first);
- `app/modules/code_index/infrastructure/models.py` (`CodeIndexState`, `:35-143`);
- `tests/test_code_index_store_postgres.py`: `_COLUMNS` (`:72-106`), `_CHECKS`
  (`:167-175`), the `new_columns` set of the 0033 downgrade test (`:832-839`), the head
  assertions at `:727`, `:752`, `:878`, a new migration test;
- `tests/test_app_reinstall_schema_postgres.py:67-69` (the head and the revision chain);
- `docs/CICD.md` §3: a new top row of the downgrade-loss table (above `:244`; rule
  `:237-238`) and the revision list `:263`.

**What to do:**

- Columns `review_full_requested_at timestamptz NULL` and
  `review_full_requested_sha varchar(64) NULL` (U3).
- CHECK `ck_code_index_states_review_full_sha_at`:
  `review_full_requested_sha IS NULL OR review_full_requested_at IS NOT NULL` ("sha
  implies at": a NULL target records only the time; decided by the coordinator, 23.10). `purge()` is not changed and
  keeps both columns.
- `down_revision = "20261020_0037"`; downgrade drops the CHECK and the columns. The same
  objects go into the ORM model.

**Tests to add (PG):** the new migration test, in the shape of
`tests/test_code_index_store_postgres.py:680-727`: the columns and the CHECK exist; the
CHECK refuses a sha without a time and accepts a time without a sha; downgrade to
`20261020_0037` drops them and keeps the state row; upgrade restores them.

**Done when:**

- [x] `uv run alembic heads` shows one head;
- [x] upgrade → downgrade → upgrade on a clean database;
- [x] `tests/test_initial_migration.py`, `tests/test_alembic_heads.py` and
  `tests/test_app_reinstall_schema_postgres.py` are green;
- [x] the RED run and the GREEN run are recorded;
- [x] common verification is green.

**Dependencies:** Task 0.

### Task 2: The review trigger decision

**Commit:** `feat(code_index): decide the review trigger (#144)`

**Files:**

- new `app/modules/code_index/domain/review_trigger.py`;
- new `tests/test_code_index_review_trigger.py` (suggested name).

**What to do:**

- A pure function over the locked row's fields and `now` that returns which writes to make
  (plan, "Decision": the table, M1 in full, M4, M5).
- Suggested input: the `RagIndexStatus` from `rag_index(...)`
  (`app/modules/code_index/domain/rag_index.py:37-70`), so status derivation stays there;
  plus `purge_reason`, `wake_requested_at`, `last_error_code`, `target_commit_sha`,
  `last_used_at`, the two throttle columns and `now`.
- No I/O; the result never reaches the trace.

**Tests to add (unit, literal inputs and expected outputs):**

- every row of the plan's table;
- M1: a repeat at 23 h 59 min refused, at 24 h allowed for a new target, refused for the
  same target; a NULL target → only the 24 h limit;
- the extension: NULL → extend; 59 min → no; 1 h → extend;
- M5: a set marker → no request; a healable `last_error_code` (`vcs_unavailable`) →
  request; a terminal one (`limit_exceeded:download_bytes`) → no request.

**Done when:**

- [x] every table row has a test;
- [x] the RED run and the GREEN run are recorded;
- [x] common verification is green.

**Dependencies:** Task 1 (the throttle columns exist by name).

### Task 3: RAG readiness port and use case

**Commit:** `feat(reviews): add the RAG readiness port and use case (#144)`

**Files:**

- new `app/modules/reviews/application/rag_index.py`;
- new `tests/test_rag_readiness.py` (suggested name).

**What to do:**

- `RagReadiness(enabled, index, default_head_sha)`, the port `ReviewIndexAccess` with
  `open_for_review(repository_id) -> RagReadiness` (flush-only), `ReviewIndexUnitOfWork`,
  and the use case `OpenReviewIndex.execute(repository_id) -> RagReadiness` that commits
  (all suggested names; plan, "Port, use case and capability set").
- M6: the capability set constant, empty, with a helper (suggested) that answers whether a
  pinned `(review_system_key, review_system_version)` has a retrieved section
  (`app/modules/reviews/application/policy_snapshot.py:136-137`).
- Import from `code_index` only `app/modules/code_index/domain/rag_index.py`.

**Tests to add (unit, fakes of the port):**

- the use case commits exactly once and returns the port's value;
- a port error propagates and nothing is committed;
- the capability set is empty: `("review.system", "v3")` has no retrieved section.

**Done when:**

- [x] no import of `code_index.application` or `code_index.infrastructure` from
  `reviews.application`;
- [x] the RED run and the GREEN run are recorded;
- [x] common verification is green.

**Dependencies:** Task 0.

### Task 4: Request a build and extend `last_used_at` on a review read ⚑

**Commit:** `feat(code_index): request a build and extend last_used_at on a review read (#144)`

**Files:**

- new `app/modules/code_index/infrastructure/review_index.py`;
- new `tests/test_code_index_review_index_postgres.py` (fixture pattern of
  `tests/test_code_index_maintenance_postgres.py:59-120`; helpers imported from
  `tests.test_code_index_store_postgres`).

**What to do:**

- `SqlAlchemyReviewIndexAccess(session)` and `SqlAlchemyReviewIndexUnitOfWork` (suggested)
  implementing Task 3's port (plan, "Adapter and its transaction"):
  1. `repositories` `FOR SHARE`, then `RAG_ON` and `GITHUB_PROVIDER` in a statement of
     their own (`app/modules/code_index/infrastructure/maintenance.py:209-226`); false →
     `enabled=False`, the state is not read;
  2. the state row `FOR UPDATE SKIP LOCKED`
     (`app/modules/repositories/infrastructure/rules_status.py:228`); empty → a plain read:
     no row, or a locked row read-only with no writes;
  3. `clock_timestamp()` after the lock; Task 2's decision; the marker and the generation
     only through `request_build(target_commit_sha=None)` / `enable()`; `last_used_at` and
     the throttle columns by the adapter's own UPDATE with that time;
  4. return `rag_index(...)` over the row after the writes and
     `default_head_sha = target_commit_sha`.
- The `CodeIndexStore` port and `_PORT_METHODS` do not change.

**Tests to add (PG; set `rag_enabled = true` on the repositories, since `_repository`
leaves it false):**

- `stale` → marker set, target unchanged, the review returns without waiting;
- `ttl` → generation + 1, `empty`, marker, throttle recorded;
- a repeat within 24 h queues no full; the same target after 24 h is refused, a new target
  is allowed;
- `failed`, `removed`, `disabled` and no row → no writes;
- the extension at most once an hour;
- AC13: `a` and `b` with `last_used_at` 31 days ago
  (`tests/test_code_index_store_postgres.py:2395`); a review reads `a` → the TTL purge
  (`purge_unused`) purges only `b`; an incremental publication does not change
  `last_used_at`;
- the race with toggle off (as `tests/test_code_index_maintenance_postgres.py:536`): no
  resurrection;
- SKIP LOCKED: another session holds the row (holder pattern of
  `tests/test_code_index_store_postgres.py:2473`) → the call returns at once, read-only,
  no writes;
- the toggle off or a GitLab installation → `enabled=False` and no state read;
- isolation: a read of `a` never writes `b`.

**Done when:**

- [x] every scenario above is a PG test;
- [x] the RED run and the GREEN run are recorded;
- [x] common verification is green with zero skips.

**Dependencies:** Tasks 1, 2, 3.

### Task 5: `context.rag` readiness in the context stage ⚑

**Commit:** `feat(reviews): record context.rag readiness in the context stage (#144)`

**Files:**

- `app/modules/reviews/application/review_context_stage.py` (constructor `:190-215`,
  `run` `:272-374`);
- `tests/test_review_context_stage.py`;
- `tests/test_openapi_contract.py` (`RUN_ACTION_TOOLS`, `:112`);
- `contracts/openapi.yaml` (the `RunStatus` description `:914` and the `RunAction.tool`
  description `:1325`).

**What to do:**

- An optional constructor argument `rag=None` (Task 3's use case).
- The gate runs after the skip exits (`:350`) and before `context.build` (`:352`), with the
  except chain of `:363-371`; its result or its error is kept.
- Record `context.rag` once per attempt whenever the gate ran, right after the
  `context.build` try/except (`:353-371`), on both paths — success and the caught build
  failure (`return None`, `:371`) — and before the `ALL_CHANGES_IGNORED` skip (`:372`)
  (decided by the coordinator, 23.10; M8). Use `RunTrace.record` (plan, "Trace `context.rag`"): no step when disabled; `not_ready` with the reason from
  `RagIndex.not_ready_reason`, `error_code` only for `failed`; a usable index →
  `not_ready` / `prompt_version` (M6); a failed gate → `{error: {type, message}}`.
- Request `{index_status, indexed_commit_sha, default_head_sha, stale}`.
- The tool name is a literal or a `*_TOOL` constant, so
  `tests/test_openapi_contract.py:1334-1339` finds it; add `context.rag` to the set and to
  both descriptions.

**Tests to add (unit, fake gate):**

- `rag=None` and toggle off → `["policy.load", "context.build"]` (as
  `tests/test_review_context_stage.py:239`);
- `context.rag` for `empty`, `building`, `failed` (with its code), `ready` and `stale`
  (`prompt_version`), written after `context.build`;
- a caught build failure → `context.rag` is still recorded after `context.build`; an
  `ALL_CHANGES_IGNORED` payload → `context.rag` is recorded before the skip; a re-raised
  `RunCancelled` from the build → no `context.rag`;
- a fake `RagIndex` with a patched `not_ready_reason` → that value is the step's `reason`;
- a failed gate → the step carries `{error}`, the run continues; `RunCancelled` re-raises;
- `stale` → the gate is called once and nothing waits.

**Done when:**

- [x] the run-action catalog tests and the openapi description tests are green;
- [x] the RED run and the GREEN run are recorded;
- [x] common verification is green.

**Dependencies:** Task 3.

### Checkpoint: after Tasks 1–5

- [ ] Gates green; the focused PG run has zero skips.
- [ ] The decision table, the adapter and the stage agree on every status.

### Task 6: Wire the RAG readiness gate

**Commit:** `feat(worker): wire the RAG readiness gate (#144)`

**Files:**

- `app/worker.py` (`review_context_stage_factory`, `:524-591`);
- the worker composition test that covers the factory (find it with
  `grep -n review_context_stage_factory tests/`).

**What to do:** pass `rag=OpenReviewIndex(...)` built on `SqlAlchemyReviewIndexUnitOfWork`
over the factory's `session_factory` into `ReviewContextStage(...)` (`:574-589`).

**Tests to add:** the factory builds a stage with the gate set.

**Done when:**

- [x] `tests/test_worker_queue_postgres.py` is green in the focused run;
- [x] the RED run and the GREEN run are recorded;
- [x] common verification is green.

**Dependencies:** Tasks 4, 5.

### Task 7: PR 1 documentation ⚑

**Commit:** `docs: review trigger, last_used_at on read, context.rag not_ready (#144)`

Before the commit, the **coordinator** (not the development subagent) re-checks the open
PRs and their files (plan, "Coordination").

**Files and edits:**

- `docs/SYSTEM_DESIGN.md`:
  - §10 `:1505` and `:1543-1545`: drop "until #144"; where the throttle lives (U3, M1);
  - §11 `:1594`: the two columns and the CHECK;
  - `:745-751`: the downgrade revision list gains the new revision (derived).
- `docs/PIPELINE_SPEC.md`: `:110` (written now, only with the toggle on, after
  `context.build`), `:123-128` (the attempt order), `:173` (`error_code?`, `{error}`,
  `default_head_sha` = the index target).
- `docs/CONTEXT_AND_VERIFICATION_SPEC.md` §3.5 `:420-426`: the `failed` code; the review
  requests an update without waiting.
- `docs/adr/0001-graph-rag-v1.md` §6.1 `:231`: a link to the SD place of the throttle.
- Derived (schema docs, as #143 did): `docs/BACKEND_ARCHITECTURE.md:512-520`,
  `docs/erd/erd.mmd:166-182`.

**Tests to add:** none.

**Done when:**

- [x] every listed anchor is edited;
- [ ] the coordination re-check is done;
- [x] common verification is green.

**Dependencies:** Task 6.

### Checkpoint and publication: PR 1

- [ ] One Alembic head; upgrade → downgrade → upgrade on a clean database.
- [ ] Gates green; the focused PG run has zero skips.
- [ ] Clean `code-reviewer` review (only `git show` / `git diff`, no checkout); check the
  branch afterwards; check the advice against `.agents/rules/backend.md` before the push.
- [ ] `/verify-run`.
- [ ] PR body What / Why / How to verify / `Refs #144`, no closing keyword; deviations U4,
  M1–M10 and the derived ones (plan, "Deviations"); the ui follow-up (map `context.rag` to
  CONTEXT); reviewer ndovnar; #144 → On Review.
- [ ] Manual e2e after PR 1 (plan, "Verification", steps 1–4).

## PR 2: `feat(reviews): add graph candidates to the review context`

### Task 1: Resolve one import binding without the per-run caps

**Commit:** `feat(reviews): resolve one import binding without the per-run caps (#144)`

**Files:**

- `app/modules/reviews/application/ast_resolution.py` (`ImportResolver`, `:499-583`);
- `tests/test_ast_resolution.py` (fakes `:53-101`).

**What to do (M9):** a new public `ImportResolver.resolve_binding(...)` (suggested name)
that resolves one binding without the 10/100 symbol caps (`:36-37`); `resolve()` calls it
inside, so L4 behaviour does not change.

**Tests to add:**

- `resolve_binding` past 100 symbols still resolves;
- shadowing (`local_bindings`) → `ambiguous` / `shadowed`; the same name in another module
  → not resolved to it; `repository.save()` is not a binding;
- the existing `resolve()` tests stay green unchanged.

**Done when:**

- [ ] the RED run and the GREEN run are recorded;
- [ ] common verification is green.

**Dependencies:** PR 1 opened.

### Task 2: Graph hints in one repeatable-read transaction ⚑

**Commit:** `feat(code_index): read graph hints in one repeatable-read transaction (#144)`

**Files:**

- new `app/modules/reviews/application/graph_retriever.py` (the `GraphRetriever` port, ADR §5);
- new `app/modules/code_index/infrastructure/graph_retriever.py` (adapter and the new
  `SqlAlchemyCodeIndexSnapshotUnitOfWork`, suggested);
- new `tests/test_rag_graph_retriever_postgres.py`.

**What to do:**

- One transaction whose first statement is `SET TRANSACTION ISOLATION LEVEL REPEATABLE
  READ, READ ONLY`: `get_state` plus `incoming_edges` per target (≤ ~200)
  (`app/modules/code_index/infrastructure/index_store.py:1180-1188`). It closes before any
  blob read.
- Queries only by the run's `repository_id`.
- No READY row in this read → the fresh status for `not_ready`, no hints.
- Return the `indexed_commit_sha` read here.

**Tests to add (PG):** isolation `a` / `b` with the same paths; a concurrent publication
does not change the read; `SHOW transaction_isolation` = `repeatable read` and
`transaction_read_only` = `on` inside; an unready or purged index → no hints.

**Done when:**

- [ ] the RED run and the GREEN run are recorded;
- [ ] common verification is green with zero skips.

**Dependencies:** Task 1.

### Task 3: RAG blob reads through the blob cache

**Commit:** `feat(reviews): read RAG blobs through the blob cache (#144)`

**Files:**

- new `app/modules/reviews/application/rag_context.py` (the cached reader; suggested);
- new `tests/test_rag_context.py`.

**What to do (M10):**

- Read a candidate blob through `BlobCache.get`
  (`app/modules/reviews/infrastructure/blob_cache.py:89-104`); on a miss, `read_blob` from
  the PR revision source, the same size, binary and UTF-8 checks as `_Files.text`
  (`app/modules/reviews/application/review_context.py:208-242`), then `put` in a short UoW
  of its own (`app/modules/reviews/application/process_run.py:349-354`).
- Every distinct blob counts toward the 20-read limit on a hit and on a miss.
- A failed `put` is logged; the run goes on.
- No DB transaction spans a GitHub read.

**Tests to add:** a hit does not call `read_blob`; a miss reads once and puts; both count;
the 21st distinct blob → `rag_limit`; a failing `put` → logged, the text is returned.

**Done when:**

- [ ] the RED run and the GREEN run are recorded;
- [ ] common verification is green.

**Dependencies:** Task 1.

### Task 4: Select and confirm reverse-edge candidates ⚑

**Commit:** `feat(reviews): select and confirm reverse-edge candidates (#144)`

**Files:** `app/modules/reviews/application/rag_context.py`, `tests/test_rag_context.py`
(fakes copied from `tests/test_review_context.py:83-227` with several revisions).

**What to do (plan, "Targets", "Filters", "Confirmation at head"):**

- Targets from `affected_symbols` walked up to the top-level symbol; exported names; a
  module-level change → `(path, "")`; the graph path `old_path or new_path`.
- Filters in order: dedupe by importer → PR files out silently → `denied(path)` before any
  read (only the `excluded` counter) → L4 `shown` symbols out. Priority and limits per ADR
  §9; over a limit → `rag_limit` with the path.
- Confirmation at head with its own `_Files` and `ImportResolver` (Task 1), reusing what L4
  loaded, without the 20-file cap. Text and `blob_sha` only from the PR tree.
- Places: an inner declaration that reads the alias without rebinding plus the import line;
  whole if ≤200 lines and ≤2 000 tokens, else the signature.
- Outcomes: `rag_candidate_missing`, `rag_edge_unconfirmed`, `source_read_failed`.

**Tests to add:**

- a reverse edge is used; a PR file and an L4 symbol are not duplicated;
- an ignored candidate is never read (assert on `Repository.read`) and only counted;
- missing; read failure; the 11th file and the 4th place → `rag_limit`;
- F6: the same name in another module, shadowing by a parameter, local rebinding,
  `repository.save()` → no candidate;
- F5: the graph points at `new_utils`, head imports `old_utils` → `rag_edge_unconfirmed`;
  a signature moved inside the file → the ranges are the head lines, as literals;
- isolation: two repositories with the same paths; the blocks carry the run's
  `repository_id`.

**Done when:**

- [ ] the RED run and the GREEN run are recorded;
- [ ] common verification is green.

**Dependencies:** Tasks 2, 3.

### Task 5: The RAG slot and retrieved blocks in the payload ⚑

**Commit:** `feat(reviews): add the RAG slot and retrieved blocks to the payload (#144)`

**Files:**

- `app/modules/reviews/application/review_context.py` (`ContextLevel` `:84-88`,
  `ReviewContextPayload` `:131-145`, `execute` `:310-546`);
- `app/modules/reviews/application/review_context_stage.py` (`context_summary`, `:128-175`);
- `tests/test_rag_context.py`, `tests/test_review_context.py`.

**What to do (plan, "Where", "Payload"):**

- An optional `rag: RagPlan | None` on `execute`; the RAG part runs after L4 and before
  the `finally` (`:506`).
- `retrieved: tuple[RetrievedBlock, ...]` and `rag: RagStats | None` on the payload;
  `ContextLevel.RAG`; role `retrieved_excerpt`; `selection_reason`, the edge kind and
  `index_commit_sha`; the block revision is the PR side.
- The slot is `min(4000, remaining)` through the budget of `take()` (`:397-403`); the blocks
  go to `retrieved`; they count in `tokens_used`, `levels_present` and `tokens_by_role`.
- M8: an early return (`:382-391`, `:429`, `:442`) with a plan → `rag` stats `used` with the
  reason and zero counters.

**Tests to add:** 4 000 tokens cap; L1 and L4 intact while L3 → `budget`; the counters and
`tokens_by_role` include `retrieved_excerpt`; each early return gives zero counters with
its reason; without a plan the payload is unchanged.

**Done when:**

- [ ] the RED run and the GREEN run are recorded;
- [ ] common verification is green.

**Dependencies:** Task 4.

### Checkpoint: after Tasks 1–5

- [ ] Gates green; the focused PG run has zero skips.
- [ ] A payload with a plan holds only PR-revision blocks.

### Task 6: Broken-reference candidates ⚑

**Commit:** `feat(reviews): add broken-reference candidates (#144)`

**Files:** `app/modules/reviews/application/rag_context.py`,
`app/modules/reviews/application/review_context.py`, `tests/test_rag_context.py`.

**What to do (M7; plan, "Broken reference"; ADR §8.3):**

- The LEFT tree and the facts of changed, deleted and renamed files, read before the graph
  transaction; their reads count in the slot.
- The importer is a candidate when the import resolves at `diff_old_sha` into a module
  exporting the name, and otherwise at head.
- Two blocks: RIGHT `exact_lines` at head, LEFT `signature` at `diff_old_sha`; highest
  priority in the slot.
- No `diff_old_sha` → `old_revision_unknown`.

**Tests to add:** rename and delete; an import broken before the PR is not blamed; one fixed
at head → no candidate; no `diff_old_sha` → `old_revision_unknown`; the two blocks carry
one revision each.

**Done when:**

- [ ] the RED run and the GREEN run are recorded;
- [ ] common verification is green.

**Dependencies:** Task 5.

### Task 7: Record `context.rag` usage and wire the retriever

**Commit:** `feat(reviews): record context.rag usage and wire the retriever (#144)`

**Files:**

- `app/modules/reviews/application/review_context_stage.py`;
- `app/worker.py` (`context_builder`, `:537-555`);
- `tests/test_review_context_stage.py`.

**What to do:**

- The stage passes `RagPlan(index_commit_sha, stale)` only for a usable index and a pinned
  prompt version in the capability set (M6).
- `context.rag` `used`: `{outcome, candidates: {found, confirmed, dropped: {…}}, tokens}`
  with `found = confirmed + Σdropped`, no paths.
- The RR read found no READY row → `not_ready` with the fresh status.
- Open, decided here (next to M8): the shape of `context.rag` after a caught build failure,
  when the RAG part did not complete. Record the choice in the PR body.
- The factory builds the retriever and the cached blob reader for each run.

**Tests to add:** a capability set patched to contain the pinned version → `used` with
counters; an empty set → `not_ready` / `prompt_version` and no graph read; the gate ↔ graph
race → `not_ready` with the fresh status.

**Done when:**

- [ ] the RED run and the GREEN run are recorded;
- [ ] common verification is green.

**Dependencies:** Task 6.

### Task 8: PR 2 documentation

**Commit:** `docs: RAG retrieval contract (#144)`

Before the commit, the coordinator re-checks the open PRs and their files.

**Files and edits:**

- `docs/PIPELINE_SPEC.md:110`, `:173`: `used`, the skip reasons, `old_revision_unknown`
  in the `dropped` keys (decided by the coordinator, 23.10);
- `docs/CONTEXT_AND_VERIFICATION_SPEC.md` §3.5;
- `docs/SYSTEM_DESIGN.md:1457`, `:1470`;
- `docs/adr/0001-graph-rag-v1.md` §2 `:70`: the shape of a `retrieved` item.

**Tests to add:** none.

**Done when:**

- [ ] every listed anchor is edited;
- [ ] common verification is green.

**Dependencies:** Task 7.

### Checkpoint and publication: PR 2

- [ ] Gates green; the focused PG run has zero skips.
- [ ] Clean `code-reviewer` review; check the branch; check the advice against
  `.agents/rules/backend.md`; `/verify-run`.
- [ ] PR body What / Why / How to verify / `Refs #144`, no closing keyword; deviations M7,
  M8, M9, M10, `old_revision_unknown`; reviewer ndovnar.
- [ ] After PR 1 merges, rebase PR 2 on `main`.

## External: after the merges

- [ ] Post the #130 comment naming the v4 owner (U2), once the user decides (pending;
  PR 3 stays blocked on it).
- [ ] Status in #144 and #139; board 12 (#144 back to In Progress after PR 1).
- [ ] The ui one-line follow-up: map `context.rag` to CONTEXT in `TOOL_STAGE`.
- [ ] Lab paperwork: `docs/reports/144/`, the mirror, `economics.md`, `/verify-run`.
- [ ] Plan PR 3 in its own session after the #130 remainder (v4) and the merge of #230.
- [ ] Clean up the worktree, the database `dmc_144` and the vhost `dmc_144`.
