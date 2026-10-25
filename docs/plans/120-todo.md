# Task List: Markdown Rules and Reflection Pass (#120)

## Historical scope and present behavior

These phases record the work in #133, not a claim that its target pipeline is connected to
today's worker. Review-round references: [round 2](https://github.com/larchanka-training/dmc-268-api-t6/pull/133#pullrequestreview-5557688806),
[round 5](https://github.com/larchanka-training/dmc-268-api-t6/pull/133#pullrequestreview-5568780909).

At the #133 boundary the legacy repository path supplied `allowed_v2_rules=None`, so the
post-processor's ability to retain v2 attribution did not establish its persistence in the
worker. That legacy default still applies. The explicitly composed snapshot pipeline now
carries allowed names in `VerifiedReviewOutput`, so its publication can retain attribution.
The legacy `parse_review_output` entry point silently strips `evidence_refs` even from a
v2-looking input before v1 validation; `parse_review_output_v2` is a separate contract.

#133 recorded verifier IDs/statuses in logs, not durable structured free-text explanations.
The current explicit pipeline carries drop codes in `VerifiedReviewOutput`, and publication
can persist `Finding.drop_reason`. #290 added the verifier gateway adapter and `llm.verify`
trace hand-off to `StoreReviewOutput`; that trace excludes free-text `reason` and has a null
`manifest_id`. Both worker compositions still omit Reflection Pass, so their wiring remains
in #130. These later changes are not attributed to #133.

The old exact/lowercase slug lookup and secondary `any` branch were superseded by #230
stable service-default IDs. The historical slug test used a name equal to its own slug and
did not vary case; it did not prove exact-case precedence. #216 tests the current stable-ID
behavior separately.

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
  - Add `ReviewFindingV2` with `evidence_refs: list[EvidenceRef]` (keeping `ReviewFinding` v1 unchanged for backwards compatibility).
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
  - Cleansing on any rejected candidate, including partial rejection (neutral summary when all are rejected).
  - Sanitized logging (only IDs and statuses).
- [x] **Task 16: P2 prompt trust statements & post-processor**
  - Added trust statement in `review.verifier.v1.md`.
  - Preserve `ALLOWED_V2_RULE_NAMES` and `service-defaults:*` in `_repair_attribution`.
- [x] **Task 17: P2 documentation & test suites**
  - Updated `review/README.md`, `docs/TEST_PLAN.md`, `tests/test_reflection_evaluation.py`, `tests/test_execute_review.py`.

## Phase 6: Review Round 2 Fixes (Alexander Khomutov / axyi #5557688806)
- [x] **Task 17b: Mutation & Coverage Gates**
  - Covered `comment_body` helper, XML escaping edge cases, and achieved 100% test coverage across review output and prompt builder.

## Phase 7: Review Round 3 Fixes (Alexander Khomutov / axyi #5562633453)
- [x] **Task 18: P1 reviewer prompt deferral (Option a)**
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
  - Threaded `allowed_v2_rules` through `FindingPostProcessingInput`, `FindingsPostProcessor.process()`, and `RunRepository.get_post_processing_input()` as an explicit-caller capability. The legacy repository supplied `None`; this did not establish v2 database attribution in the #133 worker.
  - Added regression test `test_findings_post_processor_v2_preserves_attribution_for_body_only_findings`.
- [x] **Task 22: P2 Manifest & Rendering Fidelity**
  - Rendered service defaults lines in XML numbered to match manifest `shown_ranges` `1..sum(checks)`.
  - Provided neutral `done_well` text for cleansed summaries.
  - Allowed legacy runs without snapshot to bypass reflection pass.
- [x] **Task 23: P2 Documentation Alignment**
  - Aligned `review/README.md`, `docs/TEST_PLAN.md`, `120-plan.md`, and `120-todo.md`.

## Phase 8: Review Round 4 Fixes (Alexander Khomutov / axyi #5563263769)
- [x] **Task 24: P2 Seeding Test Assertion**
  - Updated `test_prompt_seeding.py` to expect 5 inserted prompt versions and version 2 for active `review.system` after deleting reviewer prompt v3.
- [x] **Task 25: P2 Clean Error Wrapping & Finding Parsing**
  - Prevented double-wrapping of `InvalidReviewOutput` in `execute_review.py`.
  - Cleaned up redundant snapshot checks in `_run_reflection_pass` and added warning log for legacy runs with reflection pass.
  - Added `limit` and `actual` attributes to `PolicyBudgetExceededError` and passed to `PolicyError`.
- [x] **Task 26: P2 Error Mapping & Requirement Refs Validation**
  - Cleaned unreachable `TimeoutError` check in `reflection_pass.py` and mapped foreign exceptions with `verification_invalid_output` and `verification_unavailable` to canonical classes.
  - Enforced required `check_id` on `service_defaults` and strict matching of requirement ref blocks to candidate finding `rule_name`.
- [x] **Task 27: P2 Complete Test Coverage for Mutation Gates**
  - Added unit tests for candidate XML attributes, verifier prompt `block_id` and service defaults, verifier token guard, `_split_policy_lines`, `shown_line_count` excluding removed lines, `StoreReviewOutput`, and `verification_*` error catalog in worker.

## Phase 9: Review Round 5 Fixes (Alexander Khomutov / axyi #5568780909)
- [x] **Task 28: Multi-source requirement_refs validation**
  - Allowed multiple `requirement_refs` in verifier decisions referencing distinct policy blocks (e.g. both `service-defaults` and `policy-rules`).
  - Added unit tests for multi-source requirement refs and strict validation of each ref against its block.
- [x] **Task 29: Error catalog & worker mapping**
  - Mapped `verification_unavailable` and other retryable failures in `_TRANSIENT_FAILURE_REASONS`.
- [x] **Task 30: Rebase onto main**
  - Rebased branch cleanly on `origin/main` (`9de20a5`).

## Phase 10: Review Round 6 Fixes (Alexander Khomutov / axyi #5572159225)
- [x] **Task 31: Block ID & Path length limits**
  - Capped slug in `file_block_id` to max 64 characters (`re.sub(...).strip("-")[:64].rstrip("-")`), guaranteeing `file-{slug}-{digest[:8]}` is at most 78 chars and comfortably below 128.
  - Aligned `ReviewFindingV2.path` `max_length` with `MAX_PATH_BYTES` (1024, was 512).
  - Added unit test with 200+ character path traversing prompt builder, call manifest, and `EvidenceRef`.
- [x] **Task 32: Schema synchronization & required rule_name**
  - Removed `= None` default from `ReviewFindingV2.rule_name`, keeping it strictly required per JSON schema contract.
  - Added `maxLength` properties to `review-output.schema.v2.json` and `verifier-output.schema.v1.json` (`path`: 1024, `block_id`/`check_id`/`rule_name`: 128, `candidate_id`: 64).
  - Updated schema test in `test_schemas_v2.py` to assert against `f.is_required()`.
- [x] **Task 33: Case-insensitive slug matching & secondary source validation**
  - Added lowercase slug key to `service_defaults_check_lines` in `reflection_pass.py` for real rule names with spaces ("Naming Consistency" -> "naming-consistency").
  - Added tests for real rule name slug matching and secondary source check_id and line range validation branches.
- [x] **Task 34: Scope boundary alignment with #130**
  - Confirmed deferral of verifier gateway adapter, live worker wiring, and context-fitting manifest to #130 per tech lead directive.

## Phase 11: Review Round 7 Fixes (Alexander Khomutov / axyi #5574663268)
- [x] **Task 35: Required check_id in RequirementRef**
  - Removed `= None` default from `RequirementRef.check_id`, enforcing required nullable string per CONTEXT_AND_VERIFICATION_SPEC §7.2 and schema.
  - Updated tests in `test_reflection_evaluation.py` and `test_review_output_v2.py` to supply `check_id: None`.
- [x] **Task 36: Verifier schema property and required validation**
  - Added `test_verifier_schema_properties_match_pydantic_models` in `test_schemas_v2.py` asserting schema `required` matches `is_required()` on `VerificationOutput`, `VerificationDecision`, `RequirementRef`, and `EvidenceRef`.
  - Added `minLength: 1` to `check_id` in `verifier-output.schema.v1.json` and tested rejection of empty string and acceptance of null.
- [x] **Task 37: Mutation & Boundary Tests**
  - Added boundary tests for `path` 1024, `block_id` 128, and `check_id` 128 in `test_review_output_v2.py`.
  - Added a historical slug test whose rule name equalled its slug; it did not vary case or prove exact-case precedence. Added secondary source upper-bound validation in `test_reflection_pass.py`. #230 later superseded that slug lookup with stable IDs (see above).
