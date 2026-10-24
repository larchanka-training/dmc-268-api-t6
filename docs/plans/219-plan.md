# Implementation Plan: Issue #219 (Reflection Guard, Budget, Summary, and CVS/RFS Conformance)

## Overview
This plan implements the remaining scope of #120 transferred to #219 by tech lead Alexander Khomutov:
bringing the Reflection Pass, prompt envelope, pre-send guards, token counters, summary builder, candidate filtering, and verifier inputs into strict alignment with `docs/CONTEXT_AND_VERIFICATION_SPEC.md` (CVS), `docs/RULES_FORMAT_SPEC.md` (RFS), and `docs/PIPELINE_SPEC.md` (PS).

## Architecture Decisions & Conformance

1. **Conservative Token Counter (RFS §3.3 :233-234, gateway parity `llm/settings.py:55`)**:
   - `EstimatedTokenCounter` must use 3 characters per token (`(len(text) + 2) // 3`) to align with the gateway token counter and provide a conservative upper estimate.
   - Verification test: `.review/rules.md` on 39,390 characters yields ~15,883 tokens and is rejected against the 12,000 policy budget with `PolicyBudgetExceededError` / `policy_prompt_budget_exceeded`.

2. **Pre-Send Guard on Post-Fit Input (CVS §4.1, §5.2, RFS §7.4)**:
   - In `ExecuteReviewRun`: `fit_review_context` is executed prior to the reviewer LLM call so that large files (e.g. 2,500 lines) are fitted into the model budget rather than triggering false-positive `llm_context_overflow`.
   - `verify_pre_send_guard` validates the fitted context. If mandatory blocks (policy, metadata, system) cannot fit into the budget without code, it fails closed with `PreSendGuardError` -> `RunFailure("llm_context_overflow")`.
   - Verifier input in `reflection_pass.py` is fitted according to CVS §7.1 (:782): room is reserved first for IDs, candidates, and mandatory policy documents before code excerpts are included.

3. **Provenance Pre-Send Guard (CVS §5.2 :99, :636-646, :725-726)**:
   - A dedicated pre-send guard validates the manifest blocks of every LLM call (review, verifier, conventions):
     - Blocks must belong to the active policy/diff snapshot.
     - Paths must be supported (no `unsupported_path` or deny-set rename/copy paths).
     - Block revisions must match the PR side (`head_sha` for RIGHT, `diff_old_sha` for LEFT, never an index commit revision).
   - Any violation aborts transmission immediately with `policy_context_violation` (not a candidate drop, but a failed send).

4. **Candidate-Level Drop vs Whole-Run Failure (CVS §6.2 :702, :729-730)**:
   - In `execute_review.py`: syntactically valid candidates whose `evidence_refs` point to unshown blocks or invalid lines are dropped as individual candidates with `evidence_not_shown`, without failing the entire run.
   - Schema/syntax errors (missing fields, wrong types) continue to fail the response with `llm_invalid_output`.
   - In `reflection_pass.py` and `review_output.py`: allowlist of evidence roles includes `retrieved_excerpt` (CVS §3.5). Block revisions and line boundaries are validated per CVS §7.1–7.2.

5. **Pass Ordering (CVS §6.2 :704)**:
   - Verifier runs on candidates *after* Pass 4 (coordinates, evidence validation, lint filter, dedup).
   - If candidates are empty after Pass 4 filters, the Verifier is not called (0 calls).

6. **Deterministic Summary Construction (CVS §8.2 :999-1038)**:
   - The raw model summary is never published under any circumstances.
   - The backend constructs `data["summary"]` deterministically from supported findings *after* applying the severity threshold:
     - `problem`: count and categories of supported findings after threshold (or neutral text when empty).
     - `done_well`: factual statement of analyzed scope.
     - `effort`: "large" for critical, "medium" for high, "small" for medium/low, "none" for info/zero.
   - Probe test: when severity threshold is `critical` and findings are only `medium`, the final summary reports 0 findings and clean status, not "Identified 1 verified issue".

7. **Verifier Inputs & Stable IDs (RFS §6, §8.2, CVS §7.1–7.2)**:
   - `service-defaults:<ID>` uses stable check IDs visible to the model in the envelope.
   - Verifier receives candidate `evidence_refs` and low-trust conventions.
   - Verifier prompt explicitly states: lack of basis for attribution requires `insufficient_context`.

8. **Profile Values & Parity (Part 8, RFS §7.2, §7.6)**:
   - Configured `execution_profile_of` in `worker.py` with explicit versions: `verifier_model=llm.primary.model`, `counter_version="counter/v2"`, `tokenizer_version="tokenizer/v1"`, `recovery_profile_version="recovery/v1"`, and `budget_profile_version="budget/v1"`.
   - Documented `"counter/v2"` factory resolution in `RULES_FORMAT_SPEC.md §7.2`.

9. **Spec Naming Alignments (Part 10, CVS:601, RFS:794-802)**:
   - `StageName`: `stage: Literal["conventions", "review", "verification"]` plus `recovery_kind: Literal["none", "retry", "repair", "fallback"]`.
   - `source_kind`: `diff` -> `source_code`, `candidate_finding` -> `candidate_findings`.
   - `side: Literal["LEFT", "RIGHT"] | None` on `ManifestBlock`.

## Phases & Tasks

### Phase 1: Conservative Token Counter & Post-Fit Pre-Send Guard (Parts 2 & 3)
- [ ] Task 1.1: Update `EstimatedTokenCounter` in `prompt_budget.py` to 3 chars/token (`(len(text) + 2) // 3`).
- [ ] Task 1.2: Add unit tests in `test_prompt_budget.py` verifying conservative 3 chars/token behavior and rejection of 39,390-char `.review/rules.md` against 12,000 token limit.
- [ ] Task 1.3: Update `ExecuteReviewRun` in `execute_review.py` to fit review context before pre-send guard and model call.
- [ ] Task 1.4: Implement verifier context fitting in `reflection_pass.py` prioritizing IDs and mandatory policy documents over diff excerpts (CVS §7.1).
- [ ] Task 1.5: Add tests for large diffs (e.g. 2,500 lines) passing execution without false-positive overflow.

### Phase 2: Provenance Pre-Send Guard (Part 1)
- [ ] Task 2.1: Implement `verify_provenance_pre_send_guard(manifest, snapshot)` checking allowed roles, source kinds, snapshot IDs, and revisions.
- [ ] Task 2.2: Ensure conventions call builds a manifest and runs the provenance pre-send guard.
- [ ] Task 2.3: Wire provenance guard into `ExecuteReviewRun` and `ReflectionPass`.
- [ ] Task 2.4: Add unit tests verifying `policy_context_violation` is raised when unallowed sources, wrong revisions, or unsupported paths are present.

### Phase 3: Candidate-Level Drop & Verifier Evidence Rules (Part 5)
- [ ] Task 3.1: In `execute_review.py`, change attribution failure on unshown evidence blocks to drop individual candidates (`evidence_not_shown`) instead of raising `InvalidReviewOutput`.
- [ ] Task 3.2: Add `retrieved_excerpt` to evidence role allowlist in `reflection_pass.py` and `review_output.py`.
- [ ] Task 3.3: Verify block revision against PR side in verifier evidence validator.
- [ ] Task 3.4: Add tests for single candidate drop while remaining valid candidates proceed to verifier.

### Phase 4: Pass Ordering & Deterministic Summary (Parts 4 & 6)
- [ ] Task 4.1: Reorder pipeline passes in `execute_review.py` so coordinates, evidence, and dedup occur before verifier invocation.
- [ ] Task 4.2: If candidate list is empty after filters, bypass verifier (0 calls).
- [ ] Task 4.3: Implement deterministic summary builder applied to post-severity-threshold findings in all cases (including 0 candidates and all supported).
- [ ] Task 4.4: Add test probe for severity threshold `critical` with `medium` findings verifying clean summary.

### Phase 5: Verifier Inputs, Stable IDs, Profile Values & Spec Naming (Parts 7, 8, 10)
- [ ] Task 5.1: Update `PromptBuilder` to render stable check IDs for `service-defaults:<ID>` and feed candidate `evidence_refs` to verifier.
- [ ] Task 5.2: Update verifier prompt with explicit instruction regarding `insufficient_context` for unsupported attributions.
- [ ] Task 5.3: Align `StageName` (`conventions`, `review`, `verification` + `recovery_kind`) and `source_kind` (`source_code`, `candidate_findings`).
- [ ] Task 5.4: Set explicit profile values in `worker.py` (`verifier_model`, `counter_version="counter/v2"`, etc.) and document in `RULES_FORMAT_SPEC.md §7.2`.
- [ ] Task 5.5: Clean up comments in `worker.py` and `policy_snapshot_unit_of_work.py` referencing closed #120.

### Phase 6: Non-Vacuum Tests & Quality Gates (Part 9)
- [ ] Task 6.1: Update AC 6 fake verifier in `test_reflection_evaluation.py` to check prompt inputs (code, policy, candidate).
- [ ] Task 6.2: Add test for zero candidates with snapshot present in `ExecuteReviewRun`.
- [ ] Task 6.3: Run full quality gates: `ruff check .`, `ruff format --check .`, `mypy --platform linux .`, `pytest`.
