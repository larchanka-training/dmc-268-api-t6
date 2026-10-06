# Tasks: ECB EUR→USD Rate for LLM Accounting

Implement in the current `fix/66-llm-live-run-gaps` checkout and update
existing PR #68. Do not create another branch or PR. Before editing a file
owned by **another** open PR, comment there or wait for its merge as required
by `AGENTS.md`. Use test-first development and mark a task only after its
criteria pass.

## Task 1: Parse one official ECB rate

**Description:** Add a provider adapter for ECB series
`EXR/D.USD.EUR.SP00.A?lastNObservations=1&format=csvdata`, with an immutable
quote carrying rate, source, observation date, and retrieval time.

**Acceptance criteria:**

- [x] A valid daily USD/EUR row yields an exact positive `Decimal` USD-per-EUR
      quote; the value is not inverted.
- [x] Wrong series, duplicate/missing rows, invalid/future date, zero/NaN/
      infinite rate, oversized body, and HTTP or CSV errors are rejected.
- [x] The HTTP request uses a short timeout and sends no LLM credentials.

**Verification:**

- [x] Mock HTTP tests with literal ECB-shaped CSV pass.
- [x] Confirm the adapter has no database dependency or new package.

**Dependencies:** Plan approval; new files have no observed PR overlap.

**Files likely touched:** `app/modules/reviews/infrastructure/llm/ecb_fx.py`,
`tests/test_ecb_fx.py`.

**Estimated scope:** Small (2 files).

## Task 2: Share a bounded quote cache across concurrent runs

**Description:** Make quote refresh single-flight within one worker process.
Refresh after one hour, throttle failed refreshes for five minutes, and permit
the last validated quote for at most seven calendar days from observation.

**Acceptance criteria:**

- [x] Concurrent callers on a cold or expired cache make one ECB request and
      receive the same immutable quote.
- [x] A failed refresh uses a still-usable quote with a `stale_cache` marker;
      no quote or a quote older than seven days yields an explicit unavailable
      result. A failed refresh does not overwrite a newer quote.
- [x] Cancellation and a later retry do not leave the cache locked or poison
      its state; UTC wall time decides observation age and monotonic time
      decides refresh intervals.

**Verification:**

- [x] Clock-controlled asyncio tests cover weekends, a long holiday, age
      boundary, concurrent calls, outage, and recovery.
- [x] Check warning content contains source/date but no key or response body.

**Dependencies:** Task 1.

**Files likely touched:** `app/modules/reviews/infrastructure/llm/ecb_fx.py`,
`tests/test_ecb_fx.py`.

**Estimated scope:** Small (2 files).

## Checkpoint A: Adapter

- [x] Tasks 1–2 focused tests pass.
- [x] Recheck PR ownership. Before overlapping edits, post updated coordination
      on #65 and comments on other affected open PRs, or wait for their merge.

## Task 3: Replace startup gate and bind one quote to accounting

**Description:** Replace the static environment rate in the gateway path with
one per-call ECB quote. Remove the worker's synchronous requirement for
`LLM_EUR_TO_USD_RATE`. Keep PR #68's known-route USD floor and conservative
paid-answer estimate.

**Acceptance criteria:**

- [x] A known EUrouter call obtains a usable quote before its pre-call budget
      read. That exact rate drives the EUR route ceiling and the paid EUR
      `usage.cost` conversion, even if the shared cache refreshes during the
      LLM request.
- [x] Cold-cache ECB failure/staleness prevents the provider request with
      retryable `llm_unavailable`, zero calls, and no charge. For a paid EUR
      answer from an unknown endpoint, failed lazy FX resolution records raw
      trace and conservative USD usage once, then fails without another LLM
      request.
- [x] USD answers retain their current accounting, EUR answers remain exact to
      six decimal USD places, the attempt deadline is rechecked after FX wait,
      and no ECB request occurs inside a DB transaction.
- [x] Worker settings with a known EUrouter route no longer require an env
      rate, and ECB outage alone does not prevent worker startup. The first
      relevant call fetches lazily; with no usable quote it fails before LLM.

**Verification:**

- [x] Gateway tests assert literal prices and a budget boundary above the
      historical 1.1225 rate, paid conversion, concurrent refresh, outage,
      and transaction separation.
- [x] Worker startup/config tests and existing currency and
      malformed-paid-answer tests still pass.

**Dependencies:** Task 2 and relevant coordination comments on still-open PRs,
or their merges. Work remains in PR #68.

**Files touched:** `app/modules/reviews/infrastructure/llm/gateway.py`,
`settings.py`, `app/worker.py`, `app/bootstrap/llm_gateway.py`,
`tests/test_llm_gateway.py`, `tests/test_queue_messages.py`,
`tests/test_llm_gateway_cli.py`, `tests/test_llm_transport_currency.py`.
Worker and CLI provider composition with dedicated ECB HTTP client lifecycle
moved here from Task 4 so both entrypoints stay operational. The legacy
`LLM_EUR_TO_USD_RATE` is ignored at runtime; known EUrouter calls require a
usable ECB quote before the provider request. Task 4 retains provenance/output.

**Estimated scope:** Medium (5 files). Write the worker startup test before
removing the fail-fast check, then the gateway boundary tests before code.

## Task 4: Record quote provenance in all entrypoints

**Description:** Persist source/date/rate/stale marker with each quoted
`llm.call` and surface it through the worker and database-free CLI composition
added in Task 3.

**Acceptance criteria:**

- [x] `llm.call` request metadata includes quote source, observation date,
      rate, and cache status without altering the raw provider response or
      `usage_events` schema.
- [x] CLI/live-run emits per-call FX provenance in JSON, including failures
      after a paid answer. Verify worker HTTP client lifecycle and CLI
      cold-cache ECB failure remain correct after metadata integration.
- [x] No LLM API key or prompt appears in ECB requests, quote logs, or new
      trace fields.

**Verification:**

- [x] Trace serialization, worker composition, and CLI tests pass with fake
      ECB and LLM transports; no live network is needed in CI.
- [x] Inspect the `llm.call` metadata contract and response preservation.

**Dependencies:** Task 3 and updated #65 coordination for the bootstrap/trace
files, unless #65 merges first.

**Files likely touched:** `app/modules/reviews/application/llm.py`,
`app/bootstrap/llm_gateway.py`, `tests/test_llm_gateway_cli.py`,
`tests/test_llm_gateway.py`.

**Estimated scope:** Medium (4 files; worker and CLI composition are in Task 3).

## Checkpoint B: Gateway and entrypoints

- [x] Focused gateway, trace, worker, and CLI tests pass.
- [x] All ECB and LLM network calls are outside DB transaction boundaries.
- [x] The same quote is demonstrably used before and after one paid call.

## Task 5: Remove the manual FX variable from workflows

**Description:** Stop propagating `LLM_EUR_TO_USD_RATE` in staging and manual
live-run workflows and update their configuration tests. The worker fetches
ECB itself, so staging must allow outbound HTTPS to `data-api.ecb.europa.eu`.

**Acceptance criteria:**

- [x] Staging `worker.env` and live-run workflow no longer read or require
      `vars.LLM_EUR_TO_USD_RATE`; empty/legacy variable cannot override ECB.
- [x] GitHub Step Summary shows each call's quote source/date/rate/stale-cache
      marker, including paid-answer failures; all CI tests remain network-free
      and any real ECB smoke check is explicitly manual.
- [x] No silent static FX fallback remains in worker or CLI. Repository variable
      deletion waits for post-deploy verification; Task 6 updates the operator
      instructions. This task does not delete the remote variable.

**Verification:**

- [x] Deployment and live-run workflow contract tests pass; `rg` finds no
      runtime dependence on `LLM_EUR_TO_USD_RATE`.

**Egress verification:** staging worker uses Compose's project default network,
without `internal: true` or `network_mode: none`; its outbound HTTPS to
`data-api.ecb.europa.eu` still needs a manual smoke check after deployment.

**Dependencies:** Task 4 and updated #65 coordination for the live-run and CI
workflow, unless #65 merges first.

**Files likely touched:** `.github/workflows/ci-cd.yml`,
`.github/workflows/llm-live-run.yml`, `deploy/scripts/env-file.sh`,
`tests/test_deploy_staging_services.py`, `tests/test_llm_live_run_workflow.py`.

**Estimated scope:** Medium (5 files).

## Task 6: Document ECB sourcing and failure policy

**Description:** Replace manual-rate instructions with the ECB cache,
staleness, provenance, and failure contract.

**Acceptance criteria:**

- [x] SECRETS.md gives operators the ECB URL, access requirement, and incident
      behavior; no manual rate update is required.
- [x] PIPELINE_SPEC §4.5 and SYSTEM_DESIGN §15 describe one quote per call,
      source/date in trace, and the published-price/estimated-token limits of
      the pre-call guard.
- [x] Docs state the seven-day maximum observation age and cold-cache
      fail-closed behavior, distinguishing pre-call and paid-answer failures.

**Verification:**

- [x] Cross-check docs against final error classes, trace fields, and cache
      behavior; `git diff --check` passes.

**Dependencies:** Tasks 3–5. Before editing docs, coordinate with #65/#75/#61
for PIPELINE_SPEC and #65/#75/#74/#69/#58 for SYSTEM_DESIGN as applicable,
or wait for their merges.

**Files likely touched:** `docs/SECRETS.md`, `docs/PIPELINE_SPEC.md`,
`docs/SYSTEM_DESIGN.md`.

**Estimated scope:** Medium (3 files).

## Task 7: Final gates, review, PR update, and operational check

**Description:** Finish the agent-loop with full validation, independent review,
and an update to existing PR #68.

**Acceptance criteria:**

- [x] `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy .`,
      and `uv run pytest` all pass on the final diff.
- [x] Standards and spec reviews report no unresolved findings; the branch is
      rebased on `main`.
- [x] PR #68's `What` / `Why` / `How to verify` / `Refs` body describes ECB
      sourcing without a closing keyword, and the branch is pushed.
- [ ] Request approval on the current PR head.
- [ ] After merge/deploy, a redacted staging check confirms ECB egress and the
      latest acceptable quote; the manual `LLM live run` records source/date
      and no longer needs a repository FX variable.

**Verification:**

- [x] Record gate outputs, review findings, and the updated
      [PR #68](https://github.com/larchanka-training/dmc-268-api-t6/pull/68) link.
- [ ] Record staging/deploy and live-run links after the operational checks.

**Dependencies:** Tasks 1–6 and Checkpoints A–B; do not publish a new PR.

**Files likely touched:** None locally beyond task checklist updates.

**Estimated scope:** Small (verification and publication).

## Checkpoint C: Ready for review

- [ ] All tasks and checks above are complete or explicitly marked pending
      for post-merge operations.
- [ ] No files owned by still-open PRs other than #68 were edited without a
      comment there. Confirm staging egress from the worker container after
      merge; never print API keys.
