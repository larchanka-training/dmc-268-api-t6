# Task list: pin prompt bindings per run (#130, PR D)

See [130-run-pins-plan.md](130-run-pins-plan.md) for the design. That plan is the source of
truth and this list adds no scope. R3-a … R3-i, D1–D6 and Q1 refer to the plan. Baseline is
`588d435e` on `feat/130-run-pins`, stacked on #280; the Alembic head is `20261024_0038`.

Each task is one Conventional commit whose subject ends with `(#130)`; ⚑ marks a task with more
than 10 edits across its files. Every task that writes
code is delegated to a subagent through a brief file (`standards/workflow.md` §5.1). **Every
brief says: never read `.env` of any clone, and only `git show`/`git diff` in other clones.**

Every task is test-first: write the named tests, watch them fail for the stated reason, then
write the code. Assert literal values (backend.md §5), for example the checksums of
`review.conventions.v2` = `225a8f7d56835cf1307ddb25feac602c3d10e03c34ee51afc90a1f99af101454`
and `review.verifier.v2` = `1f676b4fea8477d77b83a106a6c62e78f67d19f71937d4a02093d11a9d276088`.
Never derive an expected value from the code under test.

**Common verification.** Run this for every task:

```
uv run ruff check .
uv run ruff format --check .
uv run mypy . --exclude '^docs/reports/'
TEST_DATABASE_URL=postgresql+psycopg://app:app@localhost:5432/imp130d \
TEST_RABBITMQ_URL=amqp://app:app@localhost:5672/imp130d uv run pytest
```

- A skipped PG or RabbitMQ test never counts as passing. Run the focused files with `-rs`
  and expect zero skips.
- Known local-only noise: 7 failures in `tests/test_deploy_monitoring.py` from the
  `~/.local/bin/sh` wrapper. Re-run them with `~/.local/bin` off PATH.
- Run `uv sync` first; there is no `.venv` at planning time. Check `df -h /tmp` before the
  full suite.

New test files (suggested names): `tests/test_run_prompt_pins_migration.py` (Task 1) and
`tests/test_run_prompt_pins_postgres.py` (Tasks 2–5, `@pytest.mark.integration`, skip without
`TEST_DATABASE_URL`). PG tests seed prompts with
`seed_prompt_versions(session, load_prompt_assets(REPO_ROOT / "review" / "prompts"))` minus
`review.system` v1, which makes system v3, conventions v2 and verifier v2 active. The
`portal_schema` seed holds a fake `review.system` v1 (checksum `"c" * 64`), so seeding the
packaged v1 again is refused as an immutable prompt change. The claim and requeue helpers follow
`tests/test_run_publication_settings_postgres.py` (`_claim`, `SqlAlchemyRunLifecycleStore.requeue`).

## Task 0: Plan documents

**Commit:** `docs(plans): plan run pins for prompt bindings (#130)`

**Files:** `docs/plans/130-run-pins-plan.md`, `docs/plans/130-run-pins-todo.md`.

- [ ] `docs/plans/` is matched by the local `.git/info/exclude:14`, so stage the two files with
  `git add -f` (tracked plans such as `143-plan.md` show the precedent);
- [ ] `uv sync`, then run the full gate once on `588d435e` and note the result (pass/fail
  counts, skips) in the PR body as the baseline;
- [ ] nothing else: Q1 is decided (Task 4 dropped); quote the ruling in the PR body.

## Task 1: Pin columns

**Commit:** `feat(reviews): add run prompt pin columns (#130)`

**Files:**
- `alembic/versions/20261024_0039_run_prompt_pins.py` (new);
- `app/modules/reviews/infrastructure/models.py` (`Run`, next to `prompt_version_id` at `:255`);
- tests listed below.

**Tests first:**
- `tests/test_run_prompt_pins_migration.py::test_prompt_pin_migration_keeps_history_null_and_reverts`.
  Shape as `tests/test_run_publication_settings_migration.py:43`.
  - Upgrade to `20261024_0038`, seed a run, upgrade to `20261024_0039`.
  - The two columns are NULL on the existing run.
  - Inserting an unknown UUID into `conventions_prompt_version_id` raises `IntegrityError`.
  - Downgrade to `0038` drops the two columns.
- Move the head pins to 0039:
  - `tests/test_app_reinstall_schema_postgres.py:67-69` (heads list and chain);
  - `tests/test_code_index_store_postgres.py:727`, `:752`, `:878`;
  - `tests/test_run_origin_postgres.py`: the `to_jsonb(r)-…` subtraction #280 extended. Add
    `-'conventions_prompt_version_id'-'verifier_prompt_version_id'`, and
    assert those columns are NULL for the seeded runs.

**What to do:**
- Revision `20261024_0039`, `down_revision = "20261024_0038"`. Use explicit `op.add_column` for:
  - `conventions_prompt_version_id` UUID, FK `prompt_versions.id`, NULL;
  - `verifier_prompt_version_id`, same.
- No `execution_profile` column (Q1 decision). No backfill and no CHECK (D1, D3). Leave FK
  names at the PostgreSQL defaults (backend.md §3 item 7). Explicit downgrade.
- ORM: `Mapped[UUID | None]` with `ForeignKey("prompt_versions.id")` ×2. A comment says what
  each pins and why NULL is allowed (historical run; no active row).

**Done when:**
- [ ] `tests/test_initial_migration.py` (`compare_metadata == []` at `:91`) is green;
- [ ] `tests/test_alembic_heads.py` is green, with one head;
- [ ] the migration test is green with zero skips.

**Dependencies:** Task 0. **Precondition:** the coordinator has posted the coordination comments
(plan table) — Task 1 is the first edit to files shared with #280/#283 (AGENTS.md).

## Task 2: Pin conventions and verifier at creation ⚑

**Commit:** `feat(reviews): pin conventions and verifier prompts at run creation (#130)`

**Files:**
- `app/modules/reviews/application/try_enqueue_webhook_run.py` (`RunInsertCandidate` `:98-113`);
- `app/modules/reviews/infrastructure/webhook_runs.py` (`lock_candidate` `:71-109`,
  `insert_webhook_run` `:127-149`);
- `app/modules/reviews/infrastructure/rerun_store.py` (`Run(...)` `:71-93`);
- `tests/test_github_run_trigger.py:101` and `tests/test_webhook_full_path.py:221` (candidate
  builders);
- the new PG file.

**Tests first** (`tests/test_run_prompt_pins_postgres.py`):
- `test_webhook_creation_pins_active_conventions_and_verifier_and_rolls_back` (R3-a, R3-i).
  - Create through `SqlAlchemyWebhookRunUnitOfWork` → the run row holds the ids of the active
    conventions v2 and verifier v2 rows.
  - When the caller leaves the UoW without commit, no run exists.
  - Mirror #280's `test_webhook_creation_reads_fresh_settings_and_rolls_back_with_its_transaction`.
- `test_rerun_pins_current_prompt_rows_while_original_keeps_its_own` (R3-b).
  - Activate a newer verifier: seed a temporary `review.verifier.v3.md` asset with
    `seed_prompt_versions`.
  - Then `SqlAlchemyRerunUnitOfWork(..., allow_unscoped=True).runs.create_rerun`.
  - The new row pins v3 and the original row still pins v2.
- `test_retry_keeps_prompt_pins_after_a_newer_verifier_is_activated` (R3-c). Claim, activate
  v3, requeue, claim attempt 2: both columns are unchanged.
- `test_missing_active_conventions_or_verifier_still_creates_the_run` (R3-g, D3).
  - Seed `review.system` only. The run row exists with both columns NULL (assert the row, not the
    publisher outcome).
  - Then seed conventions only: verifier NULL, conventions set.
- `test_repository_prompt_override_applies_to_review_system_only`. A repository with
  `prompt_version_id` set pins that review prompt plus the active conventions and verifier rows.

**What to do:**
- `RunInsertCandidate`: add `conventions_prompt_version_id: UUID | None` and
  `verifier_prompt_version_id: UUID | None`, both **without defaults**, so every constructor
  states them. Put them after `max_comments`. If #220 lands first with its defaulted `provider`,
  keep the non-default fields before it. `PendingRunMessage` and the queue contract do not change.
- `lock_candidate`: two `select(PromptVersion.id).where(key == …, is_active)` scalars, written
  like `:73-77`. A missing row → `None` (D3), never a `CandidateMiss`.
- `insert_webhook_run` values and `create_rerun` `Run(...)`: write both ids from the candidate.
- No network call is added to either transaction (backend.md §3 item 4). Repositories only
  flush (§3 item 3).

**Done when:**
- [ ] the five tests are green; `tests/test_run_publication_settings_postgres.py` and
  `tests/test_webhook_run_trigger_postgres.py` stay green;
- [ ] gates green.

**Dependencies:** Task 1.

## Task 3: `get_pins` reads the pins

**Commit:** `feat(reviews): read pinned prompt bindings in policy pins (#130)`

**Files:**
- `app/modules/reviews/infrastructure/policy_snapshot_unit_of_work.py` (`get_pins` `:93-132`);
- `tests/test_run_prompt_pins_postgres.py`;
- `tests/test_prompt_seeding.py`.

**Tests first:**
- `test_get_pins_returns_complete_bindings_from_pinned_rows` (R3-d). For a run created through
  the store:
  - `pins.prompt_bindings.is_complete is True`;
  - `conventions_version == "v2"`, `verifier_version == "v2"`;
  - the ids equal the run's columns;
  - the checksums equal the literal v2 hashes above;
  - `review_system_version == "v3"` (the active system row).
- `test_historical_run_keeps_todays_pins_and_digest` (R3-e, D4). On a run with NULL columns
  (the `tests/portal_postgres.py` seed):
  - the bindings equal `PromptBindings(review_system_checksum=…, conventions_checksum="",
    verifier_checksum="", review_system_version=…, review_system_id=…)`;
  - the profile equals the injected one;
  - `build_policy_snapshot(pins, …).policy_digest` equals the digest built from those
    literal pre-D pins.

  `tests/test_policy_snapshot_postgres.py:81-92` must stay green unchanged.
- `tests/test_prompt_seeding.py::test_seeded_verifier_checksums_match_the_builder_reading` (R3-f,
  unit). For each `review/prompts/review.verifier.v*.md`:
  - the `load_prompt_assets` checksum equals
    `sha256(files("review.prompts").joinpath(name).read_text(encoding="utf-8").encode()).hexdigest()`,
    which is what `VerifierPromptBuilder` computes (`reflection_pass.py:328-342`, `:374-381`);
  - a text-mode newline change would otherwise surface only in F as `SNAPSHOT_INTEGRITY_ERROR`.

**What to do:**
- In `get_pins`, add two `aliased(PromptVersion)` outer joins on the new columns. Select
  checksum and version of each.
- Build `PromptBindings` with ids from the run columns, checksums from the rows and
  `f"v{version}"` for each version. Never rely on the `"v2"` dataclass defaults when a row exists.
- A NULL column keeps today's empty checksum, `None` id and default version (D4).
- Rewrite the comment at `:118-121`: it says the conventions and verifier prompts are unpinned
  until #130.

**Done when:**
- [ ] the three tests are green;
- [ ] `tests/test_review_prompt_pins_postgres.py` and
  `tests/test_run_policy_snapshot_origin_postgres.py` stay green;
- [ ] gates green.

**Dependencies:** Task 2.

## Task 4: Dropped

Dropped by the Q1 ruling of 2026-10-24 (plan, Q1): `execution_profile` stays frozen in the
first policy snapshot, as today, and `runs` gets no `execution_profile` column. D does not
touch `run_lifecycle_store.py` or `app/worker.py`; the verifier-model seam is E's alone.

## Task 5: Dormancy pin

**Commit:** `test(reviews): pin dormancy of complete run pins (#130)`

**Files:** `tests/test_run_prompt_pins_postgres.py` (and a comment fix if a code comment became
false).

**Test:** `test_complete_pins_make_snapshot_ready_but_prompt_input_stays_legacy`. Seed the three
prompts and create a run through the store. The snapshot UoW injects a complete profile, as the
worker does today (Q1 decision). Then:
- `PreparePolicySnapshot` with a fake document source returns a snapshot with
  `prompt_bindings.is_complete` and `is_ready_for_llm` both true (lock 2 is lifted);
- `SqlAlchemyReviewPromptRepository.get_review_prompt_input(run_id, …).policy_snapshot is None`
  (lock 1 holds, as in `tests/test_review_prompt_pins_postgres.py:104`);
- `ReviewContextStage.policy_key` still equals `snapshot.ignore_digest` for a policy with no
  tainted paths, so the conventions cache identity is unchanged;
- the worker still builds `ExecuteReviewRun` without `reflection_pass` (`worker.py:278-280`).
  The existing `tests/test_execute_review.py:521` proves the fail-closed branch, so no new
  assertion on private fields.

**Done when:**
- [ ] the test is green with zero skips;
- [ ] gates green.

**Dependencies:** Task 3.

## Checkpoint and publication

- [ ] Re-check the open PRs' files (`gh pr view N --json files` for 220, 227, 229, 254, 283)
  and the migration head before the PR. Renumber 0039 if another revision took it (D6).
- [ ] The PR body follows What / Why / How to verify / Refs:
  - `Refs #130`, `Refs #219`;
  - stacked on #280;
  - Q1 ruling quoted;
  - Live-path impact copied from the plan;
  - "Docs for G" list;
  - no closing keyword.
- [ ] Review runs in a clean subagent context (`/code-review`), never by the writer.
- [ ] Post-run verification goes through `/verify-run`.
