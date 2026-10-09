# Implementation Plan: durable enqueue after superseded Run cancellation (#109)

## Overview

Fix the loss of a successful CI delivery for head B while the previous head A is
still an active Run with cancellation requested. Persist waiting in the existing
webhook receipt and retry through the production delivery dispatcher until A is
terminal. Recheck the current PR and CI before creating B. No new dependency,
migration, API endpoint, or change to the Run active-state definition is expected.

Issue: https://github.com/larchanka-training/dmc-268-api-t6/issues/109.
Inspected base: `92250a1fb3b65f88a2ea111d33613f9a4af1d1d2` (`origin/main`),
branch `fix/109-superseded-ci-run`. The user approved this plan and its proposed seams; implementation follows the
vertical slices below. Commands and observed results are recorded in `109-todo.md`.

## Evidence and affected boundaries

1. `SqlAlchemyWebhookRunStore.insert_webhook_run` considers `queued`, `running`,
   and `publishing` active, even when `cancel_requested` is true. This is correct:
   the unique index `uq_runs_one_active_per_code_change` uses those same states.
2. `TryEnqueueWebhookRun.execute` currently maps every active blocker to final
   `duplicate (active_run)`. The store returns no blocker head or cancellation
   information to the use case.
3. `TriggerFromDelivery` returns only rendered log strings, so the dispatcher and
   receiver cannot distinguish a retryable wait from a final duplicate safely.
4. CI targeting writes `ci_status={"event": "check_suite"}` or status evidence.
   The dispatcher returns `processed_ci`; `ReceiveGitHubDelivery` then marks the
   receipt projected even when no B Run was inserted.
5. `SqlAlchemyDueNoCiCandidates.list_due` requires `ci_status == {}` and
   `wait_for_ci=auto`. Therefore it cannot recover this delivery; extending only
   that sweep would also miss `always` and `never` repositories.
6. Existing receipt rows already contain JSONB payload, lease/token, `retry_after`,
   and terminal markers. Claim/pending selection and crash recovery are reusable.
   Existing generic deferrals stop after three attempts, so reusing that bounded
   path directly would merely postpone the loss.

## Architecture decisions

- Distinguish a temporary supersession blocker from a final duplicate using typed
  data. Under the existing PR row lock, read the active Run's head and cancellation
  flag, and report a deferred enqueue only for a different head whose active Run
  has `cancel_requested=true`. Same-head active duplicates and
  `head_already_reviewed` remain final. Preserve `active_run` as the human-readable
  reason; do not parse log text to drive control flow.
- Propagate retry intent through a small structured trigger outcome and a dedicated
  dispatch status, for example `deferred_run_trigger`. CI events with several PR
  targets must wait if any target is deferred; replaying already-enqueued targets
  remains safe through existing Run uniqueness. Retain existing detail formatting.
  A narrow compatibility union for existing `str | None` trigger doubles is allowed
  during the contract slice: those values always mean final outcomes, whereas only
  structured data can request a retry. Avoid broad unrelated test refactoring.
- Add a dedicated flush-only receipt operation, e.g. `retry_run_trigger`, that
  checks the current claim token, clears its lease/token, and sets `retry_after`.
  It leaves `projected_at`, `projection_failed_at`, and `projection_deferred_at`
  unset, and does not consume the generic three-attempt counter. Lost claims must
  raise rather than silently report successful persistence.
- Use a short explicit retry interval (30 seconds, aligned with the webhook worker
  poll). Supersession waiting has no fixed attempt ceiling: long cancellation is
  not a delivery failure. Generic unknown-installation/repository deferrals and
  dispatch-failure policies keep their current limits and tests.
- Persist retry before returning from projection. A crash before finalization
  leaves the claim reclaimable after its lease; a crash after retry commit leaves
  `retry_after` persisted. Receipt duplicate handling must not overwrite either.
  Pending receipts are not eligible for the finished-receipt purge.
- Every retry executes the normal target lookup and `DetermineCiEligibility`.
  The latter reads detached snapshots, performs GitHub CI I/O outside transactions,
  reads current state again, and the enqueue transaction locks/rechecks its
  candidate. Closed/unlabeled/disabled PRs and stale B events become terminal
  without creating B; changed CI blocks insertion. No saved success decision is
  reused. PR/label retries use their existing authoritative projection behavior.
- Keep existing durable Run publication replay for a crash/broker failure after
  insert. The new mechanism must not treat `publication_pending` as a request to
  insert another Run. Do not alter worker cancellation, partial indexes, frozen
  Alembic revisions, or widen the no-CI sweep.

The guarantee concerns state known to the application and current CI returned by
GitHub. PR lifecycle/label updates arrive through the existing webhook projection;
this issue does not introduce independent polling of all remote PR metadata.

## Dependency graph and vertical slices

`PG reproduction → typed supersession result → durable receipt retry → restart /
redelivery proof → current-state invalidation proof → documentation and gates`.
Details, acceptance criteria, files, and verification are in `109-todo.md`. Each
implementation slice starts with a failing public-boundary test and then the
minimum behavior needed to pass it. The initial PG regression stays red until the
durable retry slice is complete; record that expected failure explicitly.

### Phase 1: reproduce and expose temporary blocking

- [x] Task 1: permanent PostgreSQL regression and already-terminal A control.
- [x] Task 2: typed temporary-blocker result through enqueue and delivery dispatch.
- [x] Checkpoint A: the control is green; the race is demonstrated without sleeps;
      active uniqueness and ordinary duplicate tests still pass.

### Phase 2: guarantee eventual processing

- [x] Task 3: durable unbounded supersession wait in the receipt receiver/store.
- [x] Task 4: restart, duplicate delivery, concurrent claim, and publication recovery.
- [x] Checkpoint B: the original PG regression passes with no event after A becomes
      terminal, including after more than three retries and a reconstructed worker.

### Phase 3: verify invalidation and finish

- [x] Task 5: current head/PR/label/enabled/CI invalidation matrix on PostgreSQL.
- [x] Task 6: operator documentation and all repository gates.
- [x] Checkpoint C: each issue AC has executable evidence; relevant PG tests really
      ran, with zero skips; reviewable branch and verification notes are ready.

## PostgreSQL runtime and test seams

The coordinator verified a running `postgres:17-alpine` container named
`dmc268-webhook-local-postgres-1`, exposed on localhost port 5433. Docker works
with the environment's required sandbox escalation. Do not stop that stack, remove
its volume, reuse its existing database destructively, or print its secret values.
Create a fresh task-specific disposable database there after privately checking the
local test connection settings, or start an isolated postgres:17-alpine container
if connection settings are unavailable. The DB user needs `CREATE SCHEMA`.
Remove only the task-created database/container after tests.

Use `TEST_DATABASE_URL=postgresql+psycopg://<test-user>:<test-password>@127.0.0.1:5433/<disposable-db>`.
The existing fixture in `tests/test_webhook_run_trigger_postgres.py` creates a
random schema, upgrades real Alembic migrations, and drops that schema in teardown.
Production `ReviewsApiResources` builds the actual receiver, dispatcher, CI
provider, and enqueue stores. Fake only external GitHub HTTP and confirmed queue
publication; do not fake PostgreSQL queries or the application dispatch/enqueue.
No LLM or real GitHub credentials are needed for this regression.

Extend `Pipeline` to retain/reconstruct a composed receiver and replay pending
receipts without calling `deliver` again. Use a deterministic clock seam or make
the stored retry due between polls; do not sleep 30 seconds or generate new webhook
events to force progress. Explicitly count receipts/deliveries before and after
cancellation. To cover HTTP ingress too, submit signed `POST /webhooks/github`
through the real FastAPI app with overrides only for the disposable receipt UoW
and test webhook secret, assert 202, and then process its durable receipt with the
production composition. Restore dependency overrides in teardown.

Public seams proposed for approval: signed HTTP receipt acceptance, receipt replay
result/durable fields, enqueue result, PostgreSQL Run rows/constraints, and confirmed
Run publisher messages. Worker finalization may be driven through the existing
worker UoW/store if convenient; a narrowly documented SQL fixture transition of A
to `cancelled` with `cancel_reason='superseded'` is acceptable for this enqueue race.
It must happen after the B CI receipt is first processed, not before.

Focused commands (with the disposable DB URL exported):

```bash
uv run pytest tests/test_webhook_run_trigger_postgres.py -v -rs
uv run pytest tests/test_github_run_trigger.py tests/test_github_installation_dispatch.py tests/test_github_webhook_delivery.py tests/test_webhook_full_path.py -q -rs
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run pytest
```

For the full suite, use an isolated RabbitMQ test vhost if available because existing
broker integrations redeclare/delete topology; never use a live `/` vhost. The
required CI job already supplies PostgreSQL 17 and RabbitMQ and rejects integration
skips, so no workflow change is expected. A skipped PG test is not evidence that
the race is fixed. Record any unrelated runtime limitations separately.

## Risks and mitigations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Treating `cancel_requested` as terminal | High | Keep active state predicate and DB partial unique index untouched; test running/publishing blockers. |
| Three-attempt limit loses a long cancellation | High | Dedicated retry release does not increment generic counters; exercise at least four waiting polls before terminal A. |
| Different head or intent changes during retry/CI I/O | High | Current target/eligibility and locked candidate recheck; PG invalidation cases and existing state-change unit tests. |
| Duplicate receipts or worker crash after side effect | High | Claim-token guard, durable retry, existing Run keys and outbox; reconstruct receiver and replay same receipts. |
| Structured result changes several trigger test doubles | Medium | Introduce additive typed retry outcome compatibly, keep final string/None doubles working; restrict touched files per slice. |
| Several PRs share one CI head | Medium | Aggregate retry intent across targets; already-enqueued targets must not suppress waiting for another PR. |
| Indefinitely active A | Medium | Continue durable retry with a bounded polling frequency; existing worker recovery is responsible for making A terminal. Log wait/retry reason. |
| Generic network/database failures become terminal | Medium | Preserve current failure policy; test transient publication recovery via existing Run replay, do not silently redesign all delivery retries. |
| Open PR #108 owns canonical spec docs | High | Update unowned `docs/WEBHOOK_WORKER.md` and these plan files; do not edit #108 files without a comment there. |

## Scope, prerequisites, and completion

No product decision or blocking issue dependency is outstanding. Before coding:
obtain plan/seam approval, verify current open-PR ownership for implementation
files, and prepare the disposable PG database. Avoid the files owned by PR #108:
`docs/TEST_PLAN.md`, `docs/SYSTEM_DESIGN.md`, `docs/PIPELINE_SPEC.md`,
`docs/CONTEXT_AND_VERIFICATION_SPEC.md`, and `docs/RULES_FORMAT_SPEC.md`.

After implementation and clean review, publish using the repo workflow: conventional
title ≤72 characters, body `What` / `Why` / `How to verify` / `Refs`, `Refs #109`
and tracker #106 without closing keywords or a Development-panel link. Rebase on
main before requesting review. A current-head approve and resolved threads are
required before merge; tech lead verifies AC and records status in #106/board 12.
This planning agent does not commit, push, publish, or mark the issue complete.


## Implementation verification and handoff

All six implementation tasks and checkpoints passed. Evidence and pending external
follow-through are in `109-todo.md`: 304 focused tests passed with zero skips;
all 34 composed webhook PostgreSQL cases executed, and the full suite passed 2426
tests with zero skips. Exact Ruff lint/format and mypy gates passed. Final
independent Standards/Spec reviews each returned zero findings. No new dependency,
migration, Run index, cancellation finalization, or PR #108 owned file changed.

The publisher fetched and rebased on current `main` (`92250a1`); the base and
implementation were unchanged, and PR #108 still has no overlapping files.
The coordinator dropped only `dmc268_issue109_tests` and removed only the task
RabbitMQ container `dmc268-issue109-rabbitmq` and its anonymous volume, both
successfully. The pre-existing PostgreSQL stack remains untouched. Publication
is prepared as a draft PR; GitHub current-head approval/merge and tech-lead
issue/tracker/board acceptance remain pending with their respective owners.
