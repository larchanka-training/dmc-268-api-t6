# Tasks: replay warnings and GitHub failure docs (#206)

Plan: [206-plan.md](206-plan.md). Original baseline: `66077f292bcd829d1807c520f48b48fd053891f2`; final verification base: `5d183fe6cb3a0bb0ed68ebba062f1852f2d16bf9`.
**Plan and five coordination comments explicitly approved on 2026-10-23. Tasks 1–5 complete. All incremental and final reviews passed with Standards 0 / Spec 0 findings. Ready for publisher handoff; publication itself pending.**

## Task 1: Confirm preconditions and record baseline

**Description:** Establish the approved scope, ownership coordination and current offline baseline.

**Acceptance criteria:**
- [x] Human approves the saved plan; separately authorizes the five proposed PR comments.
- [x] Comments are posted on still-open owning PRs before their files are edited; main/ownership rechecked.
- [x] Existing replay suite and recorded replay outcomes, JSON/warnings/console/exit/HEAD, and pin-union inventory saved outside Git.

**Verification:** `uv run pytest tests/test_eval_replay.py -q`; baseline replay and union check in plan.
Verified with approved uv cache/runtime access: baseline suite 98 passed; replay 24 cases, warnings=[], exit 0; pins recorded/current/union 46/46/46, intersection empty. Original baseline HEAD/main was 66077f2; final verification uses 5d183fe. Evidence: /private/tmp/dmc-268-206-baseline-*, replay-before.*, pins-before.json, ownership.json.
**Dependencies:** None. **Files likely touched:** None (temporary evidence only). **Scope:** Small.

## Task 2: Expose simultaneous pin and content drift

**Description:** Prove the masked diagnostic with temporary fixtures, then make the minimal fix.

**Acceptance criteria:**
- [x] Regression first fails on the current `elif`: fixture starts clean, then adds a rule pin and changes an existing recorded rule's bytes.
- [x] Green regression asserts both literal warnings in path-set/digest order; manifest and raw answer bytes stay unchanged.
- [x] Implementation independently compares the recorded-path digest without changing safety handling or report/scoring behavior.

**Verification:** Run the named regression RED then GREEN; run `uv run pytest tests/test_eval_replay.py -q`, including C6 dropped-pin/custom-rule tests.
RED: 1 failed (missing digest warning); GREEN: 1 passed. Evidence: /private/tmp/dmc-268-206-red.*, green.*, task2-tests.*. Replay suite: 99 passed (exit 0), including existing C6 dropped-pin/custom-rule tests. Targeted Ruff lint/format and git diff --check passed. Independent review passed: Standards 0 / Spec 0 findings; Task 3 authorized.
**Dependencies:** Task 1. **Files likely touched:** `tests/test_eval_replay.py`, `review/scripts/eval_replay.py`. **Scope:** Small.

## Task 3: Verify warning and exit compatibility

**Description:** Keep observable behavior unchanged for clean or singly changed inputs.

**Acceptance criteria:**
- [x] Clean/path-only/digest-only/combined fixture cases match the exact warning matrix in the plan; clean gives `[]`.
- [x] Warning-only publishable CLI runs return 0, both warnings render in console; current error/nonpublishable exit tests remain green.
- [x] Replay-suite checkpoint passes without changing real pins or committed responses.

**Verification:** Target compatibility checks and full `uv run pytest tests/test_eval_replay.py -q`.
Six targeted checks passed (exit 0): new-rule path-only, rule-edit digest-only, combined regression
(all assert clean inputs first), new public `main()` CLI warning/exit check, existing missing-manifest
CLI error and paid-metadata nonpublishable CLI check. Replay suite: 100 passed (exit 0).
No further production change; targeted Ruff lint/format and `git diff --check` passed.
Evidence: `/private/tmp/dmc-268-206-task3-compatibility.*`, `task3-tests.*`, `task3-validation.txt`.
Second independent review passed: Standards 0 / Spec 0 findings; Task 4 authorized.
**Dependencies:** Task 2. **Files likely touched:** `tests/test_eval_replay.py` (only if needed beyond current assertions). **Scope:** Small.

## Task 4: Align documentation with existing VCS classification

**Description:** Correct the diff-failure row and commit-message error paragraph after coordination.

**Acceptance criteria:**
- [x] PIPELINE_SPEC diff row includes 401/422/429 without headers, other non-excluded HTTP errors and timeout/connection failures as `diff_fetch_failed`.
- [x] README gives matching `commit_messages_fetch_failed` classes and keeps invalid-page failures; 404/nonlimited 403 stay `github_forbidden` in both documents.
- [x] Wording matches numeric Retry-After/remaining=0 limit evidence, source classifier and adjacent commit/access rows; GitLab/other sections unchanged.

**Verification:** Manual source-to-doc class comparison; `git diff --check`; inspect exact two-file diff.
Manual comparison matched `classify_vcs_error`: 404/nonlimited 403 forbidden; limited 403,
all 429, 5xx/401/422/other HTTP errors and timeout/transport/connection failures retryable.
The adjacent commit-message/access rows are unchanged; only the diff row and README error
paragraph changed. Invalid-page validation remains documented. Ownership refreshed: same five
commented PRs; no new owner. `git diff --check` passed. Evidence:
`/private/tmp/dmc-268-206-task4-docs.diff`, `task4-validation.txt`, `ownership-task4.json`.
Task 4 independent review passed: Standards 0 / Spec 0. README was further clarified to ASCII decimal digits after updating to main 5d183fe; final full-diff review also passed with Standards 0 / Spec 0.
**Dependencies:** Tasks 1 and 3. **Files likely touched:** `docs/PIPELINE_SPEC.md`, `review/README.md`. **Scope:** Small.

## Task 5: Prove final invariants and hand off for review

**Description:** Complete mandatory gates and reproducible evidence before review/publication.

**Acceptance criteria:**
- [x] `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy .`, `uv run pytest` all pass; record actual skips/limitations.
- [x] Post-change recorded replay equals pre-change warnings, metrics/provenance, console and exit at the same base; no newly introduced warning.
- [x] Changed files intersect neither recorded/current pin union nor response directory; manifest/response bytes unchanged; Standards/Spec review has no findings.

**Verification:** Full gates; replay-after command and report comparison; pin-union check in plan; ownership/main recheck and review diff.
Final verification at `5d183fe6cb3a0bb0ed68ebba062f1852f2d16bf9` (HEAD = fetched main;
remote main rechecked unchanged at handoff):
- `uv run ruff check .`: passed, exit 0.
- `uv run ruff format --check .`: 528 files already formatted, exit 0.
- `uv run mypy .`: no issues in 525 source files, exit 0.
- `uv run pytest` with reporting-only `PYTEST_ADDOPTS=-ra`: 4,186 passed, 886 skipped,
  2 dependency deprecation warnings in 352.63 s, exit 0. Skips: 845 require PostgreSQL,
  24 require PostgreSQL + RabbitMQ, and 17 require RabbitMQ; integration URLs were unset.
- Before/after replay JSON, console and exit files are byte-identical; warnings=[], exit 0,
  unchanged metrics/provenance. Exact base replay source was executed in memory at its
  original file path for the fresh-base baseline, without changing reviewed worktree files.
- Recorded/current/union pins: 46/46/46; changed-file intersection empty. Every pin and all
  25 recorded response/manifest files match base Git-object bytes; no response edits.
- Ownership rechecked: same five commented PRs; no new owner. `git diff --check` passed.
- Each incremental review and the final full-diff review reported Standards 0 / Spec 0.
  Final review includes the refreshed base and README ASCII-decimal clarification.

Evidence: `/private/tmp/dmc-268-206-final-{lint,format,mypy,pytest}.{log,exit}`,
`final-skips.json`, `replay-{before,after}.{json,log,exit}`, `replay-comparison.json`,
`pins-after.json`, `ownership-final.json`, and `final-validation.json`.
Implementation/review checkpoints complete; ready for publisher handoff. Publication itself
is pending. No commit, push or PR was created by development.

**Dependencies:** Tasks 2–4. **Files likely touched:** This checklist/evidence notes only if needed. **Scope:** Small.

## Final checkpoint

- [x] All acceptance criteria and full gates are verified; any rebase-related changes reverified.
- [x] Agent-loop reviewer reports no Standards or Spec findings.
- [x] Implementation, verification and review are complete; ready for publisher handoff under the authorized workflow. Actual branch/commit/push/PR publication is pending; tech lead closes #206 after acceptance.
