# Implementation Plan: GitHub App reinstall (#113)

## Status and implementation gate

**Approved by the tech lead on 2026-10-10.** The user approved the complete
preservation contract and authorized implementation after checking for an existing
PR. No open PR covered #113; ownership comments were posted before overlapping
files changed. The contract is recorded in `docs/SYSTEM_DESIGN.md` §6.9.

Source: [issue #113](https://github.com/larchanka-training/dmc-268-api-t6/issues/113).
Planning date: 2026-10-10. Working tree initially clean on
`fix/111-worker-portal-url`, HEAD `f5a1e56a8259636f17d3c2862f8de89e53762e8b`.
`origin/main` and GitHub's current main both resolve to
`92250a1fb3b65f88a2ea111d33613f9a4af1d1d2`. Current branch contains #111;
implementation must start from updated main on `feat/113-app-reinstall`, without
including #111. Planning creates only this file and `113-todo.md`; no branch switch.

## Problem and current behavior

- `Repository` is unique by `(provider_installation_id, external_id)`; Run/PR,
  rules and conventions hang from its internal UUID.
- OAuth creates a Workspace per new external installation ID and stores only
  `metadata.account_login`. `GitHubInstallation` has no numeric account ID.
- Deleted/removed projection writes revocation tombstones, deletes all matching
  user repo grants, and sets `enabled=false`. Delivery-effect markers make replay
  safe; OAuth generation reservations and timestamps prevent stale grant revival.
- Created/added upsert unconditionally sets `enabled=true`. SYSTEM_DESIGN §6.9
  explicitly documents that it loses a user's pause.
- Portal access is the intersection of JWT Workspace claims, current Workspace
  grants, and current installation/repository grants. `enabled` is not authorization.
- Worker messages carry an external installation ID, but some lifecycle targets
  are reconstructed from the Repository's current installation. Rebinding without
  guarding old work can accidentally reuse old work under the new installation.

## Approved contract (2026-10-10)

### Identity, preservation and authorization

1. Preserve a Repository UUID, its PR/Run history (including findings/actions), all
   settings (`default_engine`, `wait_for_ci`, `max_comments`, `review_event`, prompt
   selection), immutable RuleVersions/active rule, conventions, and its Workspace
   UUID/budget when the new GitHub installation belongs to **the same verified
   numeric account ID** and fresh OAuth confirms access to that exact external
   repository ID. Login/full name are mutable presentation fields, never proof.
2. Same account means installation owner (organization/user), not the OAuth user's
   ID. Source account evidence must have been persisted from authenticated GitHub
   OAuth installation listing while that exact old installation still existed.
   Target evidence and the complete repository list come from the fresh user-token
   listing. Repository access is restored separately for each confirming user.
3. Create a new `ProviderInstallation` UUID/row with the new external ID; never
   overwrite/recycle the old external installation ID. Reuse the old Workspace
   only when its account lineage is unambiguous. Move only OAuth-confirmed same-
   account Repository FKs to the new installation in the same transaction that
   installs the confirming user's new grants. Repository children retain their IDs.
4. Webhook `created`/`added`, login names, a matching repo ID alone, an old JWT and
   refresh alone cannot grant access or authorize rebind. Before fresh OAuth:
   `/api/repos` and `/api/runs` omit the old data; detail/mutation returns 404.
   Keep the shared access predicate and all #101 guarantees. After confirming user
   A, user B with a stale JWT still lacks new repo grants until B confirms access.
5. Rebind only from an installation durably marked deleted, with matching known
   account ID and an unambiguous source Repository. Partial reinstall selection
   leaves unconfirmed old repositories archived/inaccessible. Mere per-repo
   removal is not proof of installation replacement.

The GitHub REST listing exposes numeric `account.id`; user-token repository
listing supplies current access. See [GitHub installation REST endpoints](https://docs.github.com/en/rest/apps/installations?apiVersion=2022-11-28).

### Pause and App availability

- Separate persisted user preference from App availability: retain `Repository.enabled`
  as the user's review preference and add `installation_available` (or an equivalent
  clearly named boolean). Effective review eligibility/API `enabled` is
  `enabled AND installation_available`.
- PATCH `enabled` changes only the user's preference. Removed/deleted changes
  availability to false and revokes grants; created/added restores availability
  after authorized linkage, never changes the user's preference. Newly created
  repositories default to preference true. No new public API field is required.
- Update every worker/trigger/rerun eligibility read to use the effective value;
  repository visibility continues to use grants, independent of pause/availability.
- Legacy `enabled=false` cannot reveal whether removal or a user caused it. Preserve
  false conservatively as paused, document that the user can explicitly enable
  reviews after reconfirmation. Never infer the former pause state.

### Transfers, missing evidence and ambiguity

- Transfer to a different account creates a fresh Repository UUID under the target
  Workspace/installation, even if GitHub external repo ID is unchanged. New owner
  receives default settings/rules and preference true; no old history, rules,
  conventions, pause or Workspace/budget is copied. Source data remains archived
  under its source account and receives no target grants. Returning to a source
  account must still satisfy fresh OAuth and unambiguous source identity; never
  merge across account boundaries.
- Existing rows with only `account_login` cannot auto-rebind. Fresh OAuth may
  establish numeric identity only for that exact existing installation while it
  is still accessible. Already deleted legacy installations without such evidence
  take the documented clean-slate path; preserve the old archive inaccessible.
- Multiple eligible source rows/Workspaces are not guessed or automatically merged:
  create a fresh target identity and leave archives inaccessible. Log a non-sensitive
  diagnostic. No manual data-merging UI or administrative migration in this issue.

### Stale events, queued work and transactions

- Add a durable installation deletion marker. Old rows/tombstones/delivery effects
  stay tied to old installation UUIDs. Delayed distinct removals from an old
  installation cannot disable/revoke a rebound repository's new installation.
- Deleted installations cannot be reactivated by old created/added receipts. Before
  onboarding GitHub calls and again in the short write transaction, reject retired
  source installation work; a transaction already fetching during delete must not
  upsert or re-enable after deletion. New-installation receipts remain deferred until
  OAuth links it, then replay idempotently without resetting pause/rules.
- Old PR/CI deliveries remain scoped to their old external installation identity;
  no routing to the new installation by repo ID/name. Audit resolver/projection
  lookups; avoid changing public receipt outcomes unless needed for correctness.
- Old queued/retrying/nonterminal Runs must not silently switch to the new
  installation when lifecycle targets are rebuilt. Cancel/skip them with the existing
  appropriate terminal state and guard claimed/publishing work against a retired or
  mismatched installation before further provider calls. Keep historical snapshots;
  a new user rerun after reconfirmation explicitly uses the new installation.
  Already in-flight external requests may finish; no transaction may span GitHub/LLM.
- Preserve OAuth generation ordering and revocation timestamp checks. Serialize
  account-lineage selection/new-installation creation/rebind across different users
  with a per-account PostgreSQL advisory transaction lock or equivalent unique-row
  lock, then installation rows in deterministic order. Keep network calls between
  the existing reservation/apply transactions. Recheck target deletion/tombstones
  before moving each repository: a snapshot older than target removal must not
  create grants or make preserved data visible.

## Persistence direction

Add nullable positive BIGINT `account_external_id` and nullable `deleted_at` to
ProviderInstallation, plus `installation_available` to Repository. Null numeric
account ID is explicitly unverified legacy identity. Keep existing uniqueness
constraints; index account lookup if needed. No broad schema refactor/account table.
New migration only, named with the next unused `YYYYMMDD_NNNN` revision; never edit
merged migrations. Backfill availability conservatively from existing revocation
evidence; retain `enabled` values. Migration does not call GitHub or infer identity
from login. Check how deleted legacy rows with whole-installation tombstones are
marked; migrations must avoid treating per-repo removal as installation deletion.

Implement rebind in a focused flush-only adapter consumed by the OAuth application
boundary; use case owns commit. Explicitly check uniqueness collisions and archive
ambiguity. Existing Workspace access does not provide a repo grant to other users.

## Work order and checkpoints

Use tasks in `113-todo.md`. Sequence: decision/ownership → numeric identity/schema
→ effective pause → same-account rebind → stale worker guards → PostgreSQL proof
→ docs/review/publication. High-risk security behavior is tested before publication.
No parallel mutation of shared model/link-store files. Review standards/spec in
independent agents according to agent-loop after the implementation gates pass.

## Open PR ownership (observed 2026-10-10)

| PR | Owned files relevant to this issue | Coordination |
| --- | --- | --- |
| [#108](https://github.com/larchanka-training/dmc-268-api-t6/pull/108) | SYSTEM_DESIGN.md, TEST_PLAN.md, PIPELINE_SPEC.md | Comment before editing SYSTEM_DESIGN; avoid unrelated docs |
| [#122](https://github.com/larchanka-training/dmc-268-api-t6/pull/122) | SYSTEM_DESIGN.md, app/main.py | Comment before editing SYSTEM_DESIGN; no main.py change planned |
| [#123](https://github.com/larchanka-training/dmc-268-api-t6/pull/123) | run_repository.py, list_runs.py, main.py and related tests | Reuse shared predicate; no direct edits planned |
| [#121](https://github.com/larchanka-training/dmc-268-api-t6/pull/121) | github_installation_dispatch.py, webhook_runs.py, WEBHOOK_WORKER.md and related tests | Prefer resolver/sync guard/new feature test; comment if edits required |
| #124 | WEBHOOK_WORKER.md and eval files | Avoid these files |
| #125 / #126 | deployment files and their plans/tests | No overlap planned |

Neither new #113 plan file is owned by an open PR. This table is the planning-time
inventory; the authorized refreshed ownership check and coordination comments are
recorded below in Implementation base and ownership confirmation.

## Verification and evidence

TDD: red test → minimal implementation → refactor for each slice. Real migrated
PostgreSQL, fake GitHub HTTP, no real installation/token needed. Canonical scenario:
seed OAuth-verified account 700/installation 17 with Workspace, paused repo 101,
custom settings/rules and Run/PR; process deleted delivery → created delivery for
installation 29 → OAuth reconfirmation of 29/account 700/repo 101 → deferred replay.
Assert the original UUIDs/settings/rules/history survive; portal denies all prior
data before OAuth, pausing survives afterward, and no grants are copied to user B.
Include unpaused baseline, missing repo selection, cross-account 701, login rename,
legacy missing identity, duplicate receipts, concurrent users/callbacks, a target
removal racing OAuth, old source removal replay and retired queued Run.

Required gates: `uv run ruff check .`, `uv run ruff format --check .`,
`uv run mypy .`, `uv run pytest`. PostgreSQL proof must actually run, never count a
skip as AC evidence. See `docs/TEST_PLAN.md`: start postgres/rabbitmq with compose,
set `TEST_DATABASE_URL` and dedicated `TEST_RABBITMQ_URL`, and run
`uv run pytest -rs` with zero skips, including every marked integration case.
`uv run pytest --collect-only -m integration -q` records the included integration
count; no second standalone subset execution is required. Record exact tests,
commands and results in PR, including migration upgrade check. No new dependencies
are expected. No UI repository changes required.

## Risks and remaining development verification

| Risk | Mitigation |
| --- | --- |
| Login reuse/account transfer leaks history | Verified numeric account identity plus fresh per-user repo grants; fail closed for legacy/ambiguity |
| Created replay clears pause | Availability separate from user preference; literal API and DB assertions |
| Old source receipt/run mutates new lineage | Immutable installation rows, retired guards, FK-scoped revocations, queued/publishing tests |
| OAuth/removal or cross-user creation races | Existing generations/timestamps plus account lock and target tombstone recheck; PG concurrency tests |
| Legacy flags/identity cannot be reconstructed | Conservative paused/clean-slate fallback explicitly included in approval |
| Existing PR owns documentation | Explicitly authorized comments before edits, refresh ownership inventory |

Development must enumerate every existing `Repository.enabled` consumer and verify
worker publication/retry paths before deciding the smallest guard adapter. Check
existing VCS snapshot fields before adding any Run column: prefer stored message or
snapshot identity where already durable. Avoid schema/queue contract expansion
unless the present code cannot distinguish old work safely; report a material
contract expansion for approval instead of silently implementing it.

The tech lead accepted the complete preservation contract and its explicit
legacy/transfer fallbacks on 2026-10-10. Local stack
availability, migration number, and exact minimal worker file set are execution
checks, not unanswered product choices. Final PR uses What / Why / How to verify /
Refs, references #113 without a closing keyword or Development-panel link, rebases
on main, and waits for current-head approval and resolved review threads. Tech lead
accepts the AC and closes the issue; publication does not imply merge authorization.

## Implementation base and ownership confirmation (2026-10-10)

Implementation branch: `feat/113-app-reinstall`, refreshed `origin/main` base
`8cf3608d80d0d1cf79e56e333b50d6a4d2052ab8`. Isolated worktree excludes #111 and
unrelated #117/#129 plans. No open PR references #113 or reinstall.
Authorized coordination comments: [#108](https://github.com/larchanka-training/dmc-268-api-t6/pull/108#issuecomment-6099161965),
[#127](https://github.com/larchanka-training/dmc-268-api-t6/pull/127#issuecomment-6099162456),
[#131](https://github.com/larchanka-training/dmc-268-api-t6/pull/131#issuecomment-6099162883),
and [#121](https://github.com/larchanka-training/dmc-268-api-t6/pull/121#issuecomment-6099163285).
#122/#123 are no longer open; #124/#125/#126 files remain outside this scope.

## Effective-enabled reader audit (Task 4, 2026-10-10)

Task 3 established API `enabled = enabled AND installation_available` and
installation lifecycle writes change only availability. `PATCH enabled` remains
preference-only; grant-based repository visibility remains unchanged.

| Persisted preference consumer | Required effective behavior | Slice |
| --- | --- | --- |
| `repository_settings.py` `_to_settings` | API list/detail/PATCH reads effective enabled | Task 3 complete |
| `installation_repository_unit_of_work.py` | Onboarding/removal write availability and preserve preference | Task 3 complete |
| `ci_eligibility_candidates.py` `get` | Detached current-head CI snapshot uses effective eligibility before GitHub | 4a |
| `webhook_runs.py` `lock_candidate`, `pending_messages` | Locked trigger snapshot and pending publication selection use effective eligibility | 4a |
| `github_pull_request_projection.py` `get_or_create_locked` | PR projection refuses a paused/unavailable repository with the existing disabled outcome | 4a |
| `no_ci_sweep_candidates.py` `list_due` | Sweep omits paused/unavailable repositories | 4a |
| `run_lifecycle_store.py` `lock_for_claim` | Worker guard receives effective eligibility | 4b complete |
| `rerun_store.py` `create_rerun` | Reuses effective locked candidate and refuses paused/unavailable through existing conflict/409 | 4b complete |

Application readers consume `repository_enabled` snapshots: CI precheck,
webhook candidate comparison and worker guard retain their existing semantics
once adapters provide the effective value. No authorization predicate uses the
preference flag. Model column declarations and PATCH preference writes intentionally
remain unchanged. Six review adapters require changes, so Task 4 is split into
four-adapter 4a and worker/rerun 4b with independent checkpoints; the approved
product contract is unchanged. Old-installation routing and durable Run identity
remain the separate Tasks 6–7 audit and guards.


## Fresh OAuth rebinding audit (Task 5, 2026-10-10)

The apply transaction keeps the user generation lock first, then takes all known
account transaction locks in numeric order and all relevant source/target
installation rows in external-ID order, including missing-account snapshots.
Rebinding requires numeric evidence in the current snapshot; a stored target owner
from another callback never substitutes for it. A deleted legacy row cannot acquire
source evidence from late OAuth. Existing target repositories/Workspaces are retained
on uniqueness collisions or Workspace mismatch; archives are never merged.

The focused flush-only adapter preserves child rows by moving only the confirmed
Repository FK and availability. Existing revocation timestamps filter the current
snapshot before either rebind or grants. PG tests use terminal historical Runs here;
retired positive webhook locking is Task 6 and old nonterminal Run cancellation and
provider/publication guards remain Task 7. No Run or queue schema expansion.

The preservation HTTP fixture uses the API-supported fast engine; a separate PG
partial-selection test proves an existing deep setting survives unchanged.

Workspace ambiguity counts all known-account deleted sources, including empty
Workspaces. A second empty Workspace forces a clean target; a unique empty source
retains its Workspace/budget, while newly onboarded repositories get fresh default
identity because no archived Repository exists. Duplicate eligible Repository
checks remain separate. Both branches are explicit PG RED/GREEN controls.


## Retirement and provider-work audit (Tasks 6–7, 2026-10-10)

Task6a uses a required application predicate, wired to a short Sync preflight UOW,
before onboarding providers, queued repository starts and subsequent phases. The
positive write transaction checks the same installation row under NO KEY UPDATE
lock before any upsert; whole deletion holds that lock and atomically records the
permanent marker. Retired positive onboarding returns the existing empty result;
no new receipt status. Whole-deleted same external IDs cannot regain source grants
via later OAuth; partial repository removal remains reconfirmable.

Task6b1 adds a mandatory projection-store source check in a closed preflight UOW
before current/timeline reads and between phases. The apply store locks the source
installation before CodeChange and holds it through save/cancellation/commit.
Known retired sources keep the existing disabled/other-installation outcomes, and
unknown sources keep unknown-repository; stale source reads cannot cancel rebound
Runs. Standalone fake stores explicitly declare active; no optional bypass. Trigger target lookup closes its transaction before
calling the enqueuer; receipt installation identity must survive that gap and CI/Run
insertion checks to prevent old-source work using a newly rebound target (6b2).

Cancellation of nonterminal Runs alone is insufficient: terminal redelivery's
check_run_report and VCS inputs reconstruct the current Repository installation.
Task7a will pin a durable original installation association before rebind and
populate both Run creation paths; Task7b will guard provider work against a retired
or mismatched origin. This necessary internal persistence is within the approved
stale-work contract and was approved by the coordinator; no public/queue change.
Historical portal access keeps the current Repository grant predicate, and
terminal history payload/state remains preserved. No timestamp-based inference.


## Trigger retirement and lock-order checkpoint (Task 6b2a, 2026-10-10)

Permanent deleted_at participates in both detached CI eligibility and the locked
Run candidate independently of mutable Repository flags. Locked enqueue reads and
locks the current source installation before CodeChange, then retains it through
insert/commit. A source read which no longer matches the Repository FK refuses the
candidate. Rerun takes that same source-first path before its existing PR/active
checks; closed/active conflict still takes precedence over missing configuration.
A deterministic PG barrier reproduces the former PI ↔ PR lock cycle using NOWAIT,
then proves normal creation after the reorder. Network CI runs after its read UOW
closes. Receipt-origin propagation across the target/enqueue gap remains 6b2b;
original historical/queued Run provider identity remains Task7, not claimed here.


## Receipt origin checkpoint (Task 6b2b, 2026-10-10)

Receipt target reads lock/check the external source installation before any PR read
or CI-status write. The internal receipt enqueuer port requires that source ID;
closed target UOWs pass it through detached CI eligibility and final locked enqueue.
Both detached snapshots refuse a different current installation before/after CI;
locked insertion independently compares the expected origin even when an eligibility
port returns a current target. Trusted current-state sweeps retain the optional
internal default, while receipt dispatch has no optional source bypass. CI calls
run outside transactions; an already-started source read may finish, but cannot
create a new-installation Run. PG tests cover always and never CI modes and normal
fresh events. The worker's unavailable eligibility fallback only adopts the keyword
signature and keeps its existing RuntimeError behavior.

The same production durable receiver/resolver/onboarding composition defers new29
created before fresh OAuth, then OAuth wakes its saved receipt. Replay preserves
the archived exact Repository/active rules/settings/pause; created alone makes no
provider call or target installation row. Task8 still owns the full signed HTTP
webhook and portal authorization journey; historical Run provider guards are Task7.


## Original Run persistence checkpoint (Task 7a1, 2026-10-10)

A nullable internal Run.origin_provider_installation_id UUID FK is pinned by
new0030 before any Repository FK change. Migration backfills both terminal and
nonterminal historical Runs through their original CodeChange/Repository. The
fresh OAuth rebinding adapter pins any remaining NULL source histories immediately
before moving a confirmed Repository, retaining existing origins and unrelated
histories. This origin-only update retains updated_at explicitly so SQLAlchemy's
onupdate cannot alter historical payload metadata. Whole Run JSON equality proves
all other persisted fields/state stay unchanged. PG downgrade0029→upgrade0030 and
fresh upgrade/base downgrade metadata parity are verified. The nullable legacy
case is intentionally explicit; Task7b fails it closed rather than inferring after
rebind. Locked candidate/writer population is the separate Task7a2 checkpoint.


## Locked Run creation origin (Task 7a2, 2026-10-10)

The mandatory internal RunInsertCandidate carries the immutable physical
installation UUID read under the source installation lock. Both webhook Core
insert and rerun ORM creation store that explicit origin. PG controls exercise
both application use cases before and after same-account17→29 rebind, proving
origin matches the locked physical source and the existing message external ID.
PendingRunMessage/StoredRunMessage and public Run DTOs gain no fields. Runtime
retirement/mismatch guards remain the next checkpoint.

The request audit additionally identified onboarding token→details/tree/label HTTP
requests (including label pacing) which need rechecks between inner requests.
An already-started token exchange may finish, but cannot authorize a new subsequent
request after retirement. This will be proved in a bounded provider-safety follow-up
with Run source blob loops/VCS pagination and publication retries similarly audited.


## Onboarding inner-request audit repair (Task 6c, 2026-10-10)

Root's token/pacing audit proved phase guards alone were insufficient: a token POST
started live could finish after retirement, then an unguarded details/tree GET or
label POST would begin. A required application per-call checkpoint is now passed
through all three onboarding provider ports, bound to the existing physical source
UUID predicate. HTTP adapters check before token and before the actual API request;
label creation checks again after pacing. Each predicate UOW fully closes before
network/sleep; retirement is the existing skipped/no-op result. Real token HTTP
and label pacing races reproduce four violations before the fix, and four active
controls prove normal behavior. No optional production checkpoint bypass or new
receipt status. PR current/timeline token/pagination checks remain Task6d; Run
provider/check-run/publication request guards remain Task7b.


## CI inner-request review repair (Task 6e, 2026-10-10)

The cumulative Spec review after6c found a P2: the CI adapter still started check
pages/status after retirement during token minting or a prior page. A required
internal checkpoint now rereads a detached eligibility candidate before token,
every check-suite page and commit status, pinning the initial source installation
and head even for sweep without an explicit receipt hint. Refusal returns existing
ineligible reasons (disabled_repository, stale_head, stale_state); no public/queue
contract changes. Every read closes before the HTTP call. Deterministic real token
and paginated HTTP controls cover deletion, head change and FK rebind, while active
baselines enqueue normally and refused paths leave Runs and CI history unchanged.
Two production files: determine_ci_eligibility.py, github_ci.py. Apply this review
repair before the approved PR current/timeline inner-request slice6d.


## PR inner-request audit repair (Task 6d, 2026-10-10)

Current PR and reviewer timeline adapters now require the source checkpoint at
actual request boundaries: before token, before current GET and every timeline
page. The callback holds the existing installation lock only in a closed read UOW,
then returns existing repository refusal statuses through the projector. Real token
HTTP races exercise synchronize, label, timeline token and timeline pagination.
Refused paths preserve complete PR/Run JSON; active controls retain normal metadata,
lifecycle projection and cancellation of obsolete work. This supplements verified
phase/apply locks, without a new receipt status or public/queue field. Shared token
mint/cache singleflight is unchanged; a token operation already entered live may
finish, then the adapter rechecks before its next GitHub API request.


## Atomic source Run cancellation before rebind (Task 7b1, 2026-10-10)

After NULL origins are pinned while the old FK is still known, rebind cancels only
queued/running/publishing Runs whose immutable origin matches that source. The
source installation → Repository → Run locks stay held through FK move and state
notification; no provider call occurs in the transaction. Internal error_code is
installation_replaced, lease is cleared, finished_at recorded. Terminal rows and
independent valid target29 work are unchanged. Late worker finish/requeue, publisher
finalization and StoreReviewOutput are fenced by existing locked state predicates,
so an already in-flight old answer cannot revive the cancelled Run or overwrite its
payload/children. Dedicated PG controls prove the lock order and concurrent late
finish, while fresh confirmer history remains readable through current grants.
The previous7a NULL queued pin proof now allows only the explicit cancellation
lifecycle fields; historical payload equality and immutable origins remain checked.
Runtime provider/origin guards still belong to subsequent7b checkpoints.


Task7b2a runtime checkpoint: provider-facing projections use the immutable Run
origin join and require that physical installation to remain live, current for the
Repository and available. Claim acquires source installation, Repository, then Run
locks and re-reads current metadata after waiting. Inactive queued deliveries use
the existing skipped/repo_disabled outcome and terminal redelivery has no check-run
target. Portal history still uses current Repository grants. Shared positive
fixtures now seed explicit physical origins; no runtime inference for NULL exists.
Request-level publication/VCS and final LLM transport retry/fallback checkpoints
remain mandatory bounded follow-ups; an outer phase checkpoint is insufficient.


Task7b2b publication checkpoint: required Run-bound callbacks read immutable-origin
activity in a fully closed UOW before token and every subsequent HTTP call, including
lookup/create/PATCH, review marker/head/POST/comments, coordinates fallback and
retries. Inactive PublishContext carries no installation target and terminalizes
publishing before stale-pointer or unavailable-gateway early returns; active cases
retain no-op behavior. Finalization acquires original installation, Repository,
then Run locks and retains its terminal-state fence. Retired PUBLISHING cancels
without changing Finding/Comment payloads, including whole deletion before rebind
and a last already-started request completing before the final DB write.


Task7b3 implements the required Run-bound checkpoint at VCS token operations,
each paged file GET, final PR refetch, tree GET and blob stream entry. Cached
Run metadata remains immutable, while each actual request rereads original
installation eligibility in a closed UOW. Best-effort classification/cache
catches propagate RunCancelled. Production worker composition supplies these
callbacks through the processor and GitHubRunSource; portal file-line reads
are authorized cache-only reads and have no GitHub fallback.


Task7b3 review found a final-response persistence race: StoreDiffSnapshot could
mutate an already cancelled old-origin Run and append diff children after fresh
OAuth rebind. The authorized one-source repair locks origin installation,
Repository and Run in that order, refreshes persisted identity/lifecycle and
requires active original origin plus RUNNING before mutation. Terminal history
reads remain available through current grants; producer storage retries cannot
change historical terminal snapshots. This is separate from permitted append-only
audit/paid usage for operations whose provider requests had already started.


Task7b4 binds each LLM operation to its RunCallContext and rereads original-origin
activity and cancellation in a closed UOW before pricing and after awaited FX/
ledger reads. The required callback reaches every actual transport key POST, so
retry, fallback, JSON repair and key rotation cannot start the next request after
retirement. RunCancelled propagates outside transport error redaction/retry policy.
Already-started paid answers retain usage and raw append-only audit; production
build_gateway always supplies the persisted guard, while the documented separate
database-free eval/manual composition explicitly trusts its standalone context.


Task7b4 review repairs are mandatory within the same checkpoint: the actual
per-request LLM callback rechecks that a complete call timeout remains after its
closed-UOW origin read, including rotated keys. Ordinary ContextPayload writes
are durable review inputs rather than append-only provider audit. Their UPSERT
acquires original installation, Repository and Run locks, refreshes persisted
identity/state and requires active origin plus RUNNING, so an already-started
paid conventions answer can settle usage/audit but cannot insert or overwrite
terminal context history after deletion or fresh OAuth replacement.


Task8 public verification composes real signed durable delivery transport and
replay, actual RSA bearer verification and OAuth/refresh routes over migrated PG.
User-specific fresh callbacks establish account provenance and grant boundaries;
same-account preservation and cross-account clean-target controls share the actual
chronology. Cached historical file/action reads stay under current grants and
perform no provider fallback. Tests-only Task8 and final documentation share one
final mandatory full gate with all configured integration cases before publication.


## Final technical handoff

Pinned implementation base: `8cf3608d80d0d1cf79e56e333b50d6a4d2052ab8` on
`feat/113-app-reinstall`. Final whole-feature independent Standards0/Spec0 reviews
have no feature exclusions. Configured final `uv run pytest -rs`:2712 passed,
zero skips,330.91 s;409 marked integration cases were counted by collection and
included in that full run. Ruff check passed, format368 files, mypy365 files.
Detailed executed migration/public-route/race evidence is in `113-todo.md`.
Technical implementation/documentation/verification is complete. Commit, push,
PR creation/attachment remain publisher actions; current-head human approval,
resolved review threads, tech-lead AC acceptance, board12 and issue closure remain
external handoff steps. No merge or issue auto-close is inferred.
