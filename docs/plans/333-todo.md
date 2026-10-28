# #333 Bitbucket Cloud task list

> This is the original implementation checklist. Current review repairs are
> tracked in [333-live-acceptance.md](333-live-acceptance.md) and PR #340;
> historical gate counts below describe earlier heads and do not prove the
> final rebased diff.

Status: T1 official-document research and public UUID-locator probe recorded;
live PR publication checks and native-suggestion decision still open. T2 UUID
schema and scoped lookup passed independent review (0 Standards / 0 Spec).
T3/T4 and T5 passed independent review (0 Standards / 0 Spec). T6 PR/push
projection and immutable Run origin passed independent review (0 Standards /
0 Spec). T7 pinned VCS adapter passed independent review (0 Standards /
0 Spec). T8 worker/policy/snapshot path passed independent review (0 Standards /
0 Spec). T9 comment publication passed independent review (0 Standards / 0 Spec);
live PR capability proof remains pending. T10 status publication passed
independent review (0 Standards / 0 Spec). T11 RAG indexing/retrieval and T12
UI passed independent review (0 Standards / 0 Spec). T13 deployment and docs
have focused tests and static gates green; round 5 integration and final
independent review are in progress. The full backend pytest gate remains due
on the stable final diff. Historical API base: `88bbc073`; round 5 candidate
base: `10d2848a`; published UI #195 head and fixture pin:
`46b2807492a52e100bcfb48052b1e640cbd50beb`. Recheck both PR heads before
publication. Issue: https://github.com/larchanka-training/dmc-268-api-t6/issues/333.

## Task 1: Fix the provider contract before implementation

**Description:** Validate current Atlassian Cloud API/token scopes, webhook delivery
headers, repository UUID, revision diff and comment/suggestion behavior. Prototype
the connection and suggestion operation on a disposable Bitbucket repo. Decide the
browser connection endpoints, access policy, webhook route and status key; record
exact request/response examples and error mapping in the plan/OpenAPI draft.

**Acceptance criteria:**

- [x] Documented provider capabilities and minimum API-token scopes are sourced in `333-plan.md` (2026-10-26).
- [x] Read-only public-repository UUID-locator probe returned 200; no private-repository or scoped-token behavior was established (`333-plan.md`).
- [ ] Disposable private-repo API probe confirms access, inline coordinates, status association and uncertain webhook retry behavior.
- [ ] Native code suggestion syntax is demonstrated, or the documented plain inline patch-text fallback is explicitly accepted without claiming native support.
- [x] Draft backend/UI endpoints, local access policy, secret lifecycle and UUID mapping are explicit in `333-plan.md`.
- [x] Browser contract added to `contracts/openapi.yaml` after coordinator commented on PR #283; OpenAPI contract and runtime checks passed (201 tests).

**Verification:** Document links and sanitized API evidence; review the contract with
the coordinator/user. **Dependencies:** None. **Likely files:** `docs/plans/333-plan.md`,
`contracts/openapi.yaml` (draft). **Scope:** S.

## Task 2: Persist UUID identity and workspace ownership

**Description:** Write failing PostgreSQL tests for two portal workspaces binding
the same immutable remote UUID, mutable slug rename, collisions within one portal
workspace and migration preservation. Add the smallest schema changes that
represent Bitbucket identity without integer
hashes. Keep GitHub/GitLab data and APIs stable.

**Acceptance criteria:**

- [x] Repository UUID survives rename and production lookup never resolves across workspace or installation.
- [x] Duplicate UUID binding within one portal workspace and conflicting installation ownership fail closed; the same remote UUID may be connected by a second portal workspace.
- [x] Existing provider rows migrate without changing their identity.

**Verification:** 143 focused PostgreSQL migration tests pass; ruff, format and
mypy pass. Independent review: 0 Standards / 0 Spec. The local full pytest run
was interrupted at 15% after its known macOS SIGXCPU failure to avoid stale
collection when the then-numbered migration 0044 (now 0045) was added for T3.
**Dependencies:** T1's documented UUID/identity contract only; the live
comment/suggestion/status probe does not block the identity schema.
**Files:** `app/modules/integrations/bitbucket/infrastructure/models.py`,
`app/modules/integrations/bitbucket/infrastructure/identity_store.py`,
`alembic/versions/20261026_0044_bitbucket_identity.py`,
`tests/test_bitbucket_identity_postgres.py`, `app/bootstrap/db_metadata.py`.
**Scope:** M.

## Task 3: Add encrypted credential authority and revocation

**Description:** Test per-workspace token binding, rotation, revocation and stale
run origins; implement a Bitbucket connection store/request authority using the
existing GitLab encryption pattern, with provider reads outside transactions.

**Acceptance criteria:**

- [x] Tokens and webhook secrets are encrypted at rest; GET/list responses omit them. A new binding or explicit repository-secret rotation returns that repository's secret once for manual webhook setup.
- [x] Revoking one workspace prevents its next request/final write and leaves another workspace usable.
- [x] Provider HTTP cannot leave the trusted Bitbucket origin with a token.

**Verification:** Fake HTTP plus PostgreSQL revocation tests pass. Installation
rows hold only the shared encrypted API token; migration 0046 stores each
repository's encrypted webhook secret separately. Independent review 0/0 was
recorded before this redesign, so review must be repeated. Ruff, format and
mypy pass; full pytest remains due on the stable final diff.
**Dependencies:** T2. **Likely files:** `app/modules/integrations/bitbucket/application/manage_connection.py`,
`app/modules/integrations/bitbucket/infrastructure/connection_store.py`,
`app/modules/integrations/bitbucket/infrastructure/request_credentials.py`,
`tests/test_bitbucket_connections_postgres.py`, `tests/test_bitbucket_request_authority.py`.
**Scope:** M.

## Task 4: Expose scoped browser connect/disconnect

**Description:** Start with failing ASGI tests for workspace authorization, private
repo discovery, connect/disconnect and token rotation. Implement thin FastAPI
transport over application use cases; update canonical OpenAPI.

**Acceptance criteria:**

- [x] ASGI/PostgreSQL tests show an authorized user can connect a provider fixture and see it in the common repository list; private-repository access remains a live gate.
- [x] Unauthorized workspace access and disconnected/expired credentials fail closed, including after current membership is removed.
- [x] With two repositories under one Bitbucket workspace installation, disconnecting one deactivates only that binding and revokes only its webhook secret; the other remains usable. The last disconnect revokes the installation credential.
- [x] Connecting a second repository reuses the existing credential without silently replacing its token; token replacement is an explicit rotate operation.
- [x] Disconnect cannot be undone by stale work: a per-repository binding generation fences only that repository while sibling in-flight targets remain valid; shared generation changes on token rotation or last revoke.
- [x] Each repository receives a distinct one-time webhook setup secret and URL; rotating one secret leaves the sibling's secret intact.
- [x] Token rotation resolves repository fixtures by immutable UUID after a slug rename and refreshes saved names/URLs; a public UUID-locator probe returned 200, while the private runtime locator still needs live proof.
- [x] Token rotation compares each binding generation after network I/O; a concurrent disconnect+rebind of the same repository returns 409 instead of rotating against stale authority.
- [x] Provider discovery bounds decoded JSON to 2 MiB, rejects oversized/arbitrary cursor queries, and forwards a bounded valid `Retry-After` on 429.

**Verification:** ASGI, PostgreSQL and OpenAPI focused suite: 227 passed after
the four independent-review fixes. RED/GREEN PostgreSQL and ASGI regressions
pass for sibling in-flight targets, rename-safe rotation and provider
response bounds/retry handling. A read-only public UUID-locator probe returned 200;
an ASGI/PostgreSQL RED/GREEN race test covers disconnect+rebind during token
rotation without changing the active repository ID set;
provider parser also passed fixture-based private-repository responses without
printing credentials; no private provider request was made in this round.
Ruff, format and mypy pass. Independent review 0 Standards / 0 Spec; final full pytest pending.
**Dependencies:** T3. **Likely files:** `app/modules/integrations/bitbucket/api/connections.py`,
`app/bootstrap/bitbucket.py`, `app/main.py`, `contracts/openapi.yaml`,
`tests/test_bitbucket_connection_api.py`. **Scope:** M.

### Checkpoint A (T1–T4)

- [ ] Identity/permission/credential contract reviewed; all backend gates green.
- [x] Two-workspace PostgreSQL tests ran rather than being skipped on the
  round 5 candidate; repeat them after the final rebase.

## Task 5: Receive signed webhook receipts

**Description:** RED tests for raw-byte HMAC, malformed headers/body, duplicate
delivery, conflicting payload and concurrency. Add authenticated receipt ingestion
with bounded body and leases, using current webhook conventions.

**Acceptance criteria:**

- [x] Invalid/missing signature is rejected before parsing or durable work; body is capped at 256 KiB and duplicate/malformed headers are rejected.
- [x] Same delivery is accepted idempotently; changed payload under one ID is rejected, including concurrent admission.
- [x] Receipt leases survive process restart and do not cross provider/connection; stale secret rotation and disconnect also fence admission and replay. The first valid signed receipt marks only its repository `active` atomically.

**Verification:** 198 focused ASGI, PostgreSQL migration, replay and OpenAPI tests
passed. Ruff, format check and mypy passed; independent review 0 Standards /
0 Spec. Final full pytest remains pending. Live retry UUID stability still requires the disposable
PR/webhook probe in T1; effect deduplication by `(PR, head)` belongs to T6.
**Dependencies:** T3–T4 repository-scoped secret and receiver path. **Likely files:** `app/modules/integrations/bitbucket/api/webhook.py`,
`app/modules/integrations/bitbucket/application/accept_webhook.py`,
`app/modules/integrations/bitbucket/infrastructure/webhook_receipts.py`,
`app/main.py`, `tests/test_bitbucket_webhook_delivery.py`. **Scope:** M.

## Task 6: Project PR/push and trigger one run per head

**Description:** RED tests for PR create/update, relevant push, out-of-order delivery,
stale cancellation and redelivery; connect Bitbucket event projection to generic run
trigger and queue with immutable origin.

**Acceptance criteria:**

- [x] New PR/head creates one `provider=bitbucket` run; metadata-only and duplicate events do not.
- [x] Current PR state wins over delayed payloads; superseded runs are cancelled.
- [x] Queue replay/retry retains Bitbucket provider and workspace origin. A stale AMQP pointer after disconnect/rebind is rejected at worker claim and VCS lookup; the sibling repository remains valid.

**Verification:** Fake provider and PostgreSQL webhook→projection→run/queue tests:
20 passed after the replay and application-boundary review fixes, including
RED→GREEN recovery when PR projection commits before
Run insertion crashes or defers; a same-head replay repairs the missing Run,
while metadata-only delivery after an existing Run stays inert. Shared worker,
GitHub and GitLab focused regressions on the pre-review diff: 625 passed.
`uv run ruff check .`, `uv run ruff format --check .` and `uv run mypy .`
passed. The full `uv run pytest` gate is deferred to the stable final diff;
T6 independent review: 0 Standards / 0 Spec. Out-of-order old payload and stale canonical
updated timestamp cannot restore an older head; `repo:push` enters receipt
replay/NOTIFY, and redelivery does not create a second run.
**Dependencies:** T5. **Likely files:** `app/modules/integrations/bitbucket/application/project_pull_request.py`,
`app/modules/reviews/infrastructure/webhook_runs.py`,
`app/modules/reviews/infrastructure/amqp_messages.py`,
`tests/test_bitbucket_run_trigger.py`, `tests/test_bitbucket_run_trigger_postgres.py`.
**Scope:** M.

### Checkpoint B (T5–T6)

- [x] A signed webhook through the durable queue reaches exactly one Bitbucket
  Run in local integration tests; live delivery remains Task 14.
- [x] Existing GitHub/GitLab webhook/queue regressions passed in local focused
  runs; rerun the full gate on the final head.

## Task 7: Read pinned Bitbucket PR metadata and diff

**Description:** RED tests for source/destination heads, base, fork PR, rename,
binary/large files, pagination, rate limits and incomplete diff. Implement
`VcsProvider` metadata/diff/blob calls using immutable origin and pinned revisions.

**Acceptance criteria:**

- [x] Metadata and diff match a single verified head/base snapshot.
- [x] Missing/truncated data is reported as incomplete, never an empty clean review.
- [x] No credentials reach a response-provided redirect or unrelated host.

**Verification:** HTTP MockTransport contract tests cover immutable repository and
fork UUIDs, pinned compare and merge base, diffstat count/pagination, malformed,
renamed, missing and oversized patches, UTF-8 path limit, rate limits and redirect
containment. Review fixes cover replay from a persisted synthetic blob ID with a
fresh adapter, inaccessible private fork source and Git quoted, unquoted and
mixed-quote Unicode rename paths with tab-delimited patch markers. PostgreSQL
run-locator regression checks UUID routing after sibling disconnect/rebind.
Focused VCS/PG regressions: 152 passed (two upstream Starlette deprecation
warnings). `uv run ruff check .`,
`uv run ruff format --check .`, `uv run mypy .`, and `git diff --check` passed.
T7 independent review: 0 Standards / 0 Spec. Full `uv run pytest` is deferred
to the stable final diff.
**Dependencies:** T3, T6. **Likely files:** `app/modules/reviews/application/vcs_diff.py`,
`app/modules/reviews/infrastructure/bitbucket_vcs.py`,
`app/modules/integrations/bitbucket/infrastructure/run_http.py`,
`tests/test_bitbucket_vcs.py`. **Scope:** M.

## Task 8: Feed policy, snapshot/v4 and worker context

**Description:** RED tests for policy files, source tree, pinned file reads,
provider selection and full worker composition; wire Bitbucket sources to existing
review stages with no GitHub/GitLab calls.

**Acceptance criteria:**

- [x] Existing rules/settings and snapshot/v4 use pinned Bitbucket revisions.
- [x] Manual rerun, cancellation and history work through ordinary APIs.
- [x] A full mocked run succeeds with GitHub/GitLab transports configured to fail.

**Verification:** T8 RED→GREEN covers Bitbucket policy/AGENTS and conventions at
the pinned target base, source and fork head trees/files, fixed UUID routes,
no redirect token forwarding, explicit worker routing and persisted origin
admission. The PostgreSQL worker test uses a pool of one and reacquires a DB
lease during provider HTTP; ASGI rerun, run detail/list history and cancellation
use the real SQL store. It also checks binding rotation and stale rerun refusal.
Full mocked snapshot/v4 review and
verifier pass with the other-provider port set to fail; unknown provider stays
fail closed. Directory pagination follows the validated opaque `next` cursor;
`size`/`page` are optional, and a policy file on page 2 is tested. Bounded PR
commit messages include an explicit incomplete marker and fail if unavailable.
Historical manual reruns remain allowed across shared-token or repository-secret
rotation while the binding epoch is continuous; disconnect/rebind changes the
epoch and denies the old run. Official Bitbucket Source API documentation confirms
directory entry commit hashes refer to the revision in the URL and directory
self links end in `/`: https://developer.atlassian.com/cloud/bitbucket/rest/api-group-source/ .
Official pagination semantics: https://developer.atlassian.com/cloud/bitbucket/rest/ .
Focused non-PG regressions: 109 passed. PostgreSQL Bitbucket webhook/projection
and existing GitLab/GitHub rerun regressions: 39 passed. Ruff, format and mypy pass; full
`uv run pytest` remains due on the stable final diff. Independent review: 0 Standards /
0 Spec.
**Dependencies:** T7. **Likely files:** `app/bootstrap/bitbucket_reviews.py`,
`app/modules/reviews/infrastructure/bitbucket_run_source.py`,
`app/modules/reviews/infrastructure/bitbucket_revision_source.py`,
`app/worker.py`, `tests/test_bitbucket_worker_routing.py`. **Scope:** M.

## Task 9: Publish inline findings and one summary

**Description:** RED tests for line coordinates on both diff sides, suggestion
representation agreed in T1, markers, partial failure, replay and changed head.
Implement Bitbucket comment gateway under existing publication authority.

**Acceptance criteria:**

- [ ] Correct inline findings and manual patch-text suggestion fallback are proven on a live disposable PR; native applyable suggestion support remains unproven.
- [x] Exactly one summary and no duplicate findings appear on tested retry/redelivery paths, including an ambiguous POST response and a partial inline failure.
- [x] Head, destination repository UUID, fork source readability and bound credential changes stop old publication before its next outward write.

**Verification:** The official [Cloud PR comment API](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-pullrequests/) documents `content.raw`, `inline.from`/`inline.to`, POST 201 and paginated list. The implementation labels a fenced patch as manual text; it does not assert a native applyable REST suggestion field. Focused Bitbucket/OpenAPI suite: 260 passed; migration and related PostgreSQL suite: 138 passed. Two concurrent publication pointers contend for a short PostgreSQL claim; one wins, while lease loss fences the old owner and a heartbeat renews ownership during slow HTTP. Remote markers recover partial and ambiguous writes. No DB transaction spans provider HTTP, and token rotation blocks the persisted Run origin. Ruff, format, mypy and `git diff --check` pass; independent review and full backend pytest pending. A disposable PR plus write token is still required for T1/T9 live proof.
**Dependencies:** T8 and T1's live inline/suggestion capability decision;
do not implement or claim native suggestions before that gate.
**Likely files:** `app/modules/reviews/application/publish_run_review.py`,
`app/modules/reviews/infrastructure/bitbucket_review_publication.py`,
`app/modules/reviews/infrastructure/bitbucket_publication_resources.py`,
`tests/test_bitbucket_review_publication.py`. **Scope:** M.

## Task 10: Publish Bitbucket result status

**Description:** RED tests for pending/success/failure/cancel transitions,
deterministic key and retry after ambiguous response. Route Bitbucket runs to the
commit-status API without GitHub check-run or GitLab status calls.

**Acceptance criteria:**

- [x] Current head exposes one updated Bitbucket status with a run link in the mocked provider; live PR proof remains in T1.
- [x] Repeated transitions update the same key; stale heads cannot gain success. Two portal workspaces reviewing the same remote PR/head use distinct keys and claims.
- [x] Existing GitHub/GitLab status behavior stays green on T10's focused regression run.

**Verification:** RED→GREEN PostgreSQL tests cover delayed running after success,
ambiguous terminal write recovered by leader replay without a later event,
same-head rerun (including tied creation timestamps with a newer, lower UUID),
close-PR cancellation,
lease expiry, stale head/token fences and two portal workspaces on one remote
PR/head. MockTransport tests cover fixed origin, fork source, author 403 and
redirect refusal. The portal-workspace key and 403 regression passed 3 focused
PostgreSQL tests (including Alembic metadata drift); the lower-UUID rerun RED
failed and then passed with the database-generated Bitbucket Run order. Status and key unit suite:
11 passed. GitHub/GitLab/Bitbucket worker and publication regressions: 235 passed;
PostgreSQL check-run/worker/status regressions: 18 passed, 3 skipped. Before
the portal-workspace key change, the broad migration suite passed 152 tests;
the changed migration metadata was rechecked afterward. Ruff, format, mypy
and `git diff --check` pass. Independent review: 0 Standards / 0 Spec; full
backend pytest pending.
**Dependencies:** T8. **Likely files:** `app/modules/reviews/infrastructure/bitbucket_ci.py`,
`app/modules/reviews/application/handle_review_run.py`,
`app/worker.py`, `tests/test_bitbucket_status.py`. **Scope:** M.

### Checkpoint C (T7–T10)

- [x] Full mocked Bitbucket review including publication/status is green on the local round 1 repair diff (`tests/test_bitbucket_worker_full_path.py`); live proof remains open.
- [x] The composed Bitbucket worker mock rejects GitHub/GitLab adapter calls (`tests/test_bitbucket_worker_full_path.py`).

## Task 11: Add Bitbucket RAG indexing and review retrieval

**Description:** RED tests for default-branch snapshot/compare/archive, webhook
refresh, review graph retrieval, UUID/tenant cache separation and revocation. Add
Bitbucket source and provider-specific selection to generic indexer.

**Acceptance criteria:**

- [x] Enabled Bitbucket RAG builds/refreshes an index and injects relevant context into review.
- [x] Repositories/workspaces cannot read one another's index or credential.
- [x] Disconnect/revocation stops new reads and retires affected index only.

**Verification:** Bounded fixed-origin `/src/{sha}/` listing and raw-file tests,
PostgreSQL index/tenant isolation and default-branch push tests, plus a persisted
Bitbucket Run/v5 → worker ReviewContextStage → citable retrieved prompt/manifest
test pass. The same remote UUID in another portal workspace yields no graph
context and uses its own credential. A revoked binding loses target access and
becomes eligible for index disarm while its sibling stays enabled. Since
#350, the canonical v5 prompt is seeded as the common active version; new
Bitbucket, GitHub and GitLab Runs bind that active version unless a repository
has an explicit override. Distinct revision snapshots use a full rebuild; the
unverified `/get/{ref}.gz` route is
not used. Focused tests: 346 passed, 4 skipped without PostgreSQL; 45 passed
with PostgreSQL. Ruff, format and mypy pass; independent review and final full
pytest remain pending. **Dependencies:** T3, T7. **Files:**
`app/modules/code_index/infrastructure/bitbucket_snapshot_source.py`,
`app/modules/code_index/infrastructure/effective_rag.py`,
`app/modules/code_index/infrastructure/index_refresh.py`,
`app/bootstrap/code_index_worker.py`, `tests/test_bitbucket_rag.py`.
**Scope:** M.

## Task 12: Build UI connection and provider views

**Description:** In a separate UI checkout, start with failing Zod/API and
component tests, then implement connect/disconnect, private repo list, provider
links/settings and Bitbucket run history/results. Use the approved OpenAPI contract.

**Acceptance criteria:**

- [x] Connection and disconnect are visible and correctly workspace-scoped;
  mock mode also exercises discovery, two bindings, rotation and independent disconnect.
- [x] Bitbucket repository/PR links, settings and run results render correctly.
- [x] Unknown API data is parsed at the boundary; GitHub/GitLab UI regressions stay green.

**Verification:** `pnpm lint`, `pnpm check-types`, `pnpm format:check`,
`pnpm test`, `pnpm build`, `pnpm verify:prod-bundle`; for `src/` also
`pnpm --dir docs-site check-types` and `pnpm --dir docs-site build`.
**Dependencies:** T4, T8–T10. **Likely files:** `src/entities/repository/model/schemas.ts`,
`src/entities/repository/api/index.ts`, `src/pages/repositories/RepositoriesPage.tsx`,
`src/widgets/repository-list/ui/RepositoryList.tsx`, tests beside changed files.
**Scope:** M; split into connection and views if over five files.

## Task 13: Document contracts and user flow

**Description:** Update backend system/pipeline/secrets/test docs and UI docs-site
with connection, permissions, webhook, RAG, publication, revocation and limitations.
Link both PRs. Core docs-site pages require the special reviewer statement in UI
`AGENTS.md`; prefer a targeted non-core page where suitable.

**Acceptance criteria:**

- [x] Browser OpenAPI and actual endpoint contract agree; webhook contract is documented separately in `docs/BITBUCKET_WEBHOOK.md`.
- [x] User docs explain connection, setup, review results and disconnect without exposing secrets.
- [x] No dependency was added; lockfiles did not change.

**Verification:** Staging CI bundle, env-file, Compose and rollback tests: 232
passed; local Compose and browser/webhook contract tests plus OpenAPI: 203 passed.
API ruff, format, mypy and `git diff --check` green. UI docs-site typecheck/build
green and `/bitbucket` generated; UI lint, check-types, format and build green.
UI full test passed (146 files, 2344 tests), build and production bundle
verification passed on this frozen T13 diff.
Independent review and final full backend pytest remain pending.
**Dependencies:** T4–T12. **Likely files:** `contracts/openapi.yaml`,
`docs/SYSTEM_DESIGN.md`, `docs/PIPELINE_SPEC.md`, `docs/SECRETS.md`,
UI `docs-site/content/docs/...`. **Scope:** M; split by repository.

## Task 14: Run live acceptance and publish evidence

**Description:** Use a real private Bitbucket repo and PR to connect, trigger a
succeeded review, inspect inline/suggestion/summary/status, redeliver event and
confirm no duplicates. Save sanitized evidence and URLs in #333/PR description.

**Acceptance criteria:**

- [ ] Real PR URL and succeeded run URL/UUID are recorded with head/deployment SHA.
- [ ] Inline, suggestion, one summary, status and post-redelivery counts are shown.
- [ ] GitHub/GitLab/backend/UI regressions and all required gates are green.
- [x] Align the five UI #195 Zod mirror assertions and regenerate
  `tests/fixtures/ui_zod_contracts.json` from the current committed UI head
  `46b2807492a52e100bcfb48052b1e640cbd50beb` (2026-10-28; not a
  merged UI SHA). No temporary provider overlay remains in
  `tests/test_ui_zod_contracts.py`. The fixture requires `provider` for
  repository, run session and PR projections and covers ten Bitbucket
  connection DTOs, including 1–128 character response slugs. The historical
  `39ad6768` and `99077506` fixtures had five Bitbucket mirror mismatches;
  the round 5 contract slice passed 365 tests after integrating stacked API
  #359. Recheck the UI head before publication and repin to the
  merged SHA after UI #195 is squash-merged.

**Verification:** Live Bitbucket UI/API, PostgreSQL counts, backend and UI gates;
independent Standards/Spec review. Exact sanitized procedure:
[`333-live-acceptance.md`](333-live-acceptance.md). Current blockers:
disposable private repo PR and scoped credential are unavailable; only local
checks are authorized. The `staging` Environment lacked the Bitbucket keyring
secret and both variables when names were checked on 2026-10-27; recheck before
deploy. Earlier local gate evidence on API base
`f160ce4c` and UI commit `34f81ce9dc5631e7e2061aa9958ec7dac13c5dcb`:
backend ruff check, ruff format check, mypy and diff check passed; focused
rebased PG/API/deployment tests 596 passed; UI lint, typecheck, format,
2344 tests, production build/bundle and docs-site typecheck/build passed.
The full backend run with PostgreSQL completed with 9064 passed, 85 skipped,
3 deselected, 2 upstream warnings in 30m49s. The three deselections are
`test_request_over_its_cpu_allowance_is_killed_by_sigxcpu` and the two
`test_validate_uses_the_profile_of_the_selected_corpus[main-*]` cases;
all three failed identically on a clean `origin/main` archive without #333.
The initial unfiltered run completed with 18 failed, 8963 passed and 85
skipped; the remaining failures were repaired and verified by the final run.
The final wrapper had a zsh `status` read-only variable error after pytest
printed its successful summary, so the recorded process exit reflects the
wrapper rather than a failing test. Historical Task 9 evidence on API
`430d6604` and UI `39ad67682d1247d2674015ed80dd1abcdf223ce9`:
182 UI Zod/OpenAPI contract tests, 25 focused repository/Bitbucket/prompt tests,
ruff check, ruff format check, mypy and diff check passed. Those results do
not establish the current candidate; see the review-round checklist for
current contract gates and live acceptance blockers. No live acceptance
evidence is claimed.
**Dependencies:** T9–T13 and the remaining T1
live probe/accepted suggestion contract. **Likely files:**
`docs/plans/333-todo.md` (evidence checklist), PR bodies. **Scope:** M.

## Review follow-ups outside PR #340

The round-1 review of PR #340 marked the P3 items below for separate work.
They are tracked in [follow-up issue #360](https://github.com/larchanka-training/dmc-268-api-t6/issues/360),
assigned to `ndovnar`. They remain open technical debt, not completed
acceptance criteria; do not cite this checklist as a merged fix. The partial
schema correction for P3 3 remains in this PR.

| P3 | Follow-up | Acceptance criterion |
| --- | --- | --- |
| 1 | Remove unused Bitbucket connection paths and define `intake_enabled` on rotation. | No dead credential client or revoke path remains; lifecycle tests cover the selected intake state. |
| 2 | Bound token-rotation fan-out and deadline. | A slow or failed repository cannot cause unbounded work, and the failing repository is identified without exposing credentials. |
| 3 | Paginate workspace connection bindings. | `list_bindings` is bounded, `nextCursor` is stable and real, and page/tenant boundaries are tested. |
| 4 | Cache default rule-set reads. | Repeated GET/discovery avoids file reads while returned rules stay deterministic after an update. |
| 10 | Re-encrypt stored tokens during key rotation. | An operator can migrate Bitbucket/GitLab ciphertexts before old-key removal, with rollback instructions and tests. |
| 13 | Scale push projection beyond 500 open PRs. | A push with more than 500 candidates completes or resumes safely, including fork sources, without silently losing reviews. |
| 20 | Replace cross-module imports of private Bitbucket helpers. | Public helpers or shared value objects replace `_path`, `_check_response` and `_parse_uuid` with behavior tests green. |
| 26 | Bound provider calls per inline finding. | Measured GET/POST and claim-refresh counts for N findings are bounded without weakening head or claim fences. |
| 27 | Avoid Bitbucket status reads for other providers. | GitHub/GitLab Run transitions make zero Bitbucket status-source queries; Bitbucket transitions retain status intent. |

Earlier P3 items retained in #340 have these round 5 gates: #5 has direct proxy
mutation tests; #7 has migration/ORM CHECK-name tests; #9 remains a private
percent-encoded UUID-locator live probe; #14 adds `updated_on` and
`diagnostics:ping` acceptance cases; #21 requires the strict
`vcs_provider_name` behavior in the PR body; #23 has heartbeat failure logging
proof; #28 tests credential-generation recovery; #32 boots Bitbucket workers
without fake GitHub credentials; #39 replaces stale links/checkpoints here;
#44 pins the open UI #195 head now and tracks the merged-SHA repin in #360.
None of these statements substitutes for a final published-head test run.

### Round 4 P3 45–60 disposition (local candidate, not published proof)

| P3 | Disposition | Remaining gate |
| --- | --- | --- |
| 45–46 | Task 15 removes the duplicate checkpoint and unreachable size branch. | Final diff and index gates on the published SHA. |
| 47 | Symlink/non-UTF-8 skip and 429 tests are in the candidate; Task 11 adds unsafe diffstat full fallback and 5xx/subrepository proofs. | Task 11 independent review passed; final gates and published SHA remain. |
| 48 | Task 15 reuses unchanged Bitbucket importer content across base/head with complete pinned trees and diff. | Request-count and PostgreSQL RAG proof on the final candidate. |
| 49 | Fork projection, deleted-fork close/decline and prefix handling are in the candidate. | Task 19 mutation tests and private fork live probe. |
| 50 | Queued re-entry and status backoff are fixed in the candidate. | Final PostgreSQL suite. |
| 51 | Pipeline spec now distinguishes Bitbucket `STOPPED` from GitHub's absent check-run for `repo_disabled`. | Final docs review. |
| 52 | Tech-lead decision: whether superseded old-head `INPROGRESS` needs remote `STOPPED`. | Record decision and private Bitbucket merge-check probe. |
| 53–55 | OpenAPI/runtime response bounds, rules-status descriptions and Bitbucket-only redaction with standard GitHub/GitLab 422 are in the candidate. | Final contract tests and published SHA. |
| 56 | Direct security/helper mutation tests are in the candidate. | Final gates. |
| 57 | Task 15 keeps RAG index refresh on signed default-branch pushes when review is disabled, RAG is enabled and the installation and binding remain available. | PostgreSQL refresh proof and final gates. |
| 58 | Task 19 adds copied/draft/ORM and production-composition proofs and replaces brittle call-count assertions. | Final gates on the integrated candidate. |
| 59 | SD publication sentence and PIPELINE_SPEC `context.manifest` punctuation corrected. | Final docs review. |
| 60 | Rules-status provider defaults removed in the candidate. | Final contract tests. |

Two release follow-ups remain explicit: after UI PR #195 is squash-merged,
repin `UI_CONTRACT_COMMIT` and regenerate the API fixture from the merged SHA
([issue #360](https://github.com/larchanka-training/dmc-268-api-t6/issues/360));
merge and deploy API PR #340 before UI PR #195. Full live acceptance still
needs a disposable private Bitbucket PR and a scoped token. The earlier
public read-only locator probe does not prove private access, inline,
suggestion, status or redelivery behavior.

### Final checkpoint

- [ ] All issue AC, including real Bitbucket proof, satisfied and linked.
- [ ] Every development slice has a fresh independent review with zero findings.
- [ ] Both repos rebased on current main, all gates green, PRs opened and linked.
- [ ] Keep issue open for tech-lead AC check; do not use closing keywords.
