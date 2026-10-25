# #215 implementation checklist

Plan: [215-plan.md](215-plan.md). Status: published as ready-for-review [PR #297](https://github.com/larchanka-training/dmc-268-api-t6/pull/297); all agent deliverables are complete.

- [x] Approve the plan before implementation (agent-loop planning gate).
- [x] Coordinator rechecks open PR ownership and comments on #290, #279, #254, #227
  and #220 before shared-document edits; retain the separated scope in the plan.
- [x] Task 1: Add only the new late-switch regression in `tests/test_deploy_staging_services.py`.
  Deploy A/B, fail C pull, invoke CI rollback with the same `103-1`, assert restored B
  `.env`/state, `rolled_back=true`, no `release unchanged`, and actual rollback calls.
- [x] Task 1 RED: Move marker block after new-image pull temporarily; record behavioural
  failure of the new test. Restore `deploy.sh` exactly; keep all #179 tests unchanged.
- [x] Task 1 GREEN/checkpoint: New test and all three rollback files pass without skips;
  production diff is empty and mutation evidence is retained for review.
- [x] Task 2: Correct CICD §1 diagram/job condition and first-deploy bootstrap wording.
- [x] Task 2: Correct CICD §5 empty-image behaviour and `rollback.yml` input description:
  auto rollback/login-failure reapply current release, promotion consequence, explicit
  SHA/digest needed to go farther back.
- [x] Task 2: Correct edge-vs-ports Compose delivery, optional explicit image,
  no secrets-bundle upload in manual workflow, and TLS failure before application changes.
- [x] Task 3: Add sorted webhook-worker `REDIS_URL`; qualify verifier sample by image,
  role and configured optional values (identify all five omitted optional names).
- [x] Task 3: Add `release unchanged` as no-diagnostics path with CICD failure-table link.
- [x] Acceptance checkpoint: All five issue AC and all seven docs scope points covered;
  no runtime, monitoring, dependency or shared-staging changes.
- [x] Task 4: Prepare disposable PostgreSQL and isolated RabbitMQ test vhost; configure
  `TEST_DATABASE_URL` and `TEST_RABBITMQ_URL` following TEST_PLAN/README.
- [x] Task 4 gates: `uv run ruff check .` and `uv run ruff format --check .` pass.
- [x] Task 4 gates: `uv run mypy .` and `uv run pytest` pass with zero skips.
- [x] Task 4 review: Fresh Standards and Spec reviews return zero actionable findings;
  remediate and repeat review as required, preserving mutation and gate evidence.
- [x] Task 4 publishing: Rebase main; publisher creates `chore/215-rollback-switch-tails`,
  commits/pushes without bypassing hooks, opens PR with What/Why/How to verify/Refs,
  `Refs #215`, no closing keyword or Development link; attach PR to chat.

## After publication — human acceptance

Current-head approval and resolved threads are required before merge. The tech lead accepts
AC, closes #215 and updates board 12; these are human follow-up actions, outside agent-loop
publication. Required gates and independent review precede implementation completion.


## Task 1 verification evidence (2026-10-25)

- Ownership: coordinator rechecked open PR files; the regression test has no open-PR
  owner. Required shared-doc scope comments were posted on #290, #279, #254, #227, #220.
- Added only `test_ci_auto_rollback_after_a_failed_image_pull_restores_the_working_release`.
  A/B use runs `101-1`/`102-1`; C pull fails with `103-1`, then the actual CI helper receives
  the same `103-1`. The compatible default probe admits B; assertions cover C env/B state
  before rollback, no C up, restored B env/state, `rolled_back=true`, B pull → probe → up,
  immutable B probe/up IDs, and unchanged B `.deploy-state.previous`.
- Initial correct-code check: `uv run pytest
  tests/test_deploy_staging_services.py::test_ci_auto_rollback_after_a_failed_image_pull_restores_the_working_release
  -q` → **1 passed in 6.02s**.
- RED: moved the complete release-switch comment/marker block from before `.env` to
  immediately after `docker pull "${IMAGE}"`, preserving its contents. The same test command
  with `--basetemp=/tmp/dmc215-late-switch-red` → **1 failed in 3.18s**, exit 1. The behavioral
  assertion `assert f"rolled back to {IMAGE_B}" in result.stdout` failed: CI instead emitted
  `release unchanged: deploy run 103-1 stopped before the release switch; skipping auto rollback`.
  The retained fake host `.env` still contained literal IMAGE_C (the `c` digest).
- Restoration ran in `finally` and checked exact original bytes. `deploy.sh` also equals
  `git show HEAD:deploy/scripts/deploy.sh`; SHA256
  `886a99b3bf3e6f25baaa737b7e03c4e1578d93b0d5bbe4f9e0bd62006f85664a`.
  Removing only the new test from the current test file yields byte-for-byte HEAD content;
  all existing #179 tests are unchanged.
- GREEN: same new-test command after restoration → **1 passed in 5.72s**.
- Static gates: `uv run ruff check .` → **All checks passed**;
  `uv run ruff format --check .` → **546 files already formatted**;
  `uv run mypy .` → **Success: no issues found in 539 source files**.
- Three-file checkpoint: `uv run pytest tests/test_deploy_staging_services.py
  tests/test_rollback_revision.py tests/test_rollback_verifier.py -q` → **257 passed in
  595.67s (0:09:55), zero skips**, exit 0. Task 1 independent Standards/Spec reviews: zero findings.
  No production changes, dependencies, commits, pushes or live staging actions were made.


## Task 2 verification evidence (2026-10-25)

- Coordinator approved starting prose edits after Task 1 GREEN/restoration and zero-finding
  Standards/Spec reviews while the read-only three-file checkpoint continued; the checkpoint
  remains unchecked until actual completion.
- CICD §1 now routes failed deploy through the release-switch decision, sends early stops to
  `release unchanged`, and names previous/bootstrap rollback with §5's failure-table reference.
  The Deploy staging row states the same condition and first-deploy behavior.
- CICD §5 and the workflow image-input description name both completed auto rollback and
  failed `docker login` as cases where empty image may reapply the working release. The
  runbook covers `rolled back to …`, possible `:staging-previous` promotion to the same release,
  and explicit SHA/digest to go farther back. Verified against deploy's state copy before
  login and rollback's previous selection, auto/manual persistence, success output and
  promotion's staging-to-previous sequence.
- Qualified both Compose-from-checkout statements: rollback uses host Compose; the manual
  workflow refreshes it only for edge, while ports/Terraform uploads scripts and retains it.
  Target image may be explicit. Verified `Upload deploy files` edge guard and
  `Upload rollback scripts` ports guard/source lists in `rollback.yml`.
- Replaced the manual secrets-bundle claim with Compose/scripts delivery; workflow sends no
  secrets bundle. TLS wait now follows uploaded files, provisioning and proxy reload, and
  precedes application changes through deploy/rollback scripts. Read both workflow sequences;
  a read-only order check confirmed prepare-host < certificate-wait < deploy/rollback.
- Read the Mermaid labels using the existing literal `\n` line-break convention. All CICD
  relative file-link targets exist; §5 and «Действия при отказе» headings are present.
  `git diff --check` passes. An exact text comparison masking only the image description
  proves `rollback.yml` execution remains byte-for-byte HEAD. No wording-sensitive test
  references the changed input description; no redundant test rerun was added for prose.
- Task 2 diff is ready for independent review. Task 3 is untouched.


## Task 3 verification evidence (2026-10-25)

- Added sorted `REDIS_URL` to the webhook-worker JSON example, matching that role's staging
  environment and `Verifier.expected_keys` in `scripts/verify_rollback.py`.
- Example now qualifies image, role, configured optional secrets/configuration and lists
  the verifier's five omitted optional staging names: `GITHUB_CLIENT_ID`, `LLM_BASE_URL`,
  `LLM_MODEL`, `LLM_FALLBACK_MODEL`, `PORTAL_URL`. Checked the role allowlists in `env-file.sh`.
- Added auto-skip `release unchanged` beside revision refusal/bootstrap as a path without
  diagnostics, with a direct `CICD.md#действия-при-отказе` link. The target heading exists;
  rollback's early gate exits before diagnostics. Parsed all three sample JSON arrays:
  each is sorted and includes `REDIS_URL`.
- Task 3 diff is ready for independent review. No test was added for prose that describes
  existing contracts. Task 2 independent Standards/Spec reviews both reported zero findings.

## Task 4 gate setup (2026-10-25)

The coordinator provisioned this task's disposable `dmc215-postgres-1` (localhost:55435)
with database `app` and `dmc215-rabbitmq-1` (localhost:55675), created isolated vhost `test`,
and granted the app user vhost permissions. Both services were healthy. Final pytest uses
`TEST_DATABASE_URL=postgresql+psycopg://app:app@127.0.0.1:55435/app` and
`TEST_RABBITMQ_URL=amqp://app:app@127.0.0.1:55675/test`; it runs sequentially, without xdist,
following TEST_PLAN §2 / README Tests. No shared staging service is used.


## Base advancement before final gates (2026-10-25)

- Coordinator fetched `origin/main`: `e6d3077cfcb5584f8af0524ce4a0c8a658b2c5fb` →
  `f445589a7bc5b339ee9e14c741149abd5ce14e08` (15 commits, 86 paths; disjoint from the four
  implementation files). Final verification must run against the new base.
- Gracefully interrupted only the initial old-base full pytest process with SIGINT. It
  reported **58 passed, 2 warnings in 102.40s**, then KeyboardInterrupt/exit 2. This run is
  superseded and is not a passing gate. Its complete log is retained outside Git at
  `/tmp/dmc215-full-pytest-superseded-e6d3077.log`.
- The three-file checkpoint completed normally before the update: **257 passed**, zero skips.
  Initial full static gates passed (546 files formatted; mypy 539 sources); all four exact
  gates will be rerun on the new base with the same disposable service configuration.

- Safe update completed with all six approved file SHA256 hashes preserved through stash/apply;
  only the four implementation paths and two untracked plan documents changed afterward.
  Recovery stash `459b82236a167baf36614c5941ab3924a8b29f0e` is retained until publication;
  `/tmp/dmc215-pre-base-update.patch`, plan backups and hash manifest provide additional recovery.
- Current-base static gates passed: `uv run ruff check .` → **All checks passed**;
  `uv run ruff format --check .` → **554 files already formatted**;
  `uv run mypy .` → **Success: no issues found in 547 source files**.
  `jq` is available at `/usr/bin/jq` for the workflow contract test.
- The initial current-base native `uv run pytest` run was superseded by the Linux parity
  gate below after confirming the inherited macOS Bash 3.2 failure; it is not a passing gate.


## Genuine Linux parity gate (2026-10-25)

- Native macOS full run showed two failures inherited from current main: pristine
  `f445589` copies of `tests/test_eval_live_workflow.py` and `.github/workflows/eval-live.yml`
  in `/tmp/dmc215-pristine-f445589` reproduce
  `test_validate_uses_the_profile_of_the_selected_corpus[main-legacy]` and `[main-none]`:
  `/bin/bash: line 8: profile[@]: unbound variable`. The main-corpus empty `profile=()`
  expansion runs under `-euo pipefail`; macOS `/bin/bash` is 3.2.57. Both rules-corpus cases
  pass. Pristine reproduction: **2 failed, 2 passed in 3.73s**. These two files are unchanged
  by #215. No source/test shim or weakened assertion was used.
- Stopped only the known-failed native full Python process gracefully after that reproduction.
  Native final summary: **2 failed, 3518 passed, 2 warnings in 1455.68s (0:24:15)**,
  KeyboardInterrupt/exit 2. Retained complete native-platform log outside Git:
  `/tmp/dmc215-full-pytest-native-bash32.log`. This is superseded platform evidence, not GREEN.
- Genuine Linux runner: owned container `dmc215-linux-gates`, task-local prepared image
  `dmc215-linux-gates-env:local` (SHA256
  `df423d1e9af6a3ae2b3a5301357a1bfec643a839ea7e0b2bf0de2a9ee8b5f093`), based on existing
  `ghcr.io/astral-sh/uv:python3.13-bookworm-slim`. Installed tool prerequisites only in its
  disposable filesystem: GNU `/bin/bash` 5.2.15, git 2.39.5, jq 1.6, OpenSSL 3.0.22,
  Python 3.13.11, uv 0.9.30. `uv sync --locked --all-groups` succeeded.
- Source and Git metadata mounts are read-only. Owned volume `dmc215-linux-venv` masks
  `/workspace/.venv`; `UV_PROJECT_ENVIRONMENT=/workspace/.venv` ensures the real fake-host uv
  subprocesses also use Linux dependencies without touching the native macOS environment.
  uv/Ruff/mypy/pytest caches are writable under `/tmp`; pytest's only added option relocates
  cache_dir. No skips, xdist, test shadowing, monkeypatch, or shell replacement were added.
- Runner uses only `dmc215_default` network and this task's stopped-native-run service
  configuration: DB `app` on `dmc215-postgres-1:5432`, RabbitMQ vhost `test` on
  `dmc215-rabbitmq-1:5672`. `git rev-parse HEAD` inside is `f445589a7bc5b339ee9e14c741149abd5ce14e08`.
- Same pristine-main four-case reproduction inside Linux: **4 passed in 0.11s**;
  log `/tmp/dmc215-pristine-linux-repro.log`. The passing environment matches the workflow's
  Linux GNU Bash semantics; repository code and test expectations remain unchanged.
- Exact final commands run inside the runner as `docker exec dmc215-linux-gates uv run ...`:
  `ruff check .`, `ruff format --check .`, `mypy .`, `pytest`. Lint and format passed
  (554 files already formatted); mypy passed with **547 source files**; full pytest passed
  with **5919 passed, 2 warnings in 1436.46s (0:23:56), zero skips**, exit 0. Complete Linux
  pytest log: `/tmp/dmc215-full-pytest-linux.log`. These four completed gates are on fixed
  base `f445589a7bc5b339ee9e14c741149abd5ce14e08`.
- Task-owned Linux container/image/volume above remain available for verification until
  publication, then coordinator/publisher cleanup; preserve pre-existing base image. The
  compose stack also owns disposable PostgreSQL/RabbitMQ volumes and unused `dmc215_redis-data`.


## Final base coordination after successful full gate

- Final independent Standards and Spec rechecks on `f445589` both returned zero actionable
  findings. All implementation/acceptance, mutation, exact gate and fixed-base review items
  are verified; publication remains unchecked.
- One fetch after full completion found `origin/main` at
  `74b42a7134fb9f272d93c913344e5b826b03b523`. Its changed paths are disjoint from #215, but
  include AST CPU-limit and review publication/lifecycle code beyond the previously known
  token-cache changes. Coordinator was informed before any base adaptation, as instructed;
  The coordinator then authorized one final fixed update and the changed-test validation below.


## Final fixed-base validation and handoff (2026-10-25)

- Coordinator authorized one final advance to fixed
  `74b42a7134fb9f272d93c913344e5b826b03b523`; no further moving-main chase. Independent changes
  #168/#171/#209 and agent issue/QA tooling are disjoint from #215 deployment contracts.
- Preserved all six files through stash/apply with exact SHA256 equality and no conflicts;
  only the four implementation paths plus the two plan documents remain changed. Additional
  recovery stash `6ec05f981710afd22b6b1369a16d2a8c1a07433f` remains intact beside the earlier
  `459b82236a167baf36614c5941ab3924a8b29f0e`. Recovery hash manifest/patch/plan backups are in
  `/tmp/dmc215-pre-final-base-hashes.json`, `/tmp/dmc215-pre-final-base.patch`, and
  `/tmp/dmc215-final-backup-215-{plan,todo}.md`.
- Same genuine Linux runner and isolated PG/Rabbit services observed the new source only
  after the completed full gate. Exact final static commands passed, exit 0:
  `uv run ruff check .` → **All checks passed**;
  `uv run ruff format --check .` → **566 files already formatted**;
  `uv run mypy .` → **Success: no issues found in 556 source files**.
- Sequential focused Linux pytest used every changed top-level `tests/test_*.py` file
  between the two bases (20 files; helper `tests/ast_workers/cpu_bound.py` is not a test
  argument) plus the exact new late-switch regression. **388 passed, 2 warnings in 58.12s,
  zero skips**, exit 0. Exact command: `/tmp/dmc215-final-focused-command.txt`; log:
  `/tmp/dmc215-final-focused-linux.log`; selected argument list:
  `/tmp/dmc215-final-focused-tests.json`. This result is a focused final-base check, not a
  claim that the full suite ran on this later base. The **5919-pass** complete suite remains
  explicitly tied to `f445589` above.
- Coordinator's final independent Standards and Spec reviews on `74b42a7` both reported
  **zero actionable findings**. Open-PR ownership was rechecked unchanged; all required scope
  comments exist. Runtime script still equals HEAD and all pre-existing #179 test bytes are
  unchanged when only the new test is removed. `git diff --check` passes.
- Implementation/gates/review are complete and ready for publication. Publisher checkbox
  intentionally remains unchecked until actual publication. No feature commit, push or PR
  was created by the development agent. Both recovery stashes and the owned Linux runner,
  image, venv volume and disposable service stack remain intact for publisher verification;
  coordinator cleans them after publication. Development stops editing at this handoff.


## Publication evidence (2026-10-25)

- Publisher fetched current main immediately before publication: it still matched final
  validation base `74b42a7134fb9f272d93c913344e5b826b03b523`. No further base update was needed.
- Created branch `chore/215-rollback-switch-tails`; staged the two plan files by exact path
  and tracked modifications with `git add -u`. Implementation commit `051a5bf` includes only
  the six approved paths. All configured applicable commit hooks passed; no bypass was used.
- Pushed the branch and created normal ready-for-review
  [PR #297](https://github.com/larchanka-training/dmc-268-api-t6/pull/297), with the required
  What / Why / How to verify / Refs sections, template checklist and `Refs #215`. No reviewer
  was assigned because the user will assign one. The PR was attached to this chat.
- Publication is recorded only after successful PR creation/attachment in this docs-only
  follow-up. Agent deliverables are complete. Human current-head approval, resolved review
  threads and tech-lead acceptance remain separate; no merge, issue closure or board update
  was performed. Both recovery stashes and task Docker resources remain for the coordinator.
