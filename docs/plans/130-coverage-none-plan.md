# Implementation plan: #130 PR F5, R4 "an all-unsupported change ends with coverage none and no model calls"

Branch `feat/130-coverage-none`. It is stacked on PR #346 (F3, `fix/168-trace-and-output`, head `db5c5cf5`), which
sits on `main` `916796ac` (#283).
DB and vhost: `imp130f5`. The task list is in [130-coverage-none-todo.md](130-coverage-none-todo.md).

## 1. Overview

Remaining item R4 of #130 is defined by:

- acceptance 6224180604, "PR, в котором все изменения unsupported или unsupported + ignored, не вызывает модель
  (CONTEXT §11, coverage none)";
- the F1 plan `130-snapshot-path-plan.md:412,475,570` and TL-11 (`:535`): "R4 is F5, after #283, gated like F1".

The canon has two parts:

- CVS §2.2 table `:151-155`, row `:154`. When the inventory is complete and trusted, has at least one T, holds only
  E/T otherwise and has no incomplete entry, the result is a backend `FinalReviewResult`:
  - coverage=none, findings=[], verdict=null;
  - 0 LLM calls;
  - publishing, then `succeeded`, with a neutral check-run and summaryOnly=false.
- CVS §11 `:2033`, `:2035`, `:2036`, and PS `:258-263`: "это не T19 и не summaryOnly".

Here **T** is `unsupported_path`, `identity_unresolved`, `too_large` and `unsupported_change` (CVS `:127-129`).
**E** is masks, generated files, binary files and a proven `empty_file`.

F5 also folds the duplicated `pr_base_sha` trace field of `review_context_stage.py` into one helper. This was
promised in the #324 thread (4371402763, 4371461778) and recorded in the #168 part 3 acceptance (6411080696, item 4).

## 2. Item status at `db5c5cf5`

| Item | Status | Anchor | Change (one line) | Tests (red first) | Files | Open-PR overlap |
| --- | --- | --- | --- | --- | --- | --- |
| R4a: T classification | partly | `run_coverage.py:327`, `coverage_schema.py:112-114`; TP `:452` "В очереди #213 unsupported_path сохраняет partial" | on the snapshot path only, count `unsupported_path` and `identity_unresolved` as T, so a T-only inventory is `none`; legacy unchanged | v4: unsupported_path-only, identity-only and unsupported+ignored are `none`; the same input on v2/v3 stays `partial`; mixed, incomplete-inventory and missing_patch cases stay `partial`; a saved `none` restores | `coverage_schema.py`, `run_coverage.py`, `review_context_stage.py:564` | none |
| R4b: zero-LLM none route | open | stage `:564-586`; `process_run.py:307-311`; `execute_review.py:226-228` | on the snapshot path a T-only, not summary-only change records `context.build`, skips conventions, review and verifier, and publishes a backend none result | stage, processor and use-case fakes; a PostgreSQL run through the worker composition; legacy unchanged; summary-only boundary | `review_context.py`, `review_context_stage.py`, `execute_review.py`, `store_review_output.py`, `run_repository.py` | #344 and #340 (stage and import blocks; separate hunks); #330 (`execute_review.py:262+`, separate) |
| R4c: render none | done (#283) | `findings_post_processor.py:374-405`, `:433-476`, `check_runs.py:170-182`, `verdict.py:60-63` | none | already pinned: `test_clean_without_text_views.py:163`, `test_clean_without_text_result.py:248` | none | none |
| Helper `pr_base_sha` | open, **3 copies** | `review_context_stage.py:164-165` (`context_summary`), `:623-627` (build-failure branch, added by #283), `:656-657` (`_record_nothing_to_review`) | one module-level helper, behaviour-neutral | existing 5 tests in `tests/test_diff_map_pinned_base.py` (`:114`, `:182`, `:200`, `:213`, `:319`) | `review_context_stage.py` | #344 (separate hunks) |

## 3. What #283 already does on the main path (`916796ac`)

What F5 builds on:

- **`run_coverage.py` (new, 626 lines).**
  - `_project_run_coverage` (`:261-345`) classifies every stored file in this order:
    - DiffMap omission;
    - ignore and deny-set;
    - proven empty;
    - binary/generated;
    - too_large;
    - summary_only;
    - proven metadata-only;
    - patch state;
    - send-scope loss;
    - reviewed.
  - Status: NONE ⇔ complete inventory, `reviewed == 0`, and every reason in `{too_large, unsupported_change}` (`:327-332`).
    E-only inventories are NONE too.
  - `all_excluded` (`:128-135`) is complete, every file E and no reasons.
  - Restore (`:408-560`) re-validates saved coverage against the durable snapshots and the publication DiffMap.
    It already requires the `unsupported_path` and `identity_unresolved` counts to match (`:507-512`).
- **`coverage_schema.py`.** `valid_coverage_status` (`:100-128`) is shared by both persistence readers. NONE allows
  only `too_large` and `unsupported_change`.
- **`review_context_stage.py`.**
  - Coverage is projected after `apply_ignore` (`:562-564`).
  - Service-excluded names join the deny-set (`:565-570`).
  - **The only coverage-based early exit is T19.** `coverage.all_excluded` gives `RunSkipped("all_changes_ignored")`
    (`:574-584`), before RAG, the build and conventions, on both paths. `no_changes` (`:535-541`) and the builder's
    `ALL_CHANGES_IGNORED` (`:634-635`) are the other skips.
  - Every `context.build` response carries `coverage`, the build-failure branch included (`:617-629`).
- **`execute_review.py`.** It computes `incomplete_paths` (send-scope loss) and re-wraps the answer as
  `VerifiedReviewOutput` on both paths (`:327-369`).
- **`process_run.py`.** It stores an empty blob for a proven empty file. Nothing coverage-related.
- **`run_repository.py`.** `get_post_processing_input` (`:745-844`):
  - restores coverage from the latest `context.build` row that has `coverage`;
  - builds the publication map from that row's `diff.diff_old_sha` (`read_context_coverage_pins`,
    `run_coverage.py:381-405`).
- **`store_review_output.py`.**
  - It requires a backend summary (`:180-181`).
  - It derives the verdict with `coverage_verdict` (`:196`).
  - It writes `coverage` into `review.postprocess` (`:234`).
  - It always writes `llm.review_output` from the answer it is given (`run_repository.py:863-880`).
- **`findings_post_processor.py`.**
  - It applies `finalize_run_coverage` (`run_coverage.py:172-210`).
  - The summary (`:374-405`): "Текстовые изменения не проверены: …" for NONE.
  - The body line "**Verdict:** нет доступных для анализа изменений" (`_overview_block`, `:433-476`).
  - `check_runs.py:170-182` gives the neutral «AI-ревью: нет доступных для анализа изменений».
- **What #283 does not do.** Its own PR body says it "does not claim the zero-LLM lifecycle is complete". PS
  `:225-230` (and the same block in CVS `:1380-1385`, RFS `:1433-1438` and SD `:1863-1868`) keeps
  none/summary-only/no-text partial as targets.

## 4. An all-T change today, and where "all unsupported" is decided

Where the facts come from:

- **`vcs.fetch_diff` has no `unsupported_path`.** Its omissions are `binary`, `empty_file`, `too_large`, `generated`
  and `missing_patch` (`vcs_diff.py:93-98`).
- **`unsupported_path` and `identity_unresolved` come from the DiffMap.** `_omission` (`diff_map.py:360-367`) sets
  them. The builder only copies them into its `omissions` diagnostics (`review_context.py:456-463`).
- **Coverage status is the single decision point.** `_project_run_coverage` (`run_coverage.py:283-332`) sets it.
  `StoreDiffSnapshot` withholds an unsupported path from model patches and from the 3 000-line threshold
  (`get_run_diff.py:113-125`; `excluded` is `review_context_stage.py:434-445`). It still counts the lines of a
  `too_large` file (`_UNCOUNTED_OMISSIONS = {binary, generated}`, `get_run_diff.py:159-172`).
- **Which T are reachable live.**
  - `unsupported_change` is never reached: `_proven_metadata_only` (`run_coverage.py:347-372`) needs an exact
    metadata patch, and neither adapter produces one. GitHub and GitLab send no patch for a pure rename or mode
    change, which gives `missing_patch`, i.e. partial.
  - The reachable cases are:
    - `too_large` (a blob over 1 MiB, or an oversized patch);
    - `unsupported_path` (for example a backslash or control character in the name);
    - `identity_unresolved` (a rename or copy without a usable old name).

Today, for the same input, on the snapshot path (v4) and the legacy path (v3 or lower, still created by a
repository's `prompt_version_id` override, `webhook_runs.py:113`):

| Input (complete inventory) | Coverage | Model calls today (both paths) | Published today |
| --- | --- | --- | --- |
| only `unsupported_path` / `identity_unresolved`, with or without E | **partial** (`unsupported_path` is incomplete at `:327`) | the builder returns `summary_only`/`no_reviewable_files` (`review_context.py:516-519`), a `context_payloads` row is written; conventions (`llm.call`, or a cache hit) and review `llm.call` with no changed files; on v4 the reflection pass runs, and the verifier is called only if a candidate survives | succeeded; verdict null; «AI-ревью: проверка неполная»; body "Проверка неполная: неподдерживаемый путь (n)"; a model finding with an unknown path adds `unknown_path` |
| only `too_large` (≤ 3 000 counted lines) / `unsupported_change`, with or without E | **none** | the same calls | succeeded; verdict null; «AI-ревью: нет доступных для анализа изменений» (an `unknown_path` drop turns it partial) |
| only `too_large`, > 3 000 counted lines | none, and the snapshots `summary_only=true` (`too_large` precedes `summary_only` at `:298-300`) | the same calls | succeeded; summaryOnly=true; «AI-ревью: только сводка» |
| only E | none, `all_excluded` | **0** (T19) | skipped, `all_changes_ignored`, check-run skipped |

## 5. Design decisions

- **DD1. One T set, widened together with the route, on the snapshot path only (TL-Q3).**
  - `coverage_schema.py` gets `TECHNICAL_REASON_CODES = {too_large, unsupported_change, unsupported_path,
    identity_unresolved}`.
  - **The projection.** `project_run_coverage(..., snapshot_path: bool = False)` (`run_coverage.py:253-258`) hands
    the flag to `_project_run_coverage`. With True, `incomplete` (`:327`) uses the full set; otherwise it keeps
    today's `{too_large, unsupported_change}`.
  - **The stage is the only caller that produces a status, and it knows the path.**
    - The call is `review_context_stage.py:564`.
    - `self._snapshot_for_llm` is set in `begin` (`:463-478`), and `run()` calls `begin` first when needed
      (`:509-510`). The stage passes `snapshot_path=self._snapshot_for_llm is not None`.
  - **The restore stays as is.** Its re-projection (`run_coverage.py:497`) uses only the counts of the no-policy
    projection (`:498-512`), never its status.
  - **The validator is widened path-agnostically.** `valid_coverage_status` (`:112-114`) is read at
    `run_coverage.py:513` and `saved_review_result.py:106`. Widening it to the full set only accepts more, and only
    the snapshot path writes such a row, so no path-aware validator is needed.
  - **Legacy stays byte-identical.**
    - unsupported_path- or identity-only stays partial, and too_large- or unsupported_change-only stays none. Both
      still call the model.
    - That legacy "none + LLM" state already exists since #283. It is left as it is, under "legacy unchanged".
  - `missing_patch`, `patch_*`, `summary_only`, `review_input_incomplete`, `diff_inventory_incomplete`,
    `classification_unavailable` and `unknown_path` stay incomplete.
- **DD2. The route is gated to the snapshot path (TL-11).** The stage takes it when all of the following hold:
  - `self._snapshot_for_llm is not None`;
  - coverage is NONE with at least one reason, i.e. T-only (a `RunCoverage` property, for example `technical_only`,
    so the stage adds no import next to #344's import hunk);
  - no stored snapshot is `summary_only` (DD5).

  The check comes after the T19 check (`:574-584`) and before `_open_rag` (`:586`). The legacy path keeps its model
  calls.
- **DD3. Trace of a none attempt.**
  - The order is `policy.load → vcs.fetch_diff → context.build → review.postprocess`, then `github|gitlab.publish_review`
    from the publish consumer. There is no `context.rag`, no `llm.call` and no `llm.repo_conventions`.
  - There is **no `llm.review_output`** row: no model answer exists (TL-Q1).
  - F3's rule holds: one `context.build` per attempt.
  - The `context.build` response is `{status: "no_reviewable_text", reason: "no_reviewable_text", diff: {digest, files,
    diff_old_sha, file_list_complete, pr_base_sha?}, ignored_files, blocks: [], coverage}`. It needs a new
    `ContextStatus.NO_REVIEWABLE_TEXT` (`review_context.py:116-120`).
  - **`diff_old_sha` is mandatory.** The DiffMap digest includes it (`diff_map.py:258`), and the publisher rebuilds
    the map from it. Without it, `restore_context_coverage` falls back to `classification_unavailable`/partial with
    a warning, and the run would publish «проверка неполная». `_record_nothing_to_review`'s current `diff` has only
    `{digest, files}`.
  - The extra fields are opt-in for the none record only. The T19 records (`no_changes`, `all_changes_ignored`)
    stay byte-identical, as PS `:280` documents them and `tests/test_diff_map_pinned_base.py:195` pins them.
  - **The `context_payloads` row is written** with the same response (`save_context_payload`, as at stage
    `:637-640`). This keeps F3's invariant that "the attempt's one context.build is also its context_payloads row".
    The only exceptions stay the skips and a caught build failure.
    - Nothing in the app reads the row: the only code is the writer, `run_lifecycle_store.py:762-783`.
    - The coverage restore and the publication map read the `context.build` run_action
      (`run_repository.py:787-829`). The run detail reads `review.postprocess` (`saved_review_results.py:43`).
    - The row write also re-checks the origin and `running` (`run_lifecycle_store.py:764-772`). A revoked origin
      therefore stops before the store, as on a normal run.
- **DD4. Signal.**
  - The stage raises `NoReviewableText` (new, defined in `review_context.py` next to `ContextStatus`) after recording
    `context.build`.
  - It propagates out of `RunProcessor.prepare` before the conventions call (`process_run.py:307-311`).
    `process_run.py` and both protocols stay unchanged.
  - `ExecuteReviewRun.execute` catches it around `prepare` only (`:226`). It then publishes the backend result and
    skips `get_review_prompt_input`, `get_pull_request_meta`, `draft_review` and the reflection pass.
  - The handler sees the run as handled (ACK). A raise outside `ExecuteReviewRun` would surface as `internal_error`.
    Only the worker composition with a stage can raise it (`worker.py:881-893`).
  - **Integrity checks are kept.** `stage.begin` (`review_context_stage.py:455-480`) already enforces these at
    `policy.load`:
    - the snapshot-path refusals of `snapshot_for_review` (`review_path.py:66-76`): served provider, snapshot
      present, `is_ready_for_llm`, and that it pins the run's review prompt;
    - `validate_execution_profile_versions`;
    - the R7 policy budget.

    Existing tests pin them (`tests/test_review_path.py:287`, `:640`). The route skips only the re-checks in
    `ExecuteReviewRun.execute` (`:238-312`). Of these, only `verify_verifier_mandatory_budget` is not also done in
    `begin`. It guards a verifier call that a none run never makes, so a T-only v4 run whose mandatory blocks do
    not fit the verifier window now ends `succeeded`/none instead of failing with `policy_prompt_budget_exceeded`
    after a conventions call. This is accepted, and recorded in the PR body.
- **DD5. Summary-only is not routed.**
  - A `too_large`-only change over 3 000 counted lines keeps today's summary-only path.
  - Excluding `too_large` from the threshold is #107/#175 (PS `:274`, "Legacy main ещё включает too_large в порог").
  - Routing it would publish «только сводка» with 0 LLM. That is a different canonical outcome (TL-Q4).
- **DD6. Backend publication.**
  - It goes through the common `StoreReviewOutput` (T8 guard, `store_review_output`, `enter_publishing`, the queue
    pointer).
  - The input is an explicit backend marker: an empty mapping type defined in `store_review_output.py` and passed
    through the existing `execute(run_id, raw_output)`, so the `ReviewOutputHandler` protocol in `review_output.py`
    stays unchanged (DD8).
  - No answer is parsed and no placeholder summary is stored. The post-processor's backend summary is the only one.
  - `run_repository.store_review_output` takes `model_output: dict | None` and writes no `llm.review_output` for
    None.
  - The verdict is null, so the event is `COMMENT` (`verdict.py:66-70`), also for a repository set to
    `REQUEST_CHANGES`.
  - The inline `PublishReviewOutput` (`review_output.py:666`) is not composed with a stage in production. It stays
    unchanged and fails closed on the marker (`InvalidReviewOutput`).
- **DD7. The helper.**
  - `_with_live_base(diff: dict[str, Any], pr_base_sha: str | None) -> dict[str, Any]`, module level, used by the
    three copies and by the DD3 record.
  - Commit it first, so the feature commit adds no fourth copy.
- **DD8. Eval static inputs untouched.**
  - `eval_provenance.py:19-42` (`_REVIEW_CODE`) lists `review_output.py`, `run_failures.py`, `prompt_builder.py`,
    `llm.py` and `llm/*`.
  - F5 adds no type or line to them, so `static_digest` and the recorded replay stay byte-identical.

## 6. Tasks (one commit each, test-first; details, files and mutants in the todo)

1. `refactor(reviews): record the live PR base through one helper (#130)`: DD7.
2. `feat(reviews): count unsupported paths and unresolved identities as technical skips (#130)`: DD1.
3. `feat(reviews): store a backend result without a model answer (#130)`: DD6; not yet wired.
4. `feat(reviews): finish an all-unsupported change without model calls (#130)`: DD2–DD5.
5. `docs(specs): describe the zero-LLM none route of the snapshot path (#130)`: §8.

Checkpoints:

- after task 2: the gates, and the unit coverage suite (`tests/test_run_coverage.py`, `tests/test_clean_without_text*.py`);
- after task 4: the full suite with PostgreSQL and RabbitMQ;
- after task 5: the docs lines are re-checked against the code.

## 7. Tests that pin today's behaviour

- **With DD1 gated, no existing test flips.**
  - `tests/test_clean_without_text_context.py::test_empty_marker_cannot_skip_invalid_identity_or_incomplete_inventory`
    (`:108-113`) runs on legacy (v2) bindings and keeps `partial` with `{unsupported_path: 1}`. It becomes the
    legacy pin.
  - Its v4 twin (`none`, routed) is new in T2 and T4.
  - TP `:452` cites the test as the legacy pin.
- No run-level test pins "an all-T run calls the model" today; `grep too_large|unsupported_path` over the stage,
  processor, worker and e2e tests finds none. The behaviour is new-test territory.
- These stay green unchanged:
  - `test_run_coverage.py::test_unresolved_identity_is_never_hidden_by_an_empty_or_binary_marker` (code beside T is
    partial);
  - `test_exclusion_only_stage_skips_before_any_model_input` (T19);
  - `test_clean_without_text_result.py::test_proven_technical_only_result_has_none_coverage_and_null_verdict`;
  - the check-run view tests.

## 8. Docs in this PR versus G2

Only the lines F5 makes false. Line numbers are at `db5c5cf5`.

| Doc | Lines | Change |
| --- | --- | --- |
| PS, CVS, RFS, SD (the shared #213 block) | PS `:225-230`, CVS `:1380-1385`, RFS `:1433-1438`, SD `:1863-1868` | "zero-LLM lifecycle summary-only/none/no-text partial остаются целевыми" becomes: none is active on the snapshot path since #130 F5; summary-only and no-text partial stay targets of #107. The four copies stay identical. |
| PS | `:258-263` (after "это не T19 и не summaryOnly") | add one sentence: on the snapshot path the route is built (#130 F5) with the trace of DD3; legacy (≤ v3) does not take it and still calls conventions and review, with unsupported_path and identity_unresolved partial there (DD1) and too_large/unsupported_change none as since #283; summary-only by too_large lines keeps its path (`:274`) |
| PS | `:280` `context.build` row | add the none response of DD3; the `context_payloads` sentence is unchanged (the row is written, F3's exceptions stay the skips and a caught build failure) |
| PS | `:707` succeeded row | "(общий zero-LLM lifecycle none остаётся целью #107/#175)" becomes "на snapshot path — без вызовов модели (#130 F5); legacy вызывает модель" |
| TP | `:452`, `:454` | the coverage column names the F5 tests; drop "после остатка #130" and "В очереди #213 unsupported_path сохраняет partial" |

Not changed:

- **openapi.** No schema change. `RunStatus`, `Verdict` and the nullable verdict descriptions (`:913`, `:963`,
  `:1182`, `:1658`) already cover a null verdict, and #283 added no public coverage DTO. The UI contract pins
  (`UI_CONTRACT_COMMIT`, `ui_run_step_zod_contracts.json`) are untouched.
- **CVS §2.2 / §11, RFS `:1150-1160` / `:1309-1312`, SD `:355-366` / `:2022-2027`.** These already state the
  target.
- **CICD.** No migration and no env.

G2 (`docs/plans/130-snapshot-path-docs-plan.md` on `docs/130-snapshot-path-docs`, PR #343, `97f3baae`) keeps
P1–P14, C4, C8, C9, R3, R6, S4, T1–T6 and O1–O3:

- **No F5 line overlaps a G2 item.** The none order is written into the PS `:258` paragraph, not into the order
  bullets of P5 (`:125-131`).
- **Signal G2:** T1 (TP §2.7 coverage map) adds the F5 test files.

## 9. Overlap with open PRs

| PR | Shared file | Hunks (their base) | F5 hunks | Verdict and comment |
| --- | --- | --- | --- | --- |
| #346 (base, F3) | stage, PS, TP | all | stacked | merge #346 first; F5 rebases on main after it |
| #344 (axyi, #168) | `review_context_stage.py`, `tests/test_review_context_stage.py`, RFS | stage `:57-61` (review_ignore imports), `:476-487` (`begin`); RFS `:739`, `:1076` | stage `:143-170`, `:562-590`, `:617-629`, `:644-669`; no new stage import; tests in new files | separate; no comment needed (own PR); keep new imports out of `:57-69` |
| #330 (axyi, #144) | `execute_review.py`, `review_context.py`, `tests/test_execute_review.py`, PS `:170` | `execute_review.py:44-50`, `:262-284`, `:293`; `review_context.py:13`, `:69`, `:170-215`, `:281`, `:672-745` | `execute_review.py:41-47` (one name in the `review_context` import), `:225-230`; `review_context.py:116-121` | separate (one unchanged line or more between); FYI comment in #330: F5 adds an early return before `get_review_prompt_input` |
| #340 (ndovnar, #333) | `review_context.py:271`, `run_repository.py` (`:7-683`), `handle_review_run.py`, `review_path.py` (adds `bitbucket`), `publish_run_review.py`, PS `:744`, `:808`, TP `:91-145`, `:658`, `:999`, `:1085` | as listed | `run_repository.py:846-880`; none of the others | no textual overlap; **coordination comment in #340**: Bitbucket v4 runs will reach the zero-LLM none route (0 inline, body only, no `llm.review_output`), so its publisher and status adapter must handle an empty-findings none result; its e2e should cover it, or it is a follow-up |
| #343 (axyi, G1) | CVS, RFS, SD, openapi | CVS `:509-1131`, RFS `:600-910`, SD `:877-2152` | CVS `:1380`, RFS `:1433`, SD `:1863` | separate; G2 note above |
| #334 (skvertl), #342 (createunix) | CVS `:832-1673`, SD `:29-1906`; RFS `:402-1132`, openapi | as listed | CVS `:1380`, SD `:1863`, RFS `:1433` | separate; no comment |
| #339, #345 | none | | | none |

## 10. Eval and live impact

- **Eval is not affected.**
  - `review/scripts/eval_live.py` and `eval_replay.py` import none of `review_context_stage`, `run_coverage`,
    `process_run` or `execute_review`. They render prompts from the case patches.
  - No case among the 24 main and 8 rules cases has an unsupported path, a too_large file or a T-only patch. The
    largest patch is 438 lines of code files, and the "unsupported" in LOG-04's README is prose.
  - DD8 keeps `static_input_paths` and `static_digest` identical, so a replay stays byte-identical.
- **Live is affected on merge.** v4 is active since F2 (#332), so every new GitHub and GitLab run takes the route
  for a T-only inventory.

## 11. Deploy note

- **Order.**
  1. Merge #346.
  2. Rebase F5 on main and merge it.
  3. The merge deploys staging (`Deploy staging`, CICD §1).

  No ui deploy is needed before or after: no contract changes, and a succeeded run with verdict null already
  renders. This repository has no api prod job. Any prod promotion comes after the staging smoke below.
- **Smoke on a sandbox PR, through the staging App.** Expect on each:
  - 0 `llm.call` and 0 usage events;
  - trace `policy.load, vcs.fetch_diff, context.build, review.postprocess, github.publish_review`;
  - a COMMENT review with "**Verdict:** нет доступных для анализа изменений";
  - check-run neutral «AI-ревью: нет доступных для анализа изменений»;
  - API `status=succeeded`, `verdict=null`, `summaryOnly=false`.

  Run it on:
  1. a PR that adds only `a\b.txt` (`unsupported_path`);
  2. a PR that adds only a text file over 1 MiB with at most 3 000 lines (`too_large`).

  Smoke 1 has been checked only at the stage level. If GitHub's file listing or the blob read fails for such a name
  before the stage, report that and rely on smoke 2.
- **Rollback.**
  - No DB revision is added; the floor stays at `0043`.
  - A pre-F5 image rejects saved `none` coverage that holds `unsupported_path` or `identity_unresolved` at both
    boundaries (old `valid_coverage_status`). Only v4 runs processed under F5 have such rows.
  - **API.** `load_review_results` warns with the run id and serves `fallback_review_result`
    (`saved_review_result.py:146-170`): classification_unavailable/partial, verdict null, summary "…Проверка
    неполная: нет достоверного сохранённого покрытия…".
  - **Publisher retry.** For a run left in `publishing`, the publisher reposts the stored `run.review_body`
    unchanged (`run_lifecycle_store.py:465+`). The final check-run then reads the same fallback
    (`run_lifecycle_store.py:425-439`), so «AI-ревью: проверка неполная» sits beside a «нет доступных…» body.
  - A run still `running` is re-staged by the old image under the old rules: partial, with the model called.
- **Cost.** Each such run saves the conventions and review calls.

## 12. Risks

| Risk | Impact | Mitigation |
| --- | --- | --- |
| none record without `diff_old_sha` → restore fallback → «проверка неполная» | High | DD3; the PostgreSQL test asserts restored `none` and no warning |
| a legacy (v3 override) run takes the route or changes its coverage | Med | DD1/DD2 pass `snapshot_for_llm` to the projection and the route; a legacy test with the same input asserts `partial` and both model calls |
| summary-only too_large routed with 0 LLM and «только сводка» | Med | DD5; boundary tests 3 000 / 3 001 |
| eval static digest drift | Low | DD8 |
| rebase conflict with #344 or #330 imports | Low | no new import in the stage; one name inside the `review_context` import block of `execute_review.py` |

## 13. Decisions of the tech lead

- **Q1.** Decided by the tech lead on 2026-10-27 (per the coordinator's brief): accepted; a none run goes through
  publishing and ends `succeeded`, not skipped, posts a COMMENT review with the none body and a neutral check-run,
  writes no `llm.*` rows, and writes the `context_payloads` row, which keeps F3's invariant.
- **Q1b.** Decided by the tech lead on 2026-10-27 (per the coordinator's brief): accepted; the route skips
  `verify_verifier_mandatory_budget`, because no verifier call is made, so a T-only v4 run that would fail that check
  now ends as none.
- **Q2.** Decided by the tech lead on 2026-10-27 (per the coordinator's brief): accepted; every T-only inventory takes
  the route, as the canon says (CVS `:154`, TP `:454`), which live means `unsupported_path`, `identity_unresolved` and
  `too_large` up to 3 000 lines.
- **Q3.** Decided by the tech lead on 2026-10-27 (per the coordinator's brief): accepted; the widened classification
  and the route come together, on the snapshot path only (the DD1 `snapshot_path` flag), legacy stays byte-identical
  (an `unsupported_path`-only legacy run is still partial), and the pre-existing deviation, legacy `too_large`-only =
  none + LLM since #283, is left alone and recorded as a note.
- **Q4.** Decided by the tech lead on 2026-10-27 (per the coordinator's brief): accepted; `too_large` over 3 000 lines
  stays off the route, on the summary-only path, and the threshold fix stays with #107/#175.
- **Q5.** Decided by the tech lead on 2026-10-27 (per the coordinator's brief): accepted; no ui change before F5, and
  the coordinator files a ui follow-up: a user-docs line, plus the optional rule "a `context.build` with `status
  no_reviewable_text` counts as context done".

- **Deploy unit (F5 review, 2026-10-27).** A rebase-merge must not deploy an intermediate commit: after T2
  alone a v4 `unsupported_path`-only run is none + LLM, because the route lands in T4. The PR head is the deployable
  unit.

### Q5 evidence (kept for the ui follow-up)

- **Verified by running it.** A throwaway vitest ran `runProgress` on the ui#199 head (`dmc-268-ui-t6-197`
  `0d1f9b1`, identical to ui main `18cb51b`). It used the none log `policy.load, vcs.fetch_diff, context.build`
  [+ `review.postprocess`] [+ `github.publish_review`]:

  | Run state | Stages |
  | --- | --- |
  | running | context process |
  | publishing, succeeded | all finish (by status), with or without `review.postprocess` |
  | failed or cancelled after `review.postprocess`, or with a failed publish | context and analysis finish, publishing error/stopped |
  | failed or cancelled after `context.build` but before `review.postprocess` | **context error/stopped** |

- **What that last row means.** Only a store-transaction failure, a T8 guard miss or a cancel in that sub-second
  window lands there. Context did finish, but there is no LLM stage to blame.
- **#283 never produces such a log live.** Every published run passes conventions, which write
  `llm.repo_conventions` on a hit and on a miss. ui#198 is docs-only (`how-it-works.mdx`) and says nothing about
  it.

## 13a. Out of scope (stays with #107/#175)

- The summary-only and no-text-partial zero-LLM routes (CVS `:152`, `:155`; TP `:456-457`).
- Excluding T lines from the 3 000-line threshold (PS `:274`).
- `FinalReviewResult`/`review.finalize` (PS `:118`).

## 14. Gates (every task; run `uv sync` first if `.venv` is missing; check `df -h /tmp` before long runs)

```
uv run ruff check .
uv run ruff format --check .
uv run mypy . --exclude '^docs/reports/'
TEST_DATABASE_URL=postgresql+psycopg://app:app@localhost:5432/imp130f5 \
TEST_RABBITMQ_URL=amqp://app:app@localhost:5672/imp130f5 uv run pytest
```

Known local-only noise: 7 failures in `tests/test_deploy_monitoring.py` from a `~/.local/bin/sh` wrapper. They pass
with `~/.local/bin` off PATH. Never read any `.env`.
