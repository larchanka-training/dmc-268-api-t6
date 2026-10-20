# Todo: Issue #219 (Reflection Guard, Budget, Summary, and CVS/RFS Conformance)

## Phase 1: Conservative Token Counter & Post-Fit Pre-Send Guard (Parts 2 & 3)
- [x] Update `EstimatedTokenCounter` in `prompt_budget.py` to 3 characters per token (`(len(text) + 2) // 3`).
- [x] Add unit test in `test_prompt_budget.py` verifying conservative 3 chars/token behavior.
- [x] Add unit test in `test_policy_budget.py` / `test_prompt_budget.py` showing `.review/rules.md` on 39,390 characters yields ~15,883 tokens and raises `PolicyBudgetExceededError` against 12,000 limit.
- [x] Update `ExecuteReviewRun` in `execute_review.py` to fit context via `fit_review_context` before `verify_pre_send_guard` and `draft_review`.
- [x] Implement verifier context fitting in `reflection_pass.py` prioritizing IDs, candidates, and mandatory documents before diff excerpts (CVS §7.1).
- [x] Add unit tests for 2,500-line diff file fitting into budget without premature `llm_context_overflow`.

## Phase 2: Provenance Pre-Send Guard (Part 1)
- [x] Implement `verify_provenance_pre_send_guard` checking manifest blocks against snapshot (paths, origins, roles, revisions).
- [x] Support conventions call manifest and guard.
- [x] Integrate provenance guard into `ExecuteReviewRun` and `ReflectionPass`.
- [x] Add unit tests verifying `policy_context_violation` is raised when unallowed sources or mismatched revisions are present.

## Phase 3: Candidate-Level Drop & Verifier Evidence Rules (Part 5)
- [x] Update candidate attribution in `execute_review.py` so unshown evidence refs drop individual candidate (`evidence_not_shown`) instead of failing whole run.
- [x] Allow `retrieved_excerpt` in `reflection_pass.py` and `review_output.py` evidence role allowlist.
- [x] Check block revisions and line boundaries in verifier evidence validation.
- [x] Add unit tests for candidate-level drop and `retrieved_excerpt` acceptance.

## Phase 4: Pass Ordering & Deterministic Summary (Parts 4 & 6)
- [x] Reorder pipeline in `execute_review.py` so Pass 4 (coordinates, evidence, dedup) runs before verifier invocation.
- [x] Skip verifier call if candidates are empty after Pass 4 filters (0 calls).
- [x] Implement deterministic backend summary builder from post-threshold findings for all cases (0 candidates, partial, all supported).
- [x] Add test for severity threshold `critical` with `medium` findings verifying clean summary.

## Phase 5: Verifier Inputs, Stable IDs, Profile Values & Spec Naming (Parts 7, 8, 10)
- [x] Render stable check IDs for `service-defaults:<ID>` and feed candidate `evidence_refs` into verifier input.
- [x] Update verifier prompt with instruction that lack of attribution basis requires `insufficient_context`.
- [x] Align `StageName` (`stage: conventions|review|verification`, `recovery_kind`) and `source_kind` (`source_code`, `candidate_findings`).
- [x] Set explicit profile values in `worker.py` (`verifier_model`, `counter_version="counter/v2"`, etc.) and document in `RULES_FORMAT_SPEC.md §7.2`.
- [x] Clean comments referencing #120 in `worker.py` and `policy_snapshot_unit_of_work.py`.

## Phase 6: Non-Vacuum Tests & Quality Gates (Part 9)
- [x] AC 6 fake verifier checks prompt inputs in `test_reflection_evaluation.py`.
- [x] Zero candidates with snapshot test in `test_execute_review.py`.
- [x] `uv run ruff check .`
- [x] `uv run ruff format --check .`
- [x] `uv run mypy --platform linux .`
- [x] `uv run pytest` (domain and integration suites)
