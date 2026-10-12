# PR #132 review correction plan (#113)

## Scope and evidence

Address all five P2 threads and assess all thirteen P3 notes from the tech-lead review
of `d6820df35fbc860696b89be30eb493cb06cf5928`. Update existing PR #132; do not open
another PR, merge companion PRs, resolve the reviewer's threads, or close #113.
Original `113-plan.md` and `113-todo.md` remain historical evidence.

Sources: `/tmp/pr132-review.json`, `/tmp/pr132-review-body.md`, issue #113, current
AGENTS.md, backend/git rules, original #113 plans and relevant adapters/tests.
Current fetched main is `46a9077`; #127, #125, #126 and #121 remain open. Rebase now
onto current main, and disclose that their future combined-head validation is still
pending. The user requested the agent-loop correction work. Present these saved artifacts for
the planning skill's human plan approval before implementation.

## Decisions

- P2-1: webhook/sync positive upsert must defer a repository that has a unique
  deleted archive under the same verified numeric account until fresh OAuth confirms
  that exact external repository ID. Preserve archive UUID, pause, settings, children
  and grants isolation. Deferred sync must neither create a target/default-rule row
  nor grant access. Keep the decision and write in the same short locked transaction.
- Use an explicit deferred result in the sync port/use case (nullable UUID or a small
  result type); avoid throwing an operational error that retries the whole receipt
  forever. Existing non-archive sync stays idempotent; OAuth alone authorizes rebind.
- P2-1(c): retain the permitted permanent clean-target outcome when first OAuth ran
  while the source was still active. Document it and test it, including repeated OAuth
  after late deletion. Do not move an established target Workspace/history implicitly.
- P2-2: in unmerged migration 0029's partial-revocation predicate, legacy `enabled=true`
  proves a later positive old-code projection. Whole-installation deletion remains
  unconditional. Preserve false preferences conservatively. No merged revision edits.
- P2-3/P3-13: inactive-origin queued/running/publishing work reaches
  `cancelled/installation_unavailable` without provider I/O, including retry and
  reconciliation paths. Distinguish this from user pause (`skipped/repo_disabled`).
  Use ownership/state compare-and-set guards and immutable origin; avoid manufacturing
  a queue pointer for the replacement installation. Reconciler discovery must see
  retired work even though provider projections remain fail closed.
- P2-4: retain audit containing the returned review ID after a successful POST even
  if retirement precedes comment lookup/finalization. Preserve the returned POST receipt
  through cancellation (a SubmittedReview receipt with no comments, or a cancellation
  carrying the review_id), record append-only audit, and let the existing finalization
  fence cancel. Never start another HTTP request, including comment GET, after
  retirement. Keep pre-request checkpoints; no relaxed post-POST read exception.
- P3-7: provider/repository fences use PostgreSQL FOR SHARE, in installation-first
  order; Run row writes keep their exclusive lock. Prove retirement/rebind still
  block, while independent workers and FK KEY SHARE do not serialize unnecessarily.
- P3-8: accept the current short-session per-request checkpoint cost for this fix.
  Preserve prompt retirement detection; no every-K-blob or timed relaxation. Explain
  the tradeoff in the review reply. A combined SELECT optimization is optional future work.
- P3-5: keep account/installations locking; add deterministic callback-versus-retirement
  interleaving with explicit synchronization and a mutation-sensitive assertion. Do
  not claim the old concurrent-callback tests prove every lock is necessary.
- P3-12: retain the early projector guard, which prevents provider calls on retired
  work. Preserve cancellation propagation in blob classification and prove there
  are no omission entries/classification warnings after retirement. Remove a redundant
  post-price LLM checkpoint only if transport checkpoints cover all subsequent work;
  otherwise explain its retained defense and exact purpose.

## Work partition and acceptance criteria

### Task 0 — Rebase and ownership (coordinator, prerequisite)

Rebase detached review work onto fetched main, preserving #108 terminology in the
Run, Контекст, Finding, Comment data rows and Прогоны API row. Add only origin to Run
and paused/unavailable 409 to run actions. Keep current main's entire design text.

Acceptance: current main is ancestor; no companion PR is merged; all overlap owners
receive an explanatory comment before editing their files. Recheck ownership immediately
before publication. Likely conflicts/owners: #127 SYSTEM_DESIGN; #124 SYSTEM_DESIGN and
WEBHOOK_WORKER; #121 WEBHOOK_WORKER, trigger/enqueue; #125/#126 CICD; #135 PIPELINE_SPEC;
#134 vcs_diff/github_vcs tests. Parent coordinates all ownership comments; developer C owns shared documentation edits
after source tasks. Coordinator does not modify codebase files during development.
Verification: inspect rebase diff against main, clean status except intended work.

### Task 1 — Defer archive onboarding (developer A, after 0)

Files: repository sync use case/store, GitHub rebinding adapter if shared selection is
needed, `test_app_reinstall_postgres.py`, repository-sync tests. Add HTTP chronology in
an independently owned new test file if necessary to keep tasks small.

Acceptance: (a) added after first OAuth cannot preempt later preservation; (b) first
org user's subset plus created replay cannot preempt second user's other repository;
(c) late source deletion retains documented clean target permanently. Assert exact
UUID/settings/pause/history and per-user access, not just row counts; confirm unrelated,
foreign-account and ambiguous archives still onboard cleanly. Test commit has red proof.
Verification: focused sync and reinstall PG tests against real PostgreSQL.

### Task 2 — Legacy schema/rollback semantics (developer A, after 1)

Files: unmerged 0029, `test_app_reinstall_schema_postgres.py`.
Acceptance: partial revocation/no grant + enabled=true upgrades available; false stays
paused conservatively; whole deleted installation remains unavailable even if true.
Downgrade folds `enabled = enabled AND installation_available` before dropping the
column. Seeded downgrade test proves unavailable rows cannot revive on 0028.
Verification: existing-schema upgrade, fresh schema and data-bearing downgrade tests.

### Task 3 — Terminalize retired work (developer B, after 0)

Files: lifecycle store, reconciliation use case, worker handler/guard as needed,
`test_run_origin_guard_postgres.py`, `test_reconcile_runs.py` and focused retry tests.
Acceptance: queued, expired running and expired publishing retired-origin Runs become
terminal on repeated reconciliation; retryable `_fail` after retirement cannot leave
QUEUED orphan; no run/review queue publication or GitHub/LLM call occurs. Active-origin
retries and lease ownership remain correct; null/mismatched origin is fail closed.
Claim reports installation_unavailable separately from user pause. Existing terminal
payload is unchanged. Split implementation/testing if more than five files required.
Verification: deterministic PG reconciler/worker tests plus active control cases.

### Task 4 — Audit/context cancellation evidence (developer C, after 0)

Files: GitHub review publication gateway, publication tests (unit and PG),
`test_run_context_origin_postgres.py` (avoid developer B's lifecycle store edits).
Acceptance: retirement immediately after successful POST preserves a
`github.publish_review` action with review_id, no new review POST, terminal state
cancelled/installation_unavailable and fenced findings/comments. RUNNING + retired
origin context write raises RunCancelled(installation_unavailable), inserts no payload
and leaves history intact; removing the origin guard makes the test fail.
Verification: real gateway with httpx.MockTransport and real PostgreSQL fence tests.

### Checkpoint 1 — All P2 behavior

Run focused tests with PostgreSQL before proceeding to polish; review the deferred
onboarding result and terminalization API together. No DB transaction spans network I/O.

### Task 5 — Lock and cancellation proof (B then C; coordinate shared files)

B owns lifecycle `_lock_provider_rows` and run_repository snapshot fence FOR SHARE.
A owns callback lock test. C owns blob cancellation classification test and diagnostics.
Files are partitioned explicitly; B yields lifecycle store before C needs any edit.

Acceptance: deterministic FOR SHARE fences prevent rebind/retire races and permit
concurrent provider readers/FK insertion; callback lock test fails without relevant
locking. Blob classification cancellation creates no omission/warning. `update_check_run`
RunCancelled and projector InstallationRetiredError emit info with ID/reason. Existing
retirement guards continue denying provider calls. HTTP PG test file has its own
TEST_DATABASE_URL skip fixture; shared setup helpers come from a non-test module.
Verification: focused lock races and cancellation tests; explain any retained redundant
checkpoints rather than weakening guarantees to satisfy a mutation.

### Task 6 — Documentation/public behavior (developer C, after 1–5)

Files: SYSTEM_DESIGN §6.9; WEBHOOK_WORKER positive-upsert wording; PIPELINE_SPEC §6,
T6/T11; CICD deployment runbook; developer C prepares proposed existing PR description for the publisher.
Acceptance: documents match paused-preference behavior, permanent late-delete clean
identity, public errorCode values installation_replaced/installation_unavailable
(cancelled, class, UI text), and claim error-code distinction. Runbook stops all old
Run writers (`api`, `webhook-worker`, and any worker rerun writer) before origin
migration, resumes new-image services afterward, and notes rollback identity loss.
Explain that account_external_id loss on downgrade is irreversible and #106's original
already-deleted legacy installation 168453694 cannot restore #57 history. No receipt
backfill/contract extension is included. PR body states rerun now returns 409 for pause
or App unavailability. Current UI maps every 409 to active-run/closed-PR wording; report
this evidence as a UI follow-up limitation, without silently modifying another repo.

### Checkpoint 2 — Full proof and review

Use uv for everything. Dedicated services are ready at
`TEST_DATABASE_URL=postgresql+psycopg://app:app@127.0.0.1:55628/issue113` and
`TEST_RABBITMQ_URL=amqp://app:app@127.0.0.1:55634/issue113`; fixtures migrate random
schemas. With those variables exported,
run `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy .`,
`uv run pytest -rs`. Confirm all integration cases execute with zero skips; collect
actual pass/file counts and timings. Run dedicated integration suite only if required
by CI/evidence; do not reuse original 2712/368/365 counts. Run the whole-plan final gates once after all implementation tasks; per-task checks
remain focused. Independent Standards and Spec reviewers then review current-main-to-head
changes; fixes require affected checks and renewed final required gates after source
changes. Reviewer-provided P2 seams define the TDD inputs; no additional seam confirmation
is needed after human plan approval.

### Task 7 — Publish to existing PR (publisher, after checkpoint 2)

Commit conventional correction changes with Refs #113; no --no-verify. Push the existing
PR branch with force-with-lease only for the reviewed rebase. Update What / Why / How to
verify / Refs and before-merge checklist, removing stale draft/no-reviewer/base claims.
Reply in each of the five threads with commit SHA and precise test evidence; post one
concise P3 disposition summary including accepted checkpoint cost and UI limitation.
Leave thread resolution to the reviewer. Attach existing PR. Monitor required CI.

Acceptance: #132 has the tested current head and all comments answered; no new PR,
merge, issue auto-close or Development-panel link. Required companion order remains
#127/#125/#126/#121 before #132; rebase and full combined-head gates again once they land.
Preserve #121 RunTriggerOutcome plus required keyword-only
expected_installation_external_id and adapt fakes. Preserve #127 fail-closed AuthScope
bullet; put 409 with 422 actions wording. Document/test old waiting CI receipt outcome
at that future integration. Current-head approve and resolved threads remain reviewer
requirements, not agent-authorized merge steps.

## P3 disposition map

| Note | Planned disposition |
| --- | --- |
| 1 downgrade | Task 2 seeded semantics; Task 6 identity-loss warning |
| 2 original #106 legacy case | Task 6 PR limitation; no evidence backfill |
| 3 rerun 409/UI | Task 6 API behavior disclosure and UI evidence/follow-up |
| 4 obsolete enabled wording | Task 6 WEBHOOK_WORKER/projector wording |
| 5 locking proof | Task 5 deterministic callback-versus-retirement test |
| 6 HTTP DB skip fixture | Task 5 own fixture/helper outside test modules |
| 7 exclusive provider locks | Task 5 FOR SHARE with deterministic race tests |
| 8 checkpoint cost | Keep strict checks; explicit accepted-cost reply |
| 9 migration live old writers | Task 6 stop-writers deployment runbook |
| 10 public errorCode catalog | Task 6 PIPELINE_SPEC T6/T11 and §6 |
| 11 silent control-flow exceptions | Task 5 info diagnostics with ID/reason |
| 12 cancellation/redundant checks | Task 5 classification proof; targeted assessment |
| 13 queued origin error code | Task 3 cancelled/installation_unavailable |

## Risks and remaining questions

No implementation-blocking human decision remains: reviewer explicitly permits the
late-deletion clean target and mandates deferred archive projection. Future merge-set
head is unavailable until companion PRs land; state this limitation truthfully. UI
409 wording remains a separate frontend limitation. Do not automatically file that
issue (create-issue has its own explicit approval gate). New P3 lock tests must use
barriers/observed lock waits rather than sleeps or chance scheduling. New dependency
is not planned; if necessary, document it and commit uv.lock under repository rules.
