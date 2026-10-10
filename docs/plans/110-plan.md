# Implementation Plan: database revision guard for rollback (#110)

## Overview and approval

Issue: <https://github.com/larchanka-training/dmc-268-api-t6/issues/110>.
Base: `92250a1fb3b65f88a2ea111d33613f9a4af1d1d2`, verified equal to local
`origin/main` and remote `main` on 2026-10-10. The isolated managed worktree is
`/Users/qx/.codex/worktrees/110-rollback-db-revision/dmc-268-api-t6`.

Approved by the user with “делай” on 2026-10-10. The approved checker/test seams and routine
task execution may proceed without another approval request.
Keep the original checkout's uncommitted #111 changes untouched. The final change must reject
an incompatible target before changing the application stack or its rollback metadata, allow a
compatible target, and print only names of environment variables from the running API and both
workers after successful application rollback. No automatic database downgrade is introduced.

## Findings and scope

`rollback.sh` currently writes `.env`, ensures application env files, pulls the target, and calls
`compose up`. Bootstrap then runs `alembic upgrade head` and prompt seeding; API and workers depend
on bootstrap success. An unknown revision can therefore break a rollback after recreation.
Both automatic entry points (`deploy.sh` after `up` failure and CI/CD after failed deploy/health)
call the same `rollback.sh` with `ROLLBACK_MODE=auto`; the manual workflow uses its default mode.
The manual workflow promotes only after the rollback job succeeds. Preserve these paths.

The repository's migration contract is linear and `tests/test_alembic_heads.py` requires one
head. Merged migrations are frozen. The image contains Python, Alembic, SQLAlchemy and psycopg;
`uv` is removed from the production image. Runtime checker commands therefore use image Python,
while repository tools and host test utilities always run through `uv`.

## Architecture decisions

1. Add a small, fully typed host-supplied Python checker in
   `deploy/scripts/check-rollback-revision.py`. Supply it through stdin to the target image:
   `IMAGE=<target> compose run --rm --no-deps -T bootstrap python -`.
   Compose uses the existing project network and database credentials, and the explicit image
   environment override takes precedence over `.env`. This creates only a temporary checker
   container; it must not start dependencies, execute bootstrap migrations, or replace services.
   Sending the source through stdin works with historical images lacking any new application
   module and requires no Python installation on the deployment host.
   Task 2 refinement, verified with real Compose v2.19.1: even `config bootstrap` loads missing
   unrelated role env files before filtering. The preflight therefore uses a private temporary
   bootstrap-only configuration, with the same resolved PostgreSQL credentials and external
   existing `${COMPOSE_PROJECT}_default` network. It has no role env files, dependencies or
   mounts. `COMPOSE_IGNORE_ORPHANS=true` suppresses warnings for the current services without
   removing them; full stack configuration is used only after admission. The EXIT cleanup
   removes the temporary probe configuration on success or refusal.
2. Load the Alembic graph from the target image's `/srv/alembic.ini`; query current database
   revisions with SQLAlchemy/Alembic `MigrationContext`, through a short read-only connection.
   Do not invoke `alembic current` with unrestricted logs, `upgrade`, `stamp`, seeding, or DDL.
   Bound connection/query time and finish the connection before returning. Use a read-only
   transaction or equivalent DB read-only setting so the probe cannot mutate the database.
3. Admission requires exactly one target head and exactly one tracked DB revision, recognized
   by the target and equal to or an ancestor of that head. Reject unknown or unrelated revisions,
   malformed graph, multiple target/DB heads, absent/empty version table, and connection/query
   errors. This deliberately fails closed to match the current linear-graph contract. A known
   ancestor may be advanced by the existing bootstrap after admission; no downgrade occurs.
4. Pull the target and run the checker before `write_compose_env_file`,
   `ensure_app_env_files`, `compose rm/up/down`, bootstrap removal, or state writes. Pull/login
   and an ephemeral checker are permitted; existing application/store containers and persistent
   files must remain unchanged on refusal. Registry cleanup remains unconditional. A refusal
   exits nonzero, gives a controlled reason plus operator next action, and does not print
   `rolled back`, add success metadata, or permit workflow promotion.
   Final-review correction: bind admission and recreation to the strict local image ID resolved
   after the single pull. Registry tags also require exactly one matching-repository RepoDigest
   whose local ID equals that target ID. Preserve explicit digest refs; local IDs remain supported
   for the disposable verifier. Probe and all application/bootstrap up roles receive the ID via
   explicit IMAGE override, with rollback up `--pull never`; persist immutable release ref in
   `.env`/current_image for later promotion. This closes remote/local tag movement across the
   admission/up/promotion boundary without changing normal deploy's pull policy or permissions.
5. Checker failures produce controlled, secret-safe messages. Safe revision identifiers may be
   included after validation; do not print URLs, passwords, SQL parameters, exception repr,
   traceback, full env, or raw Docker output. Captured unexpected target-container failures must
   be replaced with a generic preflight error, with no fallback to proceeding.
6. Put successful application rollback diagnostics in the shared rollback path, using
   `compose exec -T <service> python ...` for `api`, `worker`, and `webhook-worker`. Read
   `os.environ.keys()` directly from each running process environment, validate names, and print
   the service label plus a sorted JSON list of keys. Do not read or print values or split line
   oriented `env` output. JSON escaping/name validation prevents malicious names/newlines from
   creating uncontrolled output. This covers manual and automatic job logs with one behavior.
   A missing expected running service makes diagnostics fail explicitly; define successful
   completion only after the health wait and diagnostics succeed, then update state.
7. Preserve the existing no-previous-release bootstrap-nginx restoration branch. It has no
   target application image and therefore no target migration graph or worker diagnostics.
   Its destructive restoration semantics are unchanged and documented separately.
8. Ship the checker alongside the existing scripts in both workflow upload manifests. Keep
   `staging.yml` service definitions unchanged unless implementation proves a minimal adjustment
   necessary. No new dependency or production migration is planned.
   Task 4 refinement: the manual Terraform/ports path previously relied on rollback scripts
   left by the last forward deploy, while its upload step was edge-only. Refresh only
   `rollback.sh`, the checker and its existing `env-file.sh` helper on ports hosts before
   execution, and restore the rollback executable bit in the shared SSH step. Preserve the
   existing Compose files, secret files and provisioning on that path.

## Acceptance coverage

| Requirement | Evidence |
| --- | --- |
| Manual and automatic preflight before recreation | Execute real rollback shell script with fake Docker in both modes; assert exact operation order and target image override. |
| Unknown revision refuses without success | Snapshot `.env`, all role env files, current/previous state and call log; assert byte equality, no rm/up/down, nonzero result, no success message or promotion. |
| Compatible graph passes | Helper tests for same head and recognized ancestor; real isolated rollback reaches healthy API and both workers and correct state semantics. |
| Fail closed on unsupported graphs/errors | Graph fixtures for unknown, unrelated, multiple/missing heads and untracked DB; fake Docker pull/probe failures and controlled error output. |
| Real images and separate PostgreSQL | Reproducible opt-in verification script, unique Compose project/network/volumes and synthetic migration in derived test image; record image IDs/digests, revisions, result codes, container IDs and file hashes. |
| Runtime environment names only | Execute real diagnostic payload against environment including PEMs, `$`, backslashes, secret canaries and hostile keys; names match and no value/PEM line appears in stdout/stderr. |
| Document admission/refusal and no downgrade | Update CICD §5 and SECRETS logging guidance; describe guarantee boundaries and operator actions. |
| Definition of Done | All four uv gates, focused tests, live verification, independent Standards/Spec reviews, draft PR with `Refs #110`. |

## Isolated two-image PostgreSQL verification

Add a reproducible, explicitly invoked verifier under `scripts/`; it must not run as a default
pytest test or add a skipped integration test to required CI's zero-skip suite. Prefer an isolated
compose override/fixture under `tests/fixtures/rollback/`, with a unique project name per run.
Use fresh PostgreSQL and RabbitMQ volumes and Redis, local-only unpublished ports or ephemeral
localhost API port, and no staging/edge shared network. Use synthetic GitHub credentials including
a generated valid RSA PEM for worker startup; never use live App/LLM credentials or send real
GitHub/LLM calls. Guard cleanup to remove only resources created by that invocation.

Build image A from the approved implementation checkout. Build distinct image B by extending A
with a test-only migration N whose `down_revision` is A's single head M. N creates a harmless
fixture table; it is copied only into the derived image, never `alembic/versions` in the repo.
The checker remains supplied by host script, so A/B also exercise historical-image support.
Resolve both local image references to immutable image IDs; local verification may use a
scoped Docker wrapper to treat already-built local image pulls as successful without bypassing
any compose, checker, migration or container operations. Document that wrapper clearly.

Execute these real-script cases:

1. Start A normally; its bootstrap migrates the disposable DB to M. Bring up the stores
   explicitly, then replace only application services with B using `up --no-deps` so the
   compatible fixture deliberately retains DB M without applying B's synthetic N. The
   synthetic migration affects only an independent fixture table, so B application behavior
   remains valid at M. Require healthy API and both workers before recording B IDs and files.
2. Run real manual rollback B to A. A recognizes M at its head; its normal bootstrap and
   health wait complete, all three services become healthy, and diagnostics contain only
   names. Repeat with a controlled B-at-M fixture and auto mode; verify previous-state rules.
3. Deploy B normally, including bootstrap, so the isolated DB reaches N. Attempt rollback B
   to A in manual mode and then auto mode. Both refuse before mutation. Compare all
   application/store container IDs, health, revision N, fixture table/data sentinel, `.env`,
   role env files and current/previous state hashes; no success output or diagnostics.
4. Exercise controlled unreachable-PostgreSQL refusal. Check API `/healthcheck` and both
   worker heartbeat health after accepted and refused rollback; scan output for all synthetic
   secret values and PEM body lines. Record redacted evidence, then clean only resources
   created by this verifier invocation.

This two-image setup proves conventional B-to-A rollback with both compatible and incompatible
DB states. The compatible setup explicitly skips B bootstrap to keep M; it is a controlled
fixture setup, not a claim that ordinary forward deployment skips migrations. Unit/behavioral
tests separately prove known-ancestor admission. Never reproduce the unsafe case on staging.

## Incremental tasks and checkpoints

Tasks are specified in `110-todo.md`: (1) checker contract, (2) rollback admission boundary,
(3) runtime key diagnostics, (4) workflow distribution, (5) live verifier, (6) runbook/evidence
and complete verification. Every implementation task follows a recorded RED → GREEN → REFACTOR
cycle; existing meaningful tests are extended rather than adding tautological source assertions.
Review each task independently, and checkpoint after tasks 2 and 4. Approval of this plan covers
routine task execution; request more input only for a material change in scope.

## Risks, limits and coexistence

| Risk | Mitigation |
| --- | --- |
| Recognized revision does not prove application/schema/secret compatibility | Describe admission as an Alembic-graph prerequisite only; retain startup/health checks and release migration review. |
| Migration can change after probe via another actor | CI's staging lock protects ordinary workflows; document operator serialization and avoid concurrent migrations. Do not span stack changes with a DB transaction. |
| Auto rollback runs after failed deployment already changed the stack | Refusal preserves state as it exists when rollback begins; it cannot undo changes from the failed forward deploy. Say so in runbook/evidence. |
| Older target lacks Python/Alembic/runtime prerequisites | Fail closed with safe generic probe error; never assume compatibility or execute downgrade. |
| Docker/DB errors contain secrets | Capture/suppress raw probe output; test canary values across every failure path. |
| #111 edits some same files in original checkout | Keep worktree isolated; notify coordinator and recheck open PR ownership before edits/review. Do not copy #111 changes or edit env-file.sh unnecessarily. |
| Supported historical DB graph differs from current single-head policy | Refuse unsupported topology; supporting branched/unversioned schemas is separate scope requiring explicit decision. |

Open PRs checked through `gh pr list` on 2026-10-10: #124 eval, #123 runs, #122 OpenAPI,
#121 webhooks, #108 context docs. No planned file overlaps those PRs. Avoid `docs/TEST_PLAN.md`
(owned by #108); record the focused verifier recipe in CICD §5/new verifier documentation.
Recheck this before implementation and publishing; comment on any newly overlapping PR before
editing an owned file, following AGENTS.md.

Implementation recheck: PR #125 (`fix/111-worker-portal-url`) also owns `ci-cd.yml`,
`env-file.sh`, `CICD.md`, `SECRETS.md` and `test_deploy_staging_services.py`.
Task 1 does not overlap. The coordinator will place the required coordination comment before
later overlapping edits; do not import #111 changes or edit its original checkout.
The coordination comment was posted before any later task edits:
<https://github.com/larchanka-training/dmc-268-api-t6/pull/125#issuecomment-6096022216>.

## Completion and publishing

Run `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy .`, and `uv run pytest`.
Run appropriate PostgreSQL/RabbitMQ integration coverage with explicitly disposable resources;
report any existing skips accurately. Do not claim live verification succeeded until recorded
results exist. Rebase on current main and rerun checks required by resulting changes.
Independent review must report zero Standards and Spec findings. Publish a draft PR by default
on `fix/110-rollback-db-revision`, with What / Why / How to verify / Refs, no closing keyword,
no Development-panel issue link, and no new dependency. Attach the created PR to the chat.
The tech lead closes #110 after acceptance; do not merge or close it automatically.
