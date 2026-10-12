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

## Round-three follow-up: four P3 findings

Review snapshot `/tmp/pr121-review-round3.json` contains the approved review submitted
`2026-10-12T16:10:38Z`. This follow-up starts from head
`93ab9c7a33ab40a629cf5252796ff6763a90ae28`, already up to date with main
`834dd24c554e62d725060ecbaf19901dc709542e`. Prior evidence above remains historical.
The user has authorized this follow-up through the agent loop.

1. **Sweep-summary correction (small, two files).** In
   `app/modules/integrations/webhooks/application/receive_github_delivery.py`, remove
   `final = wait_expired` and describe the returned flag as ordinary final deferral.
   Keep expiry persistence, WARNING, dispatch status, and retry behavior unchanged.
   First add a failing regression in `tests/test_github_webhook_delivery.py` that
   runs the sweep with an expiring supersession wait and an ordinary final
   repository/installation deferral. Assert the literal summary counts: expiry
   contributes to handled/deferred, but only the ordinary final deferral contributes
   to K (`deferred for good`). Then pass the regression and existing receipt units.
2. **Documentation synchronization (small, two files; after overlap notices).**
   Reflow the supersession paragraph in `docs/WEBHOOK_WORKER.md` around 90–100 columns.
   Explain that absolute receipt age includes worker downtime: recovery may expire
   a still-blocked receipt on its first successful waiting dispatch. Once expired,
   B needs a new push or label event; a current-head `check_suite` has populated
   `ci_status`, so the no-CI sweep cannot rescue it. The old A check-run message
   still promises review for the new commit; expiry provides only a WARNING, with
   no UI/check-run signal. Update `docs/PIPELINE_SPEC.md` §8.2's `synchronize` row
   with the one-hour superseded-blocker wait from receipt and a worker-doc link.
   Keep the mechanism unchanged: no new age origin, `ci_status` reset, alert,
   provider call, dependency, or migration.
3. **Checkpoint, review, publication (depends on 1–2).** Run the four `uv` gates
   with the coordinator's fresh isolated PostgreSQL/RabbitMQ and zero integration
   skips, preserving #131 notification coverage. Review Standards and Spec, then
   update PR #121 evidence/reviewer replies with the published SHA. Preserve
   #121-before-#132 integration order; current-head approval and owner-resolved
   threads remain merge prerequisites.

No genuine ambiguity remains. The coordinator posted overlap notices to
#137/#135/#132/#124 before documentation edits; this planning slice does not post.
Fresh isolated services are PostgreSQL 17 at
`TEST_DATABASE_URL=postgresql+psycopg://app:app@127.0.0.1:55640/app` and RabbitMQ 4
at `TEST_RABBITMQ_URL=amqp://app:app@127.0.0.1:55641/`; use
`UV_CACHE_DIR=/private/tmp/pr121-uv-cache`. `uv sync --locked` is complete.
Review: https://github.com/larchanka-training/dmc-268-api-t6/pull/121#pullrequestreview-5493358282.

### Round-three development results

The sweep-summary regression failed with K=2 instead of the literal expected K=1
(1 failed in 1.19 s), then passed after removing only `final = wait_expired` and
correcting its docstring (1 passed in 0.81 s). Existing receipt unit regressions
passed: 146 passed, 6 integration tests deselected in 0.79 s; the full gates include
integration tests. Both runs reported the same two existing deprecation warnings.
Worker and pipeline documentation now explain the unchanged absolute-age policy
and expiry consequences. Final gates passed: Ruff lint; format check 363 files
unchanged; mypy 360 source files clean; full isolated PostgreSQL/RabbitMQ pytest
2549 passed, 0 skipped, 2 existing warnings in 193.20 s, including #131 notification
coverage. The complete run used authorized escalation after the sandbox denied
localhost access. Gate summary: `/private/tmp/pr121-round3-gates.log`.
The coordinator's independent Standards and Spec reviews each returned zero
findings. The publication package updates existing PR #121 with these results and
a response to all four P3 findings; GitHub records the published SHA and response.
Fresh refs matched the baseline above, so publication uses a normal fast-forward
push. The P2 thread is owner-resolved; approval on the updated head remains a
later merge prerequisite. PR #132 stays last in the integration order.
