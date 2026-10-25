# Implementation Plan: #215 rollback switch boundary and runbook tails

Status: ready for publication; implementation, verification and final review are complete.
Issue: https://github.com/larchanka-training/dmc-268-api-t6/issues/215
Base inspected: `origin/main` / `e6d3077cfcb5584f8af0524ce4a0c8a658b2c5fb` (2026-10-25).
Full-suite verification base: `f445589a7bc5b339ee9e14c741149abd5ce14e08`.
Final publication base: `74b42a7134fb9f272d93c913344e5b826b03b523` (one final disjoint update;
all 20 changed test files plus the new #215 regression and all static gates passed).

## Outcome and boundaries

Protect the late edge of the release switch with a behavioural regression and correct the
rollback documentation. The current production marker order is correct: `deploy.sh` writes
`.deploy-switch`, then `.env`, then pulls the new image. No runtime change, dependency,
migration, shared staging exercise, or monitoring edit is planned. Preserve all existing
#179 tests unchanged. Issue line numbers have drifted; locate sections by their text.

## Decisions and dependency order

1. Obtain plan approval and coordinate overlapping files before editing them.
2. Add one regression using the existing `Host`, `STUB_PULL_FAILURE` and
   `_ci_auto_rollback` helpers; prove it detects the specified mutation.
3. Correct CICD/workflow wording and SECRETS examples in separate small tasks.
4. Run the required gates with zero skips, then independent Standards/Spec review and
   publishing. No API, service simulation, or live rollback setup is necessary for this scope.

Documentation tasks may proceed independently after coordination and the test checkpoint;
keep each shared documentation file under one editor. No schema/application dependency exists.

## Task 1: Detect a pull failure after the release switch

**Description:** Add a behavioural regression in `tests/test_deploy_staging_services.py`.
Start healthy releases A then B using distinct run IDs, making B the working release N.
Attempt C (N+1) with `DEPLOY_RUN=103-1` and `STUB_PULL_FAILURE=IMAGE_C`, then call
`_ci_auto_rollback(host, "103-1")`. Keep the default revision probe compatible.

**Acceptance criteria:**

- [x] C deploy fails at its pull after `.env` has become C, while `.deploy-state` is
  still B; no C compose-up occurs. Avoid an assertion on the marker's location/content
  that would make the required mutation fail before exercising the CI behaviour.
- [x] The CI rollback succeeds, reports `rolled back to IMAGE_B`, contains no
  `release unchanged`, restores `.env` IMAGE and state current_image to literal B,
  and records `rolled_back=true`. Calls prove B pull → compatible probe → B up using
  B's immutable ID; `.deploy-state.previous` remains B.
- [x] Existing #179 tests remain byte-for-byte unchanged and green. Both deploy and CI
  receive the same nonempty run ID; no legacy fallback or migration guard decides the case.

**Verification / test-first proof:** First write the new test without changing production.
It should pass the correct current code. Temporarily move the complete marker-write block
from before `.env` to immediately after `docker pull "${IMAGE}"`, preserving its contents.
Run the new test alone and record RED: CI emits `release unchanged` and leaves `.env` on C
instead of B (failure must come from behaviour assertions, not syntax/setup). Restore the
original script byte-for-byte, rerun the new test GREEN, then run:

```sh
uv run pytest tests/test_deploy_staging_services.py tests/test_rollback_revision.py tests/test_rollback_verifier.py -q
```

Record command, failure assertion, result and restored-script check in review/PR evidence.
Do not commit the mutation. A green test before mutation is expected because this issue
closes a coverage gap in already-correct code; mutation supplies the meaningful RED phase.

**Dependencies:** approved plan; test-file ownership rechecked before editing.
**Files likely touched:** `tests/test_deploy_staging_services.py`; `deploy/scripts/deploy.sh`
only as an uncommitted, reverted mutation.
**Estimated scope:** Small (one committed test file).

### Checkpoint: regression proof

- [x] New regression is RED under only the specified mutation and GREEN after restoration.
- [x] Three rollback test files pass without skips; existing tests and runtime code are unchanged.

## Task 2: Make the rollback runbook match workflow behaviour

**Description:** Edit the relevant `docs/CICD.md` fragments and only the `image` input
description in `.github/workflows/rollback.yml`; preserve workflow execution.

**Acceptance criteria:**

- [x] CICD §1 diagram and Deploy staging job row make automatic rollback conditional on
  crossing the release switch, reference §5's failure table, and cover bootstrap on first
  deployment. Do not imply every deploy failure restores a previous image.
- [x] CICD §5 manual rollback and workflow input explain that empty `image` selects
  `.deploy-state.previous`: after completed auto rollback or failed `docker login`, this
  may reapply the working release; it prints `rolled back to …`, promotion can move
  `:staging-previous` to that same release, and going farther back requires explicit SHA
  or digest. Keep the workflow description concise but include both conditions and the remedy.
- [x] Correct all remaining scope fragments: Compose comes from checkout for edge only
  (ports/Terraform refresh scripts and retain Compose); target image can be explicit;
  manual rollback uploads Compose/scripts, not a secrets bundle; TLS wait occurs after
  host/proxy preparation and fails before application changes by deploy/rollback scripts.

**Verification:** Compare wording with rollback workflow upload guards, prepare-host and
certificate ordering; `rollback.sh` target selection and manual/auto persistence;
`deploy.sh` login/switch order; workflow promotion. Render/read the Mermaid labels and check
relative links. Run the three rollback files again only if wording-sensitive tests require it;
all final gates remain mandatory.
**Dependencies:** Task 1 regression GREEN, restored production script and independent review;
required comments on overlapping PRs before edits. Coordinator approved Task 2 prose work
while the unchanged-suite checkpoint continues; that checkpoint remains mandatory.
**Files likely touched:** `docs/CICD.md`, `.github/workflows/rollback.yml`.
**Estimated scope:** Small (two files).

## Task 3: Correct the environment-key example

**Acceptance criteria:**

- [x] Add `REDIS_URL` in sorted order to the webhook-worker example, matching staging
  Compose and `scripts/verify_rollback.py` expectations.
- [x] State that names depend on image, role and configured optional secrets/configuration;
  this isolated verifier sample omits staging's optional `GITHUB_CLIENT_ID`, `LLM_BASE_URL`,
  `LLM_MODEL`, `LLM_FALLBACK_MODEL` and `PORTAL_URL`.
- [x] Include auto-skip `release unchanged` alongside revision refusal and bootstrap as a
  path without env diagnostics, linking to CICD §5 “Действия при отказе”.

**Verification:** Compare only the documented sample and explanation against
`deploy/compose/staging.yml`, verifier key sets, `env-file.sh` optional allowlists and
`rollback.sh` early gate. Check sorted JSON names and relative anchor. No new test is needed
for prose that mirrors these existing contracts.
**Dependencies:** Task 1 checkpoint; required comments on overlapping PRs before edits.
**Files likely touched:** `docs/SECRETS.md`.
**Estimated scope:** Small (one file).

### Checkpoint: acceptance coverage

- [x] Every issue AC and all seven documentation scope points are covered by Tasks 1–3.
- [x] No workflow runtime, marker mechanism, login/previous order, monitoring or staging changes.

## Task 4: Verify and publish the reviewed change

**Acceptance criteria / verification:**

- [x] Provision explicitly disposable local PostgreSQL and an isolated RabbitMQ `test`
  vhost following `docs/TEST_PLAN.md` §2 / README Tests; export `TEST_DATABASE_URL` and
  `TEST_RABBITMQ_URL`. Run all four exact gates: `uv run ruff check .`,
  `uv run ruff format --check .`, `uv run mypy .`, `uv run pytest`. Require zero failures
  and zero skips. Missing service configuration or skips are unresolved verification,
  not a successful gate. Do not use shared staging, weaken tests, or add skips.
- [x] Independent code-review skill checks Standards and Spec against the fixed base/issue,
  with zero actionable findings. Review the final diff for restored mutation, unchanged
  #179 tests, all documentation cases and overlap scope before marking tasks complete.
- [ ] Rebase on current main, rerun affected verification after changes, and publish through
  the agent-loop publisher on `chore/215-rollback-switch-tails`. Conventional PR title ≤72
  characters; body What / Why / How to verify / Refs, `Refs #215`, no closing keyword or
  Development-panel link. Return and attach the reviewed PR; this is the agent-loop
  delivery boundary.

**Dependencies:** Tasks 1–3; disposable service availability; independent review.
**Files likely touched:** planning checklists/evidence only; no new dependency planned.
**Estimated scope:** Small (verification and publication).

## After publication — human acceptance

One approving review on the current head and resolved review threads are merge requirements.
The tech lead accepts AC, closes the issue and updates board 12. These human actions follow
the agent-loop delivery; this plan does not authorize approval, merge or issue closure.

## Open-PR coordination and risks

Before implementation, the coordinator comments on every open PR owning shared docs, as
required by AGENTS.md, explaining the narrowly separated #215 hunks. Do not post during
planning. Recheck current PR state/diffs when starting edits and after rebasing.

| PR | Existing documentation scope inspected | #215 separation |
| --- | --- | --- |
| #290 | SECRETS LLM gateway/verifier variables | Only env-key example and rollback diagnostics in #215 |
| #279 | CICD §2.3 monitoring acceptance; SECRETS Grafana password row | No monitoring edits in #215 |
| #254 | CICD migration-loss table and revision list | Only rollback semantics/preparation wording in #215 |
| #227 | SECRETS GitLab base-URL row | No GitLab configuration edits in #215 |
| #220 | SECRETS installed GitLab configuration and staging setup | Preserve those hunks; #215 changes later env diagnostics block |

No exact target-hunk overlap was present at planning time; file-level ownership still
requires comments. The mutation must fail through the CI run gate, not because of a direct
marker-order assertion. Full-suite service availability is a verification prerequisite;
current baseline uses fake Docker and does not require PostgreSQL or a Docker daemon.
No product/spec ambiguity blocks the approved work. The user approved the plan and
execution through PR on 2026-10-25; the coordinator rechecked open PR ownership and posted
the required scope comments on #290, #279, #254, #227 and #220 before shared-document edits.

## Planning baseline

On 2026-10-25, the exact issue-required three-file command above completed successfully:
**256 passed in 367.41s (0:06:07), zero skips**, exit code 0. The issue's historical
254-test count has grown on the inspected main. This run used fake Docker and required
no PostgreSQL or Docker daemon. Initial sandbox attempts could not access uv's cache or
create the managed worktree's `.venv`; an approved escalated run created it with uv
(86 installed packages). No implementation, mutation, full-suite gate or live staging
exercise has been performed during planning.
