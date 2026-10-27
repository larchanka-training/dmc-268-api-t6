# Implementation plan: #333 Bitbucket Cloud

## Source and baseline

- Issue: https://github.com/larchanka-training/dmc-268-api-t6/issues/333 (read 2026-10-26).
- Code baseline: `origin/main` at `88bbc073`; the original checkout is an older
  `fix/111-worker-portal-url` branch. Implement on a fresh checkout of `origin/main`.
- Scope: full Bitbucket Cloud support across backend, UI and user documentation.
  Bitbucket Data Center/Server is outside scope.
- The T1 contract checkpoint below records the documented Cloud surface as of
  2026-10-26. A disposable private-repository probe is still required for
  suggestion, inline-coordinate and fork/status behavior.

## Architecture decisions

1. **Identity.** Persist the immutable Bitbucket repository UUID per provider
   installation, separate from mutable workspace/repository slugs. Never hash a
   UUID into `BIGINT` or use slugs as identity. Existing
   `ProviderInstallation.external_id`, `Repository.external_id` and
   `CodeChange.external_id` are numeric. If compatibility requires an integer,
   allocate a unique DB surrogate and constrain its mapping to the authoritative
   UUID. The same remote UUID can be connected once in each portal workspace;
   lookup always includes portal workspace and installation, and disconnect or
   revocation of one connection leaves the other intact. Preserve existing
   GitHub/GitLab rows and public API semantics. Use an additive Alembic revision
   and PostgreSQL tests. No Bitbucket rows predate this provider, so this
   revision has nothing to backfill.
2. **Connection.** Add authenticated workspace-scoped endpoints to list accessible
   Bitbucket Cloud repos, connect, inspect and disconnect; support token rotation
   and revocation. The issue requires a portal workspace member who can access the
   selected Bitbucket repository; it does not require a new administrator role.
   One credential installation may bind multiple repositories in the same
   Bitbucket workspace. Connecting another repository to it must reuse its
   credential without silently replacing the token; replacement is an explicit
   rotate operation. Each repository binding has its own encrypted webhook
   secret and receiver URL, so configuring or rotating one hook leaves sibling
   hooks intact. Repository-level disconnect deactivates only that binding and
   revokes its hook secret, leaving siblings usable. Revoke the whole
   installation only on explicit request or when the last binding is removed.
   An explicit reconnect after the last disconnect reopens the same immutable
   installation identity with a higher credential generation and a fresh
   per-repository secret.
   Each repository has a separate binding generation: partial disconnect and
   rebind fence only that repository's in-flight work. Shared credential
   generation changes on token rotation or whole-installation revocation, not
   when a sibling binding changes. For token rotation, validate every active
   repository by its immutable UUID using Bitbucket's documented
   `/2.0/repositories/%7B%7D/%7B{uuid}%7D` locator, so a renamed repo or
   workspace remains resolvable even if its old slug and token no longer work.
   Refresh names and URLs in the same final transaction. Read provider metadata
   outside DB transactions; carry each binding generation through the remote
   read and recheck the full repository ID/UUID/generation set under lock before
   replacing the shared token. This rejects disconnect+rebind during network I/O
   even when the active repository IDs look unchanged. Onboarding creates
   ordinary repository settings and rules.
   Late webhook/worker work cannot revive a disconnected binding. Use a scoped
   Atlassian API token for the first flow; freeze OpenAPI/UI after a disposable
   credential probe.
3. **Secrets and authority.** Reuse the encrypted credential and per-run origin
   authority pattern in `app/modules/integrations/gitlab/`. Bind ciphertext to
   workspace/installation, never return/log a token, and check shared and
   repository binding generations plus revocation before provider calls and
   final writes. Constrain requests to
   `api.bitbucket.org` over TLS; reject or independently validate redirects and
   pagination URLs. Network calls remain outside transactions.
4. **Webhook.** Verify `X-Hub-Signature` HMAC over original body bytes before JSON
   parsing. Persist a namespaced, bounded receipt with stable delivery identity,
   collision detection, lease/retry and durable deduplication. Normalize PR create/
   update and relevant push events to the existing projection/trigger path. Current
   PR state and pinned revisions decide run creation; `(code_change, head)` protects
   against distinct deliveries. New heads cancel old work.
5. **Analysis routing.** Extend `PullRequestLocator` and all provider selectors;
   carry Bitbucket and immutable origin through queue, worker, manual rerun,
   cancellation and publication. Add PR metadata, revision-pinned diff/diffstat,
   blob/tree and policy reads with bounded pagination/size, rename/omission
   semantics and revision checks. Incomplete answers never become clean diffs.
   Reuse snapshot/v4, rules, settings, history and manual actions. Bitbucket runs
   make no GitHub/GitLab requests.
6. **Publication.** Use Bitbucket PR comments with tested line coordinates, one
   summary marker, stable per-finding markers and a deterministic commit-status key.
   Recheck current PR head and origin before outward writes; replay discovers prior
   comments/status. Handle rate limits and transient errors with bounded retries.
   Atlassian documents suggestions in the UI, but the public REST comment schema
   inspected here does not establish a native suggestion field. Verify API syntax
   on a disposable real PR before claiming native support. Plain inline patch
   text is the documented fallback, subject to explicit AC acceptance.
7. **RAG.** Add a Bitbucket snapshot source to the provider-neutral indexer and
   extend GitHub-only eligibility specifically for Bitbucket. Index canonical
   UUID-bound repositories at pinned revisions; use their context in review;
   isolate cache/index rows by repository/workspace. Revoke/disconnect prevents
   new reads and purges or inactivates the affected index.
   The public `/src/{commit}/` directory and raw-file endpoints provide the
   snapshot. Traverse validated same-origin paginated listings, read every file
   at the pinned SHA, enforce the existing archive size and entry limits, then
   assemble a local tar archive for the generic indexer. A missing or changed
   page/file fails the build. The non-REST `/get/{ref}.gz` archive has no verified
   scoped-token and redirect contract; a read-only probe of the disposable empty
   repository returned 404, so do not use that URL or forward the token to a
   redirect. Distinct SHAs use a full rebuild because diffstat does not prove
   ancestry for an incremental index. Seed `review.system` v5 with the
   retrieved-excerpt contract inactive globally, then pin new Bitbucket Runs
   to v5 when no repository prompt override is set. GitHub/GitLab keep their
   currently active prompt, and an explicit repository override remains
   authoritative. Bitbucket v5 renders citable retrieved blocks in the review
   prompt and records their bounded source identity in the call manifest.
8. **UI/docs.** A separate UI checkout/PR implements connect/disconnect, provider
   links, repository settings, run screens and user docs. `contracts/openapi.yaml`
   is the browser API contract; webhook behavior belongs in pipeline docs.

## Dependency graph

`Documented UUID contract → identity/authority → browser connection` and
`webhook → projection/trigger → VCS/worker → publication`. The T1 live
comment/suggestion capability decision gates publication, not UUID persistence.
RAG can start after
identity and VCS authority. Integrated and live proof follows all paths. Every
development task in `333-todo.md` starts with a failing public-boundary test,
continues with minimal implementation and focused verification, then a fresh
Standards/Spec review.

## Acceptance mapping

| Issue criterion | Planned proof |
| --- | --- |
| Private repo connect/disconnect | T2–T4, T10; scoped API/UI and two-workspace PostgreSQL tests |
| Signed PR/push webhook, dedup/retry | T5–T6; raw HMAC, replay/concurrency and worker tests |
| Pinned analysis, settings, rerun/cancel/history | T7–T8, T10; full composition with rejecting GitHub/GitLab transports |
| Inline/suggestions/one summary/status, no stale duplicates | T1 capability proof, T9 and live PR evidence |
| Bitbucket RAG and isolation | T11; index build/retrieval and tenant tests |
| UUID/rename/token revocation | T2–T3, T11; PostgreSQL identity/authority tests |
| UI and GitHub/GitLab regressions | T10, T12; UI tests and both provider suites |
| Real succeeded run/redelivery and docs | T12; sanitized PR/run/status/comment evidence and doc links |

## Verification and completion

Run baseline and final backend gates: `uv run ruff check .`, `uv run ruff format
--check .`, `uv run mypy .`, `uv run pytest`. PostgreSQL integration tests need a
real `TEST_DATABASE_URL`; skipped tests do not prove DB acceptance. Run focused
GitHub/GitLab regressions after shared routing changes. Use `uv` and never
`--no-verify`. Run independent UI gates. Refresh open-PR file ownership before
editing and comment on overlapping PRs first.

Live acceptance requires a disposable Bitbucket Cloud workspace/private repo,
scoped credential and webhook secret, externally reachable receiver, test PR,
running DB/queue/workers and configured LLM. Record real PR URL, succeeded run
URL/UUID, head SHA, deployed commit, inline/suggestion/summary/status evidence and
counts after redelivery. Never put credentials into issue/PR text. If resources
are unavailable, leave live AC unchecked rather than inferring success from mocks.

## Risks and unresolved decisions

| Risk | Mitigation |
| --- | --- |
| UUID migration breaks existing providers | Additive migration, PostgreSQL and GitHub/GitLab regressions |
| Auth permissions differ from assumed flow | T1 official-doc contract and disposable credential check |
| REST cannot create native code suggestions | T1 live capability proof before T9 contract choice |
| Tenant token/index leakage | Immutable origin, fail-closed scope, generation checks, negative DB tests |
| Redirect/rate-limit surprises | Same-host URL checks, bounded pages/body, `Retry-After` tests |
| Publication after head change/replay | Pre-write head checkpoint, stable markers, live recount |
| Concurrent PR overlap | Rebase, inspect current open-PR ownership before each slice |

The coordinator presented this plan for feedback before development, as
required by the invoked agent-loop skill.

## Sources checked

- [Bitbucket webhook signature](https://support.atlassian.com/bitbucket-cloud/docs/manage-webhooks/)
- [Bitbucket pull request API](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-pullrequests/)
- [Bitbucket source API](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-source/)
- [Bitbucket commit status API](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-commit-statuses/)
- [Bitbucket API token authentication](https://support.atlassian.com/bitbucket-cloud/docs/using-api-tokens/)
- [Bitbucket UI code suggestions](https://support.atlassian.com/bitbucket-cloud/docs/review-code-in-a-pull-request/)

## T1 Cloud contract checkpoint (2026-10-26)

### Authentication and permissions

The first flow uses a workspace-scoped Atlassian API token as
`Authorization: Bearer <token>` on backend requests only. Atlassian also
documents Basic auth with account email, but Bearer avoids collecting that
second identifier. The minimum saved token scopes for manually configured
webhooks are `read:repository:bitbucket` (private repository discovery,
source, diff and build status writes) and `read:pullrequest:bitbucket` (PR
metadata and comments). The **read** scopes on comment POST and build status
POST/PUT are explicit in the current endpoint references; do not infer a need
for repository/PR write scopes from the HTTP verbs. Atlassian recommends OAuth
for integrations; revisit it after the token flow. API tokens can be scoped to
one Bitbucket workspace and cannot manage workspace actions.
[Token authentication](https://support.atlassian.com/bitbucket-cloud/docs/using-api-tokens/),
[token permissions](https://support.atlassian.com/bitbucket-cloud/docs/api-token-permissions/),
[token limitations](https://support.atlassian.com/bitbucket-cloud/docs/api-tokens/),
[PR endpoints](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-pullrequests/),
[status endpoints](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-commit-statuses/).

For the first flow, the user creates a **repository** webhook in the Bitbucket
UI after binding a repo. This requires that user to be a Bitbucket repository
admin, but not a broader saved token. The portal shows its URL, events and
generated secret once. Disconnect revokes the local binding and immediately
rejects later hook deliveries; the user also removes the hook in Bitbucket.
Automated hooks are optional: list/create/update need `read:webhook:bitbucket`
and `write:webhook:bitbucket`; deletion needs `delete:webhook:bitbucket`. Creating
a hook also needs the scopes for subscribed events. Do not request webhook
scopes in the manual flow. [Repository webhook API](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-repositories/),
[webhook setup](https://support.atlassian.com/bitbucket-cloud/docs/manage-webhooks/).

The existing portal `AuthScope` and `GitHubUserWorkspaceAccess` record
membership, not an administrator role. For #333, every browser operation must
require current portal workspace membership. Connecting/rotating additionally
verifies that the supplied Bitbucket token reads the exact chosen private repo.
The issue's AC does not justify inventing a new workspace role. This allows an
existing member to manage a connection shared with the workspace; document this
product access policy and test that no member of another workspace can view,
rotate or revoke it. A connected token alone never grants portal membership.

### Provider API and identity

| Use | Bitbucket Cloud endpoint | Constraint |
| --- | --- | --- |
| Discover repos | `GET /2.0/repositories/{workspace}` | Repository read scope; bounded pagination |
| Verify chosen repo | `GET /2.0/repositories/{workspace}/{repo_slug}` | Compare returned UUID, workspace and privacy |
| Current PR | `GET /2.0/repositories/{workspace}/{repo_slug}/pullrequests/{id}` | PR read scope; capture both repository UUIDs and head hashes |
| Pinned diff/stat | `GET /2.0/repositories/{workspace}/{repo_slug}/diff/{head}..{destination}?topic=true` and matching `/diffstat/` | Repository read scope; source head **first**, opposite `git diff` |
| Pinned file/tree | `GET /2.0/repositories/{workspace}/{repo_slug}/src/{commit}/{path}` | Repository read scope; limit size and validate redirects |
| PR comments | `GET/POST /2.0/repositories/{workspace}/{repo_slug}/pullrequests/{id}/comments` | PR read scope; paginate GET, POST returns 201 |
| Build status | `POST /2.0/repositories/{workspace}/{repo_slug}/commit/{head}/statuses/build` | Repository read scope; repeated key on same commit overwrites |

The [repository API](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-repositories/)
returns `uuid`, `full_name`, `is_private` and mutable slugs. The
[UUID guide](https://developer.atlassian.com/cloud/bitbucket/rest/intro/#universally-unique-identifier)
says UUID survives rename/move. Parse and persist the authoritative UUID;
slugs are refreshable locators and labels. A PR identity is destination repo
UUID plus numeric PR id; source repo UUID is separate for forks. No UUID is
hashed into a `BIGINT`. The PR-specific diff/diffstat endpoints return 302 to a
current revspec, so they cannot pin an earlier snapshot. The
[commit compare API](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-commits/)
defines `head..destination` with `topic=true` as the source-versus-merge-base
diff, and diffstat as paginated JSON. Fetch PR heads, compare exact SHAs, then
refetch the PR before publication. A fork whose source cannot be read or an
omitted/binary/oversize diff yields an incomplete review, never a clean one.
The T7 adapter uses UUID repository routes for all reads, compares the PR
metadata before and after diffstat and patches, validates each file patch
against the diffstat paths and line counts, and limits diffstat to 200 files
over two pages. It validates UTF-8 path bytes, JSON and cumulative response
sizes. The per-file `diff?path=` behavior and fork compare availability still
require the disposable PR live probe before final acceptance; a provider
response that cannot be verified becomes an omission or fetch failure.
For fork PRs, the adapter reads the pinned source commit through the source
repository UUID before and after diff fetching; an unreadable private fork
fails the review instead of producing a clean result. After a stored diff
snapshot is retried by a fresh worker, its synthetic blob IDs are resolved
from a newly verified PR/diffstat snapshot, so no process-local map is required.
The [source API](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-source/)
can return 301 to Atlassian media for LFS: never forward the bearer token to
an unvalidated redirect. Follow pagination only to an exact trusted HTTPS API
origin, with page and body limits.

### Signed webhook and publication

Manual Bitbucket hook configuration uses URL
`https://<portal-host>/webhooks/bitbucket/{installation_id}/{repository_id}`,
a distinct random secret for each repository binding,
and events `repo:push`, `pullrequest:created`, `pullrequest:updated`. The
receiver authenticates `X-Hub-Signature: sha256=<hex>` against the original
body bytes before parsing JSON, using constant-time comparison. The signature
is absent if the hook has no secret and such deliveries are rejected.
`X-Event-Key` selects the event; `X-Hook-UUID` identifies the hook;
`X-Request-UUID` identifies a request and `X-Attempt-Number` reports retries;
the official event page does **not** promise UUID stability across attempts,
so T5 must probe it and also use `(PR, head)` for effect deduplication. The URL
selects the binding and its secret before any body parsing. Receipts are
namespaced by repository binding and hook UUID, and constrained to the remote
repository UUID. Refetch current PR state because events can arrive late; one
`(PR, head)` creates one run. [Webhook signatures](https://support.atlassian.com/bitbucket-cloud/docs/manage-webhooks/),
[event headers and payloads](https://support.atlassian.com/bitbucket-cloud/docs/event-payloads/).

Documented comment POST shape (new-file line; real line behavior still needs a
probe):

```json
{"content":{"raw":"Finding text\n\nSuggested patch to apply manually:\n```suggestion\nreplacement\n```\n<!-- ai-review:RUN_ID:FINDING_ID -->"},"inline":{"path":"src/example.py","to":42}}
```

Old-file lines use `inline.from`. Do not send both coordinates. The summary
uses `content.raw` with one stable marker and no `inline`; list comments
before retry to find markers. The [REST comment schema](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-pullrequests/)
documents `content` and `inline`, but no native suggestion field. The
[Bitbucket UI guide](https://support.atlassian.com/bitbucket-cloud/docs/review-code-in-a-pull-request/)
documents native suggestions in the editor, not an API format. The fenced
block above is **plain comment text**, not an asserted applyable suggestion.
If a disposable PR probe cannot establish native API creation, use that
fallback only with explicit acceptance of the issue's suggestion AC.

Build status POST request shape:

```json
{"key":"AI-REVIEW-<32 hex chars derived from portal workspace UUID, repository UUID, PR number and head SHA>","state":"INPROGRESS","name":"AI Code Reviewer","description":"AI review running","url":"https://review.example/runs/RUN_ID","refname":"feature-branch"}
```

Use one deterministic key per repo/head and update it to `SUCCESSFUL` or
`FAILED`. For cancellation, use `FAILED` with a cancelled description until a
Cloud probe confirms whether `STOPPED` is accepted. `refname` must equal the PR
source branch for association. Reusing a key overwrites only a status by the
same Atlassian author (or a repo admin), so rotation to another account must
handle status ownership instead of assuming overwrite.
The worker persists one desired status per portal workspace, immutable repository
UUID, PR number and head SHA. Two portal workspaces reviewing the same remote PR
have independent status keys and leases. A short PostgreSQL lease serializes
writes for each key while
provider HTTP runs outside a transaction; a heartbeat and generation fence
guard each write. The leader replays ambiguous or interrupted transitions and
periodically reasserts terminal states. A `403` records an actionable
`author_forbidden` failure and stops automatic retries for the current Run;
rotation to a different author does not create another key. Bitbucket has no
conditional status-write version, so an already in-flight provider request
that outlives its lease can temporarily overwrite a newer state; the 10-second
HTTP timeout, 90-second renewed lease and terminal reconciliation bound and
repair that case rather than claiming strict remote ordering.
`bitbucket_run_origins.run_order` is a database-generated monotonic order for
Bitbucket Run insertion. Same-head reruns use this order for authority even when
database timestamps tie and the newer Run has a lower random UUID.
[Status API](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-commit-statuses/),
[status author rule](https://developer.atlassian.com/cloud/bitbucket/bitbucket-api-changes-commit-status/).

### Draft browser contract for T4/UI

The UI reads `provider` as a required `github | gitlab | bitbucket` field on
repository, run and pull-request references. It rejects absent or unknown
values at its Zod boundary and follows server supplied repository/PR URLs.
The Bitbucket connection screen requires an explicit portal workspace choice
from `/auth/me`; it never picks the first workspace. Discovery and connection
lists follow their bounded cursors. A shared credential rotation is a distinct
action from repository disconnect or per-repository webhook-secret rotation.
The one-time webhook secret remains transient in UI state. Demo mode implements
the same workspace-scoped connection lifecycle without calling a live API.

These paths use the portal JWT, never the Atlassian token. `workspace_id` is
the portal UUID. Save the API token under the installation/workspace identity,
and each webhook secret under installation/workspace/repository identity. Rotate
the shared credential generation on token replacement or last-repository
disconnect; partial disconnect/rebind advances only that repository's binding
generation. Secrets appear only in request bodies or a **one-time** repository
webhook setup response. No token or webhook secret appears in GET, logs or
ordinary errors. The implemented contract is in `contracts/openapi.yaml`; PR
#283's owner was notified before editing that shared file.

| Method and path | Request | Success |
| --- | --- | --- |
| `POST /api/workspaces/{workspace_id}/bitbucket/discovery` | `{bitbucketWorkspace,apiToken,cursor?}` | `200 {repositories:[{uuid,workspaceSlug,slug,name,isPrivate,htmlUrl}],nextCursor?}`; no credential persisted |
| `POST /api/workspaces/{workspace_id}/bitbucket/connections` | `{bitbucketWorkspace,repositorySlug,repositoryUuid,apiToken}` | `201 {id,repositoryId,repositoryUuid,workspaceSlug,repositorySlug,status,credentialGeneration,webhook:{url,events,secret}?}`; `id` is shared installation ID, `repositoryId` is binding ID, webhook is null on idempotent connect; new binding starts `awaiting_webhook`, an idempotent connect returns its current per-repository status |
| `GET /api/workspaces/{workspace_id}/bitbucket/connections` | none | `200 {connections:[{id,repositoryId,repositoryUuid,workspaceSlug,repositorySlug,status,credentialGeneration,webhookUrl}],nextCursor:null}`; each status is `awaiting_webhook` until its first signed receipt and `active` thereafter |
| `PUT /api/workspaces/{workspace_id}/bitbucket/connections/{installation_id}/credential` | `{apiToken}` | `200 {id,credentialGeneration}`; verifies all active repositories before replacing shared token |
| `POST /api/workspaces/{workspace_id}/bitbucket/connections/{installation_id}/repositories/{repository_id}/webhook-secret` | none | `200 {url,events,secret}` once, rotating only this repository's hook secret |
| `DELETE /api/workspaces/{workspace_id}/bitbucket/connections/{installation_id}/repositories/{repository_id}` | none | `204`, safe on repeat; last binding revokes the installation credential |

The receiver accepts `POST /webhooks/bitbucket/{installation_id}/{repository_id}`
with `X-Hub-Signature: sha256=<hex>` over at most 256 KiB of raw body and
`X-Event-Key`, `X-Hook-UUID`, and `X-Request-UUID`. It rejects invalid signatures
before JSON parsing. A signed receipt pins the immutable installation/repository
origin and both repository binding and secret generations. PostgreSQL rechecks
those generations under locks before insert; secret rotation and disconnect
invalidate an in-flight delivery. First valid receipt atomically stores
`confirmed_at` on that repository and marks its browser status `active` without
altering a sibling repository's status. Shared `intake_enabled` is only an
internal indication that some signed hook arrived since the last token rotation.
The receipt key namespaces the hook/request UUID pair by installation and
repository; the documented headers do not prove request UUID stability across
provider retries, so T6 additionally deduplicates effects by `(PR, head)`.

`repositoryUuid` in the connect request is a selection guard only: resolve the
slug with the submitted token and compare the returned UUID/workspace before
saving it. Return 400 for invalid input, 401 for portal auth, 404 for a
workspace/connection outside membership, 409 for stale generation or UUID
ownership conflict, 422 for inaccessible repo or missing scopes, 429 for a
provider rate limit (bounded `Retry-After`), and 502 for provider failure.
Provider discovery accepts only bounded page/after cursors on the trusted
repository-list path and reads at most 2 MiB of decoded JSON per response.
T4 pins these public HTTP shapes with ASGI tests and OpenAPI.

### Live capability proof still required

The 2026-10-26 read-only probe used a short-lived token from a local private
file (never copied into the repo): `GET /2.0/repositories/nvdovnar/test-ai-review`
returned 200, `uuid={3791f4cc-f59a-4412-9a52-f34a7829ba1c}`,
`full_name=nvdovnar/test-ai-review`, `is_private=true`, workspace
`uuid={b2d0d95e-1f8d-4990-9fb2-f09f87ae5f60}`. `GET` the open PR list
returned 200 with `size=0`, so no comment, suggestion, status or webhook
capability was probed; no write was made. A second read-only probe of the
documented UUID locator returned 200 for the same private repository, proving
rotation can resolve it after a slug change. [Atlassian UUID URI contract](https://developer.atlassian.com/cloud/bitbucket/rest/intro/#repository-object-and-uuid).
Once a disposable PR exists, read it,
create one inline
comment and one candidate native suggestion on disposable lines; inspect the
actual PR UI and GET comment representation; post/update/read one build status
with `refname`; inspect signed webhook headers and redelivery. Record sanitized
HTTP status, request shape, returned IDs/coordinates, UI result and any scope
error. Then delete test comments/hook and revoke token. A failed or missing
native-suggestion proof leaves that AC open; mocks/docs cannot close it.
