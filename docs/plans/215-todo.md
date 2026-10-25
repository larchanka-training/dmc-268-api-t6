# #215 implementation checklist

Plan: [215-plan.md](215-plan.md).
Delivery: [PR #297](https://github.com/larchanka-training/dmc-268-api-t6/pull/297).

## Completed implementation

- [x] Approve the plan and coordinate shared-document ownership before editing.
- [x] Add only the late-switch image-pull regression with the same nonempty deploy/CI run ID;
  verify B env/state restoration, rollback output, immutable identity and call order.
- [x] Prove RED with the late-marker mutation, restore `deploy.sh` exactly, then prove GREEN.
  Preserve every pre-existing #179 test and all runtime scripts.
- [x] Align CICD diagram/job condition, first-deploy bootstrap, manual image selection,
  Compose delivery, secrets-bundle wording and TLS/application ordering with the workflows.
- [x] Add sorted webhook-worker `REDIS_URL`, qualify optional-key examples and link all
  no-diagnostics paths to the CICD failure table.
- [x] Run initial gates and independent Standards/Spec reviews; publish PR #297.

## Historical validation — 2026-10-25, before this review follow-up

These results describe earlier source snapshots; they are not final-revision gate claims.

- The new test passed on correct code, failed when the marker block moved after the new-image
  pull, and passed after exact script restoration. The failed assertion expected
  `rolled back to …B`; CI emitted `release unchanged: deploy run 103-1 stopped before the
  release switch; skipping auto rollback`, leaving C in `.env`.
- The exact three-file rollback command in the plan passed: **257 passed, zero skips**.
  Removing only the new 35-line regression restored the original test file byte-for-byte.
- A complete genuine-Linux gate run passed all four required commands, including
  **5919 passed, 2 warnings, zero skips** for `uv run pytest`, with disposable PostgreSQL and
  an isolated RabbitMQ test vhost. Source mounts were read-only; no test or shell shim was used.
- After a later main update, static gates passed and a focused Linux run of 20 changed test
  files plus the new regression passed: **388 passed, 2 warnings, zero skips**. This was a
  focused check; the complete 5919-test run preceded that update.
- Native macOS Bash 3.2 reproduced two unrelated empty-array failures in unchanged eval
  workflow tests on pristine main. The corresponding four-case Linux reproduction passed.
  The interrupted native full run is not a passing gate.
- Independent Standards and Spec reviews reported zero actionable findings before publication.
  Applicable commit hooks passed without bypasses.
- The review independently confirmed **257 passed, zero skips** across the rollback files.
  Its late-marker mutation yielded **1 failed / 256 passed**, only the new regression RED;
  putting the marker between env writing and pull kept the new test GREEN.
- Published-head [CI run 40184882105](https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/40184882105)
  passed **5985 tests, zero skips**: 4997 unit and 988 integration tests. This result predates
  the review-follow-up rebase and edits.

## Review follow-up — 2026-10-25

- [x] Rebase onto the pinned fetched main without conflicts; recheck current ownership and
  coordinate with #280, #279, #227 and #220 before shared-document edits.
- [x] P2: make empty-image reapplication unconditional after completed application auto rollback
  or deploy login refusal; describe promotion's same-release previous update when staging pulls.
- [x] P3-1: describe refusal before application changes, CI `release unchanged` with exit 0
  while the job fails, and manual rollback's missing-password refusal when targeting an image.
- [x] P3-2: describe absent-previous bootstrap restoration, its output and skipped promotion.
- [x] P3-3: state that auto rollback uses Compose already uploaded by failed run N+1; retain
  manual edge refresh versus ports/Terraform script-only refresh.
- [x] P3-4: give `release unchanged` its own terminal node without a rollback error claim.
- [x] P3-5: replace session chronology with durable scope, completed work and dated evidence;
  remove local artifact paths, recovery details, obsolete commit/base references and chat prose.
- [x] Compare corrections with deploy/rollback scripts, upload/prepare ordering, promotion and
  the CI failure step. Add no prose-mirroring tests; preserve the original regression/runtime.
- [x] Local `uv run ruff check .`: all checks passed; `uv run ruff format --check .`:
  **590 files already formatted**; `uv run mypy .`: **580 source files, no issues**.
- [x] Rerun the exact three rollback files in genuine Linux on this rebased revision:
  **257 passed, zero skips in 81.10s**. Source and Git mounts remained read-only.
- [x] Run independent Standards and Spec reviews across the complete six-path PR delta:
  **zero actionable findings each**.

Publication and final-head verification: use [PR #297 checks](https://github.com/larchanka-training/dmc-268-api-t6/pull/297/checks)
to confirm the required unit and integration jobs pass with zero skips on the current head.
Historical full-suite results above do not certify the current head.

Current-head approval, resolved review threads, tech-lead acceptance, issue closure and board
updates remain required after publication; this checklist does not claim them complete.
