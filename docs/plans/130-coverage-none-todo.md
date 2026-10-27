# Task list: #130 PR F5, R4 coverage none without model calls

The design is in [130-coverage-none-plan.md](130-coverage-none-plan.md). This list adds no scope.

- **Base:** PR #346 head `db5c5cf5` on `main` `916796ac`.
- **Branch:** `feat/130-coverage-none`.
- **DB and vhost:** `imp130f5`.

Rules for every task:

- Each task is one Conventional commit with the subject `… (#130)`.
- Test first: add or flip the tests, watch them fail for the stated reason, then change the code.
- The gates for every task are the four commands in plan §14.
- New tests go into new files, `tests/test_coverage_none_*.py`. They stay out of
  `tests/test_review_context_stage.py` (#344) and `tests/test_execute_review.py` (#330).
- Do not edit `review_output.py`, `run_failures.py` or any other `_REVIEW_CODE` file
  (`review/scripts/eval_provenance.py:19-42`), so the eval static digest stays as it is.
- Every brief says:
  - never read any `.env`;
  - no checkout, switch or stash in other clones;
  - `gh` is read-only.

## T0: Plan documents

- **Commit:** `docs(plans): plan the coverage none route (#130)`.
- **Files:** these two files, staged with `git add -f`. `docs/plans/` is excluded locally.
- **Done when:**
  - [x] only these two files are committed;
  - [x] Q1–Q5 are answered and recorded in plan §13.

## T1: One helper for the live PR base (DD7)

- **Commit:** `refactor(reviews): record the live PR base through one helper (#130)`.
- **File:** `app/modules/reviews/application/review_context_stage.py`.
  - Add `_with_live_base(diff, pr_base_sha)` at module level, next to `context_summary`.
  - Use it at all three copies:
    - `:164-165` (`context_summary`);
    - `:623-627` (the build-failure response);
    - `:656-657` (`_record_nothing_to_review`).
  - Adds the key only when not None. Behaviour-neutral.
- **Tests:** no new test. The existing tests in `tests/test_diff_map_pinned_base.py` pin all three copies:
  - `:114` `test_a_retargeted_pr_is_mapped_on_the_pinned_run_base`;
  - `:182` `test_a_pr_on_its_pinned_base_records_no_live_base`;
  - `:200` `test_a_pr_without_changes_records_its_live_base_only_after_a_retarget`;
  - `:213` `test_the_live_base_of_an_attempt_is_not_carried_into_the_next_one`;
  - `:319` `test_optional_context_failure_keeps_the_retarget_only_when_it_exists`.

  Run them before and after.
- **Mutants that must fail an existing test:**
  - the helper drops the None guard (`:182`, `:200` no-retarget case, `:319[False]`);
  - the helper writes another key (`:114`, `:200`, `:319[True]`);
  - one call site keeps `self._pr_base_sha` from a previous attempt (`:213`).
- **Done when:**
  - [x] `grep -n 'pr_base_sha"\] =\|{"pr_base_sha"' review_context_stage.py` finds only the helper;
  - [x] the five tests pass;
  - [x] the gates pass.

## T2: T classification (DD1)

- **Commit:** `feat(reviews): count unsupported paths and unresolved identities as technical skips (#130)`.
- **Files:**
  - `app/modules/reviews/application/coverage_schema.py`: add `TECHNICAL_REASON_CODES`. `valid_coverage_status`
    (`:112-114`) allows NONE only with reasons in that set.
  - `app/modules/reviews/application/run_coverage.py`:
    - `project_run_coverage(..., snapshot_path: bool = False)` (`:253-258`) hands the flag to
      `_project_run_coverage`;
    - `incomplete` (`:327`) uses the full set only with the flag;
    - the restore re-projection (`:497`) is unchanged, since it uses counts only;
    - add the property `RunCoverage.technical_only` (`status is NONE and bool(reasons)`), next to `all_excluded`
      (`:128-135`).
  - `app/modules/reviews/application/review_context_stage.py:564`: pass
    `snapshot_path=self._snapshot_for_llm is not None`.
  - `tests/test_clean_without_text_context.py:108-113` stays unchanged as the legacy pin (v2 bindings, `partial`).
  - New `tests/test_coverage_none_classification.py`.
- **Red tests:** pure functions plus the stage on legacy bindings.
  1. `unsupported_path`-only (`src\\bad.py`) with `snapshot_path=True` is `none`, `reviewed_files=0`, reasons
     `[unsupported_path]` with path None. With the default, it is `partial`, as today. On a v4 stage the
     `context.build` coverage is `none`; the same input on a v2 stage is `partial`.
  2. `identity_unresolved`-only (a rename without a previous name) is `none`.
  3. `unsupported_path` plus a masked file (`**/*`), and `unsupported_path` plus binary, are `none`.
     `all_excluded` is false and `technical_only` is true.
  4. `unsupported_path` plus a missing-patch ordinary file is `partial`.
  5. `unsupported_path` plus reviewed code is `partial`.
  6. `unsupported_path` with an incomplete inventory is `partial` (`diff_inventory_incomplete`).
  7. A saved `none` with `unsupported_path` restores through `restore_context_coverage`, with no
     "Invalid saved context coverage" warning (caplog), and through `saved_review_result`.
  8. `valid_coverage_status(NONE, reasons={"missing_patch": 1})` is False, and the same call with
     `{"unsupported_path": 1}` is True. Both are literals.
  9. `technical_only` is false for E-only, for partial and for complete.
- **Mutants that must fail:**
  - the projection keeps the old set (1, 2, 3);
  - the projection ignores the flag, which widens legacy (1, the v2 half);
  - the stage passes a constant flag (1, the stage halves);
  - the projection is widened but the validator is not (7, 8);
  - `missing_patch` is added to the set (4, 8);
  - `identity_unresolved` is left out (2);
  - the status ignores `reviewed` (5);
  - the inventory check is dropped (6);
  - `technical_only` ignores the reasons (9).
- **Done when:**
  - [x] 1–9 pass and `tests/test_clean_without_text_context.py` passes unchanged;
  - [x] `tests/test_run_coverage.py` and `tests/test_clean_without_text*.py` pass unchanged otherwise;
  - [x] the gates pass.

## Checkpoint A (after T2)

- [x] The gates are green.
- [x] The coverage unit suites are green.

## T3: Backend result without a model answer (DD6)

- **Commit:** `feat(reviews): store a backend result without a model answer (#130)`.
- **Files:**
  - `app/modules/reviews/application/store_review_output.py`:
    - add a marker type, an empty `Mapping[str, object]`;
    - on the marker, `execute` does not call `parse_review_output`;
    - it builds an empty `ReviewOutput` whose summary is the post-processor's backend summary;
    - it passes `model_output=None` to the repository;
    - the rest of `execute` is unchanged.
  - `app/modules/reviews/infrastructure/run_repository.py`: `store_review_output` (`:846-900`) takes
    `model_output: dict[str, object] | None`. None writes no `llm.review_output`. Findings and `review_body` are
    stored as before. Widen the type by redeclaring `store_review_output` in `PublishingRepository`
    (`store_review_output.py:55`, a sub-protocol of `ReviewOutputRepository`). Widening a parameter is
    Liskov-safe, so `review_output.py:612` stays untouched.
  - New `tests/test_coverage_none_publication.py` (fake UoW) and `tests/test_coverage_none_publication_postgres.py`.
- **Red tests:**
  1. The fake UoW with saved `none` coverage (too_large) gets `review.postprocess` with:
     - `verdict` None;
     - `coverage.status == "none"`;
     - `inline == 0`;
     - the summary literal `"Подтверждённых замечаний нет; Текстовые изменения не проверены: Превышен лимит размера, содержимое не проверено (1)."`.

     The body contains `"**Verdict:** нет доступных для анализа изменений"`. Expected text is literal, not
     recomputed (backend.md §5).
  2. No `llm.review_output` is written (repository spy).
  3. With `repository_review_event = REQUEST_CHANGES` the queue pointer event is `COMMENT`.
  4. PostgreSQL:
     - `run.state = publishing`;
     - exactly one `review.postprocess`;
     - no `llm.review_output` row;
     - `run.review_body` is set.

     A second `execute` returns False and adds no row.
  5. A model answer (not the marker) still writes `llm.review_output`, as today: a regression guard.
- **Mutants that must fail:**
  - the marker is parsed as a model answer (1);
  - `llm.review_output` is still inserted (2, 4);
  - the verdict comes from `verdict()` instead of `coverage_verdict` (1);
  - the event ignores the null verdict (3);
  - the T8 guard is skipped on the second call (4).
- **Done when:**
  - [x] 1–5 pass;
  - [x] the path is not wired yet, so live is unchanged;
  - [x] the gates pass.

## T4: The zero-LLM none route on the snapshot path (DD2–DD5)

- **Commit:** `feat(reviews): finish an all-unsupported change without model calls (#130)`.
- **Files:**
  - `app/modules/reviews/application/review_context.py`: add `ContextStatus.NO_REVIEWABLE_TEXT =
    "no_reviewable_text"` (`:116-120`), and `class NoReviewableText(Exception)` beside it.
  - `app/modules/reviews/application/review_context_stage.py`, in `run()`, after the T19 check (`:574-584`) and
    before `_open_rag` (`:586`). When `self._snapshot_for_llm is not None and coverage.technical_only and not
    any(s.summary_only for s in stored)`:
    - record `context.build` through `_record_nothing_to_review`, with an opt-in parameter (for example the
      `diff_map`) that only the none record passes. The diff then carries `digest`, `files`, `diff_old_sha`,
      `file_list_complete` and the live base through T1's helper. Status and reason are `no_reviewable_text`, and
      the response includes `coverage`;
    - the T19 records (`no_changes`, `all_changes_ignored`) stay byte-identical: `{digest, files, pr_base_sha?}`
      (PS `:280`);
    - save the `context_payloads` row with the same response (`save_context_payload`, as at `:637-640`). This keeps
      F3's invariant; a failed write or a revoked origin ends the attempt before publishing;
    - raise `NoReviewableText`.

    Add no import in `:57-69`.
  - `app/modules/reviews/application/execute_review.py:226`: wrap only `prepare` in `try/except NoReviewableText`.
    On it, `return await self._publisher.execute(run_id, <T3 marker>)`. Import the name inside the existing
    `review_context` import (`:41-47`) and the marker from `store_review_output`.
  - New `tests/test_coverage_none_route.py`, using the v4 stage from `tests/test_review_path.py:92`, `:235`, `:715`.
  - New `tests/test_coverage_none_route_postgres.py`, extending the `tests/test_snapshot_path_e2e_postgres.py`
    composition with a T-only diff.
- **Red tests:**
  1. On a v4 stage with `unsupported_path` only:
     - `NoReviewableText` is raised;
     - the trace is `["policy.load", "context.build"]`;
     - the `context.build` response has status `no_reviewable_text`, `coverage.status == "none"` and
       `diff.diff_old_sha` equal to the merge base;
     - the RAG is not opened and the builder is not called (a counting builder);
     - the `Payloads` spy holds one row equal to the `context.build` response.
  2. The same input on a v2/v3 stage returns a payload (the builder runs) and raises nothing.
  3. E-only on v4 still raises `RunSkipped("all_changes_ignored")`: T19 comes first. Its `context.build` `diff`
     equals the literal `{"digest": <map digest>, "files": 1}`, without `diff_old_sha` or `file_list_complete`. No
     test pins this today; the `no_changes` literal is pinned at `test_diff_map_pinned_base.py:195`.
  4. `too_large`-only on v4, with 3 000 counted lines: the route is taken. With 3 001 lines (`summary_only`): no
     route, the builder runs.
  5. The processor with the v4 stage propagates `NoReviewableText`. The conventions spy is not called; the
     `test_review_path.py` `RecordingConventions` pattern works for this.
  6. `ExecuteReviewRun` with a processor that raises it:
     - the publisher gets the marker;
     - `get_review_prompt_input`, `get_pull_request_meta` and `draft_review` are not called;
     - the reflection pass is not called;
     - the result is True.
  7. PostgreSQL, worker composition, one v4 run whose only change is `a\b.txt`:
     - outcome ack;
     - `state == publishing`;
     - `tools == ["policy.load", "vcs.fetch_diff", "context.build", "review.postprocess"]`;
     - 0 `llm.call` and 0 manifests;
     - no HTTP body sent to the LLM transport;
     - 0 `usage_events`;
     - `review.postprocess.coverage.status == "none"` (restored, not `classification_unavailable`), with no
       restore warning;
     - `verdict` None;
     - a queue pointer with event `COMMENT`;
     - one `context_payloads` row (schema 2) equal to the `context.build` response.
  8. PostgreSQL, the same diff on a v3 run: `llm.call` operations `["conventions", "review"]` as today. Legacy is
     unchanged.
- **Mutants that must fail:**
  - no `snapshot_for_llm` check (2, 8);
  - the route is placed before T19 (3);
  - the T19 record gains `diff_old_sha` or `file_list_complete` (3, and `test_diff_map_pinned_base.py:195`);
  - the summary-only exclusion is dropped (4);
  - the `diff` has no `diff_old_sha` (7: restore fallback, partial);
  - the `context_payloads` row is not written (1, 7);
  - `_open_rag` runs first (1);
  - conventions are called (5, 7);
  - `draft_review` or `get_pull_request_meta` is called (6);
  - the `except` wraps more than `prepare` (6, with a fake whose `draft_review` raises it);
  - the stage builds before raising (1).
- **Done when:**
  - [x] 1–8 pass;
  - [x] the full suite passes with PostgreSQL and RabbitMQ (the review's run on `e903bb68`: 9 468 passed, 3 failed,
        the known `test_gitlab_admin_postgres` CLI subprocess failures);
  - [x] `test_snapshot_path_e2e_postgres.py` and `test_clean_without_text*_postgres.py` pass unchanged.

## Checkpoint B (after T4)

- [x] The full suite is green (the same run).
- [x] `grep -rn "llm.review_output" app` shows the only writer is still `run_repository.py:863-880`.
- [x] `git diff db5c5cf5 -- $(uv run python -c "from review.scripts.eval_provenance import _REVIEW_CODE; print(*_REVIEW_CODE)")`
      is empty, or the PR body states the digest change. Diff against the branch base, so #346's own changes do not
      count.

## T5: Docs for the changed behaviour (plan §8)

- **Commit:** `docs(specs): describe the zero-LLM none route of the snapshot path (#130)`.
- **Files and lines** (at `db5c5cf5`):
  - the shared #213 block, kept byte-identical in all four copies: PS `:225-230`, CVS `:1380-1385`, RFS
    `:1433-1438`, SD `:1863-1868`;
  - PS `:258-263` (one sentence), `:280` (the none `context.build` response; the `context_payloads` sentence is
    unchanged), `:707`;
  - TP `:452`, `:454`: the coverage column names T2 and T4 tests; remove "после остатка #130" and "В очереди #213
    unsupported_path сохраняет partial".
- **Done when:**
  - [ ] `diff` of the four block copies is empty;
  - [ ] every new code anchor resolves on the branch head;
  - [ ] no G2 line is touched: PS `:104-135`, `:171-180`, CVS `:766-779`, TP `:82`, `:119`, `:197-211`, `:484-500`
        (G plan §2).

## Checkpoint C (complete)

- [x] Gates green.
- [ ] PR body: What / Why / How to verify / Refs. It names Q1–Q5 with the decisions, the deploy note (plan §11) and
      the smoke recipe.
- [x] Coordination comments posted:
  - #340: the none route reaches Bitbucket v4 runs (comment 6450105709);
  - #330: FYI, an early return before `get_review_prompt_input` (comment 6450106185);
  - #347: comment 6450106627;
  - G2 signal: the TP §2.7 map gets the F5 tests (#343 merged before F5, no comment needed).
- [x] The user-docs checkbox links the ui follow-up issue (Q5): a docs line, plus the optional rule that a
      `no_reviewable_text` `context.build` counts as context done (larchanka-training/dmc-268-ui-t6#201).
- [ ] Rebased on `main` after #346 merges.
