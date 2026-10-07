# Implementation Plan: #53 Mistral baseline publication

## Overview and evidence

Approved by the user on 2026-10-07 under `agent-loop`: implement this plan,
publish a draft PR, and execute exactly one paid workflow run with empty `model`
and the configured fallback. No repeat paid run is authorized.
The controlling remaining scope is the tech-lead decision of 2026-10-07:
https://github.com/larchanka-training/dmc-268-api-t6/issues/53#issuecomment-6035701495.

Verified remote `main` at `cb365cb872f4ff6987b7abc631413017973c50d1`, fetched on
2026-10-07. PR #65 already delivered raw-first capture, failure accounting,
fingerprints, required offline replay, manual evaluation, gateway follow-ups and
the 24-case Nemotron baseline. The two post-merge Mistral runs are accepted by the
tech lead, but their artifacts lack raw answers. Do not reimplement the original
issue body or repeat those two-model experiments.

Implementation started on `feat/53-mistral-baseline` from refreshed `origin/main`
at `cb365cb872f4ff6987b7abc631413017973c50d1`. The previous detached checkout at
`5df20bb7111dd273f9e034cccc4e7ae80e8dcaa7` was left behind, preserving these plan
files without inheriting unrelated #73 commits.

## Intended result

Add a boolean `export_raw_responses` dispatch input, default `false`. Normal runs
retain the current safe metrics/metadata artifact. An explicit opt-in produces a
separate 14-day artifact containing only the generated response files and manifest.
Use one paid run on the PR branch, with `model` empty and the configured fallback.
If publishable, replace all 24 Nemotron responses and manifest byte-for-byte with
that capture, document the measured Mistral results, and retain Nemotron's historical
metrics in the README. Explain first-answer invalidity in the PR using the actual
offline validator diagnostics.

## Architecture decisions and boundaries

- Keep changes in `.github/workflows/eval-live.yml`, its contract tests, dataset
  README, and captured response data. Reuse the recorder and offline validator.
- Keep secret exposure confined to the existing live-evaluation step, read-only
  repository permissions, manual-only trigger, concurrency, failure propagation,
  safe summaries, and existing report artifact paths.
- The raw upload must require the boolean opt-in. Use an explicit file allowlist
  from the generated manifest/case mapping or equivalent narrowly staged export;
  do not upload an entire runner directory, logs, envelopes, prompts or diagnostics.
  Include empty/invalid raw answers unchanged. Test exclusion of unexpected files.
- Preserve the 24-case corpus, prompts, rules, validators and model configuration.
  No dependencies, runtime API, schema migration, or UI changes are expected.
- The tech-lead comment says response SHA-256 values match the manifest. The
  existing manifest records paths and provenance, while per-response `raw_sha256`
  and byte counts live in the safe metadata artifact. Preserve the original manifest
  format and bytes; verify each response against those safe metadata values, then
  compare hashes of all 25 downloaded files against the files staged for commit.
  Explain this existing format detail in the PR; do not edit the recorder to invent
  hashes in the manifest.
- Workflow success and publishability do not imply a quality threshold. Invalid
  first answers remain in the denominator even when repair/fallback succeeds.

## Tasks and checkpoints

1. **Secure optional export, test-first (small/medium).** Extend contract tests to
   prove default/false yields no raw upload, true uploads only allowed response
   files plus manifest, retention is 14 days, and unsafe mutations fail. Implement
   the smallest workflow change and document the input. Run targeted eval tests.
2. **Prepare the PR branch (small).** Recheck main and open PR ownership; run all
   gates; review the export change; commit/push and open a draft PR with `Refs #53`.
   Attach the PR to this chat. Confirm the branch contains the approved workflow
   and no uncommitted input changes before dispatch.
3. **Capture once (external operation).** Verify non-secret repository variables
   select `mistral-small-4` and the agreed fallback. Dispatch `eval-live.yml` with
   branch ref, empty `model`, and `export_raw_responses=true`. Record run URL,
   attempt and exact head SHA. Wait for that run; do not retry/re-run automatically.
4. **Publish the verified baseline (mechanical 25-file data batch).** Download the
   raw and safe artifacts into temporary storage. Require a complete 24-case set,
   manifest, expected provenance, and `baseline_publishable: true`. Verify hashes,
   copy bytes unchanged, and replay with no drift warning. Document measured
   results and validator failure groups. Preserve the old baseline's metrics and
   historical provenance in README, avoiding links that imply the new manifest is
   the historical one.
5. **Final review and publication (small).** Re-run gates/replay, verify bytes after
   hooks, update PR with measured evidence, run standards/spec reviews and required
   CI, and request human review on the current head. No automatic merge or issue
   closure; the tech lead accepts and closes #53.

Checkpoint after tasks 1–2: security tests and all local gates green; workflow
changes reviewed before paid execution. Checkpoint after tasks 3–4: publishability,
byte identity, complete denominator and drift-free offline replay verified.

The 25-file baseline batch is intentionally atomic: splitting or hand-editing it
would break provenance. All authored-code tasks remain small. The dependency chain
is sequential; independent standards/spec reviews can use the agent-loop agents.

## Verification

Targeted development checks on the refreshed branch:

```sh
uv run pytest tests/test_eval_live_workflow.py tests/test_eval_live.py tests/test_eval_replay.py tests/test_eval_score.py
uv run python review/scripts/validate_dataset.py --final
uv run python review/scripts/eval_replay.py --report-json /tmp/issue-53-replay.json
```

Mandatory gates before claiming implementation complete:

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run pytest
```

Compare local replay metrics with the live report, including validity, TP/FP/FN,
precision, recall, critical recall, per-category metrics, verdict agreement and
severity mismatches. Group invalid cases from the local replay's validator output;
counts must reconcile to the invalid-case total (explain overlapping reason groups
if used). Keep raw validator diagnostics out of default Actions artifacts/summaries.
Required `Python lint / type / test` must execute committed replay and pass.

## Risks and dependencies

| Risk | Handling |
| --- | --- |
| Paid run fails or is nonpublishable | Preserve existing baseline; report run/failure in PR, leave draft, and request direction before another paid run. |
| Public raw artifact exposes unintended files | Explicit opt-in, 14-day retention, narrow allowlist, exclusion tests; export only content intended for the eventual commit. |
| Main changes fingerprinted code | Rebase before capture; after capture compare digests again. Never alter recorded provenance to suppress drift or spend on an unapproved rerun. |
| Captured bytes changed by hooks | Existing raw-response hook exclusions are retained; compare SHA-256 after staging/commit as well as before copy. |
| Low validity | Report it faithfully with validator reasons; no new quality gate or prompt tuning in this scope. |
| Secrets/access/credits unavailable | Actions uses organization secret `AI_DMC268_T6`; never request/copy it locally. Report the concrete external blocker. |

Open PR ownership checked on 2026-10-07: #92, #91, #89, #88, #87, #84, #83,
and #69 do not own the proposed workflow/test/dataset/plan files. #92 and #69 do
touch fingerprinted gateway/validator/prompt inputs, so refresh this check before
editing and before capture. Do not edit another PR's files without its required
coordination comment and appropriate authorization to send that comment.

## Approval and stop conditions

User approval covers this plan, draft PR publication, and exactly one paid workflow
run with empty `model` and configured fallback. The run may incur repair/fallback
calls; do not promise a fixed cost. No fallback-primary experiment is needed.
The repository variables and workflow permissions are external prerequisites, not
new secrets to configure locally. If final rebasing introduces drift, or the single
run cannot provide a publishable capture, keep completed work reviewable and report
the blocker instead of dispatching again. Final PR sections are `What`, `Why`,
`How to verify`, `Refs`, with no closing keyword or Development-panel issue link.

## Execution checkpoint (2026-10-07)

Tasks 1–3 completed through draft PR #93 and the sole authorized paid
[run 37607360909, attempt 1](https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/37607360909)
at `36c976b05f383d4f4af79d3cbce1c9e61dab093e`. The capture is publishable and
complete; all 25 downloaded files were copied unchanged, with response hashes
and byte counts verified against safe metadata. Offline replay has no drift and
matches the live safe report exactly after removing local validator diagnostics.
First-answer validity is 5/24; all 19 invalid cases remain in the denominator.
Detailed measurement and invalidity evidence is recorded in the task list and
dataset README. Final local gates and corpus validation pass; independent
standards/spec reviews report zero findings. Capture publication was rebased
onto main `6f25ffa4fdaf8d88487a8823b5be1af2452b68b6` without input drift;
all 25 files and committed Git blobs still match the download after commit.
Capture-head CI `37608517383` passed all eight checks, including recorded replay
and the integration suite. The evidence-only completion commit receives final
CI verification before handoff. PR #93 stays draft; human current-head approval
and tech-lead acceptance remain external steps. No additional paid run, merge
or issue closure is authorized.
