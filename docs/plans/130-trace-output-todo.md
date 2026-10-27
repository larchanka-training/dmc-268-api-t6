# Task list: #168 part 6 "trace and output" in #130 F (PR F3)

The design is in [130-trace-output-plan.md](130-trace-output-plan.md); this list adds no scope.
Base: `origin/main` `f40cabf3`. Branch `fix/168-trace-and-output`, DB and vhost `imp130f3`.

- Each task is one Conventional commit with the subject `… (#168)` and `Refs #130` in the body.
- Test first: flip and add the tests, watch them fail for the stated reason, then change the code.
- Gates for every task: the four commands in the plan.
- Every brief says: never read any `.env`; no checkout, switch or stash in other clones.

## T0: Plan documents

- **Commit:** `docs(plans): plan the #168 part 6 trace and output items (#168)`.
- **Files:** these two files, staged with `git add -f` (`docs/plans/` is excluded locally).
- **Done when:**
  - [x] only these two files are committed;
  - [x] Q1-Q3 are answered and recorded in the plan.

## T1: One `context.build` per attempt (items 1 and 2)

- **Commit:** `fix(reviews): record context.build once per attempt (#168)`.
- **Blocked by** (merge only; Q1-Q3 are decided, see the plan):
  - Q1: the ui change seen live on staging-ui before this PR merges, since a merge to `main` deploys
    staging.
- **Files:**
  - `app/modules/reviews/application/run_trace.py`:
    - delete `FAST_TOKEN_LIMIT` and `TracedReviewPromptRepository` (`:132-179`);
    - delete the orphaned imports (`:14-18`);
    - keep `ContextPayloadStore` (`:128`);
    - add the protocol `ContextTrace(RunTrace, ContextPayloadStore, Protocol)`.
  - `app/modules/reviews/application/review_context_stage.py`:
    - the keyword `payloads: ContextPayloadStore | None = None`;
    - the write `await self._payloads.save_context_payload(run_id, step.response)` between `:637` and
      `:638`, outside the `try`.
  - `app/worker.py`:
    - drop the import `:144` and the wrap `:294-295`;
    - the stage factory passes `payloads=trace` (`:661-677`);
    - narrow to `ContextTrace | None`: `GitHubAdapters.context_stage` (`:573`), the factory's return
      type (`:610`) and its inner `stage`.
  - `app/bootstrap/gitlab_reviews.py`: `trace: ContextTrace | None` at `:103`, and `payloads=trace` at
    `:156-170`.
  - `tests/test_run_policy_context_origin_postgres.py:547-557`: the wrapper's `trace` annotation becomes
    `ContextTrace | None`, otherwise mypy fails.
  - `app/modules/reviews/infrastructure/run_lifecycle_store.py:762`: `schema_version=2` (Q2(a)
    accepted).
  - Docs:
    - `docs/PIPELINE_SPEC.md`: delete `:113` and `:176`; edit `:172`, `:126` and `:130`, as in the plan
      section "Docs";
    - `docs/MONITORING.md:61`;
    - `deploy/monitoring/grafana/dashboards/overview.json:560`, `:618`.
  - Tests: see the steps.
- **Steps:**
  0. **Find the flips.**
     1. In a throwaway edit, delete the wrap at `worker.py:294-295`.
     2. Run the full suite.
     3. The red set is the authoritative flip list. Compare it with the plan's table "Tests that flip",
        add what is missing, and revert the throwaway edit.
  1. **Red.**
     - Flip the tests in the table.
     - Add the new tests below.
     - Each new test must fail on `f40cabf3` for the stated reason.
  2. **Code.** The stage write, the wiring, the deletions.
  3. **Docs and dashboard text.** "Once per attempt since F3; attempts before F3 show two."
  4. **Green.** Run the gates.
- **New tests and the mutants each must kill:**
  - `test_review_context_stage.py`, the stage writes its build response as the payload:
    - the row equals the recorded `context.build` response, for `review` and for `summary_only`;
    - kills: no write; a write of a different dict (for example without `omissions`, or before the RAG
      blocks are added).
  - `test_review_context_stage.py`, no payload on a skip or a caught build failure:
    - `no_changes`, the stage's `all_changes_ignored` before the build, the build's
      `all_changes_ignored`, and a caught build failure all give 0 writes;
    - kills: a write before the skip raise; a write in the `except` path.
  - `test_review_context_stage.py`, a store error fails the attempt:
    - a store that raises `RuntimeError` makes `run()` raise, and the payload is not kept;
    - kills: the write inside the `try` (`:607-633`), where the error is swallowed as an optional build
      failure.
  - `test_review_context_stage.py`, no ignored name in the build or the payload:
    - with `generated/**` ignored, neither the trace nor the payload contains the ignored path, and
      `ignored_files == 1`;
    - kills (item 2): writing the prompt read's `omitted_files`, or any summary built from the stored
      snapshot list.
  - `test_run_context_origin_postgres.py`, the stage write fences a retired origin (replaces
    `:74-216`):
    - the origin retires before the stage write → `RunCancelled`; the existing terminal payload is
      unchanged; 0 LLM requests;
    - the second half: the origin retires during the paid conventions call → the review call is
      refused by `CheckpointedProvider` + `AttemptCheckpoint`; 1 LLM request, 1 `usage_events`;
    - kills: a write that skips the origin lock; a composition without the checkpoint.
  - `test_snapshot_path_e2e_postgres.py`, one build per attempt on both paths, and the payload row:
    - `_SNAPSHOT_PATH_STEPS` and `_LEGACY_STEPS` have one `context.build`, and the
      `context_payloads.summary` equals it;
    - kills: keeping the wrap (`worker.py:294-295`); a GitHub factory without `payloads`.
  - `test_gitlab_worker_postgres.py`, the GitLab composition (assertion placed before `:280`, away from
    #283's hunk):
    - exactly one `context.build`, and one payload row;
    - kills: the GitLab bundle without `payloads`.
  - `test_run_policy_context_origin_postgres.py:622-625`: exactly one `context.build`.
- **Flips:**
  - `test_snapshot_path_live_pin.py:553-559`, `:613-622`: drop the prompt-read tuple and the second
    build. The provider messages stay byte-identical.
  - `test_worker_queue_postgres.py:701`, `:704-708`: the composition without a stage writes no
    `context.build` and no payload row.
- **Done when:**
  - [x] the gates pass;
  - [x] the step-0 red set matches the extended table;
  - [x] `rg -n "TracedReviewPromptRepository|FAST_TOKEN_LIMIT" app tests` is empty;
  - [ ] the PR body has:
    - [ ] the trace change: one fewer action in `GET /api/runs/{id}/actions` and in SSE, no schema change;
    - [ ] the ui dependency and the deploy order (Q1);
    - [ ] the dashboard shift (the counts halve, the p95 moves);
    - [ ] the `context_payloads` shape change (Q2);
  - [ ] the coordination comments are posted on #283, #330 and #340, after their texts are approved.

## T2: Escape `diagnostic_path` (item 3)

- **Commit:** `fix(reviews): escape diagnostic paths in the context trace (#168)`.
- **Files:**
  - `app/modules/reviews/application/diff_map.py`: `diagnostic_text`, after `is_supported_path`
    (`:170`), away from the hunks of #283 (`:178+`) and #340 (`:29-43`);
  - `app/modules/reviews/application/review_context_stage.py:196`;
  - `docs/PIPELINE_SPEC.md:172` (one clause);
  - tests in `tests/test_diff_map.py` and `tests/test_review_context_stage.py`.
- **Tests and the mutants each must kill:**
  - `"src/модуль\n.py"` → `"src/модуль\\x0a.py"`, with the Cyrillic kept.
    - Kills: no escaping; escaping all non-ASCII.
  - `"a\x85b"` (C1) and `"a‮b"` (Cf) are escaped.
    - Kills: escaping C0 only.
  - `"a\\nb"` (a backslash and an `n`) and `"a\nb"` give different texts.
    - Kills: an unescaped backslash.
  - 5 000 characters → at most 1 024 plus `…`; a control character at the cut is never split.
    - Kills: no bound; a cut inside an escape.
  - A stage run whose provider path is `"src\\bad.py"` records the escaped text, while
    `ContextOmission.diagnostic_path` and `test_review_context.py:687` stay raw.
    - Kills: escaping at the payload level.
- **Done when:**
  - [x] the gates pass.

## T3: The check-run text (item 5)

- **Commit:** `fix(reviews): correct the ignore limit check-run text (#168)`.
- **Files:** `app/modules/reviews/application/check_runs.py:55`; a test in
  `tests/test_review_context_stage.py`, next to `:921`.
- **Test and the mutant it must kill:**
  - `failure_reason("policy_ignore_limit_exceeded")` is exactly «В блоке review-ignore слишком много
    масок или слишком длинная маска.»;
  - no `_ERROR_TEXTS` value contains the word «длинна» (a word match: «длинная» starts with it);
  - kills: the old text.
- **Done when:**
  - [x] the gates pass;
  - [ ] the PR body notes that rules-status shows the text through `failure_reason`
    (`rules_status.py:111`) until #342.

## T4: Drop the dead locators (item 6)

- **Commit:** `refactor(worker): drop the unused policy source locators (#168)`.
- **Files:** `app/worker.py:644`, `:658`, `:691`, `:696`.
- **Tests:** none new. This is a refactor, guarded by ruff, mypy and the factory tests
  (`test_run_policy_context_origin_postgres.py:226`, `:538`; `test_rag_review_context_postgres.py:216`;
  `test_snapshot_path_e2e_postgres.py:325`).
- **Done when:**
  - [x] the gates pass;
  - [x] `rg -n "_locators|locators=" app/worker.py` is empty.

## After merge

- [ ] AC check in #168 (Q3 recorded).
- [ ] The dashboard captions, verified on staging.
- [ ] Signal G2: rebase P2, P5 and P6 on F3's PIPELINE_SPEC lines.
