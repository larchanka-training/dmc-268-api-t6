# Task List: GitHub App reinstall (#113)

**Status:** contract approved 2026-10-10; Tasks 1–5, Task6 durable/phase checkpoints and Task7a complete and reviewed; Task6c onboarding inner-request complete; Task6e CI repair complete; Task6d PR requests complete; Task7b1 rebind cancellation complete and reviewed; Task7b2a origin projections complete and reviewed; Task7b2b publication requests complete and reviewed; Task7b3 VCS requests and atomic persistence repair complete and reviewed; Task7b4 LLM/context guards complete and reviewed; Task8 public HTTP proof complete and reviewed; Task9 technical documentation, final gates and independent reviews complete; publication and human acceptance pending. Read `113-plan.md` for the complete
contract. Every implementation task uses TDD, literal spec assertions, no network
inside DB transactions, flush-only repositories and use-case commits.

## Task 1: Approve the contract and establish the implementation base

**Description:** Tech lead selects same-account preservation or clean slate. The
proposal is same-account preservation with the legacy/transfer fallbacks in the
plan. Root agent handles approval and ownership coordination before implementation.

**Acceptance criteria:**

- [x] Human approves preservation, numeric-account proof, per-user reconfirmation,
  preserved pause and explicit cross-account/legacy/ambiguity clean-slate behavior.
- [x] Explicit permission to post ownership coordination comments exists; relevant
  still-open PRs receive comments before owned files change.
- [x] Work begins on `feat/113-app-reinstall` from refreshed main without #111 changes.

**Verification:** Review the approved contract; recheck branch/base and open PR file
ownership. Do not claim product behavior complete at this checkpoint.
**Dependencies:** None. **Files likely touched:** `docs/SYSTEM_DESIGN.md` (§6.9).
**Estimated scope:** Small.

## Task 2: Persist verified numeric account identity

**Description:** Carry stable positive account ID from authenticated GitHub listing
through GitHubInstallation to persistence. Add conservative identity/lifecycle
schema foundations needed by subsequent paths; preserve nullable legacy identity.

**Acceptance criteria:**

- [x] `account.id` is strictly validated as positive PostgreSQL BIGINT; missing or
  malformed evidence cannot authorize rebind; login rename does not change identity.
- [x] New/known exact installations persist numeric account ID without rewriting
  an established different ID; old login-only rows are not guessed/backfilled.
- [x] New migration adds account ID/deletion/availability state with explicit
  constraints/indexes and safe existing-row values; upgrade works on PostgreSQL.

**Verification:** Adapter/unit tests in `test_installation_linking.py`; fresh and
existing-schema migration tests. Run targeted tests with `uv run pytest ...`.
**Dependencies:** Task 1.
**Files likely touched:** `link_github_installations.py`, `github_user_installations.py`,
`github_installation_links.py`, repositories `models.py`, new Alembic revision.
**Estimated scope:** Medium (5 implementation files; tests follow same feature).

**Task 2 evidence (2026-10-10):** HTTP RED 8 failed → GREEN 8 passed;
use-case RED 7 failed → GREEN 7 passed; migrated-schema RED 2 failed → GREEN
4 passed including fresh upgrade/downgrade and metadata parity; link persistence
RED 2 failed → GREEN feature suite 33 passed. Task 1 gates: Ruff lint/format,
mypy (344 files), configured full pytest **2412 passed, zero skips** (156.21s).
Task 2 gates: Ruff lint/format, mypy (346 files), configured full pytest
**2431 passed, zero skips** (189.81s). Both independent review axes reported
zero findings. No dependencies added; no commit/push. Migration revision:
`20261010_0029` over `20261007_0028`.

## Task 3: Preserve user pause across App removal and onboarding

**Description:** Separate user preference from availability in persistence and API
mapping, so revoked access stops reviews while replay cannot clear a manual pause.

**Acceptance criteria:**

- [x] Removal sets availability false/revokes grants without changing preference;
  upsert restores availability but preserves preference and existing active rules.
- [x] GET/PATCH effective `enabled` remains the public contract; paused visible
  repositories remain readable and PATCH only changes preference.
- [x] Legacy false preference remains false; all normal created/added repeat paths
  preserve it, and new repositories default to enabled preference.

**Verification:** Installation-sync unit tests and repository API PostgreSQL tests,
including PATCH false → added/created repeat → GET false and grant retention.
**Dependencies:** Task 2.
**Files likely touched:** `installation_repository_unit_of_work.py`,
`repository_settings.py`, `test_installation_repository_sync.py`,
`test_repositories_api_postgres.py`.
**Estimated scope:** Medium.

**Task 3 evidence (2026-10-10):** effective API mapping RED 1 failed → GREEN
2 passed; persisted preference/availability RED 5 failed → GREEN targeted
API/sync/portal-scope suite **31 passed** (16.62s). Old removal tests now assert
availability loss while preserving preference, backed by public API/PG tests for
paused/unpaused removals, repeated onboarding, rules/settings and grant retention.
All four gates pass: Ruff lint/format, mypy (346 files), configured full pytest
**2437 passed, zero skips** (184.60s). Both independent review axes: zero findings.

## Task 4: Apply effective review eligibility in worker paths

**Description:** Audit current enabled consumers and apply preference AND
availability before trigger, rerun and worker execution without changing access.

**Acceptance criteria:**

- [x] No trigger/rerun/worker starts review for unavailable or paused repositories.
- [x] Available unpaused repositories retain normal behavior; portal visibility
  still uses existing grants and shared access predicate.
- [x] Audit lists every enabled read and explains unchanged readers; no queue or
  public API schema expansion without necessity.

**Verification:** Targeted trigger/worker/rerun tests; then all four backend gates.
**Dependencies:** Task 3.
**Files likely touched:** `webhook_runs.py` (coordinate #121 if still open),
`run_lifecycle_store.py`, `rerun_store.py`, focused new eligibility test file.
**Estimated scope:** Medium; split if audit needs >5 implementation files.

### Task 4a: Trigger, projection, CI and sweep eligibility

- [x] Apply preference AND availability in detached CI and locked trigger snapshots,
  PR projection, pending webhook publication and no-CI sweep selection.
- [x] Prove negative paused/unavailable controls and a normal enabled/available
  baseline at public PG/use-case boundaries; no GitHub CI call or Run publication
  for an unavailable repository.
- [x] Run all four gates and independent review before Task 4b.

Implementation files: `webhook_runs.py`, `ci_eligibility_candidates.py`,
`github_pull_request_projection.py`, `no_ci_sweep_candidates.py` (four adapters).

**Task 4a evidence (2026-10-10):** CI trigger, locked trigger/pending publication,
sweep and PR projection each had the unavailable/preference=true RED control
before minimal effective-eligibility changes. New PG matrix suite **16 passed,
zero skips** (10.47s); denied trigger verifies no CI call, no publication and no
new Run. All four gates pass: Ruff lint/format, mypy (347 files), configured full
pytest **2453 passed, zero skips** (270.24s). Both independent review axes:
zero findings. Four read adapters changed; worker/rerun remain slice 4b.

### Task 4b: Worker and user rerun eligibility

- [x] Apply effective preference to worker claim snapshots and refuse paused or
  unavailable rerun via the existing 409 conflict outcome.
- [x] Keep visibility grants unchanged; explain the additional conflict reason
  accurately and verify the normal worker/rerun baseline.
- [x] Run all four gates and independent review; then complete Task 4 above.

Implementation files: `run_lifecycle_store.py`, `rerun_store.py`; existing 409
explanation updated in `rerun_run.py`, `app/main.py` and `contracts/openapi.yaml`.
Five source files total; no queue/API schema expansion.

**Task 4b evidence (2026-10-10):** Rerun HTTP/PG matrix RED **3 failed,
1 passed**: paused/unavailable repositories incorrectly returned 202; minimal
existing-CONFLICT guard GREEN **4 passed**. Worker PG matrix RED **1 failed,
3 passed**: unavailable repository invoked the pipeline; effective snapshot
mapping GREEN. Combined eligibility suite **24 passed, zero skips** (11.65s),
including readable history/grants, no denied Run creation or pipeline/retry call,
and normal baseline. Initial fixture/import errors were corrected and not counted
as behavioral RED. All four gates pass: Ruff lint/format (350 files), mypy
(347 files), full configured pytest **2461 passed, zero skips** (169.27s).
Both independent review axes: zero findings.


## Checkpoint: Identity and pause (Tasks 2–4)

- [x] Schema upgrade and targeted PostgreSQL tests pass without skips.
- [x] All four backend gates pass; #101 removal guarantees remain intact.

## Task 5: Rebind same-account Repository in fresh OAuth application

**Description:** A focused adapter selects unambiguous known deleted source lineage,
reuses Workspace, creates immutable new installation row and rebinds only repositories
confirmed by current user OAuth, together with that user's grants.

**Acceptance criteria:**

- [x] Same verified account/repo preserves Repository and Workspace UUIDs, all
  settings/history/rules/conventions; unlisted repositories remain inaccessible.
- [x] Different account, unknown legacy evidence and ambiguous archive take clean
  target identity; no old data is copied and no other user's new grants are created.
- [x] Account lock, deterministic installation locks, snapshot generation and target
  revocation checks make concurrent applies atomic/idempotent; network stays outside.

**Verification:** Focused unit tests plus PG same-account/cross-account/concurrent
callback tests. Mock HTTP verifies complete pagination before apply.
**Dependencies:** Task 4.
**Files likely touched:** `github_installation_links.py`,
`link_github_installations.py`, new `github_repository_rebinding.py`,
`test_installation_linking.py`, new `test_app_reinstall_postgres.py`.
**Estimated scope:** Medium.

**Task 5 evidence (2026-10-10):** Same-account preservation RED1 (different
Workspace) → GREEN with immutable new installation and confirmed Repository FK
rebind; late OAuth/deleted legacy evidence RED1 (NULL promoted to700) → GREEN
without manufacturing archived identity. Empty second Workspace and unique empty
source Workspace refinement RED2 → GREEN2; all deleted source Workspaces count.
Feature PG suite **17 passed, zero skips** (13.64s), including exact child/settings/
pause/budget preservation, stale second-user denial, cross-account/legacy/missing
current evidence/ambiguity/active-source negatives, partial selection and deep
setting, same/cross-user callbacks, target removal after snapshot reservation and
existing-target collision/Workspace refusal. Existing linking/generation/pagination
coverage retained. Final four gates: Ruff lint/format (352 files), mypy (349 files),
configured full pytest **2478 passed, zero skips** (192.01s). Independent standards/
spec reviews and refined helper/test rechecks both zero findings. Source positive
webhook phantom safety remains Task6; nonterminal/terminal provider work Task7.

## Task 6: Reject retired installation event work

**Description:** Mark deleted installations durably and prevent old created/added
or PR/CI receipts from onboarding/reviving source lineage after rebind. Keep source
removal effects scoped to source rows.

**Acceptance criteria:**

- [x] Deleted events commit retired state with grants/revocations/effect marker;
  duplicate delivery preserves later fresh grants.
- [x] Old positive receipts make no GitHub onboarding calls after retirement;
  in-flight onboarding cannot save after delete commits; old removals cannot
  mutate new installation visibility/availability.
- [x] New-installation created receipt remains deferred before OAuth and resumes
  safely afterward without overwriting rules/pause.

**Verification:** Event/resolver fakes count HTTP calls; PostgreSQL tests interleave
delete and positive projection, then distinct/duplicate source removal replay.
**Dependencies:** Task 5.
**Files likely touched:** `github_installation_resolver.py`,
`installation_repository_unit_of_work.py`, `sync_installation_repositories.py`,
`test_app_reinstall_postgres.py`, focused new retired-installation event tests.
**Estimated scope:** Medium. Coordinate #121 before dispatch edits if required.

### Task 6a: Permanent retirement and onboarding boundaries

- [x] Persist deleted_at with whole-removal grants/tombstones/effect markers.
- [x] Require the active predicate before provider work and between phases; close
  its read UOW before network calls. Recheck and hold the installation row lock
  throughout the short positive-write transaction.
- [x] Prove retired no-provider/no-write behavior, in-flight retirement, original
  delivery dedup and distinct old removals preserving the rebound target.
- [x] Run all four gates and fresh independent review.

Four source files: `installation_repository_unit_of_work.py`,
`sync_installation_repositories.py`, `installation_event_projector.py`,
`app/bootstrap/installation_onboarding.py`. Standalone projector unit tests use an
explicit always-active predicate; production wiring has no silent bypass.

Evidence: meaningful RED retired provider/write/in-flight controls → GREEN; full
configured suite 2487 passed, zero skips (182.66 s), Ruff check/format and mypy
passed. Spec review 0 findings; Standards P3 required helper future import fixed,
static gates and all 79 helper consumers passed afterward.

### Task 6b1: Retired PR and label projection

- [x] Reject retired source before current/timeline GitHub reads; lock/check source
  installation during the projection write transaction to serialize rebind/delete.
- [x] Prove old source PR/label receipts do not mutate rebound history or call GitHub,
  including in-flight reads; retain fresh positive and current receipt outcomes.
- [x] Freeze, all four gates and fresh review before trigger slice.

Evidence: valid RED 5 failed / 5 passed (retired sync/closed/label, next-phase
read, absent installation write lock); two production files now require the port.
Targeted PG and existing projection/webhook controls: 106 passed (see gate record
when complete). Active and already-rebound/in-flight source paths are distinct.
Initial full gate: 2498 passed / 1 failed (unlinked other-installation detail).
The existing refusal classification now runs for a missing source row too; literal
known-other vs truly-unknown PG controls were added, retaining no-provider behavior.
All 26 retirement/production-trigger controls and three static gates passed after
repair; the full configured gate is repeated before completion.

Final evidence: 2501 passed, zero skips (182.03 s); Ruff check/format (355
files), mypy (352 files) passed. Standards and Spec repair rechecks: 0 findings.

### Task 6c: Onboarding inner HTTP request boundaries (audit follow-up)

- [x] Required per-call provider checkpoint invokes the existing physical-source
  active predicate in a closed UOW before token and before API requests, including
  after label pacing. Retirement keeps the existing skipped/no-op outcome.
- [x] Deterministic real token-request retirement and pacing controls prove no
  subsequent GET/POST; active installations retain normal onboarding.
- [x] All four gates and fresh onboarding review; cumulative CI finding repaired next.

Root's request audit found that phase preflights alone permit a next HTTP request
if deletion occurs during token minting or pacing. Prior phase checkpoint results
remain verified; overall source-event isolation needs these explicit follow-ups.
Four source files: installation_event_projector and details/tree/label adapters.
No ownership overlap for the three adapters or six direct test consumers.

Evidence: meaningful RED4 next-request violations /4 active baselines passed
→ GREEN8 with real token POST adapters, independent PG NOWAIT probes at token/API
and pacing boundaries, no new Repository/RuleVersion/Run after retirement. All18
retirement/deferred-provider PG tests passed (10.32 s); 449 directHTTP/projector/
onboarding/token/durable receipt consumers passed (19.47 s). Four production
files; all checkpoint ports required, unit adapters use explicit trusted no-op.
Full configured gate: 2543 passed, zero skips (205.27 s); Ruff check/format359
and mypy356 passed. Standards0 and onboarding Spec0. Cumulative Spec P2 found
the analogous CI token/page/status request gap; Task6e repairs it before Task6d.
No new receipt status.

### Task 6e: CI inner HTTP request boundaries (Spec P2 repair)

- [x] Required detached checkpoint pins initial source and head before token, each
  check-suite page and final commit status request; keep existing ineligible reasons.
- [x] Token/page/status retirement, head/source change and active controls prove no
  subsequent request, no Run/CI write and closed network UOW boundaries.
- [x] All four gates and fresh review/recheck before Task6d.

Evidence: meaningful RED5 next-request violations /3 active baselines passed
→ GREEN8. Source/head controls intentionally omit receipt hints so the immutable
initial candidate remains pinned during sweep. PG NOWAIT network probes pass;
refused paths preserve Run count4 and unchanged CI history. Static gates passed;
402 affected CI/trigger/durable/projection/PG consumers passed (42.43 s).
Ruff check/format359 and mypy356 passed; full configured gate: 2551 passed,
zero skips (277.07 s). Fresh Standards/Spec0 each; prior cumulative CI P2 resolved.

Two production files: determine_ci_eligibility.py and github_ci.py. Ownership
verified unowned; existing fake consumer ownership comments cover affected tests.

### Task 6d: PR current/timeline inner HTTP request boundaries

- [x] Required per-call source checkpoints stop next current-PR/timeline request
  after token minting or between timeline pages; reuse existing refusal outcomes.
- [x] Token/page retirement and active controls retain closed network UOW boundaries.
- [x] All four gates and fresh review before Run-origin runtime slices.

Evidence: valid RED4 next-request violations /4 normal controls passed (6.15 s)
→ GREEN8 (7.14 s), covering synchronize/label token→current GET, timeline token
and page→next page. Independent PG NOWAIT probes at actual HTTP boundaries prove
closed read UOWs. Refused PR/Run whole-row JSON stays equal; active projection still
cancels obsolete work and retains normal lifecycle metadata semantics. Three source
files: project_github_pull_request, github_current_pull_request, github_reviewer_timeline.
340 affected PR/durable/token/path consumers passed (18.81 s). Ruff
check/format359 and mypy356 passed; full configured gate: 2559 passed, zero
skips (400.70 s). Fresh Standards/Spec0 each.

### Task 6b2: Source identity through trigger eligibility

- [x] Carry receipt installation identity across the target-read → enqueuer gap;
  recheck before CI provider calls and locked Run insertion.
- [x] Prove deterministic read/rebind/enqueue race refuses old-source work, while
  legitimate new-installation events and sweep retain normal behavior.
- [x] Freeze, all four gates and fresh review; then complete Task 6 above.

#### Task 6b2a: Retirement and enqueue lock order

- [x] CI snapshots include permanent source retirement in eligibility; locked enqueue
  acquires the source installation before PR/Run locks and retains it through insert.
- [x] Rerun uses the same lock order, retaining conflict/not-configured precedence.
- [x] PG negative/positive controls prove no retired CI/Run work and atomic source
  lock; full gates and fresh review before receipt identity propagation.

Evidence: valid retirement/CI/source-lock RED 4 failed / 2 active baselines passed
→ GREEN 6. Deterministic rerun lock inversion RED raised PG 55P03 at the competing
source-first writer's PR lock; reorder fixes it without changing outcome precedence.
Twelve new PG controls plus existing CI/enqueue/rerun suites: 96 passed (29.91 s).
Source files: ci_eligibility_candidates.py, webhook_runs.py, rerun_store.py.
Full configured gate: 2513 passed, zero skips (188.27 s). Ruff check/format
(356 files), mypy (353 files) passed; fresh Standards/Spec each 0 findings.
Receipt identity remains Task 6b2b.

#### Task 6b2b: Receipt identity through eligibility and insertion

- [x] Thread expected external installation identity through target/CI/final locked
  enqueue; target CI writes hold the source lock too.
- [x] Deterministic target-read/rebind/enqueue, CI-observation and insertion races
  refuse source work; fresh target events/sweep remain normal.
- [x] Full gates and fresh review, then complete Task 6b2/Task 6.


Evidence: valid target-gap/retirement/lock RED 7 failed / 3 active baselines passed
→ GREEN; independent locked-insert origin RED 1 failed / 1 positive passed → GREEN.
Fifteen PG controls cover PR/label/CI × always/never CI policy, rebind during CI,
retired target reads/history writes, installation locks and independent final origin
refusal. Existing trigger/dispatch/CI/worker/sweep controls: 242 passed (44.94 s)
before the three additional never-policy controls; all 15 new origin cases passed.
Five source files: trigger_from_delivery.py, determine_ci_eligibility.py,
try_enqueue_webhook_run.py, webhook_run_targets.py, app/worker.py (signature-only
unavailable fallback compatibility). No queue or public API expansion.
A focused real receiver/resolver/dispatcher/onboarding PostgreSQL proof saves new29
created before OAuth, verifies deferred receipt/no provider/no target row, confirms
fresh same-account OAuth wakes it, and replays it preserving exact Repository,
active RuleVersion/payload, custom settings and pause. Full configured gate: 2529 passed, zero skips (198.35 s). Ruff check/format
(357 files), mypy (354 files) passed. Fresh Standards/Spec reviews: 0 findings each.

## Task 7: Guard original installation for Run provider work

**Description:** Preserve old history but stop old queued/retrying/publishing work
from reconstructing current targets under the new installation. Audit existing
durable VCS/message identity first, then use the smallest necessary guard.

**Acceptance criteria:**

- [x] Queued/retrying old work is terminally skipped/cancelled using existing
  semantics; it cannot fetch, invoke LLM or publish as the new installation.
- [x] Claimed/publishing work rechecks retired/mismatched source before subsequent
  provider calls; historical snapshots are not rewritten, new rerun uses new ID.
- [x] Tests distinguish old source messages from legitimate new-installation work.

**Verification:** Worker/publisher fakes assert no calls after replacement; targeted
PG worker test covers persisted queued Run and legitimate fresh rerun.
**Dependencies:** Task 6.
**Files likely touched:** `run_lifecycle_store.py`, `handle_review_run.py`,
`publish_run_review.py`, focused new `test_reinstalled_run_guard.py`,
`test_app_reinstall_postgres.py` (exact adapters to confirm in audit).
**Estimated scope:** Medium, split into persistence and runtime checkpoints.

### Task 7a: Durable original installation association

- [x] Add a necessary internal Run origin association in a new unused migration;
  backfill/pin original current association before any Repository FK move.
- [x] Populate webhook and rerun creation paths; prove migrated historical origin
  and newly created target origin. No public API or queue contract changes.
- [x] Freeze, all four gates and fresh review before runtime guards.

#### Task 7a1: Schema, backfill and pre-rebind pin

- [x] Model nullable immutable origin UUID, new0030 FK/backfill and PG migration
  upgrade/downgrade/metadata proof for terminal and nonterminal history.
- [x] Pin NULL source histories before Repository FK move; preserve all payloads
  and previously stored origins. No runtime inference after rebind.
- [x] Full gates and fresh review before writer propagation.

Evidence: valid missing-column/backfill RED → migration GREEN; then terminal/queued
NULL-origin pin RED → GREEN. Whole historical Run JSON equality caught automatic
updated_at mutation; the origin-only update now explicitly retains updated_at.
Existing nonNULL origins stay unchanged and unrelated histories remain NULL.
21 migration/metadata/full downgrade-base round-trip and preservation controls
passed (12.49 s). Full configured gate: 2531 passed, zero skips (201.21 s).
Test-only typed tuple view repair retains all assertions/runtime; 21 proof consumers
passed again (12.11 s), Ruff check/format359 and mypy356 passed. Fresh
Standards/Spec: 0 findings each. Three source files: reviews models,
new0030 migration, github_repository_rebinding. Writers/runtime remain7a2/7b.

#### Task 7a2: Locked candidate and production writers

- [x] Carry physical installation UUID from the locked Run candidate into both
  webhook and rerun writers; prove fresh source/target origins on PostgreSQL.
- [x] Full gates and fresh review before runtime guard slices.

Evidence: 4 meaningful writer REDs (webhook/rerun × active17/reinstalled29) created
NULL origins → GREEN with explicit locked physical UUID in both production paths.
97 writer/trigger/race/preservation consumers passed (23.90 s). Existing exact PG
candidate assertions retain full equality; their seed now uses a fixed literal
physical installation ID matching the fake contract. Three source files:
try_enqueue_webhook_run.py, webhook_runs.py, rerun_store.py; no queue/API fields.
Full configured gate: 2535 passed, zero skips (206.86 s). Ruff check/format359,
mypy356 passed; fresh Standards/Spec each 0 findings. Request-level token→API/onboarding checks belong to the
upcoming provider safety slices, in addition to Run-origin runtime guards.

### Task 7b1: Cancel source work atomically before rebind

- [x] Pin NULL origins, then cancel queued/running/publishing original-source Runs
  before Repository FK move; retain terminal whole-row history and target work.
- [x] Fresh OAuth/store PG proof verifies lifecycle-only changes and late finish,
  requeue/publication state fences; all four gates/fresh review.

Evidence: valid RED6 missing cancellation for three nonterminal states × explicit/
NULL origin (5.48 s) → GREEN6 (7.31 s). Strengthened PG controls prove source,
Repository and Run NOWAIT locks remain held before FK move; concurrently attempted
late finish returns false. Independent valid target29 running work and terminal
whole-row history stay unchanged. Late finish/requeue/publication plus valid late
StoreReviewOutput preserve all Run JSON and findings/actions/context payload JSON,
and enqueue no publication. Final six proofs passed (4.32 s); 124 other affected
preservation/worker/output controls passed. Ruff check/format359 and mypy356 pass;
full configured gate2565 passed, zero skips (216.79 s). Fresh Standards/Spec: 0 findings each.

One production source: github_repository_rebinding.py. Internal reason
installation_replaced; catalog documentation belongs to Task9. Locks retain the
source installation → Repository → Run order, no provider call in transaction.

### Task 7b2a: Original-installation projections and delivered Run guards

- [x] Fail closed for retired, rebound and NULL origins in claim, cancellation,
  check reports, VCS locators, queued/publishing and cancellation-signal projections.
- [x] Deliver old queued messages to a terminal skipped state, suppress terminal
  check-run redelivery, preserve current-grant portal history and active controls.
- [x] Full gates and fresh reviews before publication request checkpoints.

Evidence: valid RED15 with3 active controls (8.30 s) → GREEN18 (9.11 s).
Deterministic PG claim barriers prove source and Repository locks are held before
waiting on Run (NOWAIT55P03), replacement during source-lock wait refuses old
work, and a distinct manual target29 Run reaches both check-run and pipeline.
Completed historical whole Run JSON remains unchanged; queued old messages become
skipped/repo_disabled. Dedicated NULL negatives remain; NULL pin proof explicitly
clears fixture origins, while operational fixtures seed physical origins.
Affected118 worker/reconciler/VCS/portal/store consumers passed (37.68 s); final
50 strengthened origin/rebind/migration proofs passed (26.56 s). Ruff check/format361,
mypy358 pass; full configured gate2586 passed, zero skips (226.37 s). Fresh Standards/Spec each0 findings.

Five sources: shared run_origin predicate, run_lifecycle_store, run_repository,
github_pull_request_projection and removal of unused RunGuardSnapshot check target.
Operational fixture origins become explicit; dedicated NULL controls remain.
Next7b2b consolidates publish context and request checkpoints if wiring fits five
sources, otherwise uses bounded follow-up slices. VCS request guards remain pending.

### Task 7b2b: Publication and check-run request checkpoints

- [x] Required closed-UOW Run-origin callback before token and every check-run or
  review HTTP request, including retry/fallback/head/marker/comments boundaries.
- [x] Required origin flag and nullable inactive target; inactive publishing becomes
  cancelled/installation_unavailable before provider work or final DB payload write.
- [x] Finalization locks installation → Repository → Run and retains terminal fence;
  whole deletion without rebind and already-in-flight last requests have PG proof.
- [x] Full gates and fresh reviews before VCS request guards.

Evidence: actual token-inflight retirement RED2 with2 active controls (3.23 s)
→ GREEN4 (3.12 s). Expanded24 PG controls cover token, lookup→POST/PATCH,
review marker/head/POST/comments, retries/coordinates and rebind during POST;
all passed (15.28 s). Retirement after the last response before final DB write
cancels even without rebind, preserving exact finding JSON and no Comment insert.
Rebind during POST preserves installation_replaced and never starts target29 work.
Own early-return RED6 (3.78 s) caught inactive stale-pointer/missing-gateway runs
remaining publishing; origin-first cancellation repairs them. Final38 PG controls
(21.46 s) include NULL/mismatch/retired consumer/finalization plus two active exact
no-op stale/missing-gateway controls. 375 affected consumers passed (56.92 s),
including terminal-history and late-payload fences. Ruff check/format362,mypy359
pass; full configured pytest2624 passed (262.81 s), zero skips; Standards0/Spec0.

Five sources: run_lifecycle_store, handle_review_run, check_runs, publish_run_review,
github_review_publication. No public API or queue expansion. Later VCS five-source
slice and LLM actual-transport retry/fallback guard remain required.

### Task 7b3: VCS source request checkpoints

- [x] Required Run-bound callback before token, every PR/files/tree request,
  final PR refetch and blob stream entry; Classified wrapper forwards it.
- [x] Cached GitHubRunSource rechecks original identity for subsequent requests;
  NULL/missing/mismatch/retired input refuses provider work without inference.
- [x] RunCancelled propagates through both optional blob-classification/cache
  catches, suppressing later provider phases.
- [x] Full gates and fresh reviews before the final LLM request checkpoint slice.

Evidence: real token-inflight tree/blob retirement RED2 +2 active controls
(4.05 s) → GREEN4 (3.78 s). Expanded34 PG controls cover PR/diff/refetch tokens,
pagination, final refetch, tree→AGENTS blob, blob loops and cached source after
production fresh OAuth rebind17→29, retirement and NULL origin. Literal HTTP
prefixes and NOWAIT installation/Run locks prove no next request and closed UOW.
321 affected consumers passed (54.16 s), retaining complete pagination, durable
replay, immutable snapshot and optional-cache baselines. Ruff363/mypy360 passed; first full gate2658 passed (318.76 s), zero skips.
Review then found the final-response persistence P2 below, so this gate does not
complete Task7b3; the atomic repair and rechecks remain required.

Five production sources: vcs_diff, github_vcs, vcs_failures, process_run,
github_run_source. Production worker's VCS path receives the processor callback;
its GitHubRunSource composition receives the Run repository, so no alternate
installation-authenticated VCS path bypasses these required per-call checks.
Portal GetRunFileLines is cache-only: current-grant historical cache hit/miss
coverage remains Task8, with no provider fallback to add. LLM actual transport
retry/fallback/repair/key-rotation guards remain a separate required checkpoint.

### Task 7b3r: Atomic diff storage repair from review

- [x] Source installation → Repository → Run locks precede a refreshed active
  origin and RUNNING fence before snapshot marker/timestamps or child writes.
- [x] Already stored snapshots and whole terminal Run/payload/children remain
  immutable; actual final-response fresh OAuth/deletion and cached-state races
  have PostgreSQL proof, with active producer/idempotent controls.
- [x] Full four gates and same-reviewer Standards/Spec rechecks resolve the P2.

Review-found gap: a final PR refetch could complete after fresh OAuth cancelled
old-origin work; StoreDiffSnapshot then changed cancelled Run.diff_snapshotted_at,
automatic updated_at and inserted children. Valid RED2 +active1 (3.18 s) captured
whole-row/child differences for production freshOAuth17→29 and deletion plus
lifecycle finalization. Ordered origin/lifecycle fence GREEN5 (5.79 s) retains
revision-check precedence and raises existing RunCancelled before any mutation.
Expanded23 PG controls passed (16.83 s): all lifecycle states with/without stored
history, NULL/retired/mismatched origins, NOWAIT lock-order proof and replacement
while acquisition waits with a preloaded stale ORM Run (populate_existing).
Only two preexisting operational writers now seed claimed/RUNNING semantics;
portal historical fixtures remain succeeded. One additional source run_repository
is the authorized findings-first repair, giving cumulative six VCS sources.
Final affected374 consumers passed (54.82 s). Public-only bootstrap cleanup
retains20 source race controls (13.47 s); Ruff364/mypy361 pass. Full configured2681 passed (317.51 s), zero skips; Ruff364/mypy361
passed; same Standards0/Spec0 rechecks resolve the persistence P2.

### Task 7b4: LLM actual request guards (complete and reviewed)

- [x] Required context-bound gateway callback and per-call transport callback
  before every key POST, including retries/fallback/repair and after FX/ledger awaits.
- [x] Production build_gateway binds closed-UOW original Run origin/cancellation;
  missing/NULL/mismatch/retired fail closed, while standalone eval explicitly no-ops.
- [x] Active/retirement controls preserve deadlines and already-started paid usage
  and append-only trace, suppressing any next HTTP; all gates and fresh reviews.

Evidence so far: valid RED2 (retry/key continuation after retirement) plus active2
→ GREEN4. Essential real build_gateway/PG/actual-HTTP matrix19 passed (11.31 s):
retry/key rotation/fallback/JSON repair and active controls, missing/NULL/mismatched/
retired first-call refusals, retirement during FX/ledger reads, and in-flight paid
usage/raw audit retained without altering terminal Run/Findings/diff/comment JSON.
Final370 affected gateway/transport/eval/CLI and strengthened raw-paid-answer
controls passed (23.45 s); Ruff366/mypy363 pass. Full configured gate and fresh
Standards/Spec reviews are running with source/tests frozen. Explicit trusted
standalone callbacks retain offline eval and existing diagnostic assertions.

Initial full configured gate2700 passed, zero skips (305.42 s), but reviews found
2 P2s: the final live checkpoint could consume the remaining deadline, and late
paid conventions could insert/overwrite a terminal ContextPayload. This baseline
is not Task7b4 completion. Findings-first RED6 plus active4 (5.76 s) → GREEN10
(6.69 s): actual first/rotated HTTP after a slow checkpoint is refused at89 s with
90 s call timeout; real GenerateRepoConventions→GatewayConventionsModel→HTTP→
ExecuteReviewRun→SQL prompt/TransactionalRunTrace preserves missing/existing
whole ContextPayload after deletion and actual fresh OAuth rebind, with paid
usage/raw audit retained. Required final request callback rechecks the deadline.
Ordinary context UPSERT locks original installation→Repository→Run, refreshes
persisted identity and requires active origin/RUNNING before mutation. Shared
lifecycle lock helper is reused; append-only provider action writes stay permitted.
Ruff367/mypy364 pass; final478 affected consumers passed (86.82 s).
Final full configured gate2710 passed, zero skips (297.31 s); same Standards0/Spec0
rechecks confirm both P2s resolved.

Four cumulative sources (approved findings-first exception): llm/gateway,
llm/transport, bootstrap/llm_gateway, run_lifecycle_store. Existing eval_live
fake forwarding is coordinated with PR124; no queue/public API expansion.

### Task 7b: Provider, publication and terminal-redelivery guards

- [x] Refuse retired/mismatched origin before subsequent provider work, including
  already-terminal check-run redelivery and dynamically reconstructed VCS targets.
- [x] Preserve terminal historical state/payload and portal readability under the
  current Repository grants; new rerun uses the new immutable origin.
- [x] Freeze, all four gates and fresh review; then complete Task 7 above.

## Checkpoint: Rebinding and source segregation (Tasks 5–7)

- [x] Security negative cases and PG race tests pass; all four gates pass.
- [x] Source installation external ID stays immutable; no old work crosses lineage.

## Task 8: Prove deleted → created(new ID) → OAuth end to end

**Description:** Real migrated PG fixture seeds custom old data, feeds durable
deliveries, OAuth listing and deferred replay, and queries actual portal routes.

**Acceptance criteria:**

- [x] Before OAuth `/api/repos` and `/api/runs` omit old data; detail and mutation
  return 404 even with old JWT; created alone does not restore it.
- [x] Same-account OAuth restores exact old IDs/settings/rules/PR/Run and paused
  state for confirmer; stale second user stays denied; unpaused baseline works.
- [x] Cross-account/legacy/selection/rename, concurrent callbacks, target removal
  race, old-source replay and queued Run guard are verified with literal values.

**Verification:** Both infra URLs configured; `uv run pytest
tests/test_app_reinstall_http_postgres.py -rs` exercises real OAuth, signed durable
webhooks and portal routes with zero skips. Existing preservation/race tests remain
in `test_app_reinstall_postgres.py`. Final full `uv run pytest -rs` includes all
marked integration cases; collection records their count without a second subset
execution. Use random-schema Alembic fixtures, fake GitHub HTTP and real PG.
**Dependencies:** Task 7.
**Files touched:** new `test_app_reinstall_http_postgres.py`; existing preservation
and race proof lives in `test_app_reinstall_postgres.py`. Ownership was refreshed
before changes; shared tests retain their regression assertions.
**Estimated scope:** Small (split focused test files if fixture becomes unwieldy).

Evidence: two real composed HTTP chronologies passed (10.49 s). Actual A/B OAuth
against live17/account700 establishes saved JWT/refresh sessions before custom
settings/rules/conventions/cache/history and pause. HMAC deleted17/replay then
created29/deferred replay reaches durable receipt/real resolver/dispatcher with no
target/provider before OAuth. Refresh emits no GitHub calls and cannot restore
old-grant visibility; fresh A OAuth restores exact same-account identities/history
and pause, B remains denied until own callback. Old A JWT reads its newly confirmed
grants. Cache/action history has no provider fallback; paused rerun409 then PATCH
and rerun202 pins new29. Whole terminal Run/PR/rules/conventions/diff/context/actions/
blob rows stay unchanged. Different-account701 gets a distinct enabled Repository,
no old history/conventions access; source17 stays permanently retired and immutable.
Production unchanged. Final53 affected OAuth/refresh/onboarding/scope/reinstall
regressions passed (35.64 s); Ruff368/mypy365 passed. Same Standards0/Spec0. Reviewed test-resource cleanup now runs in guaranteed
inner finally; both composed cases rerun passed (9.77 s), static gates remain green. Per coordinator's final consolidation, mandatory full four gates and
configured integration suite run after Task9 docs before publication; no artificial
RED is manufactured for already completed production behavior.

## Task 9: Finish documentation and publish for review

**Description:** Rewrite §6.9 around approved final contract, document conservative
legacy consequences and transfer behavior, collect evidence and use agent-loop
standards/spec reviews before an issue-referencing PR.

**Acceptance criteria:**

- [x] SYSTEM_DESIGN §6.9 states Run/PR, settings, rules, Workspace, pause, transfer,
  legacy handling and authorization timing; remove the conflicting unconditional
  re-enable statement. Record any final departures from proposal in the plan.
- [x] Gates and all integration tests pass with zero integration skips; record test
  names/results and migration evidence. No new dependency expected.
- [x] Independent final standards/spec reviews pass with no feature exclusions.
- [ ] Publisher verifies branch against current main and creates/attaches the PR
  with What / Why / How to verify / Refs, no closing keyword/Development link.

**Verification:** `uv run ruff check .`; `uv run ruff format --check .`;
`uv run mypy .`; `uv run pytest -rs` with configured PG/RabbitMQ;
`uv run pytest --collect-only -m integration -q` counts integration cases included
in that full execution (no standalone subset execution claimed). Review current-head diff and ownership comments.
**Dependencies:** Task 8.
**Files likely touched:** `docs/SYSTEM_DESIGN.md` (#108/#127 coordination), these
two #113 plan files, optional `113-verification.md` with executed evidence.
**Estimated scope:** Medium.

Final technical evidence: all gates used both isolated infra URLs
`TEST_DATABASE_URL=postgresql+psycopg://app:app@127.0.0.1:55628/issue113` and
`TEST_RABBITMQ_URL=amqp://app:app@127.0.0.1:55634/issue113`.
`uv run ruff check .` passed; `uv run ruff format --check .` reported368 formatted
files; `uv run mypy .` passed365 source files. `uv run pytest -rs` passed2712 tests
in330.91 s with zero skips and two existing dependency deprecation warnings.
`uv run pytest --collect-only -m integration -q` counted409 integration cases
included in that same full execution (2303 deselected by collection only,1.13 s).
No standalone integration subset execution is claimed. Final independent
Standards0/Spec0 reviews cover the whole feature without exclusions. Fresh/base
SHA remains `8cf3608d80d0d1cf79e56e333b50d6a4d2052ab8`.
Migration proof is executed in `test_app_reinstall_schema_postgres.py` (existing
upgrade and fresh positive-BIGINT/nullable legacy), `test_run_origin_postgres.py`
(terminal/nonterminal backfill, upgrade/downgrade parity, NULL-history pin and both
production writers), alongside the two actual OAuth/signed-webhook chronologies
in `test_app_reinstall_http_postgres.py`. No dependency or queue/public schema
field was added. Sources stop changing here for publisher handoff.

## Final checkpoint

- [x] Approved behavior implemented and verified by automated AC evidence.
- [ ] Tech lead confirms issue #113 AC against the published current head.
- [ ] PR attached to chat; current-head approve and resolved threads required
  before merge; no merge authorization inferred from publication request.
- [ ] Tech lead accepts AC, updates board 12 and closes issue without PR auto-close.
