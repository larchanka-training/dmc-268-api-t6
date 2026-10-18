# Task List: Markdown Rules and Reflection Pass (#120)

## Phase 1: Foundation (Domain Contracts & Policy Envelope)
- [x] **Task 1: Domain models for Policy Snapshot & Bindings**
  - Domain models defined by Engineer 3 in #134; consumed in `app/modules/reviews/application/policy_snapshot.py` alongside `CallManifest` and `ManifestBlock`.
  - Add unit tests verifying snapshot immutability, document presence, and digest checks.
- [x] **Task 2: PromptBuilder envelope & prompt injection defenses**
  - Update `PromptBuilder` to accept `PolicySnapshot` with Markdown documents.
  - Implement block isolation with `block_id`, `source_role`, `source_kind`, and line numbers.
  - Implement robust escaping preventing XML breakout and role manipulation (`</repo_rules>`, system role spoofing).
  - Add unit tests for XML escaping, tag integrity, and prompt injection payloads.
- [x] **Task 3: Policy token budgeting & pre-send guards**
  - Implement policy token estimation/counter and verify rendered policy ≤ 12,000 tokens.
  - Implement pre-send guard checking total input token budget (fast: 60k, deep: 150k).
  - Add unit tests for budget enforcement and `policy_prompt_budget_exceeded` rejection.

## Checkpoint 1: Foundation
- [x] Unit tests pass for policy snapshot, envelope rendering, and injection guards (`uv run pytest tests/test_policy_snapshot.py tests/test_prompt_builder.py tests/test_prompt_builder_markdown.py tests/test_policy_budget.py`).

## Phase 2: Attribution v2 & Schema Contracts
- [x] **Task 4: ReviewOutput v2 & Attribution Validation**
  - Update `ReviewFinding` with `evidence_refs: list[EvidenceRef]`.
  - Validate `rule_name` values against allowed enum (`.review/rules.md`, `AGENTS.md`, `service-defaults:<ID>`, `null`).
  - Implement attribution resolver: check that `evidence_refs` point to shown lines in call manifest.
  - Add unit tests for v2 finding parsing, valid/invalid evidence_refs, and attribution checks.
- [x] **Task 5: Schema artifacts**
  - Add `review/schemas/review-output.schema.v2.json` and `review/schemas/verifier-output.schema.v1.json`.
  - Add unit tests validating sample payloads against draft 2020-12 schemas.

## Checkpoint 2: Attribution & Schemas
- [x] Unit tests pass for v2 schema and attribution validation (`uv run pytest tests/test_review_output_schema.py`).

## Phase 3: Reflection Pass (Verifier Engine)
- [x] **Task 6: Verifier models & prompt**
  - Add `VerificationDecision` and `VerificationOutput` models to `app/modules/reviews/application/reflection_pass.py`.
  - Add verifier system prompt `review/prompts/review.verifier.v1.md` and verifier prompt builder.
  - Add unit tests for verifier prompt construction and manifest creation.
- [x] **Task 7: Reflection Pass executor**
  - Implement `ReflectionPass.verify(...)` executing batch verification for candidates.
  - Validate verifier response: exactly one decision per candidate, valid `evidence_refs` and `requirement_refs`.
  - Handle empty candidate set as a no-op (0 calls).
  - Handle model errors, schema validation errors, and timeouts per spec.
  - Add unit tests covering `supported`, `contradicted`, and `insufficient_context` outcomes.

## Checkpoint 3: Reflection Pass
- [x] Unit tests pass for Reflection Pass (`uv run pytest tests/test_reflection_pass.py`).

## Phase 4: Integration & Verification
- [x] **Task 8: Pipeline integration**
  - Wire `ReflectionPass` into `ExecuteReviewRun`: candidate generation -> verification -> filtering only `supported` -> post-processing.
  - Add integration tests verifying contradicted/insufficient candidates are dropped before publication.
- [x] **Task 9: Fixed evaluation examples & negative injection suites**
  - Test on fixed positive examples: confirmed violations pass verifier and are published.
  - Test on fixed negative examples: hallucinated violations, inapplicable rules, and ungrounded claims are rejected.
  - Test prompt injection attempts in code, diff, and rules: verifier and reviewer remain secure.
- [x] **Task 10: Final polish, lint, format & typing**
  - Run `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy .`, and `uv run pytest`.
  - Ensure zero regressions across entire test suite.

## Phase 5: Review Round 1 Fixes (Alexander Khomutov / axyi #5547099992)
- [x] **Task 11: P1 security escaping & regex narrowing**
  - Narrow `_SERVICE_DEFAULTS_ID_RE` to `^[a-zA-Z0-9_\-]+$`.
  - Add entity escaping for quotes (`&quot;`, `&apos;`) in XML attributes via `_attribute`.
- [x] **Task 12: P2 evidence & requirement validation**
  - Validate `evidence_refs` (roles `diff`, `source_excerpt`, `ast_excerpt`) and `requirement_refs` (roles `policy_document`, `service_defaults`, strict `check_id`).
  - Enforce matching `requirement_refs` for rule-attributed findings.
- [x] **Task 13: P2 manifest fidelity & block_id**
  - Implement `shown_ranges: tuple[tuple[int, int], ...]` in `ManifestBlock`.
  - Explicit `block_id` on `<file>` tags in v2 snapshot and verifier prompts.
  - Retain `service-defaults` in snapshot and manifest.
- [x] **Task 14: P2 fail-closed guards & error catalog**
  - Check `is_ready_for_llm`, budget checks (`verify_pre_send_guard`), missing `reflection_pass` when snapshot present.
  - Register `verification_unavailable` (retryable), `verification_invalid_output` and `verification_context_overflow` (terminal).
- [x] **Task 15: P2 summary cleansing & sanitized logging**
  - Cleansing when all candidates rejected (neutral summary, zero hallucinated defects).
  - Sanitized logging (only IDs and statuses).
- [x] **Task 16: P2 prompt trust statements & post-processor**
  - Added trust paragraphs in `review.system.v2.md` and `review.verifier.v1.md`.
  - Preserve `ALLOWED_V2_RULE_NAMES` and `service-defaults:*` in `_repair_attribution`.
- [x] **Task 17: P2 documentation & test suites**
  - Updated `review/README.md`, `docs/TEST_PLAN.md`, `tests/test_reflection_evaluation.py`, `tests/test_execute_review.py`.

## Phase 6: Review Round 3 Fixes (Alexander Khomutov / axyi #5562633453)
- [x] **Task 18: P1 review.system.v3.md deferral (Option a)**
  - Deleted `review/prompts/review.system.v3.md` and `tests/fixtures/prompt_builder/sample.system.v3.txt`.
  - Reverted pinned system prompt in `policy_snapshot.py` and `tests/test_prompt_seeding.py` to `review.system.v2.md`.
  - Added `test_pinned_checksums_match_files_and_golden_fixtures` asserting byte-level SHA-256 for active prompts.
- [x] **Task 19: P2 Rebase & Limits Relocation**
  - Rebased on `origin/main` (`f8fe9c0`) and resolved `MAX_COMMIT_MESSAGES`/`MAX_COMMIT_MESSAGE_CHARS` import from `commit_prefix_limits.py`.
- [x] **Task 20: P2 Error Handling & Schema Strictness**
  - Enforced 1:1 decision count match with candidates in `reflection_pass.py`.
  - Sanitized error messages and logged payloads; masked raw input values in Pydantic `ValidationError`.
  - Cataloged `verification_budget_exceeded` and `verification_deadline_exceeded` in `run_failures.py`.
  - Translated `TimeoutError`/`llm_timeout` to `VerificationUnavailableError` and generic errors to `VerificationUnavailableError`.
- [x] **Task 21: P2 Attribution & Post-Processing**
  - Threaded `allowed_v2_rules` through `FindingPostProcessingInput`, `FindingsPostProcessor.process()`, and `RunRepository.get_post_processing_input()`.
  - Added regression test `test_findings_post_processor_v2_preserves_attribution_for_body_only_findings`.
- [x] **Task 22: P2 Manifest & Rendering Fidelity**
  - Rendered service defaults lines in XML numbered to match manifest `shown_ranges` `1..sum(checks)`.
  - Provided neutral `done_well` text for cleansed summaries.
  - Allowed legacy runs without snapshot to bypass reflection pass.
- [x] **Task 23: P2 Documentation Alignment**
  - Aligned `review/README.md`, `docs/TEST_PLAN.md`, `120-plan.md`, and `120-todo.md`.

