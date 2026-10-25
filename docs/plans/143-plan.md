# Implementation plan: RAG index lifecycle, update triggers and purge (#143)

## Status and baseline

Approved by the user on 2026-10-20 under the agent-loop skill. This document is the
English task brief for the executor of both stacked PRs. It translates the approved plan
and adds no new decisions, columns, methods or scope. Where the approved plan leaves a
name or a shape implicit, this brief says **suggested** (the executor may pick another
name) or **derived** (it follows from the cited code, not from a new decision).
Issue: https://github.com/larchanka-training/dmc-268-api-t6/issues/143, epic #139.
Baseline: `origin/main` `29ac426` (#202 rules status and #223 CI merged). The single Alembic
head is `20261018_0035`. Worktree `<isolated-checkout>`.
Branches, stacked: `feat/143-rag-index-purge` (PR 1, from `origin/main`) →
`feat/143-rag-index-triggers` (PR 2, on PR 1). Task checklist: [143-todo.md](143-todo.md).

Every `file:line` below was checked against `29ac426` on 2026-10-20. The approved plan's
references hold, with three corrections applied here:
`installation_repository_unit_of_work.py:168-209` (was `:167-208`); the SD §8.2 rows are
`closed` `:939`, installation `:943` and `push` `:944` (the plan named `:939, :944`); and
the TTL lifetime rule is ADR §4, while ADR §5 names the leader tasks (the plan said "ADR
§5"). PR #132 is merged, so the issue's warning about it no longer applies.

## Context

This issue is part 4 of epic #139 (Graph RAG v1). The blockers are lifted:

- ADR #140 is accepted (`docs/adr/0001-graph-rag-v1.md`);
- #141 is merged (the schema);
- #142 is merged and closed on 2026-10-19 (the indexer and the `index.repo` queue);
- #145 is merged (the `repositories.rag_enabled` flag and PATCH `/api/repos/{id}`).

Today nobody enqueues an index job:

- the application never calls `enable()`, `purge()` or `wake()`;
- nobody writes `target_commit_sha`, so the status `stale` never appears
  (`docs/SYSTEM_DESIGN.md:1528`);
- the GC and TTL leader tasks do not exist (`docs/SYSTEM_DESIGN.md:1515`);
- removing an installation only clears `installation_available`
  (`app/modules/repositories/infrastructure/installation_repository_unit_of_work.py:168-209`).
  The index stays, and `_workspace_bytes`
  (`app/modules/code_index/infrastructure/index_store.py:501`) keeps counting it against
  the Workspace quota.

Goal:

- Triggers request a job that brings the index up to the HEAD of the default branch. The
  triggers are the toggle, a merge, a push to the default branch, the reconcile with HEAD,
  and a reinstall.
- The index is purged when the toggle is turned off, on `installation_repositories.removed`,
  on `installation.deleted`, and by TTL.
- GC physically deletes the rows.

## Issue acceptance criteria (numbered for cross-reference)

AC numbers follow the order of the checkboxes in issue #143. "PR n / Task m" points at
[143-todo.md](143-todo.md).

| AC | Requirement (issue #143) | Covered by |
| --- | --- | --- |
| AC1 | `pull_request.closed` with `merged=true` into the current default branch of an enabled repository requests a job up to the default-branch HEAD; the change set follows ADR #140 from `indexed_commit_sha`, not the PR diff; jobs of one repository merge to the last target; the delivery only requests the job. A merge into another branch, a close without merge and open-PR events leave the index unchanged (review #139, F2) | M3; PR 2 Tasks 2, 4, 8. Tests (PR 2 Task 10): two merges before the dispatcher → one wake-up, incremental up to the second; another branch, a close without merge, `opened`, `synchronize`, `edited` → the state row and the marker are unchanged |
| AC2 | The default branch is re-read on every update; after it changes, the next reconcile builds a full index of the new branch | M4 (the reconcile writes the live branch and SHA; the #142 job reads HEAD at its start); M11 (`repositories.default_branch` is not written). Test (PR 2 Task 10): the default branch changes → the reconcile sets the marker → full with `BRANCH_CHANGED` |
| AC3 | Reconcile: `indexed_commit_sha` behind the default-branch HEAD (direct push) → a job catches up; equal → no job | M4; PR 2 Tasks 2, 3, 5. Tests (PR 2 Task 10): behind → marker; equal → no wake-up; both G1 states of #142 (`empty` with NULL indexed and `ready` with an old indexed SHA), each with `last_error_code` and no message in the queue |
| AC4 | Turning the flag on requests a full index; turning it off deletes the index rows with an explicit DELETE; a repeated delivery or call is idempotent | M6, M1. Off: PR 1 Task 6. On: PR 2 Task 7. The explicit DELETE is the GC leader task (PR 1 Tasks 1–5). Tests: PR 1 Task 6 (off purges, a repeat changes nothing, an unavailable repository is purged too); PR 2 Task 7 (on creates the row with generation 1 and the marker) |
| AC5 | `installation_repositories.removed` and `installation.deleted` delete the index of the affected repositories while the `repositories` row stays; after a reinstall by the same account (#113) with the flag on, a full index is requested | M7 (PR 1 Task 7), M5 (PR 2 Tasks 3, 4). Tests: PR 1 Task 7 (removal purges, the `repositories` row stays, a repeated delivery changes nothing); PR 2 Task 10 (`removed` → `added` or a #113 rebind → re-arm → generation + 1, full) |
| AC6 | A periodic TTL purge deletes the index of disabled repositories older than the threshold and runs once with several replicas; the threshold is in the settings and in SD §10 | U1 (**deviation**: any live index, not only disabled ones; `failed` is excluded); PR 1 Tasks 2–5, 9. Once with several replicas: the worker leader lock plus the `FOR UPDATE` re-check. Tests (PR 1 Task 2): the re-check under a held lock → `False`; two parallel runs → generation grows by exactly 1 |
| AC7 | PG tests for every trigger and for no index change from open-PR events | PR 1 Tasks 6, 7; PR 2 Task 10, including the push trigger (U4): a push to the default branch → marker and target = `after`; a push to another branch, to a tag or with a zero `after` → nothing changes |
| AC8 | PG race tests per ADR #140: purge during the download, purge during the write, a stale message after a purge, off → on before the old job ends; afterwards a disabled repository has no index rows, and a re-enabled one accepts only the job with the new fencing token (review #139, F1) | PR 1 Task 8 (the first three scenarios); PR 2 Task 10 (off → on). Each scenario ends with `CollectIndexGarbage` and a raw row count check |
| AC9 | Ordering tests: events delivered in reverse order → the index converges to the default-branch HEAD, and `indexed_commit_sha` never moves back; a force push → full (review #139, F2) | M4, U2; PR 2 Task 10: push B, then a late push A → target = A, the job publishes B, the reconcile sets target = B, and indexed never equals A; force push → `Ancestry.DIVERGED` → full with `NOT_AHEAD` |

DoD (gates, docs, PRs with `Refs #143` and no auto-close, approve on the current head,
threads resolved, tech-lead acceptance, status in #139 and on board 12) is covered by the
process section and the external items in the todo.

## User decisions of 2026-10-20

- **U1. TTL follows the ADR** (ADR §4 lifetime row; ADR §5 leader task).
  - Condition: `last_used_at < now − CODE_INDEX_TTL_DAYS` (default 30) for any live index.
  - Before the purge, a re-check under `FOR UPDATE`.
  - Found while exploring: `failed` is not in the TTL set. GC has already removed its
    rows, and a TTL purge would turn `failed` into `empty`/`ttl`, which opens the repeated
    full build that ADR §7 forbids.
  - The deviation from the wording of AC6 ("disabled repositories") goes into the PR body.
  - Consequence until #144: the index of an enabled repository is purged after 30 days.
    PATCH `ragEnabled=true` brings it back.
- **U2. Wake-ups are an outbox on the state row.**
  - New column `code_index_states.wake_requested_at` (migration in PR 2).
  - Triggers write the target and the marker in their own transaction.
  - The leader worker wakes the job after the commit and clears the marker with a CAS.
  - The API and `webhook-worker` never publish to AMQP.
- **U3. Two stacked PRs**, each with `Refs #143` and no closing keyword.
  - PR 1: the purge.
  - PR 2: the publishers.
- **U4. A push to the default branch is a trigger too.** This is beyond the Scope; the
  deviation goes into the PR body. Push must be enabled in the staging App settings (#202
  needs the same).

## Author decisions (stated in the PR body)

- **M1. Purge.**
  - The logical purge (`CodeIndexStore.purge`: generation + 1) runs in the caller's
    transaction.
  - The physical DELETE is done by the GC leader task within 1 h (ADR §4, §6).
  - The AC4/AC5 wording "explicit DELETE" is met by GC: it is an explicit DELETE, not a
    cascade.
  - Tests run GC before they check "no rows".
- **M2. Moved to #144:** the trigger on a review (`docs/adr/0001-graph-rag-v1.md:213`) and
  extending `last_used_at` on a read.
- **M3. Merge.**
  - Condition: `closed`, `merged`, and `base.ref == repository.default_branch` from the
    payload. On `PullRequestEvent` that is `action == "closed"`, `state is
    PullRequestState.MERGED` (`_event_from_payload` maps `merged` to the state) and
    `target_branch == repository_default_branch`.
  - Target = `merge_commit_sha`. Without it, only the marker is set.
  - The hook goes before the projection's early return
    (`app/modules/integrations/webhooks/application/github_installation_dispatch.py:216`);
    otherwise a merge in a repository with paused reviews is lost.
  - The delivery makes no GitHub call.
- **M4. Reconcile with HEAD.**
  - Candidates: `repositories.rag_enabled AND repositories.installation_available AND
    provider_installations.deleted_at IS NULL AND provider_installations.provider = 'github'
    AND code_index_states.purge_reason IS NULL AND code_index_states.status <> 'failed'`.
    (Derived: `deleted_at` and `provider` are columns of `provider_installations`,
    `app/modules/repositories/infrastructure/models.py:60,63`.)
  - `head()` is called outside any transaction.
  - The target is always rewritten to the live HEAD. This clears a "stuck" `stale` after
    deliveries in reverse order.
  - The marker is set if any of these holds:
    - (`indexed_default_branch`, `indexed_commit_sha`) ≠ the live (branch, sha);
    - indexed is NULL (G1 of #142);
    - the profile versions are outdated.
- **M5. Re-arm.**
  - Condition: `rag_enabled AND installation_available AND provider_installations.deleted_at
    IS NULL AND provider = 'github' AND (no state row OR purge_reason IN ('removed',
    'disabled'))` → `enable()` together with the marker. The provider filter matches M4:
    `snapshot_sources` raises for a non-GitHub repository.
  - Under `FOR SHARE` on the `repositories` row.
  - Covers AC5 (the #113 rebind, a repeated `added`) and the backfill of repositories
    enabled before #143.
  - `ttl` is left alone.
- **M6. Toggle.**
  - Acts only when `ragEnabled` is present in the request body. The UI sends only changed
    fields (ui#109, FRONTEND_ARCHITECTURE in the ui repo), so editing globs or `enabled`
    does not resurrect a TTL-purged index.
  - `ragEnabled=false` in the body → `purge(disabled)`.
  - `ragEnabled=true` in the body and the repository available → `enable()`:
    - creates the row, or re-arms a purged one (`ttl` included), with the marker;
    - does nothing on a live row;
    - does nothing on `failed`. A retry goes through off → on.
  - Ports `CodeIndexPurger` / `CodeIndexEnabler` live in `repositories.application`; the
    adapter lives in `code_index.infrastructure` and works on the caller's session. After
    the write, the response is read again.
  - The ORM import of `CodeIndexState` for the read (the outer join in
    `app/modules/repositories/infrastructure/repository_settings.py:15,26-36`) stays.
    Rewrite `docs/BACKEND_ARCHITECTURE.md:467-471`: writes go through the port; the read is
    the exception.
- **M7. removed / deleted.**
  - `disable_repository` → `UUID | None`, `disable_installation` → `list[UUID]`
    (`RETURNING`).
  - `purge(removed)` for `sorted(ids)` runs after `record_removal_delivery`, in the same
    transaction. Lock order: `repositories` → state.
- **M8. `leader_tick`:** run sweep and replay catching `Exception` only, then `code_index.tick()`, then re-raise the caught exception; a cancellation (`CancelledError`) skips the index tick.
  - Each task has its own `try/except` and a monotonic gate. All index tasks together get
    one budget, about 10 s per tick, so gates that open together do not delay sweep and
    replay.
  - The order test sweep → replay (`tests/test_queue_messages.py:613-642`) stays green.
  - A failing task does not skip replay.
  - Without the GitHub App the reconcile is off; without the consumer the dispatcher is off.
- **M9. Enumeration.** A separate port `CodeIndexMaintenance` (no `repository_id`) in
  `app/modules/code_index/application/maintenance.py`, with its adapter as a separate class.
  The `CodeIndexStore` contract is not broken.
- **M10. Settings.**
  - Only `CodeIndexSettings` fields → `CODE_INDEX_*`, with lines in `.env.example` and in
    SD §10.
  - Compose and `env-file.sh` are not touched: that is the secrets allowlist, and #142 did
    not add `CODE_INDEX_*` there either.
- **M11.** `repositories.default_branch` is not written. The observation goes into the PR.

## Out of scope

- **#144:** the trigger on a review (ADR `:213`), extending `last_used_at` on a read, and
  bringing an index back after TTL through `enable()` rather than a bare wake-up.
- Writing `repositories.default_branch` (M11).
- Compose and `deploy/.../env-file.sh` (M10).
- **#97** (GitLab): the reconcile candidates are `provider = 'github'` only.

## Coordination (before the edit)

Before every docs commit, re-check the open PRs and their files. As of 2026-10-20:

- **#220** (open; adds `20261018_0036` and `20261018_0037`): comment there about
  `app/worker.py`, `app/webhook_worker.py`, `app/modules/repositories/infrastructure/models.py`,
  SD, WEBHOOK_WORKER, `.env.example`, `tests/test_code_index_store_postgres.py` and the
  migration number.
- **#221** (open; adds `20261019_0036`): comment there about SD, CICD,
  `tests/test_code_index_store_postgres.py` and the migration order (#221 = 0036). #221 also
  edits `docs/BACKEND_ARCHITECTURE.md`; name it in the same comment.
- **#144**: a hand-off comment, in the form of the #142 → #143 hand-off. What it hands over:
  - the trigger on a review (ADR `:213`);
  - extending `last_used_at` on a read. Until then, under U1, every index is purged after
    30 days;
  - bringing an index back after TTL through `enable()`, not a bare wake-up.

## PR 1: `feat(code_index): purge the index on disable, removal and TTL`

Branch `feat/143-rag-index-purge` from `origin/main`. No migration.

### Store (`app/modules/code_index/{application,infrastructure}/index_store.py`)

- Extract the garbage predicate of `collect_garbage`
  (`app/modules/code_index/infrastructure/index_store.py:854-868`) into a module-level
  function over SQL expressions. The store and the enumeration both use it.
  - Derived: inside `collect_garbage` the predicate closes over Python values read under
    `FOR SHARE` (`generation`, `published`, `floor`, `leased`, `displaced_due`,
    `:845-852`). The shared function takes SQL expressions for all five, so that
    `with_garbage` can correlate them with `code_index_states` columns.
  - Suggested signature: `garbage_condition(model: _GraphModel, *, generation:
    ColumnElement[int], published: ColumnElement[int], floor: ColumnElement[int], leased:
    ColumnElement[int | None], displaced_due: ColumnElement[bool]) -> ColumnElement[bool]`.
    A NULL `leased` spares nothing (today: `true()` when `leased is None`), so the SQL form
    is `or_(leased.is_(None), model.valid_from != leased)`.
  - The store keeps reading the state under `FOR SHARE` and passes the values as bound
    literals: behaviour is unchanged.
- New port and adapter method `purge_unused(repository_id: UUID, unused_for: timedelta) -> bool`:
  - takes the state row `FOR UPDATE`;
  - runs the TTL purge (`purge` with `PurgeReason.TTL`) only if
    `purge_reason IS NULL AND status <> 'failed' AND last_used_at < clock_timestamp() - unused_for`;
  - returns whether it purged.
- `tests/test_code_index_store_contract.py`: add the method to `_PORT_METHODS` and rewrite
  the comment at `:49` (the TTL seam now exists and takes `repository_id`; the leader
  enumeration lives in the separate `CodeIndexMaintenance` port).

### Enumeration (`app/modules/code_index/{application,infrastructure}/maintenance.py`)

Port `CodeIndexMaintenance` (no `repository_id`), adapter as a separate class (suggested
`SqlAlchemyCodeIndexMaintenance`). Like `SqlAlchemyCodeIndexStore(session)`, the adapter is
built on a session and never commits; the leader task opens and commits the transaction, so
the PR 2 re-arm re-check can share one session with `enable()`. The listing methods return
repository ids ordered by `repository_id`, after the cursor `after`:

- `unused(unused_for: timedelta, after: UUID | None, limit: int) -> list[UUID]`:
  `purge_reason IS NULL` is required, because a purge keeps `last_used_at`. It mirrors the
  re-check: `status <> 'failed' AND last_used_at < clock_timestamp() - :unused_for`.
- `with_garbage(after: UUID | None, limit: int) -> list[UUID]`: an expired lease
  (`lease_token IS NOT NULL AND lease_expires_at <= clock_timestamp()`), or `EXISTS` garbage
  rows in `code_index_files`, using `garbage_condition` with the correlated state columns:
  `generation`, `published_publication`, `floor_publication`, `CASE WHEN lease_token IS NOT
  NULL THEN reserved_publication END`, and `published_at IS NOT NULL AND published_at <=
  clock_timestamp() - interval '10 minutes'` (`_DISPLACED_GRACE`,
  `app/modules/code_index/infrastructure/index_store.py:91`). The
  PK prefix `(repository_id, generation, valid_from, path)` keeps the probe cheap.

### Leader tasks (shared tick budget per M8)

Suggested module `app/modules/code_index/application/leader_tasks.py`.

- `PurgeUnusedIndexes`: pages `unused(...)`; one short transaction per candidate
  (`purge_unused` + commit).
- `CollectIndexGarbage(batch_rows, budget_seconds, clock)`:
  - an in-memory round-robin cursor over `with_garbage(after, limit)`;
  - per repository: commit `revoke_expired_lease` in its own transaction, then
    `collect_garbage(repository_id, batch_rows)` batches, one transaction each, until a
    batch deletes fewer than `batch_rows` or the budget runs out.
- `CodeIndexLeaderTasks`: gates and isolation.
  - One gate per task on a monotonic clock: TTL every `CODE_INDEX_TTL_INTERVAL_SECONDS`, GC
    every `CODE_INDEX_GC_INTERVAL_SECONDS` (PR 2 adds converge at most every 60 s, dispatch
    on every tick and the reconcile every `CODE_INDEX_RECONCILE_INTERVAL_SECONDS`; the
    order is converge, dispatch, GC, TTL, reconcile).
  - Derived from M8: one deadline per tick, about 10 s, as a module constant (not an
    env knob). `CodeIndexLeaderTasks` passes the remaining time down to each task;
    a task starts no new unit after the deadline.
  - Each task runs inside its own `try/except Exception` with a logged error; `tick()`
    never raises.
- `app/worker.py`:
  - `WorkerProcess.code_index: CodeIndexLeaderTasks | None = None`
    (`WorkerProcess` at `app/worker.py:707-718`);
  - `leader_tick` per M8 (catch `Exception`, index tick, re-raise; cancellation skips the tick) when
    `code_index` is set;
  - built in `run_worker` (`app/worker.py:805`) from `settings.code_index` and
    `session_factory`, and handed to the `WorkerProcess`.

### Settings

New `CodeIndexSettings` fields in `app/bootstrap/code_index_worker.py:80-157`. Each field
reads `CODE_INDEX_<FIELD>`, so the field names are fixed by the variable names:

| Variable | Field | Default | Note |
| --- | --- | --- | --- |
| `CODE_INDEX_TTL_DAYS` | `ttl_days` | 30 | |
| `CODE_INDEX_TTL_INTERVAL_SECONDS` | `ttl_interval_seconds` | 3600 | |
| `CODE_INDEX_GC_INTERVAL_SECONDS` | `gc_interval_seconds` | 300 | `__post_init__` check `< 3600` (ADR: physical delete within 1 h) |
| `CODE_INDEX_GC_BATCH_ROWS` | `gc_batch_rows` | 10000 | |

### Purge triggers

- Port `CodeIndexPurger` in `repositories.application` (suggested module
  `app/modules/repositories/application/code_index.py`), shape `purge(repository_id: UUID,
  reason: PurgeReason) -> None`. `repositories.application` already imports
  `code_index.domain` (`app/modules/repositories/application/repository_settings.py:11`).
- Adapter `app/modules/code_index/infrastructure/purger.py`:
  `SqlAlchemyCodeIndexPurger(session)`, which calls `SqlAlchemyCodeIndexStore(session).purge`.
  Both UoWs expose it as a property on their own session (suggested name `index_purger`);
  their constructors do not change:
  - `SqlAlchemyRepositorySettingsUnitOfWork`
    (`app/modules/repositories/infrastructure/repository_settings.py:133-151`) and its
    Protocol `RepositorySettingsUnitOfWork`
    (`app/modules/repositories/application/repository_settings.py:60-62`);
  - `SqlAlchemyInstallationRepositoriesUnitOfWork`
    (`installation_repository_unit_of_work.py:250-258`) and its Protocol
    `InstallationRepositoriesUnitOfWork`
    (`app/modules/repositories/application/sync_installation_repositories.py:64-68`).
- `UpdateRepository` (`app/modules/repositories/application/repository_settings.py:86-96`):
  `update_repository` → `purge(PurgeReason.DISABLED)` when the body has `ragEnabled=false`
  (`change.rag_enabled is False`) → `get_repository` again → commit.
- `SyncInstallationRepositories.disable`
  (`app/modules/repositories/application/sync_installation_repositories.py:149-174`) and the
  UoW (`installation_repository_unit_of_work.py:168-209`): `RETURNING` and the purge per
  M7. The Protocol return types change at `sync_installation_repositories.py:49-53`.

Observation for the docs task (not a redesign): the property makes
`repositories.infrastructure` construct a `code_index.infrastructure` adapter. Cross-module
infrastructure imports already exist (for example
`installation_repository_unit_of_work.py` imports
`workspaces.infrastructure.github_installation_locks`), and `.agents/rules/backend.md` §3
regulates only the dependency direction. The BA rewrite at `:467-471` names it.

### PR 1 tests

- PG, store (`tests/test_code_index_store_postgres.py`), AC6:
  - the re-check in `purge_unused`: `last_used_at` is refreshed under a held lock
    (`_wait_until_blocked`, `:1062`) → `False`;
  - two parallel runs → generation grows by exactly 1 ("once with several replicas");
  - `failed`, an already purged row and a fresh row → `False`, nothing changes.
- PG, enumeration (new, suggested `tests/test_code_index_maintenance_postgres.py`; the name
  matches the focused-run glob):
  - an expired lease is selected;
  - displaced rows are selected;
  - rows under a live lease are not selected;
  - no candidates ⇒ raw row counts equal visible counts;
  - `unused` skips purged and `failed` rows and honours `after` and `limit`.
- Unit (new, suggested `tests/test_code_index_leader_tasks.py`):
  - gates on fake clocks;
  - task isolation (one task raising does not stop the others; `tick()` does not raise);
  - budget and cursor (the round-robin resumes after the last repository);
  - one transaction per store call (the `Tracker` pattern, `tests/test_build_repository_index.py:123`).
- Unit, worker (`tests/test_queue_messages.py`): the test at `:613-642` stays green
  unchanged; new: the index tick runs after replay; an index task that raises does not
  skip replay and does not raise out of `leader_tick`.
- Unit, settings (`tests/test_code_index_worker.py`): defaults, env parsing,
  `CODE_INDEX_GC_INTERVAL_SECONDS=3600` refused.
- PG, repositories (AC4, AC5, AC7):
  - toggle off purges the index, a repeat changes nothing, an unavailable repository is
    purged too, a PATCH without `ragEnabled` purges nothing
    (`tests/test_repository_rag_postgres.py`);
  - `removed` and `deleted` purge the index while the `repositories` row stays; a repeated
    delivery changes nothing. Where: the new `tests/test_code_index_lifecycle_postgres.py`,
    or next to the removal tests in `tests/test_portal_scope_postgres.py:334-396` (then add
    that file to the focused run).
  - Note: `tests/test_installation_repository_sync.py` has no `integration` marker (fakes
    only); its fakes (`:44`, `:80-83`, `:115`) and the `RETURNING` unit test (`:373`, with
    `FakeDisableSession` at `:350`) change with M7, and it runs in the plain gate.
- AC8, three of the four scenarios: the new `tests/test_code_index_lifecycle_postgres.py` on
  the harness of `tests/test_code_index_indexer_postgres.py`. Every scenario ends with
  `CollectIndexGarbage` and the check `_raw_rows == (0, 0, 0, 0)` (`_raw_rows` at
  `tests/test_code_index_indexer_postgres.py:906`).
  - Purge during the download: `Vcs.archive()` (`:315`) waits for a real trigger
    (`UpdateRepository` / `disable`) → `LEASE_LOST`.
  - Purge during the write: `_ActOnParse(call=2)` (`:1097`), `batch_files=1`.
  - An old message after the purge → `STALE`.

### PR 1 documents

- SD:
  - §6.9 `docs/SYSTEM_DESIGN.md:588-590`: the index is not carried over a reinstall, and
    `deleted` purges it. The same wording as the ADR (comment in #143 of 16.10);
  - `:662-664`: turning the toggle off purges;
  - §10 `:1412`, `:1437-1443`: the variables and U1;
  - `:1515`: the leader tasks.
- ADR §4, §6.
- `docs/BACKEND_ARCHITECTURE.md:462-472`.
- `docs/WEBHOOK_WORKER.md:263`.
- `.env.example:86-109`.

## PR 2: `feat(code_index): request index updates on toggle, merge, push, drift`

Branch `feat/143-rag-index-triggers` on PR 1.

### Migration

The next free number after the head of `main` at commit time; do not hard-code it here. #221
already took `20261019_0036` after its rebase, and #220 holds `20261018_0036`/`0037`. If #221
merges first, ours is `…_0037` with `down_revision = "20261019_0036"`. Agree the number in
#220/#221 before the commit; whoever merges second renumbers.

- Column `wake_requested_at timestamptz NULL`.
- CHECK `ck_code_index_states_wake_live`:
  `wake_requested_at IS NULL OR (purge_reason IS NULL AND status <> 'failed')`.
- A partial index on the marker (`WHERE wake_requested_at IS NOT NULL`; suggested name
  `ix_code_index_states_wake_requested`, like `ix_code_index_states_live_lease` in
  `20261017_0034` and `ix_repository_rules_status_refresh` in `20261018_0035`).
- A partial index `code_index_files (repository_id) WHERE valid_to IS NOT NULL` (suggested
  name `ix_code_index_files_closed`) for the GC probe of `with_garbage`, which today reads
  every live file row of a repository without garbage (PR 1 review round 1, S3).
- The ORM model gets the column, the CHECK and the indexes in the `__table_args__` pattern of
  `app/modules/code_index/infrastructure/models.py:40-52,72`.
- Documents (same PR, per CICD §3):
  - CICD §3: the downgrade-loss row goes on top of the table (header `docs/CICD.md:237-238`, current top row `:239`);
  - the revision lists in `docs/SYSTEM_DESIGN.md:731` and `docs/CICD.md:255`;
  - `docs/erd/erd.mmd` (the `CODE_INDEX_STATES` block, `:166`);
  - `docs/BACKEND_ARCHITECTURE.md` (the revision paragraph, `:490-496`).

### Store

Three methods join the contract list (`_PORT_METHODS`). Do not add the marker to `IndexState`.

- `purge` and `_failure()` (`app/modules/code_index/infrastructure/index_store.py:112-134`)
  clear the marker. `_failure()` is
  called by `mark_failed` (`:782`) and by QUOTA in `claim_lease` (`:477`); without the
  clear, the CHECK fails inside the claim.
- `enable()` (`:384-405`):
  - creation and re-arm set `wake_requested_at = clock_timestamp()` together with
    `last_used_at`;
  - a live row and `failed` stay a no-op.
- `request_build(repository_id, *, target_commit_sha: str | None) -> bool`:
  - under `FOR UPDATE`, only for a live, non-`failed` row;
  - writes the target when given, and always sets a fresh marker (`clock_timestamp()`), so
    a CAS clear of an older marker misses. Precedent for the marker:
    `request_rules_refresh` (`app/modules/repositories/infrastructure/rules_status.py:49`).
- `reconcile_head(repository_id, *, generation, branch, commit_sha, versions_current) -> bool`:
  M4 under `FOR UPDATE`. A missing row, another generation, a purged or a `failed` row →
  `False` with no write. Otherwise it writes `target_commit_sha = commit_sha` and sets the
  marker on any M4 condition; it returns whether it set the marker.
- `clear_wake_request(repository_id, requested_at) -> bool`: CAS,
  `SET wake_requested_at = NULL WHERE repository_id = :id AND wake_requested_at = :requested_at`.

### Enumeration

New `CodeIndexMaintenance` methods:

- `wake_requests(limit)`: rows with `wake_requested_at IS NOT NULL` and no live lease,
  oldest marker first, with `repository_id`, `generation` and `wake_requested_at`;
- `heads(after, limit)`: the M4 candidates (`code_index_states` ⋈ `repositories` ⋈
  `provider_installations`), each with its `IndexedRepository`
  (`app/modules/code_index/application/ports.py:15`), `generation` and profile
  (`IndexProfile` or `None`);
- `to_rearm(limit)`: the M5 condition (`repositories` left join `code_index_states`).
- `to_disarm(limit)`: live rows whose effective RAG is off → `purge(disabled)` or
  `purge(removed)` in the same converge pass (PR 1 review round 1, F12).

### Leader tasks

Order within a tick: converge, dispatch, GC, TTL, reconcile. The dispatch sends the markers
converge set in the same tick. GC runs before TTL: physical deletion has the one-hour target
(ADR §4), while the 30-day TTL tolerates delay, so a TTL backlog never starves GC. The
reconcile runs last on what is left of the tick budget; its markers go out on the next tick.

- `RearmIndexes` (the converge pass, `ConvergeIndexes`): at most every 60 s. Per candidate
  one short transaction: re-check M5 with the `repositories` row locked `FOR SHARE`, then
  `enable()` (which sets the marker), commit. The re-check and `enable()` share one session.
- `DispatchIndexWakeups`: every tick, right after converge.
  - Rows under a live lease are skipped and keep their marker: a fresh LEASED wake-up is
    confirmed and dropped (`app/modules/code_index/infrastructure/amqp.py:317-326`).
  - `wake()` outside any transaction, then the CAS `clear_wake_request`.
  - If `wake()` fails, the pass stops and the markers stay.
- `ReconcileIndexHeads`:
  - `CODE_INDEX_RECONCILE_INTERVAL_SECONDS=600` (field `reconcile_interval_seconds`),
    `CODE_INDEX_RECONCILE_BATCH=50` (field `reconcile_batch`), within the tick budget;
  - a cursor over `heads(after, limit)`;
  - one token cache per run: add a `cache` parameter to `snapshot_sources`
    (`app/bootstrap/code_index_worker.py:187-215`). Without it, each job keeps its own
    `InMemoryInstallationAccessTokenCache` as today;
  - `versions_current = profile is not None and versions.built(profile)` (`IndexVersions`,
    `app/modules/code_index/application/build_index.py:141-160`);
  - a rate-limited `VcsUnavailable` (`app/modules/code_index/application/vcs.py:119`) →
    the installation is skipped until `retry_after`;
  - `VcsAccessDenied` or `head()` → `None` → the repository is skipped.
- Wiring: move `index_consumer` before `compose_worker_process`
  (`app/worker.py:862-885`). The consumer exposes `wakeups` and the source factory, which
  the dispatcher and the reconcile take. Without the GitHub App, `index_consumer` yields
  `None` (`code_index_worker.py:291-297`), so both stay off.

### Toggle on

Per M6: `CodeIndexEnabler` next to `CodeIndexPurger` (shape `enable(repository_id: UUID) ->
None`), implemented in `code_index/infrastructure/purger.py` on the caller's session.
`UpdateRepository` calls it when the body has `ragEnabled=true` and the re-read shows the
repository available (`RepositorySettings.rag_enabled` is already masked by
`installation_available`,
`app/modules/repositories/infrastructure/repository_settings.py:41`).

- `test_settings_api_patch_stores_the_rag_toggle`
  (`tests/test_repository_rag_postgres.py:229`; the assertion at `:240`) is inverted: the
  response is still `empty`, and there is a state row with generation 1 and the marker.
- `test_unavailable_repository_reads_rag_disabled_but_keeps_the_preference` (`:270`) seeds a
  published state row before the PATCH, so its assertion is "the row is unchanged: same
  generation, no marker". The approved plan's "no row" holds only for a separate case
  without a seeded row; add that case.

### Merge trigger

- DTO (`app/modules/integrations/webhooks/api/pull_request_dtos.py`):
  `_GitHubPullRequestDto.merge_commit_sha` (`:86`) and `_GitHubRepositoryDto.default_branch`
  (`:54`; not the class of the same name in `workspaces`), both optional.
- `PullRequestEvent` (`app/modules/reviews/application/project_github_pull_request.py:58`)
  gets `merge_commit_sha` and `repository_default_branch`, default `None`; filled in
  `_event_from_payload`.
- Port `CodeIndexRefreshHandler.execute(installation_external_id, repository_external_id,
  commit_sha) -> bool` in the dispatcher module, like `RulesRefreshOnPushHandler`
  (`github_installation_dispatch.py:139-142`). Implementation `RequestIndexRefresh` in
  `code_index.application`: resolve the repository and its flags, `request_build`, commit.
  Resolution precedent: `request_for_provider_repository`
  (`app/modules/repositories/infrastructure/rules_status.py:297-314`).
- Wiring: a new dispatcher keyword next to `rules_refresh=` in
  `app/bootstrap/reviews_api.py:345-356`.
- The delivery status does not change; the detail gains `index=requested`.

### Push trigger

- `_PushDelivery` (`app/modules/integrations/webhooks/api/push_event_dtos.py:21-35`) gets an
  optional `after`: a zero SHA or `deleted` → `None`. `PushEvent`
  (`github_installation_dispatch.py:76-86`) carries it (suggested `after: str | None = None`).
- In `_push_result` (`github_installation_dispatch.py:268-290`) the index refresh runs
  independently of the rules refresh; both are idempotent. Derived from the AC7 test: a
  push with `after is None` requests no index update.
- The status stays as today: one per delivery. The index outcome goes into the detail.

### PR 2 tests

- AC1:
  - two merges before the dispatcher → one wake-up, incremental up to the second;
  - another branch, a close without merge, `opened`, `synchronize`, `edited` → the row and
    the marker are unchanged.
- AC7, push (U4):
  - a push to the default branch → marker and target = `after`;
  - a push to another branch, to a tag and with a zero `after` → nothing changes.
- AC2: the default branch changed → the reconcile sets the marker → full `BRANCH_CHANGED`.
- AC3:
  - behind → marker; equal → no wake-up;
  - both G1 states (`empty`/NULL and `ready`/old) with `last_error_code` and no message in
    the queue.
- AC5: `removed` → `added` or a rebind → re-arm → generation + 1, full.
- AC9:
  - push B, then a late push A: target = A, the job publishes B, the reconcile brings
    target back to B, and indexed never equals A;
  - force push: `Ancestry.DIVERGED` → full `NOT_AHEAD`.
- AC8, the fourth scenario:
  - the job waits on parse → PATCH false → PATCH true → the job is released;
  - the old job → `LEASE_LOST`; the new one (generation + 2) publishes; the old message →
    `STALE`;
  - after GC only rows of the new generation remain.
- `test_worker_without_github_app_starts_and_stays_healthy`
  (`tests/test_worker_queue_postgres.py:908`) stays green.
- Unit: store PG tests for each new method and the marker clear by `purge`, `mark_failed`
  and QUOTA; enumeration PG tests; leader-task unit tests (re-arm, dispatch stops on a
  failed wake, reconcile skips per installation and per repository); dispatcher tests in
  `tests/test_github_installation_dispatch.py` (next to `:1286-1314`).

### PR 2 documents

- SD:
  - §7.4 `docs/SYSTEM_DESIGN.md:869-870`, `:902-910`: the publishers;
  - §8.2 `:939`, `:943-944`: the `pull_request.closed`, installation and `push` rows;
  - §10;
  - `:1528`: `stale` is now possible.
- `docs/WEBHOOK_WORKER.md:200-238`, `:399`.
- `.env.example:86-93`: drop "nothing publishes index.repo before #143".
- ADR §6.1: the U4 deviation.

## Process

**Environment:**

- `git fetch`; the worktree `<isolated-checkout>` from `origin/main`;
- `uv sync`, `.env` (never read or print it);
- a dedicated database `<disposable-db>` on the local PG17 and a dedicated RabbitMQ
  vhost `<isolated-task-vhost>`: parallel sessions pick up each other's NOTIFY traffic;
- `df "<temporary-directory>"` before long runs.

**Loop per `agent-loop`:**

- The first commit of PR 1 is `docs/plans/143-plan.md` and `143-todo.md` (English, the
  executor brief for both PRs).
- Implementation is done by a subagent from the brief file. Every brief states explicitly:
  do not read the clone's `.env` (incident of 2026-10-20 with the dev App PEM).
- Review is done by a clean `code-reviewer`: only `git show`/`git diff`, no checkout;
  after the review, check the branch.
- Check the reviewer's advice against `.agents/rules/backend.md` before pushing.
- Conventional commits with `(#143)`; gates green on every commit.

**PRs:**

- body What / Why / How to verify / `Refs #143`, no closing keyword; no new dependencies;
- the deviations below;
- reviewer ndovnar;
- #143 → On Review once PR 2 is ready;
- PR 2 is rebased after PR 1 merges.

**After PR 2 merges:**

- enable Push in the staging App;
- on the first real indexing, measure the worker VmHWM (item 6 of the 19.10 comment).
  Staging only with the user's explicit OK;
- check the AC, add a note in #139, update the board;
- lab paperwork: `docs/reports/143/`, mirror in
  `<private-paperwork-directory>`, `economics.md`, `/verify-run`;
- clean up the worktree, the database and the vhost.

## Deviations (stated in the PR body)

- **U1 (PR 1).** TTL purges any live index unused for `CODE_INDEX_TTL_DAYS`, not only
  disabled ones (ADR §4). Until #144 extends `last_used_at` on a read, the index of an
  enabled repository is purged after 30 days; PATCH `ragEnabled=true` brings it back.
- **`failed` is excluded from TTL (PR 1).** GC already removed its rows; a TTL purge would
  turn it into `empty`/`ttl` and open the repeated full build ADR §7 forbids.
- **M1 (PR 1).** "Explicit DELETE" (AC4, AC5) is the GC leader task within 1 h, not a
  DELETE in the caller's transaction (ADR §4).
- **M6, the read side (PR 1).** The ORM read of `CodeIndexState` in the settings adapter
  stays as an exception; writes go through `CodeIndexPurger`/`CodeIndexEnabler`.
- **M10 (PR 1, PR 2).** New variables live only in `CodeIndexSettings`, `.env.example` and
  SD §10; compose and `env-file.sh` are not touched.
- **M2 (PR 2).** The trigger on a review and extending `last_used_at` on a read move to
  #144.
- **U4 (PR 2).** A push to the default branch is a trigger, beyond the Scope; the staging
  App needs the Push subscription.
- **M11 (PR 2).** `repositories.default_branch` is not written; the reconcile reads the live
  default branch instead. Observation only.

## Lab-side work

- Report `docs/reports/143/` (lab-local) and its mirror in
  `<private-paperwork-directory>`;
- a row in `economics.md`;
- after PR 2 merges, check the #143 AC and add a note in #139.

## Verification

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy . --exclude '^docs/reports/'   # CI runs `uv run mypy .`; the exclude skips the lab-local ignored reports
uv run pytest
TEST_DATABASE_URL=<disposable-db-url> \
TEST_RABBITMQ_URL=<isolated-task-vhost-url> \
  uv run pytest -m integration -rs tests/test_code_index_*_postgres.py \
  tests/test_repository_rag_postgres.py tests/test_worker_queue_postgres.py \
  tests/test_initial_migration.py   # 0 skips
```

- `tests/test_installation_repository_sync.py` and `tests/test_alembic_heads.py` carry no
  `integration` marker, so `-m integration` deselects them; they run in the plain
  `uv run pytest` gate. If the removal PG tests go into `tests/test_portal_scope_postgres.py`,
  add it to the focused run.
- Migrations (PR 2): `uv run alembic heads` shows one head; upgrade → downgrade → upgrade
  on a clean database.
- Manual e2e after PR 2 (local compose, the dev App, `axyi/dmc268-t6-sandbox*`):
  1. PATCH `ragEnabled=true` → the worker log shows `ready`.
  2. Merge a PR in the sandbox → target = the merge SHA → incremental.
  3. A direct push → the reconcile → incremental.
  4. PATCH false → state `empty`/`disabled`; within 5 min no graph rows remain.
- Staging only after the user's explicit OK.
