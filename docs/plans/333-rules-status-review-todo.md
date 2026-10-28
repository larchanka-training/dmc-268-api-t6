# Todo: Bitbucket repository rules status (UI #195 review round 3)

## Task 1: Claim Bitbucket rules requests

**Acceptance criteria**

- [x] A pending request for an active Bitbucket repository is claimable with its provider; inactive installations remain unclaimed by the existing `deleted_at` filter.
- [x] GitHub claim and GitHub-only push lookup remain unchanged.

**Verification**: Add a failing PostgreSQL claim test first, then run focused rules-status tests and API lint/type checks.

**Evidence**: New PostgreSQL claim test failed RED with `targets == []`, then passed GREEN after provider-aware claim. With `TEST_DATABASE_URL` on disposable local PostgreSQL, `tests/test_rules_status.py` and `tests/test_rules_status_postgres.py` passed (90 tests), followed by the active-claim and two inactive-claim cases (3 passed). The negative cases protect existing `deleted_at` and `installation_available` guards, so no extra production change was needed. Ruff lint, Ruff format check, and mypy passed for the four changed Python files and again for the extended integration test. Full repository gates remain for Task 5.

**Dependencies**: None.

**Likely files**: `app/modules/repositories/application/rules_status.py`, `app/modules/repositories/infrastructure/rules_status.py`, `tests/test_rules_status_postgres.py`.

**Scope**: Medium.

## Task 2: Read Bitbucket rules at the pinned default-branch head

**Acceptance criteria**

- [x] Reader uses `BitbucketRepositorySnapshotSource.head()` and `rules_document(sha)` outside a transaction, maps present/missing/invalid/unavailable to the existing wire contract, and never treats an uncertain read as missing.
- [x] Binding or credential changes prevent a stale read from being accepted; GitHub reader behavior remains intact.

**Verification**: RED/GREEN `httpx.MockTransport` tests for present, missing, malformed/inaccessible and rotated binding; focused reader tests and mypy.

**Evidence**: Reader tests first failed collection because `ReadBitbucketRulesDocument` was absent; eight mapping/dispatch cases and a `httpx.MockTransport` pinned `/src` present case now pass. Three PostgreSQL race cases first failed because `save` accepted stale readings; after claim carries `BoundBitbucketTarget` and the save transaction rechecks active credential/binding generations, all three pass. A changed target releases its claim while keeping the refresh marker. Provider HTTP runs after the claim transaction and before the save transaction; no network occurs inside either. MockTransport tests proved complete empty listing → `missing`, incomplete/oversized or total-budget-exhausted listing → retryable `unavailable`, 429 → rate-limited `unavailable` with preserved `Retry-After`, and actual oversized rules file → `invalid`. The listing-budget and 429 tests failed RED before the source fix. A later exact-size regression failed RED for truncated and overlong HTTP 200 file bodies; `rules_document` now compares the raw body with the pinned listing size and maps either mismatch to retryable `unavailable`. That focused source/reader suite passed 25 tests after the fix, with Ruff lint/format and mypy passing. The earlier focused suite covering GitHub rules, Bitbucket source, request authority and PostgreSQL status passed 120 tests. Full repository gates remain for Task 5.

**Dependencies**: Task 1.

**Likely files**: `app/modules/repositories/application/rules_status.py`, new `app/modules/repositories/infrastructure/bitbucket_rules_status.py`, `app/bootstrap/reviews_api.py`, new `tests/test_bitbucket_rules_status.py`.

**Scope**: Medium.

## Task 3: Wake and run the Bitbucket rules worker

**Acceptance criteria**

- [x] `webhook-worker` composes rules refresh when Bitbucket alone is configured and dispatches each claimed provider to the correct reader.
- [x] New/reactivated Bitbucket bindings request the first rules read in the binding transaction; GET still requests missing/stale reads.

**Verification**: Failing then passing worker-composition and connection tests; PostgreSQL integration test for pending-to-terminal status if database is available.

**Evidence**: Bitbucket-only worker composition failed RED because `compose_rules_refresher` was absent; the PostgreSQL browser-connect test failed RED because the first binding had no `refresh_requested_at` row. Both passed GREEN after the worker gained an HTTP client and provider-aware refresher when either GitHub or Bitbucket is configured, and the Bitbucket connect use case requested a refresh for each new/reactivated binding before commit. The worker startup test also exercises the Bitbucket-only `run_forever` path and verifies its refresher receives a cipher without a GitHub token provider. A subsequent PostgreSQL + `httpx.MockTransport` end-to-end regression failed RED because a Bitbucket-only worker claimed both Bitbucket and GitHub pending requests. The claim now filters to configured readers. GREEN verifies the Bitbucket request moves from REST `pending` to terminal `missing` with the pinned SHA and default version, while GitHub's request remains pending; a GitHub-only worker then processes GitHub while leaving a new Bitbucket request intact. Focused rules-status, Bitbucket connection, and worker suite: 128 passed; an earlier broader suite including source tests passed 141. Ruff lint/format and mypy passed on the three newly changed Python files. Full repository gates remain for Task 5.

**Lost-access wording audit**: A suspected Bitbucket diagnostic mentioning GitHub App was not user-visible. `repository_access_predicate` hides an inactive Bitbucket binding before `GetRulesStatus` runs, so its GET is 404 and requests no refresh; PostgreSQL regression verifies this. For GitHub, the existing lost-access test verifies the canonical `policy_access_denied` diagnostic and no refresh. `RulesDiagnostic.from_error` discards `_no_installation_access`'s internal English `PolicyError` text and emits the shared code-based Russian message. No production wording or provider field was changed. The focused PostgreSQL cases passed 3 tests; Ruff lint/format, mypy, and `git diff --check` passed.

**Dependencies**: Tasks 1–2.

**Likely files**: `app/webhook_worker.py`, `app/modules/integrations/bitbucket/infrastructure/connection_store.py`, `tests/test_webhook_worker_notifications.py`, `tests/test_bitbucket_connections_postgres.py`.

**Scope**: Medium.

## Task 4: Align documentation and UI mock evidence

**Acceptance criteria**

- [x] `contracts/openapi.yaml` and `docs/FRONTEND_ARCHITECTURE.md` describe real Bitbucket read triggers and terminal responses; GitHub behavior remains accurate.
- [x] The UI mock's connected Bitbucket `pending` → `missing` sequence remains covered by its tests and matches the implemented API path.

**Verification**: API OpenAPI contract tests, UI mock tests, `git diff --check`, docs-site checks.

**Evidence**: API OpenAPI now distinguishes GitHub onboarding/default-branch push from Bitbucket new/reactivated binding and GET missing/stale/retryable triggers; Bitbucket `repo:push` has no immediate rules refresh. It describes pinned GitHub tree/blob and Bitbucket `/src` reads, configured-provider claims, and uncertain Bitbucket listings as `unavailable`. `docs/BACKEND_ARCHITECTURE.md` records the cross-module infrastructure read-model and same-transaction refresh request; application uses own ports and public contracts of other modules. UI architecture describes the same trigger and `pending` → terminal behavior. Its GitHub `docs-t6` and connected Bitbucket `demo-bitbucket/private-repo` mocks already cover `pending` → `missing`; both remain unchanged. API contract/Zod suite: 182 passed. UI mock transport suite: 108 passed. UI document Prettier check and both worktrees' `git diff --check` passed. The UI `pnpm` command returned no output and was interrupted; the already-installed local Prettier/Vitest binaries ran the checks. No docs-site files changed, so docs-site build was not needed. Full repository gates remain for Task 5.

**Published UI pin follow-up**: After UI PR #195 reached clean head `e5dfb6cd4dadedce1feb715a36731941eabb4a6d`, regenerated the 23-schema API fixture from that exact checkout and updated `UI_CONTRACT_COMMIT`. All 23 schema payloads compare equal to the prior pin; only provenance changed. API Zod/OpenAPI tests: 182 passed. Ruff lint/format, mypy for the pin test, and `git diff --check` passed.

**Final UI pin follow-up**: Regenerated from clean published UI PR #195 head `c581bf660837c3a7d03101c3740f946282a752b1` and updated `UI_CONTRACT_COMMIT`. The 23 schema keys are unchanged. Only five payloads changed: `bitbucketDiscoveryRequest`, `bitbucketConnectRequest`, and `bitbucketCredentialRotateRequest` gained `apiToken.maxLength: 4096` and strict printable-ASCII `pattern`; `bitbucketWebhookSetup` gained `pullrequest:fulfilled` and `pullrequest:rejected`; `bitbucketConnectResponse` embeds that changed events enum. The only other fixture change is the provenance commit. UI Zod tests: 5 passed. The combined Zod/OpenAPI contract suite: 177 passed, 5 failed on those exact OpenAPI token length and webhook enum differences because this isolated API worktree still has the older three-event OpenAPI contract. The active integration candidate has the intended five-event/4096 contract and remains untouched here. Ruff lint/format, mypy for the pin test, and `git diff --check` passed. Re-run the combined contract suite after integrating this pin into the updated API candidate.

**Dependencies**: Tasks 2–3.

**Likely files**: API `contracts/openapi.yaml`, `tests/test_openapi_contract.py`; UI `docs/FRONTEND_ARCHITECTURE.md`, existing `src/app/mocks/mockTransport.test.ts`.

**Scope**: Medium.

## Task 4b: Match the published UI token and webhook contract on the stacked base

**Acceptance criteria**

- [x] Discovery, connect and credential rotation accept only 1–4096 printable ASCII token characters before provider I/O; a trailing newline, space, non-ASCII or 4097th character yields 422 without echoing the token in the response.
- [x] Connect and webhook-secret rotation return all five events in the API #340 candidate/OpenAPI order: `repo:push`, `pullrequest:created`, `pullrequest:updated`, `pullrequest:fulfilled`, `pullrequest:rejected`.
- [x] The checked OpenAPI token constraints and event enum match the generated API schema and UI fixture pinned to `c581bf660837c3a7d03101c3740f946282a752b1`.

**Verification**: RED first: run the five failing UI Zod/OpenAPI mirror cases and add a runtime invalid-token/no-manager/no-echo test for all three routes plus a five-event response test. GREEN: run `uv run pytest tests/test_ui_zod_contracts.py tests/test_openapi_contract.py tests/test_bitbucket_connection_api.py`, then the four required API gates. Keep secret values out of failure output. **Dependencies**: Task 4's exact UI pin; before Task 5 publishing. **Likely files**: `app/modules/integrations/bitbucket/api/connections.py`, `app/main.py` (422 redaction), `contracts/openapi.yaml`, `tests/test_openapi_contract.py` (runtime and mirror assertions). **Scope**: Medium, four files; do not copy unrelated slug/provider changes from the separate integration candidate.

**Evidence**: The published UI pin first made exactly five OpenAPI mirror cases fail (three missing `apiToken.maxLength` constraints and two embedded three-event enums). A new HTTP test then failed RED because a 4097-character token reached the manager. A separate redaction test failed RED because an unknown JSON field name appeared in 422 `loc`; it also covers a lone surrogate in a non-token field. The shared request DTO now enforces length and printable ASCII before provider I/O, the Bitbucket 422 path omits `input`/`ctx`, limits location segments, and serializes ASCII-escaped JSON. Connect and webhook-secret rotation return all five events. The focused Zod/OpenAPI/Bitbucket API suite passed 209 tests, including an OpenAPI JSON Schema validator check for all three token fields. Full Ruff lint, Ruff format check, mypy, and `git diff --check` passed. The full pytest run finished with 7606 passed, 1692 skipped, 10 failed and 3 setup errors in 11m34s. Ten failures/errors are local loopback socket binds denied by the sandbox, one is the existing macOS `SIGXCPU` behavior, and two are `/bin/bash` empty-array `profile[@]` failures. No Task 4b or changed-area test failed; the full gate cannot be green in this sandbox. The earlier run was interrupted after the redaction regression was found.

**PR #356 P3 follow-up — Bitbucket rules proxy and wording**: The initial worker construction test failed RED because it had no dedicated Bitbucket rules client. The rules client now uses `trust_env=False` and `follow_redirects=False`, is owned and closed by the worker, and is injected separately into the rules source. A further Bitbucket-only startup test with an invalid `SSL_CERT_FILE` failed RED with `FileNotFoundError` because the worker still constructed an unused GitHub client; it now creates that client only when GitHub is configured. Bitbucket-only mode has two safe Bitbucket clients; a new dual-provider case proves GitHub retains its default `trust_env=True` while Bitbucket rules use their isolated client. The Bitbucket diagnostic test failed RED in three cases because no-head, rate-limited, and transient reads exposed GitHub-specific Russian text. Provider-aware status mapping now uses neutral text for those Bitbucket cases while preserving the GitHub messages. Focused worker, GitHub rules, Bitbucket rules, and PostgreSQL test collection: 84 passed and 43 skipped (`TEST_DATABASE_URL` unset). Full `ruff check .`, `ruff format --check .`, `mypy .`, and `git diff --check` passed. Final full pytest: 7610 passed, 1692 skipped, 10 failed, 3 setup errors in 10m09s. Failures are the same sandbox loopback socket (10 cases), macOS `SIGXCPU` (1), and Bash empty-array `profile[@]` (2) environment cases seen in the earlier full run; no changed-area test failed.

## Task 5: Integrate, review, publish and answer threads

**Acceptance criteria**

- [ ] Independent review reports zero Standards and Spec findings; all API and changed UI gates pass.
- [ ] API #340 integrates the isolated fix and UI contract pin without overwriting parallel review changes, then publishes a new head.
- [ ] New P2 and existing P1 inline threads in UI #195 receive precise replies; neither is self-resolved before staging evidence.

**Verification**: Compare final API/UI heads with remote, inspect clean worktrees and CI, then perform staging probe after API deployment.

**Dependencies**: Tasks 1–4b; parallel API review integration and staging deployment for final closure.

**Likely files**: PR #340 body/review threads, PR #195 inline threads; no new source file.

**Scope**: Medium.
