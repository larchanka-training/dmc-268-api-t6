# Task List: Markdown Rules and Reflection Pass (#120)

## Phase 1: Foundation (Domain Contracts & Policy Envelope)
- [x] **Task 1: Domain models for Policy Snapshot & Bindings**
  - Create `app/modules/reviews/application/policy_snapshot.py` with `PolicyDocument`, `PolicySnapshot`, `PromptBindings`, `ExecutionProfile`, and `CallManifest`.
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
