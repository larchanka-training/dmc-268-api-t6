# Implementation plan: PR #132 review follow-up (#151)

## Status and baseline

Approved by the user on 2026-10-13 under the agent-loop skill. Tasks 1-6 are
implemented, verified and independently reviewed; the first PR is published as
[#152](https://github.com/larchanka-training/dmc-268-api-t6/pull/152), initially Draft
while remote CI runs. Both branches must start solely from refreshed main; the
later refactor must not import this branch.
Issue: https://github.com/larchanka-training/dmc-268-api-t6/issues/151
Baseline: refreshed `origin/main`, `c1d8f59d9ff3f332a3595857dbe612115ad559e8`.
Current worktree: `/Users/qx/.codex/worktrees/151-review-quality/dmc-268-api-t6`.
Current branch: `fix/151-pr132-followup`.
Human approval of this combined plan authorizes both independent PRs; the later
refactor plan records details without another approval gate unless scope changes.

The issue covers six nonblocking P3 findings from final review #132, not the rest
of backlog #106. Production checkpoint wiring is already correct. The first PR
adds its missing regression and fixes documentation (P3-1/2/3/4/6). P3-5 is a
separate future refactor PR from a separate worktree; do not put it on this branch.
No migration, dependency, public contract, queue, or deployment change is planned.

## Evidence and design decisions

### P3-1: test the public worker composition

`compose_worker_process` creates `AttemptCheckpoint` from the claimed run and passes
it to `github.context_stage(trace, checkpoint)`. `review_context_stage_factory`
forwards that checkpoint to `ReviewContextStage`, `GitHubPolicyDocumentSource`, and
`GitHubRevisionSource`. Existing `test_run_policy_context_origin_postgres.py`
constructs the checkpoint and stage directly, so it cannot detect a broken handoff
in `compose_worker_process`.

Add a regression to that test file using real PostgreSQL lifecycle, run/policy/diff
stores, production `compose_worker_process` and `review_context_stage_factory`.
Invoke `process.handle_run.execute(run_id)` on a queued eligible run. Never read or
invoke private `_pipeline`, and never inject a replacement pipeline. Supply test
queue/check-run/review/eligibility adapters and deterministic model output through
supported public composition inputs. HTTP may use `httpx.MockTransport`; the token,
VCS, policy, and revision adapters remain production implementations.

Observe the real stage factory being called only after the database run is claimed
(`running`, claimed worker, incremented attempt). The active control must reach
policy loading and changed/imported context retrieval, with the imported model blob
or persisted context evidence proving the context path ran. A later unrelated
model/output failure must not substitute for proof of successful context execution.

For the revoked case, retire the origin during an actual policy request after
claim, preferably the base-policy root tree response before the nested policy tree
request. Record the HTTP call boundary; assert zero additional policy/context
requests, zero replacement-installation token requests, and cancelled run with
`installation_unavailable`. Use an explicit path/phase marker rather than brittle
global token-call ordinals, since the composed worker also reads the diff/check-run.
Keep independent `FOR UPDATE NOWAIT` probes in HTTP handlers to prove network I/O
occurs outside installation/Repository/Run transactions. The active case must
attempt the request that revocation prevents.

Mutation proof: temporarily substitute an async no-op for **only** the checkpoint
argument supplied by `compose_worker_process` to the context-stage factory. Keep
`CheckpointedProvider` and all origin/persistence guards intact. The revoked-case
request-count assertion must fail because a subsequent policy tree/blob request
occurs; a constructor/type failure or an unrelated model failure is invalid RED
evidence. Restore the exact production wiring and rerun the focused tests green.
Prefer a test-only wrapper around the real stage factory that substitutes the
received callback for the mutation run, avoiding a permanent `app/worker.py` edit.
If that does not prove the exact call-site handoff, use a transient source mutation
only after satisfying the file-ownership rule below; never commit the mutation.

### P3-2: describe the guarantees actually tested

Update `docs/plans/113-review-round8-context.md`. Explicitly call the initial
pre-tree-cache checkpoint and top-level `ReviewContextStage.begin/run` checkpoints
**defense in depth**. The post-cache-await checkpoint and per-token/per-request
checks carry their recorded request-boundary guarantees. Do not imply a separately
proved zero-cache-access guarantee for the earlier checks. Retain historical RED/
GREEN counts and their recorded scope; add #151 composition evidence separately.
No new independent cache guarantee or extra product behavior is required.

### P3-3: migration targets and irreversible user pause

Read-only migration chain is:
`20261010_0030` (Run origin) -> `20261010_0029` (account/deletion/availability) ->
`20261012_0030` (#136 convention policy key) -> `20261011_0029` (policy snapshots)
-> `20261007_0028`.

Correct `docs/CICD.md` rollback section around lines 214-221 and
`docs/SYSTEM_DESIGN.md` installation lifecycle text around lines 662-668:

- Downgrade **to** `20261012_0030` already executes downgrades of
  `20261010_0030` and `20261010_0029`: drops Run origin, `account_external_id`,
  `deleted_at`, and `installation_available`.
- Availability is folded into `enabled AND installation_available`.
  `enabled=true, installation_available=false` becomes `enabled=false`;
  re-upgrade preserves that false value and cannot recover the original preference,
  numeric identity. Retained whole-installation revocations may backfill `deleted_at`
  and the current Repository FK may backfill origin; these do not guarantee recovery
  of the removed historical column values.
- A deeper downgrade **to** `20261007_0028` additionally drops policy documents/
  snapshots and removes non-NULL-policy-key conventions while removing that key.
- Image rollback does not itself downgrade schema. Recovery of removed data needs
  a verified backup; no migration is rewritten or new migration added.

The existing data-bearing test in `test_app_reinstall_schema_postgres.py` targets
0028, while `test_run_origin_postgres.py` covers origin loss to 0029. Read those
for evidence. A new isolated data-bearing regression targeting 0030 may be added
if needed to substantiate re-upgrade preference loss; it must use a new random
schema and literal expected booleans, never a shared database downgrade.

### P3-4: origin fences include policy/AST paths

Edit `docs/SYSTEM_DESIGN.md` §8.4 to explicitly include the protected-write and
request-fence scope. Also align the existing installation guarantee paragraphs in
around lines 626-652 so the two descriptions agree. Locate headings/paragraphs
by content before editing: explicitly list policy
`documents` and `snapshots` among protected writes and policy/AST context tree and
blob reads among protected provider requests. Distinguish recorded historical
payload preservation from new writes requiring active origin and RUNNING. Do not
change the authority rules or claim an AST parser network call exists.

### P3-6: historical round-six evidence

Add an explicit `Historical` notice to each of:

- `docs/plans/113-review-round6-final.md`
- `docs/plans/113-review-round6-todo.md`
- `docs/plans/113-review-round6-plan.md`
- `docs/plans/113-review-round6-integration.md`

Each notice names checkpoint `d88725771ef89a94756ea3a06befd37b44b425d4` on base
`2251b5936822827fc75b38cfc6b352c6a5e89a6b`. Preserve old results as evidence of that
checkpoint, not of current main or the #151 branch. Rewrite present-tense claims
such as current-base/current-head prerequisites where they suggest current status.

## Separate future P3-5 worktree and PR

After the first PR is reviewed/published, create a separate managed worktree from
refreshed main with branch `chore/151-origin-locks`. Record its own plan/todo and
baseline before development. Publish independently: do not wait for a tech-lead
merge and do not copy or cherry-pick the first PR diff into this refactor. If the
first PR has not merged, rerun its composition regression in the first worktree
and the store/lock regression suite in the second; report their distinct heads.

Extract shared lock ordering into a reviews infrastructure helper, likely a new
`app/modules/reviews/infrastructure/run_origin_locks.py`. Preserve the sequence
installation `FOR SHARE` -> Repository `FOR SHARE` -> Run `FOR UPDATE`, the source
installation join, NULL-origin behavior, queries/row refresh, origin/running guards,
exception codes, and each caller's transaction/commit boundary. The helper must
not own transactions, commit, invoke network calls, or merge caller-specific guards.

Known callers to consolidate:

- `SqlAlchemyPolicySnapshotStore._lock_active_run`: used by `stage_documents` and
  `create_snapshot`; retain `populate_existing=True`, active-origin and RUNNING checks.
- `SqlAlchemyRunRepository.store_diff_snapshots`: retain revision binding checks,
  `populate_existing=True`, active-origin/RUNNING guards, and immutable snapshots.
- `SqlAlchemyRunLifecycleStore`: `_lock_provider_rows` precedes Run locks in
  `lock_for_claim`, `finish`, `requeue`, `complete_publication`, and
  `retire_stale_origin` (five lifecycle sites).
- `SqlAlchemyRunTraceStore.save_context_payload`: one trace-store site in the same
  `run_lifecycle_store.py` module. Together with the policy guard and diff writer,
  the extraction covers eight sites.

A typed helper can accept the caller's existing final Run select, acquire the
shared installation/Repository locks, then execute that select with `FOR UPDATE
OF runs`; retain caller-specific selected columns and refresh options. Confirm
strict mypy compatibility before settling the signature. Do not broaden the
refactor to unrelated cancel/rerun locks or OAuth account/rebind transaction logic.

Re-run real PostgreSQL guards, diff/context/publication retirement tests and
concurrent rebind tests. In particular preserve
`test_policy_writer_waits_for_rebind_and_rejects_after_it_commits`,
`test_policy_writer_holds_provider_share_locks_while_waiting_for_run`,
`test_claim_holds_source_and_repository_before_waiting_for_run_lock`,
`test_source_replacement_while_claim_waits_refuses_old_work_but_new_origin_starts`,
and diff writer waiting/row-refresh regressions. They prove blocking with
`pg_blocking_pids`/NOWAIT, not just elapsed time. Run the composition regression
again in the first worktree if not yet on main. Publish this refactor as its own PR referencing #151.

## Execution order and verification

1. Obtain plan approval; recheck open PR ownership and post the required concise
   coordination comments to #124/#148 before editing their owned documentation.
2. Establish a dedicated PostgreSQL test database or disposable random schemas;
   do not use the unrelated PostgreSQL service on host port 5432. Coordinator saw
   PostgreSQL 17 test candidates on 5434/5433, but verify ownership and isolation
   before using either. Do not read or print secrets. Use fixture-generated random
   schemas (`portal_schema`) and their teardown, never downgrade a shared schema.
3. Before any implementation, set `TEST_DATABASE_URL` for the disposable test target
   and run the baseline command below with **zero skipped tests**. Record exact
   revision, counts, duration, and environment availability without credentials.
4. Implement the public composition regression; produce mutation RED and restored
   GREEN evidence. Existing production behavior should require no source fix.
5. Correct P3-2/3/4 docs; verify factual claims against source and regression evidence.
6. Mark four P3-6 documents Historical; run gates and fresh Standards/Spec review.
7. Publisher opens the first PR; attach it to the chat. Refactor P3-5 follows only
   in its separate worktree/PR with its own review/gates. Tech lead closes the issue.

Required baseline before code:

```sh
uv run pytest tests/test_run_policy_context_origin_postgres.py tests/test_run_policy_snapshot_origin_postgres.py tests/test_run_origin_guard_postgres.py -rs
```

First PR focused verification also covers existing context stage/source behavior:

```sh
uv run pytest tests/test_run_policy_context_origin_postgres.py tests/test_run_policy_snapshot_origin_postgres.py tests/test_run_origin_guard_postgres.py tests/test_review_context_stage.py tests/test_github_policy_source.py tests/test_github_revision_source.py -rs
```

Refactor focused verification adds `tests/test_run_diff_origin_postgres.py`,
`tests/test_run_context_origin_postgres.py`, `tests/test_run_publication_origin_postgres.py`,
`tests/test_run_origin_postgres.py`, and `tests/test_app_reinstall_postgres.py`.
Migration-proof runs use `tests/test_app_reinstall_schema_postgres.py` and
`tests/test_run_origin_postgres.py` on disposable schemas only.

For each PR, required final gates:

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run pytest
```

Configure dedicated RabbitMQ for any full-suite broker integrations (they reset
review topology); never point them at another session's broker. Run with PostgreSQL
configured and report any unavailable broker or other intentional skips explicitly.
Focused PostgreSQL acceptance regressions must never be counted as passing if skipped.
Do not claim all acceptance criteria met until the separate P3-5 PR is published.

## Ownership, assumptions, and blockers

Open PR file lists checked read-only on 2026-10-13: #148 owns `app/worker.py` and
`docs/SYSTEM_DESIGN.md`; #124 also owns `docs/SYSTEM_DESIGN.md`; #133 owns policy/
review application files but none of the proposed store/helper files. Recheck before
each edit. AGENTS.md forbids editing another open PR's files without a comment
there. After plan approval the coordinator posts concise coordination comments to
#124/#148 as required preparation under that user instruction, then performs the
authorized edits. This is preparation, not another user approval gate. Avoid worker
source changes, including transient mutation edits; use a test-only mutation wrapper.

The planning phase ran no PostgreSQL baseline or mutation experiment. Implementation
subsequently established dedicated PostgreSQL/RabbitMQ, a zero-skip baseline and the
public-composition mutation proof; see [151-verification.md](151-verification.md).
The plan gate is satisfied. Both independent branches must start solely from
refreshed main, and the P3-5 refactor must not import the first PR diff. No further
product-scope decision or approval gate is needed unless the authorized scope changes.
