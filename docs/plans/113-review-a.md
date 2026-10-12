# Developer A correction evidence (#113 / PR #132)

Worktree: `<worktree>`.
Development base: rebased `69505ab`, current main `46a9077`.

## P2-1 archive projection

Red: `uv run pytest tests/test_app_reinstall_postgres.py -q -k positive_sync`
with dedicated PostgreSQL: 2 failures, both returned a fresh default-rule onboarding
result where the accepted behavior requires an empty result. Cases: first OAuth has
no repositories; first org user has subset 103 and later user confirms archive 101.

Green: explicit `UUID | None` sync-port result; `None` skips rule onboarding and
commits without a target repository or new grants. A verified same-account archive
in the target Workspace is deferred until exact-repository OAuth. Existing target
rows and already accepted OAuth repository grants retain clean-target behavior.
The latter preserves the conservative missing-account-snapshot control: no rebind,
but a freshly accepted clean repository may onboard normally. Foreign account,
legacy source, ambiguous archives and source-active controls still onboard cleanly.

Public sync + OAuth regression assertions cover archive UUID, pause, availability,
engine, CI preference, comment count, review event, prompt, conventions, code changes,
runs, action history, default-rule count and per-user repository/run access. Replays
create no default rule or grant before confirmation. Late deletion has two controls:
clean onboarding before and after deletion, repeated OAuth, stable clean UUID and
Workspace, untouched source history/settings, no implicit history merge.

## P2-2 and P3-1 migration

Red: existing-schema test failed for repository 106: partial revocation/no fresh grant
and legacy `enabled=true` upgraded unavailable. Added row 107 confirms whole deletion
remains unavailable despite `enabled=true`. Green: only the partial predicate requires
`enabled=false`; whole deletion is unconditional.

Red: seeded downgrade test failed for repository 101: unavailable + enabled true revived
on 0028. Green: unmerged 0029 folds `enabled = enabled AND installation_available`
before dropping availability. Four literal preference/availability combinations cover
false/false, false/true, true/false and true/true. No merged migration changed.

## P3-5 deterministic locks

Shared advisory-key helper is used by callback and positive sync. Sync acquires the
account fence before installation rows, using callback's sorted installation order;
it rechecks active target state after locks. Decision and write remain one short DB
transaction with no network.

Callback-retirement proof holds account 700's advisory lock with no target rows,
observes callback `pg_stat_activity.wait_event='advisory'` and `pg_blocking_pids`, then
retires source and releases. OAuth preserves source Workspace/UUID/pause/history.
Removing callback's account lock fails with `OAuth bypassed the account lock before
retirement`; mutation was restored in `finally`.

Callback/sync proof queues callback first, observes its account wait, starts sync,
observes both account waits, then releases and asserts completion without deadlock,
existing archive UUID and one immutable default rule. Removing sync's account lock
also fails; mutation was restored in `finally`. The tests poll an observed lock condition
with a bounded timeout; they do not assume a sleep establishes ordering. PostgreSQL
statistics snapshots are refreshed between observations.

## Focused verification

Dedicated `TEST_DATABASE_URL=<dev-db>`:
`uv run pytest tests/test_installation_repository_sync.py tests/test_app_reinstall_postgres.py tests/test_app_reinstall_schema_postgres.py -q`
— **35 passed** (9 sync unit, 23 reinstall PostgreSQL, 3 schema PostgreSQL),
zero skips, 21.88 seconds.

Focused `uv run ruff check`, `uv run ruff format --check` and `uv run mypy`
passed for the seven application/test files and unmerged migration (mypy covers seven
files, migration excluded). Final whole-plan gates are coordinator-owned.

Files: sync application/store; `github_installation_locks.py` new shared helper;
GitHub installation links; unmerged 0029; installation sync unit tests; reinstall PG
and schema PG tests; this evidence. No commits, pushes or shared TODO edits performed.

## Fresh reviewer iteration: NULL-to-numeric account lock race

Independent Spec review found a target identity transition between the initial unlocked
read and target row locking. The synchronized PostgreSQL regression initially failed
with real `psycopg.errors.DeadlockDetected`: sync read target account NULL; public OAuth1
confirmed no repositories and assigned account 700; sync then held target while public
OAuth2 held account/source and waited target; sync refreshed 700 and waited source.
The test uses asyncio events, observed PostgreSQL blocking and a two-second database
lock timeout, so the red cannot leave the suite hanging.

Correction: infrastructure captures the initial numeric identity, locks the active
installation with `populate_existing`, and raises application-defined
`InstallationIdentityChanged` when locked identity differs. Application `execute` and
`is_active` exit the failed UOW before retrying, releasing every target lock, then acquire
account first and installations in global order. Numeric identity is immutable once
assigned, so retry is bounded to one; a repeated change propagates as an error. INFO
logs identify installation UUID and account-transition reason. No repository commits,
network call, savepoint assumption or swallowed inactive result is involved.

The PG regression now passes for both public `execute` and `is_active` paths, asserting
callbacks finish without deadlock, preflight returns true, positive sync actually
onboards clean target, and archive UUID/history ownership is unchanged. Four unit
controls verify one retry, rollback counts, second-transition propagation, commit
behavior and INFO diagnostics.

Standards follow-up: worker eligibility matrix's two unavailable-origin rows now assert
`cancelled/installation_unavailable`; the available-but-paused row retains
`skipped/repo_disabled`. Lifecycle implementation was unchanged.

Final affected command with dedicated PostgreSQL:
`uv run pytest tests/test_installation_repository_sync.py tests/test_app_reinstall_postgres.py tests/test_effective_review_eligibility_postgres.py -q`
— **62 passed** (13 sync unit, 25 reinstall PostgreSQL, 24 eligibility PostgreSQL),
zero skips, 27.62 seconds. Ruff lint/format and mypy passed for the five affected files.
Source/tests are stable for coordinator final gates and fresh review; only evidence was
edited after that handoff. No new dependency or Git mutation.
