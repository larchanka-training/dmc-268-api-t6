# Task list: RAG index lifecycle, update triggers and purge (#143)

See [143-plan.md](143-plan.md) for the design. That plan is the source of truth; this list
adds no scope. AC1–AC9 are the numbered issue acceptance criteria in the plan's table;
U1–U4 and M1–M11 are the decisions named there. Baseline `origin/main` `29ac426`, Alembic
head `20261018_0035`. Two stacked PRs:

- PR 1, branch `feat/143-rag-index-purge` from `origin/main`: Task 0, then Tasks 1–9;
- PR 2, branch `feat/143-rag-index-triggers` on PR 1: Tasks 1–11.

Each task is one Conventional commit whose subject ends with `(#143)`. ⚑ marks a task with
more than 10 edits across the files it touches. Every task that writes code is delegated
to a subagent through a brief file (`standards/workflow.md` §5.1). The coordinator keeps
coordination, verification and the commands. **Every brief says: never read `.env` of any
clone.**

Every task is test-first: write the named tests, watch them fail, then write the code.
Pure refactors and docs tasks instead keep the existing tests green.

Dependency order: PR 1 Task 0 → 1 → … → 9; PR 2 Task 1 → … → 11. PR 2 starts after PR 1
is opened. Each task lists its direct dependencies.

**Common verification.** Every task that adds a commit runs these before it is marked done:

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy . --exclude '^docs/reports/'
uv run pytest
TEST_DATABASE_URL=postgresql+psycopg://app:app@127.0.0.1:5432/dmc_143 \
TEST_RABBITMQ_URL=amqp://app:app@127.0.0.1:5672/dmc_143 \
  uv run pytest -m integration -rs tests/test_code_index_*_postgres.py \
  tests/test_repository_rag_postgres.py tests/test_worker_queue_postgres.py \
  tests/test_initial_migration.py
```

- The focused integration run must report **zero skips**. A skipped PostgreSQL or
  RabbitMQ test never counts as passing.
- `tests/test_alembic_heads.py` and `tests/test_installation_repository_sync.py` carry no
  `integration` marker, so `-m integration` deselects them. They run in the plain
  `uv run pytest` gate.
- If the removal PG tests (PR 1 Task 7) go into `tests/test_portal_scope_postgres.py`, add
  that file to the focused run.
- Use only the dedicated database `dmc_143` and the vhost `dmc_143`, never another
  session's: parallel sessions pick up each other's NOTIFY traffic. The vhost must exist
  with permissions for `app` (recipe in `docs/TEST_PLAN.md:44`). Do not print credentials.
- `df /tmp` before long runs.

## PR 1: `feat(code_index): purge the index on disable, removal and TTL`

### Task 0: Plan documents

**Commit:** `docs(plans): plan the rag index lifecycle (#143)`

**Files:** `docs/plans/143-plan.md`, `docs/plans/143-todo.md`.

**What to do:** commit the English plan and task list translated from the approved plan.

**Tests to add:** none.

**Done when:**

- [x] both files translate the approved plan for both PRs: U1–U4, M1–M11, the PR designs,
  the AC1–AC9 table, the deviations and the verification;
- [x] no new decision, column, method or scope is added;
- [x] only the two files are committed; the gates are unaffected.

**Dependencies:** none.

### Task 1: Shared garbage predicate

**Commit:** `refactor(code_index): share the garbage predicate (#143)`

**Files:** `app/modules/code_index/infrastructure/index_store.py`.

**What to do:**

- Move the inner `garbage(model)` of `collect_garbage` (`app/modules/code_index/infrastructure/index_store.py:854-868`) into a
  module-level function over SQL expressions (plan, "Store"; suggested name and signature
  `garbage_condition(model, *, generation, published, floor, leased, displaced_due)`).
- `collect_garbage` keeps reading the state under `FOR SHARE` (`:842-852`) and passes the
  values as bound literals. A NULL `leased` spares nothing:
  `or_(leased.is_(None), model.valid_from != leased)`.
- No behaviour change.

**Tests to add:** none. The existing GC tests in `tests/test_code_index_store_postgres.py`
stay green.

**Done when:**

- [x] the predicate is one module-level function that `collect_garbage` calls;
- [x] the full suite and the focused PG run are green with zero skips.

**Dependencies:** Task 0.

### Task 2: TTL purge with a re-check

**Commit:** `feat(code_index): purge an unused index after the ttl (#143)`

**Files:**

- `app/modules/code_index/application/index_store.py` (port method and docstring);
- `app/modules/code_index/infrastructure/index_store.py`;
- `tests/test_code_index_store_contract.py` (`_PORT_METHODS`, the comment at `:49`);
- `tests/test_code_index_store_postgres.py`.

**What to do:**

- Add `purge_unused(repository_id: UUID, unused_for: timedelta) -> bool` to `CodeIndexStore`
  and `SqlAlchemyCodeIndexStore`.
- It takes the state row `FOR UPDATE`, reads `clock_timestamp()`, and purges with
  `PurgeReason.TTL` only if `purge_reason IS NULL AND status <> 'failed' AND last_used_at <
  clock_timestamp() - unused_for` (U1). It returns whether it purged.
- Rewrite the comment at `tests/test_code_index_store_contract.py:49`: the TTL seam now
  exists and takes `repository_id`; the leader enumeration is the separate
  `CodeIndexMaintenance` port.

**Tests to add (PG, AC6):**

- `last_used_at` refreshed under a held lock (`_wait_until_blocked`, `:1062`) → `False`,
  generation unchanged;
- two parallel `purge_unused` runs → generation grows by exactly 1, and one of them
  returns `True`;
- a `failed` row, an already purged row and a fresh row → `False`, no change;
- an old live row → `True`, status `empty`, `purge_reason = 'ttl'`, `last_used_at` kept.

**Done when:**

- [x] the contract tests pass with the new method in `_PORT_METHODS`;
- [ ] the RED run and the GREEN run are recorded;
- [x] common verification is green.

**Dependencies:** Task 1.

### Task 3: TTL and GC enumeration

**Commit:** `feat(code_index): enumerate ttl and gc candidates (#143)`

**Files:**

- new `app/modules/code_index/application/maintenance.py` (port `CodeIndexMaintenance`);
- new `app/modules/code_index/infrastructure/maintenance.py` (adapter, suggested
  `SqlAlchemyCodeIndexMaintenance`);
- new `tests/test_code_index_maintenance_postgres.py` (suggested; the name matches the
  focused-run glob).

**What to do:**

- `unused(unused_for, after, limit)`: `purge_reason IS NULL AND status <> 'failed' AND
  last_used_at < clock_timestamp() - :unused_for AND repository_id > :after`, ordered by
  `repository_id`, limited.
- `with_garbage(after, limit)`: an expired lease, or `EXISTS` garbage rows in
  `code_index_files` via the Task 1 function with the correlated state columns (plan,
  "Enumeration").
- No `repository_id` parameter (M9). The `CodeIndexStore` contract is unchanged.

**Tests to add (PG):**

- an expired lease is selected;
- displaced rows (older than 10 min after a publication) are selected;
- rows under a live lease are not selected;
- no candidates ⇒ the raw row counts equal the visible counts;
- `unused` skips purged and `failed` rows and honours `after` and `limit`.

**Done when:**

- [x] both methods return ids ordered by `repository_id` after `after`;
- [x] common verification is green with zero skips.

**Dependencies:** Task 1.

### Task 4: TTL and GC leader tasks ⚑

**Commit:** `feat(code_index): add the ttl purge and gc leader tasks (#143)`

**Files:**

- new `app/modules/code_index/application/leader_tasks.py` (suggested);
- new `tests/test_code_index_leader_tasks.py` (suggested, unit).

**What to do:**

- `PurgeUnusedIndexes`: pages `unused(...)`; one short transaction per candidate
  (`purge_unused` + commit).
- `CollectIndexGarbage(batch_rows, budget_seconds, clock)`: an in-memory round-robin cursor
  over `with_garbage`. Per repository: commit `revoke_expired_lease` on its own, then
  `collect_garbage(repository_id, batch_rows)` batches, one transaction each, until a batch
  deletes fewer than `batch_rows` or the budget runs out.
- `CodeIndexLeaderTasks`: one monotonic gate per task and a shared deadline of about 10 s
  per tick as a module constant (M8; not an env knob). It passes the remaining
  time to each task. Each task runs in its own `try/except Exception` with a logged error;
  `tick()` never raises.

**Tests to add (unit):**

- gates on fake clocks: a task runs once its interval has passed, not before;
- isolation: one task raising does not stop the others, and `tick()` does not raise;
- budget: `CollectIndexGarbage` stops at the deadline; the cursor resumes after the last
  repository on the next run;
- a batch smaller than `batch_rows` moves on to the next repository;
- one transaction per store call (the `Tracker` pattern, `tests/test_build_repository_index.py:123`).

**Done when:**

- [x] the unit tests assert literal call sequences;
- [ ] the RED run and the GREEN run are recorded;
- [x] common verification is green.

**Dependencies:** Tasks 2, 3.

### Task 5: `leader_tick` in the worker

**Commit:** `feat(worker): run the index leader tasks in leader_tick (#143)`

**Files:**

- `app/worker.py` (`WorkerProcess` `:707-718`, `run_worker` `:805`);
- `app/bootstrap/code_index_worker.py` (`CodeIndexSettings` `:80-157`; a composition
  helper for the leader tasks is fine here);
- `tests/test_queue_messages.py`;
- `tests/test_code_index_worker.py`.

**What to do:**

- `CodeIndexSettings`: `ttl_days=30`, `ttl_interval_seconds=3600`,
  `gc_interval_seconds=300`, `gc_batch_rows=10_000` (`CODE_INDEX_TTL_DAYS`,
  `CODE_INDEX_TTL_INTERVAL_SECONDS`, `CODE_INDEX_GC_INTERVAL_SECONDS`,
  `CODE_INDEX_GC_BATCH_ROWS`); `__post_init__` refuses `gc_interval_seconds >= 3600`.
- `WorkerProcess.code_index: CodeIndexLeaderTasks | None = None`.
- `leader_tick`: run sweep and replay catching `Exception` only, then `code_index.tick()`, then re-raise the caught exception; a cancellation (`CancelledError`) skips the index tick, when `code_index` is set.
- `run_worker` builds `CodeIndexLeaderTasks` from `settings.code_index` and
  `session_factory` and hands it to the `WorkerProcess`.

**Tests to add (unit):**

- `test_worker_leader_tick_runs_the_sweep_then_the_outbox_replay_every_thirty_seconds`
  (`tests/test_queue_messages.py:613-642`) stays green unchanged;
- the index tick runs after replay;
- an index task that raises does not skip replay and does not raise out of `leader_tick`;
- settings: defaults, env parsing, `CODE_INDEX_GC_INTERVAL_SECONDS=3600` refused.

**Done when:**

- [x] `run_worker` wires the leader tasks;
- [x] common verification is green, including `tests/test_worker_queue_postgres.py`.

**Dependencies:** Task 4.

### Task 6: `CodeIndexPurger` and toggle off

**Commit:** `feat(repositories): purge the index when rag is turned off (#143)`

**Files:**

- new port module in `repositories.application` (suggested
  `app/modules/repositories/application/code_index.py`): `CodeIndexPurger.purge(repository_id:
  UUID, reason: PurgeReason) -> None`;
- new `app/modules/code_index/infrastructure/purger.py`: `SqlAlchemyCodeIndexPurger(session)`;
- `app/modules/repositories/application/repository_settings.py` (Protocol
  `RepositorySettingsUnitOfWork` `:60-62`, `UpdateRepository` `:86-96`);
- `app/modules/repositories/infrastructure/repository_settings.py`
  (`SqlAlchemyRepositorySettingsUnitOfWork` `:133-151`);
- `tests/test_repository_rag_postgres.py`.

**What to do:**

- The UoW exposes the purger as a property on its own session (suggested `index_purger`);
  the constructor does not change.
- `UpdateRepository.execute`: `update_repository` → if `change.rag_enabled is False`,
  `purge(PurgeReason.DISABLED)` → `get_repository` again → commit (M6). A body without
  `ragEnabled` purges nothing.

**Tests to add (PG, AC4, AC7):**

- PATCH `ragEnabled=false` on an enabled repository with a published index → state `empty`,
  `purge_reason = 'disabled'`, generation + 1; after `CollectIndexGarbage` no graph rows
  remain (M1);
- a repeated PATCH false changes nothing (same generation);
- an unavailable repository is purged too;
- a PATCH that changes only `enabled` or `branchFilter` leaves the state row unchanged;
- the response is the re-read after the purge.

**Done when:**

- [ ] the RED run and the GREEN run are recorded;
- [x] common verification is green.

**Dependencies:** Task 5 (GC for the "no rows" check).

### Task 7: Purge on `removed` and `deleted` ⚑

Marked ⚑ by this brief: the source plan did not mark it, but the Protocols and fakes it
must touch take it past 10 edits.

**Commit:** `feat(repositories): purge the index on repository removal and installation deletion (#143)`

**Files:**

- `app/modules/repositories/application/sync_installation_repositories.py` (Protocol return
  types `:49-53`, UoW Protocol `:64-68`, `disable` `:149-174`);
- `app/modules/repositories/infrastructure/installation_repository_unit_of_work.py`
  (`disable_repository` / `disable_installation` `:168-209`, UoW property `:250-258`);
- `tests/test_installation_repository_sync.py` (fakes `:44`, `:80-83`, `:115`; the
  `RETURNING` unit test `:373` with `FakeDisableSession` `:350`);
- a PG test file: the new `tests/test_code_index_lifecycle_postgres.py`, or next to the
  removal tests in `tests/test_portal_scope_postgres.py:334-396`.

**What to do (M7):**

- `disable_repository` → `UUID | None`, `disable_installation` → `list[UUID]`, both with
  `RETURNING Repository.id`.
- `disable`: after `record_removal_delivery`, in the same transaction, `purge(PurgeReason.REMOVED)`
  for `sorted(ids)`. Lock order: `repositories` → state.
- The installation UoW exposes the same purger property.

**Tests to add:**

- PG (AC5, AC7): `removed` purges the index of the named repository; `deleted` purges the
  index of every repository of the installation; the `repositories` rows stay; a repeated
  delivery changes nothing; after `CollectIndexGarbage` no graph rows remain;
- unit: the fakes return the new types; `disable` purges in sorted id order after the
  delivery marker.

**Done when:**

- [ ] the RED run and the GREEN run are recorded;
- [x] common verification is green.

**Dependencies:** Task 6.

### Task 8: AC8 race tests ⚑

**Commit:** `test(code_index): cover purge races with a running job (#143)`

**Files:** `tests/test_code_index_lifecycle_postgres.py` (new, or extended from Task 7).

**What to do:** reuse the harness of `tests/test_code_index_indexer_postgres.py` (`Vcs`
`:286`, `_ActOnParse` `:1097`, `_raw_rows` `:906`). Import it, or extract it to a shared test
helper in this commit if importing a test module does not work. Every scenario ends with
`CollectIndexGarbage` and `_raw_rows == (0, 0, 0, 0)`.

**Tests to add (PG, AC8, three of four scenarios):**

- purge during the download: `Vcs.archive()` (`:315`) waits for a real trigger
  (`UpdateRepository` with `ragEnabled=false`, or `SyncInstallationRepositories.disable`) →
  the job ends `LEASE_LOST`;
- purge during the write: `_ActOnParse(call=2)` with `batch_files=1` → `LEASE_LOST`, no write
  after the purge;
- an old message after the purge → `STALE`.

**Done when:**

- [x] all three scenarios assert literal outcomes and zero raw rows;
- [x] common verification is green with zero skips.

**Dependencies:** Tasks 6, 7.

### Task 9: PR 1 documentation ⚑

**Commit:** `docs: document the index purge, ttl and gc (#143)`

Before the commit, the **coordinator** (not the development subagent) re-checks the open
PRs and their files and posts the coordination comments in #220 and #221 (plan,
"Coordination").

**Files and edits:**

- `docs/SYSTEM_DESIGN.md`:
  - §6.9 `:588-590`: the index is not carried over a reinstall, and `deleted` purges it (the
    ADR wording, comment in #143 of 16.10);
  - `:662-664`: turning the toggle off purges;
  - §10 `:1412`, `:1437-1443`: the four new variables and U1 (any live index, `failed`
    excluded);
  - `:1515`: the leader tasks exist.
- `docs/adr/0001-graph-rag-v1.md` §4, §6.
- `docs/BACKEND_ARCHITECTURE.md:462-472`: writes go through `CodeIndexPurger`; the read is
  the exception (rewrite `:467-471`); name the UoW constructing the `code_index` adapter.
- `docs/WEBHOOK_WORKER.md:263`: `deleted` and `removed` purge the index.
- `.env.example:86-109`: the four `CODE_INDEX_*` lines.

**Tests to add:** none.

**Done when:**

- [x] every listed anchor is edited;
- [ ] the coordination comments are posted;
- [x] common verification is green.

**Dependencies:** Task 8.

### Checkpoint and publication: PR 1

- [ ] Gates green; the focused PG run has zero skips.
- [ ] Clean `code-reviewer` review (only `git show`/`git diff`, no checkout); check the
  branch afterwards; check the advice against `.agents/rules/backend.md` before the push.
- [ ] `/verify-run`.
- [ ] PR body What / Why / How to verify / `Refs #143`, no closing keyword; deviations U1,
  `failed` excluded from TTL, M1, M6 (read side), M10; reviewer ndovnar.

## PR 2: `feat(code_index): request index updates on toggle, merge, push, drift`

### Task 1: Migration

**Commit:** `feat(code_index): add the index wake-up marker (#143)`

**Files:**

- new `alembic/versions/<next free id>_code_index_wake_marker.py` (suggested slug): the next
  free number after `main`'s head at commit time, agreed in #220/#221 first;
- `app/modules/code_index/infrastructure/models.py` (`__table_args__` `:40-52,72`);
- `docs/CICD.md` (§3 table, new top row above `:239`; the revision list `:255`);
- `docs/SYSTEM_DESIGN.md:731`;
- `docs/erd/erd.mmd` (`CODE_INDEX_STATES` `:166`);
- `docs/BACKEND_ARCHITECTURE.md:490-496`.

**What to do:**

- Column `wake_requested_at timestamptz NULL`.
- CHECK `ck_code_index_states_wake_live`:
  `wake_requested_at IS NULL OR (purge_reason IS NULL AND status <> 'failed')`.
- Partial index `WHERE wake_requested_at IS NOT NULL` (suggested
  `ix_code_index_states_wake_requested`).
- Partial index `code_index_files (repository_id) WHERE valid_to IS NOT NULL` (suggested
  `ix_code_index_files_closed`) for the GC probe of `with_garbage` (PR 1 review round 1,
  S3).
- Downgrade drops the indexes, the CHECK and the column. The same objects go into the ORM
  model.

**Tests to add (PG):** the CHECK refuses a marker on a purged row and on a `failed` row.

**Done when:**

- [ ] `uv run alembic heads` shows one head;
- [ ] upgrade → downgrade → upgrade on a clean database;
- [ ] `tests/test_initial_migration.py` and `tests/test_alembic_heads.py` are green;
- [ ] common verification is green.

**Dependencies:** PR 1 opened.

### Task 2: The marker in the store ⚑

**Commit:** `feat(code_index): request index builds through the wake-up marker (#143)`

**Files:**

- `app/modules/code_index/application/index_store.py`;
- `app/modules/code_index/infrastructure/index_store.py`;
- `tests/test_code_index_store_contract.py` (`_PORT_METHODS`);
- `tests/test_code_index_store_postgres.py`.

**What to do:**

- `purge` and `_failure()` (`:112-134`) clear `wake_requested_at`. `_failure()` serves
  `mark_failed` (`:782`) and QUOTA in `claim_lease` (`:477`).
- `enable()` (`:384-405`): creation and re-arm set `wake_requested_at = clock_timestamp()`
  together with `last_used_at`; a live row and `failed` stay a no-op.
- `request_build(repository_id, *, target_commit_sha: str | None) -> bool`.
- `reconcile_head(repository_id, *, generation, branch, commit_sha, versions_current) -> bool`.
- `clear_wake_request(repository_id, requested_at) -> bool` (CAS).
- Semantics: plan, PR 2 "Store". Do not add the marker to `IndexState`.

**Tests to add (PG):**

- `purge`, `mark_failed` and a QUOTA claim clear the marker; the QUOTA claim no longer
  trips the CHECK;
- `enable()` sets the marker on creation and on re-arm (`ttl` included), not on a live or
  `failed` row;
- `request_build`: writes the target when given, always refreshes the marker, `False` on a
  missing, purged or `failed` row;
- `reconcile_head`: the target is always the live SHA; marker on a branch change, a SHA
  change, NULL indexed, or outdated versions; no marker when equal; `False` on another
  generation;
- `clear_wake_request`: clears on a matching time; misses after a newer request.

**Done when:**

- [ ] the contract tests list the three new methods;
- [ ] the RED run and the GREEN run are recorded;
- [ ] common verification is green.

**Dependencies:** Task 1.

### Task 3: Wake-up, head and re-arm enumeration

**Commit:** `feat(code_index): enumerate wake-ups, heads and re-arm candidates (#143)`

**Files:**

- `app/modules/code_index/application/maintenance.py`;
- `app/modules/code_index/infrastructure/maintenance.py`;
- `tests/test_code_index_maintenance_postgres.py`.

**What to do:**

- `wake_requests(limit)`: marked rows without a live lease, oldest marker first, with
  `repository_id`, `generation`, `wake_requested_at`;
- `heads(after, limit)`: the M4 candidates joined through `repositories` and
  `provider_installations`, each with `IndexedRepository`, `generation` and the profile;
- `to_rearm(limit)`: the M5 condition.
- `to_disarm(limit)` (PR 1 review round 1, F12): live state rows (`purge_reason IS NULL`)
  whose effective RAG is off, each with the purge reason: `rag_enabled` false → `disabled`;
  `installation_available` false or the installation deleted → `removed`. It catches rows
  left live by removals or toggles that predate PR 1.

**Tests to add (PG):**

- `wake_requests` skips a row under a live lease and returns it after the lease expires;
- `heads` excludes: `rag_enabled` false, unavailable, a deleted installation, a
  non-`github` provider, purged, `failed`; it honours `after` and `limit`;
- `to_rearm` includes a missing row and `removed`/`disabled`, and excludes `ttl`, a live
  row, `rag_enabled` false, an unavailable repository, a deleted installation and a
  non-`github` provider.
- `to_disarm` returns a live row of a repository with `rag_enabled` false (`disabled`), an
  unavailable one and a deleted installation (`removed`); it skips purged rows and rows of
  repositories with effective RAG on.

**Done when:**

- [ ] common verification is green with zero skips.

**Dependencies:** Task 2.

### Task 4: Dispatcher and re-arm

**Commit:** `feat(code_index): dispatch index wake-ups and re-arm indexes (#143)`

**Files:**

- `app/modules/code_index/application/leader_tasks.py`;
- the maintenance port/adapter if the `FOR SHARE` re-check of M5 lives there;
- `tests/test_code_index_leader_tasks.py`;
- a PG test for the `FOR SHARE` re-check in `tests/test_code_index_maintenance_postgres.py`.

**What to do:**

- `RearmIndexes` (at most every 60 s): per candidate one short transaction, re-check M5
  with the `repositories` row locked `FOR SHARE`, then `enable()`, commit. The re-check and
  `enable()` share one session.
- The same pass disarms: per `to_disarm` candidate one short transaction, re-check under
  the same `FOR SHARE` lock, then `purge(reason)`, commit. Re-arm and disarm are one leader
  task (suggested name `ConvergeIndexes`).
- `DispatchIndexWakeups` (every tick): rows under a live lease keep their marker; `wake()`
  outside any transaction, then `clear_wake_request` in a short transaction; a failed
  `wake()` stops the pass and the markers stay.
- Both join `CodeIndexLeaderTasks` and the shared tick budget. The order within a tick is
  converge, dispatch, GC, TTL, reconcile: GC runs before TTL, because physical deletion has
  the one-hour target and the 30-day TTL tolerates delay.

**Tests to add:**

- unit: dispatch wakes, then clears with the read `wake_requested_at`; a failing `wake()`
  stops the pass with no clear; no transaction is open during `wake()`;
- unit: re-arm calls `enable()` once per candidate in its own transaction;
- PG: a re-arm racing a PATCH `ragEnabled=false` does not resurrect the index.
- PG: a live index of a repository whose RAG is off or whose installation is gone is purged
  by the pass with the right reason, and GC then leaves no rows.

**Done when:**

- [ ] common verification is green.

**Dependencies:** Task 3.

### Task 5: Reconcile with HEAD ⚑

**Commit:** `feat(code_index): reconcile indexes with the default-branch head (#143)`

**Files:**

- `app/modules/code_index/application/leader_tasks.py`;
- `app/bootstrap/code_index_worker.py` (`CodeIndexSettings`; `snapshot_sources` `:187-215`);
- `tests/test_code_index_leader_tasks.py`;
- `tests/test_code_index_worker.py`.

**What to do:**

- Settings `reconcile_interval_seconds=600` and `reconcile_batch=50`
  (`CODE_INDEX_RECONCILE_INTERVAL_SECONDS`, `CODE_INDEX_RECONCILE_BATCH`).
- `snapshot_sources` gets a `cache` parameter. When set, the sources share it (one cache per
  reconcile run). Unset, each job keeps its own `InMemoryInstallationAccessTokenCache`.
- `ReconcileIndexHeads`: a cursor over `heads`; `head()` outside any transaction;
  `reconcile_head(..., versions_current=profile is not None and versions.built(profile))`
  in a short transaction; a rate-limited `VcsUnavailable` skips the installation until
  `retry_after`; `VcsAccessDenied` or `None` skips the repository; within the tick budget.

**Tests to add (unit):**

- the gate opens every `reconcile_interval_seconds`;
- one token cache per run, shared by its sources;
- rate limit → the other repositories of that installation are skipped in this run; another
  installation goes on;
- access denied and `None` → skipped, no store write;
- no transaction is open during `head()`;
- the cursor resumes after `reconcile_batch` repositories; settings defaults and env.

**Done when:**

- [ ] the RED run and the GREEN run are recorded;
- [ ] common verification is green.

**Dependencies:** Task 4.

### Task 6: Worker wiring

**Commit:** `feat(worker): wire index wake-ups and the head reconcile (#143)`

**Files:**

- `app/worker.py` (`:862-885`);
- `app/bootstrap/code_index_worker.py` (`IndexConsumer`, `index_consumer` `:278-333`);
- `tests/test_queue_messages.py` and/or `tests/test_code_index_worker.py`.

**What to do:**

- Move `index_consumer` before `compose_worker_process`.
- `IndexConsumer` exposes `wakeups` and the source factory. The dispatcher takes
  `wakeups`; the reconcile takes the factory.
- Without the GitHub App, `index_consumer` yields `None` (`:291-297`): the reconcile and the
  dispatcher are off, and converge, GC and TTL keep running.

**Tests to add:**

- `run_worker` wires all five leader tasks with the GitHub App, in the order converge,
  dispatch, GC, TTL, reconcile, and only converge, GC and TTL without it;
- `test_worker_without_github_app_starts_and_stays_healthy`
  (`tests/test_worker_queue_postgres.py:908`) stays green.

**Done when:**

- [ ] common verification is green, including `tests/test_worker_queue_postgres.py`.

**Dependencies:** Task 5.

### Task 7: Toggle on

**Commit:** `feat(repositories): enable the index when rag is turned on (#143)`

**Files:**

- the `repositories.application` port module (add `CodeIndexEnabler.enable(repository_id:
  UUID) -> None`);
- `app/modules/code_index/infrastructure/purger.py` (implements it on the caller's session);
- `app/modules/repositories/application/repository_settings.py`;
- `app/modules/repositories/infrastructure/repository_settings.py`;
- `tests/test_repository_rag_postgres.py`.

**What to do (M6):** `UpdateRepository` calls `enable()` when `change.rag_enabled is True` and
the re-read shows `rag_enabled` (already masked by `installation_available`,
`app/modules/repositories/infrastructure/repository_settings.py:41`), then reads again and
commits. A live row and `failed` stay a no-op; a purged row (`ttl` included) is re-armed.

**Tests to add (PG, AC4):**

- invert `test_settings_api_patch_stores_the_rag_toggle` (`:229`, assertion `:240`): the
  response is still `empty`; the state row exists with generation 1 and the marker;
- `test_unavailable_repository_reads_rag_disabled_but_keeps_the_preference` (`:270`): the
  seeded row is unchanged (same generation, no marker); add a case without a seeded row
  where no row appears;
- PATCH true on a live `ready` row and on a `failed` row → no change; on a `ttl`-purged row →
  re-armed with the marker.

**Done when:**

- [ ] the RED run and the GREEN run are recorded;
- [ ] common verification is green.

**Dependencies:** Task 2.

### Task 8: Merge trigger ⚑

**Commit:** `feat(webhooks): request an index update on a merge to the default branch (#143)`

**Files:**

- `app/modules/integrations/webhooks/api/pull_request_dtos.py` (`_GitHubRepositoryDto` `:54`,
  `_GitHubPullRequestDto` `:86`, `_event_from_payload`);
- `app/modules/reviews/application/project_github_pull_request.py` (`PullRequestEvent` `:58`);
- `app/modules/integrations/webhooks/application/github_installation_dispatch.py` (port
  `CodeIndexRefreshHandler` next to `RulesRefreshOnPushHandler` `:139-142`; the constructor;
  the hook before `:216`);
- new `RequestIndexRefresh` in `code_index.application`, with a resolve read in
  `code_index.infrastructure`;
- `app/bootstrap/reviews_api.py` (`:345-356`);
- `tests/test_github_installation_dispatch.py`, `tests/test_github_pr_projection.py`.

**What to do (M3):**

- Optional `merge_commit_sha` and `repository.default_branch` in the DTOs;
  `PullRequestEvent.merge_commit_sha` and `repository_default_branch` default to `None`.
- In `execute`, for a `PullRequestEvent` with `action == "closed"`, `state is
  PullRequestState.MERGED` and `target_branch == repository_default_branch`, call the
  handler before the projection's early return
  (`:216`). The status is unchanged; the detail gains `index=requested`.
- `RequestIndexRefresh.execute(installation_external_id, repository_external_id,
  commit_sha) -> bool`: resolve the repository and its flags (precedent
  `request_for_provider_repository`, `app/modules/repositories/infrastructure/rules_status.py:297-314`),
  `request_build(target_commit_sha=commit_sha)`, commit. No GitHub call.

**Tests to add (unit):**

- DTO: `merge_commit_sha` and `default_branch` parsed when present, `None` when absent;
- dispatch: a merge into the default branch calls the handler with the merge SHA, also when
  the projection returns early (paused repository); another branch, a close without merge,
  `opened`, `synchronize` and `edited` do not call it; the detail carries `index=requested`.

**Done when:**

- [ ] common verification is green.

**Dependencies:** Task 2.

### Task 9: Push trigger

**Commit:** `feat(webhooks): request an index update on a push to the default branch (#143)`

**Files:**

- `app/modules/integrations/webhooks/api/push_event_dtos.py` (`_PushDelivery` `:21-35`);
- `app/modules/integrations/webhooks/application/github_installation_dispatch.py`
  (`PushEvent` `:76-86`, `_push_result` `:268-290`);
- `tests/test_github_installation_dispatch.py` (next to `:1286-1314`).

**What to do:**

- `_PushDelivery.after` optional; a zero SHA or `deleted` → `None`. `PushEvent` carries it
  (suggested `after: str | None = None`).
- `_push_result`: the index refresh runs independently of the rules refresh, both
  idempotent; a push with `after is None` requests no index update. One status per
  delivery as today; the index outcome goes into the detail.

**Tests to add (unit):**

- a push to the default branch calls both handlers, the index one with `after`;
- `rules_refresh` unset → the index refresh still runs;
- another branch, a tag, a zero `after` or `deleted` → no index call;
- the existing push tests stay green.

**Done when:**

- [ ] common verification is green.

**Dependencies:** Task 8.

### Task 10: Trigger, ordering and toggle race tests ⚑

**Commit:** `test(code_index): cover the index triggers, ordering and toggle races (#143)`

**Files:** `tests/test_code_index_lifecycle_postgres.py`.

**What to do:** end-to-end PG scenarios through the real triggers, `DispatchIndexWakeups`,
the job and `CollectIndexGarbage` (plan, "PR 2 tests").

**Tests to add (PG):**

- AC1: two merges before the dispatcher → one wake-up, incremental up to the second; another
  branch, a close without merge, `opened`, `synchronize`, `edited` → the row and the marker
  unchanged;
- AC7 (U4): a push to the default branch → marker and target = `after`; another branch, a
  tag and a zero `after` → nothing changes;
- AC2: the default branch changed → the reconcile sets the marker → full `BRANCH_CHANGED`;
- AC3: behind → marker; equal → no wake-up; both G1 states (`empty`/NULL and `ready`/old)
  with `last_error_code` and no message in the queue;
- AC5: `removed` → `added` or a #113 rebind → re-arm → generation + 1, full;
- AC9: push B, then a late push A → target = A, the job publishes B, the reconcile brings
  target back to B, indexed never equals A; force push → `Ancestry.DIVERGED` → full
  `NOT_AHEAD`;
- AC8, fourth scenario: the job waits on parse → PATCH false → PATCH true → the job is
  released; the old job → `LEASE_LOST`, the new one (generation + 2) publishes, the old
  message → `STALE`; after GC only rows of the new generation remain.

**Done when:**

- [ ] every scenario asserts literal values;
- [ ] common verification is green with zero skips.

**Dependencies:** Tasks 6, 7, 9.

### Task 11: PR 2 documentation ⚑

**Commit:** `docs: document the index update triggers (#143)`

Before the commit, the coordinator re-checks the open PRs and their files (plan,
"Coordination") and posts the #144 hand-off comment.

**Files and edits:**

- `docs/SYSTEM_DESIGN.md`:
  - §7.4 `:869-870`, `:902-910`: the publishers (outbox marker, leader dispatch);
  - §8.2 `:939`, `:943-944`: the `pull_request.closed`, installation and `push` rows;
  - §10: the reconcile variables;
  - `:1528`: `stale` is now possible.
  - §6.9: after a same-account reinstall (#113 rebind) or a repeated `added`, the re-arm
    pass gives the same `repository_id` a full build when RAG is on (replace PR 1's
    "second part of #143" note);
  - the toggle paragraph near `:662-664`: toggle on calls `enable()` (replace PR 1's
    "second part of #143" note);
- `docs/WEBHOOK_WORKER.md:200-238` (the push section), `:399` (the status list).
- `.env.example:86-93`: drop "nothing publishes index.repo before #143"; add the two
  reconcile lines.
- `docs/adr/0001-graph-rag-v1.md` §6.1: the U4 deviation; §6: the converge pass (re-arm and
  disarm).
- `docs/CONTEXT_AND_VERIFICATION_SPEC.md:1718` and `docs/TEST_PLAN.md:376`: "physical
  deletion within an hour" is a target reached by GC on consecutive ticks (same wording as
  SD §10 after PR 1).

**Tests to add:** none.

**Done when:**

- [ ] every listed anchor is edited; the #144 hand-off is posted;
- [ ] common verification is green.

**Dependencies:** Task 10.

### Checkpoint and publication: PR 2

- [ ] One Alembic head; upgrade → downgrade → upgrade on a clean database.
- [ ] Gates green; the focused PG run has zero skips.
- [ ] Clean `code-reviewer` review; check the branch; check the advice against
  `.agents/rules/backend.md`; `/verify-run`.
- [ ] PR body What / Why / How to verify / `Refs #143`, no closing keyword; deviations U4,
  M2, M11, M10; reviewer ndovnar; #143 → On Review.
- [ ] After PR 1 merges, rebase PR 2 on `main` (renumber the migration if #220/#221 merged
  first).

## External: after merge and issue acceptance

- [ ] Enable Push in the staging App.
- [ ] Manual e2e (plan, "Verification"): PATCH on → `ready`; merge → incremental; direct
  push → reconcile → incremental; PATCH off → `empty`/`disabled`, no graph rows within 5 min.
- [ ] On the first real indexing, the worker VmHWM (item 6 of the 19.10 comment). Staging
  only with the user's explicit OK.
- [ ] Check the AC, add a note in #139, update board 12.
- [ ] Lab paperwork: `docs/reports/143/`, mirror in
  `base/projects/team-6/api-t6-paperwork/issue-143/`, `economics.md`, `/verify-run`.
- [ ] Clean up the worktree, the database `dmc_143` and the vhost `dmc_143`.
