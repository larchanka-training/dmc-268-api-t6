# Implementation plan: pin prompt bindings per run (#130, PR D)

## Status and baseline

This is the planning output of agent-loop step 1, for the executor of PR D in phase 2 of #130.
Issue: https://github.com/larchanka-training/dmc-268-api-t6/issues/130 (R3 of the remainder
inventory, together with #219 part 8). Branch `feat/130-run-pins` in worktree
`projects/dmc-268-api-t6-130d`. It is **stacked on PR #280** (`feat/212-pin-threshold`, `588d435e`,
ndovnar, #212: one commit on `main` `4427ca8a`). The only commits between `4427ca8a` and today's
`origin/main` `1ae74b1a` are create-issue skill commits.
Alembic head on this branch: `20261024_0038` (#280). Task list: [130-run-pins-todo.md](130-run-pins-todo.md).

Every `file:line` below was checked at `588d435e` on 2026-10-24. The planner did not run the
gates. Task 0 records the baseline run.

**Q1 is decided: D pins only the prompt bindings and Task 4 is dropped** (see Q1 and the open
questions below). Tasks 0–3 and 5 remain.

## Context

`get_pins` (`app/modules/reviews/infrastructure/policy_snapshot_unit_of_work.py:93-132`) builds
`PromptBindings` from `Run.prompt_version_id` only. The conventions and verifier checksums are
`""` and their ids are `None` (`:122-128`). `execution_profile` is whatever the worker injects
(`:129`; injected via `app/worker.py:534`, `:583-585`, `:659`, built by `execution_profile_of`
at `app/worker.py:504-523`). It becomes fixed only when the first snapshot is stored
(`app/modules/reviews/application/prepare_policy_snapshot.py:159-200`).

So `PromptBindings.is_complete` (`policy_snapshot.py::PromptBindings.is_complete`)
is never true, `PolicySnapshot.is_ready_for_llm` (`:342-347`) is never true, and
`execute_review.py:225-229` would refuse a snapshot. That is lock 2 of the dormant path.

RFS §7.2 (`docs/RULES_FORMAT_SPEC.md:696-701`) and the acceptance text (#130, "prompt_bindings и
execution_profile закрепляются при создании run, до чтения VCS") want all three prompt bindings
and the execution profile fixed once per run. Retries reuse them; a rerun takes the current values.

Facts this plan rests on:

- **Run creation happens in three processes, all through `lock_candidate`
  (`app/modules/reviews/infrastructure/webhook_runs.py:40-109`).**
  - Webhook-worker: `TriggerFromDelivery` → `TryEnqueueWebhookRun`, composed at
    `app/bootstrap/reviews_api.py:313-327`.
  - Review worker: `SweepNoCi`, `app/worker.py:790-799`, `:823-829`.
  - API rerun: `app/main.py:655` → `rerun_store.py:47-99`. It reuses `lock_candidate` at `:56`.
  - The rows are inserted at `webhook_runs.py:127-156` and `rerun_store.py:71-93`.
- **Only the review worker has LLM settings.**
  - `deploy/scripts/env-file.sh:17` sends `LLM_MODEL`, `LLM_FALLBACK_MODEL`, `LLM_BASE_URL` and
    the keys to `worker.env` only. `env_file_for_key` (`:21-35`) maps each key to exactly one file.
  - In `deploy/compose/staging.yml`, `api` reads `api.env` (`:9-10`) and `webhook-worker` reads
    `app.env` + `webhook-worker.env` (`:109-111`). Local `docker-compose.yml` sets `LLM_*` only
    for `worker`.
  - `LlmSettings.from_env` raises without API keys for a known endpoint
    (`app/modules/reviews/infrastructure/llm/settings.py:275-276`). The provider route comes out
    of that same parse (`:280-284`).
- **Verifier prompts are in the DB.**
  - `seed_prompts` copies every `review/prompts/*.vN.md`, `review.verifier.v1/v2` included, into
    `prompt_versions` and activates the newest version per key (`app/bootstrap/seed_prompts.py:95-144`).
  - `VerifierPromptBuilder` reads the packaged file and compares
    `sha256(read_text().encode())` with `bindings.verifier_checksum`
    (`app/modules/reviews/application/reflection_pass.py:319-381`).
- **`PINNED_PROMPT_CHECKSUMS` is not a runtime gate** (`policy_snapshot.py::PINNED_PROMPT_CHECKSUMS`). It is used
  by `tests/policy_snapshot_fakes.py::make_complete_prompt_bindings` defaults,
  prompt-seeding and review-prompt-pin tests, and the offline snapshot builder
  `review/scripts/eval_live.py::_policy_snapshot`.
- **The live conventions prompt is the active row at processing time**
  (`run_repository.py:458-488`). The live review prompt is `Run.prompt_version_id`
  (`review_prompt_repository.py:81-91`, which never sets `policy_snapshot`).

## Decisions of this plan (author; the tech lead may overturn)

- **D1 — storage.** One new revision adds two nullable columns to `runs`, and no backfill:
  `conventions_prompt_version_id` and `verifier_prompt_version_id`, UUID, FK
  `prompt_versions.id`, independent of each other. No `execution_profile` column (Q1 decision).

  FK columns rather than a JSONB `prompt_bindings`:
  - they mirror `prompt_version_id` (`models.py:255`);
  - the checksums stay in immutable `prompt_versions` rows, where seed refuses changes
    (`seed_prompts.py:123-127`);
  - the review id is not duplicated.

  No CHECK constraint: each column may be NULL on its own (D3). FK names follow PostgreSQL
  defaults (backend.md §3 item 7).
- **D2 — verifier and conventions choice.** At creation, pin the **active** `review.conventions`
  and `review.verifier` rows, exactly as `lock_candidate` already picks the active
  `review.system` when the repository has no override (`webhook_runs.py:71-79`).
  - A packaged-version constant cannot work: `PromptBindings.is_complete` needs the verifier
    **id**, and only the DB row has one.
  - "Latest packaged" follows from the seed's newest-is-active rule. Today that is
    conventions v2 and verifier v2.
  - `Repository.prompt_version_id` overrides `review.system` only.
  - `PINNED_PROMPT_CHECKSUMS` stays as it is. It lists exactly those checksums for conventions
    v2 and verifier v2, and it is not consulted at runtime. System v3 stays out of it (R8, F).
- **D3 — missing active row.** If no active conventions or verifier row exists, the run is
  still created with that column NULL. There is no new `CandidateMiss`.
  - Today a run is created and then fails at processing
    (`run_repository.py:482-483` for conventions). The verifier is not used live.
  - Returning `MISSING_PROMPT` would turn that into UNCONFIGURED/422, which is a live-path change.
  - Many PG fixtures seed only `review.system` (`tests/portal_postgres.py:101`,
    `tests/test_fast_engine_postgres.py:66`, `tests/test_worker_queue_postgres.py:200`, …).
- **D4 — historical runs (NULL pins).** `get_pins` keeps today's behaviour exactly:
  - review-only bindings, with the conventions and verifier versions at the dataclass defaults;
  - the worker-injected profile.

  Why:
  - It mirrors #280's "historical NULL keeps the live fallback".
  - It invents no pins the run never made.
  - The `policy_digest` a new worker computes for a run created before the migration equals
    the one the old code computed. So a snapshot race across the deploy boundary cannot raise
    `SNAPSHOT_INTEGRITY_ERROR` (`prepare_policy_snapshot.py:193-198`).
- **D5 — values.** `execution_profile_of` keeps its values (tech lead, 2026-10-24):
  `tokenizer/v1`, `counter/v2`, `recovery/v1`, `budget/v1`, `review-output/v2`, temperature
  `0.0`. Their validation tests exist: `tests/test_review_context_stage.py:817-888`. The
  profile stays the worker-injected one, frozen by the first stored snapshot (Q1 decision); D
  does not touch `app/worker.py`.
- **D6 — migration id `20261024_0039_run_prompt_pins`**, `down_revision = "20261024_0038"`.
  #283 also uses `20261024_0039`, so the id is renumbered at rebase: whichever PR merges second
  renumbers its revision and `down_revision` onto the head it finds. #254 (`20261023_0038`) and
  #220 (`20261020_0037/0038`) branch from 0037 and follow the same rule.

## Q1 (decided, Task 4 dropped): where the execution profile comes from

Decided by the tech lead on 2026-10-24: neither variant goes into D. `execution_profile` stays
frozen in the first policy snapshot, as today (`prepare_policy_snapshot.py:161-166` reuses the
stored snapshot; policy.load runs before any model call, `process_run.py:155-157`). D pins only
`prompt_bindings` at run creation; Task 4 and the JSONB `execution_profile` column are dropped.
The RFS §7.2:781 wording ("при создании run") is a known deviation, fixed later in docs PR G.
The variants below stay as the record.

The brief asks to pin the profile "in the same transaction that creates the run". Two of the
three creating processes have no LLM settings, so a choice is needed.

- **B — recommended: pin once at the first claim, in the review worker.**
  - What it does: `SqlAlchemyRunLifecycleStore.claim` (`run_lifecycle_store.py:166-185`) runs
    `UPDATE runs SET execution_profile = :p WHERE id = :id AND execution_profile IS NULL` in the
    claim transaction (`handle_review_run.py:374-377`). `:p` is
    `execution_profile_of(settings.llm)`, injected through
    `partial(SqlAlchemyRunLifecycleUnitOfWork, …)` at `worker.py:766`.
  - Pros:
    - It happens before any VCS read of the attempt, once per run, and is reused by every retry.
      A rerun is a new row and gets a fresh pin.
    - One source of truth: the same `LlmSettings` into which E adds `LLM_VERIFIER_*`. So the
      seam stays one line in `execution_profile_of`.
    - No deploy change. The api keeps reading no LLM configuration (`docs/SECRETS.md`, least
      privilege).
    - In F, attempt 1 can never meet a model its worker is not configured for.
  - Cons:
    - It deviates from the creation-transaction wording (RFS §7.2 `:696-698`, acceptance).
    - A deploy between creation and first claim changes the profile of a run that has not
      started.
    - G must rewrite RFS §7.2.
- **A — spec-literal: pin at creation in all three processes.**
  - What it needs:
    - a key-free builder `execution_profile_from_env(env) -> ExecutionProfile | None` that copies
      the provider-route logic of `_profile_from_env`, guarded by a parity test against
      `execution_profile_of(LlmSettings.from_env(env + keys))`;
    - an optional `execution_profile` constructor argument (default `None`) on
      `SqlAlchemyWebhookRunStore`/`UnitOfWork` and on `SqlAlchemyRerunStore`/`UnitOfWork`.
      It must be optional: #220, `run_notifications.py:25` and `rerun_store.py:56` construct
      the store bare;
    - writes in `insert_webhook_run` and `create_rerun`;
    - wiring at `reviews_api.py:313-327`, `:409-413` and `worker.py:790`;
    - a **deploy change**: deliver `LLM_MODEL`, `LLM_FALLBACK_MODEL`, `LLM_BASE_URL`, an optional
      `LLM_PROVIDER` and later E's `LLM_VERIFIER_*` to api and webhook-worker. That means
      env-file.sh's one-file rule, `ci-cd.yml:786-793`, both compose files, SECRETS and their
      tests.
  - Risks: values drift between containers, and E's seam grows to two places. Without the deploy
    change, webhook and rerun runs pin NULL and fall back to the injected profile (D4).
    D stays dormant either way.
- C (rejected): a profile row seeded at deploy by `bootstrap`. It needs the same env delivery
  (`staging.yml:146-148` has only `DATABASE_URL`) plus a table.

## Requirements → tests

| Req | What | Proven by |
|---|---|---|
| R3-a | Conventions and verifier pinned in the creation transaction, webhook path | Task 2: `test_webhook_creation_pins_active_conventions_and_verifier_and_rolls_back` |
| R3-b | Same on the rerun path; a rerun takes the current active rows | Task 2: `test_rerun_pins_current_prompt_rows_while_original_keeps_its_own` |
| R3-c | A retry reuses the pins | Task 2: `test_retry_keeps_prompt_pins_after_a_newer_verifier_is_activated` |
| R3-d | `get_pins` reads the pins; versions `v{n}` from the rows | Task 3: `test_get_pins_returns_complete_bindings_from_pinned_rows` |
| R3-e | Historical NULL runs: today's bindings and the injected profile, same digest | Task 3: `test_historical_run_keeps_todays_pins_and_digest`; existing `tests/test_policy_snapshot_postgres.py:81-92` unchanged |
| R3-f | Seeded verifier checksum equals what `VerifierPromptBuilder` computes | Task 3: `test_seeded_verifier_checksums_match_the_builder_reading` |
| R3-g | Missing active conventions or verifier row: run still created, column NULL | Task 2: `test_missing_active_conventions_or_verifier_still_creates_the_run` |
| R3-h | Dropped (Q1 decision): the profile stays frozen by the first stored snapshot | — |
| R3-i | No network inside the creation transaction (backend.md §3 item 4) | By construction: only SELECTs. Caller rollback test (R3-a) |
| #219 ч.8 | Profile values pass validation | Existing `tests/test_review_context_stage.py:817-888` |
| Dormant | Complete pins make the snapshot ready, yet nothing reaches the prompt | Task 5: `test_complete_pins_make_snapshot_ready_but_prompt_input_stays_legacy` |

## Tasks (one Conventional commit each, test-first)

Details, files and checklists are in the todo. Order: 0 → 1 → 2 → 3 → 5 (4 dropped).

0. `docs(plans): plan run pins for prompt bindings (#130)`. Commits the
   two plan files and records the baseline gate run.
1. `feat(reviews): add run prompt pin columns (#130)`.
   - Red: a migration test (upgrade keeps historical runs NULL, FK rejects an unknown prompt,
     downgrade drops the columns), plus the head pins moved to 0039.
   - Green: the revision and the ORM columns. `tests/test_initial_migration.py:91`
     (`compare_metadata == []`) stays green.
2. `feat(reviews): pin conventions and verifier prompts at run creation (#130)`.
   - Red: PG tests R3-a/b/c/g.
   - Green: two non-default `RunInsertCandidate` fields, two SELECTs in `lock_candidate`, the
     values in `insert_webhook_run` and `create_rerun`. Update the two candidate builders in tests.
3. `feat(reviews): read pinned prompt bindings in policy pins (#130)`.
   - Red: R3-d/e/f.
   - Green: `get_pins` with two aliased outer joins on `PromptVersion`. Rewrite the comment at
     `:118-121`, which D makes false.
4. Dropped by the Q1 decision: no execution profile pin in D.
5. `test(reviews): pin dormancy of complete run pins (#130)`. An end-to-end PG test: a ready
   snapshot, `get_review_prompt_input(...).policy_snapshot is None`, and the conventions cache
   identity unchanged.

## Coordination (comments are posted by the coordinator before the edit)

| PR | Shared file → hunk | What to say |
|---|---|---|
| #280 (ndovnar, base) | `webhook_runs.py` `lock_candidate` return / `insert_webhook_run` values; `rerun_store.py` `Run(...)`; `try_enqueue_webhook_run.py` `RunInsertCandidate`; `policy_snapshot_unit_of_work.py` `get_pins`; `models.py` `Run` | Stacked as agreed (6387289216). D extends these hunks and reworks none. It rebases on `main` after #280 merges |
| #220 (ndovnar, GitLab) | `RunInsertCandidate` (adds a defaulted `provider` after `prompt_version_id`); `lock_candidate` (new GitLab branch, `_lock_gitlab`); `create_rerun` preamble; migrations 0037/0038 | D adds two non-default fields, so after a rebase the non-default fields go before `provider`. The two prompt SELECTs must cover the GitLab candidate too. Renumber migrations at rebase. D no longer touches `run_lifecycle_store.py` or `worker.py` (Q1 decision) |
| #227 (AndrewBlinets) | `worker.py` `pipeline` hunk `@@ -774`; `run_lifecycle_store.py` `check_run_report` | No overlap: D touches neither file (Q1 decision) |
| #229 (AndrewBlinets) | `run_lifecycle_store.py` `publish_context`, `complete_publication` | No overlap: D does not touch `run_lifecycle_store.py` (Q1 decision) |
| #283 (ndovnar) | `models.py` `CodeChangeDiff` (`:137`); `run_lifecycle_store.py` `check_run_report`; migration `20261024_0039` | Same 0039 id → renumber at rebase. No hunk overlap |
| #254 (axyi, #144) | `worker.py` `review_context_stage_factory` `:535-586`, where today's profile is injected; migration `20261023_0038` | D leaves the injection as it is. That keeps D4's fallback and avoids the hunk |
| E (axyi, sibling) | `llm/settings.py` (`LLM_VERIFIER_*`, E-owned) | The verifier-model seam (`execution_profile_of`, `worker.py:512` and `:514`) is E's alone: D does not touch `app/worker.py` at all |

## Live-path impact

D changes no review a live run produces. Locks 1 and 2 of the dormant path:

- Lock 1 holds: `review_prompt_repository.py:81-91` never sets `policy_snapshot`; the worker
  builds `ExecuteReviewRun` without `reflection_pass` (`worker.py:278-280`); conventions get no
  snapshot (`process_run.py:71-75`, T-c).
- Lock 2 opens for new runs.

What is observable:

1. **New runs' stored snapshots have complete `prompt_bindings`**; the profile is the
   worker-injected one, as today. Their `policy_digest` therefore differs from what the same policy gave before D.
   The digest is read only by the `policy.load` trace (`review_context_stage.py:109-114`) and by
   the discarded `ReviewContextPayload` (`process_run.py:245`).
   - The conventions cache keys on `policy_key`, which derives from `ignore_digest`
     (`review_context_stage.py:226-245`). That is unchanged; Task 5 pins it.
   - All `execution_profile` consumers in `app/` sit behind a present snapshot.
     `review_context_stage.py:273` only validates versions, and those are code constants.
2. **`lock_candidate` runs two more indexed SELECTs** inside the creation transaction (DB only).
3. **Missing prompt rows behave as today (D3), and historical runs behave as today (D4).**

## Docs for G

None in D. Nothing a D commit touches becomes false in a document, except the code comment at
`policy_snapshot_unit_of_work.py:118-121`, which Task 3 rewrites. Drift for G:

- RFS §7.2 `:696-698`: storage form (D1). `:781` ("при создании run"): the profile is fixed
  by the first stored snapshot, not at creation (known deviation, Q1 decision).
  `:725-726`: `verifier_model` per E.
- RFS §7.1 `:641` and `:644`, and RFS §7.4 `:785-788`: "Инженер 4 (#219)".
- SD `:1579` (the `Run` row of the data model) and the ERD gain the two columns.

## Open questions for the tech lead

1. Decided by the tech lead on 2026-10-24: neither B nor A; the profile stays frozen in the first policy snapshot, Task 4 and its column are dropped, RFS §7.2:781 is a known deviation for docs PR G.
2. Decided by the tech lead on 2026-10-24: questions about incomplete pins are deferred to PR F; D ships D3 as planned.
3. Decided by the tech lead on 2026-10-24: reading conventions by the pinned id is deferred to PR F.
4. Decided by the tech lead on 2026-10-24: the handling of runs with incomplete pins is deferred to PR F.

## Gates and test environment

```
uv run ruff check .
uv run ruff format --check .
uv run mypy . --exclude '^docs/reports/'
TEST_DATABASE_URL=postgresql+psycopg://app:app@localhost:5432/imp130d \
TEST_RABBITMQ_URL=amqp://app:app@localhost:5672/imp130d uv run pytest
```

- Database and vhost `imp130d` already exist. Use only those.
- Known local-only noise: 7 failures in `tests/test_deploy_monitoring.py` from a
  `~/.local/bin/sh` wrapper. They pass with `~/.local/bin` off PATH.
- Run `uv sync` first if `.venv` is missing; it is missing at planning time.
- Check `df -h /tmp` before long runs.
- Never read any `.env`.
