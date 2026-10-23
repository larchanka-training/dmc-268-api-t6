# Implementation Plan: worker consumer resilience (#185)

Status: approved by the user on 2026-10-20, including the shutdown budgets,
scoped lifecycle policy and required coordination comments. All seven implementation
tasks and the CI fixture follow-up are complete. Published as ready-for-review
[PR #234](https://github.com/larchanka-training/dmc-268-api-t6/pull/234) from
`fix/185-review-consumer-resilience`; the user assigns the reviewer.

Historical verification on the previous head includes clean independent reviews,
all four local gates, 5045 actual-PG/broker tests with zero skips, and green hosted
CI on `84ab17dd3b46be3a49693a88d10c1cf2a0a813f6`. The 2026-10-22 review follow-up
rebases onto current main and clarifies comments/docs without runtime changes;
its current-head review and verification are recorded separately from that evidence.

## Evidence and scope

Source: [issue #185](https://github.com/larchanka-training/dmc-268-api-t6/issues/185).
Initial baseline on 2026-10-20: `2326273e6ab7fcea89d1cf3b94b88a4be66ca5e9`.
The review follow-up rebased onto main `8a621798d01bd72a5ce6c343addc0d00661af724`
on 2026-10-22.
Work only in the managed `185-review-consumer-resilience` worktree. The original
checkout, its `fix/111-worker-portal-url` branch, and unrelated untracked docs are excluded.

Read: AGENTS.md, agent rules/skills, SYSTEM_DESIGN §7.3–7.4, PIPELINE_SPEC §4,
TEST_PLAN, current worker/AMQP/VCS/index code, queue tests and deployment configuration.

Current behavior matters:

- Review handlers catch application errors, but their final ACK/NACK and the retry
  exhaustion NACK can escape. `consume()` awaits a handler without a delivery guard;
  its failure cancels every sibling in the worker's TaskGroup.
- `republish_with_attempt()` already guards publish, fallback NACK and post-confirm
  ACK failures. Preserve its success/failure result and ordering; add regressions for
  every settlement path rather than assuming this helper needs rewriting.
- `_get_delivery_attempts()` omits `OverflowError`; the index-oriented
  `current_unexpected_attempts()` already catches it.
- VCS classification already ignores ordinary nonnumeric Retry-After using
  `isdigit()`. Unicode digit characters, Python's integer digit limit and timedelta
  overflow remain hazards. Ordinary invalid values need regression tests too.
- `run_worker()` delegates signal handling to its caller, but `main()` installs none.
  Local and staging worker Compose services define no `stop_grace_period`; deploy
  and rollback scripts do not override the stop timeout (Docker's default is 10 s).
- Index cancellation already performs a fenced, shielded lease release with no error
  code; `test_a_cancelled_job_releases_its_lease` proves it. Reuse this path. Review
  cancellation currently stops its pipeline/watchdog but leaves the running lease.

## Acceptance criteria versus operational constraints

Issue acceptance criteria:

1. An ACK/NACK failure for one review delivery cannot stop other worker consumers;
   include an ACK failure on a closed/replaced channel.
2. `x-attempt=float('inf')` is invalid input, logs a warning and follows the existing
   retry algorithm without raising OverflowError.
3. SIGTERM stops new deliveries; current work finishes or its lease is released
   before the stop deadline; cover this with tests.
4. Nonnumeric and enormous Retry-After values cannot crash the review path.
5. Existing index.repo behavior/tests remain valid.

Operational constraints (also mandatory, but not new product features):

- Keep `runs.attempt` increment-on-claim, maximum 3, and AMQP's separate unexpected
  error counter, maximum 3. Keep local fallback TTL/cap, confirmed handoff ordering,
  x-death semantics and retry queues/delays (30 s / 2 min / 10 min) unchanged.
- Never add `nack(requeue=True)` to a delivery guard. The existing publish-failure
  fallback inside `republish_with_attempt()` remains; it is not a new guard action.
- Cancellation must propagate; guards catch `Exception`, not `BaseException`.
- Preserve index routing, retries, fencing, parser ownership and settlement guard.
  Only add an opt-in lifecycle stop signal to its intake loop.
- No new dependency, HTTP endpoint, DB schema/migration, AMQP schema or Postman
  resource is needed. Keep network calls outside database transactions.

## File ownership and coordination before implementation

Open PR files were checked via GitHub on 2026-10-20; recheck after plan approval.
Neither plan artifact overlaps an open PR. No PR comment is needed to write these plans.
AGENTS requires a comment on each affected open PR **before** editing its files:

| Open PR | Relevant overlap | Safe development resolution |
| --- | --- | --- |
| [#220](https://github.com/larchanka-training/dmc-268-api-t6/pull/220) | reviews/amqp.py, handle_review_run.py, app/worker.py, tests/test_review_run_worker.py, tests/test_worker.py, tests/test_worker_queue_postgres.py, lifecycle store and shared specs | Coordinate exact delivery guard, cancellation hook and composition hunks; retain GitLab behavior. Prefer new shutdown modules/tests. Rebase if merged; reread changed handlers before implementing. |
| [#228](https://github.com/larchanka-training/dmc-268-api-t6/pull/228) | bootstrap/code_index_worker.py, app/worker.py, tests/test_code_index_worker.py, tests/test_worker.py, README.md, shared specs | Coordinate only optional stop-intake plumbing; leave index guard/build/retry policy intact. Prefer a new worker runbook. |
| [#227](https://github.com/larchanka-training/dmc-268-api-t6/pull/227) | app/worker.py, docker-compose.yml, shared specs | Coordinate supervisor/main and worker stop_grace_period hunks. |
| [#230](https://github.com/larchanka-training/dmc-268-api-t6/pull/230) | app/worker.py | Coordinate lifecycle wiring; do not change reflection/provider pipeline wiring. |
| [#229](https://github.com/larchanka-training/dmc-268-api-t6/pull/229) | publication/lifecycle code and shared specs | Avoid its files with separate shutdown adapter/runbook; coordinate if actual implementation expands into them. |

The proposed implementation scope includes posting these required coordination
comments after the user approves this plan and before overlapping edits. No comments
are posted during planning. Refresh file lists and baseline after intervening merges.

## Architecture decisions

### Delivery isolation

Keep application-error retry behavior inside the existing review handlers. Add a
last-resort per-delivery guard in review `consume()`, covering decode rejection,
handler invocation, retry helper invocation and final ACK/NACK. Log queue name,
message id and traceback without body/customer data. If the message is already
processed, do not settle it again. Otherwise make at most one best-effort
`nack(requeue=False)` to prevent an open channel with prefetch=1 from stalling;
catch/log a failure of that NACK too. A closed channel leaves redelivery to RabbitMQ.
Do not send a successfully confirmed handoff back through application retry counting.

Regression coverage explicitly includes both handlers and their invalid-message,
ordinary completion and DEAD_LETTER paths; `_requeue_after_error()` exhaustion and
republisher errors; and `republish_with_attempt()` publish, ACK and fallback NACK
failures. Change helpers only where a red test demonstrates missing isolation.

### Safe headers

Add OverflowError to the existing invalid x-attempt handling. Keep the live local
fallback count, max(header, local)+1 calculation, and budget unchanged. Test both
review queues and shared index regressions.

Parse Retry-After as ASCII nonnegative decimal seconds with bounded work before
`int()`/`timedelta()`. Ignore malformed/unsupported values, including HTTP-date
values, as the current implementation does. Preserve valid representable delays;
saturate oversized numeric values at a safe finite long-delay value (10 minutes),
which still selects the existing 10-minute queue and marks a limited 403 as limited.
Do not turn a syntactically numeric but huge rate-limit into github_forbidden.
Test values exceeding timedelta range and >4,300 digits without converting the
entire number; also empty, alphabetic, negative, Unicode-digit, zero, 120 and 300.

### Stop intake separately from current delivery

Add a shared, small infrastructure consumer runner around `queue.iterator()`.
Its concrete stop primitive is closing the active iterator (RabbitMQ basic.cancel).
The runner owns one receive task, one active handler, a stop waiter and one close
task. Do not overlap external close with a pending `__anext__`: aio-pika 10.1's
robust iterator can otherwise re-consume while `_consumer_tag` is absent. During an
idle stop, cancel receive once, shield-await its own cancellation/close cleanup,
then close explicitly after it terminates. During active work there is no pending
receive; close intake while the handler drains. Check stop before iterator entry,
after subscription, before each receive and immediately before invoking a handler.
A fetched but undispatched stop-tie remains unprocessed until its dedicated channel
closes, so it cannot enter the delivery-error DLQ guard. The SDK returns internal
buffered deliveries on close; active yielded work remains owned by its handler.
Always retrieve/clean up receive, handler, stop and close tasks, avoiding repeated
cancellation of a receive already cleaning up. Iterator/close failures remain visible.
Do not close the AMQP connection/channel or cancel the active handler at this first
stage. Later supervision bounds cleanup and resource teardown.

Both review `consume()` and `IndexConsumer.consume()` accept an optional stop event;
without it their existing serial behavior remains. The index delivery guard body
remains unchanged. Test actual buffering/basic.cancel semantics and cancellation-in-
flight against the disposable local RabbitMQ during Task 3, alongside event-driven
fake tests; final Task 7 adds full worker/lease composition proof. No live upstream
provider call is part of this evidence.

### Bounded SIGTERM lifecycle

Use a process-level stop event registered for SIGTERM in `main()`; SIGINT may use the
same path. Pass it into `run_worker()` so tests can trigger shutdown without sending
signals to pytest. Unregister signal handlers at exit. Keep unexpected fatal startup
or iterator failures visible rather than silently restarting them in a tight loop.

Proposed fixed budgets: 20 s to drain current deliveries, 10 s for cancellation and
lease cleanup, 5 s for closing resources; a 35 s application ceiling with 40 s
`stop_grace_period` in both worker Compose services. These are independent of review
attempt and index job deadlines. Tests inject shorter budgets; no new env surface.

On signal: stop intake on all three queues and stop scheduling leader work, let
active handlers finish during drain, then cancel only those still active once.
Wait for existing index cancellation cleanup while DB/AMQP/parser clients are still
open. Keep required cleanup shielded from the initial cancellation, but bounded by
the cleanup budget; do not issue repeated task.cancel() calls that cut lease cleanup.
Close connection and other resources only after those tasks/cleanup have finished.

For a claimed review interrupted after drain, use a private per-execution cancellation
context and the existing lifecycle UoW. Once the pipeline has stopped, expire only the matching worker's still-running
lease in a short committed transaction. Preserve state/attempt/error fields and
let the existing reconciler perform T12/T13 (including the third-attempt terminal
outcome); do not directly requeue, decrement attempts or add an error code. Cover
cancellation between claim commit and initial check-run update as well as during
pipeline execution. A changed owner, terminal run or publishing run is not modified.
Publishing has no running lease to release: leave its durable saved output and
existing idempotent reconciliation path intact.

Prefer existing lifecycle operations over a new abstraction. `extend_lease()` is
owner-fenced but accepts both RUNNING and PUBLISHING, so it cannot be blindly used
for a running-only cleanup. Task 4 TDD proved safe reuse under the existing Run
row lock held through commit; no store/port/fake contract changes are needed.
This implementation detail
does not alter the approved owned-running-only behavior. Index BuildRepositoryIndex
already owns its fenced cleanup; do not duplicate or change its job implementation.

Bounded exit must include event-loop teardown: asyncio.run can wait on outstanding
cancellation-resistant tasks/default-executor work. Implement a process-level deadline
backstop in the entrypoint, armed only after shutdown begins and removed on clean
exit, to enforce the 35 s ceiling after best-effort cleanup. Keep it injectable for
tests; subprocess tests must include a parked task/executor case. A blocked event
loop, unavailable database, SIGKILL or OOM cannot promise committed cleanup; log
failure where possible and retain existing lease-expiry/fencing recovery. Never
expire a review lease while a still-running pipeline could continue making writes.
The initial 5-second pipeline cancellation wait is not proof of quiescence: retained
per-execution cleanup keeps its consumer pending and worker resources open until the
pipeline actually stops, then expires the owned running lease. If it never stops, the
independent process deadline exits with the existing lease retained for reconciliation.
An instance-local set also owns pipeline futures that outlive normal watchdog/failure
returns. The explicit lifecycle join runs after the consumer TaskGroup ends and before
resource contexts close, protected through caller cancellation. This preserves ordinary
deadline/failure/retry timing while keeping resources available beneath pending writers;
no global task scan or constructor cleanup hook is needed.

## Ordered implementation and verification

The implementation followed this order:

1. Isolate review delivery settlement failures.
2. Safely parse retry-related transport headers.
3. Add stop-aware intake for reviews and index.
4. Release an interrupted review's owned lease without changing retry semantics.
5. Wire bounded SIGTERM lifecycle and resource teardown.
6. Align local/staging stop budgets and document operation.
7. Prove cross-consumer isolation and shutdown against existing real-stack fixtures.
   The actual closed-channel ACK test additionally proved that review consumers
   obtained with `ensure=False` were absent from robust channel restoration.
   Task 7 therefore includes the coordinated narrow passive `ensure=True`
   registration fix plus its integration proof (two files); index policy is unchanged.

Each task starts with a failing public-boundary pytest test, then minimal code,
focused verification and independent Standards/Spec review. Use plain pytest with
`asyncio.run`, fake ports and deterministic synchronization events, not long sleeps.
Every completed implementation task must pass `uv run ruff check .`,
`uv run ruff format --check .`, `uv run mypy .`, `uv run pytest`.

Existing transport assets: contracts/openapi.yaml; JSON schemas/examples for
review.run, review.publish and index.repo; tests/test_contract_schemas.py,
tests/test_queue_messages.py and tests/test_worker_queue_postgres.py. None of these
wire contracts changes. Reuse existing PostgreSQL/RabbitMQ integration fixtures and
httpx.MockTransport provider fakes; do not create Postman workspaces, mocks or
collections for an internal AMQP/process lifecycle fix. Run real integration tests
with disposable TEST_DATABASE_URL and TEST_RABBITMQ_URL; distinguish actual passes
from skips, and report unavailable infrastructure as a validation gap.

## Risks and limits

| Risk | Mitigation/evidence |
| --- | --- |
| ACK fails after successful publication; two copies exist | Preserve existing confirm-before-ACK/idempotency and retry memory semantics; do not recount settlement as handler failure. |
| Stop races with prefetch/buffered deliveries | Stop flag check before dispatch; actual broker test proves no new job starts and active delivery is not prematurely settled. |
| Cancellation lands after claim but before pipeline | Cleanup hook encompasses all post-claim work, with owner fencing and no mutation of publishing/terminal rows. |
| Executor or cancellation-resistant work prevents Python exit | Explicit process deadline backstop and real subprocess exit test; no early lease release while work still writes. |
| Cleanup database call hangs or fails | Bounded cleanup, observable failure, existing fenced lease expiry; do not claim recovery was committed when it was not. |
| Collision with GitLab/RAG/reflection work | Mandatory PR coordination before edits, narrow modules/tests, refresh baseline and review after rebase. |
| CI integration tests silently skip | Record real PostgreSQL/RabbitMQ run evidence; all unit gates alone are insufficient proof of broker shutdown. |

## Approval and completion

The user approved the 20/10/5-second budgets plus 40-second Compose grace,
saturation of oversized numeric Retry-After into the existing long bucket, and owned
review-lease expiry using the unchanged reconciler on 2026-10-20. Required scoped PR
coordination comments were also approved. No product decision blocks this plan.

After approval, implement/review tasks sequentially where files overlap; update the
todo with evidence. Rebase on main before review/publishing, run all gates, obtain
zero Standards/Spec findings, and publish per the agent-loop/pull-request skills.
Issue/board/merge lifecycle remains subject to repository rules; no closing keyword
or Development-panel link. Planning has not run implementation gates or live tests.
