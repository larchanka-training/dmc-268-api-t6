# Implementation plan: verifier through the gateway (#130, PR E)

## Status and baseline

Draft for tech-lead approval, written 2026-10-24 by the planning step of the agent-loop skill.
Issue: https://github.com/larchanka-training/dmc-268-api-t6/issues/130 (assignee axyi). This is PR E of
phase 2 of the #130 remainder: R9, R11, R13 (narrowed), R16 and counter parity. Task checklist:
[130-verifier-gateway-todo.md](130-verifier-gateway-todo.md).

- Baseline: `origin/main` `1ae74b1a`, single Alembic head `20261020_0037`. E adds no migration.
- Worktree: `projects/dmc-268-api-t6-130e`. Branch: `feat/130-verifier-gateway`. Test DB and vhost: `imp130e`.
- Every `file:line` below was checked against `1ae74b1a` on 2026-10-24.
- **Suggested** marks a name or shape the executor may change. **Derived** marks something that follows from
  the cited code rather than from a new decision.

## Context

The verifier exists only in tests. `ReflectionPass(` is never built in `app/`, and the worker composes
`ExecuteReviewRun` without it (`app/worker.py:277-280`). `VerifierModel` is a bare protocol,
`verify(*, system_prompt, user_prompt)` (`app/modules/reviews/application/reflection_pass.py:125-130`), and nothing
implements it on top of the LLM gateway. So, today:

- **One model pair.** The gateway always calls `settings.primary` and then `settings.fallback`
  (`app/modules/reviews/infrastructure/llm/gateway.py:226-227`). `StructuredTask` (`gateway.py:124-135`) has no
  route field.
- **Error mapping.** `llm_payment_required`, `llm_context_overflow` and `llm_rate_limited` all fall through to the
  retryable `VerificationUnavailableError` (`reflection_pass.py:602-620`).
- **R13.** Verifier decisions are only logged, under the *reviewer's* call id (`execute_review.py:479-488`).
- **R9.** The gateway validates every review answer against the v1 schema (`llm/answers.py:45-56`, `:74-95`).
  The snapshot path, however, needs v2 findings with `evidence_refs`
  (`execute_review.py:341-375` rejects a candidate without them).
- **R16.** Repair re-sends the invalid answer as a raw `assistant` message (`gateway.py:275-286`). Its size is
  checked only by `_fits_context` (`:287`). That breaks RFS §8.1 (`docs/RULES_FORMAT_SPEC.md:836-839`) and is
  listed as uncovered in `docs/TEST_PLAN.md:200`.
- **Counter parity.** The pre-send estimate uses `task.counter` when it is set, otherwise the profile heuristic
  (`gateway.py:362-369`). It never takes the larger of the two; р.4 №2 is closed by parity pins instead
  (decision 11).

E is a **dormant** PR. Every new path runs only once a `ReflectionPass` or a snapshot-path task exists, and both
arrive with PR F. E adds no `review.system.v4.md` and no `seed_prompts.py` change, wires no `ReflectionPass` in the
worker, and leaves `RETRIEVED_PROMPT_VERSIONS` alone.

## Requirements and tests

| Item | Requirement | Tasks | Tests that pin it (literal values from the specs) |
| --- | --- | --- | --- |
| Route | `LLM_VERIFIER_MODEL` and `LLM_VERIFIER_FALLBACK_MODEL` default to `LLM_MODEL` / `LLM_FALLBACK_MODEL` and are validated like the existing routes (TL 24.10) | 1, 2 | Unset: the verifier route is the review pair. A known model on a foreign endpoint without keys raises `LlmConfigError` naming `LLM_VERIFIER_API_KEYS`. A verifier task's request body carries the verifier `model`, and its fallback is the verifier fallback. |
| R11 | Gateway adapter for `VerifierModel`, covering usage ledger, admission, output reserve and repair/fallback | 5, 6, 7 | The `usage_events` operation is `"verification"`. One schema-invalid answer gives `["primary","repair"]`; two give `["primary","repair","fallback"]`. Repair and fallback manifests pass the pre-send guard. The verifier window and price are used for the context and cost checks. |
| R11 | Shared cap of 4 calls per (run, attempt) and the run cost cap | 2, 12 (OQ-2 = A) | After a review spends 3 calls, the verifier gets exactly 1 call and no fallback, and the original error class is kept. The arithmetic is in "Call budget" below. |
| R11 | Error mapping: 402 → `llm_payment_required` with no run retry; overflow → `verification_context_overflow`; 429 → `verification_unavailable` with Retry-After | 5, 8 | `RetryDelays().for_failure(1, f)` gives `("2m", 2 min)` for Retry-After 45 s or none, and `("10m", 10 min)` for 300 s. 402 is not in `RETRYABLE_ERROR_CODES`. |
| R13 | run_action `llm.verify` with request `{candidate_ids, accepted_call_id, manifest_id}` (`docs/PIPELINE_SPEC.md:177`; `manifest_id` is `null`, OQ-4), no migration, `reason` not persisted | 9 | A PG row `llm.verify` sits before `review.postprocess` in the same commit. No `reason` key anywhere. A legacy store writes no `llm.verify`. A second `enter_publishing` adds no second row. |
| R9 | Answer schema chosen by the bound review prompt version (OQ-1); the live review keeps v1 | 10 | No snapshot, or review prompt `v1`–`v3`: the provider schema equals `review-output.schema.json` without annotations. Review prompt `v4`: the v2 file, with `start_line==line` normalised to `null` and the ordering normalised. An unknown version (`v9`) raises `SNAPSHOT_INTEGRITY_ERROR` before any call. |
| R16 | Repair receives the previous answer as bounded, escaped data (RFS §8.1) | 4 | Roles are `[system, user, user]`. `</previous_model_output>` inside the answer arrives escaped. An answer longer than the bound is cut, with a marker. The live shape `[system, user, assistant, user]` is unchanged (`tests/test_llm_gateway.py:1209`). |
| Parity | The pre-send estimate stays the pinned task counter (the fitted prompt is the prompt sent); a parity test keeps counter/v2 equal to the profile `chars_per_token` heuristic (decision 11) | 3 | Lengths 0/1/3/4/3000/3001 give 0/1/1/2/1000/1001 under both counters. Every `from_env` profile counts 3.0 characters per token. The live estimate is 1101 with no counter and with `counter/v2`, for both `KNOWN_MODELS`. |
| D seam | `verifier_model` / `fallback_verifier_model` come from the verifier route | 11 | With a default environment the profile is unchanged (`tests/test_review_context_stage.py:815-833` stays green). With `LLM_VERIFIER_MODEL=mistral-small-3.2-24b` the pinned `verifier_model` follows it. |

## Design decisions

1. **Settings** (`llm/settings.py:86-111`):
   - Add `LlmSettings.verifier` and `LlmSettings.verifier_fallback`, both `ModelProfile | None = None`.
     The defaults matter because `LlmSettings(primary=…, fallback=…)` is constructed directly in many tests.
   - Add `verifier_route() -> tuple[ModelProfile, ModelProfile | None]` (suggested name). It returns
     `(verifier or primary, verifier_fallback if set else fallback)`.
   - `from_env` parses `LLM_VERIFIER_` with `inherit=primary` and `LLM_VERIFIER_FALLBACK_` with
     `inherit=<verifier route primary>`, reusing `_profile_from_env` (`:232-373`). It parses each only when its
     `…MODEL` is set.
   - Derived consequences: keys follow the endpoint, `prompt_json` needs `LLM_ALLOW_PROMPT_JSON`, and
     `KNOWN_MODELS` floors apply.
2. **Route** (`gateway.py:124-135`, `:224-227`):
   - `StructuredTask.route: Literal["review", "verifier"] = "review"`. `generate` takes its primary/fallback pair
     from that route.
   - The per-(run, attempt) tracker (`:349-357`) is keyed by `run_id`, so the verifier shares the cap of 4 with
     conventions and review. Derived.
   - `max_prompt_tokens()` stays primary-based. The verifier send budget is R10 (F).
3. **Accepted call** (R13):
   - `GatewayResult.accepted_call_id: UUID | None` (suggested) is the `call_id` of the manifest of the call whose
     answer was accepted:
     - the base manifest for primary and retry;
     - the new manifest for repair (`:302-307`) and fallback (`:331-335`);
     - `None` when the task has no `base_manifest`.
   - Retry calls keep the base `call_id` today. CVS:597-598 wants a manifest per call; that goes to G (see "Docs").
4. **Retry-After** (verifier only):
   - `LlmCallFailed` (`application/llm.py:57-76`) gains `retry_after_s: float | None = None`, and the gateway's
     final raise (`:347`) passes `last.retry_after_s`.
   - `classify_failure` is **not** changed. If it read the field, live review delays would move from 2 min to
     10 min for Retry-After above 120 s.
   - `ReflectionPass` raises `RunFailure("verification_unavailable", retry_after=…)`, and `RetryDelays` then
     applies 2m/10m (`run_failures.py:136-146`, PIPELINE_SPEC:229, :311). A 429 without a header gets
     `retry_after=timedelta(0)`, so the rate-limit branch still gives ≥ 2 min.
5. **Protocol** (`reflection_pass.py:125-130`):
   - Signature: `verify(*, system_prompt, user_prompt, manifest: CallManifest, context: ReviewContext)`.
   - Return type: `VerifierReply | Mapping | str | bytes`, where `VerifierReply(output, accepted_call_id)` is
     suggested.
   - `ReflectionPass.verify` keeps its signature and return type, because about 70 test call sites use it. A new
     `verify_recorded(...) -> VerificationRecord` (suggested) feeds R13.
   - The 19 existing `VerifierModel` fakes in 4 test files gain `**_: object` (⚑ Task 6).
   - The `last_result` attribute pattern (`llm/models.py:66`) was rejected: it would put mutable per-call state on
     the port.
6. **Validation split** (derived from CVS §7.3, "repair с ошибками схемы"):
   - The gateway validator for the verifier is the strict schema `review/schemas/verifier-output.schema.v1.json`
     plus `VerificationOutput`. These errors get one repair, then the fallback.
   - The 1:1 candidate-ID check and the ref checks against the manifest stay in `ReflectionPass`. They fail
     straight to `verification_invalid_output` without repair (PIPELINE_SPEC:311).
7. **Adapter** (`llm/models.py`):
   - `GatewayVerifierModel(gateway, run)` sends `operation="verification"`, which matches `StageName`
     (`policy_snapshot.py:27`) and PIPELINE_SPEC:170.
   - It uses `route="verifier"` and `bounded_repair=True`.
   - `counter` is `token_counter_for(profile.counter_version)` when there is a snapshot, otherwise `None`.
   - `review_context` and `policy_snapshot` come from `context`.
   - `llm.call` records carry `replace(run, prompt_version_id=bindings.verifier_id)` when the snapshot pins it.
8. **R13 channel**:
   - `VerifiedReviewOutput` (`review_output.py:366`) carries plain dicts, not `VerificationDecision`:
     `reflection_pass.py` already imports `review_output`, so the reverse import would create a cycle.
   - `StoreReviewOutput` passes them to `enter_publishing` (`run_repository.py:775-800`) through new optional
     kwargs. `enter_publishing` inserts `llm.verify` before `review.postprocess`, only if no `llm.verify` row
     exists, in the same transaction. This follows CVS §7.4 and is idempotent like `llm.review_output`
     (`:698-713`).
   - Both existing `enter_publishing` fakes take `**kwargs` (`tests/test_review_output.py:369`, `:455`).
   - The `tool` field is a free string in the API (`api/dtos.py:78`) and in the UI Zod schema
     (`src/entities/run/model/schemas.ts:84`, `z.string()`). No contract change.
   - Response: `{decisions: [decision.model_dump(exclude={"reason"})]}`. `manifest_id`: see OQ-4.
9. **R9 key** (OQ-1, decided):
   - The selector keys on the bound review prompt version, `policy_snapshot.prompt_bindings.review_system_version`,
     not on the profile's `response_schema_version`. `v4` → `review-output/v2`; `v1`, `v2`, `v3`, or no snapshot →
     v1; anything else (e.g. `v9`) → `PolicyError(SNAPSHOT_INTEGRITY_ERROR)`.
   - The selector raises inside `review_with_gateway` before the call, and not in
     `validate_execution_profile_versions`, which runs live (`prepare_policy_snapshot.py:165`, `:170`;
     `review_context_stage.py:273`).
   - `PromptBindings` defaults `review_system_version="v2"` (`policy_snapshot.py:141`), so the five snapshot
     gateway tests (`PromptBindings("cs1","cs2","cs3")`) stay on v1.
   - `app/worker.py:520` pins `response_schema_version="review-output/v2"` live; that pin is not the selector.
10. **R16 gate**:
    - An explicit `StructuredTask.bounded_repair: bool = False` (suggested). It cannot key on `base_manifest`,
      because live review and conventions already set it (`models.py:96-102`, `:147-153`).
    - The verifier always sets it. F sets it for review and conventions.
    - The repair turn becomes one user message with these parts:
      - `<previous_model_output block_id="previous-output" truncated="…">`: the answer, cut to the bound and
        XML-escaped with `xml.sax.saxutils.escape`;
      - `<validation_errors>`: each error escaped, since jsonschema echoes model text;
      - the service-owned instruction.
    - The `previous-output` manifest block hashes the bounded text that is shown (other blocks hash source text,
      `prompt_builder.py:375`), with `shown_line_count` equal to its line count. The bound is OQ-5.
11. **Parity**: Decided by the tech lead on 2026-10-24: р.4 №2 is closed by a parity test instead of a max — a max in the gateway alone would refuse a prompt the v1 fitter produced; the remaining risk is a cost-cap overshoot on counter/v1 snapshots only, which the live worker never pins.
    - `_estimate` (`gateway.py:362-369`) is unchanged: it estimates with the task counter when one is set.
    - `tests/test_repo_conventions.py:1312` stays green unchanged.

## Call budget (R11: worst case against the cap of 4 and $0.50)

**How the cap works today:**
- The cap is `GatewayPolicy.max_calls_per_attempt = 4` (`settings.py:77`), shared by every `generate()` of one
  (run, attempt). It is checked at entry (`gateway.py:231-236`) and again before each call (`:453`).
- A stage's primary, retry and repair calls stop at `max_calls - 1` while a fallback exists (`:237`), and the
  fallback takes the last slot.
- There is no per-stage reserve. CVS §7.3 (`docs/CONTEXT_AND_VERIFICATION_SPEC.md:837-878`) and
  PIPELINE_SPEC:260 require one slot reserved for each mandatory stage that has not started yet.

**Demand per attempt:**
- Without the cap, an invalid-output chain is C 3 + R 3 + V 3 = 9 calls (6 on a conventions cache hit).
- With transient retries, every stage can reach 4, for 12 calls (8 on a hit).
- On the happy path, a cache miss uses C→R→V = 3 calls and leaves 1 for recovery.

**Legitimate path cut by the missing reserve:**
- Cache miss `C1 → R1 invalid → R2 repair → R3 fallback` uses all 4 slots. The verifier is then stopped at the
  entry check with `llm_unavailable` ("already made 4 provider calls"). E maps that to the retryable
  `verification_unavailable`, so the whole attempt is paid for again.
- CVS §7.3 forbids R3 here: the verifier slot is reserved, and the reviewer should fail with its own
  `llm_invalid_output`.
- `C1 → C2 → C3 → R1` cuts the verifier the same way.
- This is already live today for conventions against review: conventions can spend all 4 slots.

**Paths that stay within spec:**
- `C1 → R1 → R2 repair → V1` fits. If V1 is invalid, the run ends in a terminal `verification_invalid_output`,
  which CVS calls an "explicit failed".
- Cache hit `R1 → R2 → V1 → V2`: the gateway makes V2 a fallback, not the repair that the CVS example shows,
  because `:237` holds the last slot for the fallback. This is a deviation, listed for G.

**Cost** (`KNOWN_MODELS`, fast):
- A primary call is at most 52 000 × $0.56125/M + 8 000 × $2.35725/M = **$0.048043**. A fallback call is at most
  52 000 × $0.2245/M + 8 000 × $0.449/M = **$0.015266**. An ECB quote can only raise these figures.
- An attempt makes at most 4 calls, about $0.19, so the $0.50 cap never cuts inside one attempt.
- Three attempts × 4 primary calls come to $0.576 > $0.50, so the cost check can stop the third attempt. At the
  verifier stage that check yields the terminal `verification_budget_exceeded`, which matches the spec.

**Decision (OQ-2 = A):**
- The cap value is not changed.
- Task 12 adds the dormant `StructuredTask.reserved_calls` (default 0); F sets the values.
- Task 2 adds a test that pins today's behaviour (the verifier entering at 4 calls fails with `llm_unavailable`);
  it still holds at `reserved_calls=0`.

## Live-path impact

| Task | What a live run sees | Why it is safe, and the pinning test |
| --- | --- | --- |
| 1 | `LLM_VERIFIER_*` is parsed at worker start, only when set | Neither staging nor `.env.example` sets it. An invalid value fails start like any `LLM_*` variable. Test: an unset environment gives `verifier_route() == (primary, fallback)`. |
| 2, 5 | New fields with defaults (`route="review"`, `accepted_call_id`, `retry_after_s`) | `classify_failure` does not read `retry_after_s`. Test: `classify_failure(LlmCallFailed(RATE_LIMITED, …, retry_after_s=300)).retry_after is None`. |
| 3 | Nothing: Task 3 adds tests only | Parity test: `input_tokens_estimate` is identical with no counter and with `counter/v2`, for both `KNOWN_MODELS`. |
| 4 | Nothing: `bounded_repair` defaults to `False` | `tests/test_llm_gateway.py:1209` (review) and `:2910` (conventions) stay green unchanged. **Gated, live unchanged** (OQ-3, decided). |
| 6–9 | Nothing: no `ReflectionPass` in the worker; the `enter_publishing` kwargs default to `None` | A PG test shows that a legacy store writes no `llm.verify`. |
| 10 | Nothing: without a snapshot the v1 schema and validator are used | Test: the request `response_format` equals the v1 file without annotations. |
| 11 | Same pinned values with a default environment | `tests/test_review_context_stage.py:815-833` stays green unchanged. |

## Coordination (comments posted by the coordinator before the edit)

**#227** (AndrewBlinets, CHANGES_REQUESTED, head `4da55046`):
- `llm/gateway.py`: #227 edits `_call`'s `_RecordDraft(...)` (`:469`), the `_RecordDraft` fields (`:746-750`) and
  `finish` (`:771-776`). E edits:
  - `StructuredTask` and `GatewayResult` (`:124-145`);
  - `generate` (`:224-347`);
  - `_result` and `_failed` (`:723-737`);
  - plus a new module-level helper.
- There is no textual overlap. The semantic seam is #227's new `operation` field in `llm.call`: the verifier adds
  `"verification"`. Ask whether its overview groups usage by operation.
- `.env.example`: #227 `:32-43` vs E `:72-80`.
- `docs/SECRETS.md`: #227 `:159` vs E `:130-138`.
- `app/worker.py`: #227 `:774` vs E `:511-514`.
- `store_review_output.py`: #227 `:152-175`, `:188-205` vs E's `enter_publishing(...)` call (`:180-199`). They are
  adjacent, so expect a textual conflict; whoever merges second rebases.
- `run_repository.py`: #227 `:660-685` vs E `enter_publishing` `:775-800`.
- `review_output.py`: #227 `:497`, `:619` vs E `VerifiedReviewOutput` `:366-390`.
- `application/llm.py`: #227 `LlmCallRecord` `:134-153` (adds `operation`) vs E `LlmCallFailed` `:57-76`.
- `tests/test_rules_md_injection.py`: #227 `:729-737` vs E's fakes `:311`, `:341`, `:349`.
- E adds no tests to `tests/test_llm_gateway.py`. New tests go into new files.

**#283** (ndovnar):
- `store_review_output.py` `:142-175`, `:193-201` is adjacent to E's call site; expect a conflict.
- `run_repository.py` `:799-823` deletes the lines right after `enter_publishing`; the hunks are adjacent but do
  not overlap.
- `review_output.py` `:21`, `:501`.

**#229** (AndrewBlinets):
- `review_output.py` `:472-532`, `:618`;
- `store_review_output.py` `:151`;
- `run_repository.py` `:629-720`;
- `tests/test_rules_md_injection.py` `:70`, `:729`, `:970` (vs E's fakes `:311-349`).
- None overlaps E's hunks.

**#220** (ndovnar):
- `.env.example` `:9-17`, `:35-67`;
- `docs/SECRETS.md` `:107`, `:152+` (it inserts right after E's LLM table);
- `app/worker.py` hunks at `:459-485` and `:677+`;
- `run_repository.py` `:438-470`.
- None overlaps. E does not touch `run_failures.py` or `check_runs.py`: every code E needs is already registered
  (`run_failures.py:45-75`, `check_runs.py:65-69`).

**#254** (axyi): `app/worker.py` `:535-568` is close to E's `:511-514` but does not overlap.

**#280** (ndovnar, D is stacked on it): `run_repository.py` `:638-665` (`get_post_processing_input`), which does
not overlap E's `enter_publishing` `:775-800`. E does not touch `policy_snapshot.py` or `prepare_policy_snapshot.py`.

The overlap lists come from intersecting E's complete file list with each PR's full, paginated file list on
2026-10-24.

**Seam with D** (`verifier_model`/`fallback_verifier_model`):
- The values come from `LlmSettings.verifier_route()`.
- D pins only `prompt_bindings` at run creation; `execution_profile` stays frozen in the first policy snapshot
  (`prepare_policy_snapshot.py:161-166`) and D does not touch `execution_profile_of`.
- E wires `execution_profile_of` (`app/worker.py:512`, `:514`) in Task 11, unconditionally.

**Seams with F**:
- F builds `ReflectionPass(GatewayVerifierModel(gateway, run))` in the worker.
- F sets `bounded_repair=True` on review and conventions, and sets `reserved_calls`.
- F adds the v4 prompt; binding it switches the review answer schema to `review-output/v2` (decision 9).
- F carries `LLM_VERIFIER_*` to staging (compose, `env-file.sh`, CI) if a different verifier model is chosen
  (OQ-6).
- E reserves neither money nor time for the verifier. CVS §7.4 wants both reserved before the review call (an
  upper-bound verifier cost, and one verifier call + 10 s of deadline); the existing adapters do not reserve
  them either. That goes to F, or is listed for G, and OQ-2 does not cover it.
- F sizes the verifier send budget from the verifier route (R10). `send_budget_for` also reads
  `os.environ["LLM_CONTEXT_WINDOW"]` for both stages (`prompt_budget.py:164-169`).

## Docs for G (drift E leaves; nothing in them becomes false on E's merge)

- **RFS §7.2:**
  - `:725-726` "verifier_model = review_model" needs to describe the separate `LLM_VERIFIER_*` pair.
  - `:720-724` needs the parity rule of р.4 №2 (decision 11): counter/v2 equals the profile heuristic, and the
    gateway estimates with the pinned counter.
  - `:730` `recovery/v1` needs to say the previous output is now bounded data.
  - `response_schema_version` (`review-output/v2`) is not listed anywhere in the docs, and the review answer
    schema follows the bound review prompt version (decision 9).
- **PIPELINE_SPEC:**
  - `:177` `llm.verify` "код пока не пишет" (it is written on the snapshot path), plus `manifest_id`, plus no
    `reason`;
  - `:170` and `usage_events` need the `verification` operation;
  - `:303-311` needs the verifier 402/429 mapping;
  - §9 `:576` should say v2 and the verifier schema are implemented but not live-checked.
- **CVS:**
  - §7.3: the reserve is the per-task `reserved_calls` that F sets, and V2 is a fallback rather than a repair;
  - `:597-598`: retry calls reuse the base `call_id`.
- **TEST_PLAN** `:200`: the repair injection row is covered on the snapshot path.
- **SD §15 OQ-2** and `docs/SECRETS.md`/`CICD.md`: carrying `LLM_VERIFIER_*` to staging (OQ-6).
- **Provider route:** `ExecutionProfile` has one provider-route pair (`policy_snapshot.py:181-182`), so the
  verifier route's provider is not pinned (D, RFS §7.1).
- **Join key:** `llm.verify.candidate_ids` cannot yet be joined to `findings` rows. `review.postprocess`
  `candidate_ids` is a #107 target (PIPELINE_SPEC:176).

## Open questions for the tech lead

1. **OQ-1, R9 key.** Decided by the tech lead on 2026-10-24 (per the coordinator's brief): key on the bound review prompt version, not on `response_schema_version` (decision 9).
2. **OQ-2, call reserve.** Decided by the tech lead on 2026-10-24 (per the coordinator's brief): A, E adds the dormant `StructuredTask.reserved_calls` (default 0) in Task 12 and F sets the values.
3. **OQ-3, R16 scope.** Decided by the tech lead on 2026-10-24 (per the coordinator's brief): gate it behind `bounded_repair`, off for live review and conventions; the verifier sets it.
4. **OQ-4, `manifest_id`.** Decided by the tech lead on 2026-10-24 (per the coordinator's brief): `llm.verify` writes `manifest_id = null` until manifests are persisted.
5. **OQ-5, previous-output bound.** Decided by the tech lead on 2026-10-24 (per the coordinator's brief): 24 000 characters.
6. **OQ-6, carrying `LLM_VERIFIER_*` to staging.** Decided by the tech lead on 2026-10-24 (per the coordinator's brief): no deploy plumbing in E (compose, `env-file.sh`, CI); that is PR F.

## Gates (every task; run `uv sync` first, the worktree has no `.venv`; `df -h /tmp` before long runs)

```
uv run ruff check .
uv run ruff format --check .
uv run mypy . --exclude '^docs/reports/'
TEST_DATABASE_URL=postgresql+psycopg://app:app@localhost:5432/imp130e \
TEST_RABBITMQ_URL=amqp://app:app@localhost:5672/imp130e uv run pytest
```

Known local-only noise: 7 failures in `tests/test_deploy_monitoring.py` caused by a `~/.local/bin/sh` wrapper.
They pass with `~/.local/bin` off `PATH`. Use only the database and vhost `imp130e`. Never read any `.env`.
