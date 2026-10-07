# #53 Mistral baseline task list

Plan: [53-plan.md](53-plan.md). Status: user approved the plan, draft PR publication and exactly one paid run
on 2026-10-07. Task 1 implementation, local verification and independent review complete;
draft PR #93 published; the sole approved paid run succeeded. Capture verified
and copied locally; final review and publication pending.

## Task 1: Secure optional raw-response export

Description: Add the default-off dispatch input and a separate narrow raw artifact.

Acceptance criteria:
- [x] `export_raw_responses` is boolean and defaults to false; omitted/false never uploads raw text.
- [x] True exports only generated case-response files and manifest, with 14-day retention.
- [x] Existing manual trigger, secret isolation, safe artifact, summary and failure contracts remain intact.

Verification:
- [x] First demonstrate the new behavioral/security tests fail on current main.
- [x] Verify enabled export excludes unexpected files and preserves empty/invalid response bytes.
- [x] Run targeted workflow/live/replay/scorer tests; retain unsafe-mutation coverage.

Dependencies: human approval; branch from refreshed main, not current detached HEAD.
Files: `.github/workflows/eval-live.yml`, `tests/test_eval_live_workflow.py`,
`test-prs-dataset/README.md`. Estimated scope: medium, 3 files.

## Task 2: Prepare draft PR for capture

Description: Publish the reviewed workflow change so the one live run uses the PR ref.

Acceptance criteria:
- [x] Main is refreshed, branch is rebased, ownership rechecked, export implementation reviewed.
- [x] Four mandatory gates pass; draft PR contains `What` / `Why` / `How to verify` / `Refs #53`.
- [x] PR is attached to chat and the exact clean branch SHA for dispatch is recorded.

Verification:
- [x] Run `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy .`, `uv run pytest`.
- [x] Run dataset validation and offline replay; review any pre-existing Nemotron drift honestly.
- [x] Inspect pushed workflow and PR diff; no unrelated detached-checkout commits included.

Dependencies: task 1; approved draft publication. Files: plan status and task-1 files.
Estimated scope: small publication operation.

## Checkpoint: Before spending

- [x] Security regression coverage and local gates green.
- [x] Approval covers one paid run; branch inputs are final for this capture.

## Task 3: Capture the Mistral baseline once

Description: Dispatch the approved workflow from the PR branch using team Actions credentials.

Acceptance criteria:
- [x] Non-secret variables select Mistral primary/fallback; dispatch has empty `model` and true export flag.
- [x] Exactly one paid workflow run is started; record run URL, attempt and branch SHA.
- [x] Download raw and safe artifacts; if absent/nonpublishable, report in PR and preserve existing baseline.

Verification:
- [x] Inspect run completion, `run_metadata.baseline_publishable`, model and sanitized provider provenance.
- [x] Require exactly 24 active case responses plus manifest; verify case mapping and safe metadata hashes.

Dependencies: task 2; Actions access, organization secret and credit availability.
Files: temporary downloaded artifacts only. Estimated scope: small external operation.

## Task 4: Commit the byte-identical capture

Description: Replace the full recorded response set and document the new measurement.

Acceptance criteria:
- [x] All 24 response files and manifest equal downloaded bytes; response SHA/length match safe metadata.
- [x] Offline replay is drift-free and matches live metrics; no invalid answer is repaired or excluded.
- [x] README has new metrics/provenance/run link, old Nemotron metrics, and correct optional-export guidance.

Verification:
- [x] Validate corpus, run offline replay to a temporary JSON report and compare metrics/provenance.
- [ ] Hash all 25 files before copy and after commit hooks; preserve manifest unchanged.
- [x] Derive first-answer invalidity groups from validator output for PR, reconciling case counts.

Dependencies: publishable task-3 run. Files: `test-prs-dataset/responses/*.json`,
`test-prs-dataset/README.md`. Estimated scope: small authored documentation plus
one indivisible, mechanical 25-file capture; never split or hand-edit raw data.

## Checkpoint: Capture accepted for publication

- [x] Publishability, completeness, byte identity and no replay drift established.
- [x] No further paid run performed; failure/drift follows plan stop conditions.

## Task 5: Complete review and evidence

Description: Publish the verified baseline and prepare human review of the final PR.

Acceptance criteria:
- [ ] Four gates, dataset validation and required CI replay pass on final changes.
- [ ] Standards/spec reviews pass; PR contains run link, measured metrics and invalidity breakdown.
- [ ] Final branch is current with main and captured digests still match; issue remains for tech-lead acceptance.

Verification:
- [ ] Recheck static/corpus digests after any rebase; never modify provenance to hide drift.
- [ ] Verify current-head review/threads requirements before any future merge; no merge in this task.
- [ ] Update this task list with actual evidence and remaining external blockers.

Dependencies: task 4. Files: plan/task status and PR body. Estimated scope: small.

## Local implementation evidence (2026-10-07)

- Branch: `feat/53-mistral-baseline`; refreshed base:
  `cb365cb872f4ff6987b7abc631413017973c50d1`. No detached #73 history included.
- Open PR ownership rechecked before edits: #92, #91, #89, #88, #87, #84,
  #83 and #69 have no overlap with workflow, test, dataset README or plan files.
- Red/green: new opt-in contract failed on main (1 failed, 15 passed), then
  passed (16 passed). Actual export security tests then failed for traversal,
  extra/missing mappings and response/manifest symlinks (5 failed, 17 passed);
  allowlist validation made all 22 pass. Expanded mutation coverage checks
  default/type, both input guards, staging-success guard and artifact scope.
- Targeted workflow/live/replay/scorer suite: 189 passed.
- Ruff lint and format pass; mypy passes for 332 files. Full pytest:
  2087 passed, 95 skipped, 2 dependency deprecation warnings (73.91 seconds).
- Dataset validation: 24/24. Offline replay exits 0 with no drift warning;
  existing Nemotron validity 16/24, TP=3, FP=5, FN=16, precision 37.5%,
  recall 15.8%, critical recall 40%, verdict agreement 7/24. Temporary report:
  `/tmp/issue-53-replay.json`. No baseline bytes changed.
- Fresh independent standards and spec reviews: zero findings for task 1.
- Publication preflight refreshed `origin/main` again: still
  `cb365cb872f4ff6987b7abc631413017973c50d1`, identical to the branch base;
  no rebase or fingerprinted-input changes are needed. Open PR ownership
  rechecked immediately before staging: the same eight PRs have no overlap.
  Only the workflow, its contract tests, dataset README and these two plan
  files are included; no dependency or recorded-response changes.
- Workflow publication commit: `36c976b05f383d4f4af79d3cbce1c9e61dab093e`;
  [draft PR #93](https://github.com/larchanka-training/dmc-268-api-t6/pull/93)
  is attached to the chat. No unrelated history or fingerprinted inputs changed.

## Single paid capture evidence (2026-10-07)

- Exactly one run was dispatched by the coordinator after user approval:
  [37607360909, attempt 1](https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/37607360909),
  source SHA `36c976b05f383d4f4af79d3cbce1c9e61dab093e`, empty `model`,
  `export_raw_responses=true`. Started 10:26:01 UTC; completed 10:28:11 UTC.
- Primary `mistral-small-4`, fallback `mistral-small-3.2-24b`; all 24 first
  provider labels are `mistral`, all gateway statuses are `accepted`.
  `baseline_publishable=true`, no nonpublishable cases.
- Downloaded raw and safe artifacts into `/tmp/issue-53-capture`. Exactly 24
  response files plus manifest; each response SHA-256/byte count agrees with
  safe metadata, and raw/safe manifests are byte-identical. The manifest format
  remains unchanged; response hashes are in safe per-case metadata.
- Copied all 25 files unchanged. SHA-256 inventory:
  `/tmp/issue-53-capture-sha256.json`. Recheck it after commit hooks.
- Offline report `/tmp/issue-53-mistral-replay.json` has no drift warnings;
  removing only validator diagnostics makes it exactly equal to the live safe
  report (including every metric, status and provenance field).
- Measured validity 5/24 (20.8%), TP=1 FP=1 FN=18, precision 50%, recall 5.3%,
  critical recall 1/5 (20%), verdict agreement 2/24 (8.3%), one severity
  mismatch (LOG-02: high versus critical).
- Actual validator diagnostics account for all 19 invalid first answers in
  disjoint groups: attribution prefix only 11; attribution + line range 4;
  attribution + line range + ordering 2; line range only 2. No empty/missing
  first answers or JSON parse failures. PR-ready details:
  `/tmp/issue-53-invalidity.md`. No repairs or exclusions of raw answers.
- README records new metrics and provenance and preserves Nemotron metrics with
  a historical manifest link pinned to `cb365cb872f4ff6987b7abc631413017973c50d1`.
- Final local gates pass: Ruff lint and format, mypy (332 files), pytest
  (2087 passed, 95 skipped, 2 dependency deprecation warnings; 66.92 seconds).
  Dataset validation passes 24/24; local replay matches live without drift.
- Initial workflow-head PR CI passed, including Python lint/type/test
  (run `37607282765`); final capture-head CI awaits publication.
- The 25-file data replacement is the approved indivisible mechanical batch;
  it changes no code and is verified by byte identity and replay, rather than
  new implementation tests. Task 1 retained its test-first proof.
- Final independent standards and spec reviews: zero findings; both verified
  capture hashes independently. All 25 pre-commit SHA-256 checks pass.
- Publication refresh found main advanced to `6f25ffa` through #84, changing
  only `.env.example`, root README, secrets docs and webhook-worker docs.
  Rebase and post-rebase replay will verify no captured-input drift.
- Commit/push, post-hook hash verification and final-head CI remain pending.
  No second paid run, merge or issue closure has been performed.
