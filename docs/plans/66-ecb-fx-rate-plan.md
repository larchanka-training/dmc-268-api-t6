# Implementation Plan: ECB EUR→USD Rate for LLM Accounting

## Overview

Replace the manually maintained `LLM_EUR_TO_USD_RATE` from PR #68 with the
latest usable ECB daily EUR→USD reference rate. One immutable quote must price
each LLM call's pre-call budget reservation and its paid EUR usage. Retain the
USD catalog floor and the existing conservative paid-answer behavior. Surface
the quote's source and observation date in the call trace and manual live-run
output. No new dependency or database migration is planned.

This extends [issue #66](https://github.com/larchanka-training/dmc-268-api-t6/issues/66)
**inside the existing [PR #68](https://github.com/larchanka-training/dmc-268-api-t6/pull/68)**
on `fix/66-llm-live-run-gaps` (planning HEAD `1d6a795`, PR base `fa31700`).
The current manual-rate implementation in that PR is the starting point.
After plan approval, extend and retest the same branch, then update the same
PR body and request review on its new head. Do not create a second branch or PR.

## Source and contract

- Fetch the ECB Data Portal's daily USD per EUR reference series
  `EXR/D.USD.EUR.SP00.A`, requesting one latest observation in CSV. The
  [ECB API examples](https://data.ecb.europa.eu/help/api/data-examples)
  document `lastNObservations` and `format=csvdata`; the
  [series definition](https://data.ecb.europa.eu/data/data-categories/ecbeurosystem-policy-and-exchange-rates/exchange-rates/reference-rates)
  identifies USD as the quoted currency and EUR as the denominator. Do not
  invert the value. The [ECB reference-rate page](https://data.ecb.europa.eu/key-figures/ecb-interest-rates-and-exchange-rates/exchange-rates)
  says rates are normally published around 16:00 CET on working days and are
  informational, not transaction prices.
- Parse the CSV with the standard library and `Decimal`. Require exactly one
  matching daily USD/EUR spot-average observation, a valid `TIME_PERIOD`, and
  a positive finite `OBS_VALUE`. Reject HTML/error bodies, duplicate or
  mismatched rows, future dates, and malformed values. Bound response size and
  use a short HTTP timeout. No key or LLM Authorization header goes to ECB.
- An `FxQuote` value contains `rate_usd_per_eur`, `observation_date`,
  `source` (the ECB series key), and `retrieved_at`. Use its observation date,
  not the retrieval time, for freshness checks.

## Architecture decisions

1. **One process-local quote provider.** A dedicated infrastructure adapter
   owns ECB HTTP, parsing, cache, and an `asyncio.Lock`. One gateway process
   shares it across concurrent runs. The first caller fetches; other callers
   await that result. A validated quote has a one-hour refresh interval.
   Failed refreshes are throttled for five minutes, with the last validated
   quote retained. This avoids a request for every LLM call or a cold-start
   stampede. The clock and HTTP client are injectable for deterministic tests.
2. **Startup and explicit stale policy.** `WorkerSettings.from_environment`
   currently rejects an EUrouter route when `LLM_EUR_TO_USD_RATE` is absent.
   Remove that synchronous requirement: validate local LLM settings at startup,
   but do not make ECB availability a deployment/startup prerequisite. A quote
   is fetched lazily on the first known EUR-capable call; an optional startup
   warm-up may log a warning but must not abort worker startup. CLI uses the
   same runtime policy and reports an FX outage as a gateway failure rather
   than a configuration error. A quote remains usable for at most seven calendar
   days from its ECB observation date, allowing ordinary weekends and longer
   holiday closures. After the one-hour refresh interval, a failed fetch may
   use that still-usable quote with a warning and a `stale_cache` marker in
   trace metadata. Never fall back to 1.1204, 1.1225, or an unlabelled rate.
   If no usable quote exists, fail before a known EUR-capable EUrouter LLM
   call as retryable `llm_unavailable` with zero provider calls and no budget
   charge. On an unknown endpoint whose paid EUR response first reveals the
   need for FX, preserve the paid raw trace and charge the existing
   conservative USD estimate before failing without an LLM retry. This
   distinction prevents a duplicate paid call.
3. **One quote snapshot per provider call.** Obtain a usable quote before the
   pre-call budget check for known EUrouter models that can route to EUR
   endpoints. Pass the same immutable object through the price-ceiling
   calculation and paid `usage.cost` conversion; never reread a mutable
   settings field after the response. Other endpoints may fetch lazily if an
   EUR paid response appears; their configured USD price is the pre-call
   estimate, so the guarantee is limited to known catalog routes. A refresh
   during another run cannot change an in-flight call's rate. Different calls
   may use newer ECB observations; `usage_events.cost_usd` remains additive.
   Recheck the attempt deadline after any FX wait and before the LLM request.
4. **Preserve the known-route floor.** At the selected ECB quote, the effective
   known EUrouter price is the componentwise maximum of the USD catalog floor
   and the published EUR route ceiling multiplied by the quote. Use it for
   both pre-call reservation and conservative accounting of malformed paid
   cost metadata. `ModelPrice` defaults remain USD catalog values; remove the
   runtime dependence on `LLM_EUR_TO_USD_RATE` from `LlmSettings.from_env`
   and `WorkerSettings.from_environment`.
   This bounds published route prices at the selected quote and estimated
   tokens, not future provider price changes or tokenizer error.
5. **Persist provenance without changing `usage_events`.** Add FX metadata
   (`source`, `observation_date`, rate, and whether a stale cache was used) to
   `llm.call` request metadata for each call that uses a quote. Keep the raw
   provider response untouched. The request/response trace already persists
   per-call JSON; `usage_events` remains the insert-only USD aggregate. Show
   the same fields per call in the CLI JSON and the `LLM live run` summary so
   an operator can reconcile costs. Log source/date and refresh outcome, not
   API keys, prompts, or raw model answers.
6. **Keep FX HTTP outside transactions.** Resolve a quote before
   `UsageLedger.run_cost_usd()` opens its short read session. Existing ledger
   and trace writes still happen after the LLM call in their own short
   transactions. Neither ECB nor LLM network calls run within a DB
   transaction. The ECB client is composed once per worker process and closed
   with the worker; the database-free CLI uses the same adapter or an injected
   fake. ECB failure must not silently restart a paid LLM request.

## Dependency graph and PR coordination

```text
current PR #68 HEAD ──> ECB adapter/cache tests ──> worker startup + gateway snapshot
gateway snapshot ──> trace/CLI ──> workflow/docs ──> gates/review ──> push PR #68
```

- Work in this checkout and update PR #68. Its own files need no separate
  ownership comment. `AGENTS.md` still requires a comment on **each other**
  open PR before editing one of its files. The existing
  [#65 coordination comment](https://github.com/larchanka-training/dmc-268-api-t6/pull/65#issuecomment-5999397578)
  covers the earlier currency edits to `settings.py`, `gateway.py`,
  `tests/test_llm_gateway.py`, and `PIPELINE_SPEC.md`, but not the new ECB
  scope in `app/bootstrap/llm_gateway.py`, `app/modules/reviews/application/llm.py`,
  workflows, CLI tests, or `SYSTEM_DESIGN.md`; post an updated #65 comment
  before those edits. #69 owns `app/worker.py`, `tests/test_queue_messages.py`,
  and `SYSTEM_DESIGN.md`; #75 and #61 own `PIPELINE_SPEC.md`; #75, #74,
  #69, and #58 own `SYSTEM_DESIGN.md`. Comment on each relevant still-open PR
  before touching its file, or defer that edit until it merges. The new ECB
  adapter/test files and these two plan files have no observed ownership
  overlap. Recheck the open-PR list before development because it can change.
- Rebase PR #68 on current `main` before requesting review and again before
  merge; a push changing the diff dismisses any existing approval.

## Phases and checkpoints

### Phase 1: Independent ECB adapter

- [ ] Task 1: Parse and validate one official ECB observation.
- [ ] Task 2: Add bounded, single-flight cache and stale/unavailable policy.

**Checkpoint A:** Mock HTTP/clock tests pass, including concurrency,
timeout, weekend/holiday age, malformed responses, and no-key requests.

### Phase 2: Worker and gateway integration in PR #68

- [ ] Task 3: Replace worker's manual-rate startup gate and bind one quote to
      pre-call pricing and paid usage.
- [ ] Task 4: Persist provenance and compose the adapter for worker/CLI.

**Checkpoint B:** Known-route budget and EUR paid-usage tests prove the same
quote was used even when the cache refreshes concurrently. No ECB HTTP occurs
while a DB session is open; no LLM call occurs without a usable required quote.

### Phase 3: Operations and publication

- [ ] Task 5: Remove the manual variable from deployment and live-run
      workflows and their contract tests.
- [ ] Task 6: Update operator and product contracts for ECB sourcing.
- [ ] Task 7: Verify with all gates and independent review, update/push PR #68,
      then smoke-test staging and live-run behavior after merge.

**Checkpoint C:** `uv run ruff check .`, `uv run ruff format --check .`,
`uv run mypy .`, and `uv run pytest` pass. Docs, trace, and CLI agree about
source/date and failure behavior. PR #68's current head is reviewed and
rebased on `main` before review/merge; staging egress is checked after merge
from the worker container without exposing keys.

## Risks and limits

| Risk | Mitigation |
|---|---|
| ECB outage blocks cold-cache EUrouter calls, including a route that would have answered in USD | Explicit fail-closed behavior, short timeout, bounded stale cache, operational alert/log and smoke test |
| Weekend/holiday quote looks old even when it is latest | Seven-day observation-age window and tests for four-day holiday closures; do not equate `retrieved_at` with publication date |
| Cache refresh changes FX mid-call | Freeze one `FxQuote` at call start and pass it to both pricing and usage conversion |
| Many runs refresh at once | Process-local lock, double-checked cache, and failed-refresh backoff; separate worker processes may have different snapshots, each recorded per call |
| ECB returns surprising CSV, stale data, or a revised observation | Validate series/date/value, reject older replacement, record source/date/rate, and test parser error paths |
| New ECB HTTP crosses a DB transaction | Resolve quote before ledger read and verify with a transaction-aware fake |
| Other open PRs change touched files | Start with new adapter; post coordination comments on each still-open intersecting PR before an edit, then rebase PR #68 on main |

## Plan approval

The proposed one-hour refresh, seven-day maximum observation age, lazy
cold-cache fetch with no worker startup failure, and fail-closed LLM call when
no usable quote exists are product decisions for human review. Per the
agent-loop skill, development starts after this plan is approved.
