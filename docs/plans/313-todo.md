# Task list: finish summary-only with no model call (#313)

See [313-plan.md](313-plan.md) for decisions D-1–D-7. Baseline `origin/main` `c89e838c`; branch `feat/313-summary-only-no-llm`. Each implementation task is one test-first slice. Do not implement from this checklist until the plan is approved.

**Common verification** (every task that touches code, and the last checkpoint in full):

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run pytest
```

Run the narrow pytest named on the task first. The full four gates run at the last checkpoint. Use `TEST_DATABASE_URL` when a task touches a database-backed path.

Before the first product edit, confirm the kickoff baseline is green:

```sh
uv run pytest tests/test_get_run_diff.py tests/test_review_context.py tests/test_execute_review.py
```

---

## Task 1: RED — public tests for zero model calls

**Description:** Add `tests/test_summary_only_no_llm.py`. Drive `ExecuteReviewRun` with a context stage and fakes whose conventions path is a cache miss and whose review and verifier paths record an `llm.call` if they run. Assert the trace. The 3 001-line case and the incomplete file list must fail on current `main` because those calls still happen. The lock, binary, and generated case asserts zero `llm.call` and `skipped` / `all_changes_ignored`; if that is already true via T19, leave the assertion as the lock and do not expect `no_reviewable_files`.

**Acceptance criteria:**

- [x] A complete file list of 3 001 allowed lines expects zero `llm.call` (no conventions, review, or verifier record), `succeeded`, `summaryOnly: true`, coverage `partial`, reason `diff_too_large`, and no inline comments.
- [x] `file_list_complete = false` expects zero `llm.call`, summary-only reason `diff_inventory_incomplete`, and not `skipped`.
- [x] A pull request of only lock, binary, and generated files, stored with the fetcher's omission markers, expects zero `llm.call` and `skipped` / `all_changes_ignored`, not `no_reviewable_files`.
- [x] Exactly 3 000 allowed lines and a complete file list still invoke the review model.
- [x] A legacy technical-only change is not expected to become summary-only `no_reviewable_files`.
- [x] One residual case (complete inventory, not proven exclusions, not legacy technical-only) expects zero `llm.call` and reason `no_reviewable_files`.

**Verification:**

- [x] `uv run pytest tests/test_summary_only_no_llm.py` fails RED on the 3 001-line and incomplete-list call counts.
- [x] `uv run ruff check .`, `uv run ruff format --check .`, and `uv run mypy .` are clean for the new test module.
- [x] Manual check: the failure is a model call (or a missing short-circuit), not a broken fixture. `uv run pytest tests -k summary_only` staying green does not count as proof.

**Dependencies:** None

**Files likely touched:**

- `tests/test_summary_only_no_llm.py`

**Estimated scope:** Small

---

## Task 2: One shared 3 000-line constant

**Description:** Make `BuildReviewContext` use `MAX_DIFF_LINES` from `get_run_diff.py` and remove the second literal `MAX_REVIEWABLE_LINES`. Keep the comparison `>`. Do not change which omissions count. Exactly 3 000 stays an ordinary snapshot; 3 001 still sets `summary_only` on the stored rows.

**Acceptance criteria:**

- [x] The number 3 000 appears as a threshold in one place, `MAX_DIFF_LINES`.
- [x] Existing tests still treat 3 000 as not summary-only and 3 001 as summary-only.
- [x] Task 1's 3 001-line and incomplete-list tests are still RED on the model call.

**Verification:**

- [x] `uv run pytest tests/test_get_run_diff.py tests/test_review_context.py tests/test_run_processor_context_stage.py -q`
- [x] `uv run ruff check .`, `uv run ruff format --check .`, and `uv run mypy .`
- [x] Manual check: a search for `3000` / `3_000` in `app/modules/reviews/application/get_run_diff.py` and `review_context.py` shows a single threshold literal.

**Dependencies:** Task 1

**Files likely touched:**

- `app/modules/reviews/application/get_run_diff.py`
- `app/modules/reviews/application/review_context.py`

**Estimated scope:** Small

---

## Checkpoint: After Tasks 1–2

- [x] The new module fails because summary-only still calls the model
- [x] Threshold tests pass
- [x] Lint, format, and mypy are clean
- [ ] Review with a human before the behaviour change if the plan's open question on the third sentence is still unanswered (it does not block Task 3)

---

## Task 3: Short-circuit summary-only before any model call

**Description:** In `ReviewContextStage.run`, after the diff map and coverage exist and before `_open_rag`, stop for an incomplete file list (`diff_inventory_incomplete`) and for a stored `summary_only` row (`diff_too_large`). Record `context.build` with `status: summary_only`, that reason, empty blocks, and coverage `partial`, then raise an exception `ExecuteReviewRun` catches beside `NoReviewableText`. Publish with `NoModelAnswer` so there is no parsed model answer and no `llm.review_output`. Do not call conventions, the review model, or the verifier. Leave T19, snapshot `NoReviewableText`, and the legacy technical-only model path in place for Task 5 to lock. The residual `no_reviewable_files` branch (plan D-2 order 7) lands here only when it does not steal those paths.

**Acceptance criteria:**

- [x] Task 1's 3 001-line case and incomplete-list case are green: zero `llm.call`, including a conventions cache miss.
- [x] Trace is `policy.load` → `vcs.fetch_diff` → `context.build` → publish, with no `context.rag` and no `llm.repo_conventions`.
- [x] Incomplete inventory wins when the line flag is also set.
- [x] Exactly 3 000 lines with a complete file list still calls the review model.
- [x] No database transaction contains a model or GitHub call. The `context.build` write happens after the fetches the stage already performed.

**Verification:**

- [x] `uv run pytest tests/test_summary_only_no_llm.py tests/test_coverage_none_route.py tests/test_execute_review.py tests/test_review_context_stage.py -q`
- [ ] `uv run ruff check .`, `uv run ruff format --check .`, and `uv run mypy .`
- [ ] Manual check: a conventions cache miss on the 3 001-line fixture records no `llm.call`.

**Dependencies:** Task 2

**Files likely touched:**

- `app/modules/reviews/application/review_context.py`
- `app/modules/reviews/application/review_context_stage.py`
- `app/modules/reviews/application/execute_review.py`
- `tests/test_summary_only_no_llm.py`

**Estimated scope:** Medium

---

## Checkpoint: After Task 3

- [x] Zero `llm.call` for 3 001 lines and an incomplete file list
- [x] Coverage-none and legacy technical-only tests still match their current contracts
- [x] Lint, format, and mypy are clean

---

## Task 4: Persist `summaryOnly` and name the reason

**Description:** When the stage chooses any of the three summary-only reasons, persist `summary_only=true` and `review_patch=null` on that run's diff rows in the same short transaction as the `context.build` record. Do not clear the UI `patch`. Branch the check-run summary and the saved review summary on the context reason, using the sentences in plan D-5. Keep the title «AI-ревью: только сводка», conclusion `neutral`, verdict null, and the run link. Do not add coverage reason codes.

**Acceptance criteria:**

- [x] An incomplete file list under 3 000 lines finishes with `summaryOnly: true` on the API flag, not only inside `context.build`.
- [x] The check-run and the saved summary name `diff_too_large`, `diff_inventory_incomplete`, or `no_reviewable_files` with the D-5 sentence for that reason. The size sentence is not used for the other two.
- [x] Coverage is `partial`, verdict is null, and there are no inline comments.
- [x] The third sentence is the confirmed open-question text, or the plan default if that question was accepted with the plan.

**Verification:**

- [x] `uv run pytest tests/test_summary_only_no_llm.py tests/test_clean_without_text_views_postgres.py -q`
- [ ] `uv run ruff check .`, `uv run ruff format --check .`, and `uv run mypy .`
- [ ] Manual check: the incomplete-list check-run summary does not contain «PR слишком большой».

**Dependencies:** Task 3

**Files likely touched:**

- `app/modules/reviews/application/review_context_stage.py`
- `app/modules/reviews/application/check_runs.py`
- `app/modules/reviews/infrastructure/run_repository.py`
- `app/modules/reviews/application/findings_post_processor.py`
- `tests/test_summary_only_no_llm.py`

**Estimated scope:** Medium

---

## Task 5: Keep T19 and legacy technical-only on their current paths

**Description:** Confirm the new branch did not publish proven exclusions as `no_reviewable_files`, and did not publish a legacy technical-only change as summary-only. The stage order is plan D-2: incomplete list, then the line flag, then the existing skip and `NoReviewableText` paths, then the residual `no_reviewable_files` short-circuit. Adjust only the branch that stole one of those paths.

**Acceptance criteria:**

- [x] Lock, binary, and generated only: `skipped` / `all_changes_ignored`, zero `llm.call`, check-run `skipped`.
- [x] `test_the_same_change_on_a_legacy_run_is_built_for_the_model` still builds a legacy technical-only change for the model.
- [x] Snapshot technical-only still raises `NoReviewableText` and still does not call the model.
- [x] The residual `no_reviewable_files` test from Task 1 is green and is not a proven-exclusion fixture.

**Verification:**

- [x] `uv run pytest tests/test_summary_only_no_llm.py tests/test_coverage_none_route.py tests/test_review_context.py -q`
- [ ] `uv run ruff check .`, `uv run ruff format --check .`, and `uv run mypy .`
- [x] Manual check: the exclusion fixture's stored rows use `omission_reason` `generated` or `binary`, matching the fetcher.

**Dependencies:** Task 3. Run after Task 4 if both edit `review_context_stage.py`.

**Files likely touched:**

- `app/modules/reviews/application/review_context_stage.py`
- `tests/test_summary_only_no_llm.py`

**Estimated scope:** Small

---

## Checkpoint: After Tasks 4–5

- [x] `uv run pytest tests/test_summary_only_no_llm.py` is green
- [x] Proven exclusions stay `skipped`; legacy technical-only still reaches the model
- [x] Summary and check-run name the reason that actually fired
- [x] Lint, format, and mypy are clean

---

## Task 6: Specs match the branch, and the PR body links the UI follow-up

**Description:** Update the spec sentences that still say a summary-only run calls the model and that zero model calls are an unmet #107 goal. Leave SYSTEM_DESIGN §6.2 as it is: the diagram already shows zero model calls and does not call that an unmet goal. Do not edit the UI repo. In the PR body, link the member comment and a UI issue that owns the two doc pages. `how-it-works.mdx` is a core page.

**Acceptance criteria:**

- [x] PIPELINE_SPEC §2 describes the summary-only trace in D-1 and no longer says the attempt always calls the model or that zero calls are an unmet #107 goal.
- [x] PIPELINE_SPEC §7 names all three reasons in the summary-only check-run cell.
- [x] SYSTEM_DESIGN §11 no longer says the current summary-only run may call the model. §6.2 is unchanged. No-text partial stays described as not done.
- [x] CVS §2.3 no longer says the summary-only zero-model lifecycle is not activated. The outcome table is unchanged.
- [ ] The PR body uses `Refs #313`, has no closing keyword, and links https://github.com/larchanka-training/dmc-268-api-t6/issues/313#issuecomment-6399973417 plus a `dmc-268-ui-t6` issue for `how-it-works.mdx` and `faq.mdx`. ui#173 is not that follow-up.

**Verification:**

- [x] `uv run pytest tests/test_summary_only_no_llm.py -q` still green (docs-only task; run it to show the behaviour under the updated spec).
- [ ] Full gates at the checkpoint below.
- [x] Manual check: a search in PIPELINE_SPEC §2, CVS §2.3, and SYSTEM_DESIGN §11 finds no sentence that the summary-only path still calls the model.

**Dependencies:** Task 4 and Task 5

**Files likely touched:**

- `docs/PIPELINE_SPEC.md`
- `docs/SYSTEM_DESIGN.md`
- `docs/CONTEXT_AND_VERIFICATION_SPEC.md`

**Estimated scope:** Small

---

## Checkpoint: Complete

- [ ] `uv run ruff check .`
- [ ] `uv run ruff format --check .`
- [ ] `uv run mypy .`
- [ ] `uv run pytest`
- [ ] `uv run pytest tests/test_summary_only_no_llm.py` green, and `uv run pytest tests -k summary_only` is not offered as the proof
- [ ] User-doc follow-up is linked from the PR body; `docs-site` was not edited in this repo
- [ ] Ready for review
