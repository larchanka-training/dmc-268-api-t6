# Implementation Plan: PR #121 review fixes

## Scope

Address round-one review at `eb2c02a` for PR #121 (`Refs #109`, `Refs #106`):
stop transient GitHub errors accumulating across successful supersession waits,
bound continued waiting, tighten the trigger protocol, and preserve temporary
contention in the no-CI sweep. Existing webhook-to-Run acceptance criteria remain
the baseline. No dependency, schema migration, or #132 feature is needed.

The user authorized these fixes through the agent loop. Implementation, test-first
regression evidence, all final gates, and both independent reviews are complete.
Publication updates existing PR #121; current-head approval and thread resolution
remain prerequisites for a later merge.

## Architecture decisions

- A successful `DEFERRED_RUN_TRIGGER` dispatch resets
  `projection_attempt_count=0`. Three consecutive dispatch failures still set
  `projection_failed_at`; ordinary repository/installation deferrals retain their
  existing attempt policy.
- Continue polling every 30 seconds while a superseded Run blocks enqueue. If
  another successful dispatch still returns `DEFERRED_RUN_TRIGGER` at or after
  `received_at + 1 hour`, finalize that receipt as failed, clear retry/claim/lease,
  and emit a specific WARNING after the use case commits. The timestamp comes
  from persisted `received_at`, not the latest poll or restart.
- Enforce reset and age finalization atomically in the token-guarded receipt
  update. Pass the use-case clock and deadline cutoff through the port; return
  whether waiting expired. Keep the repository flush-only and retain lost-claim
  exceptions for both waiting and expiry. A rollback leaves the previous claim
  and receipt state intact.
- The age limit applies to continued supersession waiting. Dispatch still
  rechecks current state and may finalize/enqueue when the blocker has become
  terminal, including after delayed worker recovery. This avoids imposing a new
  age policy on all incoming webhook processing. Expired receipts that have
  already finalized never replay or enqueue later.
- `WebhookRunTriggerHandler` and `_trigger_result` accept only
  `RunTriggerOutcome`; update test fakes rather than retain a production union
  for strings/None. Preserve rendered details, action prefixes, and
  `any(retry_required)` for CI events with multiple targets.
- `SweepNoCi` excludes terminal non-enqueue outcomes, but not `DEFERRED`.
  Candidate selection normally filters active Runs; the new behavior also
  handles contention appearing between selection and enqueue.
- GitHub calls remain outside DB transactions; enqueue continues to lock and
  recheck its candidate. No extra GitHub calls or backoff are added.

## Dependency order

1. Rebase onto current main before edits and preserve #131 notification hooks.
   Completed cleanly: base `46a9077`, rebased head `0203fb2`.
2. Write failing receipt/PG regressions, then implement reset and age finalization.
3. Independently tighten the trigger protocol/fakes and fix no-CI sweep behavior.
4. Update worker docs, run targeted and full gates, review the final diff.
5. Update existing PR evidence and reply to reviewer threads with commit SHA;
   thread owners resolve threads. Preserve the existing PR state and reviewers;
   the coordinator handles any later re-request of review.

Steps 2 and 3 may be delegated with file ownership coordination. Their shared
receipt-test fake must have one owner. Do not change a file owned by another open
PR without coordination there; #132 coordination comment already covers dispatch
and full-path fakes. Add coverage to that comment if #132 owns a trigger fake.

## Regression evidence

- Real signed webhook → ReviewsApiResources → migrated PostgreSQL, fake only
  GitHub: wait → failure → wait → failure → wait → failure → A terminal → B queued.
  Assert counter resets after each wait, retries stay durable, no failed receipt,
  no extra webhook, one active Run, and one queued publication for B.
- Three consecutive GitHub failures remain terminal; a later A terminal state
  cannot revive the failed receipt. Test both restart recovery and continued
  waiting beyond three successful polls using existing regression coverage.
- Wait before the one-hour boundary retries; at the boundary it becomes failed
  with no retry/claim/lease, WARNING after commit, no replay, and no B enqueue
  after A completes. Test age from original receipt timestamp, not poll count.
- Receipt-store tests prove reset/expiry roll back without use-case commit,
  replacement tokens cannot be changed by stale owners, and unfinished waits
  remain outside finished-receipt purge. Existing race, current-head, removed
  label, closed PR, disabled repository, live CI, and outbox cases remain green.
- Structured dispatcher fakes preserve details and temporary retry intent;
  sweep `DEFERRED` remains selectable and later enqueues without a head/label
  change. Existing terminal sweep exclusions stay intact.

## Validation and publication conditions

Use `uv` for every Python tool. Record red/green evidence for new regressions,
then run targeted webhook, trigger, projection, and installation dispatch tests.
Run `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy .`, and
`uv run pytest`; integration must run with zero skips. Dedicated disposable
PostgreSQL is on `127.0.0.1:55640` and RabbitMQ on `127.0.0.1:55641` with
test credentials `app/app`; use an isolated Rabbit vhost. PostgreSQL must be
isolated from unrelated workers: #131 NOTIFY is database-wide, not schema-wide.

Update `docs/WEBHOOK_WORKER.md` with consecutive failures, one-hour expiry,
retention eligibility after expiry, and polling cost: approximately 120 polls
per hour, two GitHub requests per CI receipt in always/auto, plus GET PR for
PR/label receipts. The ceiling is reached on the first due successful wait poll
at/after its deadline; worker downtime and dispatch failures can delay processing.

PR body keeps `What` / `Why` / `How to verify` / `Refs`, avoids closing keywords,
and records actual final file/test counts rather than copying the review's merge
tree numbers. Refresh the rebase checkbox. PR #121 lands before #132; #132 must
later preserve its installation keyword and this PR's structured retry intent
when rebasing. Do not incorporate #132 now. Merge requires a current-head
approval, all threads resolved by their owners, current main, and green checks.

## Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Age expiry changes durable receipt lifetime | Scope it to continued waits; test exact boundary, later terminal Run, and purge. |
| Reset accidentally weakens genuine failure ceiling | Prove three consecutive failures still finalize and never replay. |
| Token loss appears as successful expiry | Keep guarded update returning a row and raise if no row matched. |
| Rebase drops #131 notification behavior | Inspect the receipt-store diff and run its PostgreSQL notification suite. |
| #132 touches related protocol/fakes | Coordinate overlap; leave installation-signature changes to its later rebase. |

## Open questions

None requiring user input for the authorized review fixes.
