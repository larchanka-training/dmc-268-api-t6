# Task list: #335 GitLab staging configuration

Integration baseline `f160ce4c82220c20e627fbfdc0230b43a5f34492`.
Original design/test baseline `88bbc0734e50e33fa769940291a79ff997102994`.
Plan: [335-plan.md](335-plan.md). Human-approved; tasks 1–5 implemented; refreshed candidate passed final Linux gates and fresh cumulative reviews; publication and live acceptance pending.

## Gates before development

- [x] Human approves the plan ("ok", 2026-10-26).
- [x] Human authorizes the proposed #279/#283 coordination notices (same approval).
- [x] Refresh open PR file overlaps; publish authorized notices before shared edits.
      Only #279/#283 overlap; notices: [#279](https://github.com/larchanka-training/dmc-268-api-t6/pull/279#issuecomment-6426698992),
      [#283](https://github.com/larchanka-training/dmc-268-api-t6/pull/283#issuecomment-6426700273).
- [x] Record one historical pre-development baseline on original main: 269 passed in 278.15s (macOS/Python 3.13.4),
      no skips/failures; initial sandbox denial was before collection.
- [x] Configure task-owned disposable Linux integration resources before final gates;
      PostgreSQL/RabbitMQ readiness verified; no full gates run during preparation.

## Task 1 — Validate deployment admission (S)

Dependencies: approval. Likely files: `app/bootstrap/gitlab_deployment.py` (new),
`tests/test_gitlab_deployment_settings.py` (new).

- [x] RED: default-off CLI failed for missing module; admission without workers
      failed (exit 0 instead of 2). Added literal complete/off, complete/on and
      invalid outcomes at the same public boundary.
- [x] GREEN: reuse runtime/API parsers; admission true requires valid worker config.
- [x] Static redacted errors; no network/DB/provider/file discovery side effects.
- [x] Verify focused tests: 65 passed (36 deployment + 29 runtime); touched-file
      Ruff lint, format check and strict mypy pass. Verification evidence retained privately.
- [x] Independent Standards/Spec review reports zero findings (both task 1 reviews).

## Task 2 — Route configuration to its recipients (M)

Dependencies: task 1. Likely files: `deploy/scripts/env-file.sh`,
`deploy/compose/staging.yml`, `tests/test_deploy_staging_services.py`.

- [x] RED: actual bundle routing failed for unallowlisted GitLab keys; real Compose
      recipient rendering failed for the absent operator service. Tests cover absent
      optional values, 0600 files and API isolation.
- [x] GREEN: add dedicated `gitlab.env` for both workers and operator profile only
      (`gitlab-admin` service / `gitlab-operator` profile).
- [x] Profile-only operator gets DB + GitLab config, no ports/GitHub/LLM secrets;
      no PAT env/global token fallback and no installation changes on deploy.
- [x] Verify GitHub-only defaults and real Compose recipient/profile smoke:
      16 focused tests passed; Ruff lint/format, mypy and shell syntax pass.
      Evidence retained privately.
- [x] Independent task 2 Standards/Spec review reports zero findings (both reviews).

Checkpoint: settings and recipients contract demonstrated; no real secrets used.

## Task 3 — Validate CI configuration before host changes (M)

Dependencies: tasks 1–2. Likely files: `.github/workflows/ci-cd.yml`,
`deploy/scripts/env-file.sh`, `tests/test_deploy_staging_services.py`.

- [x] RED: actual workflow allowed enabled intake without worker config and
      recorded remote actions before admission; assert early
      failure happens before first upload/SSH host mutation, without secret output.
- [x] GREEN: bundle GitLab secret/variables and run target-image, networkless
      validator using protected temporary env input; mask and clean up correctly.
- [x] Preserve existing deployment concurrency, source mappings and empty config.
- [x] Verify focused CI/bundle/preflight checks: 28 passed, no skips; Ruff lint,
      format, mypy, shell syntax and diff checks pass. Verification evidence retained privately.
- [x] Independent task 3 Standards/Spec review reports zero findings (both reviews).

## Task 4 — Preserve deploy and rollback state (M)

Dependencies: task 3. Likely files: `deploy/scripts/env-file.sh`,
`deploy/scripts/deploy.sh`, `deploy/scripts/rollback.sh`,
`tests/test_deploy_staging_services.py`; `app/bootstrap/gitlab_deployment.py` only
if the existing target-image parser fallback needs shared support.

- [x] RED: enabled/no-workers deploy and invalid-retained-config rollback
      initially succeeded; tests now require unchanged live bytes/modes/state/switch.
      Target-image compatibility and missing-helper legacy behavior are explicit.
- [x] GREEN: stage/validate before replacement, preserve manual absent bundle vs CI
      empty-bundle semantics, use compatible target parser fallback without bypass.
- [x] Successful redeploy, failed deploy, manual/auto rollback retain current
      keyring/config and DB installation; unsupported target refuses before mutation.
- [x] Verify deployment/rollback regression: 343 passed, no skips; 17 focused host
      cases passed. Real Compose policy fixture confirmed `--pull missing` overrides
      `pull_policy: always`; task 2 real recipient/profile config smoke is green.
      Touched lint/format/mypy, shell syntax and diff checks pass. Evidence retained privately.
- [x] Independent task 4 Standards/Spec review reports zero findings (both reviews).

Checkpoint: focused regressions green; no repeated full pytest run at this point.

## Task 5 — Document the protected operator path (M)

Dependencies: task 4. Files: `.env.example`, `docs/SECRETS.md`, `docs/CICD.md`.

- [x] Explain exact GitHub secret/variable mapping, recipients, default-off and
      GitLab.com private-project vs private-network distinction.
- [x] Provide image/profile CLI create → bind → inspect → activate and revoke
      commands with existing workspace IDs, expected generation and protected stdin;
      no SQL, PAT argv, runtime secret values or automatic installation recreation.
- [x] Document redeploy/removal, retained keys, compatible rollback and recovery;
      link verified UI user-docs follow-up as required by CONTRIBUTING §8.
- [x] Verify six CLI subcommand help contracts, seven Bash example blocks and keyring
      Python example syntax without executing secret generation or onboarding.
- [x] Actual operator smoke found cached-image pull failure with `always`; restrict
      `missing` policy to operator only. Focused rendered recipient tests: 2 passed.
      Actual unchanged-source operator smoke now exits 0 without registry access;
      ordinary service `always` policies remain unchanged. Evidence retained privately.
- [x] Independent task 5 / final cumulative Standards and Spec review clean.

## Required main refresh

- [x] Preserve the full candidate, including all four new files, in a recoverable
      scoped backup and integrate latest main `f160ce4c82220c20e627fbfdc0230b43a5f34492`.
- [x] Retain upstream Grafana secret, manual monitoring acceptance and bounded
      app-RPS behavior; renumber only the new GitLab docs subsection to 2.4.
- [x] Complete refreshed focused checks (14 passed), prove unchanged implementation
      and upstream monitoring bytes, and freeze the new whole candidate. Touched-file
      Ruff lint/format, deployment/monitoring shell syntax and diff checks pass.
- [x] Fresh cumulative Standards/Spec reviews and final gates on this baseline.
      Both axes reported zero actionable findings; corrected Linux full run passed
      9092 tests with no skips/failures/errors (1573.16s, two dependency deprecation warnings).

## Task 6 — Final code gates and publication

Dependencies: task 5. All changes delegated; coordinator never writes implementation.

- [x] Run final `uv run ruff check .`: all checks passed.
- [x] Run final `uv run ruff format --check .`: 713 files already formatted.
- [x] Run final `uv run mypy .`: no issues in 703 source files.
- [x] Run final `uv run pytest` with disposable PG/Rabbit resources on Linux:
      9092 passed, no skips/failures/errors in 1573.16s, two dependency deprecation
      warnings; complete JUnit evidence checked. The earlier old-baseline attempt
      was stopped for a runner-only nested-uv venv error and is not a final verdict.
- [x] Fresh independent Standards/Spec review reports zero findings.
- [ ] Publisher stages intended files, commits, pushes, opens and attaches PR;
      explicit live acceptance status and UI docs link in its four-section body.
- [ ] Check CI once new candidate is published; fix failures with targeted loops,
      repeat complete gates only for a new candidate or unresolved failure.

## Task 7 — Real GitLab staging acceptance (evidence, separately gated)

Dependencies: reviewed deployment available, authorized operator access, protected
keyring/PAT/webhook secret, test workspace/project, healthy workers/prompts/LLM.

- [ ] Verify prerequisites securely; missing access remains an explicit pending gate.
- [ ] Create/bind/inspect/activate only the authorized disposable installation.
- [ ] Configure a real GitLab MR webhook and run a fixture yielding inline findings.
- [ ] Record one succeeded `provider=gitlab` run, no GitHub API, inline discussion(s)
      and exactly one summary; record real MR/head/delivery/run references privately.
- [ ] Repeat identical original delivery; compare literal run/publication counts,
      proving no duplicate run, inline discussion or summary.
- [ ] Revoke test installation and verify intake stop/cancellation behavior.
- [ ] Publish sanitized evidence in #335/#169 with authorized access to private proof.
- [ ] Leave issue open for the designated acceptor; do not mark live AC or the task
      complete from mocked/local results or a PR alone.
