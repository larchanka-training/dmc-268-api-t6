# Task list: #315 exact model-response cache

Base: `c89e838c` (`origin/main`).
Plan: [315-plan.md](315-plan.md).
Worktree: `D:\train\dmc-268-api-t6-wt-315`, branch `feat/315-review-cache`.

Every behavior task starts from a failing test in `tests/test_review_result_cache.py`.
That module is the file the issue's acceptance command runs. Gates for a task that
touches production code: `uv run ruff check .`, `uv run ruff format --check .`,
`uv run mypy .`, and `uv run pytest tests/test_review_result_cache.py`. The wiring
checkpoint and the final checkpoint also run `uv run pytest`.

## Planning gate

- [x] Human reviews and approves this plan before development starts.

## Task 1: Canonical review-result key, one test per part

**Description:** Add a pure function that builds the `review-result-v1` digest from
the identity in the plan (operation, repository id, model, prompt version, output
schema version, profile version, and the digest of the final messages). No Redis
and no gateway call. The rendered-message digest is the existing
`rendered_messages_sha256`. Code, policy, and rules are covered by changing the
message text one field at a time; model, prompt version, schema version, profile
version, and repository id are covered with the messages held fixed.

**Acceptance criteria:**

- [x] The failing tests below are written first and fail because the function does
      not exist, then pass.
- [x] Changing the message text that stands for code, for policy, or for rules each
      changes the digest. Changing model, prompt version, schema version, or profile
      version changes it while the messages stay the same.
- [x] Two identities that differ only by `repository_id` do not share a digest.
      `review` and `verification` do not share a digest.
- [x] Canonical JSON uses sorted keys, compact separators, and `ensure_ascii=False`.
      The Redis key prefix is `review-result:`.

**Verification:**

- [x] `uv run pytest tests/test_review_result_cache.py` — the new key tests pass.
- [x] Ruff, format check, and mypy are clean.

**Dependencies:** Planning approval.

**Files likely touched:**

- `app/modules/reviews/application/review_result_cache.py` (new)
- `tests/test_review_result_cache.py` (new)

**Estimated scope:** Small (2 files)

Failing tests to add first:

- `test_key_misses_when_code_policy_or_rules_text_changes`
- `test_key_misses_when_model_prompt_schema_or_profile_changes`
- `test_same_input_in_two_repositories_does_not_share_a_key`

## Task 2: Redis adapter that fails closed

**Description:** Add a Redis adapter behind a small port in the application module
from task 1. `get` returns the stored object or `None`. `set` stores the validated
object with TTL `int(BLOB_CACHE_TTL.total_seconds())`. A missing key, a connection
error, a timeout, malformed JSON, a non-object payload, and a payload over 256 KiB
do not raise. Oversize `set` writes nothing. Each command is wrapped in
`asyncio.timeout(0.2)`, and the module exports that 0.2 s socket timeout for the
client task 6 constructs. Logs name the failure type and do not include the payload.

**Acceptance criteria:**

- [x] The failing tests below fail against the missing adapter, then pass.
- [x] A round trip returns the same object, and the recorded TTL equals
      `BLOB_CACHE_TTL` in seconds and is not longer than that timedelta.
- [x] Unavailable Redis, a miss, and a corrupt entry all return `None` and do not
      raise.
- [x] A value whose UTF-8 JSON exceeds 256 KiB is not stored.

**Verification:**

- [x] `uv run pytest tests/test_review_result_cache.py`
- [x] Ruff, format check, and mypy are clean.
- [x] Manual check: the adapter is not referenced from `LlmGateway` or `worker.py` yet.

**Dependencies:** Task 1

**Files likely touched:**

- `app/modules/reviews/application/review_result_cache.py`
- `app/modules/reviews/infrastructure/redis_review_result_cache.py` (new)
- `tests/test_review_result_cache.py`

**Estimated scope:** Medium (3 files)

Failing tests to add first:

- `test_entry_round_trips_with_blob_cache_ttl`
- `test_miss_unavailable_redis_and_corrupt_entry_return_none`
- `test_oversize_value_is_not_stored`

## Checkpoint: After Tasks 1-2

- [x] `uv run pytest tests/test_review_result_cache.py`
- [x] `uv run ruff check .`
- [x] `uv run ruff format --check .`
- [x] `uv run mypy .`
- [x] Production reviews are unchanged: the gateway still has no cache argument.

## Task 3: Stable verifier candidate ids

**Description:** Stop minting `uuid4` for a verifier candidate. The id is the
SHA-256 hex of canonical JSON `{"index": <index in the raw findings list>,
"finding": <validated ReviewFindingV2 model_dump mode=json>}`. Assign it once the
finding has parsed, and keep it on the repaired candidate the way the uuid is kept
today. The id remains inside the verifier XML, so it remains part of the rendered
digest. Do not change `call_id`, the verifier schema, or prompt files.

**Acceptance criteria:**

- [x] The failing test below fails while the id is `uuid4` (two builds differ), then
      passes.
- [x] The same finding list yields the same ids and the same verifier user message
      on two builds. A one-field change in the finding, or a different index, yields
      a different id.
- [x] The id length is 64, within the verifier schema max. Existing reflection tests
      that supply their own ids still pass.

**Verification:**

- [x] `uv run pytest tests/test_review_result_cache.py tests/test_reflection_pass.py tests/test_worker_reflection_pass.py`
- [x] Ruff, format check, and mypy are clean.

**Dependencies:** None beyond planning approval. May run in parallel with tasks 2 and 4.

**Files likely touched:**

- `app/modules/reviews/application/execute_review.py`
- `tests/test_review_result_cache.py`

**Estimated scope:** Small (2 files)

Failing test to add first:

- `test_verifier_candidate_id_is_stable_for_the_same_finding`

## Task 4: Carry repository_id on the call context

**Description:** The claim join already loads `Repository`. Add optional
`repository_id` (default `None`) to `RunGuardSnapshot`, `ClaimedAttempt`, and
`RunCallContext`, set it from `repository.id` in `lock_for_claim`, and copy it in
`attempt_model_provider`. Existing constructors keep working. This is how a legacy
review, which has no policy snapshot, still gets a repository-scoped key.

**Acceptance criteria:**

- [x] A claimed attempt's call context carries the repository id from the guard
      snapshot. `None` stays `None` for callers that do not set it.
- [x] No migration and no change to the conventions key.

**Verification:**

- [x] `uv run pytest tests/test_review_result_cache.py` plus the existing claim/worker
      test module the new assertion lives beside if the new test cannot import the
      worker factory cleanly. Prefer the new assertion inside
      `tests/test_review_result_cache.py` by calling the same construction the worker
      uses, or a one-node test added to an existing worker test only if that file
      is already required — do not exceed the file list.
- [x] Ruff, format check, and mypy are clean.

**Dependencies:** None beyond planning approval. May run in parallel with tasks 2 and 3.

**Files likely touched:**

- `app/modules/reviews/application/handle_review_run.py`
- `app/modules/reviews/infrastructure/run_lifecycle_store.py`
- `app/modules/reviews/application/llm.py`
- `app/worker.py`
- `tests/test_review_result_cache.py`

**Estimated scope:** Medium (5 files)

Failing test to add first:

- `test_call_context_receives_repository_id_from_the_claimed_attempt`

## Task 5: Gateway hit and miss for review and verifier

**Description:** Give `LlmGateway` an optional result-cache port, default `None`.
For `review` and `verification` only, after the cancellation checkpoint and before
budget, FX, manifest, and transport, compute the key from the plan and `get`. A hit
that re-validates returns that object with empty usage, zero calls, the in-memory
`base_manifest`, no manifest row, and no `llm.call`. Record `llm.result_cache`
(`operation`, `cache: hit`) through a new best-effort method on `LlmCallTrace`.
A miss, a `None` cache, a missing repository id, or a value `task.validate` rejects
falls through to the existing loop. After a primary, retry, or primary-repair
acceptance, `set` the object under the original messages and the primary model.
Do not store a fallback success. Do not cache `conventions`. A failed `set` or a
failed hit-trace does not fail the run.

**Acceptance criteria:**

- [x] The failing tests below fail against an uncached gateway (the second call still
      hits the transport and the ledger), then pass.
- [x] Rerun of the same review messages and the same verifier messages writes 0
      `llm.call` records and appends 0 ledger events, and the second output equals
      the first. The hit trace tool is `llm.result_cache`, not `llm.call`.
- [x] A changed message (code, policy, or rules), model, prompt version, schema
      version, profile version, or repository id is a miss and a normal call.
- [x] Two repository ids do not read each other's entry.
- [x] A miss, a cache that raises, and a corrupt stored object each produce one
      normal model call. The run result is the model output, not `llm_invalid_output`.
- [x] A conventions generation is never served from this cache.
- [x] No database transaction is held across the provider call: `get` is before
      `_store_manifest` and the transport; `set` is after `record` and `record_call`
      return.

**Verification:**

- [x] `uv run pytest tests/test_review_result_cache.py`
- [x] `uv run pytest tests/test_llm_gateway.py tests/test_gateway_review_model.py tests/test_gateway_verifier_model.py`
- [x] Ruff, format check, and mypy are clean.
- [x] Manual check: with the cache left at `None`, existing gateway tests still pass
      unchanged in behavior.

**Dependencies:** Tasks 1, 2, 3, and 4

**Files likely touched:**

- `app/modules/reviews/infrastructure/llm/gateway.py`
- `app/modules/reviews/application/llm.py`
- `app/modules/reviews/infrastructure/llm_call_trace.py`
- `app/modules/reviews/infrastructure/llm/memory.py`
- `tests/test_review_result_cache.py`

**Estimated scope:** Medium (5 files)

Failing tests to add first:

- `test_rerun_of_review_and_verifier_writes_no_llm_call_and_no_usage`
- `test_each_key_part_is_a_miss_and_a_normal_call`
- `test_two_repositories_do_not_share_a_cached_answer`
- `test_corrupt_entry_and_unavailable_cache_call_the_model`

The rerun test drives `GatewayReviewModel.draft_review` and
`GatewayVerifierModel.verify` (or `generate` for both operations) with one fake
transport and one fake ledger, then asserts the finding payload is identical and
the verifier candidate ids in the cached decisions still match the second prompt.
That is the issue's "same finding set" check at the gateway boundary. A full
`ExecuteReviewRun` is not required if this test covers both operations.

## Task 6: Compose the short-timeout client in the worker

**Description:** When `settings.redis_url` is set, build a second Redis client from
that same URL with the 0.2 s socket timeouts from task 2, wrap it in the adapter,
and pass it to `build_gateway`. Leave the existing `Redis.from_url` client on the
token, AST, and tree path. When the URL is absent, pass no result cache. Close the
new client with the worker's exit stack.

**Acceptance criteria:**

- [x] The failing test below fails while `build_gateway` ignores Redis, then passes.
- [x] A configured URL constructs exactly one result-cache client with the 0.2 s
      timeouts and the gateway holds that adapter. An absent URL constructs none
      and the gateway cache is `None`.
- [x] The pre-existing Redis constructor arguments for the shared client are unchanged.

**Verification:**

- [x] `uv run pytest tests/test_review_result_cache.py`
- [x] Ruff, format check, and mypy are clean.
- [x] Manual check: `app/bootstrap/gitlab_reviews.py` is not edited; GitLab attempts
      use the process gateway this task wires.

**Dependencies:** Tasks 2 and 5

**Files likely touched:**

- `app/worker.py`
- `app/bootstrap/llm_gateway.py`
- `tests/test_review_result_cache.py`

**Estimated scope:** Medium (3 files)

Failing test to add first:

- `test_worker_gateway_uses_result_cache_only_when_redis_url_is_set`

## Checkpoint: After Tasks 3-6

- [x] `uv run pytest tests/test_review_result_cache.py`
- [x] `uv run ruff check .`
- [x] `uv run ruff format --check .`
- [x] `uv run mypy .`
- [ ] `uv run pytest`
- [x] A cached rerun writes no review or verifier `llm.call` and does not grow usage.

## Task 7: CVS §4.3, SD §10, and the run-result row

**Description:** Add the cache row to CVS §4.3 and SD §10: key (`review-result-v1`
as in the plan), storage (existing Redis, not a new server), TTL 7 days. State that
a hit skips the model call and `llm.call`, and that a miss, an outage, and a corrupt
entry are a normal call. Leave the "no assembled-context cache in v1" sentence in
place; this row is the model answer, not the assembled context. In SD §10, keep
«Результат прогона» as the per-run published result that delivery retry reads, and
make its "why" cell say that this result is not copied to another head or run by
findings hash. Point that cell at the new row for an identical final model input.
Narrow the "what we do not cache" sentence so it still forbids a findings-hash or
diff-only transfer and no longer says that every LLM answer across a rerun is
uncached. Add one sentence to PIPELINE_SPEC §2 naming `llm.result_cache` on a hit
and the absence of `llm.call` for that operation. Do not edit `contracts/openapi.yaml`
(`tool` is free text). Do not rewrite the conventions-key paragraphs.

**Acceptance criteria:**

- [x] CVS §4.3 has a row whose key, store, and TTL match tasks 1 and 2.
- [x] SD §10 has the same row, and «Результат прогона» no longer contradicts it.
- [x] The findings-hash prohibition is still stated in both documents.
- [x] PIPELINE_SPEC §2 mentions `llm.result_cache` and that a hit writes no `llm.call`
      for review or verification.

**Verification:**

- [x] Read the three edited sections against the plan's architecture decisions.
- [x] `uv run ruff check .` and `uv run ruff format --check .` if those docs are in
      the format scope; otherwise confirm the markdown tables still render.
- [x] No production code in this task.

**Dependencies:** Task 6, so the text matches the wired behavior.

**Files likely touched:**

- `docs/CONTEXT_AND_VERIFICATION_SPEC.md`
- `docs/SYSTEM_DESIGN.md`
- `docs/PIPELINE_SPEC.md`

**Estimated scope:** Medium (3 files)

## Checkpoint: Complete

- [x] `uv run pytest tests/test_review_result_cache.py` green
- [ ] `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy .`, `uv run pytest` green
- [x] The acceptance-criteria table in `315-plan.md` is satisfied
- [ ] Ready for review
