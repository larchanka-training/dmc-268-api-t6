# Implementation plan: switching on the policy-snapshot review path (#130, PR F)

## Status and baseline

Planned 2026-10-25. Decided 2026-10-25: the tech lead answered TL-1 to TL-13 (section "Decided by the
tech lead", per the coordinator's brief).
Issue: https://github.com/larchanka-training/dmc-268-api-t6/issues/130 (assignee axyi). F is the
switch of phase 2 (CVS §12 п.3, `docs/CONTEXT_AND_VERIFICATION_SPEC.md:1876-1880`; the brief's
`:1743-1747` is stale and now points into §10.5). Task checklist: [130-snapshot-path-todo.md](130-snapshot-path-todo.md).

- Base: PR C (#324, #168 part 3, not merged yet) head `0aa29d6b` on `main` `9243fd67`, which
  contains #227. F lands after C. The Alembic head is `20261024_0039`. (Planned on `703c05ec` over
  `d08e6c96`; batch 1 was rebased onto the new base.)
- Worktree: `dmc-268-api-t6-130f`. Branch: `feat/130-snapshot-path` (PR F1). Test DB and vhost: `imp130f`.
- Every `file:line` was checked against `703c05ec` on 2026-10-25 (the planning base). **Suggested** marks a name the
  executor may change.
- Commit subjects end with `(#130)`. The #168 part 6 items (F3) carry `Refs #168` in the body
  unless the tech lead keeps them under #168.

## Context

The snapshot path is built but cannot be reached. Two locks keep it off:

1. No snapshot reaches the prompt.
   - `ReviewPromptInput` is built without `policy_snapshot`
     (`infrastructure/review_prompt_repository.py:81-92`).
   - The stage payload is discarded (`application/process_run.py:245`).
   - The conventions input carries no snapshot (`infrastructure/run_repository.py:457-487`).
2. The worker never builds a `ReflectionPass` (`app/worker.py:281-284`), and `execute_review.py:219-223`
   refuses a snapshot that has none.

D now pins all three prompts when a run is created (`infrastructure/webhook_runs.py:71-90`). E added the
verifier adapter (`infrastructure/llm/models.py:127-177`) and maps v4 to ReviewOutput v2
(`llm/answers.py:47-48`).

Two facts about deploys:

- `seed_prompts` activates **max(DB rows ∪ files)** of each key (`app/bootstrap/seed_prompts.py:130`).
- It runs in the one-shot `bootstrap` job, before the services restart (`deploy/compose/staging.yml:145-148`).

The legacy path reads the prompt text by `Run.prompt_version_id` (`review_prompt_repository.py:42`). It
validates against v1 when no snapshot is attached (`llm/models.py:183-188`). That is the **v4/legacy hazard**.

## Split and recommendation

| PR | Branch (suggested) | Content | Live change | Merge after |
|---|---|---|---|---|
| **F1 prep** | `feat/130-snapshot-path` | path selector; lock 1 lifted behind it; R14; R2; R10 (review, conventions, verifier); manifest table and `context.manifest`; R12; #247 p3; R1/R18; R5; R7; R19; E-1, E-2, E-3, E-7 (time), E-9, E-12; P6-4 | **none**: every new branch needs a run bound to a v2-contract review prompt, and none exists before F2 | C |
| **F2 switch** | `feat/130-snapshot-switch` (on F1) | `review.system.v4.md`, its `PINNED_PROMPT_CHECKSUMS` entry, flipped pin, seed and golden tests: **one commit** | every new run takes the snapshot path, with the verifier, reserved slots, R5 and R7 | F1 merged **and deployed** |
| F3 | `fix/168-part6-trace` | P6-1 (covers P6-2), P6-3, P6-5, P6-6 | trace and check-run text only | #304 |
| F4 | `chore/130-verifier-env` | `LLM_VERIFIER_*` deploy wiring | none while the variables are unset | before phase 3 |
| F5 | `feat/130-coverage-none` | R4, gated like F1 | dormant until F2 | #283 |

**Recommendation: F1 + F2 (+ F3, F4, F5).** Lock 1 and R14 go into F1, not into the switch, for three reasons:

1. **Bootstrap window.** The seed makes v4 active while the old services still run. Every webhook-worker
   then pins v4 (`webhook_runs.py:73-77` reads the active row), and the old review-worker claims those
   runs. That worker must already have the full snapshot path. Otherwise it runs the v4 text with the v1
   validator, or fails terminally.
2. **Rollback.** The seed never deactivates v4 (see Context). Rolling F2 back to F1 is safe only if F1
   serves v4 runs end to end. The rollback floor after F2 is F1.
3. **Atomic by construction (CVS §12 п.3).** Enabling is one commit, the v4 file. Every gated behaviour
   turns on together with the first run bound to v4.

**#144 stack points.**
- PR3a on F1, once Tasks F1-4 (R2), F1-5 (R10), F1-6 (manifest) and F1-13 (R19) are on the branch. It
  stays dormant while `RETRIEVED_PROMPT_VERSIONS` is empty (`application/rag_index.py:15`; F does not add v4).
- PR3b on F2: v5 over v4, plus its `answers.py` mapping, in one commit.

## Design decisions

**DD1. The path selector.**
- New module `application/review_path.py` (suggested) holds
  `SNAPSHOT_REVIEW_PROMPT_VERSIONS = frozenset({"v4"})`. `answers.py` `_V2_REVIEW_PROMPT_VERSIONS` comes
  from it, so #144 adds v5 in one place.
- The bound version is `v{PromptVersion.version}` of `Run.prompt_version_id`. It is the existing join at
  `review_prompt_repository.py:42`, as in `get_pins` (`policy_snapshot_unit_of_work.py:144`).
- Bound version not in the set → **legacy**: no snapshot in the prompt, no verifier, no manifest rows,
  `reserved_calls=0`, the v1 conventions key, the 4-char stage counter. This holds even when the run has
  a ready snapshot (in-flight and pre-0039 runs).
- In the set, snapshot `is_ready_for_llm` and `pins_review_prompt(run.prompt_version_id)` → **snapshot**.
- In the set, anything else → `policy_snapshot_integrity_error` before any model call. "Anything else"
  means: no context stage, NULL pins (D-Q2/Q4), not ready, the D-1 shape, or a provider the stage does
  not serve (TL-13, refined). The error is terminal (`run_failures.py:41`).
- Only a `review.system` row picks the path: the binding carries `PromptVersion.key`, and a v4 row of
  another key bound as the review prompt keeps the legacy path (fix round of batch 1).
- **Where the refusal fires: before the conventions call.** `process_run.prepare` calls conventions
  (`:250-266`) before `ExecuteReviewRun` reads `ReviewPromptInput`, so the check sits in `stage.begin`
  right after the snapshot is created (next to R7, `review_context_stage.py:407-410`). A composition without
  a stage exists only in tests and eval (`github_adapters` always wires one, `worker.py:668-678`); there
  `process_run.prepare` refuses a v4-bound run before conventions, using the bound version returned by
  `get_run_conventions_input`. `execute_review` re-checks as defence in depth.
- Fix round of batch 2: the stage's check at `begin` keys on `_STAGE_PROVIDER`, so it passes a v4 run of
  any provider. `process_run.prepare` reads `get_run_conventions_input` once, before `stage.begin`
  (the check needs only the run's binding, so a policy error cannot mask the refusal; re-review of
  `d2d62794`) and before the diff fetch, and refuses a v4 run of an unserved provider there in both
  compositions; without a stage it also refuses the missing snapshot. So the refusal comes before
  `policy.load`, any diff read, diff write or model call. The same read feeds the conventions call.
  Accepted effect: a missing active conventions prompt now fails with `internal_error` before
  `policy.load`, so a run that `stage.begin` or `stage.run` would have refused or skipped with a
  `policy_*` code fails with `internal_error` first when that prompt is missing.
- One property, `stage.snapshot_for_llm` (snapshot or None), feeds `execute_review`, `process_run`
  (conventions) and the stage itself (R7, R19).
- As built in F1-2: one pure function, `snapshot_for_review(ReviewBinding, snapshot)`, decides at all three
  sites. `ReviewBinding` is the pinned review prompt id, its `v{version}` and the provider of the run's origin
  installation, matched exactly (`"github"`, never coerced). `get_run_conventions_input` and
  `review_prompt_repository` read it from `Run.prompt_version_id` and `Run.origin_provider_installation_id`;
  `RunConventionsInput` and `ReviewPromptInput` carry it as `review_binding` (not just the version: the
  provider is needed for TL-13 and the id for the pins check). The stage reads the id and version off its
  snapshot's prompt bindings, which C re-checks against the run's pins on every read, and the provider it
  builds the diff map with (`_STAGE_PROVIDER`).
- TL-13 on this base: the run carries a provider (its origin installation), but the stage's input
  (`RunVcsInput`) does not, and `get_run_vcs_input` stays untouched because #220 adds the provider there. So
  the stage keys on `_STAGE_PROVIDER` (GitHub, one constant with the `build_diff_map` call); #220 swaps both to
  the run's provider and extends `SNAPSHOT_REVIEW_PROVIDERS` once the GitLab path also passes the
  `ReflectionPass`. TL-13 refined (fix round of batch 1): a v4 run whose provider is not served (GitLab, any
  other name, or no origin installation) is refused with `policy_snapshot_integrity_error` before any model
  call; it never falls to legacy, which would validate the v4 answer with the v1 contract. No catalog code
  fits better: the refusal is "the snapshot path cannot be served", like the other refusals. Pinned at the
  selector, the processor with and without a stage (`require_served_provider`,
  `require_servable_without_snapshot`, before the diff) and `execute_review`.

**DD2. R2 slot (#144 Q2, Q3).**
- The stage keeps its payload as a `payload` property (`review_context_stage.py:514-536`) instead of
  dropping it at `process_run.py:245`.
- `ReviewContext` (`prompt_builder.py:166-177`) gains `context_blocks: tuple[ContextBlock, ...] = ()`
  (the payload's L2–L4) and `retrieved: tuple[RetrievedBlock, ...] = ()`. L1 stays `changed_files`,
  which the coordinate checks read. F fills `retrieved` only when `has_retrieved_section(...)` is true,
  so never in F; #144 renders it.
- Rendering, snapshot path only: after the changed files, one numbered excerpt per block, carrying
  `block_id`, path, revision, side and level. Manifest blocks get role `source_excerpt` or `ast_excerpt`
  with `shown_ranges`.
- Fit: `fit_review_context` (`prompt_budget.py:440`) drops `context_blocks` before it trims changed
  files, in the reverse of the CVS §4.1 selection order (L1 → L4 → RAG → L2 → L3): L3, then L2, then L4,
  each level from its last block. #144 owns the rule for `retrieved` (ADR order L1 > RAG > L2/L3).
  - As built in F1-4: the first draft said L4, L3, L2, which would drop the L4 signatures that CVS
    §4.1 never lets RAG, L2 or L3 displace. A binary search over the number dropped counts each
    candidate prompt whole with the fit counter, never with the stage's `ContextBlock.tokens`.
- P6-4: on the snapshot path, the payload's `ignored` omission count becomes one counter line
  (RULES §5.3) instead of a silent drop (`execute_review.py:199-204`).
  - As built in F1-4: `<excluded_files count="N" />` after `<omitted_files>`, only when N > 0, so the
    path list and its length accounting stay as they are. After a caught build failure (no payload)
    there is no counter and no excerpt.

**DD3. R10: one fit point per call.**
- **Review.**
  - `review_with_gateway` (`llm/models.py:181-233`) becomes the only fit. On the snapshot path the budget
    is `send_budget_for(profile, review route window)` and the counter is the snapshot's.
  - Then the manifest, with per-block tokens, then `verify_pre_send_guard` and the provenance guard.
  - It returns `ReviewReply(output, context, manifest)` (suggested; modelled on E's `VerifierReply`).
    `execute_review` still accepts a `Mapping`, so the 54 `draft_review` fakes keep working.
  - On the snapshot path `execute_review` drops `:241-273` and uses the reply's context and accepted
    manifest for coordinates, attribution (T-a) and the verifier. It keeps the readiness and version
    checks and `verify_verifier_mandatory_budget` (`:245`).
  - `GatewayResult` gains `accepted_manifest`, because repair and fallback change `call_id` and repair
    adds a block (`gateway.py:336-378`).
- **Conventions (R18).** On the snapshot path the pre-fit manifest and guard (`conventions.py:378-384`)
  go away. `GatewayConventionsModel` (`llm/models.py:91-125`) fits on `send_budget_for`, then builds the
  manifest, then guards.
- **Verifier.**
  - `VerifierModel` gains an optional `max_prompt_tokens()`. `GatewayVerifierModel` returns the verifier
    route window through a new `LlmGateway.max_prompt_tokens(engine, route)` (`gateway.py:244-247`).
  - `ReflectionPass` passes it to `send_budget_for` at `reflection_pass.py:512` and `:621`.
  - E-9: `send_budget_for` stops reading `os.environ` (`prompt_budget.py:164-167`); callers pass the
    window. All of its callers are on the snapshot path.
  - As built in F1-7: the window lives on a separate runtime-checkable `VerifierWindow` protocol,
    not on `VerifierModel` (about 28 verifier fakes would need it). `ReflectionPass` reads it when
    it verifies and applies it at the fit, the pre-send guard and, through
    `ReflectionPass.max_prompt_tokens()`, the mandatory verifier budget that `execute_review`
    checks before the review call (otherwise that check passes and the verifier overflows after
    a paid review). The verifier manifest counts every block with the pinned counter.
  - Fix round of the F1-7 review: the fit counted the code block alone against the budget minus
    the mandatory part, while the guard counts the system and the user message apart, with the
    newlines that join the user message; so in about 2-6 % of windows the guard refused a fitted
    prompt by 1-19 tokens. The fit now counts the whole prompt exactly as the guard does
    (`build_verifier_prompt` renders the user message through the same function for both).
    `ReflectionPass` builds its `VerifierPromptBuilder` (which reads the prompt file) when it
    first verifies: the worker gives every attempt a pass, and a legacy attempt never verifies.
- **Verifier excerpts** (decided by the tech lead on 2026-10-25, during F1-7): the verifier gets
  the L2–L4 blocks the candidates cite as evidence (CVS §7.1), rendered after the changed files
  inside `<code_context>`, and its manifest names exactly the shown blocks. An uncited excerpt is
  never sent. Fit order inside the verifier window: the candidates' own diff lines, then the
  cited excerpts (whole or not at all, in payload order), then the other diff lines; a left-out
  excerpt means `insufficient_context` from the verifier. Before F1-7 the verifier manifest
  listed every payload excerpt without rendering it.
  - Fix round of the F1-7 review: the order held only per file (the diff fit filled the files one
    by one, so an earlier file's other lines crowded out a later file's candidate lines). The fit
    (`_fit_verifier_code`) now runs in three steps over all files: every candidate's own lines,
    then the cited excerpts (one that does not fit leaves room for the next), then the other lines
    file by file, each step probing with what the earlier ones kept.

**DD4. Manifest persistence.**
- Migration `20261025_0040_context_manifests`, `down_revision = "20261024_0039"`. #283 uses
  `20261024_0039` too: whichever of the two merges second renumbers.
- Table `context_manifests`:
  - `id` uuid PK (= `manifest_id`); `run_id` FK `runs` ON DELETE CASCADE; `attempt`; `call_id` UNIQUE;
  - `stage` CHECK (conventions | review | verification);
  - `recovery_kind` CHECK (none | retry | repair | fallback);
  - `parent_call_id` NULL-able; `policy_snapshot_id` NOT NULL, FK `run_policy_snapshots.snapshot_id`
    (UNIQUE) ON DELETE CASCADE (CVS §8.3; decided by the tech lead on 2026-10-25, fix round of batch 2);
  - `digest` char(64), the SHA-256 of the canonical blocks JSON; `block_count`; `input_tokens`;
  - the CVS §5.1 identities, NOT NULL char(64), known before the call (same decision):
    `rendered_messages_sha256` (SHA-256 of the canonical JSON of the exact provider messages, in
    order), `prompt_checksum` (the stage's checksum in the snapshot's prompt bindings) and
    `model_profile_version`. CVS defines no format for the last one; it is the SHA-256 of the
    canonical JSON of the snapshot's pinned `execution_profile` as stored (`pinned_to_json`), so it
    changes with any pinned model, route, counter or profile version and stays the same on a retry;
  - `blocks` jsonb; `omissions` jsonb DEFAULT `'[]'`; `schema_version` DEFAULT 1; `created_at`;
  - index (`run_id`, `attempt`).
- `blocks` stores every `ManifestBlock` field (`policy_snapshot.py:351-365`) plus three additive
  defaults: `tokens`, `repository_id`, `truncated`.
  - Any `_ROLE_KIND_MAP` role (`prompt_budget.py:266-277`) round-trips.
  - So #144's `retrieved_excerpt` with side, revision and `shown_ranges` needs no schema change.
  - This table is the candidate checkpoint #144 agreed to.
- **Writer: the gateway.** Before each provider call of a snapshot-path task (base, repair or fallback;
  a retry reuses the row):
  - One short transaction (backend.md §3.3: the repository flushes, the UoW commits, no transaction
    around the provider call) inserts the row and the run_action `context.manifest`.
  - Request `{call_id, stage, policy_snapshot_id, diff_snapshot_id: null}`, response
    `{manifest_id, digest, block_count, input_tokens}` (PIPELINE_SPEC:174).
  - If the write fails, no provider call is made (`internal_error`, retryable).
  - As built in F1-6: the store is an optional `LlmGateway(manifest_store=…)`, wired by `build_gateway`;
    without it nothing is written and the E-3 keys carry `manifest_id: null`. The response also has
    `tokens_by_role` (#144). Blocks carry `tokens` only from the review manifest (`build_manifest` with
    the snapshot's counter) and the repair's `previous-output` block; the conventions and verifier
    builders count in F1-10 and F1-7 (the verifier's are counted since F1-7), so until then their
    rows report no tokens for their blocks.
    The step records `duration_ms: 0` (its own write is the transaction).
  - Fix round of batch 2: the call's slot (`attempt.calls`) is claimed before the manifest write
    awaits, so two generations of one attempt cannot both pass the cap; the step is inserted before
    the row, so its `SELECT … FOR UPDATE` on the run precedes the row's FK lock (no deadlock between
    two writers of one run).
- **E-3.** For snapshot-path tasks, the `llm.call` request (`application/llm.py:141-155`) adds `call_id`,
  `stage`, `policy_snapshot_id` and `manifest_id`. If #227 merges first, its `operation` key stays and
  `stage` mirrors it.
- **`llm.verify.manifest_id`** (`execute_review.py:531`) is the accepted verifier manifest, carried
  through `VerifierReply` and `VerificationRecord`.
- No field is added to `ExecutionProfile` or `PromptBindings`: C re-checks the stored JSON.

**DD5. Conventions on the snapshot path (R1, R18, R5).**
- The input uses the pinned `Run.conventions_prompt_version_id`, not the active row
  (`run_repository.py:457-487`).
- The snapshot is passed explicitly; the `getattr` at `process_run.py:258-259` goes (T-c).
- AGENTS.md text and sha come from the snapshot. `fetch_agents_md` (`conventions.py:341`), the unchecked
  second read named in R1, is skipped.
- R5: a v2 digest, built over the current `policy_key` value so the rename and copy taint stays in the key,
  goes into the existing `policy_key` column (`conventions_unit_of_work.py:26-39`). No
  migration; legacy rows never match (TL-7).

**DD6. R7.** In `stage.begin`, after the snapshot (`review_context_stage.py:407-410`), on the snapshot
path only: `verify_policy_budget(snapshot, counter, max_policy_tokens=profile.max_policy_tokens)`. It
raises `policy_prompt_budget_exceeded` (terminal), so neither the conventions nor the review model is called.

**DD7. R19 and T-d.** On the snapshot path the context build counts with
`token_counter_for(profile.counter_version)` (`prompt_budget.py:97-107`), not the 4-char
`EstimatedTokenCounter` (`review_context_stage.py:73-77`, built at `worker.py:558`). Legacy runs keep 4 chars.

**DD8. Recovery on the snapshot path** (all keyed on `task.policy_snapshot is not None`):
- E-1: `reserved_calls` (TL-2);
- E-12: `bounded_repair=True`;
- E-7: time reserve (TL-6).

**DD9. R14.**
- `attempt_provider_factory` (`worker.py:709-734`) builds `ReflectionPass(GatewayVerifierModel(gateway, run))`
  for each attempt. `AttemptReviewProvider` exposes it, and `process_review_run` passes it to `ExecuteReviewRun`.
- An `UnconfiguredModel` attempt gets no `ReflectionPass`; a legacy run fails with `llm_unavailable`
  at its first model call, as today. A v4 run on such a worker fails earlier: its snapshot carries
  the unpinned execution profile, so it is not ready and the selector refuses it with
  `policy_snapshot_integrity_error` before any call (F1-2).
- The warning at `execute_review.py:275-279` is removed; it would fire on every legacy run.
- As built in F1-7: the queue pipeline takes the pass off the `AttemptReviewProvider` before
  `CheckpointedProvider` wraps it; an injected provider (tests) runs without one. The verifier
  call does not pass `CheckpointedProvider`'s attempt checkpoint: the gateway's own checkpoint
  checks cancellation before every call and refuses a call without a whole call timeout before
  the deadline, which covers T11.
- The GitLab composition of #220 has to pass the same argument (Coordination).

**DD10. Verifier output.**
- E-2: the verification `llm.call` response is stored without `decisions[].reason` (TL-3).
- #247 p3: `_repair_attribution` (`findings_post_processor.py:259-272`) runs before the verifier.
- R12: `allowed_v2_rules=None` when no verifier call happened (`execute_review.py:449-461`).
- As built in F1-8: the repair runs after lint and dedupe, on the run's rule names (the same
  `Run.rule_version_id` rules the publisher reads). On the v2 contract the parse already refuses a
  rule name outside `.review/rules.md`, `AGENTS.md` and `service-defaults:<ID>`, and pass 3 drops a
  name whose document is not in the manifest; so what the repair changes before the verifier is a
  body that claims a custom instruction while `rule_name` is null: the prefix goes, the verifier
  decides. The repair's `body_only` routing of such a finding is not carried to publication (the
  publisher no longer sees the prefix); review attention for F2.
- As built in F1-7, fail-closed after its review (decided by the tech lead on 2026-10-25): the
  verifier task carries `redact_answer`, and the gateway stores a redacted body as an allow-list:
  the provider metadata at the top level (`id`, `object`, `created`, `model`, `provider`,
  `system_fingerprint`, `usage`) and, for every choice, `index`, `finish_reason` and a message of
  `role` and the answer text (list-form content joined). The answer text of a valid verifier answer
  is stored as `candidate_id`, `status`, `evidence_refs` and `requirement_refs` of each decision;
  any other text is withheld whole. Reasoning, tool calls, refusals, logprobs and the legacy
  `text` are dropped. Review and conventions responses are stored as sent. A `RunFailure` that is
  not a provider error (a failed manifest write: `internal_error`) and a cancellation now pass
  through `ReflectionPass` instead of becoming `verification_unavailable`; provider errors keep
  their `verification_*` mapping. `llm.verify.manifest_id` is `GatewayResult.accepted_manifest_id`.

## Hazard, D-1, in-flight runs, deploy order (D-2)

**Hazard.** Three measures close it:
- DD1 fails closed for a v4 run without a ready snapshot.
- F2 merges only after F1 is deployed.
- A runbook line: no image older than F1 may be deployed once v4 is seeded. Such an image would run v4 on
  the legacy path, because the seed keeps v4 active.

Tests: F1-2 shows a v4-bound run without a context stage gets `policy_snapshot_integrity_error` with
0 `llm.call`. F1-14 runs a v4 run through F1's worker composition, end to end.

**D-1.** `get_snapshot` has two callers: the stage (`prepare_policy_snapshot.py:202`) and
`create_snapshot` (`policy_snapshot_unit_of_work.py:316`).
- So C's terminal error reaches only retries of runs created during D's deploy window. Those runs were
  terminal long before C or F deploys.
- (a), the default: keep C's behaviour. A test pins that it fails before any model call. The F1 PR body
  carries a read-only staging query: active runs whose snapshot lacks `conventions_id` while
  `Run.conventions_prompt_version_id` is set.
- (b), only if that count is above 0: `verify_snapshot_pins` accepts exactly "stored review-only bindings,
  run pins all three" as historical. The snapshot is then not ready, and a v3-bound run goes legacy (TL-10).

**At the F2 deploy.**
- Queued and running runs pinned to v3 or lower finish on the legacy path, retries included.
- Pre-0039 runs have NULL conventions and verifier pins and go legacy.
- Runs created after the seed pin v4 and take the snapshot path on F1/F2 workers.
- A v4 run with a NULL pin (a key had no active row) fails terminally before any call.
- `Repository.prompt_version_id` overrides at v3 or lower stay legacy. Nothing in `app/` writes that
  column; `webhook_runs.py:71` only reads it.

**Deploy order.**
1. C is deployed.
2. F1 is merged and deployed. Bootstrap applies 0040 (an additive table); the seed is a no-op.
3. Check that the review worker runs the F1 image.
4. F2 is merged and deployed. Bootstrap seeds v4 as the active version. F2 does not merge while #220
   creates GitLab runs and the stage does not take the provider from the run: such a run would pin v4
   and fail terminally (TL-13).
5. Phase 3 smoke: the E-4 double user turn, and the sandbox PRs.

Rolling F2 back to F1 is safe. The floor goes to #297's rollback runbook.

## Requirements and tests

| Item | PR / Task | Proving test (suggested names) |
|---|---|---|
| R1 (review) | F1-3 | the snapshot run's user message holds the rules.md and AGENTS.md text of the snapshot |
| R1 (conventions), R18, T-b, T-c | F1-10 | conventions use the pinned id and the snapshot AGENTS.md; no `fetch_agents_md` call; one manifest after fit |
| R2 | F1-4 | L2–L4 blocks in the user message with block_id, revision and side; manifest roles; fit drops L3 first, L4 last |
| R4 | F5 (after #283) | all files unsupported → coverage none, 0 `llm.call`, neutral check-run |
| R5 | F1-11 | the v2 key misses on a legacy row; a v3 run keeps the old key |
| R7 | F1-12 | a v4 run with more than 12k policy tokens fails at `policy.load` with 0 `llm.call`; the same docs on v3 proceed |
| R8 | F2-1 | v4 is seeded and active, the checksum is pinned, the golden envelope matches, `test_review_prompt_pins_postgres.py:160` is flipped |
| R10, T-a | F1-5, F1-7 | one fit per call; attribution uses the accepted (repair or fallback) manifest; the verifier uses the verifier window |
| R12, #247 p3 | F1-8 | 0 candidates → `allowed_v2_rules` None; the verifier sees the repaired `rule_name` |
| R14 | F1-7 | the worker composition passes a `ReflectionPass`; a v4 run makes a verification `llm.call` |
| R19, T-d | F1-13 | the snapshot path counts with counter/v2; legacy keeps 4 chars (`context.build` numbers unchanged) |
| Manifest, E-3 | F1-6 | a `context_manifests` row and a `context.manifest` action per base, repair or fallback call; `llm.call` carries the ids; a #144 role round-trips |
| E-1, E-7, E-12 | F1-9 | snapshot tasks reserve slots and time and use bounded repair; legacy tasks keep 0 and off |
| E-2 | F1-7 | the verification `llm.call` response has no `reason` |
| E-4 | phase 3 smoke | two user turns accepted by EUrouter/Mistral |
| E-5 | closed (TL-5) | none: #227's contract stays, `verification` is never an SSE `llmOperation` |
| E-6 | G | docs |
| E-8 | follow-up | profile field change (DD4 last line) |
| E-9 | F1-5 | `send_budget_for` ignores `LLM_CONTEXT_WINDOW` in the environment |
| E-10, E-11, T-e, #280 P2 | done | none |
| D-1 | F1-2 | test (a), or (b) if TL-10 picks it |
| D-2 | PR bodies F1/F2 | runbook |
| D-3 | not in F | (P3 optional, as in D) |
| D-Q2/Q4 | F1-2 | v4 with a NULL pin → integrity error, 0 calls |
| P6-1, P6-2 | F3-1 | one `context.build` per attempt; `context_payloads` still written from the stage summary |
| P6-3, P6-5, P6-6 | F3-2, F3-3, F3-4 | escaped `diagnostic_path`; typo; dead code |
| P6-4 | F1-4 | the snapshot prompt shows the excluded-file counter, never a path |
| `LLM_VERIFIER_*` | F4-1 | env-file and workflow tests list the four keys |
| Legacy pin | F1-1 | characterization test, kept green by every F1 task |

## Live-path impact

- **F1: none.**
  - F1-1 pins one fixture run on the legacy path: v3, D pins, a ready snapshot, the real stage. It pins the
    exact provider messages, the `llm.call` count and request keys, the run_action sequence, an empty
    `context_manifests`, `reserved_calls=0` and the conventions cache key.
  - Every later F1 task keeps it green.
  - Leftovers on legacy runs: one log warning removed (DD9) and one `ReflectionPass` object built per attempt.
- **F2: the switch.** Every new run gets the snapshot prompt, the verifier call, the new slot reserve
  (cache miss: C1 → R1 → R-fallback → V1, TL-2), the R5 miss once per repo, and R7 failures.
- **F3:** `context.build` is written once per attempt, so the dashboard counts halve and the p95 shifts.
  The `overview.json` descriptions (`:560`, `:618`) and `MONITORING.md:61` are edited in the same commit.
  Also changed: `diagnostic_path` escaping in the trace and one check-run string.
- **F4:** none while the variables are unset ("only when non-empty", `env-file.sh:17`).

## Coordination (comments posted by the coordinator before the edit)

- **C** (axyi, not yet a PR): F is stacked on it.
  - `policy_snapshot_integrity.py` `verify_snapshot_pins` (D-1 option b).
  - `review_context_stage.py` `begin` and `run`.
  - `check_runs.py:55` (P6-5).
- **#303** (ndovnar, #216):
  - `execute_review.py` (snapshot branch `:219-289`);
  - `policy_snapshot.py`: the `ManifestBlock` fields; #303 removes 114 lines, which shifts its anchors;
  - `prompt_budget.py` (`send_budget_for`, fit);
  - `prompt_builder.py`: #303 renames the snapshot renderers to public; F adds the excerpt renderer;
  - `reflection_pass.py` (`:512`, `:621`);
  - `run_repository.py:718-720`.
  - Ask for its ETA. If it merges first, F1 rebases onto it.
- **#304** (axyi, #247 p2):
  - `prompt_budget.py`, `reflection_pass.py`, `review_context_stage.py`, `run_trace.py`.
  - Merge it before F1 and F3.
- **#283** (ndovnar, #213):
  - `execute_review.py`, `process_run.py`, `review_context_stage.py`, `llm/models.py` (review fit),
    `run_repository.py`, `store_review_output.py`, `findings_post_processor.py`.
  - Migration id collision at 0039 (DD4).
  - R4 (F5) builds on its `run_coverage.py`.
- **#227** (AndrewBlinets, #170):
  - `application/llm.py` and `gateway.py` `_RecordDraft`: its `operation` field against E-3.
  - `run_action_records.py` `llm_operation` allow-list (E-5).
  - `worker.py`.
  - `docker-compose.yml` and `SECRETS.md` (F4).
- **#220** (ndovnar, GitLab):
  - `worker.py` `process_review_run` is restructured there, with a GitLab `ReviewContextStage`, so R14 must
    reach both paths.
  - `conventions.py` and `process_run.py` (R18), `review_context_stage.py`, `run_repository.py`, `ci-cd.yml`,
    `SECRETS.md`.
  - After F2, GitLab runs also pin v4.
- **#302** (ndovnar, #214): `worker.py` (R14); `CICD.md` and `SECRETS.md` (F4).
- **#297** (ndovnar, #215): `CICD.md` and `SECRETS.md` (F4); the rollback floor F1 belongs in its rollback runbook.
- **#279** (lama2x2, #186): `ci-cd.yml`, `CICD.md`, `SECRETS.md` (F4).
- **IMP#144:** answers to Q1–Q7 are in DD2–DD4 and in "#144 stack points". The fail-open at
  `prompt_budget.py:384` and the guard internals stay with #144; F only moves the call point and adds
  `repository_id`.

## Docs for G (false after F2, not after F1)

- PIPELINE_SPEC `:111`, `:115`, `:171`, `:174`, `:175`, `:177`: "код пока не пишет" for `context.manifest`,
  `llm.verify` and the `llm.call` fields.
- PIPELINE_SPEC `:132`: the canon puts `context.build` after conventions; F3 keeps the stage's single
  build before them.
- CVS §5.2 "Текущее состояние" (`:770-780`). CVS §7.3: the example `C1 → R1 → R2 repair → V1` holds only
  without a fallback (TL-2). CVS §4.2: the R5 key deviation (TL-7).
- RFS §7.2 values; TEST_PLAN §3.4 row "боевой worker его не подключает"; the rollback floor in CICD.
- False since F1-7, left for G because #303 rewrites both paragraphs: PIPELINE_SPEC "Состояние
  verifier после #133" (`:571-590`) and TEST_PLAN §3.4 (`:483-495`) still say a reflection pass
  without a snapshot is skipped with the `execute_review_legacy_run_without_snapshot` warning
  and that neither composition passes it. PIPELINE_SPEC `:177`: `llm.verify` now names its
  `manifest_id`.

## Decided by the tech lead on 2026-10-25

Recorded per the coordinator's brief; one line per decision.

- **TL-1** (a), per the coordinator's brief: F1 is the dormant prep with lock 1 and R14 behind the path selector (pinned review prompt v4 → snapshot path, ≤v3 → legacy, v4 without a ready snapshot → fail closed before any model call); F2 is only `review.system.v4.md` and its pin; F3, F4, F5 are separate.
- **TL-2** (b), per the coordinator's brief: `reserved_calls` review 1, conventions 2 (CVS §7.3), snapshot path only; legacy unchanged; with a fallback the cache-miss chain is C1 → R1 → fallback → V1, and preferring repair is a follow-up.
- **TL-3** (b), as recommended, per the coordinator's brief: the verification `llm.call` response is stored without `decisions[].reason`.
- **TL-4** (a), as recommended, per the coordinator's brief: the gateway writes `context.manifest`, because only it sees repair and fallback.
- **TL-5** Decided: keep #227's contract; E-5 closed without SSE change. The verifier's `llm.call` carries `operation="verification"` in `run_actions` only; `SSE_LLM_OPERATIONS`, the openapi `RunStepEvent.llmOperation` enum and PIPELINE_SPEC:63 stay as they are.
- **TL-6** as recommended, per the coordinator's brief: the E-7 time reserve is in F1, gated (the review's own call + 90 s + 10 s); the E-7 money reserve and E-8 (pin the verifier provider route) are follow-ups.
- **TL-7** (b), as recommended, per the coordinator's brief: a v2 digest of the pinned identities goes into `policy_key` (namespace, `policy_digest`, the current `policy_key` value with the tainted rename and copy paths of `review_context_stage.py:362-381`, `matcher_version`, `defaults_checksum`, the conventions prompt checksum, profile versions); the deviation from CVS §4.2 is recorded for G.
- **TL-8** yes, as recommended, per the coordinator's brief: R7 fails closed above 12k policy tokens on the snapshot path, with a read-only staging query before F2 that lists repos above the limit.
- **TL-9** as recommended, per the coordinator's brief: keep the stage's `context.build` (before conventions) and remove the prompt-read step; the dashboard descriptions and MONITORING change in the same commit, and G aligns the canon.
- **TL-10** (a), as recommended, per the coordinator's brief: keep C's terminal error for the D-1 shape, pinned by a test; the F1 PR body carries the read-only staging query.
- **TL-11** (a), as recommended, per the coordinator's brief: R4 is F5, after #283, gated like F1.
- **TL-12** confirmed, per the coordinator's brief: `context_manifests` is the candidate checkpoint (blocks of any `_ROLE_KIND_MAP` role; `ManifestBlock` gets `tokens`, `repository_id`, `truncated`); #144 PR3a stacks on F1 after tasks 4, 5, 6 and 13, PR3b on F2.
- **TL-13** (b), per the coordinator's brief, refined after the batch-1 review: the selector sends only GitHub runs to the snapshot path until the stage takes the provider from the run (it hard-codes `VcsProviderName.GITHUB`, `review_context_stage.py:472`); a v4 run of another provider (or without an origin) is refused with an explicit terminal error before any model call, never sent to legacy; F2 does not merge while #220 creates GitLab runs without that stage change.
- **TL-14** (2026-10-25, during F1-7), per the coordinator's brief: the verifier receives the L2–L4 excerpt blocks the candidates cite as evidence (CVS §7.1), within the verifier-route window; uncited excerpts are not sent, and one that does not fit is left out, so the usual `insufficient_context` applies.
- **#144 manifest response** (added under TL-12, accepted by the coordinator), per the coordinator's brief: the `context.manifest` step response is `{manifest_id, digest, block_count, input_tokens, tokens_by_role: {<role>: int}}`, the tokens per role over all manifest blocks after the fit, counted with the pinned counter (R19); task 6 describes it in openapi and PIPELINE_SPEC `:174`.

## Tasks (details and "done when" in the todo)

F1 (`feat/130-snapshot-path`):
0. `docs(plans): plan the snapshot path switch (#130)`
1. `test(reviews): pin the legacy review path before the switch (#130)`
2. `feat(reviews): choose the review path by the pinned prompt version (#130)`
3. `feat(reviews): pass the attempt snapshot to snapshot-path reviews (#130)`
4. `feat(reviews): render the L2-L4 context of the payload (#130)`
5. `refactor(reviews): fit, manifest and guard the review once (#130)`
6. `feat(reviews): store the manifest of every snapshot-path call (#130)`
7. `feat(worker): run the reflection pass on snapshot-path reviews (#130)`
8. `fix(reviews): repair attribution before the verifier (#130)`
9. `feat(llm): reserve slots and time for later stages on the snapshot path (#130)`
10. `feat(reviews): give conventions the pinned prompt and the snapshot (#130)`
11. `feat(reviews): key snapshot-path conventions by repo-conventions-v2 (#130)`
12. `feat(reviews): check the policy budget at policy load (#130)`
13. `feat(reviews): count the context stage with the pinned counter (#130)`
14. `test(reviews): run a v4-bound review through the worker composition (#130)`

F2 (`feat/130-snapshot-switch`): 1. `feat(prompts): add review.system.v4 and switch the snapshot path on (#130)`

F3 (`fix/168-part6-trace`, Refs #168):
1. `fix(reviews): record context.build once per attempt (#130)`
2. `fix(reviews): escape diagnostic paths in the trace (#130)`
3. `fix(reviews): correct the ignore limit check-run text (#130)`
4. `refactor(worker): drop the unused policy source locators (#130)`

F4 (`chore/130-verifier-env`): 1. `chore(deploy): pass the verifier model variables to the worker (#130)`

F5 (`feat/130-coverage-none`, after #283): 1. `feat(reviews): finish an all-unsupported change without model calls (#130)`

## Gates (every task; run `uv sync` first if `.venv` is missing; check `df -h /tmp` before long runs)

```
uv run ruff check .
uv run ruff format --check .
uv run mypy . --exclude '^docs/reports/'
TEST_DATABASE_URL=postgresql+psycopg://app:app@localhost:5432/imp130f \
TEST_RABBITMQ_URL=amqp://app:app@localhost:5672/imp130f uv run pytest
```

Known local-only noise: 7 failures in `tests/test_deploy_monitoring.py` from a `~/.local/bin/sh` wrapper.
They pass with `~/.local/bin` off PATH. Never read any `.env`.
