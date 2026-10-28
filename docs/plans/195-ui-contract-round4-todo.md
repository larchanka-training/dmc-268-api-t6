# UI #195 round 4: API contract counterpart checklist

## Task 1 — Pin UI `46b2807` and reproduce mirror failures

- [x] Confirm the UI checkout is clean at `46b2807492a52e100bcfb48052b1e640cbd50beb`.
- [x] Regenerate `tests/fixtures/ui_zod_contracts.json`; confirm its provenance and set `UI_CONTRACT_COMMIT` to the same full SHA.
- [x] Capture three RED mirror cases: `bitbucketConnectResponse`, `bitbucketConnectionItem`, `bitbucketConnectionList`, showing slug bound mismatch.

**Evidence:** The exact UI checkout was clean. Regeneration retained the 23-schema selection and separate RunStepEvent supplement. Before response DTO changes, the OpenAPI/UI suite reported 218 passed and exactly three mirror failures, each `maxLength None != 128` for the response slugs. The earlier provenance change also failed RED against the old `c581bf6` fixture, then passed after regeneration. A token-pattern mutation that allowed a space failed the new parity test on that character; the fixture was restored from the committed UI source.

**Files:** `tests/fixtures/ui_zod_contracts.json`, `tests/test_ui_zod_contracts.py`, `tests/test_openapi_contract.py`. **Dependency:** none. **Size:** small.

## Task 2 — Match API response bounds

- [x] Test 128 accepted and 129 rejected for both slug fields on both `ConnectResponse` and `ConnectionItem`.
- [x] Add `Field(min_length=1, max_length=128)` to both slug fields in each DTO.
- [x] Assert OpenAPI exposes `minLength: 1` and `maxLength: 128` for both response components; verify nested list through the third mirror case.
- [x] Confirm the three mirror cases are GREEN; leave `DiscoveredRepository` unchanged.

**Evidence:** Five focused DTO/HTTP cases failed RED before the DTO changes (four 129-character response slugs accepted and no runtime OpenAPI maxLength). After the DTO and static OpenAPI change, 128-character values pass and 129-character values raise validation for both fields of both response models. The runtime OpenAPI schema reports `(minLength, maxLength) = (1, 128)`, the request bounds remain 128, and `DiscoveredRepository` still accepts 129-character provider slugs. Bitbucket API and OpenAPI/UI Zod contract suite: 262 passed, including the three previously failing nested mirror cases.

**Files:** `app/modules/integrations/bitbucket/api/connections.py`, `tests/test_openapi_contract.py`, possibly focused DTO/API tests. **Dependency:** Task 1. **Size:** small.

## Task 3 — Gates, review, stacked Draft PR

- [x] Run focused contract and Bitbucket API tests.
- [x] Run `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy .`, `uv run pytest` (static gates passed; full pytest recorded below).
- [ ] Review fixture provenance, token-pattern parity, response DTO diff, and current #340 head.
- [ ] Publish an isolated stacked Draft PR into #340; do not push directly to #340. Coordination: https://github.com/larchanka-training/dmc-268-api-t6/pull/340#issuecomment-6476646581.
- [ ] Check CI and mergeability; relay PR URL and any remaining staging dependency.

**Dependency:** Task 2. **Size:** medium.

**Local evidence:** The combined Bitbucket API, OpenAPI and UI Zod contract suite passed 262 tests. Full Ruff lint and format check passed, as did mypy; `git diff --check` passed. The full local pytest finished with 8361 passed, 1789 skipped and 3 failed in 8m09s. Those three failures are outside the changed area: macOS did not enforce the CPU `SIGXCPU` expectation in `test_ast_worker`, and `/bin/bash` raised `profile[@]: unbound variable` in two `test_eval_live_workflow` cases. No changed-area test failed. CI on Linux remains the required final suite gate. No files were committed or pushed by the development agent.
