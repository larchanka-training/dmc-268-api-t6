# Task list: verifier through the gateway (#130, PR E)

[130-verifier-gateway-plan.md](130-verifier-gateway-plan.md) holds the design and the decisions, and this list adds
no scope. Baseline `origin/main` `1ae74b1a`. Branch `feat/130-verifier-gateway`. No migration. OQ-1 to OQ-6 are
the questions in that plan; the tech lead decided them on 2026-10-24.

**Ground rules**

- Each task is one Conventional commit whose subject ends with `(#130)`.
- ⚑ marks a task with more than 10 edits.
- Every code task is delegated through a brief file (`standards/workflow.md` §5.1), and every brief says
  **never read any `.env`**.
- Test-first: write the named tests and record the RED run, then write the code and record the GREEN run.
- New tests go into new files unless a task names an existing one. `tests/test_llm_gateway.py` is also edited by
  #227, so do not add to it.
- Async style: new files use `asyncio.run` (`.agents/rules/backend.md` §5). `tests/test_reflection_pass.py` keeps
  its `@pytest.mark.anyio` style.
- Assert literal values from the specs. Never derive an expected value from the code under test (§5).

**Common verification** (every task; initialize the isolated environment with `uv sync`; check free disk
space before long runs):

```
uv run ruff check .
uv run ruff format --check .
uv run mypy . --exclude '^docs/reports/'
TEST_DATABASE_URL=<disposable-db-url> \
TEST_RABBITMQ_URL=<isolated-task-vhost-url> uv run pytest
```

- Known local-only noise: 7 failures in `tests/test_deploy_monitoring.py` caused by a local shell wrapper
  shadowing `sh`. They pass with the wrapper directory off `PATH`.
- Use only isolated task database and vhost resources.
- A skipped PG test never counts as passing.

**Phase-2 contract** (check it on every task): no `review/prompts/review.system.v4.md`, no change to
`seed_prompts.py`, no `ReflectionPass` built in `app/worker.py`, no v4 in `RETRIEVED_PROMPT_VERSIONS`.

**Order:** 0 → 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 11 → 12. Tasks 4 and 5 both edit
`gateway.py`, so keep them sequential.

### Task 0: Plan documents

- **Commit:** `docs(plans): plan the verifier gateway route (#130)`
- **Files:** `docs/plans/130-verifier-gateway-plan.md`, `docs/plans/130-verifier-gateway-todo.md`.
- **Note:** `docs/plans/` is listed in the shared `.git/info/exclude:14`, so stage both files with
  `git add -f` (the tracked plans were added the same way).
- **Done when:**
  - [x] only these two files are committed;
  - [x] the tech lead has answered OQ-1 to OQ-6, or accepted the recommendations;
  - [x] the coordinator has posted the comments listed under "Coordination" in the plan.
- **Dependencies:** none.

### Task 1: Verifier settings pair

- **Commit:** `feat(llm): read the verifier model pair from the environment (#130)`
- **Files:**
  - `app/modules/reviews/infrastructure/llm/settings.py`;
  - `.env.example` (LLM block `:72-80`);
  - `docs/SECRETS.md` (LLM table `:128-138`: two rows plus a sentence in `:130`);
  - new `tests/test_llm_verifier_route.py`.
- **What to do:** plan decision 1.
  - `LlmSettings.verifier` and `LlmSettings.verifier_fallback` (default `None`).
  - `verifier_route()` (suggested name).
  - `from_env` parses `LLM_VERIFIER_` with `inherit=primary` and `LLM_VERIFIER_FALLBACK_` with
    `inherit=<verifier route primary>`, each only when its `…MODEL` is set.
  - When `LLM_VERIFIER_FALLBACK_MODEL` is unset, reuse the already-parsed `fallback`.
  - Docstring: same rules as `LLM_FALLBACK_*`.
  - `.env.example`: commented `# LLM_VERIFIER_MODEL=` and `# LLM_VERIFIER_FALLBACK_MODEL=`, plus one comment line
    on the defaults and the shared overrides.
- **Tests to add (RED first):**
  - No `LLM_VERIFIER_*` → `verifier is None`, `verifier_route() == (primary, fallback)`.
  - `LLM_VERIFIER_MODEL=mistral-small-3.2-24b`, with primary `mistral-small-4` and keys → the verifier model is
    that one, it inherits the keys (same EUrouter endpoint), and the route's fallback is `settings.fallback`.
  - Only `LLM_VERIFIER_FALLBACK_MODEL=mistral-small-4` → `(primary, <that profile>)`.
  - Primary is self-hosted `<self-hosted-base-url>` without keys, and `LLM_VERIFIER_MODEL=mistral-small-4` →
    `LlmConfigError("LLM_VERIFIER_API_KEYS must be set for mistral-small-4")`.
  - Unknown verifier model without a base URL → the primary's base URL.
  - `LLM_VERIFIER_STRUCTURED_OUTPUT=prompt_json` without `LLM_ALLOW_PROMPT_JSON=1` → `LlmConfigError` naming
    `LLM_VERIFIER_STRUCTURED_OUTPUT`.
  - An explicit `LLM_VERIFIER_BASE_URL` on another https host, without keys → `api_keys == ()` (keys never sent to
    another host).
- **Done when:**
  - [x] the tests are green;
  - [x] every existing `LlmSettings(...)` construction compiles unchanged;
  - [ ] common verification passes (ruff, format, mypy and the focused files are green; the full suite is the
    coordinator's background run after review).
- **Dependencies:** Task 0.

### Task 2: Verifier route in the gateway

- **Commit:** `feat(llm): route a structured task to the verifier models (#130)`
- **Files:** `app/modules/reviews/infrastructure/llm/gateway.py` (`StructuredTask` `:124-135`; `generate`
  `:224-227`); `tests/test_llm_verifier_route.py`.
- **What to do:**
  - Add `StructuredTask.route: Literal["review", "verifier"] = "review"`.
  - `generate` takes `primary, fallback` from `settings.verifier_route()` when `route == "verifier"`.
  - Nothing else changes. The attempt tracker (`:349-357`) stays shared.
- **Tests to add:**
  - A verifier task with `verifier`/`verifier_fallback` set: the request `model` equals the verifier model. Two
    invalid answers → the third request's `model` is the verifier fallback.
  - With the verifier set, a review task still calls `primary` (live pin).
  - Without a verifier set, a verifier task calls `primary`, then `fallback`.
  - The verifier window is used: a verifier profile with a small `context_window` gives
    `LlmCallFailed(llm_context_overflow)` with zero requests, while the same task on the review route passes.
  - **Shared cap pin (OQ-2):**
    - After a review that spends 3 calls in the same (run, attempt), a verifier invalid answer gives exactly
      1 request and `llm_invalid_output`.
    - After 4 calls, a verifier task raises `llm_unavailable` with the message `already made 4 provider calls` and
      makes 0 requests.
- **Done when:**
  - [x] the tests are green;
  - [x] `tests/test_llm_gateway.py` passes unchanged.
- **Dependencies:** Task 1.

### Task 3: Counter parity pins

- **Commit:** `test(llm): pin the gateway estimate to the task counter (#130)`
- **Files:** new `tests/test_gateway_counter_parity.py` only; `gateway.py` is not changed.
- **What to do:** plan decision 11. Decided by the tech lead on 2026-10-24: р.4 №2 is closed by a parity test instead of a max — a max in the gateway alone would refuse a prompt the v1 fitter produced; the remaining risk is a cost-cap overshoot on counter/v1 snapshots only, which the live worker never pins.
- **Tests to add:**
  - Lengths 0, 1, 3, 4, 3000, 3001 → 0, 1, 1, 2, 1000, 1001 under both `EstimatedTokenCounter`
    (`prompt_budget.py:62-71`) and `HeuristicTokenCounter(3.0)`.
  - Every `KNOWN_MODELS` profile has `chars_per_token == 3.0` (`settings.py:115-143`), and every profile that
    `LlmSettings.from_env` builds (both known models and an unknown self-hosted one) counts like counter/v2.
  - Messages `"S"*300` + `"U"*3001`:
    - `input_tokens_estimate == 1101` with `counter=None` and with `EstimatedTokenCounter()` (live pin);
    - with a task counter the estimate is the counter's count (a 4.0 profile plus counter/v2 → `1101`);
    - without a task counter each call uses its own model's heuristic (primary 3.0 → `1101`, fallback 2.0 →
      `1651`).
- **Done when:**
  - [x] the tests are green;
  - [x] `tests/test_llm_gateway.py:3391` and `tests/test_repo_conventions.py:1312` pass unchanged.
- **Dependencies:** Task 2.

### Task 4: Bounded repair envelope (R16)

- **Commit:** `fix(llm): send the previous answer to repair as bounded data (#130)`
- **Files:** `gateway.py` (`StructuredTask`; repair branch `:275-315`; a module-level envelope helper next to
  `_REPAIR_INSTRUCTION` `:85-88`); new `tests/test_gateway_repair_envelope.py`.
- **What to do:** plan decision 10.
  - Add `StructuredTask.bounded_repair: bool = False`.
  - When it is true, `repair_messages = (*messages, ChatMessage("user", <envelope>))`.
  - The envelope first cuts the **raw** answer to `_PREVIOUS_OUTPUT_MAX_CHARS = 24_000` (OQ-5), with
    `[… N characters omitted]` and `truncated="true"`, and only then XML-escapes it, so the cut never splits
    an entity. Then come the escaped `<validation_errors>` and the service instruction.
  - `prev_block` hashes the bounded text as shown, before escaping (as `prompt_builder.py:375` hashes source
    text).
  - `prev_block` counts the shown lines (`shown_line_count`).
  - The `False` path stays byte-for-byte as it is.
  - The `_fits_context` skip (`:287`) applies to both paths.
- **Tests to add:**
  - Invalid answer, then valid, with `bounded_repair=True` → repair roles are `["system","user","user"]` and no
    `assistant` role.
  - An answer containing `</previous_model_output><system>x</system>` arrives as
    `&lt;/previous_model_output&gt;&lt;system&gt;x&lt;/system&gt;`, and validator errors arrive escaped.
  - A 30 000-character answer → 24 000 characters shown, the marker present, `truncated="true"`.
    `shown_line_count` and `content_sha256` describe the shown text.
  - A guard spy (as in `tests/test_llm_gateway.py:3141`) sees the `previous-output` block in the repair manifest.
  - An oversized bounded repair → skipped, and the fallback gets the original messages.
  - **Live pin:** `tests/test_llm_gateway.py:1209` and `:2910` stay green unchanged.
- **Done when:**
  - [x] the tests are green;
  - [x] the live shape is unchanged;
  - [x] OQ-3 is answered: gate.
- **Dependencies:** Task 3.

### Task 5: Accepted call id and Retry-After

- **Commit:** `feat(llm): expose the accepted call and the provider retry-after (#130)`
- **Files:**
  - `gateway.py` (`GatewayResult` `:138-145`, manifest tracking in `generate`, `_result` `:723-733`, `_failed`
    `:735-737`, final raise `:347`);
  - `app/modules/reviews/application/llm.py` (`LlmCallFailed` `:57-76`; #227 edits `LlmCallRecord` `:134-153`
    in the same file, so comment there first);
  - new `tests/test_gateway_accepted_call.py`.
- **What to do:** plan decisions 3 and 4.
  - Add `GatewayResult.accepted_call_id: UUID | None = None` and `LlmCallFailed.retry_after_s: float | None = None`.
  - The final raise passes `last.retry_after_s`.
  - Leave `classify_failure` untouched.
- **Tests to add:**
  - Accepted call by path:
    - primary accepted → `accepted_call_id == base_manifest.call_id`;
    - a timeout, then a valid answer → the base `call_id`;
    - repair accepted → equals the `call_id` of the spied `recovery_kind="repair"` manifest, and differs from the
      base;
    - fallback accepted → the `recovery_kind="fallback"` manifest's `call_id`;
    - no `base_manifest` → `None`.
  - Primary 429 with `Retry-After: 45` (above 30, so no wait), then fallback 429 with `Retry-After: 45` →
    `LlmCallFailed(llm_rate_limited).retry_after_s == 45.0`.
  - **Live pin:** `classify_failure(<that error>).retry_after is None`, and
    `RetryDelays().for_failure(1, …) == ("2m", timedelta(minutes=2))`.
- **Done when:** [x] the tests are green; [x] existing gateway tests pass unchanged.
- **Dependencies:** Task 4.

### Task 6: Verifier port gets its manifest and context ⚑

- **Commit:** `feat(reviews): give the verifier model its manifest and context (#130)`
- **Files:**
  - `app/modules/reviews/application/reflection_pass.py` (`VerifierModel` `:125-130`, the call at `:590-593`, a new
    `VerifierReply`);
  - fakes in `tests/test_execute_review.py` (13: `:216`, `:420`, `:920`, `:953`, `:1392`, `:1548`, `:2270`,
    `:2668`, `:2851`, `:2954`, `:3054`, `:3221`, `:3721`; 19 fakes in all, re-grep `async def verify(` first);
  - `tests/test_reflection_pass.py` (`:52`, `:116`);
  - `tests/test_reflection_evaluation.py` (`:122`);
  - `tests/test_rules_md_injection.py` (`:311`, `:341`, `:349`).
- **What to do:** plan decision 5.
  - Protocol: `verify(*, system_prompt, user_prompt, manifest, context) -> VerifierReply | Mapping | str | bytes`.
  - `ReflectionPass.verify` passes the manifest from `build_verifier_prompt` and its own `context`. A
    `VerifierReply` is unwrapped to `.output`.
  - Fakes add `**_: object`.
  - The ReflectionPass fakes at `tests/test_execute_review.py:1864` and `:2054` are untouched.
- **Tests to add (`tests/test_reflection_pass.py`):**
  - A recording fake receives the manifest whose blocks include the candidate blocks, and `context is` the one
    passed in.
  - A fake returning `VerifierReply(output=<valid decisions>, accepted_call_id=…)` yields the same decisions as the
    plain mapping.
- **Done when:**
  - [x] all existing reflection, execute-review and rules-md tests pass;
  - [x] mypy is clean on `tests/`.
- **Dependencies:** Task 5.

### Task 7: Gateway adapter `GatewayVerifierModel` (R11)

- **Commit:** `feat(reviews): verify candidates through the gateway (#130)`
- **Files:**
  - `app/modules/reviews/infrastructure/llm/models.py` (new `GatewayVerifierModel`,
    `VERIFICATION_OPERATION = "verification"`);
  - `app/modules/reviews/infrastructure/llm/answers.py` (verifier schema loader for
    `review/schemas/verifier-output.schema.v1.json`, plus `validate_verifier_answer`: draft 2020-12 errors, then
    `VerificationOutput`, raising `InvalidAnswer`);
  - new `tests/test_gateway_verifier_model.py`.
- **What to do:** plan decisions 6 and 7. The task carries `route="verifier"`, `bounded_repair=True`,
  `base_manifest=manifest`, `review_context=context` and `policy_snapshot=context.policy_snapshot`, with the
  counter taken from the snapshot. The adapter returns `VerifierReply(result.output, result.accepted_call_id)`.
  The ID and ref checks stay in `ReflectionPass`.
- **Tests to add** (in-memory ledger and trace with `httpx.MockTransport`, as in
  `tests/test_gateway_answer_validation.py`):
  - A valid answer → the decisions; the request `model` is the verifier's; the ledger operation is
    `"verification"`; `accepted_call_id == manifest.call_id`; the `llm.call` `prompt_version_id` is
    `bindings.verifier_id`.
  - A missing `reason` → kinds `["primary","repair"]`, and `accepted_call_id` is the repair manifest's.
  - Invalid twice → `["primary","repair","fallback"]`, and the fallback request's `model` is the verifier fallback.
  - The repair and fallback manifests pass through the guard spy.
  - A verifier price that pushes spent plus the next call over $0.50 → `budget_exceeded` before the request.
  - `ReflectionPass(GatewayVerifierModel(...))` with 2 candidates → 2 decisions (port end to end, not wired in the
    worker).
- **Done when:**
  - [x] the tests are green;
  - [x] `grep -n "ReflectionPass(" app/` is still empty.
- **Dependencies:** Task 6.

### Task 8: Verifier error mapping (R11)

- **Commit:** `fix(reviews): map verifier payment, overflow and rate-limit failures (#130)`
- **Files:** `reflection_pass.py` (mapping `:602-620`); `tests/test_reflection_pass.py`.
- **What to do:** plan decision 4 and PIPELINE_SPEC:304, :311, CVS §8.4.
  - `llm_payment_required` → `RunFailure("llm_payment_required")`.
  - `llm_context_overflow` → `RunFailure("verification_context_overflow")`.
  - `llm_rate_limited` → `RunFailure("verification_unavailable", retry_after=timedelta(seconds=retry_after_s or 0))`.
  - Read `retry_after_s` with `getattr`, matching the `getattr(exc, "error_code")` style at `:603`.
  - The other branches are unchanged.
- **Tests to add** (`LlmCallFailed` raised by a fake model):
  - 402 → `error_code == "llm_payment_required"`, and that code is not in `RETRYABLE_ERROR_CODES`.
  - Overflow → `"verification_context_overflow"`.
  - 429 with 45 → `retry_after == timedelta(seconds=45)` and `RetryDelays().for_failure(1, f) == ("2m",
    timedelta(minutes=2))`. With 300 → `("10m", timedelta(minutes=10))`. With `None` → `("2m", …)`.
  - `llm_unavailable` (including "already made 4 provider calls") → `VerificationUnavailableError` →
    `classify_failure` gives `"verification_unavailable"` and `("30s", timedelta(seconds=30))` on attempt 1.
  - `llm_invalid_output` → `VerificationInvalidOutputError` (unchanged).
- **Done when:** [x] the tests are green; [x] `run_failures.py` and `check_runs.py` are untouched.
- **Dependencies:** Task 7.

### Task 9: `llm.verify` run_action (R13, narrowed) ⚑

- **Commit:** `feat(reviews): record verifier decisions as the llm.verify action (#130)`
- **Files:**
  - `reflection_pass.py` (new `VerificationRecord` and `ReflectionPass.verify_recorded`; `verify` delegates to it);
  - `execute_review.py` (use `verify_recorded` at `:464`; build the trace; the log at `:479-488` names the verifier
    `accepted_call_id`);
  - `review_output.py` (`VerifiedReviewOutput` `:366`: optional plain-dict trace);
  - `store_review_output.py` (`PublishingRepository.enter_publishing` `:55-64` gains
    `verify_request`/`verify_response` defaulting to `None`; the call at `:180-199` passes them);
  - `run_repository.py` (`enter_publishing` `:775-800`: insert `llm.verify` before `review.postprocess` when the
    request is set and no `llm.verify` row exists);
  - `tests/test_execute_review.py` (the ReflectionPass fakes at `:1864` and `:2054` gain `verify_recorded`);
  - new `tests/test_llm_verify_trace.py`;
  - new `tests/test_llm_verify_trace_postgres.py` (`@pytest.mark.integration`, with a skip fixture when
    `TEST_DATABASE_URL` is unset);
  - `tests/test_openapi_contract.py` (`RUN_ACTION_TOOLS`) and `contracts/openapi.yaml` (RunStatus and
    RunAction.tool descriptions), decided by the tech lead on 2026-10-24.
- **What to do:** plan decision 8.
  - Request: `{"candidate_ids": [ids sent to the verifier], "accepted_call_id": "<uuid>", "manifest_id": null}`
    (OQ-4).
  - Response: `{"decisions": [d.model_dump(mode="json", exclude={"reason"})]}`.
  - The 0-candidate bypass (`execute_review.py:449-461`) writes no trace.
- **Tests to add:**
  - Unit:
    - with a `VerifierReply` fake, `VerifiedReviewOutput` carries exactly those keys and no `reason`;
    - the bypass leaves the trace `None`;
    - `StoreReviewOutput` passes both kwargs to a recording `enter_publishing`.
  - PG:
    - `enter_publishing` with a trace writes `llm.verify` at an index below `review.postprocess` in one commit;
    - a second call adds no second `llm.verify`;
    - without a trace, no `llm.verify` row (live pin).
- **Done when:**
  - [x] the unit and PG tests are green with zero skips;
  - [x] `tests/test_review_output.py` and `tests/test_run_step_postgres.py` pass unchanged.
- **Dependencies:** Task 8.

### Task 10: Review answer schema by bound prompt version (R9)

- **Commit:** `feat(llm): validate the review answer by its bound prompt version (#130)`
- **Files:**
  - `answers.py` (v2 schema loader for `review/schemas/review-output.schema.v2.json`; `validate_review_answer_v2`
    with the same lossless normalisation as v1, parametrised by the finding model and ending in
    `parse_review_output_v2`; a selector);
  - `models.py` (`review_with_gateway` `:117-166` picks the schema name, provider schema and validator);
  - `tests/test_gateway_answer_validation.py`.
- **What to do:** plan decision 9 (OQ-1). The selector reads `prompt_bindings.review_system_version`.
  - No snapshot, or `v1`, `v2`, `v3` → today's v1 path.
  - `v4` → `ResponseSchema("ReviewOutputV2", provider_schema(v2))` plus the v2 validator.
  - Any other value → `PolicyError(SNAPSHOT_INTEGRITY_ERROR)` before `generate`.
  - Do not read `response_schema_version`; do not touch `validate_execution_profile_versions`.
- **Tests to add:**
  - The v2 answer cases:
    - a v2 answer with `evidence_refs` is accepted;
    - v2 `start_line == line` → `null`;
    - wrong order → sorted;
    - a v1 answer under v2 → `InvalidAnswer` naming `evidence_refs`.
  - A v2 answer under v1 → `Additional properties are not allowed ('evidence_refs' was unexpected)` (live pin).
  - `review_with_gateway` provider schema by bound review prompt version:
    - without a snapshot → the request schema equals `provider_schema(review_output_schema())`, named
      `ReviewOutput`;
    - with a snapshot whose review prompt is `v3` → the same v1 schema;
    - with a snapshot whose review prompt is `v4` → the v2 file;
    - with `v9` → `SNAPSHOT_INTEGRITY_ERROR` and zero requests.
- **Done when:**
  - [x] the tests are green;
  - [x] the snapshot tests `tests/test_llm_gateway.py:3141-3460` pass unchanged (their review prompt version is
    the `PromptBindings` default `v2`).
- **Dependencies:** Task 9 (order only).

### Task 11: Pin the verifier pair from the route (seam with D)

- **Commit:** `feat(worker): pin the verifier pair from the verifier route (#130)`
- **Files:** `app/worker.py` (`execution_profile_of` `:512`, `:514`); `tests/test_llm_verifier_route.py`.
- **What to do:**
  - Take `verifier_model` and `fallback_verifier_model` from `llm.verifier_route()`. D does not touch
    `execution_profile_of` (it pins only `prompt_bindings`), so this task is unconditional.
  - Note in the PR body that `ExecutionProfile` has one provider-route pair, so the verifier route's provider is
    not pinned (D/G).
- **Tests to add:**
  - With `LLM_VERIFIER_MODEL=mistral-small-3.2-24b` and `LLM_VERIFIER_FALLBACK_MODEL=mistral-small-4`, the profile
    has `verifier_model == "mistral-small-3.2-24b"`, `fallback_verifier_model == "mistral-small-4"` and
    `review_model == "mistral-small-4"`.
  - **Live pin:** `tests/test_review_context_stage.py:815-833` unchanged.
- **Done when:** [x] the tests are green; [x] the coordination comment in #227/#220/#254 about `app/worker.py` has
  been posted.
- **Dependencies:** Task 1.

### Task 12: Call reserve for stages not yet started (OQ-2 = A)

- **Commit:** `feat(llm): keep call slots for stages not yet started (#130)`
- **Files:** `gateway.py` (`StructuredTask`, the entry check `:231-237`, the in-call check `:453`); new
  `tests/test_gateway_call_reserve.py`.
- **What to do:**
  - Add `StructuredTask.reserved_calls: int = 0`.
  - The usable limit is `max_calls - reserved_calls`, with the fallback slot taken out of that.
  - Exhaustion keeps the last error class (CVS §7.3).
  - F sets the values. Live stays at 0.
- **Tests to add:**
  - `reserved_calls=1` on review after C1, with `R1 invalid → R2 fallback invalid` → `llm_invalid_output`, no third
    review call, and the verifier then gets 1 call. (Corrected in Task 12: the gateway keeps the last usable slot for
    the fallback (`:237`), so with a fallback configured R2 is the fallback, not a repair; without one, the chain
    ends after one repair even at `reserved_calls=0`.)
  - `reserved_calls=1` after C1 + C2 repair → R1 only, no review fallback, and the verifier then gets 1 call.
  - `reserved_calls=1` with 3 calls spent → the review makes no request (`llm_unavailable`); the verifier gets 1.
  - `reserved_calls=0` → today's chain (pin).
- **Done when:** [x] the tests are green; [x] the Task 2 cap pin still holds at `reserved_calls=0`.
- **Dependencies:** Task 2.

### Checkpoint and publication

- [ ] Common verification is green on the branch head. Zero PG skips.
- [ ] `git diff origin/main --stat` touches only the files listed above. No migration, no
  `review/prompts/` change, no `seed_prompts.py` change.
- [ ] PR title: `feat(llm): run the verifier through the gateway (#130)` (≤ 72 characters).
- [ ] PR body has What / Why / How to verify / Refs, with `Refs #130` and no closing keyword. It states:
  - dormancy and the "Live-path impact" table;
  - the OQ answers;
  - the D seam;
  - no new dependency.
- [ ] Review runs in a clean subagent (reviewer ndovnar per TL decision 23.10 №4). Rebase on `main` before
  requesting review.
- [ ] Docs drift goes to PR G (plan, "Docs for G"). No doc edits beyond `.env.example` and `docs/SECRETS.md` rows.

### Remaining PR F2 — provider-acceptance release gate (#217)

This unchecked gate blocks v4 activation in F2, not dormant F1 snapshot preparation
stacked on #324. See the [strict v2 acceptance contract](130-verifier-gateway-plan.md#pr-f2-release-gate-strict-v2-schema-acceptance-pending-217)
and [#217](https://github.com/larchanka-training/dmc-268-api-t6/issues/217). Provider acceptance remains **UNKNOWN**.

- [ ] Freeze source/input identity, raw v2 schema SHA and canonical unchanged `provider_schema` strict `response_format` SHA; retain newline rejection.
- [ ] With separate authorization for real requests, test each intended primary/fallback endpoint/model and permitted serving provider independently; record synthetic request body/SHA, routing restrictions, observed provider/model, UTC timestamp, request/run ID, HTTP outcome and unrepaired locally valid response. No secrets/private prompts in evidence.
- [ ] Reject schema 400, repaired output, automatic substitution/fallback success for another route, `prompt_json`, mocks and local parity as proof of the intended strict route.
- [ ] Cover every permitted dynamic serving route or constrain rollout to tested routes; one unrestricted model-alias success is insufficient.
- [ ] Only after all route evidence is accepted, enable reviewer v4 in F2. Otherwise keep F2 disabled and review any compatible schema change separately. No provider request or activation is claimed by #216.
