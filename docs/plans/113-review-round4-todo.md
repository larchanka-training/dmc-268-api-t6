# PR #132 round-four todo (#113)

Historical plan/todo and prior gate counts remain historical evidence.

## Planning and integration

- [x] Read the 19:08 UTC review and repository rules/skills.
- [x] Verify actual main 620c81d; #125 merged, #121/#126 still open.
- [x] Rebase before edits: e417c2ec5af2f7e5556614c38474743e31ebaa98 on main620c81d.
- [x] Preserve upstream #125 env/config/tests and both CICD bootstrap/rollout contracts.
- [x] Record rebase: only CICD conflict; app/alembic unchanged; nine upstream main files.

## Developer A — P3-3

- [x] Strengthen five origin/pause cases with observable update_check_run assertion.
- [x] Preserve literal DB outcomes and zero pipeline/retry/provider work.
- [x] Reordered disabled-before-origin guard mutation fails (real PostgreSQL).
- [x] Restore exact production source; focused original implementation green.
- [x] Save portable command/count/mutation proof; no private local paths.

## Developer B — P3-1/P3-2/P3-4

- [x] Remove private paths/DSNs from every docs/plans/113 proof document.
- [x] Preserve truthful historical SHAs, source scope, counts and timings.
- [x] Make round-three final proof reader-facing; retain verified historical evidence.
- [x] CICD documents missed webhook deliveries and operator manual redelivery.
- [x] CICD recovery description corrected in round five: blocked bootstrap and fix-forward; independent old writers only.
- [x] New-image-only wording qualified to successful path; preserve #125/#126 contracts.
- [x] Verify webhook wording against primary GitHub documentation and runbook against source.
- [x] Private-reference sweep and git diff --check clean; save portable proof.

## Freeze, gates and review

- [x] All mutation windows ended; production source restored.
- [x] uv run ruff check .
- [x] uv run ruff format --check .
- [x] uv run mypy .
- [x] uv run pytest -rs with configured PostgreSQL and RabbitMQ; zero skips.
- [x] Record actual integration collection count and current-head execution results.
- [x] Fresh Standards review: zero actionable findings.
- [x] Fresh Spec review: zero actionable findings.

Static full gates passed: Ruff check; 386 files already formatted; mypy found no issues
in 383 source files. Full pytest: 2897 passed, 2 existing warnings in 363.02s, zero skips.
All 503 collected integration cases ran in that full suite; collection took 1.40s
(503 selected / 2394 deselected). Independent Standards and Spec reviews: 0 findings
on each axis. See [round-four final proof](113-review-round4-final.md).

## UI issue and existing PR publication

- [x] Verify [UI #105](https://github.com/larchanka-training/dmc-268-ui-t6/issues/105) read-back and board 12 Todo placement; link in [API #106](https://github.com/larchanka-training/dmc-268-api-t6/issues/106#issuecomment-6131037958).
- [x] Link UI #105 in the published PR #132 body on `8a87fe9`.
- [x] Prepare PR body cleanup and links to historical/current plans/todos.
- [x] Record tested base/source, evidence and companion status; no future-integration claims.
- [x] Normal-hook commit Refs #113, explicit intended staging and verified leased push.
- [x] Publish one concise summary responding to all four P3 observations and UI #105, with SHA/evidence; do not resolve threads.
- [x] Required CI green on published head; report existing PR and follow-up issue URLs.

Published head `8a87fe9d10794ba92002f537b349f84ee7dbdac9`; required
[CI run 38298570137](https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/38298570137)
succeeded. See the historical publication section of the round-four final proof.

## External merge prerequisites pending at this historical checkpoint

- [ ] #121/#126 merged; #132 rebased last on actual combined main.
- [ ] Both RunTriggerOutcome and expected_installation_external_id retained.
- [ ] Waiting old-head CI receipt regression and all gates/CI verified on combined head.
- [ ] Current-head human approve and reviewer-owned thread resolution.
- [ ] Tech lead coordinates merge timing and stops every old writer before merge.
