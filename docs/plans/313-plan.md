# Implementation plan: finish summary-only with no model call (#313)

## Status and baseline

Issue: https://github.com/larchanka-training/dmc-268-api-t6/issues/313  
Branch: `feat/313-summary-only-no-llm` (already checked out, equal to `origin/main` at `c89e838c`).  
Task checklist: [313-todo.md](313-todo.md).

This document is the plan. It does not implement the product change.

**Baseline before the first edit** (from the issue kickoff; not re-run while planning):

```sh
uv run pytest tests/test_get_run_diff.py tests/test_review_context.py tests/test_execute_review.py
```

`uv run pytest tests -k summary_only` is already green on `main` and does not prove that the model was not called. The proof is the new module `tests/test_summary_only_no_llm.py`.

## Overview

A pull request that is summary-only still walks the normal attempt and can call the model for conventions (cache miss), review, and the verifier. The specs already say that run has zero model calls. This change makes the worker stop before those calls, publish a backend summary, and finish `succeeded` with `summaryOnly: true`, coverage `partial`, and one of the three existing reasons.

The line threshold stays 3 000 changed lines. Exactly 3 000, with a complete file list, remains an ordinary review. 3 001 or more is summary-only with reason `diff_too_large`. `file_list_complete = false` is summary-only with reason `diff_inventory_incomplete` and is never `skipped`. A pull request whose every file is a proven exclusion (lock, binary, generated, ignore mask, proven empty file), with a complete inventory and no technical skip, stays `skipped` / `all_changes_ignored` (T19) with zero model calls. Anything the context builder still labels `no_reviewable_files` after those earlier outcomes is summary-only with that reason, also with zero model calls.

Out of scope: new thresholds, reason codes, or rate limits; which lines count toward the 3 000 (too-large files stay in the legacy counter; that retune is #175); the zero-model route for no-text partial (`summaryOnly: false`); legacy technical-only runs, which keep the model call #130 left them; UI run-bar step order (ui#143); editing `docs-site` in the UI repo.

## Current behaviour

Production order after #168 part 6, in `ReviewRunProcessor.prepare` then `ExecuteReviewRun`:

1. Policy load (`ReviewContextStage.begin`).
2. Fetch and `StoreDiffSnapshot`. `summary_only` is set on the stored rows when allowed added+deleted lines are greater than `MAX_DIFF_LINES` (3 000). Binary and generated rows do not count. Excluded rows do not count. The comparison is `>`, so 3 000 does not set the flag. `review_patch` is cleared on those summary-only rows; the UI `patch` stays.
3. Blob prefetch (GitHub), still before the stage.
4. `ReviewContextStage.run`: merge-base and diff map, then coverage. Proven exclusions (`coverage.all_excluded`) raise `RunSkipped("all_changes_ignored")` before any model call. On the snapshot path, technical-only inventory raises `NoReviewableText` before RAG and the context builder (#130 F5). `ExecuteReviewRun` publishes that with `NoModelAnswer` and no `llm.call`.
5. Otherwise the stage opens RAG and runs `BuildReviewContext`. That builder returns early, before the tree read, for `diff_inventory_incomplete`, `no_changes`, `all_changes_ignored`, `no_reviewable_files`, and `diff_too_large` (`MAX_REVIEWABLE_LINES`, also 3 000). Returning does not stop the attempt.
6. `GenerateRepoConventions.execute` then runs. A cache miss writes `llm.call`.
7. `ExecuteReviewRun` still calls the review model and, when candidates remain, the verifier. Summary-only findings are later dropped as `insufficient_context`. The check-run title is already «AI-ревью: только сводка», and the summary line is always «PR слишком большой: только сводка».

The public `summaryOnly` flag is `bool_or` of `code_change_diffs.summary_only`. Only the line threshold sets that column. An incomplete file list under 3 000 lines is summary-only inside `context.build` and still shows `summaryOnly: false`.

`BuildReviewContext` returns `no_reviewable_files` when nothing is allowed and the change is not a pure ignore. The live stage usually never gets there for lock, binary, and generated files: those omissions are exclusions, `all_excluded` is true, and T19 fires first (`tests/test_coverage_none_route.py`, binary-only). The issue's line citation is the builder, which unit tests call directly. Do not freeze that builder reason as the published outcome for proven exclusions.

The issue's note that conventions run before context build is stale. After #168, `context.build` runs before the conventions model. The hazard is unchanged: cutting only `draft_review` leaves the conventions `llm.call` on a cache miss.

## Architecture decisions

### D-1 — Where the attempt stops

Stop in `ReviewContextStage.run` after the diff map and coverage exist, and before `_open_rag`, the context builder's tree and blob reads, `GenerateRepoConventions.execute`, the review model, and the verifier. `ExecuteReviewRun.execute` catches the new exception around `prepare`, in the same place it already catches `NoReviewableText`, and publishes with the existing `NoModelAnswer` marker: nothing is parsed as a model answer, and `llm.review_output` / `llm.verify` are not written.

Trace for this branch: `policy.load` → `vcs.fetch_diff` → `context.build` (`status: summary_only`, `reason` one of the three, `blocks: []`, coverage `partial`) → `review.postprocess` → `github.publish_review` or `gitlab.publish_review`. No `context.rag`, no `llm.call`, no `llm.repo_conventions`.

Record `context.build` with the same short traced write the none path uses. GitHub (diff, pull request, merge-base) has already returned. Do not open a transaction that contains a later model or GitHub call. Publishing stays on `StoreReviewOutput`: its write transaction, then the existing publish queue.

Blob prefetch in `prepare` stays where it is. Moving it is not this issue.

### D-2 — Which outcome wins

Decide in this order. The first match wins.

| Order | Condition | Outcome | Model calls |
| --- | --- | --- | --- |
| 1 | `file_list_complete` is false | `succeeded`, `summaryOnly: true`, coverage `partial`, reason `diff_inventory_incomplete` | 0 |
| 2 | Stored rows are `summary_only`, or the shared counter is greater than 3 000 | same, reason `diff_too_large` | 0 |
| 3 | Confirmed empty inventory | existing `skipped` / `no_changes` | 0 |
| 4 | Proven exclusions only (E), complete inventory, no technical skip | existing `skipped` / `all_changes_ignored` (T19) | 0 |
| 5 | Snapshot path, technical-only (T beside E) | existing coverage `none`, `summaryOnly: false` (`NoReviewableText`) | 0 |
| 6 | Legacy technical-only, including a builder result of `no_reviewable_files` for that inventory | unchanged: the builder still runs and the model is still called | as today |
| 7 | Builder reason `no_reviewable_files`, and the row above did not claim it | `succeeded`, `summaryOnly: true`, coverage `partial`, reason `no_reviewable_files` | 0 |
| 8 | Anything else, including exactly 3 000 allowed lines and a complete file list | ordinary review | as today |

`skipped` remains only `no_changes` and `all_changes_ignored`. Incomplete inventory is checked before the empty-list skip. The existing `not stored and files_changed == 0` guard is a confirmed empty list (`len(files) == files_changed == 0`), so it stays. An incomplete list must not use it.

Order 2 uses the snapshot flag `StoreDiffSnapshot` already wrote, so a builder that would still return `review` or `no_reviewable_files` for a short anchorable subset cannot keep the model call. Incomplete inventory (order 1) beats the line flag when both are true.

Order 6 is deliberate. `BuildReviewContext` labels a legacy too-large-only change `no_reviewable_files` today, and `test_the_same_change_on_a_legacy_run_is_built_for_the_model` expects that change to be built for the model. CVS calls that inventory coverage `none`, and #130 left the model call on legacy. Do not publish it as summary-only.

Order 4 is the lock, binary, and generated pull request when the fetcher stored `omission_reason` `generated` or `binary` (locks are `is_generated_path`). The new test asserts `skipped` / `all_changes_ignored` and zero `llm.call`. It must not assert `no_reviewable_files`.

Order 7 is the residual the issue keeps: nothing reviewable, not proven E, not the legacy technical-only route. No new reason code.

No-text partial (`summaryOnly: false`, zero model calls) stays a #107 goal. This issue does not activate it.

### D-3 — One threshold

Keep the number 3 000 and the comparison `>`. Define it once as `MAX_DIFF_LINES` in `get_run_diff.py`. `review_context.py` imports that name and deletes the second literal. Do not change `_changed_line_count`: binary and generated stay uncounted, too-large text stays counted. Both call sites must use that one value so a later edit cannot move only one of them.

The stage's order-2 decision follows the snapshot flag that this same constant produced. The builder's early return stays as a second use of the same constant for attempts that reach it.

### D-4 — `summaryOnly` for every summary-only reason

The API and the check-run read `code_change_diffs.summary_only`. When the stage chooses any of the three reasons, persist `summary_only=true` and `review_patch=null` on that run's diff rows in the same short transaction as the `context.build` record, with no network call inside it. The UI `patch` column is left as stored. Order 2 rows are already flagged; the write makes incomplete inventory and residual `no_reviewable_files` match. A retry classifies the stored rows again and does not call the model.

Coverage stays the existing schema. Do not add `diff_too_large` or `no_reviewable_files` to `REASON_TEXT`. `diff_inventory_incomplete` is already a coverage reason. The context.build `reason` string is what the summary and the check-run name.

### D-5 — Words in the summary and the check-run

Title stays «AI-ревью: только сводка». Conclusion stays `neutral`. Verdict stays null. There are no inline comments and no supported findings.

Replace the single summary line «PR слишком большой: только сводка» with the reason:

| Reason | Summary line |
| --- | --- |
| `diff_too_large` | Построчное AI-ревью не выполнено: превышен лимит diff |
| `diff_inventory_incomplete` | Построчное AI-ревью не выполнено: список изменённых файлов неполон |
| `no_reviewable_files` | Построчное AI-ревью не выполнено: нет проверяемых файлов |

The first two sentences are CVS §2.3 plus the existing `REASON_TEXT` for an incomplete list. The third is the recommended line for the residual reason; it must not say the diff exceeded 3 000 lines and must not say the run was skipped. The saved review summary names the same reason. The run link in the check-run summary stays (PIPELINE_SPEC §7).

### D-6 — How zero `llm.call` is proved

`tests/test_summary_only_no_llm.py` drives `ExecuteReviewRun` (the worker entry after claim) with a context stage, a conventions generator, and a review model that would record `llm.call` if invoked, including a conventions cache miss. Assert the trace has no `llm.call`, no `llm.repo_conventions`, no `llm.review_output`, and no `llm.verify`.

Required cases:

- 3 001 allowed lines, complete file list: zero calls, `succeeded`, `summaryOnly: true`, coverage `partial`, reason `diff_too_large`, no inline comments.
- `file_list_complete = false`: zero calls, summary-only `diff_inventory_incomplete`, not `skipped`.
- Only lock, binary, and generated files, stored the way the fetcher stores them: zero calls, `skipped` / `all_changes_ignored`, not `no_reviewable_files`.

Also lock the boundaries the issue states, in the same module:

- Exactly 3 000 allowed lines and a complete file list is not this short-circuit (the review model is still invoked).
- A legacy technical-only change is not published as summary-only `no_reviewable_files`.
- One residual `no_reviewable_files` case (complete inventory, not proven E, not legacy technical-only) has zero calls and that reason.

### D-7 — Docs and the UI follow-up

Update PIPELINE_SPEC §2 so the summary-only bullet describes this branch and no longer says that zero model calls are an unmet #107 goal or that the attempt always calls the model. Update the §2 paragraphs that say the current summary-only run calls the model and drops findings afterwards (about lines 228–251 and the summary-only paragraph that follows the #168 canon). Update PIPELINE_SPEC §7 so the summary-only check-run cell names all three reasons, not only the size limit and an incomplete list.

SYSTEM_DESIGN §6.2 already draws this branch («backend summary-only/partial, summaryOnly=true, 0 LLM») and does not say the code fails to do it. Leave that diagram. #210 marked the target flow there; it did not leave an "unmet goal" sentence in §6.2. The sentence that still says the current summary-only run may call the model is in §11 (about lines 1884–1890). Update that sentence so summary-only zero-model is no longer described as a future lifecycle. Leave no-text partial, and the rest of #107, as not done.

In CVS §2.3, delete only the status sentence that says the summary-only zero-model lifecycle is not activated yet and that conventions or review calls are still possible. Leave the outcome table and the no-text partial goal as they are.

User-visible behaviour changes (the check-run no longer always says the PR is too large; a summary-only run spends no model call and has no line comments). `docs/CONTRIBUTING.md` §8 therefore needs a link, in the API PR body, to a PR or issue in `dmc-268-ui-t6`. Do not edit `docs-site` in this task. ui#173 is closed and only introduced the docs site; linking it does not track these pages. The member comment on api#313 (https://github.com/larchanka-training/dmc-268-api-t6/issues/313#issuecomment-6399973417) names the pages: `docs-site/content/docs/how-it-works.mdx` (accordion «What is not reviewed») and `faq.mdx` («The review has no line comments»). `how-it-works.mdx` is a core page, so the UI review has to include «`how-it-works.mdx` прочитана целиком». The implementation PR body links that comment and a UI follow-up issue filed for those two edits. `Refs #313`. No closing keyword.

## Dependency graph

```
Task 1  RED public tests
   │
   ├── Task 2  one threshold constant
   │
   └── Task 3  short-circuit and publish with zero llm.call
            │
            ├── Task 4  persist summaryOnly; summary and check-run name the reason
            │
            └── Task 5  T19 and legacy technical-only stay put
                     │
                     └── Task 6  specs, then the PR body link for user docs
```

Tasks 2 and 3 can be written after Task 1. Task 4 and Task 5 both depend on Task 3 and touch the stage, so they stay sequential. Task 6 waits until the sentences in D-5 are the ones the tests assert.

## Task list

### Phase 1: Prove the gap

- [ ] Task 1: RED — public tests for zero model calls
- [ ] Task 2: One shared 3 000-line constant

### Checkpoint: Tests describe the contract

- [ ] `tests/test_summary_only_no_llm.py` fails on the 3 001-line and incomplete-list cases because the model is still called
- [ ] The proven-exclusion case fails only if the run is not already `skipped` with zero calls; it must not be rewritten to expect `no_reviewable_files`
- [ ] Existing `tests/test_get_run_diff.py` and `tests/test_review_context.py` threshold tests stay green
- [ ] `uv run ruff check .`, `uv run ruff format --check .`, and `uv run mypy .` are clean

### Phase 2: Stop the model call

- [ ] Task 3: Short-circuit summary-only before conventions, RAG, review, and verifier

### Checkpoint: Zero model calls

- [ ] `uv run pytest tests/test_summary_only_no_llm.py` — the call-count cases for 3 001 lines and an incomplete list are green
- [ ] Exactly 3 000 lines still reaches the review model
- [ ] `uv run pytest tests/test_coverage_none_route.py tests/test_execute_review.py tests/test_review_context_stage.py`
- [ ] Lint, format, and mypy clean

### Phase 3: Outcome the reader sees

- [ ] Task 4: Persist `summaryOnly` and name the reason
- [ ] Task 5: Keep proven exclusions on T19 and legacy technical-only on its current path

### Checkpoint: Published outcome

- [ ] The new module is green, including `summaryOnly`, coverage `partial`, the reason text, and the skipped exclusion case
- [ ] `uv run pytest tests/test_clean_without_text_views_postgres.py tests/test_review_run_worker.py -q` for the check-run title, or the narrower test Task 4 adds

### Phase 4: Specs

- [ ] Task 6: Specs match the branch; PR body carries the UI follow-up link

### Checkpoint: Complete

- [ ] `uv run ruff check .`
- [ ] `uv run ruff format --check .`
- [ ] `uv run mypy .`
- [ ] `uv run pytest`
- [ ] PIPELINE_SPEC §2 no longer calls summary-only-without-a-model an unmet #107 goal
- [ ] PR body links the api#313 comment and a UI issue for `how-it-works.mdx` and `faq.mdx`

## Risks and mitigations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Cutting only `draft_review` leaves a conventions `llm.call` on cache miss | High | D-1 stops inside `prepare`, before `GenerateRepoConventions.execute`. The new test uses a cache miss. |
| Treating every builder `no_reviewable_files` as summary-only | High | D-2 order 6 leaves legacy technical-only on the current model path. Task 5 locks the existing legacy test. |
| Publishing proven exclusions as `no_reviewable_files` | High | T19 stays before the residual branch. The new test expects `skipped`. |
| Incomplete inventory finished as `skipped` / `no_changes` | High | Order 1 runs before the empty-list outcome. The test uses a list the provider does not confirm. |
| `summaryOnly` stays false for an incomplete list because only the line counter sets the column | High | D-4 writes the flag for all three reasons. |
| The two 3 000 constants drift | Med | D-3 keeps one literal. The stage trusts the snapshot flag that literal produced. |
| Check-run still says the PR is too large for an incomplete list | Med | D-5 branches the sentence on the context reason. |
| A DB transaction wraps a model or GitHub call | High | Classification and the `context.build` write happen after the fetches already made. Publish stays on the existing queue. No new network inside the write. |
| Giant-PR blob prefetch still runs before the stage | Low | Accepted. The issue's budget leak is the model call. Moving prefetch is out of scope. |
| Reusing `NoModelAnswer` makes a summary-only run look like coverage none | Med | The stored coverage and `summary_only` flag are what the post-processor and check-run read. Tests assert `partial` and `summaryOnly: true`, not `none`. |

## Spec conflicts and how this plan resolves them

1. **Conventions before context build.** The issue says conventions run before context assembly. The code and PIPELINE_SPEC after #168 do the opposite: `context.build`, then the conventions model. The plan follows the code. The model call to cut is still the one in `prepare` after the stage.
2. **`no_reviewable_files` is not a CVS outcome.** CVS §2.2 sends "nothing to analyze" to `skipped`, coverage `none`, or no-text partial. The issue keeps the builder reason `no_reviewable_files` for the residual and says proven exclusions become `skipped`. This plan does that, and does not activate no-text partial.
3. **Legacy technical-only uses the same builder reason.** A legacy too-large-only change is `no_reviewable_files` in the builder and is still sent to the model (PIPELINE_SPEC, #130 / #283). The issue's third acceptance bullet is about lock, binary, and generated files, which are exclusions. Those stay on T19. Legacy technical-only is not switched to summary-only.
4. **Zero model calls are still described as future.** PIPELINE_SPEC §2, CVS §2.3, and SYSTEM_DESIGN §11 say the code still calls the model. SD §6.2 already draws zero calls and is not the stale sentence. Task 6 updates the stale sentences for summary-only only.
5. **What the 3 000 counts.** CVS excludes technical skips from the counter. The code still counts too-large text (`get_run_diff._changed_line_count`). The issue forbids a new threshold and says the current 3 000 stays. The counted set is not retuned (#175).
6. **Check-run copy.** PIPELINE_SPEC §7 names the size limit and an incomplete list, and the code always says «PR слишком большой». Neither names `no_reviewable_files`. D-5 adds that sentence without a new code. Confirm the wording in Open questions before Task 4.
7. **Issue line numbers.** `MAX_REVIEWABLE_LINES` is `review_context.py` line 88, and the two returns are lines 571 and 578, not 81, 475, and 483. Behaviour matches the issue; the citations are stale.

## Open questions

1. **Check-run and summary sentence for `no_reviewable_files`.** CVS §2.3 and `REASON_TEXT` specify the size limit and the incomplete list only. The plan's default is «Построчное AI-ревью не выполнено: нет проверяемых файлов». Confirm that sentence before Task 4, or replace it. The other two sentences are already specified and are not waiting.

No other question blocks Task 1–3. ui#173 cannot be the §8 follow-up (it is closed). Filing the UI issue is part of Task 6's PR body, not a product decision.

## Parallelization

One implementer should take the tasks in order. Task 2 is a separate commit from Task 3 only so the constant change is obvious; it does not need a second person. Docs (Task 6) wait on the sentences Task 4 locks.
