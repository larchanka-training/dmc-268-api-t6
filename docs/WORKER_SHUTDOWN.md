# Worker shutdown and delivery resilience

The Compose `worker` service runs `python -m app.worker` directly. It owns the
`review.run.fast`, `review.publish` and `index.repo` consumers, the leader loop and
its heartbeat. The local and staging worker definitions both set
`stop_grace_period: 40s`. The webhook worker has its own lifecycle.

## First-signal deadline

The first SIGTERM or SIGINT starts a 35-second process deadline and requests stop.
Signal handling is installed before network startup. A signal received before the
asyncio loop is bound is remembered and applied when the loop starts. Repeated
signals neither restart the deadline nor cancel active tasks again.

| Time from first signal | Worker action |
| --- | --- |
| Immediately | Stop new queue intake and leader scheduling; stop the heartbeat. Cancel interrupted startup if consumers do not exist yet. |
| Up to 20 seconds | Drain the active deliveries with their database, broker, HTTP and parser resources available. |
| Up to 30 seconds total | Cancel remaining consumers once and allow their cancellation/lease cleanup to finish. |
| Before 35 seconds total | Close resources after owned work is quiescent. The 20 + 10 second phase budget leaves a 5-second teardown allowance before the absolute deadline. Pending cleanup keeps resources open. |
| At 35 seconds | The independent process backstop exits if shutdown has not returned, including blocked-loop, resistant-task and executor teardown cases. |
| At 40 seconds | Compose's worker grace expires; Docker may send SIGKILL if the process remains alive. |

The deadline stays armed around the entire `asyncio.run` call, including
`Runner.close`, pending async tasks and the default executor. Clean completion
restores the original signal handlers and disarms the deadline only after that
call returns. Forced exit from the SIGTERM deadline is status 143; SIGINT uses
130. A clean signal shutdown returns normally.

The drain/cancel phases apply once consumers exist; startup cleanup is also covered
by the first-signal deadline. These budgets are separate from review attempt
deadlines and index job deadlines.
The process backstop is armed by an OS signal. A library caller that cancels
`run_worker` without a signal still gets cancellation propagation and protected
cleanup; that operation does not arm the signal-owned hard deadline.

## Delivery and lease ownership

Review and index consumers intentionally share stop coordination during shutdown.
With a stop event, both stop intake while their active work drains or is cancelled.
With `stop=None`, index consumption retains its legacy serial behavior.

Stopping intake closes the queue subscription without closing the dedicated
channel beneath an active handler. The SDK returns its internally buffered
messages using the existing broker requeue behavior. A delivery fetched at the
stop boundary but never dispatched stays unprocessed until its dedicated channel
closes, allowing broker redelivery. No handler starts after an observed stop.

An interrupted review keeps its consumer pending until its pipeline stops. Only
then does a fresh short transaction lock the Run, require `running` and expire
the matching worker's lease. State, attempt and error fields are unchanged.
A different owner, terminal Run or publishing Run is preserved. The existing
reconciler performs T12/T13 recovery; a third attempt still becomes terminal
without a fourth database claim. Publishing retains its saved output and existing
idempotent recovery path. Index work uses the existing fenced cancellation lease
release in `BuildRepositoryIndex`.

A pipeline that resists cancellation keeps its lease while cleanup waits. The
worker also explicitly joins owned pipelines that outlive a normal watchdog or
failure outcome before closing resources. Normal watchdog outcomes, retry timing
and counters are unchanged. A forever-pending pipeline reaches the process
backstop with resources still open; cleanup is not reported as committed.

The review consume-level per-delivery guard isolates handler and retry-helper
failures, including ACK/NACK settlement, without nested settlement guards. These
exceptions are logged with queue, message id and traceback. An unprocessed review
delivery gets one best-effort NACK(false); a failure of that settlement is logged
and the consumer continues. Cancellation
propagates. Existing publish-confirm, ACK/fallback-NACK and three-error retry
behavior remains in place. Invalid x-attempt values log a warning and use the
existing fallback. Malformed Retry-After values are ignored; oversized numeric
Retry-After values select the existing 10-minute bucket. Valid representable delays and leading zeros retain their
semantics.

SIGKILL, OOM, an unavailable database or forced process exit cannot promise a
committed lease update. Existing lease expiry, owner fencing and reconciliation
remain the recovery mechanism in those cases.

## Stop and inspect

For a local stack, use the configured service grace without a shorter timeout:

```sh
docker compose stop worker
docker compose logs --tail=100 worker
```

On staging, use the existing deployed Compose project, overlay and env-file
invocation from [CICD.md](CICD.md), stopping only `worker`. Preserve its 40-second
grace; an external shorter stop timeout can prevent application cleanup. Starting
or deploying the worker again uses the ordinary deployment procedure.

Useful log messages:

- `Worker intake stopping; draining active deliveries`: stop reached the supervisor.
- `Worker cleanup deadline exceeded`: tracked consumer/background work passed its
  cleanup allowance; resources remain open for the process backstop.
- `pipeline is still active; interrupted lease retained until quiescence`: review
  cancellation is waiting for its pipeline before lease expiry.
- `Worker joining ... still-active review pipelines`: final resource teardown is
  waiting for a pipeline that outlived a delivery outcome.
- `interrupted lease cleanup failed`: cleanup did not commit; inspect the traceback
  and the persisted lease before relying on reconciler recovery.
- `Failed to handle ... message ...` / `Failed to nack ...`: a delivery or its
  fallback settlement failed. Check subsequent delivery progress and broker health.

The heartbeat is liveness, not review progress. It stops during shutdown. Exit 143
from this signal-owned deadline does not prove cleanup committed; inspect the
persisted Run/index lease and existing reconciler behavior.

## Verification

Fast delivery, header and process checks:

```sh
uv run pytest tests/test_review_consumer_resilience.py tests/test_queue_messages.py tests/test_review_run_worker.py
uv run pytest tests/test_worker_shutdown_process.py
uv run pytest tests/test_deploy_staging_services.py -k worker_stop_grace
```

Set `TEST_DATABASE_URL` and `TEST_RABBITMQ_URL` to disposable local PostgreSQL and
RabbitMQ services before real-stack verification. The fixtures migrate isolated
schemas and may delete/redeclare review broker topology; use test services.

```sh
uv run pytest tests/test_review_shutdown.py tests/test_worker_shutdown.py tests/test_worker_queue_postgres.py
```

With the URLs unset, integration fixtures skip and do not establish broker or
persisted-lease behavior. The actual-stack tests use fake HTTP provider responses;
they do not verify live GitHub or LLM availability. Subprocess tests deliver real
signals and exercise process/Runner teardown separately from those provider mocks.

Run the repository gates before declaring a change complete:

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run pytest
```
