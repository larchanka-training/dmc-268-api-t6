# Implementation Plan: Markdown Rules and Reflection Pass (#120)

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
   - Unverified/contradicted candidates are dropped; #133 logs sanitized IDs/statuses. Durable free-text verifier reasons were not implemented (see the scope note above).
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

### Phase 6: Review Round 2 Fixes (Alexander Khomutov / axyi #5557688806)
- [x] **P2 Mutation & Coverage Gates**: Covered `comment_body` helper, XML escaping edge cases, and achieved 100% test coverage across review output and prompt builder.

### Phase 7: Review Round 3 Fixes (Alexander Khomutov / axyi #5562633453)
- [x] **P1 System Prompt Preservation (Option a)**: Removed `review.system.v3.md` and golden fixtures to defer prompt rollout until #130; strictly preserved and pinned byte-level SHA-256 for `review.system.v2.md`.
- [x] **P2 Rebase & Limits Relocation**: Rebased on `origin/main` (`f8fe9c0`) and imported `MAX_COMMIT_MESSAGES`/`MAX_COMMIT_MESSAGE_CHARS` from `commit_prefix_limits.py`.
- [x] **P2 Error Handling & Schema Strictness**: Enforced exact 1:1 decision counts with candidate IDs, sanitized error messages (truncating IDs to 64 chars and hiding raw input payloads in ValidationError), cataloged `verification_budget_exceeded` and `verification_deadline_exceeded`, and mapped timeout/unavailable/invalid errors.
- [x] **P2 Attribution & Post-Processing**: Added the `allowed_v2_rules` hand-off to `FindingsPostProcessor` and `RunRepository`. This made v2 attribution retention available to an explicit caller; the legacy repository supplied `None`, so #133 did not establish database attribution in the worker.
- [x] **P2 Manifest & Rendering Fidelity**: Numbered service-defaults lines in prompt matching manifest `shown_ranges`; neutral summary `done_well` text on cleansed summaries; legacy runs bypass reflection when `context.policy_snapshot is None`.
- [x] **P2 Test Coverage & Docs**: 100% statement and branch coverage on review output v2 and reflection evaluation; aligned `review/README.md`, `docs/TEST_PLAN.md`, and plans.

### Phase 8: Review Round 4 Fixes (Alexander Khomutov / axyi #5563263769)
- [x] **P2 Seeding Test Assertion**: Updated `test_prompt_seeding.py` to expect 5 inserted prompt versions and version 2 for active `review.system` after deleting reviewer prompt v3.
- [x] **P2 Finding Deserialization & Clean Error Wrapping**: Prevented double-wrapping of `InvalidReviewOutput` inside `execute_review.py` candidate deserialization loop.
- [x] **P2 Budget Error Details**: Populated `limit` and `actual` in `PolicyBudgetExceededError` and passed them to `PolicyError(PolicyErrorCode.PROMPT_BUDGET_EXCEEDED)`.
- [x] **P2 Clean Reflection Gate & Warning Log**: Cleaned up duplicate snapshot checks inside `_run_reflection_pass` and added warning log when legacy run is invoked with reflection pass.
- [x] **P2 Error Mapping & Canonical Exceptions**: Cleaned unreachable `TimeoutError` check in `reflection_pass.py` and mapped foreign exceptions with `verification_invalid_output` and `verification_unavailable` to canonical classes.
- [x] **P2 Requirement Refs & Service Defaults Validation**: Enforced required `check_id` on `service_defaults` requirement refs and strict matching of requirement ref blocks to finding `rule_name`.
- [x] **P2 Complete Test Coverage for Mutation Gates**: Added unit tests covering candidate XML attributes, verifier prompt `block_id` and service defaults, verifier token guard, `_split_policy_lines` strictly splitting on newline, `shown_line_count` excluding removed diff lines, `StoreReviewOutput` passing `allowed_v2_rules`, and `verification_*` error catalog in worker.

### Phase 9: Review Round 5 Fixes (Alexander Khomutov / axyi #5568780909)
- [x] **P2 Multi-source requirement_refs**: Relaxed single-source restriction to allow verifier decisions referencing multiple distinct policy blocks (`service-defaults` and `policy-rules`).
- [x] **P2 Error catalog**: Added `verification_unavailable` and transient failure reasons to worker retry catalog.
- [x] **P2 Rebase**: Rebased on `origin/main` (`9de20a5`).

### Phase 10: Review Round 6 Fixes (Alexander Khomutov / axyi #5572159225)
- [x] **P2 Block ID & Path Bounds**: Truncated slug in `file_block_id` to max 64 characters so `file-{slug}-{digest[:8]}` (max 78 chars) never exceeds `max_length=128`. Aligned `ReviewFindingV2.path` `max_length` with `MAX_PATH_BYTES` (1024). Added test for 200+ char path through manifest and `EvidenceRef`.
- [x] **P2 Schema Synchronization**: Removed `= None` default from `ReviewFindingV2.rule_name` to keep it strictly required matching JSON schema. Synchronized `maxLength` in `review-output.schema.v2.json` and `verifier-output.schema.v1.json`.
- [x] **P2 Service Defaults Slug Matching**: Added case-insensitive slug key to `service_defaults_check_lines` in `reflection_pass.py` for real rule names with spaces ("Naming Consistency"). Added branch coverage for secondary source validation.
- [x] **P2 Scope Boundary Confirmation**: Formalized boundary with tech lead: live LLM gateway wiring, verifier gateway adapter, and context-fitting manifest deferred to #130.

### Phase 11: Review Round 7 Fixes (Alexander Khomutov / axyi #5574663268)
- [x] **P2 Required check_id in RequirementRef**: Removed `= None` default from `RequirementRef.check_id`, making it strictly required nullable string per CONTEXT_AND_VERIFICATION_SPEC §7.2 and `verifier-output.schema.v1.json`.
- [x] **P2 Verifier Schema Model Match**: Added `test_verifier_schema_properties_match_pydantic_models` in `test_schemas_v2.py` asserting exact match of schema `required` fields against `is_required()` for all verifier models (`VerificationOutput`, `VerificationDecision`, `RequirementRef`, `EvidenceRef`).
- [x] **P3 Schema minLength**: Added `minLength: 1` to `check_id` in `verifier-output.schema.v1.json` and verified empty string rejection and null acceptance.
- [x] **P3 Service Defaults Key Ordering & Upper Bound**: Added exact-key precedence over lowercase `setdefault` to the historical slug implementation. Its test used a name equal to its slug and did not exercise case differences. Secondary source upper-bound and `path` 1024, `block_id` 128, `check_id` 128 boundary tests were also added. The slug implementation was later superseded by #230 (see above).




## Risks and Mitigations
| Risk | Impact | Mitigation |
|------|--------|------------|
| Prompt injection via crafted `.review/rules.md` | High | Strict XML escaping, isolated untrusted containers, explicit role boundaries, test suite with injection payloads. |
| Incompatible contracts with Engineer 3 | Medium | Follow exact specifications in `CONTEXT_AND_VERIFICATION_SPEC.md` §3.1 and `RULES_FORMAT_SPEC.md` §7.4. No DB migrations in #120. |
| Hallucinated or invalid verifier decisions | High | Strict schema validation: exactly 1 decision per candidate, evidence_refs verified against manifest, requirement_refs checked against policy lines. |
| Budget/token overflow during verifier pass | Medium | Conservative pre-send budget check; reject early with `verification_context_overflow` if envelope exceeds limits. |
