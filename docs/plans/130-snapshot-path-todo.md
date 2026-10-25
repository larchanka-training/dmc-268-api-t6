# Task list: switching on the policy-snapshot review path (#130, PR F)

The design is in [130-snapshot-path-plan.md](130-snapshot-path-plan.md); this list adds no scope.
Base: PR #324 (C) head `0aa29d6b` on `main` `9243fd67` (planned on `703c05ec` over `d08e6c96`);
Alembic head `20261024_0039`.

- Each task is one Conventional commit whose subject ends with `(#130)`.
- Test first: write the named test, watch it fail, then write the code. A refactor keeps the existing
  tests green.
- **Every F1 task keeps `tests/test_snapshot_path_live_pin.py` (F1-1) green.**
- ⚑ marks a task with more than 10 edits.
- Every brief says: never read any `.env`. The repository only flushes; the UoW commits
  (`.agents/rules/backend.md` §3.3).

Gates for every task: the four commands in the plan, with DB and vhost `imp130f`.

## PR F1: `feat/130-snapshot-path` (dormant)

### F1-0: Plan documents
- **Commit:** `docs(plans): plan the snapshot path switch (#130)`
- **Files:** these two files, staged with `git add -f`.
- **Done when:**
  - [x] only these two files are committed;
  - [x] TL-1 to TL-13 are answered;
  - [x] the coordination comments are posted.

### F1-1: Legacy characterization pin
- **Commit:** `test(reviews): pin the legacy review path before the switch (#130)`
- **Files:** new `tests/test_snapshot_path_live_pin.py` (Postgres; reuse the fixtures of
  `tests/test_run_prompt_pins_postgres.py`).
- **Test:** a v3-bound run with D pins and a ready snapshot goes through `process_review_run` with the real
  stage and a recording transport. Assert:
  - the exact system and user bytes;
  - the `llm.call` count and request keys;
  - the run_action tool sequence;
  - `reserved_calls == 0`, `bounded_repair` off;
  - the conventions cache key `(repo, agents_md_sha, prompt_version_id, policy_key)`.
- **Done when:**
  - [x] green on `703c05ec`;
  - [x] fix round (batch-1 review): the responses of the non-call steps and a pre-0039 v3 run
    (NULL conventions and verifier pins) are pinned too;
  - [x] the `context_manifests` row count (0) is added in F1-6.

### F1-2: Path selector ⚑
- **Commit:** `feat(reviews): choose the review path by the pinned prompt version (#130)`
- **Files:**
  - new `app/modules/reviews/application/review_path.py`;
  - `infrastructure/llm/answers.py` (`:47-48` imports the set);
  - `application/review_context_stage.py` (`begin` `:400-414`: refusal right after the snapshot, before conventions);
  - `application/process_run.py` (`:250-266`: refusal before conventions when there is no stage) and
    `infrastructure/run_repository.py` (`get_run_conventions_input` returns the binding);
  - `application/execute_review.py` (`ReviewPromptInput` gets `review_binding`: id, version, provider; re-check);
  - `infrastructure/review_prompt_repository.py` (select `PromptVersion.version` and the origin provider);
  - new `tests/test_review_path.py`; `tests/test_review_prompt_pins_postgres.py`;
  - `tests/test_sqlalchemy_run_repository.py`: one fake row of the prompt read gains the three columns
    (a file of #283, #227 and #220, outside the posted coordination).
- **Tests:**
  - v3 → legacy, even with a ready snapshot;
  - v4 + ready → snapshot;
  - v4 → `policy_snapshot_integrity_error` with 0 `llm.call` (no conventions call either) when: no context stage, NULL pin (D-Q2/Q4),
    not ready, pins mismatch;
  - D-1 (a): a stored review-only snapshot with all three run pins → terminal integrity error before any
    call. Under TL-10 (b): legacy instead;
  - fix round (batch-1 review): a v4 run of an unserved provider (GitLab, other name, no origin) → the
    same terminal error before any model call; only a `review.system` row picks the path.
  - fix round (batch-2 review): that refusal comes before the diff in both compositions: with a stage
    (it keys on `_STAGE_PROVIDER`) `prepare` checks the run's provider before `begin`, so
    `policy.load` does not run (re-review of `d2d62794`); a missing active conventions prompt fails
    before `policy.load` and the diff.
- **Done when:** [x] F1-1 green; [x] `answers.py` has one source for the v2-contract versions.

### F1-3: Lock 1 behind the selector (R1 review side)
- **Commit:** `feat(reviews): pass the attempt snapshot to snapshot-path reviews (#130)`
- **Files:**
  - `application/review_context_stage.py`: `snapshot_for_llm` (landed in F1-2) and `payload` properties;
    `run()` keeps the payload, `:514-536`;
  - `application/execute_review.py`: `ReviewContextFilter` protocol and context assembly `:206-216`. The
    stage's snapshot fills an empty `ReviewPromptInput.policy_snapshot` before the F1-2 re-check, so the
    re-check still drops it for a legacy binding and refuses one that pins another review prompt;
  - `tests/test_execute_review.py`; `tests/test_review_context_stage.py` (the `Withheld` fake gains
    `snapshot_for_llm`; payload test), both covered by the coordination addenda.
- **Tests:**
  - a fixture `review.system` v4 row gives the user message the snapshot's rules.md and AGENTS.md blocks;
  - `ReviewPromptInput.agents_md` is ignored on that path;
  - without a `ReflectionPass` it still raises (`:219-223`; R14 comes in F1-7).
- **Done when:** [x] F1-1 green.

### F1-4: R2 render, manifest and fit; P6-4 ⚑
- **Commit:** `feat(reviews): render the L2-L4 context of the payload (#130)`
- **Files:**
  - `application/prompt_builder.py` (`ReviewContext` `:166-177`, `render_input` `:233-262`, `build_manifest` `:351`);
  - `application/prompt_budget.py` (`fit_review_context` `:440`, `prompt_tokens` `:434`);
  - `application/execute_review.py`;
  - new `tests/test_review_context_blocks.py`.
- **Tests:**
  - L2–L4 excerpts carry block_id, path, revision and side;
  - manifest roles are `source_excerpt` and `ast_excerpt`, with `shown_ranges`;
  - fit drops L3, then L2, then L4 (the reverse of the CVS §4.1 selection order), before changed files;
  - `retrieved` is not rendered while `RETRIEVED_PROMPT_VERSIONS` is empty;
  - an ignored count appears as a counter, never as a path;
  - fix round (batch-2 review): an excerpt whose text does not match its source ranges fails with the
    terminal `policy_context_violation` before any call; on the snapshot path the PR meta carries the
    head of the attempt's diff map (stage `head_sha`), so a push after the diff map does not fail the
    guard; the L2–L4 excerpts go through the real `GatewayReviewModel`.
- **Done when:** [x] F1-1 green; [ ] IMP#144 is told PR3a can start (with F1-5, F1-6, F1-13).

### F1-5: R10 review, T-a, E-9 ⚑
- **Commit:** `refactor(reviews): fit, manifest and guard the review once (#130)`
- **Files:**
  - `infrastructure/llm/models.py` (`review_with_gateway` `:181-233`, `ReviewReply`);
  - `infrastructure/llm/gateway.py` (`GatewayResult.accepted_manifest` `:162-170`, `max_prompt_tokens(engine, route)` `:244-247`);
  - `application/execute_review.py` (drop `:241-273` on the snapshot path; use the reply);
  - `application/prompt_budget.py` (`send_budget_for` `:147-175`, no `os.environ`);
  - tests: `tests/test_execute_review.py`, `tests/test_llm_models.py` (or the existing adapter test file).
- **Tests:**
  - one `fit_review_context` call per review on the snapshot path;
  - a repair-accepted answer is attributed against the repair manifest, a fallback answer against the
    fallback manifest;
  - `send_budget_for` ignores `LLM_CONTEXT_WINDOW`;
  - a `Mapping` reply still works; on the snapshot path it logs a warning (fix round of batch 2).
- **As built:** the tests are in new `tests/test_gateway_review_model.py`. The six fit and guard cases of
  `tests/test_execute_review.py` moved there (TL decision, addendum 6400817148 on #303). The
  proxies `CheckpointedProvider.draft_review` (`handle_review_run.py`) and
  `AttemptReviewProvider.draft_review` (`worker.py`) widen their return type to `ReviewReply`.
  `GatewayReviewModel.max_prompt_tokens()` is gone; its only caller was the fit removed from
  `execute_review`. `LlmGateway.max_prompt_tokens(engine, route)` moves to F1-7, its only consumer
  (the verifier window).
- **Done when:** [x] F1-1 green; [x] mypy is clean on the fakes.

### F1-6: Manifest persistence, `context.manifest`, E-3 ⚑
- **Commit:** `feat(reviews): store the manifest of every snapshot-path call (#130)`
- **Files:**
  - new `alembic/versions/20261025_0040_context_manifests.py` (down `20261024_0039`);
  - `infrastructure/models.py` (`ContextManifest`); `application/policy_snapshot.py` (`ManifestBlock` gets
    `tokens`, `repository_id`, `truncated`);
  - new port in `application/llm.py` (`ManifestStore`) and `LlmCallRecord` fields;
  - new `infrastructure/context_manifest_store.py`;
  - `infrastructure/llm/gateway.py` (write before each send, `:270-383`);
  - worker wiring (`app/bootstrap/llm_gateway.py` `build_gateway`);
  - new `tests/test_context_manifests_postgres.py`, `tests/test_context_manifests_migration.py`;
  - `contracts/openapi.yaml` and `tests/test_openapi_contract.py` (`context.manifest` in the catalog);
    `docs/PIPELINE_SPEC.md` (`llm.call` and `context.manifest` rows);
  - the Alembic head pinned in `tests/test_app_reinstall_schema_postgres.py` and
    `tests/test_code_index_store_postgres.py` moves to `20261025_0040`;
  - E-5 (TL-5): closed without an SSE change, `run_action_records.py` stays as it is.
- **Tests:**
  - one row and one `context.manifest` action per base, repair or fallback call; a retry reuses the row;
  - `llm.call` request carries `call_id`, `stage`, `policy_snapshot_id`, `manifest_id`;
  - a failed write → no provider call;
  - a `retrieved_excerpt` block (LEFT, `diff_old_sha`, two ranges, `repository_id`) round-trips;
  - upgrade → downgrade → upgrade;
  - legacy run → 0 rows (in F1-1);
  - fix round (batch-2 review): `policy_snapshot_id` NOT NULL with FK to `run_policy_snapshots`, the
    CVS §5.1 columns (`rendered_messages_sha256`, `prompt_checksum`, `model_profile_version`) in
    0040 in place; the slot is claimed before the write (two concurrent generations stay within four
    calls); the step precedes the row (lock order); a literal digest; uncounted blocks stay out of
    `tokens_by_role`.
- **Done when:** [x] one Alembic head; [x] F1-1 green.

### F1-7: R14, verifier window, `llm.verify.manifest_id`, E-2 ⚑
- **Commit:** `feat(worker): run the reflection pass on snapshot-path reviews (#130)`
- **Decided by the tech lead on 2026-10-25:** the verifier receives the L2–L4 excerpt blocks the
  candidates cite as evidence (CVS §7.1), within the verifier-route window; an uncited excerpt is
  never sent, and one that does not fit is left out (the verifier answers `insufficient_context`).
- **Files:**
  - `app/worker.py` (`attempt_provider_factory`, `AttemptReviewProvider.reflection_pass`, the queue
    pipeline, `process_review_run`);
  - `application/reflection_pass.py` (`VerifierWindow`, the window at the fit, the guard and
    `ReflectionPass.max_prompt_tokens`; the cited excerpts; manifest tokens; `RunFailure` and
    `RunCancelled` pass through; `VerifierReply`/`VerificationRecord.manifest_id`);
  - `infrastructure/llm/models.py` (`GatewayVerifierModel.max_prompt_tokens`, `redact_answer`);
  - `infrastructure/llm/gateway.py` (`max_prompt_tokens(engine, route)`, `StructuredTask.redact_answer`,
    `GatewayResult.accepted_manifest_id`); `infrastructure/llm/answers.py`
    (`verifier_answer_without_reasons`);
  - `application/execute_review.py` (warning removed, the window in the mandatory verifier budget,
    `llm.verify.manifest_id`); `docs/PIPELINE_SPEC.md` (`llm.call` response row);
  - tests: new `tests/test_worker_reflection_pass.py`, `tests/test_reflection_pass_snapshot_path.py`,
    `tests/test_gateway_verifier_snapshot_path.py`; `tests/test_llm_verify_trace.py`;
    `tests/test_snapshot_path_live_pin.py` (the worker's pass is passed); fakes only in
    `tests/test_execute_review.py` and `tests/test_worker.py`, plus the inverted warning case.
- **Tests:**
  - the composition passes a `ReflectionPass` (factory, queue pipeline, `process_review_run`);
  - a v4 run makes a verification `llm.call` and `llm.verify` with a non-null `manifest_id` (the
    accepted repair call's row after a repair);
  - the verifier budget equals the verifier route window at the fit, the guard and the
    mandatory check before the review call;
  - no `reason` in the stored verification response; non-JSON text is withheld whole;
  - a finding backed only by an excerpt is verified with the excerpt in the prompt and the
    manifest; an uncited excerpt is not sent; the candidate's diff lines come first, then the
    cited excerpts, then the other diff lines;
  - every verifier manifest block carries its tokens;
  - a verifier manifest-write failure stays `internal_error`; a cancellation stays one;
  - a legacy run logs no warning, and the F1-1 pin passes the worker's pass;
  - fix round (F1-7 review): a stored verifier response keeps only the allow-list (list-form
    content, reasoning, tool calls, refusal, logprobs, `text`, every choice, invalid answers
    withheld); the own lines of every candidate come before any file is topped up; a fitted prompt
    never trips the pre-send guard (600-window sweep); a cited excerpt that does not fit leaves
    room for the next; the pass reads its prompt file when it first verifies.
- **Done when:** [x] F1-1 green.

### F1-8: R12 and #247 p3
- **Commit:** `fix(reviews): repair attribution before the verifier (#130)`
- **Files:** `application/execute_review.py` (`:396-415`, `:449-461`); `application/findings_post_processor.py`
  (`:259-272` reused); `tests/test_execute_review.py`.
- **Tests:** 0 candidates → `allowed_v2_rules is None`; the verifier receives the repaired `rule_name`.
- **Done when:** [ ] F1-1 green.

### F1-9: E-1, E-7 (time), E-12
- **Commit:** `feat(llm): reserve slots and time for later stages on the snapshot path (#130)`
- **Files:** `infrastructure/llm/models.py` (task construction `:105-124`, `:221-232`); `infrastructure/llm/gateway.py`
  (time admission near `:259`); `tests/test_llm_gateway*.py`.
- **Tests:**
  - snapshot review `reserved_calls=1`, conventions `2` (per TL-2);
  - the cache-miss chain is C1 → R1 → fallback → V1;
  - bounded repair is on;
  - the review is refused (`verification_deadline_exceeded`) when the time left is under its call + 90 s + 10 s;
  - legacy tasks are unchanged.
- **Done when:** [ ] F1-1 green.

### F1-10: R18, R1 conventions, T-b, T-c ⚑
- **Commit:** `feat(reviews): give conventions the pinned prompt and the snapshot (#130)`
- **Files:**
  - `infrastructure/run_repository.py` (`get_run_conventions_input` `:457-487`);
  - `application/process_run.py` (`:250-266`);
  - `application/conventions.py` (`:341`, `:378-384`);
  - `application/conventions_prompt.py` (`:49-54`);
  - `infrastructure/llm/models.py` (`:91-125`);
  - `tests/test_repo_conventions.py`.
- **Tests:**
  - the snapshot path reads `Run.conventions_prompt_version_id`;
  - AGENTS.md comes from the snapshot, with no `fetch_agents_md` call;
  - one manifest and guard, after the fit;
  - legacy keeps the active row and `fetch_agents_md`.
- **Done when:** [ ] F1-1 green.

### F1-11: R5
- **Commit:** `feat(reviews): key snapshot-path conventions by repo-conventions-v2 (#130)`
- **Files:** `application/conventions.py`; `application/review_context_stage.py` (`policy_key` `:362-381`);
  `tests/test_repo_conventions.py`.
- **Tests:** the v2 digest (TL-7 components) misses on a legacy row and hits on a repeat; the same policy with a
  newly tainted rename or copy path misses; a v3 run keeps the old key.
- **Done when:** [ ] F1-1 green.

### F1-12: R7
- **Commit:** `feat(reviews): check the policy budget at policy load (#130)`
- **Files:** `application/review_context_stage.py` (`begin` `:400-414`); `application/prompt_budget.py`
  (`verify_policy_budget` `:204`); `tests/test_review_context_stage.py`.
- **Tests:** v4 with more than 12k policy tokens → `policy_prompt_budget_exceeded` at `policy.load`, 0 `llm.call`;
  the same docs on v3 proceed.
- **Done when:** [ ] F1-1 green.

### F1-13: R19 and T-d
- **Commit:** `feat(reviews): count the context stage with the pinned counter (#130)`
- **Files:** `application/review_context_stage.py` (`:73-77`, `run`); `app/worker.py` (`:544-572` builder takes a
  counter); `tests/test_review_context_stage.py`, `tests/test_gateway_counter_parity.py`.
- **Tests:** v4 → `token_counter_for(counter/v2)` in the build and in the trace tokens; v3 → 4-char numbers unchanged.
- **Done when:** [ ] F1-1 green; [ ] IMP#144 is signalled.

### F1-14: Dormant end-to-end
- **Commit:** `test(reviews): run a v4-bound review through the worker composition (#130)`
- **Files:** new `tests/test_snapshot_path_e2e_postgres.py`.
- **Test:** a fixture v4 row runs through `compose_worker_process`. Expect `policy.load`, then
  `vcs.fetch_diff`, `context.build`, the conventions `context.manifest` and `llm.call`, the review
  `context.manifest` and `llm.call`, the verification `context.manifest` and `llm.call`, `llm.verify`,
  then `llm.review_output` and `review.postprocess`. A retry reuses the pins.
- **Done when:**
  - [ ] all gates pass;
  - [ ] the PR body has the D-2 order, the rollback floor and the D-1 staging query.

## PR F2: `feat/130-snapshot-switch` (on F1; merge only after F1 is deployed)

### F2-1: The switch (R8)
- **Commit:** `feat(prompts): add review.system.v4 and switch the snapshot path on (#130)`
- **Files:**
  - new `review/prompts/review.system.v4.md` (snapshot envelope, `evidence_refs`, no prefix);
  - `application/policy_snapshot.py` (`PINNED_PROMPT_CHECKSUMS` `:229-233`);
  - `tests/test_review_prompt_pins_postgres.py:160`; `tests/test_prompt_seeding.py`;
  - a v4 golden envelope test.
- **Tests:**
  - after the seed, v4 is active and new runs pin it;
  - a v3 run created before the seed stays legacy;
  - F1-14 passes with the real file.
- **Done when:**
  - [ ] gates pass;
  - [ ] IMP#144 is signalled for PR3b;
  - [ ] the PR body names the TL-8 query result.

## PR F3: `fix/168-part6-trace` (Refs #168; after #304)

- **F3-1** `fix(reviews): record context.build once per attempt (#130)`.
  - Drop the step in `TracedReviewPromptRepository` (`run_trace.py:131-174`). The stage's `context.build`
    writes `context_payloads`.
  - Edit `overview.json:560`, `:618` and `MONITORING.md:61`.
  - Test: one `context.build` per attempt, and the payload summary is filtered (P6-2).
- **F3-2** `fix(reviews): escape diagnostic paths in the trace (#130)`. File: `review_context_stage.py:184`
  (CONTEXT §9.1). Test: control characters are escaped.
- **F3-3** `fix(reviews): correct the ignore limit check-run text (#130)`. File: `check_runs.py:55`.
- **F3-4** `refactor(worker): drop the unused policy source locators (#130)`. File: `worker.py:574`, `:588`,
  `:621`, `:626`.

## PR F4: `chore/130-verifier-env`

- **F4-1** `chore(deploy): pass the verifier model variables to the worker (#130)`.
  - Files: `docker-compose.yml:48-64`, `deploy/scripts/env-file.sh:17`, `.github/workflows/ci-cd.yml:993-998`,
    `docs/SECRETS.md:57`, `:130`, `:138-139`, `docs/CICD.md`.
  - Test: env-file and workflow tests list the keys, which are passed only when non-empty.

## PR F5: `feat/130-coverage-none` (after #283)

- **F5-1** `feat(reviews): finish an all-unsupported change without model calls (#130)`.
  - Gated by the selector, on #283's `CoverageStatus.NONE`.
  - Test: coverage none, 0 `llm.call`, a neutral check-run (CVS:151).
