# PR #132 round-five corrections (#113)

The [2026-10-12 review](https://github.com/larchanka-training/dmc-268-api-t6/pull/132)
requests two P3 documentation corrections. Integrate merged #121/#126 first, then
correct rollback recovery and completed historical checklist entries.

## Base and dependencies

Combined main: `11ddfabf31a3345e6d21cdf57586281cb62cee7c` (#121 and #126 merged).
Historical round-four PR checkpoint: `8a87fe9d10794ba92002f537b349f84ee7dbdac9`.
Round-five rebased parent: `b4fb4a24c4ff8cbcf6256c289565283c51b80851`.
Rebase and source-conflict resolution are complete; no current-base test or CI
result is inferred from the historical checkpoint. Recheck main before publication.
Preserve #125 bootstrap/worker configuration, #127 AuthScope
and #108/#134/#138 behavior.

## 1. Integrate #121 (medium)

Rebase on actual main; combine `RunTriggerOutcome` with mandatory keyword-only
`expected_installation_external_id`. Retain typed receipt retry decisions,
installation fences and upstream regression tests. Adapt affected fakes/assertions.
Verify the waiting CI receipt after superseded old-head work completes against real
PostgreSQL, including restart/redelivery and obsolete-head controls. The completed
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
eligible nonterminal Runs. Retain writer shutdown before merge, webhook redelivery,
downgrade data loss and both deployment paragraphs. Describe the merged #126 guard as actual behavior; rejection does not undo a failed deploy.

Mark completed round-four publication steps using existing commit/CI evidence. Preserve
historical SHAs, test counts and timings. Keep unfinished integration/approval/merge
conditions unchecked. UI wording remains tracked by
[UI #105](https://github.com/larchanka-training/dmc-268-ui-t6/issues/105) and
[API #106](https://github.com/larchanka-training/dmc-268-api-t6/issues/106#issuecomment-6131037958).

## Verification and publication

Check rollback/compose/Alembic behavior against source; use an isolated schema for
an unknown-revision reproduction if needed. No permanent prose-only tests.
Run `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy .` and
`uv run pytest -rs` with PostgreSQL/RabbitMQ configured. Record actual integration
counts and zero skips; keep historical evidence distinct. Run independent Standards
and Spec reviews, normal commit hooks and required CI on the published head.

Before #132 merges: recheck actual combined main, preserve
its revision guard in both CICD passages, repeat combined-head verification, obtain
current-head approval and agree writer shutdown/merge timing with the tech lead.
