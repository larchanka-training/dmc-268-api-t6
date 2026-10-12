# PR #132 round-three developer A proof (#113)

The final change is test-only in `tests/test_app_reinstall_postgres.py`, plus this
proof. Production behavior was already correct. Work ran in `<worktree>` on
`fix/113-pr132-round2`, starting from published head
`83a36290192369197f6f72f5b84a65f1add6d1d1`.

This implements tasks A1 and A2 from
[the round-three plan](113-review-round3-plan.md). The blocking regression comes
from [review thread 4250090363](https://github.com/larchanka-training/dmc-268-api-t6/pull/132#discussion_r4250090363);
the prelock and late-deletion controls address the associated P3 review comments.
The existing public OAuth, synchronization and removal use cases are the seams.
No new dependency, production edit, network provider call inside a transaction,
commit, push or GitHub write was needed.

## Four-step existing-target regression

`test_existing_clean_target_recovers_after_null_account_oauth_remove_and_add`
creates the clean Repository through `SyncInstallationRepositories.execute`, not
through a fixture insertion:

1. An archived source S has verified account 700, Repository 101 and preserved
   history/settings in its original Workspace. OAuth prelinks T to account 700
   in that Workspace with no repository confirmation.
2. A different user freshly confirms T with repository 101 and NULL snapshot
   account identity. The grant is accepted, the archived Repository remains at S,
   and real synchronization creates a distinct clean Repository at T.
3. Real repository removal marks that clean row unavailable and removes its grants.
4. Two added/sync applications restore the same clean UUID and availability,
   with exactly one target row. The source's ownership, pause, settings, PR/Run
   history, immutable rule, conventions, finding, audit action and Workspace
   budget are checked against literal expectations.

Deleting only `existing is None` from the deferral predicate makes step 4 return
an empty result instead of the existing target. This is the reachable regression
from the review; it is not a test that hand-inserts the disputed clean row.

## Permanent clean target without personal listing

`test_late_source_deletion_permanently_keeps_clean_target_identity` now has a
second parameter, `oauth_lists_repository`. The new False cases use an empty
repository listing for every OAuth callback. The test explicitly asserts zero
personal grants for target 101, so a grant cannot shield the Workspace equality
condition. Both onboarding-before-deletion and onboarding-after-deletion retain
clean identity, usability and preserved source history.

Changing `if archive == target.workspace_id` to `if archive is not None` fails
only the new no-listing / onboard-after-deletion case. The other three cases stay
green, identifying the intended additional control.

## Ordered row prelocks with real PostgreSQL interleavings

The race tests use `pg_blocking_pids` and PostgreSQL lock-wait observations to
establish the interleaving. Every wait and cleanup is bounded. They contain no
arbitrary sleeps and do not depend on probabilistic stress timing.

`test_overlapping_null_account_callbacks_prelock_before_creating_targets` uses
two users with reversed snapshot orders `(29, 23)` and `(31, 23)`, NULL numeric
account evidence in every snapshot, and an independent transaction holding row
29. Account advisory locks cannot serialize these callbacks.

- If lower target 23 already exists, the first callback owns 23 before waiting
  on 29; the second callback is observed waiting behind the first. Reversing the
  prelock's row order makes the second finish prematurely and fails this control.
- If 23 is missing, the first callback must wait on existing row 29 before
  creating any target. The independent callback can create 23 and 31 and commit
  while row 29 remains fenced. Deleting the callback prelock lets the first
  create 23 prematurely; the second is observed blocked behind that uncommitted
  insert and the regression fails.

After releasing row 29, both callbacks finish. Each target exists once with NULL
account identity and the expected per-user Workspace access.

`test_sync_prelocks_archive_before_target_without_blocking_null_account_callback`
holds archived source 17 while numeric-account sync targets 29. Correct sync
waits on earlier row 17 before owning row 29. A separate NULL-account OAuth
snapshot for 29 acquires no account advisory fence and must finish before source
17 is released. Removing or reversing the sync prelock makes sync hold 29 while
waiting on 17; PostgreSQL reports the callback blocked by sync and the test
fails. The unmodified sync then defers onboarding, leaving the source archive
unchanged and the target account identity verified.

## Actual verification and mutation evidence

The disposable PostgreSQL database was supplied as `TEST_DATABASE_URL=<dev-db>`;
the disposable broker was supplied as `TEST_RABBITMQ_URL=<dev-broker>`. The latter
is not used by this file. Tests run against migrated, individually isolated fixture
schemas. Commands below were run from `<worktree>` with those environment values.

Initial unchanged-production baseline:

```sh
uv run pytest tests/test_app_reinstall_postgres.py -k 'existing_clean_target_recovers or late_source_deletion or overlapping_null_account_callbacks or sync_prelocks_archive' -q -rs
```

**8 passed, 23 deselected, 2 warnings in 6.21s; zero skips.**

Each isolated mutation used `uv run pytest tests/test_app_reinstall_postgres.py
-k '<selector>' -q`. The exact original bytes were restored in a `finally` block
after each run. All six pytest executions exited 1 with the expected regression:

| Temporary mutation | Selector | Actual result | Failure |
| --- | --- | --- | --- |
| Drop `existing is None` | `existing_clean_target_recovers` | 1 failed, 30 deselected, 1.66s | Added/sync returns no Repository after removal |
| Replace Workspace equality with archive existence | `late_source_deletion` | 1 failed, 3 passed, 27 deselected, 3.70s | No-listing / late onboarding is incorrectly deferred |
| Remove callback installation prelock | `overlapping_null_account_callbacks` | 1 failed, 1 passed, 29 deselected, 2.01s | Independent callback waits on prematurely inserted lower target |
| Reverse callback prelock order | `overlapping_null_account_callbacks` | 1 failed, 1 passed, 29 deselected, 1.97s | Existing lower target is not owned before the higher-row wait |
| Remove sync installation prelock | `sync_prelocks_archive` | 1 failed, 30 deselected, 1.44s | NULL-snapshot callback is blocked behind sync's target lock |
| Reverse sync prelock order | `sync_prelocks_archive` | 1 failed, 30 deselected, 1.20s | Same observed row-lock inversion |

All mutation runs emitted the same two existing TestClient deprecation warnings
as the green runs. Restored SHA-256 values:

- `installation_repository_unit_of_work.py`:
  `d61c153e93965ab51b0b2310318c2f58f796f775536d2c6ad6b098893bf12947`
- `github_installation_links.py`:
  `003800e1ce2e61e18e6a96615116c0468bab4d9b8d4b0151283aacd20d6cbd6d`

Final restored-production check:

```sh
uv run pytest tests/test_app_reinstall_postgres.py -q -rs
uv run ruff check tests/test_app_reinstall_postgres.py
uv run ruff format --check tests/test_app_reinstall_postgres.py
uv run mypy tests/test_app_reinstall_postgres.py
git diff --exit-code -- app/modules/repositories/infrastructure/installation_repository_unit_of_work.py app/modules/workspaces/infrastructure/github_installation_links.py
```

**31 passed, 2 warnings in 19.26s; zero skips.** Ruff passed, one file was already
formatted, mypy found no issues in one source file, and both production paths had
no diff. The mutation/test window ended after this restored green run. Full
repository gates are the coordinator's subsequent checkpoint; this document makes
no claim about their result or live GitHub acceptance.
