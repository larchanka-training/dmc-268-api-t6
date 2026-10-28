# Implementation Plan: Issue #310 — Run audit fields and metrics summary

## Overview

Issue [#310](https://github.com/larchanka-training/dmc-268-api-t6/issues/310) is open and not implemented. `GET /api/runs` returns `RunSession` without spend, no table stores who started a run, and `GET /api/metrics/summary` exists only in `contracts/openapi.yaml` with `x-status: planned`. This plan adds three sequential, separately reviewable slices in this API repo: list-item usage summed from `usage_events`, a nullable `runs.triggered_by` written when a webhook or rerun run is created, and a real metrics summary for the caller's visible repositories and workspaces. The UI dashboard (ui#186) and the docs-site copy in the issue comment are out of this repo. No new dependency. Three pull requests, in order, branch `feat/310-runs-audit-metrics` (or `feat/310-<slice>`), commit trailer `Refs #310`, no closing keyword.

## Architecture Decisions

- Stay on the current layout. Routers stay in `app/main.py` (there is no `app/entrypoints/` yet). Use cases stay under `app/modules/reviews/application/`. SQL stays in `app/modules/reviews/infrastructure/`. Ports are Protocols on the consuming use case. Repositories `flush()` only; `TryEnqueueWebhookRun` and `RerunRun` keep committing. No GitHub or LLM call inside a DB transaction. `AuthScope` stays fail-closed on new adapters (`ValueError` unless `allow_unscoped=True`).
- **Slice 1 — `usage`.** Add a read-model `RunUsage` (`tokens_in`, `tokens_out`, `cost_usd`) on `RunListItem`, defaulting to zeros so existing constructors keep compiling. Fill it in both `SqlAlchemyRunRepository.list_runs` and `get_run` with `coalesce(sum(...), 0)` scalar subqueries on `usage_events` in the existing `SELECT` (same shape as `action_count`). One statement per page, so the statement count does not grow with page size. Sum every event for the run, including `failed` runs (a short usage transaction can outlive a rolled-back attempt). Wire field is `usage { tokensIn, tokensOut, costUsd }`, always present, zeros when there are no rows. `RunDetail` repeats the property (`test_flat_extensions_repeat_their_base_schema`). `budget` on the detail stays as it is: `null` before the first LLM call, and the three numbers match `usage` when `budget` is present. Map `costUsd` with `float(Decimal)` the same way `to_run_detail_dto` maps `RunBudgetDto.cost_usd`. Do not add token limits to `usage`. No migration.
- **Slice 2 — `triggeredBy`.** One new nullable column `runs.triggered_by` `String(255)`, no backfill, new Alembic revision only (current head is `20261024_0039`; re-check the head before adding the revision; id `YYYYMMDD_NNNN`, downgrade drops the column). API field `triggeredBy: string | null` on `RunSession` and therefore on `RunDetail`. Old rows and unknown actors are `null`.
  - Qualifying webhook actor, passed explicitly into `TryEnqueueWebhookRun.execute` from `TriggerFromDelivery`: `synchronize`, `reopened`, and `labeled` where the label is `ai-review`. Value is `PullRequestEvent.sender_login`, including bots. Do not apply the own-bot filter in `project_github_pull_request.py`. A missing sender is explicit `null` (no fallback).
  - `on_ci` and `SweepNoCi` do not pass a sender. `insert_webhook_run` then resolves the push author inside the existing insert transaction: latest `webhook_events` row with `event = 'pull_request'`, `action = 'synchronize'`, non-null JSONB `payload`, matching `installation_external_id`, `payload->'repository'->>'id'`, and `payload->'pull_request'->'head'->>'sha'`, ordered by `received_at desc`. Login is `payload->'sender'->>'login'`. Payload stored only in `payload_s3_ref` is unknown → `null`. No GitHub HTTP. `opened` is not a qualifying action.
  - `TriggerFromDelivery` passes `triggered_by` only when `execute`'s signature accepts that keyword or `**kwargs`. Production `TryEnqueueWebhookRun` accepts it. Existing test fakes that omit it keep working and are not a second code path in production.
  - Rerun: inside `SqlAlchemyRerunStore.create_rerun`, read `github_user_profiles.login` for `AuthScope.user_id` in the same transaction. `allow_unscoped=True` leaves `null`. No token parsing and no GitHub call.
- **Slice 3 — metrics.** New use case `GetMetricsSummary` and a fail-closed SQL adapter. The router only decodes query params and calls the use case. Visibility is `repository_access_predicate` (same as `GET /api/runs`). An unknown or invisible `repo` returns a zero summary (HTTP 200), not another tenant's rows and not 404. `repo` does not filter the workspace block: that block is every workspace the caller can access (`GitHubUserWorkspaceAccess` for `scope.user_id`, intersected with `AuthScope.workspace_ids`).
  - Window end is `now` UTC. `24h` / `7d` / `30d` are rolling `now - 24h`, `now - 7d`, `now - 30d`. Default `24h`, matching the spec today. Invalid `range` is 422.
  - A run, its published findings, and its `usage_events` belong to the window and to the UTC calendar day of `runs.created_at`. Series days are every UTC date from the window start's date through today, ascending, zero-filled.
  - Run status counts are the current `runs.state` (all seven `RunState` values, missing ones as 0).
  - PR verdicts: among runs in the window with a terminal status (`succeeded`, `failed`, `cancelled`, `skipped`), the latest per `code_changes.id` by `(finished_at, id)`. Bucket with `run_verdict` from `list_pulls.py` (which calls `verdict`). `null` from `run_verdict` counts as `noVerdict`. A PR with no terminal run in the window is omitted. Do not reimplement the severity rule in SQL; SQL returns `status`, `summary_only`, and published severities.
  - Published findings match `get_run_review`: `published` and `drop_reason IS NULL`. Severity keys are `critical|high|medium|low|info`. Category keys are `security|correctness|performance|readability`.
  - Average duration is the mean of `finished_at - created_at` in seconds for `succeeded` runs in the set. Empty set is `0` (same zero rule as empty days).
  - `paused` is computed in the use case with the same predicate as `HandleReviewRun._guard`: `daily_budget_usd > 0` and `daily_budget_usd - spent_today_usd < FAST_RUN_COST_LIMIT` (`Decimal("0.50")`). `daily_budget_usd = 0` is not paused. `spentTodayUsd` is `sum(usage_events.cost_usd)` for that workspace since UTC midnight, the same sum as `SqlAlchemyRunLifecycleStore.lock_for_claim`.
- **Zod snapshots.** `tests/test_ui_zod_contracts.py` and `test_component_schemas_mirror_the_ui_zod_contract` compare `RunSession` to `tests/fixtures/ui_zod_contracts.json` field for field. Regenerate only from a committed ui checkout that already has the same fields (`docs/PIPELINE_SPEC.md` §16): `DMC_268_UI_DIR=<ui checkout> node tests/generate_ui_zod_contracts.mjs`, then set `UI_CONTRACT_COMMIT`. Do not hand-edit the snapshot to invent a UI schema. Each slice's wire task therefore lands OpenAPI immediately, and the snapshot task is the last task of that slice.
- Proposed closed `MetricsSummary` (replaces the indicative `tiles` / `series` map; `additionalProperties: false` on every object). Names are the implementation default until the ui#186 executor confirms them before merge (issue text):

```yaml
MetricsSummary:
  required: [range, totals, series, workspaces]
  properties:
    range: { enum: [24h, 7d, 30d] }
    totals:
      required: [runsByStatus, pullsByVerdict, findingsBySeverity, findingsByCategory,
                 succeededAverageDurationSeconds, tokensIn, tokensOut, costUsd]
      runsByStatus: { queued, running, publishing, succeeded, failed, cancelled, skipped }
      pullsByVerdict: { blocking, attention, clean, noVerdict }
      findingsBySeverity: SeverityCounts
      findingsByCategory: { security, correctness, performance, readability }
      succeededAverageDurationSeconds: number  # >= 0
      tokensIn: integer
      tokensOut: integer
      costUsd: number
    series:
      items: MetricsDay
        required: [day, findingsByCategory, runCount, averageDurationSeconds, costUsd]
        day: string format date   # UTC YYYY-MM-DD
    workspaces:
      items: WorkspaceDailyBudget
        required: [name, dailyBudgetUsd, spentTodayUsd, paused]
```

Query `repo` is added beside `range`. Remove `x-status` and `x-note` on `getMetricsSummary` in the same change as the route. `docs/SYSTEM_DESIGN.md` §12 row «Метрики» stops saying «после MVP».

## Task List

### Phase 1: Run usage on the list (Ч.1, first PR)

- [x] Task 1: Aggregate usage on the run read model
- [x] Task 2: Expose usage on RunSession

### Checkpoint: Usage on the wire

- [ ] `uv run pytest tests/test_list_runs.py tests/test_runs_api.py tests/test_run_dtos.py tests/test_run_usage_postgres.py` is green without `TEST_DATABASE_URL` except the integration file, which skips
- [ ] `test_component_schemas_mirror_the_ui_zod_contract` and `tests/test_ui_zod_contracts.py` are still red until Task 3
- [ ] Review with a human before treating Ч.1 as done

- [ ] Task 3: Refresh the Zod snapshot for usage

### Checkpoint: Ч.1 done

- [ ] `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy .`, `uv run pytest`
- [ ] `uv run pytest tests/test_list_runs.py tests/test_runs_api.py tests/test_openapi_contract.py tests/test_ui_zod_contracts.py` green
- [ ] `uv run pytest -m integration -rs -k run_usage` with `TEST_DATABASE_URL` green and nothing skipped
- [ ] Open the PR. Body: What / Why / How to verify / Refs #310. No new dependency line.

### Phase 2: Run initiator (Ч.2, second PR, after Ч.1 is merged or stacked)

- [x] Task 4: Add nullable runs.triggered_by
- [x] Task 5: Store the webhook initiator on insert
- [x] Task 6: Pass the qualifying sender into enqueue
- [x] Task 7: Store the portal login on rerun
- [x] Task 8: Expose triggeredBy on RunSession

### Checkpoint: Initiator on the wire

- [ ] Migration upgrades and downgrades; `uv run pytest tests/test_alembic_heads.py tests/test_rerun_scope.py` green
- [ ] Zod mirror is red until Task 9
- [ ] Before editing `webhook_runs.py`, check open PRs that touch it and comment on any other PR (AGENTS.md). Same check for `dtos.py`, `contracts/openapi.yaml`, and the Zod snapshot (#204 also extends `RunSession`)

- [ ] Task 9: Refresh the Zod snapshot for triggeredBy

### Checkpoint: Ч.2 done

- [ ] Full gates green
- [ ] `uv run pytest tests/test_runs_api.py tests/test_rerun_scope.py tests/test_openapi_contract.py tests/test_ui_zod_contracts.py tests/test_alembic_heads.py` green
- [ ] `uv run pytest -m integration -rs -k triggered_by` with `TEST_DATABASE_URL` green and nothing skipped
- [ ] Second PR, `Refs #310`

### Phase 3: Metrics summary (Ч.3, third PR)

- [x] Task 10: Compute the metrics summary
- [x] Task 11: Serve GET /api/metrics/summary
- [ ] Task 12: Register MetricsSummary in the Zod snapshot
- [x] Task 13: Replace the SYSTEM_DESIGN metrics row

### Checkpoint: Complete

- [ ] Full gates green
- [ ] `uv run pytest tests/test_openapi_contract.py tests/test_ui_zod_contracts.py` green
- [ ] `uv run pytest -m integration -rs -k metrics_summary` with `TEST_DATABASE_URL` green and nothing skipped
- [ ] `GET /api/metrics/summary` has no `x-status` / `x-note`; §12 no longer says «после MVP»
- [ ] Third PR, `Refs #310`
- [ ] Human confirms the `MetricsSummary` names with the ui#186 executor before merge

## Risks and Mitigations

| Risk | Impact | Mitigation |
| ---- | ------ | ---------- |
| Zod snapshot updated by hand, or UI PR lacks the field | High | Snapshot tasks run the §16 generator against a committed ui checkout. Wire tasks expect the mirror to be red until then. |
| List usage queried per row | High | Task 1 asserts statement count is equal for 1 and 3 runs via `before_cursor_execute` (pattern in `tests/test_run_origin_guard_postgres.py`). |
| Verdict or `paused` reimplemented in SQL | High | Use `run_verdict` and `FAST_RUN_COST_LIMIT`. Integration fixture: one critical finding → `blocking`; budget `0.49` left → `paused`; budget `0` → not paused. |
| `webhook_events.payload` offloaded to S3, or `opened` instead of `synchronize` | Med | Lookup returns `null`. No GitHub fetch inside the insert transaction. See open questions. |
| Parallel edits to `RunSession` (#204) or `webhook_runs.py` (other sprint-4 roles, #293) | Med | Check open PRs before editing; comment on the other PR. Do not edit those branches. |
| Alembic head moved past `20261024_0039` | Med | `down_revision` is whatever `alembic heads` prints that day. `tests/test_alembic_heads.py` must stay at one head. |
| `float(Numeric(12, 6))` drift between list and detail | Low | Both sides `float()` the same `sum(cost_usd)`. The integration test compares the two HTTP numbers. |
| Signature check hides a fake that should record the initiator | Low | Production enqueuer declares `triggered_by`. The new unit test uses a double that accepts it. PostgreSQL tests call the real store. |

## Open Questions

- Confirm `MetricsSummary` property names, the `day` date (instead of the indicative `bucketStart` date-time), duration in seconds, and `noVerdict` with the ui#186 executor before merging Ч.3. The issue says names and requiredness are agreed before merge. The schema above is the default to implement if they accept it.
- Confirm period membership: this plan attributes runs, findings, tokens, cost, and series buckets to `runs.created_at`, and the workspace "today" spend to `usage_events.created_at` since UTC midnight. Say so if the dashboard should use `finished_at` or event time instead.
- For a CI or sweep run whose head arrived only on `pull_request.opened` (no `synchronize`), this plan stores `triggeredBy: null`. Say so if `opened`'s `sender.login` should count as the push that gave the head.

## Out of scope (do not implement)

- UI pages, Zod source in `dmc-268-ui-t6`, and the docs-site edits in the issue comment (`roadmap.mdx`, `features.mdx`, ui#173, ui#186).
- Changing `workspaces.daily_budget_usd` through the API.
- An audit log of actions other than review runs.
- Backfill of `triggered_by` for existing runs.
- A new Python dependency.

## Parallelization

The three slices share `RunSession`, `contracts/openapi.yaml`, and the Zod snapshot. Ship them in order: Ч.1, then Ч.2, then Ч.3. Inside a slice the tasks are sequential. Ч.3's use case does not read `triggered_by`.
