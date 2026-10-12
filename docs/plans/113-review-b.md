# PR132 developer B lifecycle evidence (#113)

Worktree: `<worktree>`.
Branch/base supplied by coordinator: `fix/113-pr132-review`, rebased
`69505ab` / `46a9077`. No Git mutation or dependency change by B.

## Behavior and decisions

- Queued claim carries origin availability separately from user preference:
  retired, mismatched or NULL origin is `cancelled/installation_unavailable`;
  an active paused repository remains `skipped/repo_disabled`.
- Reconciler discovers inactive-origin stale queued/running/publishing IDs without
  building replacement provider messages. Each candidate is cancelled in a separate
  short UOW, rechecking origin, state and queue age/lease expiry under provider and
  Run locks. This prevents a multi-origin Run-ID batch from crossing OAuth's global
  installation order. Existing provider projections remain fail closed.
- Retry and final failure writes fence installation/repository first, then Run.
  Retired work cancels instead of becoming an orphaned QUEUED row or entering DLQ.
  State/worker ownership checks retain displaced-worker protection. Retry cancellation
  uses actual time, never the future retry available_at.
- Installation/repository fences use FOR SHARE in that order; Run retains exclusive
  write locking. Publishing finish without worker ownership retains its existing
  contract even before lease expiry; retirement reconciliation itself checks expiry.
- Check-run RunCancelled emits INFO with Run ID and reason. Every provider request
  checkpoint is retained; all transactions end before provider/queue network calls.

## Red-green proof

Real PostgreSQL uses `TEST_DATABASE_URL=<dev-db>`.

1. Queued delivery retired/rebound/NULL origin: 3 failed, 3 terminal-history controls
   passed before correction; then all 18 claim/projection cases passed.
2. Stale queued/running/publishing × retired/rebound/NULL: 9 failed before discovery
   correction; then all passed with repeated reconciliation and no queue messages.
3. Catalogued retryable worker failure after retirement: 3 failed (persisted QUEUED),
   then passed. Added last-attempt and nonretryable variants: 9 failed (failed/DLQ),
   then all 12 passed as cancelled/ACK with no retry publication.
4. Deterministic provider lock test holds Run exclusively and waits until first
   worker reaches its Run SELECT. Original provider FOR UPDATE causes independent
   worker to fail with PostgreSQL 55P03; FOR SHARE permits second worker and actual
   Run FK insertion while provider/repository NO KEY UPDATE NOWAIT remain blocked.
   Same proof covers snapshot writer; mutating its SHARE back to UPDATE fails 55P03.
5. Reconciler/callback opposite candidate order test uses events and NOWAIT:
   one-UOW-across-origins mutation fails 55P03; per-origin UOW passes and terminalizes
   both Runs without publishing. No chance scheduling or sleeps.
6. Additional controls preserve unexpired/terminal retired rows byte-for-byte and
   deny displaced-worker finalization; direct retry cancellation finished_at lies
   between actual before/after timestamps despite a ten-minute future retry.

## Files owned by B

`app/modules/reviews/application/{handle_review_run,reconcile_runs}.py`,
`app/modules/reviews/infrastructure/{run_lifecycle_store,run_repository}.py`,
`tests/{test_run_origin_guard_postgres,test_reconcile_runs,test_review_run_worker}.py`.
C exclusively performed/restored the context-origin guard mutation and updated the
VCS snapshot test watcher to FOR SHARE. No shared todo edits by B.

## Verification

Focused lint, formatting and mypy on all seven assigned source/test files passed.
Earlier worker/reconciler/origin focused run: 93 passed in 25.71s.
Final focused run including VCS/publication PostgreSQL coverage: 156 passed,
zero skips, two existing Starlette/httpx deprecation warnings, in 58.25s.
Whole-plan gates remain coordinator responsibility after all developers finish.
