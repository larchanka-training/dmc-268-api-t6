# Tasks: Issue #310 — Run audit fields and metrics summary

Implement test-first (`pytest`). One slice per task. Gates for every task that claims done, from `.agents/rules/backend.md`: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy .`, `uv run pytest`. Plain pytest; drive async code with `asyncio.run`. No `pytest-asyncio`. Unit tests use fakes of Protocols. Integration tests are `@pytest.mark.integration` and skip in the file fixture when `TEST_DATABASE_URL` is unset. Assert literal expected values. Repositories `flush()` only. No new dependency.

Issue commands that must be green at the end of each slice are repeated on the checkpoint tasks (Tasks 3, 9, and 12).

## Task 1: Aggregate usage on the run read model

**Description:** `RunListItem` carries the sum of `usage_events` for that run, and both list and detail queries compute it in the same statement as the rest of the row. `GET /api/runs/{id}` already sums usage inside `get_run_review` for `budget`; the list does not. This task does not change the HTTP body yet.

**Acceptance criteria:**

- [x] `RunListItem` has `usage` with `tokens_in`, `tokens_out`, and `cost_usd` (`Decimal`). Default is zeros so current constructors in tests keep working.
- [x] `list_runs` and `get_run` in `SqlAlchemyRunRepository` each add scalar subqueries `coalesce(sum(tokens_in), 0)`, `coalesce(sum(tokens_out), 0)`, `coalesce(sum(cost_usd), 0)` on `UsageEvent.run_id == Run.id`. No extra statement per run. Events for every attempt are included. A run with no events is zeros, not `None`.
- [x] An integration test named so `-k run_usage` selects it inserts two `usage_events` for one run (two attempts) and reads `3` + `5` tokens and `Decimal("0.125")` + `Decimal("0.250")` as `3`, `5`, `Decimal("0.375")`. A second run with no events is zeros. `before_cursor_execute` shows the same statement count for a page of 1 run and a page of 3 runs.

**Verification:**

- [x] `uv run pytest tests/test_list_runs.py tests/test_run_usage_postgres.py`
- [x] With `TEST_DATABASE_URL` set, `uv run pytest -m integration -rs -k run_usage` is green and the new test is not skipped
- [x] Manual check: the list SQL text is still a single `SELECT` (subqueries in the select list, not a loop)

Done on `feat/310-runs-audit-metrics` (not committed). `tests/test_run_usage_postgres.py` passed and was not skipped. On this Windows machine the suite-wide `-k run_usage` command also errors while collecting `tests/test_ast_worker.py` (`import resource`); that file is unchanged.

**Dependencies:** None

**Files likely touched:**

- `app/modules/reviews/application/list_runs.py`
- `app/modules/reviews/infrastructure/run_repository.py`
- `tests/test_run_usage_postgres.py`
- `tests/test_list_runs.py`

**Estimated scope:** Medium: 3-5 files

## Task 2: Expose usage on RunSession

**Description:** `GET /api/runs` items, and `RunDetail` because it repeats every `RunSession` property, gain required `usage`. Detail `budget` is unchanged (`null` when `usage_calls == 0`). When `budget` is present, its `tokensIn`, `tokensOut`, and `costUsd` equal `usage`.

**Acceptance criteria:**

- [x] `RunUsageDto` on `RunSessionDto` has `tokens_in`, `tokens_out`, `cost_usd: float`. `to_run_session_dto` copies `RunListItem.usage`, using `float` for cost the same way `to_run_detail_dto` does for `RunBudgetDto`.
- [x] OpenAPI `RunUsage` is closed (`additionalProperties: false`), required `tokensIn`, `tokensOut`, `costUsd` (integers ≥ 0, number ≥ 0). `RunSession` and `RunDetail` both require `usage` with the same `$ref`. No migration. `x-status` of other operations stays as it is.
- [x] `tests/test_runs_api.py` shows a list item `{"tokensIn": 0, "tokensOut": 0, "costUsd": 0.0}` when the fake usage is zeros, and `{"tokensIn": 3, "tokensOut": 5, "costUsd": 0.375}` when the fake sums to that. A detail whose review has `usage_calls == 1` and those same totals returns `usage` and `budget` with equal `tokensIn`, `tokensOut`, and `costUsd`.

**Verification:**

- [x] `uv run pytest tests/test_runs_api.py tests/test_run_dtos.py tests/test_list_runs.py`
- [x] `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy .` on the touched files
- [x] Manual check: `test_component_schemas_mirror_the_ui_zod_contract` for `RunSession` and `tests/test_ui_zod_contracts.py` fail until Task 3. Do not weaken those tests. Full `uv run pytest` is the Task 3 checkpoint.

Done on `feat/310-runs-audit-metrics` (not committed). The Zod mirror and `tests/test_ui_zod_contracts.py` still fail because `usage` is absent from the snapshot.

**Dependencies:** Task 1

**Files likely touched:**

- `app/modules/reviews/api/dtos.py`
- `app/main.py`
- `contracts/openapi.yaml`
- `tests/test_runs_api.py`
- `tests/test_run_dtos.py`

**Estimated scope:** Medium: 3-5 files

## Task 3: Refresh the Zod snapshot for usage

**Description:** The committed UI Zod snapshot learns `usage` from the ui commit that added the same field, so the OpenAPI mirror and the response validator accept `RunSession` and `RunDetail`.

**Acceptance criteria:**

- [ ] `tests/fixtures/ui_zod_contracts.json` is regenerated with `docs/PIPELINE_SPEC.md` §16 (`DMC_268_UI_DIR` pointing at a clean ui checkout whose commit adds `usage` on the run-session schema). `UI_CONTRACT_COMMIT` in `tests/test_ui_zod_contracts.py` equals `provenance.commit`.
- [ ] The snapshot's `runSession` and `runDetail` require `usage` with `tokensIn`, `tokensOut`, `costUsd`, both closed. No other DTO drifts.
- [ ] `uv run pytest tests/test_list_runs.py tests/test_runs_api.py tests/test_openapi_contract.py tests/test_ui_zod_contracts.py` is green, and `uv run pytest -m integration -rs -k run_usage` with `TEST_DATABASE_URL` is green with nothing skipped.

**Verification:**

- [ ] Full gates: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy .`, `uv run pytest`
- [ ] Manual check: the generator refuses a dirty ui tree; the snapshot commit sha is the ui PR commit, not an edited JSON blob

**Dependencies:** Task 2, and a merged or committed ui change that adds `usage` with the same shape

**Files likely touched:**

- `tests/fixtures/ui_zod_contracts.json`
- `tests/test_ui_zod_contracts.py`

**Estimated scope:** Small: 1-2 files

## Task 4: Add nullable runs.triggered_by

**Description:** Persist an optional GitHub login on the run row and return it from the read model as `null` until a writer sets it. Existing rows stay `null`. The HTTP field is Task 8.

**Acceptance criteria:**

- [x] New revision (do not edit a merged revision) adds nullable `runs.triggered_by` `VARCHAR(255)` and `downgrade` drops it. `tests/test_alembic_heads.py` still sees one head. `down_revision` is the head at implementation time (`20261024_0039` on this tree).
- [x] `Run.triggered_by` is `Mapped[str | None]`. `RunListItem.triggered_by: str | None = None`. `_to_run_list_item` copies `run.triggered_by`. No backfill.
- [x] Integration test (`-k triggered_by`) upgrades, inserts a run without the column set, reads `None` from `list_runs`, downgrades, and upgrades again.

**Verification:**

- [x] `uv run pytest tests/test_alembic_heads.py tests/test_triggered_by_postgres.py`
- [x] Manual check: `uv run alembic heads` prints one revision after the new file

Done on `feat/310-runs-audit-metrics` (not committed). Head at implementation time was `20261026_0043`; the new revision is `20261028_0044`. Task 3 stays open.

**Dependencies:** Task 3 (Ч.1 snapshot is on main or this branch already contains `usage`, so later `RunSession` edits stack)

**Files likely touched:**

- `alembic/versions/20261028_0040_runs_triggered_by.py`
- `app/modules/reviews/infrastructure/models.py`
- `app/modules/reviews/application/list_runs.py`
- `app/modules/reviews/infrastructure/run_repository.py`
- `tests/test_triggered_by_postgres.py`

**Estimated scope:** Medium: 3-5 files

## Task 5: Store the webhook initiator on insert

**Description:** `insert_webhook_run` writes `triggered_by` from an explicit login, or, when the caller does not supply one, from the stored `synchronize` delivery for that head. CI completion and the no-CI sweep are not actors.

**Acceptance criteria:**

- [x] `TryEnqueueWebhookRun.execute` and `WebhookRunStore.insert_webhook_run` take keyword-only `triggered_by: str | None = UNSET`. An explicit string or explicit `None` is stored as given (a bot login is stored). `UNSET` selects the push author. The repository does not `commit()`.
- [x] Push-author lookup, in the insert session, is the latest `webhook_events` row with `event = 'pull_request'`, `action = 'synchronize'`, `payload IS NOT NULL`, matching installation external id, repository external id, and `payload->'pull_request'->'head'->>'sha'`. Login is `payload->'sender'->>'login'`. No row, null payload, or null sender → `null`. No GitHub call.
- [x] Integration tests under `-k triggered_by`: explicit `dependabot[bot]` is stored; explicit `None` stays `null` even if a synchronize row exists; `UNSET` with a synchronize payload `sender.login = "pusher"` stores `pusher`; `UNSET` with only a `check_suite` payload stores `null`; a payload that is null (S3 ref only) stores `null`.

**Verification:**

- [x] `uv run pytest tests/test_github_run_trigger.py tests/test_webhook_full_path.py tests/test_triggered_by_postgres.py`
- [x] With `TEST_DATABASE_URL`, `uv run pytest -m integration -rs -k triggered_by` does not skip the new tests
- [x] Manual check: open PRs touching `webhook_runs.py` were checked; a comment was left on any other PR before editing

**Dependencies:** Task 4

**Files likely touched:**

- `app/modules/reviews/application/try_enqueue_webhook_run.py`
- `app/modules/reviews/infrastructure/webhook_runs.py`
- `tests/test_github_run_trigger.py`
- `tests/test_webhook_full_path.py`
- `tests/test_triggered_by_postgres.py`

**Estimated scope:** Medium: 3-5 files

## Task 6: Pass the qualifying sender into enqueue

**Description:** `TriggerFromDelivery` tells the enqueuer who performed the PR action that made the head reviewable. CI does not contribute a sender. Sweep keeps calling `execute` without a login, so Task 5's lookup runs.

**Acceptance criteria:**

- [x] `on_pr` for `synchronize` and `reopened`, and `on_label` for `ai-review` / `labeled`, pass `event.sender_login` (including a bot, including `None` when the payload has no sender). `on_ci` does not pass the CI sender (`UNSET`, so the push-author lookup runs).
- [x] The call passes `triggered_by` only when `execute` accepts that parameter or `**kwargs`. `SweepNoCi` is unchanged.
- [x] A unit test double that accepts `triggered_by` records `octocat` for `synchronize`, `dependabot[bot]` for `reopened`, `labeler` for `ai-review` labeled, and does not receive a login for `on_ci`. An `opened` action still does not enqueue (existing `_RUN_TRIGGER_PR_ACTIONS`).

**Verification:**

- [x] `uv run pytest tests/test_github_run_trigger.py tests/test_sweep_no_ci.py tests/test_github_installation_dispatch.py`
- [ ] Full gates green (`uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy .`, `uv run pytest`)
- [x] Manual check: a fake whose `execute` has no `triggered_by` parameter still runs (no `TypeError`)

**Dependencies:** Task 5

**Files likely touched:**

- `app/modules/reviews/application/trigger_from_delivery.py`
- `tests/test_github_run_trigger.py`

**Estimated scope:** Small: 1-2 files

## Task 7: Store the portal login on rerun

**Description:** `POST /api/runs/{id}/rerun` stores the GitHub login of the portal user who pressed rerun. The login comes from `github_user_profiles`, not from the token. `AuthScope` only has `user_id`.

**Acceptance criteria:**

- [x] `SqlAlchemyRerunStore.create_rerun` sets `triggered_by` from `github_user_profiles.login` where `id == scope.user_id`, in the same transaction as the insert, then `flush()` only. `allow_unscoped=True` stores `null`. No GitHub call. Fail-closed construction is unchanged (`tests/test_rerun_scope.py`).
- [x] Integration test under `-k triggered_by`: scope user `42` with profile login `octocat` produces a rerun row `triggered_by = octocat`. A missing profile stores `null` rather than calling GitHub.
- [x] Webhook rows are not rewritten by rerun. The new row is `trigger = rerun`.

**Verification:**

- [x] `uv run pytest tests/test_rerun_scope.py tests/test_triggered_by_postgres.py`
- [x] Manual check: rerun still returns 409 / 422 for conflict and missing rule version; the login lookup happens only after those decisions, inside the same unit of work

**Dependencies:** Task 4

**Files likely touched:**

- `app/modules/reviews/infrastructure/rerun_store.py`
- `tests/test_triggered_by_postgres.py`

**Estimated scope:** Small: 1-2 files

## Task 8: Expose triggeredBy on RunSession

**Description:** List, detail, rerun, and cancel responses include `triggeredBy` because they all serialize `RunSession`. `null` stays `null`.

**Acceptance criteria:**

- [x] `RunSessionDto.triggered_by: str | None` and `to_run_session_dto` copy `RunListItem.triggered_by`. OpenAPI `RunSession` and `RunDetail` require `triggeredBy` as `string | null`. Both schemas stay closed and in lockstep.
- [x] API test: a list item and a detail for a run with login `octocat` return `"triggeredBy": "octocat"`. A run with `None` returns `"triggeredBy": null`, not omitted.
- [x] Existing runs created before the migration are `null` (covered by the Task 4 row plus this serialization).

**Verification:**

- [x] `uv run pytest tests/test_runs_api.py tests/test_run_dtos.py tests/test_rerun_scope.py tests/test_alembic_heads.py`
- [x] Manual check: the Zod mirror for `RunSession` is red until Task 9. Do not drop `triggeredBy` from the spec to make it green.

**Dependencies:** Tasks 6 and 7

**Files likely touched:**

- `app/modules/reviews/api/dtos.py`
- `app/main.py`
- `contracts/openapi.yaml`
- `tests/test_runs_api.py`
- `tests/test_run_dtos.py`

**Estimated scope:** Medium: 3-5 files

## Task 9: Refresh the Zod snapshot for triggeredBy

**Description:** Regenerate the UI snapshot from the ui commit that adds `triggeredBy` next to `usage`, and point `UI_CONTRACT_COMMIT` at it.

**Acceptance criteria:**

- [ ] Snapshot `runSession` and `runDetail` require `triggeredBy` (`string | null`) and still require `usage`. `UI_CONTRACT_COMMIT` matches `provenance.commit`.
- [ ] `uv run pytest tests/test_runs_api.py tests/test_rerun_scope.py tests/test_openapi_contract.py tests/test_ui_zod_contracts.py tests/test_alembic_heads.py` is green.
- [ ] `uv run pytest -m integration -rs -k triggered_by` with `TEST_DATABASE_URL` is green and nothing is skipped.

**Verification:**

- [ ] Full gates green
- [ ] Manual check: generator input is a clean ui checkout at the agreed commit

**Dependencies:** Task 8, and a ui commit that adds `triggeredBy`

**Files likely touched:**

- `tests/fixtures/ui_zod_contracts.json`
- `tests/test_ui_zod_contracts.py`

**Estimated scope:** Small: 1-2 files

## Task 10: Compute the metrics summary

**Description:** A use case returns totals, a zero-filled UTC day series, and per-workspace daily budget for repositories the caller can see. The route is Task 11. SQL does not embed a second verdict formula or a second pause formula.

**Acceptance criteria:**

- [x] `GetMetricsSummary.execute(scope, *, range, repo)` rejects a missing scope the way other review adapters do (`ValueError` without `AuthScope`, unless the test constructs the SQL adapter with `allow_unscoped=True`). `range` outside `24h|7d|30d` raises `ValueError`. Default the use case does not apply; the router will default `24h`.
- [x] Against a fake port, literal cases: window `7d` zero-fills every UTC date from the start date through today; a succeeded summary-only run counts as `noVerdict`; severities `("critical",)` count as `blocking` via `run_verdict`; `daily_budget_usd = 0` and spend `10` → `paused` false; `daily_budget_usd = 1` and spend `0.60` → `paused` true (`1 - 0.60 < 0.50`); `daily_budget_usd = 1` and spend `0.40` → `paused` false. An invisible `repo` yields zero totals, a zero-filled series, and does not include another user's workspace names. The caller's workspace rows are still returned when `repo` is omitted.
- [x] Integration test `-k metrics_summary` on PostgreSQL: two visible runs on different UTC days (one `succeeded` with a published `security`/`high` finding and two `usage_events`, one `failed` with a non-zero cost) produce status counts, `blocking` for that PR, finding counts, token and cost sums, and a zero day between them. A second user's run is absent. `spentTodayUsd` equals the UTC-midnight sum used by the worker.

**Verification:**

- [x] `uv run pytest tests/test_metrics_summary.py tests/test_metrics_summary_postgres.py`
- [x] With `TEST_DATABASE_URL`, `uv run pytest -m integration -rs -k metrics_summary` does not skip the new test
- [x] Manual check: the adapter's queries are aggregates (no per-run round trip) and the session does not wrap a network call

**Dependencies:** Task 9 (avoids editing `dtos.py` / OpenAPI while Ч.2 is in flight). The use case itself does not read `triggered_by`.

**Files likely touched:**

- `app/modules/reviews/application/get_metrics_summary.py`
- `app/modules/reviews/infrastructure/metrics_summary.py`
- `tests/test_metrics_summary.py`
- `tests/test_metrics_summary_postgres.py`

**Estimated scope:** Medium: 3-5 files

## Task 11: Serve GET /api/metrics/summary

**Description:** Replace the planned metrics operation with a real route. The handler parses `range` and `repo` and calls `GetMetricsSummary`. The response uses the closed schema in the plan. `x-status` and `x-note` come off in this task so `test_spec_operations_match_the_app_routes` stays green.

**Acceptance criteria:**

- [x] `GET /api/metrics/summary` is an `async def` on `api_router` next to `list_runs`. Query `range` enum `24h|7d|30d` default `24h`. Query `repo` optional, same length bounds as `list_runs`'s `repo`. 401 when unauthenticated, 422 on a bad range, 200 with a zero body for an invisible `repo`. No 404 for an unknown repo.
- [x] DTOs are closed Pydantic models (`ApiDto`, camelCase) matching `MetricsSummary`, `MetricsDay`, and `WorkspaceDailyBudget` in `docs/plans/310-plan.md`. OpenAPI replaces the indicative schema, sets `additionalProperties: false` on every object, adds `repo`, and deletes `x-status` and `x-note` on `getMetricsSummary`.
- [x] Wiring lives in `app/bootstrap/reviews_api.py` and passes the request `AuthScope`. A unit test with a fake use case asserts the JSON keys for one day and one workspace, including `"paused": true` and `"noVerdict": 1`.

**Verification:**

- [x] `uv run pytest tests/test_openapi_contract.py tests/test_metrics_summary.py` (route and non-Zod checks; Zod mirror stays red for `usage` and `triggeredBy`)
- [x] `uv run ruff check .`, `uv run ruff format --check .`; `uv run mypy` on the touched Python files
- [x] Manual check: the operation is not in the planned set. Zod registration is Task 12; do not add `MetricsSummary` to `UI_ZOD_COMPONENTS` yet.

**Dependencies:** Task 10

**Files likely touched:**

- `app/modules/reviews/api/dtos.py`
- `app/main.py`
- `app/bootstrap/reviews_api.py`
- `contracts/openapi.yaml`
- `tests/test_metrics_summary_api.py`

**Estimated scope:** Medium: 3-5 files

## Task 12: Register MetricsSummary in the Zod snapshot

**Description:** The UI snapshot gains the metrics schema, and the API mirror starts checking it field for field. This is the Ч.3 contract checkpoint from the issue.

**Acceptance criteria:**

- [ ] Regenerate `tests/fixtures/ui_zod_contracts.json` from the ui commit that defines the same `MetricsSummary`. Add the component to `UI_ZOD_COMPONENTS` in `tests/test_openapi_contract.py` (the map of Zod name → OpenAPI component). `UI_CONTRACT_COMMIT` matches `provenance.commit`.
- [ ] `test_component_schemas_mirror_the_ui_zod_contract` passes for the new component, including `additionalProperties: false` and the required keys in the plan.
- [ ] `uv run pytest tests/test_openapi_contract.py tests/test_ui_zod_contracts.py` is green, and `uv run pytest -m integration -rs -k metrics_summary` with `TEST_DATABASE_URL` is green with nothing skipped.

**Verification:**

- [ ] Full gates green
- [ ] Manual check: names match the ui#186 agreement; if they differ, change OpenAPI and the DTOs in a follow-up commit on this slice before merging, then regenerate again

**Dependencies:** Task 11, and a ui commit whose Zod schema matches `MetricsSummary`

**Files likely touched:**

- `tests/fixtures/ui_zod_contracts.json`
- `tests/test_ui_zod_contracts.py`
- `tests/test_openapi_contract.py`

**Estimated scope:** Medium: 3-5 files

## Task 13: Replace the SYSTEM_DESIGN metrics row

**Description:** Section 12 of the system design still says the metrics operation is post-MVP. Point that row at the implemented contract.

**Acceptance criteria:**

- [x] The «Метрики» row in `docs/SYSTEM_DESIGN.md` §12 names `GET /api/metrics/summary`, `range` (`24h`, `7d`, `30d`), optional `repo`, visibility equal to `GET /api/runs`, and that an invisible `repo` is an empty summary. It does not say «после MVP» or `x-note`.
- [x] The row does not document a budget-edit API. Daily budget remains the §7.3 worker rule (`daily_budget_usd = 0` means unset; pause below the fast limit).
- [x] No other section is rewritten.

**Verification:**

- [ ] Full gates green
- [x] Manual check: `rg "после MVP" docs/SYSTEM_DESIGN.md` finds no metrics-row hit

**Dependencies:** Task 11

**Files likely touched:**

- `docs/SYSTEM_DESIGN.md`

**Estimated scope:** Small: 1-2 files
