# Implementation Plan: PR commit context (#82)

## Overview

Populate production review metadata from GitHub PR commits before PromptBuilder builds
context, then replace the entire 24-case #53 raw-first baseline. This plan is based on
refreshed origin/main `c1d8f59d9ff3f332a3595857dbe612115ad559e8` (2026-10-13).
Issue: https://github.com/larchanka-training/dmc-268-api-t6/issues/82.
User approved this plan with "go" on 2026-10-13, authorizing implementation,
required open-PR scope comments and the single complete paid capture with raw export.
External credential/balance prerequisites still apply; replacement reruns need approval.

## Architecture decisions

- Keep the existing provider-neutral `PullRequestMeta.commit_messages` tuple and
  `GitHubRunSource.get_pull_request_meta` integration point. Fetch commits in this
  infrastructure adapter (or a small adjacent helper); do not expand generic
  `VcsProvider`, change routers, add a dependency, or introduce a migration.
- Retrieve `/repos/{validated-owner}/{validated-repo}/pulls/{n}/commits` using
  `per_page=10`, numeric `page=1` and at most `page=2`. Keep the first 20 messages
  in API order; do not reorder by author/date/SHA. Stop on a short/empty page or
  the 20-entry budget. Never follow `Link` URLs: they cannot change the origin,
  request path, credentials recipient, or page budget. A full last allowed page
  does not establish a total omitted count; never fabricate that count.
- Bound each retained message to 500 Unicode characters, including an ellipsis
  when truncated, matching existing PromptBuilder `_shorten`; total retained
  message text is at most 10,000 characters. A streamed 1 MiB decoded-response
  limit per page prevents arbitrarily large commit metadata from being buffered.
  Reject malformed JSON, non-list pages, more than 10 returned records, or
  missing/non-string `commit.message`. Empty string is a valid message. Errors
  must not be silently converted into a successful empty-context review.
- Preserve existing HTTP timeout and installation authorization. Check current
  run/installation state before token acquisition and immediately before every
  commit-page request. A cancellation between pages prevents subsequent token or
  HTTP work. Run repository reads/checkpoints finish their short transaction
  before GitHub calls. Use existing `classify_vcs_error` semantics for HTTP errors
  and safe explicit `RunFailure` for malformed/oversized provider responses;
  never include token, response text, or commit text in diagnostics.
- Compose fetched commits with the already obtained PR metadata; PromptBuilder
  continues to XML-escape user text and enforce its defensive 20-by-500 cap.
  Keep the golden fixture realistic (multiline, markup, and truncation coverage).
  Existing system-prompt semantics already describe commits; avoid unrelated
  prompt tuning or verifier/v2 work. Resolve any intended format wording change
  before the single paid capture, rather than tuning after paying.

## Honest evaluation input

`eval_live._case_input` currently always supplies `commit_messages=()`. A capture
with that unchanged would not exercise the feature. Add one checked-in sidecar,
`review/eval/pr-commit-messages.json`, containing actual messages retrieved from
the five real cases' exact `source.revision` commits, with case ID, source URL,
revision and commit URL. Read these frozen inputs offline during case assembly.
Document that these are source-revision snapshots, not a reconstructed complete
historical PR commit list. Synthetic cases have no recorded commit history and
retain empty tuples; no message or curated defect/truth is invented or changed.
Fail if a real-case sidecar record is missing, duplicate, mismatched or malformed.
Do not query GitHub during live evaluation or replay. The existing workflow reads
repository-side static inputs, so this file need not alter the temporary corpus
layout. Pin the sidecar and `github_run_source.py` (plus any new shaping helper)
in `eval_provenance.static_input_paths`; `eval_live.py` is already pinned.

## Dependencies and file ownership

PR #69 is MERGED (2026-10-07), so the issue comment's prerequisite is satisfied.
Current input drift already exists on main; preserve historical manifest digests
until the complete replacement, never edit a digest to suppress the warning.

| Open PR | Relevant ownership | Implementation action |
| --- | --- | --- |
| #133, feat(reviews): add markdown rules and reflection pass (#120) | `prompt_builder.py`, `execute_review.py`, `review_output.py`, prompt tests including `test_prompt_seeding.py` | Prefer adapter/fixture tests without editing these files. Before any overlap, coordinator posts a scope comment there. If it merges before capture, rebase, rerun tests and capture only the final inputs. |
| #124, chore(eval): refresh prompt examples, pin full diagnostic (#116) | `eval_replay.py`, `tests/test_eval_live.py`, `review/README.md`, `docs/SYSTEM_DESIGN.md` | `tests/test_eval_live.py` and documentation overlaps require a scope comment before editing. Avoid modifying replay scoring. Recheck ownership immediately before work. |
| #148, feat(webhooks): share installation tokens through Redis (#62) | installation-tree adapter, token-cache/bootstrap files, `docs/SYSTEM_DESIGN.md`, installation-adapter tests | No direct source change needed; preserve token checkpoint behavior. A SYSTEM_DESIGN overlap requires a comment. |

Do not post external comments without the human's authorization under the active
agent-loop. Plan approval should explicitly authorize required scope comments.
Keep unrelated primary-checkout plans #113/#117 untouched. All development occurs
in the isolated #82 checkout.

## Ordered tasks and checkpoints

1. Define bounded retrieval tests, implement commit fetching and failure handling
   in GitHubRunSource; verify cancellation and transaction boundaries.
2. Prove the production context/prompt contract and update golden fixtures without
   changing the existing renderer contract unnecessarily.
3. Freeze honest source-revision evaluation context and make the recorder use it.
4. Pin every new shaping input and test drift detection and offline determinism.
5. Complete offline gates, update scope docs, and push the final approved branch.
6. Run one authorized paid 24-case capture on that exact branch revision, verify
   artifacts, and replace all responses/manifest byte-for-byte.
7. Recompute and document #53 metrics, then repeat the required gates.

After tasks 1-2: focused retrieval/prompt tests pass; no external network or DB
transaction regression. After tasks 3-4: corpus final validation and deterministic
fixture capture/replay pass, with new commit context actually present for real
cases and no synthetic history. Before capture: all shaping edits complete,
open-PR coordination complete, exact branch SHA/model pinned, all gates green.
The existing committed baseline's expected drift is documented until replacement.

## Paid recapture mechanism and authorization

The repository's `.github/workflows/eval-live.yml` uses the team EUrouter account
via organization secret `AI_DMC268_T6`; no local key copy is required. A repository
secret listing cannot prove org-secret availability. Missing secret fails before
provider calls. Available balance and workflow-dispatch permission remain external
prerequisites; report an actual failure instead of inventing a local credential.

After human approval for the paid full run and raw artifact export:

```sh
gh workflow run eval-live.yml --repo larchanka-training/dmc-268-api-t6 --ref <feature-branch> -f model=mistral-small-4 -f export_raw_responses=true
gh run list --repo larchanka-training/dmc-268-api-t6 --workflow eval-live.yml --branch <feature-branch> --limit 5
gh run watch <run-id> --repo larchanka-training/dmc-268-api-t6 --exit-status
gh run download <run-id> --repo larchanka-training/dmc-268-api-t6 --name eval-live-<run-id>-<attempt> --dir <safe-metadata-dir>
gh run download <run-id> --repo larchanka-training/dmc-268-api-t6 --name eval-live-raw-<run-id>-<attempt> --dir <raw-capture-dir>
```

Record the actual run ID/attempt, evaluated head SHA, selected model and effective
fallback model. Use the current baseline primary model `mistral-small-4` to keep
comparison interpretable. Workflow defaults to fast: its nominal per-case policy
budget is USD 0.50, or USD 12 across 24 cases. This is not an exact invoice or a
strict pre-call spending guarantee: retries/repair/fallback can spend balance.
Safe artifacts record configured model pricing and effective policy limits, but
export no billed-token totals or actual spend. Record those available settings
and state actual spend is unavailable from these artifacts; do not infer an
invoice or account balance. Approval remains for one complete 24-case run using
the current fast policy, including retries and
raw artifact export; a replacement rerun after failure needs fresh cost approval.

Verify all 24 active case IDs, no SEC-05, `baseline_publishable=true`, regular files,
response mapping, and per-file SHA-256/byte counts against safe metadata. Preserve
invalid/empty first answers exactly; never select repair/fallback output. Copy the
entire accepted response directory and manifest unchanged, without mixing older
cases or patching manifest digests. Run corpus validation and offline replay twice,
assert equal JSON and no drift, and report validity, TP/FP/FN, precision/recall,
critical recall, verdict agreement, per-class metrics and denominators. Update
`test-prs-dataset/README.md` and `docs/TEST_PLAN.md` with actual run evidence and
#53 comparison; record observed results, not claimed improvement.

## Verification and completion

Use `uv` for all Python tooling. Final checks:

```sh
uv run python review/scripts/validate_dataset.py --final
uv run python review/scripts/eval_replay.py --report-json <private-output>/replay-1.json
uv run python review/scripts/eval_replay.py --report-json <private-output>/replay-2.json
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run pytest
```

Compare the two replay JSON files. Report integration skips honestly if no test
DB is available. No done claim until the complete accepted capture, honest metric
update and all required gates are complete. Rebase before review; conventional
commit/PR references #82 without closing keywords or a Development-panel link.

## Risks and unresolved external prerequisites

| Risk | Mitigation |
| --- | --- |
| Untrusted Link or huge commit payload | Fixed numeric paths, two pages, streaming byte cap, malformed-data tests. |
| Reinstallation/cancellation during page reads | Checkpoints around each token/request; test no further HTTP after cancellation. |
| Open PR merges invalidate freshly paid inputs | Coordinate first; freeze/rebase final source inputs before capture; rerun only with authorization if inputs change. |
| Five real revisions cannot be verified | Block capture until their source messages are verified; never invent replacements. |
| Secret unavailable, balance exhausted, paid metadata invalid | Fail before calls when possible; preserve diagnostic artifacts and require a complete authorized replacement. |
| Old validity issues persist | Report measured first-answer validity and errors; no silent response repair or cherry-picking. |

Approval received: the user replied "go" on 2026-10-13. Implementation, required
open-PR scope comments and one full paid capture with opt-in raw export may proceed.
Credential/balance availability is not yet proven; a failed capture does not
authorize an additional paid replacement run.
