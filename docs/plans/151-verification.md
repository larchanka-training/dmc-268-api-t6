# #151 composition checkpoint verification

Implementation checkpoint: 2026-10-13, branch `fix/151-pr132-followup`, based solely
on refreshed `main` at `c1d8f59d9ff3f332a3595857dbe612115ad559e8`.
The verified implementation/test source is published in commit
`8699e7eb59379fd3ee594267c3be1590adf68180`. This checkpoint covers Tasks 1-6
(P3-1/2/3/4/6); the P3-5 refactor remains a separate PR from refreshed main.
A subsequent publication-metadata commit changes only these plan/proof/checklist
documents; it does not change the source exercised by the recorded tests.

## Isolated environment

Dedicated containers owned by this issue: `dmc268-151-tests-postgres`
(`postgres:17-alpine`) and `dmc268-151-tests-rabbitmq` (`rabbitmq:4-alpine`).
Both bind only to loopback with dynamically assigned host ports; no unrelated
service or data was modified. Each PostgreSQL fixture creates, migrates, then drops
its own random schema. The full-suite broker is dedicated because its tests reset
the review topology. Keep these containers until both independent PRs are published.

Local replay environment: `/private/tmp/dmc268-151-tests.env` (synthetic disposable
test connection settings, no production credentials). In this worktree:

```sh
source /private/tmp/dmc268-151-tests.env
uv run pytest tests/test_run_policy_context_origin_postgres.py tests/test_run_policy_snapshot_origin_postgres.py tests/test_run_origin_guard_postgres.py -rs
```

## Baseline

Before test implementation, the exact three-file command above passed **140 tests,
zero skips in 98.55 seconds**, with two dependency deprecation warnings. Log:
`/private/tmp/dmc268-151-baseline.log`. The fixture's random schemas were cleaned up.

## Public composition regression

`test_worker_composition_passes_claimed_checkpoint_to_context_stage` invokes
`compose_worker_process(...).handle_run.execute(...)` with real PostgreSQL lifecycle,
policy, diff, trace and output stores, production context factory, GitHub token/VCS/
policy/revision adapters, HTTP MockTransport and deterministic external-port fakes.
It observes the committed claim (`running`, `worker-it`, attempt 2) at stage creation.

The active case reaches the nested policy tree and imported `pkg/models.py` blob,
records a `context.build` trace with `review` status and an imported model block,
runs the review model once, and queues publication with the run in `publishing`.
The retirement case revokes the origin during the base-policy root-tree response;
no subsequent HTTP request occurs, no replacement credential is requested, the
model is never invoked, and the run ends `cancelled/installation_unavailable`.
Every HTTP handler acquires independent installation/repository/run
`FOR UPDATE NOWAIT` locks, proving these calls do not retain those row locks.
Existing stage-level active/deleted/rebound coverage remains intact. The new public
proof is intentionally active/deleted: a full installation replacement callback
has its separate `installation_replaced` terminalization semantics.

## Mutation RED and restoration

Only the new test's stage-factory wrapper was temporarily changed from forwarding
the received checkpoint to forwarding an async no-op. `app/worker.py`,
`CheckpointedProvider`, and all origin/persistence guards stayed intact. No private
pipeline was accessed or replaced. The mutant was restored immediately after pytest.

```sh
uv run pytest tests/test_run_policy_context_origin_postgres.py -k 'worker_composition and deleted' -rs
```

On the final test version the mutant failed the exact request-boundary assertion:
**8 HTTP requests versus 2 at retirement**; **1 failed, 25 deselected, zero skips in
1.95 seconds**. Log: `/private/tmp/dmc268-151-mutation.log`. The additional requests
include the nested policy tree and policy blobs, before persistence guards reject
the retired origin. This is behavioral RED, not a signature/model failure.

Restored composition control: **2 passed, 24 deselected, zero skips in 3.99 seconds**;
log `/private/tmp/dmc268-151-composition.log`.

## Final checks

- `uv run ruff check .`: passed (`/private/tmp/dmc268-151-ruff.log`).
- `uv run ruff format --check .`: passed, 433 files
  (`/private/tmp/dmc268-151-format.log`).
- `uv run mypy .`: passed, 430 source files (`/private/tmp/dmc268-151-mypy.log`).
- Final six-file focused suite: **184 passed, zero skips in 83.88 seconds**, with
  two dependency deprecation warnings. Final log
  `/private/tmp/dmc268-151-focused-final.log` supersedes earlier development runs.
- `uv run pytest`: **3427 passed, zero failures/skips in 1118.15 seconds
  (0:18:38)** with two existing dependency deprecation warnings, against both
  dedicated services; log `/private/tmp/dmc268-151-pytest.log`.
- Final database cleanup check: zero non-system/private test schemas remain.
  Both dedicated containers remain live for the separately published refactor.
- `git diff --check`: clean.

Coordinator completed ownership coordination before implementation:
[#124](https://github.com/larchanka-training/dmc-268-api-t6/pull/124#issuecomment-6153738352)
and [#148](https://github.com/larchanka-training/dmc-268-api-t6/pull/148#issuecomment-6153738739).
No production worker, dependency, migration or public contract changes were made.
The coordinated SYSTEM_DESIGN corrections are described below.

## Documentation corrections

- Round-eight context proof labels the pre-cache and top-level stage checkpoints
  as defense in depth; post-cache-await/per-request guarantees and historical counts
  remain intact. It links this separate public composition proof.
- CICD and SYSTEM_DESIGN distinguish downgrade target `20261012_0030` (origin,
  identity, deletion and availability fields removed; `enabled=true/available=false`
  folds to false and re-upgrade cannot recover that preference or numeric identity)
  from the deeper
  `20261007_0028` policy documents/snapshots and non-NULL-policy conventions loss.
  Retained revocations/current Repository FK can backfill deletion/origin fields,
  without guaranteed restoration of their former historical values.
- SYSTEM_DESIGN §8.4 and its §6.4 origin-fence summary explicitly include policy
  documents/snapshots writes and policy/AST tree/blob requests, with short
  transactions and GitHub/LLM I/O outside them.
- Four round-six documents carry Historical notices with full checkpoint/base SHAs;
  historical counts remain intact and old gate/approval wording is scoped to that
  checkpoint.

Verified read-only against the frozen migration chain
`20261010_0030 -> 20261010_0029 -> 20261012_0030 -> 20261011_0029 -> 20261007_0028`,
`policy_snapshot_unit_of_work.py`, policy/revision GitHub sources and context stage.
The existing data-bearing schema tests cover enabled-folding to 0028 and origin
loss to 0029. No migration was changed or new schema test added. Static gates were
rerun after these documentation changes; the full pytest gate reuses unchanged
production/test source.

## Independent review and handoff

Coordinator recorded fresh independent Standards and Spec reviews of Tasks 1-2,
then of the complete first-PR working-tree diff (P3-1/2/3/4/6) against pinned base
`c1d8f59d9ff3f332a3595857dbe612115ad559e8`: **zero actionable findings on each axis**.
The migration backfill wording was clarified before final review. Tasks 1-6 are
complete; commit/push/PR publication is recorded below. P3-5 stays in its own
future worktree/PR, based solely on refreshed main without importing this first PR
diff. No claim is made that all issue AC are complete or that human merge approval
has been obtained.

## Publication

Published and attached to the originating chat as
[PR #152](https://github.com/larchanka-training/dmc-268-api-t6/pull/152), from
`fix/151-pr132-followup` into `main`. A fresh fetch immediately before publication
confirmed base `c1d8f59d9ff3f332a3595857dbe612115ad559e8`; no rebase was needed.
All configured commit hooks passed; no hook bypass was used. The first publication
commit is `8699e7eb59379fd3ee594267c3be1590adf68180`.

The PR was created Draft to trigger CI, with no assigned/requested reviewers as
explicitly requested by the user. At initial publication, Python lint/type/test,
OpenAPI lint, Edge Caddyfile validation and the other started CI checks were still
in progress; no passing remote-CI result is claimed here. The coordinator will
monitor the latest head and mark Ready after required CI passes. Current-head
human approval and resolved review threads remain external merge prerequisites.
The issue stays open and its board status remains In Progress until both
independent PRs are ready. No issue-closing keyword or Development link was added.
