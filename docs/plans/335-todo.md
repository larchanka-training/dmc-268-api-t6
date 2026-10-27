# Task list: #335 GitLab staging configuration

Published integration baseline `f160ce4c82220c20e627fbfdc0230b43a5f34492`.
Round 1 integration baseline `430d6604a0fc289916b34cc5aa6bdd307cea40b8` (freshly fetched and rebased without conflicts).
Original design/test baseline `88bbc0734e50e33fa769940291a79ff997102994`.
Plan: [335-plan.md](335-plan.md). Human-approved implementation published as [PR #339](https://github.com/larchanka-training/dmc-268-api-t6/pull/339). Round 1 publication on `1f74bd78` and its approval are historical. Round 2 corrections were published on `91556a06f122cfc4a083c9f11636165cb9cd91e9`; body/replies were updated and [CI 40542067728](https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/40542067728) passed (15 successful jobs, four expected deployment/publication skips). The subsequent [round 3 review](https://github.com/larchanka-training/dmc-268-api-t6/pull/339#pullrequestreview-5671678508) requested changes. Historical local full-suite evidence (round 1: 9298; round 2: 9604 tests, zero skips/failures/errors) and prior approval do not verify or approve this new head. The bounded round 3 source amendment below covers all two P2 and seven concrete P3 units. Final publication, CI, replies and approval are recorded on the live PR. Real GitLab acceptance remains pending.

## Gates before development

- [x] Human approves the plan ("ok", 2026-10-26).
- [x] Human authorizes the proposed #279/#283 coordination notices (same approval).
- [x] Refresh open PR file overlaps; publish authorized notices before shared edits.
      Only #279/#283 overlap; notices: [#279](https://github.com/larchanka-training/dmc-268-api-t6/pull/279#issuecomment-6426698992),
      [#283](https://github.com/larchanka-training/dmc-268-api-t6/pull/283#issuecomment-6426700273).
- [x] Record one historical pre-development baseline on original main: 269 passed,
      no skips/failures.
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
      Ruff lint, format check and strict mypy pass.
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
      format, mypy, shell syntax and diff checks pass.
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
      Touched lint/format/mypy, shell syntax and diff checks pass.
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
      ordinary service `always` policies remain unchanged.
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
      warnings; complete JUnit evidence checked.
- [x] Fresh independent Standards/Spec review reports zero findings.
- [x] Publisher staged intended files, committed and pushed head `eb9e8340`, and
      opened/attached [PR #339](https://github.com/larchanka-training/dmc-268-api-t6/pull/339),
      with live acceptance pending and UI docs link in its four-section body.
- [x] Published-head [CI run 40413707402](https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/40413707402)
      passed. Deploy/Promote/Push were correctly skipped for the PR. This result
      does not verify the round 1 corrected/rebased candidate below.

## Task 7 — Real GitLab staging acceptance (evidence, separately gated)

Dependencies: reviewed deployment available, authorized operator access, protected
keyring/PAT/webhook secret, test workspace/project, healthy workers/prompts/LLM.

- [ ] Verify prerequisites securely; missing access remains an explicit pending gate.
- [ ] Create/bind/inspect, then separately authorize/activate only the disposable
      installation and establish process admission.
- [ ] Configure a real GitLab MR webhook and run a fixture yielding inline findings.
- [ ] Record one succeeded `provider=gitlab` run, no GitHub API, inline discussion(s)
      and exactly one summary; record real MR/head/delivery/run references privately.
- [ ] Repeat identical original delivery; compare literal run/publication counts,
      proving no duplicate run, inline discussion or summary.
- [ ] Revoke test installation and verify intake stop/cancellation behavior.
- [ ] Publish sanitized evidence in #335/#169 with authorized access to private proof.
- [ ] Leave issue open for the designated acceptor; do not mark live AC or the task
      complete from mocked/local results or a PR alone.

## Review round 1 — completed corrections and publication

The existing human-approved implementation/review loop authorizes corrections
within the original #335 architecture. Historical checked tasks above describe
the published head; they do not certify the round 2 candidate. No merge, closure or
production activation is authorized by this checklist.

### R0 — Rebase and coordination

Dependencies: none. Files: plan/todo, Git history and later PR metadata.

- [x] Read all three P2 threads and all fifteen P3 items, including nested test
      cleanups; update these existing plan/todo files only (P3.13).
- [x] Refresh overlaps: #340 owns `.env.example`, CI workflow, staging compose,
      env-file helper, CICD and SECRETS; #283 still overlaps CICD.
- [x] Human authorized the #340 notice and it was [published](https://github.com/larchanka-training/dmc-268-api-t6/pull/340#issuecomment-6438984537)
      before corrective edits; existing #283 notice remains applicable (P3.14).
- [x] Fetch exact current main, inspect complete delta and preserve candidate;
      rebase on it before development. Known `f160ce4c..430d6604` is ten commits
      (#247/#130/#168), no #335 path overlap; verify fetched reality (P3.15).
- [x] Verify cumulative diff preserves upstream and all original PR file bytes;
      recorded rebase head `e9a4f00e80cff8b3600ec7bb9397fa9a37eeae7c`.
      Add #340 to PR Refs and merge-order agreement in R7.

### R1 — Explicit helper failure and same-filesystem staging

Dependencies: R0. Files: env-file helper, deploy, rollback, deployment tests.

- [x] RED: helper conditional-call probes and deploy/manual+auto rollback reject
      invalid TMPDIR, retained read failures and injected temp/write/encode errors;
      no Docker validation after error, no root-relative/outside temp secret files,
      no changed live bytes/modes/state/switch/compose activity (P2 `4387180590`).
- [x] GREEN: explicitly check every fallible nested helper operation, retain read
      errors as errors, install cleanup only after safe nonempty temp paths, and
      verify cleanup/permissions under ignored-errexit contexts.
- [x] Stage under `${APP_DIR}/.stage.XXXXXX`; assert destination-owned stage and
      cleanup so admitted file replacement uses same-filesystem rename (P3.3).
- [x] Focused deployment checks, shell syntax, affected lint/type/format pass;
      record RED/GREEN and inspect final status/diff. Actual initial RED: three
      failed conditional-temp cases; disposable original-helper write/read probes
      and original-stage proof also fail. GREEN: 38 failure/staging cases pass;
      45 focused existing GitLab/CI/host regressions pass. Affected Ruff, format,
      mypy, Bash syntax and diff checks pass. Fresh R1 Standards/Spec checkpoint
      reviews report zero actionable findings; whole-candidate gates were later
      completed in R7.

### R2 — Current/legacy refusal coverage

Dependencies: R0; sequence after R1 when sharing the deployment test file.

- [x] Cover enabled-intake/no-runtime deploy and manual+auto rollback over
      `current` and `legacy`; live state/modes unchanged (P2 `4387180603`).
- [x] Prove new regressions fail after deleting only the legacy partial guard in a
      disposable mutation copy; restore mutation and record passing candidate.
- [x] Preserve valid current/legacy and unsupported-image coverage; split tests
      with `kind == "unsupported"` branches into accepted/refused tests (P3.8).
- [x] Focused host regressions and affected checks pass. Focused host
      matrix: 22 passed; isolated legacy-guard mutation: exactly three legacy
      cases fail and three current controls pass. Candidate helper unchanged;
      affected Ruff, format, mypy and diff checks pass. This repairs a coverage
      gap in an already-correct guard, not a claimed baseline implementation bug.
- [x] Fresh independent R2 Standards/Spec reviews report zero actionable findings.

### R3 — Normalized payload, shared names and real validator path

Dependencies: R1–R2. Files: env-file helper, CI workflow, validator module and its
two existing test modules, plus minimal deploy/rollback callers for safe errors.

- [x] RED/GREEN CI/host post-generator byte parity for trailing-newline key ID,
      multiline JSON, default-off and invalid settings; CI builds filtered input
      from generated role files, not the original raw bundle (P3.1). Host literal
      mode preserves actual retained values without a second normalization; six
      current/legacy deploy/manual+auto rollback regressions enforce this edge.
- [x] Check strict generator-format retained api.env/gitlab.env, read errors and
      missing final newline; surface only static allowlisted basename/category on
      deploy/rollback, with no values/paths/lines/raw stderr (P3.2).
- [x] Derive validator accepted-key knowledge from shell routing; assert literal
      expected keys against routing and actual validator payload, plus current/
      legacy outcome consistency. Distinguish application vs GitLab bundle (P3.6).
- [x] Remove unused module main/__main__ and cwd-sensitive subprocess test path;
      keep pure validate_environment tests and actual production heredoc/fallback
      boundary coverage (P3.7).
- [x] Simplify redundant accepted-key union/comment; extract synthetic keyring
      helper per test module; rename acceptance-only private-origin test; move
      sys import to module scope if needed; remove magic Docker argument count
      by parsing its boundary (remaining nested P3.8 items).
- [x] Rename early CI step to describe complete application bundle representation
      and GitLab settings validation (P3.10; docs synchronized in R5).
- [x] Focused deployment/settings suites, canary secrecy and affected checks pass:
      347 passed, no skips/failures (450.23s); affected Ruff, format, mypy, Bash
      syntax and diff checks pass. Repeated legacy guard mutation still produces
      exactly three legacy failures and three passing current controls.
- [x] Fresh independent R3 Standards/Spec reviews report zero actionable findings.

### R4 — Actionable redacted Docker failures

Dependencies: R1. Files: deploy, rollback, deployment tests; env-file helper only
if sharing a small diagnostic helper avoids duplication.

- [x] RED/GREEN recognized authentication, missing-manifest, disk-full and network
      failures yield static actionable categories; unknown raw stderr with secret
      canaries yields safe fallback. Protect/remove capture on every exit (P3.4).
- [x] Preserve stdin credentials, deploy/rollback parity and pre-mutation refusal;
      do not blindly forward raw registry stderr based on its usual contents.
- [x] Update obsolete compose-pulls-again comments for pinned `--pull missing`
      startup, remove duplicate blank line (P3.5).
- [x] Focused diagnostics, cleanup and secrecy checks pass: 111 targeted cases
      including R1/R3 failure boundaries pass, followed by 12 final-fixture smoke
      cases. Captured stderr is a regular 0600 file, credentials remain stdin,
      inherited registry directory and live state survive refusals; all capture
      files and host-owned staging/config temp directories are removed. Affected
      Ruff/format/mypy, script/fake-shell syntax and diff checks pass.
- [x] Independent R4 and final cumulative Standards/Spec reviews pass; both
      final axes report zero actionable findings on the frozen corrected candidate.

### R5 — Protected project-hook and recovery runbook

Dependencies: R3–R4. Files: SECRETS, CICD; preserve existing #283 sections.

- [x] End setup at inspect; make interactive error handling safe; explicitly obtain
      generation before one separately gated activation command (P3.9).
- [x] Add Settings → Webhooks, correct staging URL/create UUID, same protected
      webhook_secret as Secret token / X-Gitlab-Token, only Merge request events,
      SSL verification, stable webhook-id or Idempotency-Key prerequisite, and
      precise activate → process-admission → configure-hook → real MR order
      (round 3 corrects the historical pre-activation hook order)
      (P2 `4387180613`). Keep unrelated installations operational.
- [x] Explain 202/installation receipt as intake evidence only, exact original
      Recent events resend with unchanged identity/payload, publication counts,
      and installation revoke before project-hook removal with stop verification.
- [x] Document retained generator format and static filename errors (P3.2); update
      quote/trailing-backslash failure to early CI before SSH, no automatic
      rollback, and actual renamed step (P3.10).
- [x] Verify Bash examples, CLI flags, generation sequence and repo contract;
      reconcile provider details with linked official docs. Keep real MR AC pending.
      Eighteen syntax, eight command-order/session and eight current-help checks
      pass; revised docs passed fresh independent Standards/Spec review.
- [x] Read-only final R3/R4/R6 cross-sync matches the frozen docs: CI step name,
      static retained messages, role bytes/recipients, operator Python/FD 0/help
      and failure/cleanup ordering agree. R5/R6 frozen source hashes unchanged.

### R6 — Container-compatible help and env comment

Dependencies: R0; avoid concurrent edits to R3's tests or R5's docs.
Files: gitlab_admin, its tests, env example.

- [x] RED/GREEN help says python -m, shows container FD 0, distinguishes local
      extra descriptors and uses image-available python seed command (P3.11).
- [x] Wrap env-example long optional-origin comment without behavior change (P3.12).
- [x] Check all subcommand help against runbook and focused admin tests; affected
      lint/type/format checks pass. Seven current subcommands checked; 57 focused
      admin tests pass, including nine new root-help text regressions with recorded RED/GREEN;
      seven subcommand help probes exit zero.
      Fresh independent R6 Standards/Spec reviews report zero actionable findings.

### R7 — Fresh final candidate and existing PR update

Dependencies: R0–R6. Files: plan/todo and existing PR metadata/evidence.

- [x] Freeze rebased candidate and fingerprint after focused RED/GREEN checks;
      prepare fresh isolated Linux PG/Rabbit runner and verify nested uv invocation.
- [x] Run final `uv run ruff check .` on frozen corrected candidate.
- [x] Run final `uv run ruff format --check .` on that same candidate.
- [x] Run final `uv run mypy .` on that same candidate.
- [x] Run final `uv run pytest` against real isolated PG/Rabbit: 9298 passed,
      zero skips/failures/errors in 1742.39s; two dependency deprecation warnings.
      XML independently checked. Ruff passes, format passes (715 files), mypy
      passes (705 source files). All 15 frozen paths and 368 image inputs match.
      Production-image smoke passes 14 wrapper cases and 8 CLI help commands.
      One completed full run after freeze; no automatic rerun.
- [x] Fresh cumulative Standards/Spec reviews report zero actionable findings;
      account for 3 P2 + 15 P3 explicitly and preserve legacy mutation evidence.

- [x] Update PR #339 body with current What/Why/How to verify/Refs, #340 and
      second-merger rebase/manual reconciliation agreement (P3.14), exact runnable
      focused command and accurate main status for that publication (P3.15).
- [x] Publish authorized fixes on `1f74bd78db8468d5a719b0891b4c36bde6503e76`
      and retain PR #339. [CI 40497818211](https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/40497818211)
      passed: 15 jobs passed and four deploy/publish jobs were correctly skipped.
- [x] Reply to all three round 1 threads with the fix SHA; all three were resolved.
      The [round 2 human review](https://github.com/larchanka-training/dmc-268-api-t6/pull/339#pullrequestreview-5667073079)
      approved `1f74bd78` and confirms the resolved threads. This approval is
      historical once the round 2 diff is published.

Task 7 remains pending until protected live evidence exists. Completed code
publication does not imply a merge, live AC completion or issue closure.

## Review round 2 — bounded code ledger

Scope: all ten P3 findings in the linked round 2 human review, within current
PR #339. The user accepts a new approval after a diff-changing push. This ledger
ends at final code verification; PR publication/CI/review activity is recorded on
the live PR when it occurs, without another source checkbox cycle.

- [x] Refresh main/open-PR overlaps and rebase before shared-file edits, preserving
      upstream prompt/deployment work. Main `916796ac62a46001e681b8f096dd6e99701dc809`,
      rebased head `5062c7caea30c68962c683e236afb5d8a6a2869f`; both feature commits
      unchanged in range-diff and four independent CLI/plan hashes restored exactly.
- [x] Verify existing authorized #340 notice covers the same shared scope before
      edits; also publish the user-authorized [additional round 2 notice](https://github.com/larchanka-training/dmc-268-api-t6/pull/340#issuecomment-6446326442).
- [x] P3-1/P3-2: correct TMPDIR test naming, narrow rename injection to the GitLab
      validation `gitlab.env` destination, and record isolated guard-mutant failure
      plus passing unchanged public deploy/manual+auto rollback tests. Isolated
      guard mutant fails all three public operations; the human review counted
      four including the conditional-helper case (count clarification only).
      Unchanged candidate passes.
- [x] P3-4: test every `GITLAB_*` key in every `ENV_FILES` entry against literal
      allowed destinations `gitlab.env`/`api.env`; isolated misrouting fails once.
- [x] P3-6/P3-8: separate known diagnostic categories from unknown fallback,
      preserve behavioral secrecy/cleanup checks and remove two blank lines only.
      Complete unchanged deploy test file: 348 passed in 456.29s, zero
      skips/failures/errors. Affected Ruff/format/mypy and shell syntax checks pass.
- [x] P3-3: document conservative orphan-stage recovery/rotation, with no global
      deletion, active-generation removal or secure-erasure promise.
- [x] P3-7a–e: independent operator prefix; exact inspect keys; visible safe login
      and generation failure flow; post-admission hook enabled/re-enable check.
      Nine GitLab Bash blocks pass syntax checks; 13 synthetic flow cases cover
      fresh-session commands, invalid prefix/generation and login success/failure
      without live side effects. Actual CLI DTO serializer/inspect keys checked.
- [x] P3-5: help RED failed for both mismatches; identical help selection GREEN
      (12 passed), complete admin file 58 passed. `--no-deps`, reviewed bootstrap
      advice, FD 0/FD 3 and image Python retained. Touched Ruff/format/mypy pass;
      independent Standards/Spec reviews report zero actionable findings.
- [x] P3-9: cite official header versions/equality and Resend contract in both
      runbooks; installed-version/live MR/replay/revoke acceptance stays pending.
- [x] P3-10: record completed round 1 publication/body/CI/replies/resolution/approval,
      remove obsolete publication status and workstation traces, and preserve
      Task 7 pending in this separate round 2 ledger.
- [x] Complete owner-focused checks and fresh cumulative Standards/Spec review
      across all 15 feature paths: both axes report zero actionable findings and
      account for all ten P3 findings and nested P3-7 items.
- [x] Freeze final integrated candidate; run `uv run ruff check .`,
      `uv run ruff format --check .`, `uv run mypy .`, and one final `uv run pytest`
      with isolated real Linux PG/Rabbit services. All passed: format 744 files,
      mypy 734 source files; **9604 passed in 2265.01s (37m45s)**, two dependency
      deprecation warnings. Independent log/XML inspection confirmed 9604 cases,
      zero skips/failures/errors. All 15 frozen paths, dependency inputs, HEAD
      `5062c7ca` and 374 production-image inputs matched before/after verification.
      Actual production-image help passed eight network-disabled commands (root
      plus all seven subcommands). Text assertions about flags/FD/bootstrap apply
      only to root help; seven subcommand help probes exit zero. No new wrapper
      matrix or live AC proof claimed.


## Review round 3 — bounded source ledger

Source scope: two P2 plus seven concrete P3 units; existing current-PR approval and
#340 coordination apply. This ledger stops at implementation/focused proof. Final
publication/CI/replies/review request are recorded on the live PR, without another
bookkeeping commit; historical approval does not approve the new diff.

- [x] R0: rebase onto main `1727a954186b42d508b5bab2ee59391bf0e75c54`, yielding
      `4d282a343a6f893c7a588c6da52419868431c2c8`; three range-diff `=` entries,
      all 15 feature blobs and 42 new-main path blobs preserved; existing #340
      notice still covers this scope and second-merger reconciliation.
- [x] P2 hook: normal create/bind/inspect → separately authorized activate → process
      admission → register hook → acceptance MR; activate has no hook dependency.
      Recovery Test separately warns newest MR/action open, no-MR failure and real
      Run/LLM/publication/head consumption. Test before acceptance MR or push new
      head afterward; measure all its counts, then Resend original delivery.
- [x] P2 label: remove GitLab label prerequisite; document bound enabled/available
      project open/update/reopen, active connection/process admission, open/current
      head, `wait_for_ci=never`, rules/prompt and workers/LLM; isolate test MRs.
      Preserve GitHub label policy. Existing GitLab trigger suite: 91 passed,
      zero skips; cache-only warning from managed-checkout permissions.
- [x] P3-1: entire Docker-call suffix is empty; isolated top-level Docker-config
      TMPDIR-bypass mutant fails three cases at the new assertion.
- [x] P3-2: explicitly input recorded installation UUID, inspect and match identity;
      obtain fresh generation before activate/rotate, rejecting empty/malformed,
      inherited stale, EOF and inspect-error flows without terminating the shell.
- [x] P3-3: revoke inspect uses `connection.revoked` / `connection.intake_enabled`.
- [x] P3-4: service table and operator commands use `run --rm --no-deps -T`.
- [x] P3-5: select literal diagnostic prefix by operation; isolated wrong-prefix
      mutant fails six cases at the new assertion. Combined candidate boundary,
      unknown/known diagnostic selection: 27 passed in 48.93s, zero skips;
      affected Ruff/format/mypy pass. Runtime scripts unchanged.
- [x] P3-6a: one exact-stage read-only metadata/PID/PPID/PGID/comm/open-file/cwd
      examples; required visibility/tools, serialization, full child review and
      failure/empty-output stops. No secret contents or automatic cleanup.
- [x] P3-6b: R2 headings 3–5 completed; actual `91556a06` publication/body/replies,
      CI 40542067728 (15 success/four expected skips), then R3 requested changes.
      Root-help text only versus seven exit-zero subcommand probes; three public
      missing-guard cases versus four including helper clarified as historical counts.
- [x] Owner B proof: 13 extracted Bash blocks pass `bash -n`; 23 synthetic interactive
      operator flows and 12 scoped orphan fixture/error flows pass. Official upstream
      ProjectTestData/WebHookService and local eligibility/head dedup/PIPELINE_SPEC
      checked. No live server, hook Test, provider/LLM call or abandonment proof.

Source freeze is followed by fresh cumulative independent Standards/Spec reviews and
local repo-wide Ruff lint/format/mypy. Full pytest runs on the actual published head
in unchanged canonical unit/four-integration-shard/exact-once CI. Report actual new
results on the PR; historical 9604 is not a fresh count. Task 7's eight live checks
remain unchecked; no merge, deploy, acceptance or issue closure is authorized here.
