# Implementation Plan: LLM live-run gaps (#66)

## Overview

Fix three defects found in live runs: record provider cost in USD, accept two
losslessly repairable review-output deviations, and pass the configured fallback
model to the staging worker. Keep the gateway's cost limit in USD and preserve
strict rejection of meaning-changing output errors. The changes use the existing
LLM and deployment boundaries; no migration or new dependency is needed.

## Repository and coordination constraints

- PR #65 is **open** and owns `app/modules/reviews/infrastructure/llm/gateway.py`,
  `transport.py`, `settings.py`, `tests/test_llm_gateway.py`,
  `tests/test_review_output.py`, and `docs/PIPELINE_SPEC.md` (among other files).
  The user authorized coordination, and a [comment on PR #65](https://github.com/larchanka-training/dmc-268-api-t6/pull/65#issuecomment-5999397578)
  permits overlapping edits. Recheck ownership and rebase on `main` again after
  PR #65 merges, before final review.
- This branch was rebased onto current `origin/main` after #46's model and price
  updates, before Task 3. Rerun focused tests after any later rebase.
- The `LLM_FALLBACK_MODEL` repository variable is currently absent (verified
  during planning). Setting it and checking deployed `worker.env` are
  operational acceptance criteria. A passing unit test alone does not prove
  either one. Do not expose LLM keys in verification output.
- `#46` selects the model pair; this issue does not change model selection.
  HTTP 402 handling from `#53` is outside this issue.

## Architecture decisions

1. **Convert at the provider boundary before recording usage.** Preserve the
   provider's raw body for tracing, but carry `usage.cost` and
   `usage.cost_currency` distinctly through the transport. Convert to `Decimal`
   USD before constructing `LlmUsage`, so the ledger, run totals, and pre-call
   limit use the same unit. Quantize once at the existing six-decimal ledger
   boundary. `USD` uses the amount as is. Missing currency preserves the
   previous USD interpretation and emits a warning with provider/model context,
   without logging credentials or response content. An unsupported currency or
   a missing conversion rate must produce an explicit classified error; it must
   never be recorded as USD. Cover paid-answer accounting in that error path.
2. **Use a reviewed, explicit EUR→USD rate.** EUR responses do not contain a
   usable EUR→USD rate (`cost_eur` is also EUR). Propose a positive finite
   `LLM_EUR_TO_USD_RATE` setting read as `Decimal`, passed only to the worker,
   with no baked-in live exchange rate and no new network dependency. Add the
   rate to the staging bundle/allowlist and document its owner, update cadence,
   and behavior when absent. Confirm the precise failure and accounting policy
   with the tech lead before implementation; see Open questions.
3. **Normalize only after shape validation and before semantic validation.**
   In `validate_review_answer`, after JSON Schema accepts the object, change
   numeric `start_line == line` to `null`, then perform a stable sort by severity
   rank followed by descending confidence. Pass the normalized object to
   `parse_review_output` and return it to the gateway. Keep Pydantic's validators
   strict for callers of `parse_review_output` and persistence, and keep
   `review/scripts/validate_findings.py` strict as an eval-quality signal.
   `start_line > line`, malformed field types, excessive summary sentences,
   trailing title periods, and schema violations still reject. Never mutate
   the provider's raw trace; persist and publish the normalized accepted output.
4. **Represent the two verdicts in the corpus.** Move the equality and wrong
   order examples into a `normalizable/` fixture class or otherwise classify
   them explicitly. Gateway tests accept and assert the corrected values;
   schema/Pydantic/eval parity tests still expect strict rejection of the raw
   cases. Record this distinction in PIPELINE_SPEC §9's semantic table.
5. **Deliver fallback through the existing staging bundle.** Add
   `LLM_FALLBACK_MODEL` from `vars.LLM_FALLBACK_MODEL` to the workflow bundle,
   `WORKER_ENV_KEYS`, and the deployment contract test. An empty variable is
   omitted as today and means no fallback. Set the repository variable to
   `mistral-small-3.2-24b`; verify the deployed `worker.env` and worker startup.
   The known fallback's endpoint must still match the primary endpoint for
   inherited keys; verify this before deployment.

## Dependency graph and order

```text
PR #65 comment + #46 rebase ─> currency metadata/config ─> USD ledger/budget ─> §4.5 + SD §15
runtime normalization ───────────────────────────────────────────────────> §9 documentation
PR #65 merge + rebase ────────────────────────────────────────────────────> final review
staging fallback delivery ─────> repository variable ─> deploy check
EUR rate configuration ────────> repository variable ─> deploy check
```

### Phase 1: Uncontended paths

- [x] Task 1: Carry `LLM_FALLBACK_MODEL` to staging and test optional omission.
- [x] Task 2: Normalize equality and ordering on the gateway answer path, with
      corpus and parity assertions. Keep the eval script strict.

### Checkpoint: Coordinated overlap

- [x] Focused tests for Tasks 1–2 pass.
- [x] PR #65 ownership was rechecked, the coordination comment was posted, and
      this branch was rebased on `origin/main` after #46.
- [ ] After PR #65 merges, rebase on `main` and resolve any changes in gateway
      behavior before final review.

### Phase 2: Currency after coordinated rebase

- [x] Task 3: Parse currency metadata and validate an explicit EUR→USD setting.
- [x] Task 4: Record converted cost and enforce the USD run limit, including
      absent-currency warnings and non-USD error cases.
- [x] Task 5: Update PIPELINE_SPEC §4.5/§9, SYSTEM_DESIGN §15, and SECRETS.md.

### Checkpoint: Code and documentation

- [ ] Focused transport, gateway, validation, and deployment tests pass.
- [ ] `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy .`,
      and `uv run pytest` pass.
- [ ] Docs reflect the actual configured exchange-rate policy and strict eval
      behavior; no new dependency or migration was introduced.

### Phase 3: Operational verification

- [ ] Task 6: Set/read the repository variables for fallback and EUR rate,
      deploy staging, and inspect the generated worker environment without
      printing secrets. Repeat `LLM live run` if desired after the baseline
      acceptance criteria pass.
- [ ] Request review on the current, rebased PR head. Follow the repository's
      approval and resolved-thread rules; the tech lead closes the issue.

## Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Editing PR #65-owned files while it is open | Creates conflicts with its final diff | Coordination comment is posted; recheck ownership and rebase after merge |
| EUR exchange rate is stale or missing | Incorrect USD budget or failed calls | Explicit positive rate, documented update owner/cadence, tests for missing/invalid config and budget boundary |
| Unknown currency or failed conversion loses paid usage | Budget may undercount | Define an explicit error and accounting path before implementation; test it end to end |
| Normalizer accepts a changed anchor or hides invalid JSON | Wrong inline comments | Normalize equality only, after schema check; preserve strict range and summary/title validators |
| Fallback worker starts without a usable key | Staging deployment failure | Check both known model URLs and key inheritance before setting variable/deploying |
| Eval and runtime corpus expectations diverge | False test results | Mark normalizable fixtures explicitly and assert both runtime and strict eval verdicts |

## Open questions for plan approval

1. Confirm `LLM_EUR_TO_USD_RATE` as the explicit EUR source and identify who
   updates its repository variable. The observed 1.1225 on 2026-10-05 is
   evidence, not a permanent default.
2. Choose the paid-answer policy if a response declares a non-USD currency but
   no usable rate exists. Recommended: fail the call explicitly, retain raw
   trace and token usage, and charge a conservative USD estimate from the model
   price so the budget does not fail open; never label the raw amount as USD.
3. Confirm normalizing on the gateway answer path while leaving direct Pydantic
   parsing and the eval script strict. This avoids changing the semantic
   contract for other callers.

Development begins only after human approval of this plan, per the
planning-and-task-breakdown and agent-loop workflows.
