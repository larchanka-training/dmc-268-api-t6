# PR #121 review fixes: implementation plan

## Scope and references

[PR #121](https://github.com/larchanka-training/dmc-268-api-t6/pull/121)
implements superseded-Run receipt recovery for
[issue #109](https://github.com/larchanka-training/dmc-268-api-t6/issues/109)
and [issue #106](https://github.com/larchanka-training/dmc-268-api-t6/issues/106).
The [accounting and documentation review](https://github.com/larchanka-training/dmc-268-api-t6/pull/121#pullrequestreview-5493358282)
and [test-adequacy and portability review](https://github.com/larchanka-training/dmc-268-api-t6/pull/121#pullrequestreview-5494979310)
provide the follow-up requirements.

Scope: reset consecutive failures after successful supersession waits, bound
continued waiting, use structured trigger outcomes, retain temporary no-CI
contention, and distinguish ordinary final deferrals from expired waits in sweep
accounting. Preserve existing webhook-to-Run acceptance criteria. No dependencies,
migrations, additional GitHub calls, or #132 installation-signature changes.

## Architecture decisions

- A successful `DEFERRED_RUN_TRIGGER` dispatch atomically resets
  `projection_attempt_count` to zero. Three consecutive genuine dispatch failures
  still finalize a receipt; ordinary repository/installation deferrals retain
  their existing attempt policy.
- A supersession wait retries every 30 seconds without a poll-count ceiling.
  Continued waiting expires when persisted `received_at <= now - 1 hour`,
  including the exact boundary. The token-guarded update marks the receipt failed
  and clears retry, claim, and lease. Repository updates only flush; the use case
  commits and then emits the age-expiry WARNING. Lost claims raise; rollback
  preserves the previous receipt state.
- The age limit applies only when dispatch still reports waiting. If the blocker
  is already terminal during recovery, dispatch may enqueue the new head after
  the deadline. An expired receipt never replays or enqueues later. Pending waits
  resist finished-receipt purge; expired receipts become eligible for retention.
- `WebhookRunTriggerHandler` and `_trigger_result` accept `RunTriggerOutcome`
  only. Structured fakes preserve rendered details and action prefixes. CI events
  with multiple targets retain `any(retry_required)` so a waiting target keeps
  the receipt retryable.
- `SweepNoCi` leaves temporary `DEFERRED` contention selectable for the next
  sweep, even when contention arose after candidate selection. Terminal
  non-enqueue outcomes remain excluded.
- GitHub calls stay outside DB transactions. Enqueue still locks and rechecks
  the candidate, preserving one active Run and durable publication outbox intent.
- Sweep accounting reports N handled deliveries, M deferred dispatch statuses,
  and K ordinary final deferrals. Expired supersession waits count in N and M,
  but never in K.

## Regression requirements

- Signed webhook through production composition and migrated PostgreSQL, with
  GitHub faked: alternating waits/failures reset consecutive attempts and queue
  the new head once after its predecessor completes. Three uninterrupted
  failures remain terminal and cannot be revived by later predecessor completion.
- Persisted receipt age survives restart: waiting just before one hour retries;
  the inclusive boundary fails a still-blocked receipt. An already-terminal
  blocker permits recovery. Commit failure suppresses the expiry WARNING.
- PostgreSQL tests prove reset/expiry are token guarded, durable, flush only,
  and rolled back without a use-case commit. Stale tokens cannot change a
  replacement claim; unfinished waits and expired receipts follow retention rules.
- Preserve restart/redelivery/concurrent replay, waits beyond three polls,
  current head, label, PR lifecycle, repository enablement, live CI, and outbox
  coverage. Obsolete-head CI cannot authorize a newer head.
- Dispatcher tests preserve structured detail/action output and any-target retry
  intent. No-CI contention later enqueues without a head/label change; terminal
  exclusions remain covered.
- One mixed sweep contains one expired wait and two ordinary final deferrals,
  each ordinary receipt starting at attempt count two. Assert replay count `3`,
  literal summary `GitHub webhook sweep: 3 handled, 3 deferred, 2 deferred for good`,
  failed state for the wait, and deferred timestamps for both ordinary receipts.
  All three clear retry/claim/lease and cannot replay. This independently rejects
  returning the expiry flag as the ordinary-final flag.

## Operational consequences and risks

[WEBHOOK_WORKER.md](../WEBHOOK_WORKER.md) and
[PIPELINE_SPEC.md](../PIPELINE_SPEC.md) document the absolute receipt-age limit.
Worker downtime counts toward age; recovery can expire a still-blocked receipt
on its first successful waiting dispatch. Failures or an in-progress dispatch
can delay that poll. At 30-second intervals there are approximately 120 polls
per hour, with the existing two GitHub requests for CI receipts and an additional
PR fetch for PR/label receipts.

Cached current-head `ci_status` excludes the PR from the no-CI sweep after expiry.
A new push or label supplies another launch opportunity subject to eligibility.
The predecessor's check-run still promises review for the new commit; expiry has
no UI/check-run alert and its WARNING is the only failure signal.

| Risk | Required protection |
| --- | --- |
| Reset weakens the genuine-failure ceiling | Three consecutive failures still finalize without replay. |
| Age expiry changes ordinary delivery behavior | Scope it to continued waits; test terminal-blocker recovery. |
| Lost claim or rollback looks like successful expiry | Guard the update and emit WARNING only after commit. |
| Sweep K conflates expiry with ordinary final deferral | Assert asymmetric mixed-sweep counts and terminal states. |
| Related integration changes regress recovery | Preserve notification, race, eligibility, and outbox coverage. |

## Verification and integration order

Use `uv` for Python tools. Run targeted delivery, trigger, dispatcher, and no-CI
regressions, then the four canonical gates:

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run pytest
```

Set `TEST_DATABASE_URL` and `TEST_RABBITMQ_URL` for disposable isolated PostgreSQL
and RabbitMQ services as described in [TEST_PLAN.md](../TEST_PLAN.md). Require
zero integration skips and include #131 notification coverage. Run database
suites sequentially on a database isolated from unrelated workers because
PostgreSQL NOTIFY is database-wide. Keep execution logs, mutation proof, and
current run counts in verification evidence and the PR description.

Before review and merge, synchronize with current `main`. Merge requires green
checks, approval on the current head, and all review threads resolved by their
owners; the tech lead owns issue closure. These remain separate from completed
implementation acceptance criteria in [the checklist](121-review-todo.md).
[PR #132](https://github.com/larchanka-training/dmc-268-api-t6/pull/132) integrates
last and must preserve structured retry intent when rebased.
