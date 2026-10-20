# #185 implementation checklist

Status: plan approved by the user on 2026-10-20. Prerequisites and all seven
implementation tasks are complete. Final whole-issue Standards/Spec reviews report
zero findings; all four gates pass, including the actual PostgreSQL/RabbitMQ full
suite: **5045 passed, 0 skipped, 2 existing warnings in 1006.98s**. Publisher actions
remain pending; PR must be ready for review and the user will assign the reviewer.
Read [185-plan.md](185-plan.md) for architecture, acceptance criteria, ownership and risks.
All paths below are relative to the isolated managed worktree.

## Prerequisite: approval and ownership coordination

- [x] User approved the saved plan, shutdown budgets, scoped lifecycle policy and
      required coordination comments on 2026-10-20.
- [x] Fetched origin/main and refreshed all open PR file lists on 2026-10-20.
      Worktree baseline and rebased branch HEAD: `2326273e6ab7fcea89d1cf3b94b88a4be66ca5e9`;
      branch `fix/185-review-consumer-resilience`. Original checkout untouched.
- [x] After plan approval and before overlapping edits, posted the planned coordination
      comments on #220, #228, #227 and #230 with exact affected files/hunks. Coordinate
      #229 too if its lifecycle/publication files become necessary. Posted links:
      [#220](https://github.com/larchanka-training/dmc-268-api-t6/pull/220#issuecomment-6291784586),
      [#228](https://github.com/larchanka-training/dmc-268-api-t6/pull/228#issuecomment-6291785162),
      [#227](https://github.com/larchanka-training/dmc-268-api-t6/pull/227#issuecomment-6291785793),
      [#230](https://github.com/larchanka-training/dmc-268-api-t6/pull/230#issuecomment-6291786425).
      Task 1 does not touch #229-owned files; coordinate before Task 4 if needed.
- [x] Read AGENTS, agent README/rules and tdd skill/references before tests/code.
      Public seams already confirmed by the approved plan. No dependency/migration added;
      isolated `.venv` installed with `uv sync` from the existing lockfile.

## Task 1: Isolate review delivery settlement failures

**Description:** Keep a failed review delivery inside its consumer while preserving
existing application retry/handoff behavior. Cover both review queues and helpers.

**Acceptance criteria:**

- [x] RED tests exercise a failed final ACK on a closed channel, failed rejection/DLQ
      NACK, retry exhaustion NACK and unexpected retry-helper exception; the next
      message is processed and sibling consumers stay alive.
- [x] GREEN delivery guard logs Exception, attempts only best-effort NACK(false) for
      unprocessed deliveries, never adds NACK(true), and propagates CancelledError.
- [x] Existing republish publish/ACK/fallback-NACK behavior and three-error budget
      stay unchanged; no extra settle after an already processed delivery.

**Verification:** `uv run pytest tests/test_review_consumer_resilience.py tests/test_queue_messages.py tests/test_code_index_worker.py`; full gates below; reviewer checks Standards/Spec.

**Dependencies:** Prerequisite.

**Files likely touched:** `app/modules/reviews/infrastructure/amqp.py`;
`tests/test_review_consumer_resilience.py` (new). Existing helper tests are reused.

**Estimated scope:** Small, 2 files.

**Evidence (2026-10-20):**

- RED: `uv run pytest tests/test_review_consumer_resilience.py -q` before the guard:
  2 failed, both real `aio_pika.IncomingMessage` final ACKs raised
  `ChannelInvalidStateError` through `consume()`/TaskGroup (run and publish).
- GREEN first slice: same command, 2 passed. Added settlement/helper/cancellation
  regressions afterward; final resilience file: 16 passed in 0.47s. A deterministic
  event keeps the sibling pending until the next delivery has completed.
- Focused: `uv run pytest tests/test_review_consumer_resilience.py
  tests/test_queue_messages.py tests/test_code_index_worker.py -q`:
  113 passed in 18.07s. Existing three-error retry/index tests unchanged.
- Gates: `uv run ruff check .` passed; `uv run ruff format --check .` passed
  (519 files); `uv run mypy .` passed (516 source files); `uv run pytest`:
  **4041 passed, 863 skipped, 2 deprecation warnings in 541.76s**. One overlong
  log line was corrected before the final lint pass. Warnings are existing
  FastAPI/Starlette TestClient deprecations. Database/broker/live-provider fixture
  URLs were unset; skipped integrations are not claimed as live-stack proof.
- Independent Standards: 0 findings. Independent Spec: 0 findings, confirmed by
  coordinator after the final source/test edits. No commit/push/publication.
- Implementation: `consume()` guards delivery exceptions only, logs queue/message
  id and traceback, checks SDK processed state and attempts one NACK(false), logging
  any failure. Helpers and retry counters/headers/queues/index code are untouched.

## Task 2: Safely parse retry-related transport headers

**Description:** Bound malformed retry metadata without changing retry counting or
queue selection for valid input.

**Acceptance criteria:**

- [x] RED→GREEN tests prove x-attempt=inf logs invalid-header warning on both review
      delivery paths; live local count, normal headers, x-death and third failure
      retain current semantics.
- [x] RED→GREEN tests cover invalid/Unicode/negative/empty Retry-After, ordinary
      numeric values, timedelta overflow and >4,300 digits; no parsing exception
      escapes and huge numeric values select the existing 10-minute rate-limit bucket.
- [x] 404/nonlimited 403 remain github_forbidden; limited 403/429 retain appropriate
      retryable codes, including commit-message reads; index regression suite passes.

**Verification:** `uv run pytest tests/test_queue_messages.py tests/test_review_run_worker.py tests/test_code_index_worker.py`; full gates; reviewer checks Standards/Spec.

**Dependencies:** Task 1 (shared AMQP file; implement sequentially).

**Files likely touched:** `app/modules/reviews/infrastructure/amqp.py`;
`app/modules/reviews/infrastructure/vcs_failures.py`; `tests/test_queue_messages.py`;
`tests/test_review_run_worker.py`.

**Estimated scope:** Medium, 4 files.

**Evidence (2026-10-20):**

- Posted additional exact Task 2 test/header coordination before edits:
  [#220](https://github.com/larchanka-training/dmc-268-api-t6/pull/220#issuecomment-6292055025),
  [#228](https://github.com/larchanka-training/dmc-268-api-t6/pull/228#issuecomment-6292055516).
- RED `uv run pytest tests/test_queue_messages.py -q -k infinite_retry_header`:
  2 failed at `int(inf)` in both review handlers. Minimal catch change, same command:
  2 passed (61 deselected). Public handler tests preserve x-death diagnostics.
- RED `uv run pytest tests/test_review_run_worker.py -q -k oversized_numeric_retry_after`:
  4 failed (timedelta overflow and 5,000-digit int limit, metadata and commit reads).
  Minimal bounded parser, same command: 4 passed (70 deselected).
- Additional Unicode failure proof replayed the final new tests against a clean
  `git archive HEAD` baseline snapshot in `/tmp`, with the isolated locked venv:
  `uv run --no-sync pytest tests/test_review_run_worker.py -q --tb=short
  -k 'malformed_retry_after and (superscript-digit or unicode-decimal)'`:
  4 failed (superscript `ValueError`; Arabic decimal wrongly treated as a rate-limit).
  This baseline replay followed the initial oversized-header RED/GREEN slice;
  production worktree source was never reverted or swapped during verification.
- Final focused `uv run pytest tests/test_queue_messages.py tests/test_review_run_worker.py
  tests/test_code_index_worker.py tests/test_review_consumer_resilience.py -q`:
  **245 passed in 2.42s**. Added live-local-count/third-error, malformed/Unicode,
  ordinary and leading-zero values (including 5,000 zeros), largest representable
  seconds, 403/404/429, and exhausted-rate-limit regressions. Provider evidence uses
  real HTTP adapter/decorator with `httpx.MockTransport`; no live GitHub claim.
- `uv run ruff check .` and `uv run ruff format --check .` passed (519 formatted files);
  `uv run mypy .` passed (516 source files). Initial extra import blank line corrected
  with Ruff before final source/test freeze and independent review dispatch.
- Full `uv run pytest`: **4103 passed, 863 skipped, 2 existing deprecation warnings
  in 696.51s**. Integration URLs were unset; skips do not count as real-stack proof.
- Independent Standards: 0 findings; independent Spec: 0 findings after final
  source/test freeze. No dependency, HTTP/AMQP contract, migration or Postman change.
- Refetched origin/main and refreshed Task 3 file lists before proceeding:
  HEAD/origin/main still `2326273e6ab7fcea89d1cf3b94b88a4be66ca5e9`; only #220
  owns reviews AMQP and #228 owns code_index_worker, covered by posted comments.

## Checkpoint: delivery isolation

- [x] Tasks 1–2 reviewed with zero Standards/Spec findings and full gates green.
- [x] Confirmed helper coverage includes failed ACK and fallback NACK; compared retry
      counts/headers/queues to unchanged specification.

## Task 3: Stop intake while retaining active delivery work

**Description:** Add optional stop-event plumbing to review and index intake using
a shared consumer runner that owns receive, handler, stop and close tasks. Never
close concurrently with a pending `__anext__`: idle stop cancels/awaits receive once
before explicit close; busy stop closes intake while active work drains. A fetched
but undispatched tie stays unprocessed until its dedicated channel closes. Early
real RabbitMQ tests prove iterator buffering/cancellation semantics.

**Acceptance criteria:**

- [x] RED→GREEN event-driven tests stop idle and busy consumers promptly, do not
      dispatch a buffered/racing second delivery, and let the active handler finish.
- [x] Iterator watcher is cleaned up on completion/failure/cancellation; subscription
      stops without closing a channel beneath the active handler.
- [x] Default calls keep existing behavior; index guard, settle/retry policy and
      cancellation propagation are unchanged.

**Verification:** `uv run pytest tests/test_worker_shutdown.py tests/test_review_consumer_resilience.py tests/test_code_index_worker.py tests/test_queue_messages.py`; full gates; reviewer checks Standards/Spec.

**Dependencies:** Task 1; required PR coordination.

**Files likely touched:** `app/common/infrastructure/amqp_consumer.py` (new);
`app/modules/reviews/infrastructure/amqp.py`; `app/bootstrap/code_index_worker.py`;
`tests/test_worker_shutdown.py` (new).

**Estimated scope:** Medium, 4 files.

**Evidence (2026-10-20):**

- Refreshed origin/main/file ownership immediately before Task 3: baseline unchanged
  (`2326273e6ab7fcea89d1cf3b94b88a4be66ca5e9`); approved #220/#228 comments cover
  the exact review/index intake hunks. Original checkout remains untouched.
- RED `uv run pytest tests/test_worker_shutdown.py -q`: idle review stop kwarg missing
  (1 failed). Minimal shared idle runner/optional argument: 1 passed.
- RED busy review fake and actual RabbitMQ test: stop did not begin close/basic.cancel
  while handler was active (each timed out waiting for the close/cancel event). Added
  owned active/close coordination; first fake + real-broker set: 3 passed.
- RED normal stop-aware iterator exhaustion: retrieved `StopAsyncIteration` re-raised
  by cleanup (1 failed). Clear handled receive result: 1 passed.
- RED index idle stop kwarg missing (1 failed). Added optional argument/shared runner
  while retaining the index guard body: index plus idle test, 37 passed.
- Final focused broker-enabled command:
  `uv run pytest tests/test_worker_shutdown.py tests/test_review_consumer_resilience.py
  tests/test_code_index_worker.py tests/test_queue_messages.py -q`:
  **140 passed in 2.01s** with `TEST_RABBITMQ_URL` set to the disposable local broker.
  `test_worker_shutdown.py` has 23 cases, including 11 actual RabbitMQ cases:
  intake close completes while active delivery stays unsettled and buffered delivery
  returns; active handler finishes during blocked basic.cancel without resurrection;
  idle stop; caller cancellation during receive-close cleanup without double cancel;
  caller cancellation during busy-close preserves basic.cancel/buffer return and
  cancels active work once; fetched stop-tie stays unprocessed and broker redelivers
  only after channel close; caller cancellation during post-subscription, fetched-tie
  and idle-explicit close also preserves broker cancellation and no dispatch;
  first/repeated caller cancellation during normal/fatal iterator exit waits for
  final cleanup, with a prior fatal error logged while caller cancellation propagates.
- Event-driven fake regressions also cover pre/during subscription stop, index active
  drain, fatal iterator/close errors, normal exhaustion, external cancellation and
  no leftover owned tasks. No arbitrary receive timeout or iterator internals added
  to production; no handler starts after observed stop. A stop-tie is deliberately
  retained for later dedicated-channel-close redelivery, with no extra NACK policy.
- The first broker attempt used stale ports after Docker exited/restarted our owned
  containers; connection refusal was infrastructure failure, not RED proof. Broker
  setup also required durable unique queues and class-boundary SDK probes because
  RobustQueue instances have read-only slots. Coordinator restored only #185 infra;
  all behavioral RED/GREEN evidence above used the restored, ready local broker.
- Gates: `uv run ruff check .` passed; `uv run ruff format --check .` passed
  (521 files); `uv run mypy .` passed (518 source files). Ruff corrected test/import
  formatting; a typed coroutine wrapper adapts SDK `close()`'s Awaitable annotation.
- First independent Standards/Spec review found the same single P2: external caller
  cancellation during busy stop cancelled the in-flight SDK close after its consumer
  tag had been discarded, preventing __aexit__ from retrying basic.cancel. Stopped
  that obsolete full suite (exit 2, 800 passed/203 skipped in 181.65s); **this partial
  run is not a passed gate**.
- Added actual-broker `cancel_during_close` case before the fix: 1 failed because
  `cancel_interrupted=True`. Minimal fix retains the already-started close task,
  shield-awaits owned cleanup and cancels active work once. Same case: 1 passed;
  final focused suite/gates above rerun after the fix. No additional scope/cases.
- R2 Standards/Spec review confirmed the same P2 in the remaining direct close
  paths (stop after subscription, fetched stop tie and idle explicit close). Stopped
  the second obsolete full suite (exit 2, 893 passed/203 skipped in 245.89s); **this
  partial run is not a passed gate**.
- Added actual-broker `every_stop_close` regression before the systematic fix:
  3 failed (after_entry, fetched_tie, idle_explicit) because basic.cancel was
  interrupted. One retained close-task factory now owns/shields every stop-aware
  close request and final context exit, after pending receive cleanup completes.
  Stop after subscription is inside the owned try/finally scope. Same 3 cases:
  3 passed; final focused 138-pass suite and lint/format/mypy above passed afterward.
  Audit confirms only one SDK `.close()` call site (the typed coroutine wrapper);
  default `stop=None` serial behavior is unchanged.
- R3 review found the same ownership class at whole-finally cleanup: first caller
  cancellation after normal exhaustion/fatal iterator exit could let the runner return
  while its final close remained pending. No new full suite was started for R3.
- Added actual-broker `cancellation_during_final_cleanup` before the root fix:
  2 failed (runner returned before close completion); then 2 passed after retaining
  the whole cleanup as a task and shielding/re-awaiting it through repeated caller
  cancellation. The same two cases cover normal and fatal exits; caller cancellation
  takes precedence, and a prior fatal error is logged with traceback. Active/receive
  cancellation remains once-only; all owned children/results are retrieved.
- Final focused 140-pass suite and lint/format/mypy above passed after this root fix.
  Fresh R4 independent Standards: 0 findings; Spec: 0 findings after final source/test
  freeze. The final full `uv run pytest` started only after clean R4 reviews.
  Infrastructure URLs remain unset for the full gate; actual broker evidence above
  is separate from expected skipped fixtures.
- Final full `uv run pytest`: **4115 passed, 874 skipped, 2 existing deprecation
  warnings in 518.00s** (exit 0). Task 3 is complete; all four gates and R4 reviews pass.
- No live upstream provider calls, Postman resource/setup, dependency, DB schema,
  application retry/lease policy, parser ownership or connection-close change.

## Task 4: Release an interrupted review's owned lease

**Description:** Add a minimal cancellation cleanup hook that uses the existing
lifecycle UoW and safe owner-fenced operation; do not preemptively add new ports.

**Acceptance criteria:**

- [x] RED→GREEN tests cancel after claim/before initial check-run update and during
      pipeline work; after pipeline quiescence, only its owned running lease expires
      in a committed short transaction before cancellation propagates.
- [x] State/attempt/error fields remain unchanged; different-owner, terminal and
      publishing rows are untouched; third attempt still follows existing T13 with
      no fourth attempt or direct requeue.
- [x] Cancellation does not bypass cleanup, while bounded cleanup failure stays
      observable; reuse existing index cancellation lease-release tests unchanged.

**Verification:** `uv run pytest tests/test_review_shutdown.py tests/test_review_run_worker.py tests/test_reconcile_runs.py tests/test_build_repository_index.py`; run new marked PostgreSQL ownership cases with disposable TEST_DATABASE_URL; full gates; reviewer checks Standards/Spec.

**Dependencies:** Task 3; required PR coordination.

**Files touched:** `app/modules/reviews/application/handle_review_run.py`;
`tests/test_review_shutdown.py` (new, unit and marked PostgreSQL ownership tests);
`tests/test_worker_shutdown.py` for the cross-task ownership follow-up.

**Estimated scope:** Medium, 3 files after the ownership follow-up (shared shutdown
tests are also in Task 5). Reuse the existing row lock and owner-fenced
`extend_lease`, with a running-state check held through commit. No store/port change.
Task 4 baseline refetched unchanged; only #220 owns the application file; the new
test file is unowned. A narrow cleanup coordination supplement was posted before edits:
[#220](https://github.com/larchanka-training/dmc-268-api-t6/pull/220#issuecomment-6293208060).
No #229-owned store/port file was modified, so no additional #229 comment was needed.

**Evidence so far (2026-10-20; independent review/full pytest pending):**

- Initial harness attempt used an unsupported pytest asyncio marker (2 harness
  failures, not behavioral RED); corrected to this repo's `asyncio.run` harness.
- Behavioral RED `uv run pytest tests/test_review_shutdown.py -q`: **2 failed**,
  initial check-run and cooperative pipeline cancellation both retained the claimed
  `NOW + 5 minutes` lease. Minimal cleanup implementation; same command **2 passed**.
- Private per-execution context covers claim/commit/UoW exit and tracks the pipeline
  future. Cancellation cleanup retains/shields one fresh lifecycle UoW through
  repeated caller cancellation. Its existing Run row lock, running-state check and
  owner-fenced `extend_lease` commit only `lease_until = now - 1 microsecond`.
  State/attempt/errors remain unchanged; no application requeue or new error code.
- Nonquiescent pipeline retains the existing lease after the initial 5-second wait;
  the cross-task ownership follow-up below keeps the consumer pending until it stops.
  Pending child result retrieval is registered before cancelling/waiting, so a late
  exception is logged/retrieved. Cleanup DB errors log without replacing cancellation.
- Expanded unit/old review-worker command: **140 passed in 5.40s**. New unit cases
  cover claim interrupted before commit, commit-before/after, successful UoW exit,
  initial check-run, cooperative/stubborn pipeline, publishing/terminal/changed owner,
  repeated caller cancellation while cleanup commit is blocked and cleanup failure.
- Actual PostgreSQL `TEST_DATABASE_URL=postgresql+psycopg://app:app@127.0.0.1:59876/app
  uv run pytest tests/test_review_shutdown.py -q`: **17 passed, 2 existing warnings
  in 9.81s**, including 5 actual PG cases, no skips. Uses isolated seeded schemas on
  the owned disposable database; no Rabbit topology reset or live provider calls.
- PG tests observe a real `pg_blocking_pids` edge while cleanup waits for the Run
  row lock after pipeline quiescence. Committed publishing/terminal/owner changes
  are preserved; owned-running cleanup changes only the lease. Claim attempt 3,
  interruption, existing strict-expiry T13 and duplicate delivery produce failed
  `lease_expired`, attempt 3 and no fourth pipeline/DB claim. Existing informational
  transport attempt semantics are unchanged.
- Final focused command with the same disposable `TEST_DATABASE_URL`:
  `uv run pytest tests/test_review_shutdown.py tests/test_review_run_worker.py
  tests/test_reconcile_runs.py tests/test_build_repository_index.py -q`:
  **267 passed, 2 existing deprecation warnings in 17.49s**, no skips. Existing index
  fenced cancellation release tests pass unchanged.
- `uv run ruff check .` passed; `uv run ruff format --check .` passed (522 files);
  `uv run mypy .` passed (519 source files). Ruff formatted final source/tests and
  corrected a long test signature before final focused run and review freeze.
- Initial independent Standards/Spec both reported zero findings. A subsequent
  read-only self-audit reproduced an already-done pipeline failure whose exception
  remained unretrieved when caller cancellation beat `asyncio.wait` result handling.
  The temporary probe inspected a private task diagnostic only; permanent regression
  uses public `execute`, the loop exception handler, GC and logs.
- RED `uv run pytest tests/test_review_shutdown.py -q -k completed_pipeline_failure`:
  **1 failed** because the loop received `Task exception was never retrieved`.
  Minimal retrieval/logging in `_watch`'s caller cancellation path (normal classified
  failures unaffected): **1 passed**, same command. Caller cancellation and safe lease
  expiry remain intact; no full suite had started on the earlier snapshot.
- R2 final focused command above with disposable `TEST_DATABASE_URL`:
  **268 passed, 2 existing warnings in 16.38s**, including all 5 PG cases, no skips.
  Ruff check/format (522 files) and mypy (519 source files) pass after final formatting.
- Source/tests frozen and coordinator notified for fresh R2 Standards/Spec review;
  full pytest will start only after both axes report zero findings.

- R2 independent Standards: 0 findings; Spec: 0 findings on the whole Task 4 diff.
  Mandatory full `uv run pytest` afterward: **4128 passed, 879 skipped, 2 existing
  warnings in 296.68s**, exit 0. Integration URLs were unset for this normal gate;
  actual PG evidence above is separate. That historical Task 4 snapshot passed; the
  ownership follow-up below reopens review/full-gate completion. No store/port/schema change.

### Task 4 ownership follow-up discovered with Task 5

- Reopened before Task 5 full pytest (no obsolete full gate started). The first clean
  Task 5 review snapshot could let HandleReviewRun return cancellation after its
  initial 5-second child wait while the child still wrote; the supervisor then saw
  a finished consumer and closed resources beneath that pending child.
- Read-only public composition probe confirmed `pipeline_still_active=True` with
  AMQP/index/database resources already closed. No global task registry/port/hook is
  introduced: the existing per-execution retained cancellation cleanup owns the wait.
- Behavioral RED `uv run pytest tests/test_worker_shutdown.py -q -k
  review_pipeline_outliving`: **1 failed**, worker completed instead of staying pending.
  Minimal application cleanup observes the pending pipeline without cancelling it
  again, keeps consumer/resources and lease owned until quiescence, then performs
  existing locked/owner-fenced expiry. Cross regression + updated stubborn unit:
  **2 passed in 11.37s**. Repeated caller cancellation does not reach the child twice.
- Follow-up source/test scope: `handle_review_run.py`, existing stubborn case in
  `test_review_shutdown.py`, public cross regression in `test_worker_shutdown.py`.
  A process case in the existing Task 5 process file proves a truly resistant review
  pipeline keeps resources/original lease until exit143. Private cancellation-wait
  constant preserves the production 5-second initial wait while allowing injected
  short subprocess timing; no settings/env/public constructor hook changed.
- Actual subprocess resistant-review-pipeline test: **1 passed in 1.61s**. Injected
  initial pipeline wait 0.02s, process deadline 0.35s; real SIGTERM caused exit143
  after asserting a single child cancellation, resources still open and original
  unexpired lease/one claim commit retained beyond the supervisor cleanup deadline.
- Combined Task 4+5 focused command (all previous Task 5 files plus review-worker and
  reconciler regressions) with disposable PG59876/Rabbit59878: **330 passed, 2 existing
  warnings in 33.43s**, no skips; 12 actual process, 11 actual broker and 5 actual PG
  cases. Ruff check/format (524 files) and mypy (521 source files) passed. A final
  import-only reorder was corrected before the combined review freeze.
- Combined source/tests frozen and coordinator notified for fresh whole Task 4+5
  Standards/Spec review. One full pytest run will cover both reopened Task 4 and Task 5
  after both axes are clean; no obsolete Task 5 full suite had been launched.

### Watchdog-exit ownership follow-up

- Combined Spec review confirmed a P1 in a different exit path: an ordinary watchdog
  deadline could complete `_fail` after the initial cancellation wait while its child
  remained pending. The cancellation-only wait did not run, so the worker could still
  close resources beneath that writer. Normal deadline outcome/timing must stay intact.
- Re-sliced the same ownership task: `HandleReviewRun` owns every pipeline future in
  an instance-local set from creation through completion, alongside its per-execution
  context. Public `wait_for_quiescence()` observes those futures after deliveries stop;
  normal execute/watchdog/failure outcomes do not wait indefinitely or change retries.
- Worker invokes that explicit join in a protected finally after TaskGroup.__aexit__,
  inside the AMQP/resource contexts. One retained join task is shielded/re-awaited
  through first/repeated caller cancellation; all results are retrieved. Body fatal
  errors remain visible, or are logged if later caller cancellation takes precedence.
  No global task scan, constructor hook, port/store change or silent getattr fallback.
- Initial watchdog test setup wrongly expected a queued retry; corrected before
  counting behavioral RED. Existing `deadline_exceeded` is nonretryable: failed state,
  attempt 1, null lease and no retry remain unchanged before the shutdown join finishes.
- Valid RED `uv run pytest tests/test_worker_shutdown.py -q -k watchdog_orphan --tb=short`:
  **3 failed**, stop completed / external cancellation / fatal sibling escaped before
  pending pipeline quiescence. Explicit ownership/join plus legacy fake contract:
  **4 passed in 1.37s**, including the unchanged legacy composition regression.
- Final join-path cases plus actual process proof: **6 passed in 3.93s**. Covers stop,
  fatal and caller-cancel paths, first/repeated cancellation arriving in final join,
  original fatal traceback logging when cancellation wins, and real SIGTERM exit143
  while a watchdog-failed pipeline still owns resources. The normal watchdog failure
  policy and completion timing are asserted before release; the child is cancelled once.
- Final combined focused command above with disposable PG59876/Rabbit59878:
  **336 passed, 2 existing warnings in 34.63s**, no skips: 13 real process, 11 real
  broker and 5 real PG cases. Ruff check/format (524 files) and mypy (521 source files)
  all pass. Whole Task 4+5 source/tests frozen and coordinator notified for fresh
  independent Standards/Spec review before one shared full pytest gate.
  No full pytest was started on any superseded Task 5 snapshot. Fatal/unrequested
  library cancellation without an OS signal is not claimed to have the signal-owned
  35-second hard deadline; resource ownership still remains protected.
- Fresh combined whole-diff Standards: 0 findings; Spec: 0 findings (independent
  targeted confirmation included all 13 subprocess cases). One mandatory shared
  `uv run pytest` started only after both clean reviews; result pending.

## Task 5: Wire the bounded SIGTERM lifecycle

**Description:** Register process signals, coordinate stop/drain/cancel/cleanup and
keep resources available until leases have been released.

**Acceptance criteria:**

- [x] RED→GREEN injected-stop tests prove all queue intakes and leader scheduling
      stop, active work can finish during 20 s drain, then pending work receives one
      cancellation with 10 s lease cleanup before resources close (5 s allowance).
- [x] Real subprocess SIGTERM exits within the injected deadline, removes handlers
      on normal exit, and covers idle, active, repeated-signal and parked
      task/default-executor cases; deadline backstop bounds teardown.
- [x] Worker composition wires review cleanup and existing index cancellation
      release, preserves normal external task cancellation, and does not hide fatal
      startup/iterator errors or release a lease under a still-writing task.

**Verification:** `uv run pytest tests/test_worker_shutdown.py tests/test_review_shutdown.py tests/test_worker.py tests/test_worker_heartbeat.py tests/test_build_repository_index.py`; full gates; reviewer checks Standards/Spec.

**Dependencies:** Tasks 3–4; required PR coordination.

**Files likely touched:** `app/bootstrap/worker_shutdown.py` (new); `app/worker.py`;
`tests/test_worker_shutdown.py`; `tests/test_worker_shutdown_process.py` (new);
`tests/test_worker.py` (explicit no-op lifecycle join on its existing composition fake).

**Estimated scope:** Medium, 5 files. Process cases are split into a new test file
to keep real signal/Runner teardown evidence distinct from queue/composition tests;
the shared Task 4 review lease tests are kept in that slice. The fifth Task 5 file
adds only the explicit public join to an existing legacy fake; ownership refresh
confirmed #228 and its previously posted comment already covers narrow lifecycle
changes to `tests/test_worker.py`. No production getattr/no-op fallback, new settings
or dependencies are introduced.

**Evidence so far (2026-10-20; independent review/full pytest pending):**

- Refetched main: baseline remains `2326273e6ab7fcea89d1cf3b94b88a4be66ca5e9`.
  Exact ownership refresh found `app/worker.py` in #220/#227/#228/#230; helper and
  shutdown test files are unowned. Posted narrow shutdown supplements before edits:
  [#220](https://github.com/larchanka-training/dmc-268-api-t6/pull/220#issuecomment-6293547353),
  [#227](https://github.com/larchanka-training/dmc-268-api-t6/pull/227#issuecomment-6293548418),
  [#228](https://github.com/larchanka-training/dmc-268-api-t6/pull/228#issuecomment-6293549761),
  [#230](https://github.com/larchanka-training/dmc-268-api-t6/pull/230#issuecomment-6293551505).
- RED `uv run pytest tests/test_worker_shutdown_process.py -q`: **2 failed**;
  actual SIGTERM before/after loop binding killed the process with -15. Added the
  synchronous signal owner around all of `asyncio.run`, retained pre-bind stop and
  first-signal independent timer: same command **2 passed**. Clean exits restore
  original SIGTERM/SIGINT handlers and disarm only after Runner teardown returns.
- RED `uv run pytest tests/test_worker_shutdown.py -q -k
  'worker_composition or interrupts_broker'`: **2 failed**, stop ignored during broker
  startup and active composition never stopped. Added startup cancellation owner,
  optional event wiring and TaskGroup supervisor: same tests plus existing default
  worker composition **3 passed**, preserving legacy `stop=None` consumer signatures.
- Startup watcher cancels the parent only before consumer tasks exist, records that
  it initiated cancellation, and translates only that sole cancellation to normal
  shutdown. Pre-stopped worker opens no resources; startup/registration boundary
  checks prevent dispatch, while unrelated external cancellation/fatal errors remain
  visible. Background heartbeat and leader handles belong to the TaskGroup.
- Supervisor observes children with noncancelling `asyncio.wait` snapshots, stops
  background work, drains for 20 seconds, cancels remaining consumers once and waits
  through the cumulative 30-second cleanup deadline. Pending work keeps the TaskGroup
  and resources open for the independent 35-second process backstop; no parent/gather
  timeout introduces a second cancellation through retained intake/lease cleanup.
- Coordinator's read-only transition audit reproduced a queued-stop-before-child-start
  race: RED `uv run pytest tests/test_worker_shutdown.py -q -k stop_before_registered`:
  **1 failed** because leader work started. A named stop-aware background factory
  now checks stop before creating leader/heartbeat coroutines; same case **1 passed**.
- Process suite expanded to **11 actual subprocess cases**: real SIGTERM before/after
  loop binding; clean handler restoration/timer disarm; safe late signal after loop
  close; real worker composition startup/cooperative stop; repeated signals without
  deadline reset; cancellation-resistant child during Runner.close; parked default
  executor; blocked loop; resistant actual-composition cleanup retains resources past
  its cleanup budget until independent exit143. Injected process deadline is 0.35s
  for resistant cases; ordinary worker return is observed before Runner teardown cases.
- Composition event tests cover all three stop arguments, clean drain without cancel,
  one cancellation after drain, pending cleanup with resources open beyond budget,
  heartbeat ownership, external startup/runtime cancellation and fatal startup/child
  propagation. No live upstream provider calls; composition provider/infra boundaries
  use local fakes, distinct from actual PostgreSQL/RabbitMQ evidence below.
- Final focused with `TEST_DATABASE_URL=postgresql+psycopg://app:app@127.0.0.1:59876/app`
  and `TEST_RABBITMQ_URL=amqp://app:app@127.0.0.1:59878/`:
  `uv run pytest tests/test_worker_shutdown.py tests/test_worker_shutdown_process.py
  tests/test_review_shutdown.py tests/test_worker.py tests/test_worker_heartbeat.py
  tests/test_build_repository_index.py -q`: **199 passed, 2 existing warnings in 31.26s**,
  no skips. Includes 11 actual broker cases and 5 actual PG cases, plus the 11 real
  process cases. Existing index cancellation/lease release and worker behavior pass.
- `uv run ruff check .` passed; `uv run ruff format --check .` passed (524 files);
  `uv run mypy .` passed (521 source files). Ruff corrected routine suppress/line
  formatting before final verification. Source/tests frozen and coordinator notified
  for fresh Standards/Spec review; the first axes both reported zero findings.
  The cross-task ownership follow-up above reopened combined review/full-gate status;
  final combined verification is 336 passing cases, with 13 real process cases.

## Checkpoint: bounded lifecycle

- [x] Tasks 3–5 reviewed with zero Standards/Spec findings and full gates green.
- [x] Evidence shows intake stops before cancellation and lease writes finish while
      database resources remain open; process exit proof covers teardown too.

- Final combined Task 4+5 Standards: 0 findings; Spec: 0 findings. One shared mandatory
  full `uv run pytest`: **4160 passed, 879 skipped, 2 existing deprecation warnings
  in 339.86s**, exit 0. URLs unset for the normal gate; 336 focused actual-infra/process
  checks above are separate. Tasks 4–5 and the bounded-lifecycle checkpoint are complete.

## Task 6: Align deployment stop grace with worker shutdown

**Description:** Give both worker Compose services enough time for the approved
35-second application shutdown ceiling; document the lifecycle and limits.

**Acceptance criteria:**

- [x] RED→GREEN deployment contract verifies local and staging worker
      stop_grace_period=40s; service command still delivers SIGTERM to Python PID 1.
- [x] New runbook documents 20/10/5-second phases, leased index/review behavior,
      operational proof commands, log interpretation and cleanup-failure limits.
- [x] Existing deploy/rollback behavior remains valid; no secret/env allowlist,
      unrelated service timeout or public transport contract changes.

**Verification:** `uv run pytest tests/test_deploy_staging_services.py tests/test_worker_shutdown.py`; validate Compose using existing fixture/test practices; full gates; reviewer checks Standards/Spec.

**Dependencies:** Task 5; required PR coordination for docker-compose.yml.

**Files likely touched:** `docker-compose.yml`; `deploy/compose/staging.yml`;
`tests/test_deploy_staging_services.py`; `docs/WORKER_SHUTDOWN.md` (new).

**Estimated scope:** Medium, 4 files.

**Evidence so far (2026-10-20; independent review/full pytest pending):**

- Coordinator refetched main before this slice; baseline/HEAD remain
  `2326273e6ab7fcea89d1cf3b94b88a4be66ca5e9`. Refreshed all 8 open PR file lists
  with paginated REST calls: only #227 owns local `docker-compose.yml`; staging,
  contract tests and the new runbook are unowned. Posted the exact worker-only
  grace coordination before edits:
  [#227](https://github.com/larchanka-training/dmc-268-api-t6/pull/227#issuecomment-6294446259).
- RED `uv run pytest tests/test_deploy_staging_services.py -q -k worker_stop_grace`:
  **2 failed**, local and staging `worker` both lacked the 40-second grace. Added
  only `stop_grace_period: 40s` below the unchanged exec-form Python command in each;
  same command **2 passed in 0.04s**. Webhook-worker/other service timeouts untouched.
- New runbook documents first-signal 35-second ceiling (including startup, blocked
  loop and Runner/default-executor teardown), nonresetting repeated signals,
  cumulative 20/10-second phases and 5-second teardown allowance, 40-second Docker
  grace, exit143, retained resources/pipeline joins, owned-running lease expiry,
  unchanged watchdog/retry behavior, limits, logs and disposable-stack proof commands.
  The hard deadline is signal-owned; no claim is made for unrequested library cancel.
- Final targeted command with disposable `TEST_RABBITMQ_URL` on port59878:
  `uv run pytest tests/test_deploy_staging_services.py tests/test_worker_shutdown.py
  tests/test_worker_shutdown_process.py -q -k 'worker_stop_grace or
  workers_run_from_the_api_image or worker_healthcheck or
  workers_are_not_on_the_edge_network or staging_worker_uses_nonisolated or shutdown
  or sigterm or pipeline or stop or cancellation or background or fatal_startup'`:
  **75 passed, 167 deselected, 2 existing warnings in 49.53s**, no skips. Includes
  actual broker and subprocess shutdown regressions, plus existing worker image,
  dependency, healthcheck/network and selected deploy/stop contracts. The remaining
  deployment scenarios will run in the mandatory full gate after clean reviews.
- Read-only local `docker compose -f docker-compose.yml config --quiet`: exit0.
  Staging config validation used a temporary copy, empty fake service env files,
  fake required image/password values and `--env-file /dev/null`: exit0. No Docker
  container, volume, network, real env file or deployment was changed.
- `uv run ruff check .` passed; `uv run ruff format --check .` passed (524 files);
  `uv run mypy .` passed (521 source files). Exact four source/test/runbook files
  frozen and coordinator notified for fresh Standards/Spec review; full pytest only
  after both axes are clean. No application/dependency/schema/settings change.
- Fresh Task 6 Standards: 0 findings; Spec: 0 findings across all four frozen files.
  Mandatory full `uv run pytest` started afterward; result pending.

- Full mandatory `uv run pytest`: **4162 passed, 879 skipped, 2 existing warnings
  in 383.80s**, exit0. All Task 6 gates and both review axes pass; Task 6 complete.

## Task 7: Prove resilience through the existing real stack

**Description:** Reuse the current worker PostgreSQL/RabbitMQ fixtures and fake
GitHub/model adapters for delivery/lease/shutdown integration evidence.

**Acceptance criteria:**

- [x] Closed-channel ACK failure in one review consumer permits a subsequent review
      and its sibling consumer to progress; existing index delivery behavior passes.
- [x] Stop the actual worker composition while idle and while index/review work is
      active: intake is cancelled, active work finishes or its lease is released,
      buffered deliveries remain available, and connection disposal occurs last.
- [x] Reconciler recovers an expired owned review lease under unchanged T12/T13
      limits; existing domain retry, unexpected retry, DLQ and publication idempotency
      integration scenarios pass with real infrastructure, not skips.

**Verification:** With disposable TEST_DATABASE_URL and TEST_RABBITMQ_URL, run
`uv run pytest tests/test_worker_queue_postgres.py tests/test_review_shutdown.py`;
run unchanged `tests/test_code_index_worker.py`, `tests/test_build_repository_index.py`,
`tests/test_queue_messages.py` and contract-schema tests; full gates; reviewer checks Standards/Spec.

**Dependencies:** Tasks 1–6; refreshed PR ownership coordination.

**Files touched:** `tests/test_worker_queue_postgres.py` and
`app/modules/reviews/infrastructure/amqp.py`. Existing real-PG T13 proof in
`tests/test_review_shutdown.py` is reused unchanged; no new harness/provider.

**Estimated scope:** Small, 2 files. The real closed-channel ACK regression exposed
an unregistered robust review queue; the coordinator approved this narrow amendment
within the resilience scope before the production edit. Index policy is unchanged.

**Evidence (2026-10-20, complete):**

- Refreshed all open PR file lists before Task 7 edits; only #220 overlaps the
  integration file and review queue construction. Coordinated the test first
  [here](https://github.com/larchanka-training/dmc-268-api-t6/pull/220#issuecomment-6294748502),
  then the exact passive-registration amendment before its production edit
  [here](https://github.com/larchanka-training/dmc-268-api-t6/pull/220#issuecomment-6294908777).
- Actual-stack RED: `uv run pytest tests/test_worker_queue_postgres.py -q
  -k real_closed_channel_ack` with both disposable fixture URLs: 2 failed. Closing
  the delivery's underlying aiormq channel with an ordinary transport-loss reason
  produced a genuine `ChannelInvalidStateError` from ACK. The worker survived, but
  the next run remained queued (run route) or publishing (publish route). Broker
  probes confirmed the failed route had 0 consumers and 2 ready messages while
  its sibling retained 1 consumer. This was a restoration gap, not a provider error.
- Minimal GREEN: `AmqpChannels.consumer_queue()` now passively obtains its review
  queue with `ensure=True`, registering it for the installed robust channel's
  restoration. Same actual-stack regression: **2 passed, 17 deselected in 4.43s**.
  Permanent assertions prove next-run completion, sibling publication after the
  ACK failure and one restored consumer. Topology/retry/settlement policy and
  index queue construction are unchanged.
- Actual idle/busy composition proof: `uv run pytest
  tests/test_worker_queue_postgres.py -q -k stop_releases`: **2 passed,
  19 deselected in 3.99s** with both real URLs. The busy case observes persisted
  live review/index leases before stop and checks cleanup before the actual
  connection closes: review stays running at attempt 1 with expired owned lease;
  index token/expiry are cleared and pending bytes are zero. Original unsettled
  deliveries plus unstarted duplicate pointers remain available. Existing
  `ReconcileRuns` and a healthy actual worker recover the review to succeeded
  attempt 2. Idle stop starts no provider work.
- Initial idle/busy setup polling failures were test-probe errors, not behavioral
  RED: robust passive declaration returned a cached queue with stale counters.
  Explicit `queue.declare()` refreshes broker counters. Diagnostic task dumps were
  removed. One accidental URL-unset run skipped both cases; it is not stack proof.
- Provider calls use existing fake GitHub/LLM transports; no live upstream calls,
  Postman resources, dependency, schema or wire contract changes.
- Final focused command, both disposable URLs set: `uv run pytest
  tests/test_worker_queue_postgres.py tests/test_review_shutdown.py
  tests/test_worker_shutdown.py tests/test_worker_shutdown_process.py
  tests/test_review_consumer_resilience.py tests/test_queue_messages.py
  tests/test_code_index_worker.py tests/test_build_repository_index.py
  tests/test_contract_schemas.py -q --tb=short`: **385 passed, 0 skipped,
  2 existing TestClient deprecation warnings in 65.01s**. This includes the old
  real retry/DLQ/idempotency cases, 5 persisted-PG shutdown cases, real broker races
  and 13 actual signal/process cases. Final four new composition cases rerun after
  test-only formatting/type corrections: **4 passed, 17 deselected in 6.87s**.
- `uv run ruff check .` passed; `uv run ruff format --check .` passed (524 files);
  `uv run mypy .` passed (521 source files). Initial two overlong SQL strings and
  test-only explicit-import/optional-value narrowing issues were corrected before
  freeze. No production behavior changed in those static fixes.
- Final independent whole-issue Standards: **0 actionable findings**; Spec:
  **0 findings**, covering all tracked changes and all 9 new files. The final
  source/test snapshot stayed frozen through both full-suite attempts. Only this
  plan/todo completion evidence changed afterward.
- First final all-infrastructure attempt collected 5045 tests and reached 96%, but
  the owned disposable PostgreSQL container exhausted its original 512 MiB tmpfs
  at 85% (`No space left on device`, WAL PANIC/recovery failure). The coordinator
  confirmed this infrastructure failure and authorized interruption: **exit 130**,
  no final pass/error totals were emitted. This partial run is **not a passing gate**.
  Only the coordinator's owned disposable PG was replaced with a fresh anonymous
  Docker volume (28.5 GB available), bounded WAL and the same fixture URL. RabbitMQ
  remained healthy. No product fix was made for this fixture-capacity failure.
- Final replacement-fixture command:
  `TEST_DATABASE_URL=postgresql+psycopg://app:app@127.0.0.1:59876/app
  TEST_RABBITMQ_URL=amqp://app:app@127.0.0.1:59878/ uv run pytest`:
  **5045 passed, 0 skipped, 0 failed/errors, 2 existing FastAPI/Starlette TestClient
  deprecation warnings in 1006.98s (16:46), exit 0**. All opt-in PostgreSQL/broker
  integrations ran, including all 21 worker-queue, 18 review-shutdown, 42 worker
  shutdown and 13 actual process-signal cases. No live upstream behavior is claimed.
- Coordinator refreshed origin/main again at replacement gate 90%:
  HEAD/origin/main remain `2326273e6ab7fcea89d1cf3b94b88a4be66ca5e9`, so no rebase
  change is needed. Read-only `git diff --check` is clean. No commit/push by development.

## Final checkpoint and publishing handoff

- [x] Every issue acceptance criterion has named passing test evidence; record RED
      and GREEN commands/results alongside completed task entries.
- [x] All four final gates pass: `uv run ruff check .`,
      `uv run ruff format --check .`, `uv run mypy .`, `uv run pytest`.
- [x] Actual PostgreSQL/RabbitMQ verification is recorded separately from skips;
      no unnecessary Postman setup or network service mutation occurred.
- [x] Independent Standards and Spec reviews report zero findings after latest diff.
- [x] Rebase on main and recheck coordinated overlaps/gates before requesting review.
- [ ] Publisher follows pull-request skill, references #185 without closing keyword,
      reports no dependency added, pushes PR and attaches it to this chat. The user
      explicitly requested ready for review (no draft) and will assign the reviewer;
      agents must not select a reviewer. Development made no commit/push.
- [x] Keep issue/board/approval/merge actions within authorized repository workflow;
      never close issue or merge merely because automated gates pass.

Todo bookkeeping in this file accompanies each task and does not justify expanding
implementation scope. Tasks sharing AMQP, worker composition or shutdown tests run
sequentially; test execution/read-only review may run independently.
