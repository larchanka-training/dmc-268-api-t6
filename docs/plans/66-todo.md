# Tasks: LLM live-run gaps (#66)

Follow `66-plan.md`. A coordination comment was posted on open PR #65 before
overlapping edits. This branch was rebased onto `origin/main` after #46; rebase
again after PR #65 merges and before final review.
Each task is complete only when its acceptance criteria and verification hold.

## Task 1: Pass fallback model to the staging worker

**Description:** Extend the existing CI bundle and worker env-file allowlist
with the repository variable `LLM_FALLBACK_MODEL`.

**Acceptance criteria:**

- [x] Nonempty `vars.LLM_FALLBACK_MODEL` reaches only `worker.env` as
      `LLM_FALLBACK_MODEL`.
- [x] Empty/unset variable is omitted; deployment still succeeds without
      fallback.
- [x] Deployment contract test checks source, allowlist, and round trip.

**Verification:**

- [x] `uv run pytest tests/test_deploy_staging_services.py` passes.
- [x] Inspect the generated bundle test output for the named key, never a real
      secret value.

**Dependencies:** None; safe before PR #65 merges.

**Files likely touched:** `.github/workflows/ci-cd.yml`,
`deploy/scripts/env-file.sh`, `tests/test_deploy_staging_services.py`.

**Estimated scope:** Medium (3 files).

## Task 2: Accept losslessly normalizable review answers

**Description:** Normalize same-line anchors and finding order only in the
gateway's answer validation path. Keep Pydantic and the eval validator strict.

**Acceptance criteria:**

- [x] An otherwise valid answer with `start_line == line` returns
      `start_line: null`; an answer in the wrong severity/confidence order returns
      the same findings in stable descending order.
- [x] `start_line > line`, title/summary semantic errors, and malformed schema
      inputs still raise `InvalidAnswer`.
- [x] The corpus explicitly identifies normalizable raw cases; parity tests
      assert gateway acceptance and strict raw Pydantic/eval rejection.

**Verification:**

- [x] `uv run pytest tests/test_gateway_answer_validation.py tests/test_review_output_schema.py` passes.
- [x] A gateway result is confirmed to contain the normalized object that the
      later parse/persistence path consumes; the provider trace remains raw.

**Dependencies:** None for code and tests; §9 documentation follows Task 5.

**Files likely touched:** `app/modules/reviews/infrastructure/llm/answers.py`,
`tests/test_gateway_answer_validation.py`, `tests/test_review_output_schema.py`,
`tests/fixtures/review_output/{invalid,normalizable}/`.

**Estimated scope:** Medium (3 code/test files plus fixture moves).

## Checkpoint A: Coordinated review

- [x] Focused tests for Tasks 1–2 pass.
- [x] PR #65 ownership rechecked and
      [coordination comment](https://github.com/larchanka-training/dmc-268-api-t6/pull/65#issuecomment-5999397578)
      posted before overlapping edits.
- [x] Branch rebased on current `origin/main` after #46's model and price updates.
- [ ] After PR #65 merges, rebase on `main` before final review.

## Task 3: Carry provider currency and configured EUR conversion rate

**Description:** Keep amount and declared currency distinct until conversion.
Add a positive, finite decimal `LLM_EUR_TO_USD_RATE` setting and its worker
environment delivery after the rate policy is approved.

**Acceptance criteria:**

- [x] USD and EUR usage metadata are parsed without naming raw EUR `cost_usd`.
- [x] Invalid rate values are rejected; absent currency keeps legacy USD
      interpretation with a warning; unknown currency cannot silently pass.
- [x] Staging bundle can deliver the configured rate only to the worker.

**Verification:**

- [x] Focused settings/transport and deployment tests pass.
- [x] Review tests for numeric precision, zero cost, invalid currency, and
      missing/invalid rate.

**Dependencies:** PR #65 coordination comment posted; branch rebased after #46;
rate policy approved. Paid-response errors carry raw body and token counts for
Task 4 accounting. Task 4 converts valid EUR amounts before USD ledger recording.

**Files likely touched:** `app/modules/reviews/infrastructure/llm/settings.py`,
`transport.py`, relevant settings/transport tests,
`.github/workflows/ci-cd.yml`, `deploy/scripts/env-file.sh`,
`tests/test_deploy_staging_services.py`.

**Estimated scope:** Two focused substeps, each Medium (split implementation
between provider contract and deployment wiring).

## Task 4: Charge USD usage and enforce the run limit

**Description:** Convert declared EUR cost before ledger recording, then verify
the pre-call run limit uses the converted USD total across attempts.

**Acceptance criteria:**

- [x] USD response records its exact USD cost; EUR response records converted
      USD, quantized at the ledger boundary.
- [x] A EUR-priced response contributes its USD amount to subsequent run
      budget checks, including fallback calls and prior attempts.
- [x] Missing/unknown conversion follows the approved explicit error and
      paid-answer accounting policy; raw EUR is never stored as USD.

**Verification:**

- [x] `uv run pytest tests/test_llm_gateway.py` and focused transport tests pass.
- [x] Assert literal expected USD values and a budget boundary that would
      differ if EUR were mistakenly treated as USD.

**Dependencies:** Task 3; PR #65 coordination comment posted. Rebase after PR #65
merges and before final review.

**Files likely touched:** `app/modules/reviews/infrastructure/llm/gateway.py`,
`transport.py`, `tests/test_llm_gateway.py`.

**Estimated scope:** Medium (3 files).

## Task 5: Update the contract and operator documentation

**Description:** Document USD usage and conversion, the runtime normalizer
versus strict eval semantics, and staging fallback/rate configuration.

**Acceptance criteria:**

- [x] PIPELINE_SPEC §4.5 and SYSTEM_DESIGN §15 describe USD conversion and
      missing-currency behavior.
- [x] PIPELINE_SPEC §9 semantic table has a «Нормализуется в шлюзе» column for equality
      and ordering and states that `validate_findings.py` remains strict.
- [x] SECRETS.md worker env-file table and variables describe the fallback
      model, rate setting, examples, and the empty-fallback behavior.

**Verification:**

- [x] Cross-check each documented key and behavior against the final code and
      deployment test.
- [x] `git diff --check` passes.

**Dependencies:** Tasks 1–4; PR #65 coordination comment posted. Rebase after
PR #65 merges and before final review.

**Files likely touched:** `docs/PIPELINE_SPEC.md`, `docs/SYSTEM_DESIGN.md`,
`docs/SECRETS.md`.

**Estimated scope:** Medium (3 files).

## Checkpoint B: Repository gates

- [x] `uv run ruff check .`
- [x] `uv run ruff format --check .`
- [x] `uv run mypy .`
- [x] `uv run pytest`
- [x] Recheck current open PR file ownership and review branch diff.

## Task 7: Bound known-route costs at the configured FX rate

**Description:** Address the [#66 scope addition](https://github.com/larchanka-training/dmc-268-api-t6/issues/66#issuecomment-5999075669):
make the primary model's pre-call cost estimate conservative for Regolo,
preserve that ceiling as the configured EUR→USD rate changes, and correct the
budget explanation and repository-variable labels.

**Acceptance criteria:**

- [x] `mistral-small-4` uses at least Regolo's €0.50/€2.10 per million
      input/output token price (cache reads at the full input price) converted
      to USD. For both known EUrouter models, the effective price is the
      componentwise maximum of the USD catalog floor and the highest known EUR
      route price times configured `LLM_EUR_TO_USD_RATE`; a lower env override
      on the known EUrouter endpoint cannot weaken the floor.
- [x] A boundary test using literal Regolo prices and a configured rate above
      1.1225 proves a call that could exceed $0.50 is rejected with
      `BUDGET_EXCEEDED` before any provider request. Update
      `test_oq2_pair_keeps_the_sd15_catalog_prices_and_windows` and the existing
      OQ-2 primary boundary test. The same effective price covers the
      conservative paid-answer fallback path.
- [x] SYSTEM_DESIGN §15 states the shared four-call attempt ceiling, the
      possible 12-primary case (€0.5136 ≈ $0.5765 at 1.1225), and the
      9+3 case's 1.1737 USD/EUR threshold. Replace the unconditional
      “Прогон fast ≤ $0.50” cost claim with the actual pre-call guard and its
      catalog/token-estimate limits. `docs/SECRETS.md` and the CI workflow
      comment label `LLM_MODEL` a repository variable.

**Verification:**

- [x] `uv run pytest tests/test_llm_gateway.py tests/test_deploy_staging_services.py` passes.
- [x] Check literal arithmetic at FX 1.1204, 1.1225, and above 1.1225;
      inspect `LLM_MODEL` labels in both documentation and workflow.
- [x] Equivalent EUrouter URL spellings retain the price floor, provider, and
      key inheritance; an unrecognized path or IDNA-normalized Unicode-dot
      hostname cannot bypass it on either known model.
      A huge unrepresentable finite rate fails at configuration time, while a
      large representable rate still works.
- [x] Rerun `uv run ruff check .`, `uv run ruff format --check .`,
      `uv run mypy .`, and `uv run pytest` after code/documentation changes.

**Dependencies:** Tasks 3–5; human review of this plan amendment. The PR #65
coordination comment covers the overlapping settings and gateway test files;
rebase after PR #65 merges before final review.

**Files likely touched:** `app/modules/reviews/infrastructure/llm/settings.py`,
`tests/test_llm_gateway.py`, `docs/SYSTEM_DESIGN.md`, `docs/SECRETS.md`,
`.github/workflows/ci-cd.yml`. Reuse one effective-price path in settings and
gateway; if a separate gateway edit is needed, split this into price-policy
and integration substeps before development.

**Estimated scope:** Medium (5 files; split if the gateway must change).

## Checkpoint C: Added budget scope

- [x] Task 7's focused tests and all four repository gates pass on the new
      head; review the final price, FX, and rounding assumptions.
- [x] SYSTEM_DESIGN §15, SECRETS.md, and the workflow agree with code and
      GitHub variable scope.

## Task 6: Set variables and verify staging

**Description:** Finish the acceptance criteria that cannot be proven by local
tests alone.

**Acceptance criteria:**

- [x] Repository variable `LLM_FALLBACK_MODEL=mistral-small-3.2-24b` is set;
      reviewed `LLM_EUR_TO_USD_RATE=1.1204` is also set at repository scope.
- [ ] After deployment, `worker.env` contains both nonsecret keys and the
      worker starts with the intended primary/fallback pair.
- [ ] Verification captures the deployment/run links and results, without
      printing keys or attaching an issue-closing keyword.

**Verification:**

- [x] Read back both repository variables via `gh`; Environment `staging` has
      no override for either value.
- [x] Confirm `KNOWN_MODELS` assigns both selected models the same
      `https://api.eurouter.ai/api/v1` endpoint.
- [ ] After deployment, inspect staging `worker.env` with secret values
      redacted and confirm key inheritance and worker health.
- [ ] Optionally repeat `LLM live run` with the defaults after the required
      deployment checks.

**Operational evidence (05.10.2026):** The coordinator set and read back both
repository variables, checked for staging Environment overrides, and verified
the model endpoints. `deploy-staging` runs only from `main`; this PR has not
merged, so deployed `worker.env`, startup, and run results remain unverified.

**Dependencies:** Tasks 1–5 and 7, Checkpoints B–C, and deployment authorization.

**Files likely touched:** None locally.

**Estimated scope:** Small (operational).
