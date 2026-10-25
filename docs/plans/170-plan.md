# Implementation plan: overview body budget, run.step fields, post-#97 doc drift (#170)

## Status and baseline

Issue: https://github.com/larchanka-training/dmc-268-api-t6/issues/170  
Branch (recommended): `fix/170-overview-budget-and-docs`  
Baseline: `origin/main` `1d05fbe` (post-#172). Task checklist: [170-todo.md](170-todo.md).

**Baseline pytest** (planning run, no code changes):

```sh
uv run pytest tests/test_findings_post_processor.py tests/test_run_stream.py tests/test_openapi_contract.py -q
```

Result: **PASS** — 234 tests passed.

Note: issue AC says the long-path regression is “red on 7546e53”; on current `main` the test
`test_character_cap_drops_only_additional_findings` still **passes** because it asserts the
**old** behaviour (`Spill note` absent, body length may exceed 4000). The fix replaces those
assertions (RED first), then implements the new budget rules (GREEN).

## Overview

Three independent slices from acceptance review of #97:

1. **Ч.1 (P2)** — Fix review overview/body budgeting in `FindingsPostProcessor` so large
   “Checked files” lists cannot silently drop body-only findings or cut mid-finding; align
   check-run “в теле ревью” with what readers see; cap/truncate checked paths.
2. **Ч.2** — Extend `RunStepEvent` (and the `run_step` NOTIFY → SSE path) with `attempt` and
   an LLM call discriminator for `tool = llm.call`, for dmc-268-ui-t6#84.
3. **Ч.3 (P3)** — Reconcile docs and local compose/env with code after #97; do not reopen
   #194’s deferred SD/BA catalog drift or #210’s PIPELINE_SPEC §2 / SD §6.2 conventions line.

Out of scope (issue): #165 RunAction catalog, #171 GitLab payload, #169 GitLab acceptance.

## Issue acceptance criteria (cross-reference)

| AC | Requirement | Plan slice |
| --- | --- | --- |
| Ч.1 | Every non-inline finding appears in the review body or is counted in an explicit overflow line with a run link; check-run body count matches; checked files capped and exclude non-reviewable paths (or spec change) | Tasks 1–6 |
| Ч.2 | `RunStepEvent` has `attempt` and LLM call kind for `llm.call`; OpenAPI + contract + stream frame tests; comment on ui#84 with PR link | Tasks 7–11 |
| Ч.3 | Each ч.3 bullet in the issue corrected in doc or code | Tasks 12–20 |

DoD: full backend gates with `TEST_DATABASE_URL`, docs updated, PR review per `AGENTS.md`.

## Current behaviour (validated on `main`)

### Ч.1 — `_render_body` / `_overview`

```281:300:app/modules/reviews/application/findings_post_processor.py
def _render_body(
    ...
    if len(prefix) >= 4000:
        return prefix
    return body[:4000]
```

- Overview (verdict, risk, optional severity-threshold line, **Checked files**) is part of
  `prefix`, before prose and Additional findings.
- If `len(prefix) >= 4000`, the entire Additional findings section is omitted (`return prefix`).
- Otherwise the full body is truncated with `body[:4000]`, which can split a finding mid-line.
- `_overview` lists **every** key in `hunk_lines` (`run_repository.py:668` builds keys from all
  `code_change_diffs` rows). Paths with `review_patch is null` still appear as `- path` with empty
  hunk sets — inconsistent with PIPELINE_SPEC intent that skipped files are not “checked”.
- `findings_hash` (`verdict.findings_hash`) hashes finding coordinates/content only, not
  `review_body` text — layout changes must not alter hash if finding buckets are unchanged.
- Check-run `body_count` (`run_lifecycle_store.py:345`) is `len(published_findings) - inline_count`,
  not “findings visibly listed under Additional findings”. After Ч.1 it must match the body.

**#172 sync:** Any new check-run summary branch must keep `details_url=report.run_url` in every
`CheckRunView` path (`check_runs.py:132-193`) and stay consistent with PIPELINE_SPEC §7 (run link
in `succeeded` summary). Do not edit the §7 post-table paragraph without syncing SD §7.3 and
SECRETS.md.

**#149 sync:** `**Below severity threshold ({severity}):** {n}` in `_overview` must remain; gate:
`tests/test_severity_threshold_postgres.py`.

### Ч.2 — Run step pipeline

- NOTIFY payload today (`run_action_records.py:61-69`): `run_id`, `tool`, `index`, `ok`,
  `started_at`, `duration_ms` only.
- `RunStep` dataclass and `_run_step_frame` (`run_events.py:28-36`, `main.py:701-710`) mirror that.
- `llm.call` request JSON already includes `attempt` (`LlmCallRecord.request_json`, `llm.py:138-154`)
  but it is **not** copied into NOTIFY/SSE.
- Conventions vs review: both use `tool = "llm.call"`. Gateway uses `StructuredTask.operation`
  (`conventions` / `review` in `llm/models.py`) but that value is not stored on the run action
  request today — only `kind` (primary/retry/repair/fallback).
- #165 comment (issue): `RunStepEvent.attempt` should equal `runs.attempt` / `ClaimedAttempt.attempt`
  / `RunCallContext.attempt` (`worker.py:694-701`).
- UI constraint: step frames have **no** SSE `id:`; do not add replay without an explicit product
  decision (issue kickoff; ui `runStreamConnect.ts`).

### Ч.3 — Doc/code drift (inventory)

| Issue bullet | Current mismatch | Fix locus |
| --- | --- | --- |
| PIPELINE_SPEC GitLab checkpoint “перед HTTP и после ответа” | GitLab gateway has no RunProvider checkpoint hooks like GitHub | Doc: align phase table with actual trace (or add checkpoints — prefer doc unless product requires parity) |
| T14/T15 without GitLab check-run caveat | T16 row mentions GitLab; T14/T15 text implies universal check-run | Doc clarification in T14/T15 footnotes |
| §5.2 no GitLab rows | Classification only in SD | Add GitLab-oriented rows or cross-ref SD §8.4 |
| SECRETS `GITLAB_BASE_URL` “базовый URL API” | Code appends `/api/v4` (`gitlab_review_publication.py`) | Doc: instance root URL |
| `docker-compose.yml` worker | No `GITLAB_TOKEN` / `GITLAB_BASE_URL` | Add to worker `environment:` |
| `.env.example` | GitLab comment above `WORKER_ID` | Reorder comment block per issue |
| `openapi.yaml` `/api/stream` | Summary “Subscribe to run status changes”; example omits `run.step` | Summary + description/example |
| BACKEND_ARCHITECTURE | Missing `gitlab.publish_review`, run_step channel/hub, `llm.review_output` in T8 narrative | Update worker table and SSE bridge section (~375-404 on current `main`) |
| PIPELINE_SPEC workspace_id “без SELECT” | Listener still SELECTs for visibility (`run_events.py:225-241`) | Doc: document visibility SELECT; optional future optimization out of scope |
| CONTEXT / TEST_PLAN GitLab | “specified only” without #97 adapter pointer | Add pointers to GitLab gateway / #97 |
| T16 / §6 codes | Target codes mixed with implemented | Mark target vs implemented per code |
| `97-todo.md` / `97-plan.md` | Status lines stale (Tasks 7–8 open; body budget AC wrong) | Update completion + defer body-budget AC to #170 |

**Deferred elsewhere:** #194 (broader SD/BA step catalog), #210 (PIPELINE_SPEC §2 conventions line,
SD §6.2). #172 merged first — PIPELINE_SPEC §7 table/link rules are authoritative for check-run text.

## Architecture decisions

### D-1 — Body budget algorithm (Ч.1)

Replace the two special cases (`prefix >= 4000` drop findings; blind `body[:4000]`) with a
**deterministic packer** under 4000 characters:

1. Fixed head: `## Review summary` + overview core (verdict, risk summary, optional below-threshold
   line).
2. **Checked files** — sorted reviewable paths only (`review_patch is not null` on the diff
   snapshot — see D-2), capped with a trailing summary line (e.g. `… и ещё N файлов`).
3. Prose block (Main issue / Done well / Effort) — keep intact; shrink checked list before prose.
4. **Additional findings** — render in severity/order until budget exhausted; never cut inside a
   single finding bullet.
5. If any body-only findings remain unpublished in the string, append a footer line
   `**Ещё N находок:** {run_url}` (Russian per issue AC). `run_url` is injected at the
   application layer that already knows `PORTAL_URL` (not inside the pure post-processor), using
   metadata from the processor (`omitted_body_only_count` or similar).

Final stored `review_body` length **must be ≤ 4000** (issue AC and GitHub comfort). This **supersedes**
97-plan’s “path list may exceed 4000” decision — document the change in `97-plan.md` / `97-todo.md`.

### D-2 — “Checked files” set (Ч.1)

Use **reviewable diff snapshots** for the overview list: paths where the run stored a non-null
`review_patch` (same signal as prompt input in `get_run_diff.py`). Keep full `hunk_lines` for
coordinate validation unchanged unless tests prove otherwise.

Alternative (reject unless AC reinterpreted): change PIPELINE_SPEC definition of “checked” — heavier,
cross-repo.

### D-3 — Check-run body count (Ч.1)

Define `body_count` for succeeded check-runs as the number of non-inline findings **accounted for in
the review body**: visible Additional findings bullets plus any count embedded in the overflow line.
Inline count stays separate (`inline: N` in summary).

**Resolved in #170:** by construction that total equals `len(published findings) - inline`, because
every body-only finding is either a visible bullet or counted by the footer. The implementation
keeps that formula and adds bucket tests (including moved-from-inline) instead of parsing markdown
or persisting a separate counter.

### D-4 — `RunStepEvent.attempt` (Ч.2)

- Add **required** integer `attempt` on every step event (minimum 1 when run has been claimed).
- Source: `runs.attempt` at insert time. Prefer reading `Run.attempt` inside
  `insert_run_action` (one SELECT under existing row lock) so all tools get a consistent value
  without threading through every `traced_step` callsite.
- Extend NOTIFY JSON and `RunStep` dataclass; listener `run_step_listener._event` parses it.

### D-5 — LLM call kind on `llm.call` (Ч.2)

- Add optional OpenAPI property `llmOperation` (string enum `conventions` | `review`) on
  `RunStepEvent`, present when `tool == "llm.call"`, omitted otherwise.
- Persist on the run action by extending `LlmCallRecord.request_json()` with `operation` copied from
  `StructuredTask.operation` at gateway record time (already `conventions` / `review` in
  `llm/models.py`).
- Copy from stored request into NOTIFY payload at `insert_run_action` when `tool == "llm.call"`, or
  denormalize at NOTIFY time from the `request` dict already passed to `insert_run_action` for LLM
  calls.

Do **not** infer from step index relative to `llm.repo_conventions` in the SSE layer — replay and
partial traces make order fragile; persisted metadata matches #165 intent.

### D-6 — SSE frame shape unchanged (Ч.2)

Keep `event: run.step` without `id:`. New fields only inside JSON `data`. Update stream tests’
expected JSON literals accordingly.

### D-7 — Documentation edits (Ч.3)

Prefer **doc fixes** where code is intentionally simpler than the pre-#97 target model (GitLab
checkpoints, workspace_id SELECT). Prefer **code fixes** for operator-facing drift (compose, SECRETS,
`.env.example`). When touching PIPELINE_SPEC §7 table or link rules, sync SD §7.3 + SECRETS + CICD
§2.1 per #172 comment.

## Test strategy

| Area | Tests |
| --- | --- |
| Ч.1 | Extend `tests/test_findings_post_processor.py` (incl. lines 285-301 scenario: 50 long paths + body-only finding); add unit tests for truncation footer and checked-file cap; `tests/test_severity_threshold_postgres.py`; check-run integration if body_count storage changes (`tests/test_review_run_worker.py` / `test_check_run_view_sends_the_run_link...` when summary changes) |
| Ч.2 | `tests/test_openapi_contract.py` (`test_stream_declares_run_step...`); `tests/test_run_stream.py` frame assertions; `tests/test_run_step_postgres.py` NOTIFY payload; optional gateway test that `operation` appears in `llm.call` request |
| Ч.3 | No new tests — review-only; run gates to ensure no accidental code breakage |

Full gates: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy .`,
`uv run pytest` with `TEST_DATABASE_URL`.

## Merge order (if splitting PRs)

All parts fit one branch; if split for review size:

1. **PR A (P2)** — Ч.1 only + minimal PIPELINE_SPEC/97-plan note on body budget. Refs #170.
2. **PR B** — Ч.2 + OpenAPI + PIPELINE_SPEC step payload lines. Refs #170, comment on ui#84.
3. **PR C** — Ч.3 documentation and compose/env only. Refs #170.

Merge **A before B** (no conflict expected). **C** can parallelize with A/B but should rebase after
OpenAPI changes in B to avoid yaml conflicts.

Single PR is acceptable if diff stays reviewable (~15–25 files, mostly docs for ч.3).

## Risks and mitigations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| `findings_hash` / idempotency regression | High | Run `tests/test_publish_run_review.py`, GitLab publication tests; confirm hash excludes body |
| Severity threshold line broken | Med | Keep `_overview` below-threshold block; run postgres severity tests |
| UI Zod mirror drift | Med | If OpenAPI changes, follow repo convention: update ui contract in ui repo (separate PR) per past issues — note in PR body |
| NOTIFY payload size | Low | Two small fields; payload already well under 8 KB |
| Russian overflow string on English repos | Low | Product already uses Russian check-run copy; match issue AC wording |
| Doc-only PR scope creep (#194/#210) | Med | Stick to issue ч.3 bullet list; link deferrals |

## Open questions (for tech lead / user)

1. **Overflow footer language** — **Решено (A):** `**Ещё N находок:** {run_url}` на обоих
   провайдерах (GitHub и GitLab), без локализации и без ветвления по VCS.
2. **`attempt` on steps before first claim** — **Решено (B):** в NOTIFY/SSE копировать
   `runs.attempt` на момент `insert_run_action`; инвариант `attempt >= 1` (assert/ошибка, если
   insert до claim); OpenAPI `attempt` required, `minimum: 1`.
3. **UI contract** — **Решено (B):** API-PR #170 + в том же окне маленький UI-PR (Zod
   `RunStepEvent` / `attempt` + `llmOperation`, контрактный тест); прогресс-бар на poll не
   переводим. Комментарий в ui#84 со ссылками на оба PR (или api-PR + follow-up UI).
4. **Single vs split PR** — **Решено (A):** один api-PR на ветке
   `fix/170-overview-budget-and-docs` (Ч.1+Ч.2+Ч.3); UI Zod — отдельный маленький PR
   в том же окне (см. вопрос 3).
5. **GitLab publication checkpoints** — **Решено (A):** в PIPELINE_SPEC (и смежных местах ч.3)
   описать фактическое поведение шлюза GitLab после #97 — без HTTP checkpoints «перед/после»
   как у GitHub; parity checkpoints — вне #170 (отдельный issue при необходимости).

## References

- #97 acceptance comment (UI step fields): issue #97#issuecomment-6079948569
- #165 attempt source: issue #165#issuecomment-6239860269
- #172 merge notes: issue #170#issuecomment-6275817124
- #194 / #210 deferrals: issue #170#issuecomment-6275546260
- Consumer: https://github.com/larchanka-training/dmc-268-ui-t6/issues/84
