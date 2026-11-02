# Implementation Plan: exact model-response cache for review and verifier (#315)

## Overview

Cache the accepted model answer for a review call and a verifier call in the Redis
this process already uses. The key is a digest of the final provider messages plus
the repository, the model, and the prompt, output-schema, and profile versions. A hit
does not call the model, does not write `llm.call`, and does not add a `usage_events`
row; the run trace records the hit. A miss, a dead Redis, a timed-out command, and a
corrupt entry are an ordinary model call. The run does not fail and does not take
`llm_invalid_output` from the cache.

This is an optional accelerator, the same idea as the AST and tree caches: it is on
when `REDIS_URL` is set and absent otherwise. No new setting, dependency, migration,
or Redis server.

Spec: https://github.com/larchanka-training/dmc-268-api-t6/issues/315.
Base inspected: `c89e838c` (`origin/main`) in worktree `D:\train\dmc-268-api-t6-wt-315`,
branch `feat/315-review-cache`. Planning only; no production code in this step.

Out of scope, stated so a later task does not absorb them:

- Semantic cache, embeddings, or a vector store (ADR 0001; RAG v2 stays behind #146).
- A second Redis deployment. A second client to the same `REDIS_URL` is allowed.
- Copying findings from one run to another by findings hash. CVS §4.3 still forbids that.
- Redesigning the conventions, blob, AST, tree, or installation-token caches.
- Caching the conventions call. It already has its own store.

### What the code does today

- CVS §4.2 defines a canonical SHA-256 of the full conventions input. The snapshot
  path stores a narrower digest (`conventions_cache_key` in
  `app/modules/reviews/application/conventions.py`). That narrower key stays as it is.
  The review-result key is a new namespace, `review-result-v1`, built in the same
  canonical-JSON style. It does not exist yet.
- CVS §4.3 and SD §10 have no row for a model-response cache. SD §10 «Результат
  прогона» says a run result is not transferred across head/run. The prose under the
  table says LLM answers are not cached across head/run by a diff or findings hash.
- `RedisAstFactsCache` (`app/modules/reviews/infrastructure/redis_context_cache.py`)
  is the failure pattern to copy: `get`/`set` catch every exception, log it, and
  return a miss. TTL for blobs and AST facts is 7 days (`BLOB_CACHE_TTL`,
  `AST_TTL_SECONDS`). The shared `Redis.from_url` in `app/worker.py` sets no socket
  timeout.
- `LlmGateway.generate` fits nothing itself. `GatewayReviewModel.draft_review` and
  `GatewayVerifierModel.verify` render the final messages, then `generate` stores a
  manifest, calls the provider, and writes `usage_events` plus `llm.call` in their
  own short transactions after the HTTP call (`SqlAlchemyUsageLedger.record`,
  `RunTraceLlmCalls`). Conventions cache hits never enter `generate`.
- `_render_pr_meta` includes `head_sha`. An exact digest of the rendered messages
  therefore hits only when that input is identical, which for a review is a rerun of
  the same head with the same policy, rules, diff, and excerpts.
- Verifier `candidate_id` is `str(uuid.uuid4())` in `ExecuteReviewRun._run_reflection_pass`
  (`execute_review.py`, the loop over raw findings). The verifier prompt prints that
  id (`reflection_pass.py`, the `<candidate candidate_id=...>` element). The manifest
  `call_id` is also a fresh uuid, but it is not part of the provider message text.
  A rerun therefore builds a different verifier prompt today, so a digest of that
  prompt cannot hit until the candidate id is a pure function of the finding.

### Dependency graph

```
canonical key (pure)
    │
    ├── Redis adapter (fail closed, TTL, timeout)
    │
    ├── stable verifier candidate id          ─┐
    │                                           ├── gateway get/set, trace, usage
    └── repository_id on the call context     ─┘
                                                │
                                                └── worker composition (same Redis URL)
                                                            │
                                                            └── CVS §4.3 and SD §10
```

Tasks 2, 3, and 4 may run in parallel after task 1. Task 5 waits for all three.
Task 6 waits for task 5. Task 7 waits for task 6 so the docs name the behavior that
shipped.

### Acceptance criteria, mapped to tasks

| Criterion | Task |
| --- | --- |
| Rerun of the same head, policy, and model writes 0 `llm.call` rows for review and verifier and returns the same finding set | 3, 5 |
| A difference in code, policy, rules, model, prompt version, or schema version is a miss; one test per key part | 1, 5 |
| The same input in two repositories does not share an entry | 1, 4, 5 |
| Unavailable Redis, a miss, and a corrupt entry are a normal model call; the run does not fail and does not take `llm_invalid_output` from the cache | 2, 5 |
| A hit does not grow the usage ledger | 5 |
| TTL is at most the blob cache (7 days, SD §10) | 2 |
| `uv run pytest tests/test_review_result_cache.py` is green | every task; the module is the only new test file |
| CVS §4.3 and SD §10 describe the cache; the «Результат прогона» row agrees | 7 |

## Architecture Decisions

1. **Key.** One SHA-256 over canonical JSON (`sort_keys=True`, separators `(',', ':')`,
   `ensure_ascii=False`), the same encoding as `rendered_messages_sha256` and
   `conventions_cache_key`. Namespace `review-result-v1`. Fields:

   - `operation`: `review` or `verification` (conventions is never cached here).
   - `repository_id`: required. No id means do not read and do not write.
   - `model`: provider and model id of the primary route that the lookup is for.
   - `prompt_version`: on the snapshot path, `stage_prompt_checksum` for that
     operation (`review` or `verification`). On the legacy review path,
     `RunCallContext.prompt_version_id`. The system-prompt text is also inside the
     rendered digest, so a text change misses even when the version string does not.
   - `output_schema_version`: the pinned `execution_profile.response_schema_version`
     for a review. For the verifier, the `$id` of
     `review/schemas/verifier-output.schema.v1.json` (the review schema version is a
     different document and must not stand in for it). Legacy review uses the
     response-schema name already passed on `StructuredTask.schema`.
   - `profile_version`: `model_profile_version(execution_profile)` when a snapshot
     pins one (it already covers counter, budget, recovery, temperature, and routes).
     Legacy review uses a canonical digest of the primary `ModelProfile` fields that
     change the call: provider, model, context window, max output tokens, characters
     per token.
   - `rendered_input_digest`: `rendered_messages_sha256` of the **original** task
     messages, not a repair or fallback envelope.

   Code, policy, rules, DiffMap, and the shown sources are not extra columns. They
   are inside the rendered messages (policy documents, service-default rules, changed
   files, excerpts, RAG blocks, `head_sha`). CVS §4.3's "policy, DiffMap, sources,
   budgets, and build versions" is therefore the rendered digest plus `profile_version`,
   `prompt_version`, and `output_schema_version`. Tests still change code, policy, and
   rules one at a time and expect a miss, and they change model, prompt version,
   schema version, profile version, and repository id with the messages held fixed.

   Redis key: `review-result:{digest}`. The digest already contains `repository_id`,
   so the prefix only avoids `ast:`, `tree:`, and `token:`.

2. **What is stored.** The validated JSON object the gateway would return (`output`
   only), under that key, only after the primary model accepted it (a first answer,
   a retry, or a repair of the primary). The repair envelope is not the lookup key:
   a rerun sends the original messages, and the cached value is the accepted answer
   for those messages. A fallback success is not stored. Lookup uses the primary
   model only, before any provider call. Temperature stays 0, so two primary answers
   for one key are interchangeable; last write wins. Do not store provider raw
   bodies, reasons, or usage.

   Payloads whose UTF-8 JSON is larger than 256 KiB are not written. SD §10 already
   keeps Redis blobs at that size and puts larger blobs in Postgres. This cache does
   not add a table. An oversized answer is a successful call that the next run simply
   misses. That bounds eviction of installation tokens, AST facts, and trees in the
   128 MB `allkeys-lru` instance.

3. **Where get and set sit.** Inside `LlmGateway.generate`, only for `operation` of
   `review` and `verification`.

   - After one cancellation checkpoint, before budget, FX, call-slot, manifest, and
     transport. A hit must not be rejected because the next call *would* have cost
     money.
   - On a hit: return `GatewayResult` with the cached object, empty `usage`,
     `calls=0`, `accepted_manifest` set to the task's already-built `base_manifest`
     (so `GatewayReviewModel`'s assertion still holds), and `accepted_manifest_id=None`.
     Do not call `_store_manifest`, the ledger, or `record_call`. Do not increment
     the attempt's call counter.
   - On a miss: the existing loop is unchanged. After the primary result is accepted,
     `set` runs only once the ledger and `llm.call` writes of that real call have
     finished. Those writes already commit in their own short transactions. The Redis
     write is not inside them, and no database transaction is open across the HTTP
     call. A failed `set` is logged and ignored.
   - Re-validate a hit with `task.validate` before returning it. `InvalidAnswer`,
     bad JSON, a non-object, or any other read error is a miss, then a normal model
     call. The cache must not raise `llm_invalid_output` or `InvalidReviewOutput`.

4. **Trace.** A hit writes one run action, tool `llm.result_cache`, request
   `{"operation": "<review|verification>", "cache": "hit"}`, response `{}`. It is not
   an `llm.call`. The write goes through `LlmCallTrace` in its own short transaction,
   best-effort: a trace failure is logged and the cached answer is still returned.
   The response body is not copied into the trace; `llm.review_output` already stores
   the accepted review answer. OpenAPI leaves `tool` as free text, so the contract
   file stays unchanged. PIPELINE_SPEC §2 gains one sentence in the docs task so the
   step catalog names the tool.

5. **Verifier candidate id.** Replace `str(uuid.uuid4())` with the SHA-256 hex of
   canonical JSON `{"index": <raw finding index>, "finding": <ReviewFindingV2 dump>}`
   (`model_dump(mode="json")`, `sort_keys=True`, the same separators). The hex digest
   is 64 characters, which is the schema max. Compute it once the finding has
   validated, from the raw list index, and keep that id across attribution repair
   (the repair already copies `candidate_id`). Do not drop the id from the prompt or
   from the digest: CVS requires the final input in the key, and the id is part of
   that input. Two identical inputs then render the same verifier XML, so the
   verifier entry can hit, and the cached `decisions[].candidate_id` still match.
   Do not change `call_id`. It is not in the message text. Do not edit the verifier
   schema or the prompt files: the schema does not enforce a UUID format, and a
   prompt edit would change the pinned checksum for no behavioral gain.

6. **Repository id.** `PolicySnapshot.repository_id` is the id on the snapshot path
   (review and verifier). Legacy review has no snapshot, and `RunCallContext` has no
   repository today. Add `repository_id: UUID | None = None` at the end of
   `RunGuardSnapshot`, `ClaimedAttempt`, and `RunCallContext`. The claim query
   already joins `Repository`; pass `repository.id` through. `attempt_model_provider`
   copies it onto the call context. The gateway uses the snapshot id when a snapshot
   is present, otherwise the context id. If neither is set, skip the cache. Two
   repositories cannot share an entry, including a legacy review.

7. **Redis failure and timeout.** The adapter matches `RedisAstFactsCache`: any
   exception from `get` or `set` is a miss or a dropped write, with a warning that
   names the operation and the exception type and does not log the payload (it is
   customer code). Also bound the wait. redis-py's default can block for seconds
   (#209). Do not change the shared `Redis.from_url` used by tokens, AST, and the
   tree. Construct a second client from the same URL with
   `socket_connect_timeout` and `socket_timeout` of 0.2 seconds, and wrap each
   command in `asyncio.timeout(0.2)` so a fake that never returns is still a miss
   in tests. `REDIS_URL` unset means the gateway is given no cache, as the AST cache
   is given no client.

8. **TTL.** `int(BLOB_CACHE_TTL.total_seconds())` (7 days). Not a second literal.
   The test asserts that value and that it is less than or equal to the blob TTL.

9. **No new dependency.** `redis` is already in `pyproject.toml`. The port is a
   small `Protocol` next to the pure key function. The worker keeps owning
   `commit()`; this feature adds no repository.

## Task List

### Phase 1: Foundation

- [x] Task 1: Canonical review-result key, one test per part
- [x] Task 2: Redis adapter that fails closed

### Checkpoint: Foundation

- [x] `tests/test_review_result_cache.py` green
- [x] `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy .` green
- [x] Key and adapter are unused by the gateway; a review still always calls the model

### Phase 2: Wiring

- [x] Task 3: Stable verifier candidate ids
- [x] Task 4: Carry repository_id on the call context
- [x] Task 5: Gateway hit and miss for review and verifier
- [x] Task 6: Compose the short-timeout client in the worker

### Checkpoint: Wiring

- [x] A second identical review and verifier generation writes no `llm.call` and no usage
- [ ] Full gates green: `uv run ruff check .`, `uv run ruff format --check .`,
      `uv run mypy .`, `uv run pytest`

### Phase 3: Docs

- [x] Task 7: CVS §4.3, SD §10, and the run-result row

### Checkpoint: Complete

- [x] Every acceptance criterion in the table above is checked off in `315-todo.md`
- [ ] `uv run pytest tests/test_review_result_cache.py` green, and the four gates green
- [ ] Ready for review

## Risks and Mitigations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| A fresh `candidate_id` makes every verifier digest unique, so the AC rerun never hits | High | Task 3 before the gateway rerun test. The id stays inside the rendered input. |
| A corrupt entry is parsed as the model answer and becomes `llm_invalid_output` | High | Re-validate with `task.validate`; any failure is a miss, then a real call. Test it. |
| Caching the repair transcript under the repair messages, so the rerun misses | High | Lookup and store use the original messages only. |
| Returning a hit with `accepted_manifest=None` trips the snapshot-path assertion in `GatewayReviewModel` | Med | Return the in-memory `base_manifest` and do not insert `context.manifest`. |
| A 128 MB shared Redis evicts tokens and AST facts, or is empty after a restart | Med | Miss is harmless. Do not write values over 256 KiB. TTL matches the blob cache, not longer. |
| A hung Redis adds the client default wait to every review | Med | Dedicated client, 0.2 s socket timeouts, plus `asyncio.timeout` in the adapter. The shared client is unchanged. |
| A key without `repository_id` serves repo A's findings to repo B | High | The field is mandatory. Missing id disables the cache. Test two ids. |
| Cache get/set inside a database transaction that also talks to the provider | High | Get happens before manifest, ledger, and HTTP. Set happens after those short transactions have committed. |
| Fallback answer stored under the primary model and replayed when the primary would now succeed | Med | Do not store fallback successes. Lookup is primary-only. |
| Deterministic ids surprise a test that expects a UUID string | Low | Schema max is 64 and there is no `format: uuid`. The worker verifier fake reads ids back from the prompt. Do not change schema or prompts. |

## Open Questions

None. The decisions above are fixed from the current code and the issue. Nothing
here needs a human answer before implementation.
