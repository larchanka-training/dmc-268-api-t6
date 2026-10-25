# Implementation Plan: Pin run publication settings (#212)

## Overview

Capture `Repository.severity_threshold` and `Repository.max_comments` in the same
transaction that creates a run. All newly created runs, including the current legacy
pipeline, use those settings for postprocessing and retry; rerun creates a new run
with current settings. Historical runs whose new columns are NULL retain live
Repository fallback. Carry the pins into immutable policy snapshots without changing
`policy_digest`, its schema version, or conventions-cache identity.

## Architecture decisions

1. Add nullable `severity_threshold` and `max_comments` to both `runs` and
   `run_policy_snapshots`. Reuse the PostgreSQL `severity_threshold` enum without
   recreating or dropping it; cap is a small integer. New named CHECK constraints
   should permit a fully NULL historical pair or a fully populated valid pair.
   Use a new Alembic revision after `20261020_0037`; never edit merged revisions.
   No backfill: current settings cannot reconstruct the original run settings.
2. Add optional publication fields to frozen `RunPolicyPins` and `PolicySnapshot`.
   Use the application `SeverityThresholdValue` literal rather than importing the
   infrastructure enum into application code. Defaults of NULL preserve old
   constructors and model the historical state explicitly. Store the exact pair;
   never manufacture a pin while loading a historical run or existing snapshot.
3. The only production run insertion paths on the inspected main commit
   (`4427ca8a`) are `SqlAlchemyWebhookRunStore.insert_webhook_run` and
   `SqlAlchemyRerunStore.create_rerun`. Both obtain `RunInsertCandidate` through
   `SqlAlchemyWebhookRunStore.lock_candidate`. Extend this candidate with required
   settings and write both pins in both insertion paths. `manual` and `dry_run`
   are accepted trigger values but currently have no separate production writer.
   Webhook creation covers ordinary events, deferred receipts, and sweep/replay.
4. Read a fresh Repository settings pair inside the creation transaction. The
   current `session.get(Repository, ...)` may reuse a stale identity-map entity;
   refresh the selected Repository or select settings as scalar columns. Preserve
   installation-before-PR/Run locking and leave all GitHub/LLM calls outside the
   transaction. Additional Repository locking is unnecessary for capturing the
   coherent pair observed by the creation transaction.
5. Extend snapshot store load/insert mappings and `build_policy_snapshot` to copy
   pins. Leave the canonical digest payload and `POLICY_SNAPSHOT_SCHEMA_VERSION=1`
   unchanged: publication settings are stored metadata, not policy identity.
   Existing snapshots keep their stored values, including a NULL historical pair.
   Existing write-once `ON CONFLICT DO NOTHING` behavior remains in place.
6. Switch current legacy postprocessing immediately. Read Run pins regardless of
   whether a policy snapshot exists; the legacy worker does not require one.
   Only a fully unpinned historical run reads the current Repository pair at
   processing time. A historical run remains live on retry even if a snapshot row
   exists; snapshot creation must not silently convert it to pinned semantics.
   Keep rule resolution, verifier behavior, ordering, and `below_threshold_count`
   unchanged. There is no new FinalReviewResult or result-digest implementation.
7. Retry updates lifecycle/attempt state on the same row and must never rewrite
   publication pins. Rerun deliberately uses the newly read candidate rather than
   copying the original Run. Settings are internal DB state: queue messages,
   `PendingRunMessage`, HTTP contracts, and frontend require no additions.

## Implementation sequence

See [212-todo.md](212-todo.md) for bounded tasks and acceptance criteria.

- Phase 1: Write failing compatibility/digest and migrated PostgreSQL regression
  scenarios, then add the schema and immutable application contract.
- Checkpoint: historical rows remain NULL, enum reuse and upgrade/downgrade work,
  digest oracle stays unchanged, and tests fail only on missing behavior.
- Phase 2: Capture settings in all production creation paths, carry them into
  snapshots, then make postprocessing consume Run pins with historical fallback.
- Checkpoint: an actual migrated PostgreSQL scenario proves create → settings
  change → postprocess → retry → rerun semantics, using literal expected findings.
- Phase 3: Align three specifications, run required gates and real integration
  checks, review standards and issue acceptance independently, then prepare PR.

## Verification design

Unit regressions prove snapshot fields carry through and differ while
`policy_digest` remains exactly the same for identical policy content. Keep the
existing digest tests and oracle unmodified. Explicitly compare conventions-cache
keys for snapshots differing only in publication settings; change no key builder.
Exercise the postprocessing adapter's NULL fallback and new pinned query shape;
update its existing five-column fake row without weakening assertions.

A new focused `tests/test_run_publication_settings_postgres.py` should use
`portal_schema` and real SQLAlchemy stores, with only `TEST_DATABASE_URL` needed.
Do not make the acceptance tests depend on RabbitMQ when calling the stores is
sufficient. Choose explicit fixture values, for example create with `warning` and
cap `1`, change Repository to `critical` and cap `3`, process findings covering
critical, high, medium and low at valid distinct diff anchors. Assert exact
published/disposition/drop counts from the documented severity groups. Assert
both the effective settings and processed output, so a test cannot pass because
its findings are insensitive to the changed cap or threshold.

Cover these separate histories:

- Webhook creation captures both settings, then Repository changes before any
  policy snapshot or postprocessing; output still uses the original pair.
- Snapshot prepared only after that change contains the original Run pair and
  round-trips unchanged; repeated preparation never replaces it.
- Retry of the same Run after another settings change still uses original pins.
  Exercise actual lifecycle retry behavior where practical, not a second alias
  for rerun or only another freshly constructed data object.
- Completed old Run is rerun using `SqlAlchemyRerunStore`; the new Run has current
  settings while the original Run remains unchanged.
- Pre-migration Run and pre-migration snapshot retain NULL pins after upgrade;
  processing and retry use live Repository settings. Newly prepared snapshots
  for those runs retain NULL rather than freezing fallback implicitly.
- Creation rollback leaves no Run and no pins; snapshot storage rollback still
  belongs to caller UoW. Creation using a session with a preloaded stale
  Repository entity observes freshly read settings.
- Migration upgrade/downgrade/re-upgrade on a disposable schema preserves
  Repository enum ownership and rejects invalid/incomplete pin pairs.

Run focused tests red/green before the full baseline gates:
`uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy .`,
`uv run pytest`. Run the focused PostgreSQL file and relevant policy, webhook,
rerun, severity, origin suites with services available; skipped tests do not prove
the acceptance criteria. Follow `docs/TEST_PLAN.md` for the full zero-skip
integration suite and separate RabbitMQ test vhost if full integration runs.
No production networking or secrets are needed for the focused regression.
Use disposable PostgreSQL configured through `TEST_DATABASE_URL`; use a separate
RabbitMQ test vhost configured through `TEST_RABBITMQ_URL`.

## Ownership coordination

Overlap comments precede edits to the shared files listed below (AGENTS.md/backend rule).

| Open PR | Planned shared files |
| --- | --- |
| #220 | `app/modules/reviews/infrastructure/webhook_runs.py`, `app/modules/reviews/infrastructure/rerun_store.py`, `app/modules/reviews/infrastructure/run_repository.py`, `docs/PIPELINE_SPEC.md` |
| #227 | `app/modules/reviews/infrastructure/run_repository.py`, `docs/PIPELINE_SPEC.md`, `docs/CONTEXT_AND_VERIFICATION_SPEC.md`, `tests/test_sqlalchemy_run_repository.py` |
| #229 | The same planned four files as #227; avoid modifying its additional `tests/test_severity_threshold_postgres.py`, `tests/test_run_origin_postgres.py`, and `tests/test_run_lifecycle_origin_concurrency_postgres.py` unless necessary and coordinated |
| #254 | `docs/CONTEXT_AND_VERIFICATION_SPEC.md`, `docs/PIPELINE_SPEC.md`; leave `tests/test_run_policy_context_origin_postgres.py` untouched unless necessary and coordinated |

Snapshot application files, snapshot store, model, and new regression file have
no identified overlap with those PRs. The new migration revision must be checked
again against current main before publishing to avoid parallel-head collisions.
Append the new literal head/revision to the existing exact migration-graph oracle
in `tests/test_app_reinstall_schema_postgres.py`; do not replace it with a dynamic oracle.

## Documentation changes

Update PS §9's live-read and target-profile paragraphs and the severity decision
table; line numbers have shifted from the issue's historical PS:534/:536.
State legacy pins are implemented now, while verifier positioning and final-result
contracts remain future #107/#130 scope. Update CVS invariant 1 and RFS §7.1 field
table/current-state paragraph to describe actual storage, retry versus rerun,
legacy NULL fallback, unchanged digest/schema/cache, and no retroactive backfill.
Record the immediate legacy migration and historical fallback in the PR body.

## Risks and mitigations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Adding settings/schema bump to digest invalidates conventions cache | High | Do not change payload or version; existing oracle plus explicit cache-key comparison |
| Only webhook or only rerun writes pins | High | Shared candidate plus separate real-store creation tests |
| Snapshot prepared later rereads current Repository settings | High | Load only persisted Run pair; mutate settings before first snapshot in regression |
| Historical runs silently become pinned | High | NULL remains NULL; test existing and newly prepared historical snapshots |
| Stale SQLAlchemy identity map supplies obsolete settings | Medium | Refreshed scalar/entity query and same-session preload regression |
| New lock ordering crosses provider calls or deadlocks retirement | High | Preserve existing ordering; no new transaction or networking boundaries |
| Fake query tests pass while real enum/migration mapping fails | High | Real migrated PostgreSQL acceptance tests |
| Concurrent PR/main changes conflict | Medium | Coordinate overlaps and rebase before review; rerun gates after material changes |

## Open questions

None blocking. Immediate legacy adoption, no-backfill NULL fallback, unchanged
policy identity, and autonomous execution are already selected for this task.
