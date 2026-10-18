# Implementation Plan: Markdown Rules and Reflection Pass (#120)

## Overview
Expand the review LLM pipeline to accept user-defined Markdown policy documents (`.review/rules.md` and root `AGENTS.md`) from an immutable policy snapshot, protect the prompt against prompt injection via an isolated and structured envelope, establish the v2 attribution contract (`evidence_refs` and `requirement_refs`), and implement the **Reflection Pass** (a mandatory secondary LLM verification pass) to filter hallucinations and ungrounded candidate findings before publication.

## Architecture Decisions
1. **Contract Boundary with Engineer 3**:
   - Engineer 3 owns VCS reading, parsing raw bytes, `.review/ignore`, AST context, and the `run_policy_snapshots` database table and all its migrations.
   - Engineer 4 (#120) does NOT write database migrations for `run_policy_snapshots` and does NOT modify durable snapshots after creation.
   - The domain contracts for `PolicySnapshot`, `PolicyDocument`, `PromptBindings`, and `ExecutionProfile` were established by Engineer 3 in #134. Engineer 4 (#120) consumes them, defines `CallManifest` and `ManifestBlock`, and implements the rendering, pre-send guards, attribution validation, and Reflection Pass.
2. **Untrusted Markdown Envelope & Injection Defense**:
   - The system prompt belongs strictly to service instructions and few-shots. No untrusted Markdown, PR metadata, code, or findings are placed in system/developer messages.
   - User message partitions data into typed, bounded blocks (`source_role` and `source_kind`) with backend-generated `block_id`.
   - Content escaping: XML entity escaping (`&`, `<`, `>`, quotes) prevents tag breakout (`</repo_rules>` remains literal text).
   - System instruction priority: Service defaults and safety instructions take strict precedence over user-provided rules.
   - Policy budget guard: Rendered policy documents must not exceed 12,000 tokens. Total budget capped at 60,000 (fast) / 150,000 (deep) tokens.
3. **Attribution Contract v2**:
   - Reviewer returns `rule_name` (`.review/rules.md`, `AGENTS.md`, `service-defaults:<ID>`, or `null`) and 1–8 `evidence_refs` referencing shown code/diff blocks in that call's manifest.
   - Retirement of the legacy string prefix `"According to custom instructions in ..."` in v2.
4. **Reflection Pass (Verifier)**:
   - Evaluates all non-empty candidates in a single batch call.
   - For each candidate: returns `supported`, `contradicted`, or `insufficient_context` with 1–600 char reason, `evidence_refs` (1–8 for supported/contradicted) and `requirement_refs` (0–8, referencing shown policy blocks or service defaults).
   - Only `supported` candidates advance to post-processing and publication.
   - Unverified/contradicted candidates are dropped with structured reason recorded for run diagnostics.
   - Zero candidates: Verifier is skipped (no-op, 0 calls).
   - Verifier errors (timeout, invalid schema): follow failure policy (`verification_invalid_output`, etc.), never silently publish unverified candidates.

## Tasks & Phases

### Phase 1: Domain Contracts & Policy Snapshot Envelope (Foundation)
- [x] **Task 1**: Domain models for Policy Snapshot, Prompt Bindings, and Execution Profile (consumed from #134), `CallManifest` and `ManifestBlock` (`policy_snapshot.py`).
- [x] **Task 2**: Envelope rendering, XML escaping, prompt injection defenses, and call manifest generation in `PromptBuilder`.
- [x] **Task 3**: Token counter / budget pre-send guards (policy budget ≤ 12,000 tokens, total input budget).

### Phase 2: Attribution v2 & Schema Contracts
- [x] **Task 4**: `ReviewOutput v2` schema, `EvidenceRef`, and attribution validator (validating `rule_name`, `evidence_refs` against call manifest).
- [x] **Task 5**: JSON Schema definitions (`review-output.schema.v2.json`, `verifier-output.schema.v1.json`).

### Phase 3: Reflection Pass (Verifier Engine)
- [x] **Task 6**: Verifier data models (`VerificationDecision`, `VerificationOutput`), verifier system prompt (`review.verifier.v1.md`), and verifier prompt builder.
- [x] **Task 7**: Reflection Pass executor (`ReflectionPass`), handling model invocation, schema validation, decision resolution, and error classification.

### Phase 4: Pipeline Integration & Verification
- [x] **Task 8**: Wire Reflection Pass into `ExecuteReviewRun`, filtering candidates so only `supported` findings proceed to publication.
- [x] **Task 9**: Comprehensive test suite (snapshot intake, prompt injection defenses, fixed positive/negative review examples, pipeline isolation).
- [x] **Task 10**: Linting, formatting, type checking, and documentation updates.

### Phase 5: Review Round 1 Fixes (Alexander Khomutov / axyi #5547099992)
- [x] **P1 Security**: Narrow service-defaults ID regex to `^[a-zA-Z0-9_\-]+$` and add XML attribute escaping (`&quot;`, `&apos;`).
- [x] **P2 Attribution**: Full validation of `evidence_refs` and `requirement_refs`; manifest `shown_ranges` and explicit `block_id` on `<file>`.
- [x] **P2 Guards & Fail-Closed**: `is_ready_for_llm`, budget checks, `reflection_pass` presence; error codes catalog.
- [x] **P2 Cleansing & Logging**: Summary cleansing on rejected candidates; sanitized ID/status logging.
- [x] **P2 Documentation**: Updated `review/README.md`, `docs/TEST_PLAN.md`, plans.

### Phase 6: Review Round 3 Fixes (Alexander Khomutov / axyi #5562633453)
- [x] **P1 System Prompt Preservation (Option a)**: Removed `review.system.v3.md` and golden fixtures to defer prompt rollout until #130; strictly preserved and pinned byte-level SHA-256 for `review.system.v2.md`.
- [x] **P2 Rebase & Limits Relocation**: Rebased on `origin/main` (`f8fe9c0`) and imported `MAX_COMMIT_MESSAGES`/`MAX_COMMIT_MESSAGE_CHARS` from `commit_prefix_limits.py`.
- [x] **P2 Error Handling & Schema Strictness**: Enforced exact 1:1 decision counts with candidate IDs, sanitized error messages (truncating IDs to 64 chars and hiding raw input payloads in ValidationError), cataloged `verification_budget_exceeded` and `verification_deadline_exceeded`, and mapped timeout/unavailable/invalid errors.
- [x] **P2 Attribution & Post-Processing**: Passed `allowed_v2_rules` through `FindingsPostProcessor` and `RunRepository` so v2 findings (`.review/rules.md`, `AGENTS.md`) retain attribution in database and during publication.
- [x] **P2 Manifest & Rendering Fidelity**: Numbered service-defaults lines in prompt matching manifest `shown_ranges`; neutral summary `done_well` text on cleansed summaries; legacy runs bypass reflection when `context.policy_snapshot is None`.
- [x] **P2 Test Coverage & Docs**: 100% statement and branch coverage on review output v2 and reflection evaluation; aligned `review/README.md`, `docs/TEST_PLAN.md`, and plans.

### Phase 7: Review Round 4 Fixes (Alexander Khomutov / axyi #5563263769)
- [x] **P2 Seeding Test Assertion**: Updated `test_prompt_seeding.py` to expect 5 inserted prompt versions and version 2 for active `review.system` after deleting reviewer prompt v3.
- [x] **P2 Finding Deserialization & Clean Error Wrapping**: Prevented double-wrapping of `InvalidReviewOutput` inside `execute_review.py` candidate deserialization loop.
- [x] **P2 Budget Error Details**: Populated `limit` and `actual` in `PolicyBudgetExceededError` and passed them to `PolicyError(PolicyErrorCode.PROMPT_BUDGET_EXCEEDED)`.
- [x] **P2 Clean Reflection Gate & Warning Log**: Cleaned up duplicate snapshot checks inside `_run_reflection_pass` and added warning log when legacy run is invoked with reflection pass.
- [x] **P2 Error Mapping & Canonical Exceptions**: Cleaned unreachable `TimeoutError` check in `reflection_pass.py` and mapped foreign exceptions with `verification_invalid_output` and `verification_unavailable` to canonical classes.
- [x] **P2 Requirement Refs & Service Defaults Validation**: Enforced required `check_id` on `service_defaults` requirement refs and strict matching of requirement ref blocks to finding `rule_name`.
- [x] **P2 Complete Test Coverage for Mutation Gates**: Added unit tests covering candidate XML attributes, verifier prompt `block_id` and service defaults, verifier token guard, `_split_policy_lines` strictly splitting on newline, `shown_line_count` excluding removed diff lines, `StoreReviewOutput` passing `allowed_v2_rules`, and `verification_*` error catalog in worker.



## Risks and Mitigations
| Risk | Impact | Mitigation |
|------|--------|------------|
| Prompt injection via crafted `.review/rules.md` | High | Strict XML escaping, isolated untrusted containers, explicit role boundaries, test suite with injection payloads. |
| Incompatible contracts with Engineer 3 | Medium | Follow exact specifications in `CONTEXT_AND_VERIFICATION_SPEC.md` §3.1 and `RULES_FORMAT_SPEC.md` §7.4. No DB migrations in #120. |
| Hallucinated or invalid verifier decisions | High | Strict schema validation: exactly 1 decision per candidate, evidence_refs verified against manifest, requirement_refs checked against policy lines. |
| Budget/token overflow during verifier pass | Medium | Conservative pre-send budget check; reject early with `verification_context_overflow` if envelope exceeds limits. |
