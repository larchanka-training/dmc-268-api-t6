# Historical unpublished PR #132 round-seven plan (#113)

This plan records the unpublished P3 checkpoint on `075004e` plus its review-fix
delta, based on main `2251b59`. Its verification does not cover the later #136
integration; see the [round-eight plan](113-review-round8-plan.md).

Address all four P3 findings in the [2026-10-13 review](https://github.com/larchanka-training/dmc-268-api-t6/pull/132#pullrequestreview-5503707715) with regression tests and documentation corrections. Scope excludes GitLab ingestion, new public error codes, dependencies and persistent production changes.

## Base

Starting PR head: `075004e996db907b19787c91322ff8b62e43825e`, based on main `2251b5936822827fc75b38cfc6b352c6a5e89a6b` (#121/#126/#137 merged). Historical results verify only their stated checkpoints. Verify actual main again before publication and merge; a changed base requires rebase and repeated gates.

## GitLab origin — P3-1

Files: `tests/test_run_publication_origin_postgres.py` and [GitLab origin proof](113-review-round7-a.md).

- Add active/unknown GitLab origins using migrated PostgreSQL, the existing publication fixture, real lifecycle store/UoW and a typed gateway fake.
- Exercise `publish_context`, `lock_for_claim` and `PublishRunReview` with `reviews=None`. Assert `gitlab/101`, context/claim origin activity, literal `succeeded/None` with one gateway call for project 101 or `cancelled/installation_unavailable` without a gateway call. Assert zero GitHub/check-run calls.
- Prove isolated GitLab-always-active and GitLab-always-inactive regressions fail at origin assertions. Restore exact `run_origin.py` bytes after each mutation; require zero skips in the restored focused suite.

Completed focused evidence: 40 passes, zero failures/skips; both mutations failed meaningfully and production bytes were restored. Focused Ruff, format and mypy checks passed. These results do not verify the combined full suite.

## Waiting receipt — P3-2

Files: `tests/test_webhook_run_trigger_postgres.py` and [waiting receipt proof](113-review-round7-b.md).

- Persist the #121 waiting receipt, retire installation 17, rebind the same Repository to installation 29 and finish the old head. Cover retry/redelivery with unchanged-installation controls; assert same-GUID duplicates and new-GUID receipt semantics.
- Assert literal pending/terminal receipt fields, retry/attempt/claim/lease state, immutable old origin 17 and messages. Only controls create a new-head Run with origin 17; rebound cases create none and perform no installation 29 provider calls.
- Prove a combined-fence regression reaches the forbidden rebound authority/Run path and fails meaningfully. Restore every changed production file exactly and pass the focused PostgreSQL suite with zero skips.

The combined mutation produced two meaningful failures, and production source was restored. The full focused waiting-receipt suite passed 42 tests with zero failures/skips in 99.73s; its proof is complete.

## Documentation — P3-3/P3-4

Files: `docs/SYSTEM_DESIGN.md`, round-five final/docs/plan, round-four todo, [documentation proof](113-review-round7-c.md) and the prepared PR body.

- Require future GitLab Run writers to set `origin_provider_installation_id`; clarify non-GitHub wording of `installation_unavailable` when ingestion is implemented. Preserve existing publication/future-ingestion scope.
- Mark round-five evidence historical and tie verification claims to its actual checkpoint/results. Preserve historical SHAs, counts and timings. Mark completed round-four #121/#126 integration, typed trigger and waiting-receipt/gates items using round-five/six evidence.
- Keep the whole PR body concise in What / Why / How to verify / Refs order. Verification contains gate commands, verified final-head counts and the round-seven final proof. Every document URL uses the final published head, including historical proof links. Final head/count tokens are substituted only after their verification.

Documentation edits and the body template are complete. Prose/source inspection, portability sweep, section/link checks and `git diff --check` passed. Only verified final-source results are asserted in the new body; historical counts remain in their checkpoint proofs. No prose-only tests are needed.

## Final verification and publication

After all temporary mutations are restored, freeze source and run with PostgreSQL/RabbitMQ configured:

- `uv run ruff check .`
- `uv run ruff format --check .`
- `uv run mypy .`
- `uv run pytest -rs`

Record actual full/integration counts, zero failures/skips and mutation evidence in [round-seven final proof](113-review-round7-final.md). Run fresh independent Standards and Spec reviews of the actual main-to-final-working-tree delta. Publish through normal hooks to the existing PR branch, substitute verified head/counts in the body, verify required CI and respond once to the four P3 findings. Reviewers resolve their own threads. All four gates and both independent reviews are complete; the final proof records 3196 passes including 549 integration cases, zero failures/skips. This checkpoint was not published and has no required-CI result. Later combined-source verification is recorded separately.

## External merge prerequisites

Squash only: intermediate commits have failing checks. Stop every old Run writer before merge/main auto-deploy; agree timing with @axyi and obtain final-head approval with reviewer-owned threads resolved. If main advances before merge, rebase and rerun gates on that exact head. No production operations or merge are performed by this plan.
