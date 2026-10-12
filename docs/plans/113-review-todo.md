# PR #132 review correction checklist (#113)

Original `113-plan.md` / `113-todo.md` are preserved. Details, ownership, criteria and
verification: [113-review-plan.md](113-review-plan.md).

## Preparation — coordinator

- [x] Rebase on fetched main 9aedfa8 (includes #134); preserve #108 design/API terminology.
- [x] Recheck open PR file ownership and comment before overlapping edits.
- [x] Record actual tested base/head; companion #127/#125/#126/#121 remain unmerged.
- [x] Confirm dedicated PostgreSQL and RabbitMQ test vhost; no shared production queues.

## Developer A — archive onboarding and schema

- [x] Red PG test P2-1(a): added after first OAuth, before later repo confirmation.
- [x] Red PG test P2-1(b): org first-user subset, created replay, second-user repo.
- [x] Implement atomic positive-upsert deferral for unique same-account archive.
- [x] Skip default-rule onboarding for explicit deferred result; no fresh target row/grant.
- [x] PG P2-1(c): late deletion permanently clean target, including repeated OAuth.
- [x] Preserve pause/settings/children/Workspace and per-user access assertions.
- [x] Red P2-2 schema row: partial revocation/no newer grant + enabled=true.
- [x] Correct unmerged 0029 partial branch; keep whole-deletion unconditional.
- [x] P3-1 seeded downgrade folds availability into enabled before drop.
- [x] P3-5 deterministic callback-versus-retirement locking proof.

## Developer B — lifecycle and provider fences

- [x] Red P2-3 PG reconciliation for retired queued/running/publishing states.
- [x] Red retryable `_fail` after origin retirement, no orphan/retry publication.
- [x] Terminalize retired work via owned/state-guarded writes, no provider I/O.
- [x] Preserve fail-closed run/check/review pointer projections and terminal history.
- [x] P3-13 claim uses cancelled/installation_unavailable for inactive origin.
- [x] P3-7 FOR SHARE installation/repository fences; retain exclusive Run writes.
- [x] Deterministic retirement/rebind blocking and concurrent reader/FK tests.

## Developer C — external audit and cancellation proof

- [x] Red P2-4 real gateway retirement-after-POST audit/review_id test.
- [x] Preserve append-only POST audit while fencing finalization/repeated POST.
- [x] P2-5 RUNNING + retired origin context write raises and stores no payload.
- [x] Prove context-origin guard removal makes new test fail.
- [x] P3-6 HTTP PG file owns skip fixture/shared helper outside test_* module.
- [x] P3-11 projector retirement/receipt info diagnostics; B owns update_check_run log.
- [x] P3-12 classification cancellation has no omission or warning; preserve guard.
- [x] Assess redundant post-price checkpoint; retain early projector guard.

## Documentation — developer C after source tasks

- [x] Developer C owns documentation/projector docstring edits; coordinator stays out of
      codebase edits during development.
- [x] P3-4 update stale re-enable wording in WEBHOOK_WORKER and projector docstrings.
- [x] Document permanent late-delete clean target and no inferred legacy identity.
- [x] P3-10 catalogue installation_replaced/unavailable in PIPELINE_SPEC §6, T6/T11.
- [x] P3-1 document irreversible identity loss on downgrade.
- [x] P3-9 runbook stops old Run writers before origin migration.
- [x] P3-2 PR notes original #106/#57 legacy history cannot be restored.
- [x] P3-3 PR notes rerun paused/unavailable 409; record inaccurate current UI wording.
- [x] P3-8 reply accepts strict checkpoint cost; no softened retirement protection.

## Final verification and existing-PR publication

- [x] Focused red-green evidence for all five P2 comments recorded.
- [x] Run whole-plan final gates after source corrections, with focused checks per task.
- [x] uv run ruff check .
- [x] uv run ruff format --check .
- [x] uv run mypy .
- [x] uv run pytest -rs with real PostgreSQL/RabbitMQ; zero integration skips.
- [x] Independent Standards and Spec reviews on current main-to-head; fix findings.
- [x] Commit intended correction files with Refs #113; configured pre-commit checks run.
- [ ] Push existing PR branch with lease-safe rebase publication; no new PR.
- [ ] Update #132 body with actual base/head/counts and remaining combined-head caveat.
- [ ] Reply to all five review threads with SHA/test evidence; do not resolve them.
- [ ] Post all thirteen P3 dispositions and UI limitation; attach #132, monitor CI.
- [ ] After companion PRs land: rebase #132 last, preserve #121 outcome/origin kwarg,
      #127 AuthScope text, test waiting old CI receipt and run combined-head gates.
- [ ] Reviewer resolves threads and approves current head; tech lead handles issue AC.


Developer evidence: [A](113-review-a.md), [B](113-review-b.md),
[C](113-review-c.md). Source tasks, final gates and independent reviews are complete; publication and
external before-merge requirements remain pending. Final evidence:
[113-review-final.md](113-review-final.md). Companion combined-head validation is an
external before-merge prerequisite.
