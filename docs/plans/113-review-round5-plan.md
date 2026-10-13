# Historical PR #132 round-five corrections (#113)

This completed plan records checkpoint `b5e0cec92c24a8e702a90e75de5a10350f89cdaa`
on main `11ddfabf31a3345e6d21cdf57586281cb62cee7c`; it is not a plan or verification
claim for later heads. The [2026-10-12 review](https://github.com/larchanka-training/dmc-268-api-t6/pull/132)
requested two P3 documentation corrections. Round five integrated merged #121/#126,
then corrected rollback recovery and completed historical checklist entries.

## Base and dependencies

Combined main: `11ddfabf31a3345e6d21cdf57586281cb62cee7c` (#121 and #126 merged).
Historical round-four PR checkpoint: `8a87fe9d10794ba92002f537b349f84ee7dbdac9`.
Round-five rebased parent: `b4fb4a24c4ff8cbcf6256c289565283c51b80851`.
Rebase and source-conflict resolution completed at this checkpoint; its gates and
CI are recorded in the [round-five final proof](113-review-round5-final.md).
The integration preserved #125 bootstrap/worker configuration, #127 AuthScope
and #108/#134/#138 behavior.

## 1. Integrate #121 (medium)

Rebased on main `11ddfab` and combined `RunTriggerOutcome` with mandatory
keyword-only `expected_installation_external_id`. Typed receipt retry decisions,
installation fences and upstream regression tests were retained; affected
fakes/assertions were adapted. The waiting CI receipt after superseded old-head
work completed was verified against real PostgreSQL, including restart/redelivery
and obsolete-head controls. The completed
[integration proof](113-review-round5-integration.md) records 333 focused passes and
zero skips. Dependencies: none.

## 2. Correct recovery documentation (medium; after 1)

Files: `docs/CICD.md`, round-four plan/todo/documentation/final proofs; PR body.
After schema 0030, an image lacking 0030 fails bootstrap with an unknown revision;
`api`, `worker` and `webhook-worker` cannot pass `service_completed_successfully`.
Before #126, rollback changes containers and can leave staging without services;
after #126, revision validation rejects that image before container changes.
Recovery requires a fix-forward image containing 0030. NULL-origin insertion concerns
only independent old writers outside compose; only the new worker cancels their
eligible nonterminal Runs. The corrections retained writer shutdown before merge,
webhook redelivery,
downgrade data loss and both deployment paragraphs, and documented the merged
#126 guard as actual behavior; rejection does not undo a failed deploy.

Round-four publication steps were marked complete using existing commit/CI
evidence; historical SHAs, test counts and timings were preserved. Round-five
completed the outstanding #121/#126 integration, typed trigger and combined-head
verification items; human approval and shutdown/merge timing remained external.
UI wording remains tracked by
[UI #105](https://github.com/larchanka-training/dmc-268-ui-t6/issues/105) and
[API #106](https://github.com/larchanka-training/dmc-268-api-t6/issues/106#issuecomment-6131037958).

## Verification and publication

Rollback/Compose/Alembic behavior was checked against source; no old-image runtime
reproduction or production deployment was performed. No prose-only tests were
added. `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy .` and
configured PostgreSQL/RabbitMQ `uv run pytest -rs` passed: 3132 tests, including
538 integration cases, zero failures/skips. Independent Standards and Spec
reviews found no actionable issues. Normal commit hooks passed; all nine required
jobs in [CI run 38373267208](https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/38373267208)
succeeded on `b5e0cec92c24a8e702a90e75de5a10350f89cdaa`. Detailed counts and timings
are preserved in the [final proof](113-review-round5-final.md).

At round-five publication, current-head approval and writer shutdown/merge timing
remained external. Later main changes require a new rebase and combined-head
verification before merge.
