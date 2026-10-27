# Plan: GitLab staging configuration (#335)

Status: human-approved implementation published as [PR #339](https://github.com/larchanka-training/dmc-268-api-t6/pull/339); original gates and CI passed. Round 1 corrections R0–R6 and final verification are complete: all four code gates pass (9298 tests, zero skips/failures/errors), production-image smoke passes, and fresh cumulative Standards/Spec reviews report zero actionable findings. This committed snapshot precedes round 1 publication, new-head CI and thread replies; the live PR records those subsequent results. Real GitLab acceptance remains pending.
Published integration baseline: `f160ce4c82220c20e627fbfdc0230b43a5f34492` (`main`, merged monitoring #279 retained).
Round 1 integration baseline: `430d6604a0fc289916b34cc5aa6bdd307cea40b8`; fetched/rebased without conflicts, preserving all original PR file bytes and planner changes.
Original design/test baseline: `88bbc0734e50e33fa769940291a79ff997102994` (#169 merged).
Workspace: dedicated managed worktree for #335.
Specification: <https://github.com/larchanka-training/dmc-268-api-t6/issues/335>.

## Outcome and limits

Extend the existing application-secret bundle, recipient env files and staging
deployment so an operator can configure the installed GitLab runtime without
editing live containers or database rows. Retain encrypted installation credentials
in the database, keep API admission default-off and preserve GitHub-only deploys.
Then perform the real GitLab MR acceptance when the protected prerequisites exist.
No new dependency, migration, onboarding UI, Bitbucket work or production activation.
The coordinator delegates implementation; this document is not implementation.

## Original observations (`88bbc073`)

- `ci-cd.yml` bundles only GitHub/auth/LLM/portal settings. It uploads live compose
  and scripts and prepares the host **before** bundling application secrets.
- `env-file.sh` validates bundle encoding and env-file representation before writing
  its recipient files. It does not validate the GitLab runtime contract.
- `staging.yml` sends `app.env` to both workers, `api.env` to API, and dedicated
  LLM/bot files to the respective workers. No GitLab keys are wired.
- `GitLabRuntimeSettings.optional_from_environment` already implements complete
  opt-in validation; `GitLabApiSettings` separately parses admission. Reuse them.
- `deploy.sh` without `APP_SECRETS_B64` preserves host app files; CI supplies even
  an empty bundle and therefore replaces their contents. Preserve this distinction.
- `rollback.sh` retains the latest application env files, independently of image,
  and already checks image/schema compatibility before live mutation. Preserve it.
- `app.gitlab_admin` implements create/bind/inspect/activate/revoke, safe stdin/FD
  input and encrypted credentials. Create needs an existing workspace; bind needs
  prompts and packaged rules. No extra onboarding endpoint or SQL recipe is needed.
- The image packages `app/`, dependencies, prompts and rules; a profile-only
  operator container can execute `python -m app.gitlab_admin` on the deploy host.
- Prior live observations: staging healthcheck 200, GitLab admission 503/default-off;
  no successful real MR run was established. Name-only repo/environment queries
  found no `GITLAB_*`; inherited organization secrets and live values were not read.
  Existing LLM configuration must be verified, not assumed missing.

## Configuration and ownership contract

| Source in staging environment | Recipient | Storage |
| --- | --- | --- |
| secret `GITLAB_CREDENTIAL_KEYS` | both workers and trusted operator container | new owner-only `gitlab.env` |
| variables `GITLAB_ACTIVE_KEY_ID`, `GITLAB_ALLOWED_ORIGINS` | same | `gitlab.env` |
| optional variable `GITLAB_PRIVATE_ORIGINS` | same | `gitlab.env` |
| variable `GITLAB_WEBHOOK_INTAKE_ENABLED` | API only | existing `api.env` |
| installation PAT / webhook secret | supported operator CLI stdin | encrypted PAT / secret digest in DB |

API, bootstrap, broker/cache/database, monitoring and unrelated services never
receive the keyring or PAT. The operator service receives only DB settings and
`gitlab.env`, without GitHub keys or LLM credentials. A real operator smoke during
the runbook slice showed that `pull_policy: always` forced registry access even
for the cached reviewed image. The authorized narrow fix sets `missing` only on
the operator; live API/workers/bootstrap retain `always`. The unchanged-source
operator smoke then passed with an unreachable registry and no credentials. It is excluded from normal
`compose up`, has no published port and does not run automatically on redeploy.
The existing shared `app.env` remains GitHub-specific.

No values means disabled runtime and disabled admission. Complete core settings
may be provisioned with admission false for controlled onboarding. Admission true
requires the complete valid worker settings. Any nonempty partial/invalid worker
bundle or invalid admission value fails preflight. Unset or empty GitHub source values for optional private origins
remain omitted, preserving the existing bundle semantics and parser default `[]`.
A nonempty invalid value must reach validation and fail rather than become unset. A private repository on gitlab.com does not require private origins.
Custom CA deployment is not added: normal TLS trust applies; self-managed custom
trust needs a separately reviewed mounted-CA deployment change.

## Preflight before mutation

Add one small executable Python deployment validator under `app/bootstrap/`, using
the existing runtime/API parsers without DB/provider/network calls and without
logging values or parser tracebacks. A synthetic valid DB DSN supplies only the
parser requirement; it must never connect. A deployment-specific check ties enabled
API admission to valid worker settings. Test these public outcomes directly.

Run this validator in the exact immutable target image, with networking disabled
and a protected temporary env file; credentials are not Docker argv values.
CI validates before the **first** SCP/SSH host mutation, not merely before
`compose up`. Reorder the existing bundle step and add the smallest necessary
registry/image preflight steps; retain deployment concurrency and health/rollback.
Remove temporary runner files on success/failure; mask the bundle before use.

For manual `deploy.sh`, stage and validate all candidate application files before
replacing current app files, `.env`, deploy state/switch or invoking compose.
Reuse the same validator from the target image; pulling an image/logging into the
registry is allowed before admission, changing the running release is not. Do not
validate through the possibly old running worker. Invalid bundles preserve file
bytes, permissions and running service identities. Refactor only the bundle
staging boundary required for this contract, not the whole deployment system.

## Redeploy, disabling and rollback

- Configuration is environment-scoped, not release-scoped, as with current app
  secrets. Redeploy never creates an installation, rotates keys or changes bindings.
- CI writes exactly the configured bundle; an absent manual bundle retains it.
  Explicit removal in CI removes the corresponding file entry; document this.
- Disable admission with its API flag while retaining the keyring. Recovery uses
  inspect/revoke. Removing key IDs with existing ciphertext is not a safe disable.
  No automatic key rotation/removal or secret restoration from image history.
- Manual/automatic rollback retain current GitLab env and DB installations. Existing
  schema/digest admission remains mandatory. Do not downgrade DB or rewrite state.
- A target before #335 may lack the new deployment validator but already support
  #169 settings. Do not reject it merely because the new helper is absent: for
  compatible targets, validate with their existing settings parsers. Unknown or
  unsupported settings/targets must fail closed **before** modifying live files
  or services; an operator must disable admission through a valid deploy or choose
  a compatible target. Schema-incompatible pre-#169 targets remain rejected by the
  existing migration guard. Include explicit tests for supported legacy target and
  unsupported target with unchanged state, rather than assuming ignored variables
  prove compatibility. Reuse the existing guarded rollback probe where practical.

## Ordered TDD slices

Each development slice starts with a failing behavioral test, implements the
smallest change, runs its targeted checks, then receives independent Standards and
Spec review before the next slice. See `335-todo.md` for the detailed checklist.

1. **Pure admission contract (S, 2 files).** Add validator and direct tests for
   default-off, complete core with flag off/on, invalid/partial JSON/origins/key
   material, invalid booleans and static secret-free failures.
2. **Recipient files (M, 3 files).** Extend env-file routing and compose with
   `gitlab.env` and profile-only operator service; tests exercise actual bundle
   writing/recipient environments, 0600 files, absent optional entries and no PAT.
3. **CI early preflight (M, 3 files).** Extend bundle sources and validate the
   target image before upload. Tests execute the workflow shell with fake Docker,
   assert ordering and use canary values to check stdout/stderr/argv redaction.
4. **Host deploy/rollback boundary (M, up to 5 files).** Stage before replacement;
   retain manual-vs-CI semantics and stable env across successful/failed deploy,
   manual/automatic rollback, and old-image compatibility. Reuse deployment test
   fakes; add rollback workflow changes only if the compatibility probe needs them.
5. **Operator runbook (M, 3 docs).** Update `.env.example`, `docs/SECRETS.md` and
   `docs/CICD.md` with exact sources/recipients, profile CLI commands, create → bind
   → inspect → activate, generation, revoke, secret stdin and rollback recovery.
6. **Live staging acceptance (S, evidence only).** After deployment and prerequisites,
   create a disposable real MR, observe one succeeded GitLab run, inline findings
   and one summary, replay original delivery and verify no duplicate run/publication;
   revoke the test installation and verify intake stops. Publish only sanitized
   results to #335/#169, respecting private-evidence access.

Checkpoint after slices 1–2: the settings/recipient contract is review-clean.
Checkpoint after slices 3–4: negative configurations preserve the running release;
GitHub-only deployment and rollback contracts still pass.
Checkpoint after slice 5: final code gates and independent review are green; prepare
PR with explicit live acceptance status. Slice 6 stays incomplete without evidence.

## Verification budget

Baseline once: `uv run pytest tests/test_deploy_staging_services.py
tests/test_gitlab_runtime_settings.py tests/test_gitlab_admin.py`.
Each slice runs only its affected tests while iterating; retain RED/GREEN evidence.
Before publication run the four mandatory gates once against the final candidate:

```text
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run pytest
```

Use a task-owned disposable PG/Rabbit stack and Linux execution where existing
tests require it; record environment and all skips/failures. Run existing rollback
and deployment suites for changed boundaries, plus one real Compose recipient/
profile/preflight smoke with synthetic credentials and `docker compose config`
captured privately. Never print rendered secret-bearing compose output.
CI must cover actual PG/Rabbit shards; mocks do not replace integration or live AC.
Repeat the complete suite only after a relevant fix, a changed candidate or an
unresolved failure; do not loop full runs while waiting for credentials/review.

## Live acceptance prerequisites and evidence

Code work does not need production secrets. Live acceptance needs all of:

1. Deployed reviewed #335 image and a staging operator execution path (existing
   authorized SSH or a separately approved protected operator workflow); browser
   GitLab login alone does not grant service PAT or server access.
2. Stable keyring provisioned in the staging secret store and nonsecret variables;
   no keys/PAT in chat, argv, issue, Git, screenshots or logs.
3. Existing authorized test workspace and a PAT with actual project API/comment
   authority, plus a protected random webhook secret. The private test project's
   identifiers stay in local evidence, unless the user authorizes disclosure.
4. Working seeded prompt/rules, workers/queues and LLM configuration/budget. Read
   only safe configuration names/health; do not infer inherited secrets from absence
   of environment-level names. New credentials/access grants require user action.
5. TLS-reachable API webhook registered on the test project and a deterministic MR
   fixture likely to yield an inline finding. Real LLM output may require a focused
   fixture correction; empty findings are not proof of inline publication.

Record deployment/head SHA, delivery/event ID, installation/run IDs, provider,
terminal status, discussion/summary counts before/after identical replay, and
revoke outcome. Show authorized acceptors private MR evidence securely; public
issues receive sanitized results/references only. Do not mark live AC done from a
200 healthcheck, synthetic delivery, mocked model or 202 admission alone.

If these prerequisites remain missing, publish a concrete validated PR with the
live gate explicitly incomplete; do not claim #335 done, close #169/#335, activate
production or launch repeated full tests to fill the wait.

## Coordination before edits

Historical open PR file lists were checked during original planning. These notices
were authorized and published before the original shared edits (no authors identified):

- <https://github.com/larchanka-training/dmc-268-api-t6/pull/279>:
  `.github/workflows/ci-cd.yml`, `docs/CICD.md`, `docs/SECRETS.md`.
- <https://github.com/larchanka-training/dmc-268-api-t6/pull/283>:
  `docs/CICD.md`.

Approved exact notice in #279:

> For #335, I plan to update `.github/workflows/ci-cd.yml`, `docs/CICD.md` and
> `docs/SECRETS.md` for default-off GitLab staging configuration, preflight before
> host changes and the trusted operator runbook. I will preserve your monitoring
> acceptance and Grafana-secret wiring; whoever merges second rebases.

Approved exact notice in #283:

> For #335, I plan to update `docs/CICD.md` for GitLab staging configuration,
> preflight, rollback and the trusted operator runbook. I will preserve your
> #213 deployment/prompt documentation; whoever merges second rebases.

The human approved the plan and exact notices on 2026-10-26. Original overlaps
were limited to these two PRs. Authorized notices were published before edits:
[#279](https://github.com/larchanka-training/dmc-268-api-t6/pull/279#issuecomment-6426698992),
[#283](https://github.com/larchanka-training/dmc-268-api-t6/pull/283#issuecomment-6426700273).

Round 1 refresh also finds [#340](https://github.com/larchanka-training/dmc-268-api-t6/pull/340):
`.env.example`, `.github/workflows/ci-cd.yml`, `deploy/compose/staging.yml`,
`deploy/scripts/env-file.sh`, `docs/CICD.md`, `docs/SECRETS.md`. The human authorized
the new notice and it was [published before corrective shared edits](https://github.com/larchanka-training/dmc-268-api-t6/pull/340#issuecomment-6438984537).
The existing #283 notice covers
its continuing `docs/CICD.md` overlap. Add #340 to PR Refs and agree that the second
PR to merge rebases on the first, manually reconciling allowlists and recipient
tables. Refresh open PR file lists before development and publication.

## Required main refresh before publication

Main advanced by six monitoring commits after the previous candidate review. The
candidate was preserved in a scoped recoverable backup, then reapplied onto
`f160ce4c82220c20e627fbfdc0230b43a5f34492`. Keep upstream Grafana-secret wiring,
manual monitoring acceptance and bounded app-RPS checks. The merged monitoring
subsection stays 2.3; GitLab operator documentation is now 2.4. Earlier slice
results remain historical evidence; fresh cumulative review and final code gates
are required for this integrated candidate. Refreshed overlap checks passed (14 tests);
all eight non-overlap implementation files and all six upstream-only monitoring
files match their preserved sources byte for byte. The candidate is frozen for
fresh cumulative review and final gates. Real GitLab acceptance remains pending.

## User documentation and publishing

AGENTS.md and CONTRIBUTING §8 require a UI PR/issue link for user-visible backend
changes. This change enables already-implemented GitLab behavior on staging; link
the existing user-docs issue
<https://github.com/larchanka-training/dmc-268-ui-t6/issues/173> and #169's existing
GitLab docs checklist, first confirming they still cover this onboarding limitation.
Do not change core UI docs or implementation without explicit scope/review.
If the diff is strictly operator wiring, state that judgment in the PR user-docs
checklist; retain the link rather than silently ignoring the policy.

Publisher uses the pull-request skill, explicit staging paths, Conventional Commits
referencing #335, and `What / Why / How to verify / Refs` body sections. No closing
keywords or Development-panel link. Attach the created PR to the chat. Rebase before
requesting review; merge and issue closure are outside this implementation approval.

## Review round 1 corrective plan

Review of published head `eb9e8340aa63a23f8b7334219927d60fa72c92f6` requests three
P2 fixes and fifteen P3 corrections. The approved architecture and live acceptance
boundary remain unchanged. The existing approved implementation/review loop covers
these corrections; this is its next iteration, not a new feature plan.

### R0 — Refresh integration and ownership (S)

Dependencies: none; the #340 notice above is authorized and published.
Files: existing plan/todo; Git history and later PR metadata only.

- Fetch and record exact current main, preserve the candidate, then rebase before
  implementation. The known delta `f160ce4c..430d6604` contains ten commits for
  #247/#130/#168 and touches no #335 implementation paths; inspect the actual
  fetched delta rather than relying on that historical result.
- Preserve upstream policy/prompt behavior and monitoring documentation. Old gates
  and green CI describe the published head only; the corrected/rebased candidate
  needs fresh gates and review. Mark publication/CI historical completion accurately
  and keep new-round work unchecked; omit workstation/runtime traces from plans.
- Verification: clean scoped status and inspected delta before rebase; inspect the
  cumulative post-rebase diff and overlap list. Covers P3.13, P3.14 and P3.15's
  stale rebase assertion; metadata completion belongs to R7.

### R1 — Fail closed on helper I/O and stage on the destination filesystem (M)

Dependencies: R0 and published #340 notice. Files: `deploy/scripts/env-file.sh`,
`deploy/scripts/deploy.sh`, `deploy/scripts/rollback.sh`,
`tests/test_deploy_staging_services.py`.

- Explicitly check every fallible operation in the helpers called through `if !`:
  directory/file creation, role-file generation, reads, encoding pipelines,
  preflight-file writes, permissions and renames. Audit nested
  `write_app_env_files` too; `set -e` inside a subshell is insufficient. Install
  cleanup only after a nonempty private directory exists; failure must never form
  a root-relative secret path or continue to Docker validation. Missing retained
  files keep existing semantics; an existing unreadable file is an error.
- Create deploy stage with `mktemp -d "${APP_DIR}/.stage.XXXXXX"` and retain cleanup,
  owner-only access and preflight-before-replacement. Each admitted recipient file
  then moves by rename on the same filesystem; this is not an all-files transaction.
- RED/GREEN: deploy and manual/automatic rollback with invalid TMPDIR, injected
  retained-file read failure and injected creation/write/encoding failures must
  refuse before mutation, preserving bytes, modes, state/switch and compose
  activity. Assert no outside/temp secret files and cleanup; use executable error
  injection for unreadability so root test runners cannot bypass chmod. Direct
  helpers must also fail when invoked in the actual conditional context. Covers
  P2 thread `4387180590` and P3.3.

### R2 — Prove current and legacy target refusal (S)

Dependencies: R0; tests do not touch the shared implementation files.
File: `tests/test_deploy_staging_services.py`.

- Parameterize enabled-intake/no-runtime deploy and rollback refusal over current
  and legacy targets, including manual/automatic rollback. Preserve supported
  legacy acceptance and unsupported-target refusal, with unchanged live files and
  modes. Split unsupported cases from accepted current/legacy cases instead of
  branching assertions inside one parameterized test.
- RED/GREEN: remove only the legacy `admission.intake_enabled and runtime is None`
  guard in a disposable mutation copy; the new deploy and rollback regressions
  must fail, then pass on restored code. Never leave mutation in the candidate.
  Covers P2 thread `4387180603` and P3.8's `kind == "unsupported"` branches.

Checkpoint R1–R2: host refusal is fail-closed under real conditional calls and
both supported image generations, with unchanged live state and mutation proof.

### R3 — Validate one normalized configuration contract (M)

Dependencies: R1–R2 and published #340 notice. Files: `env-file.sh`,
`.github/workflows/ci-cd.yml`, `app/bootstrap/gitlab_deployment.py`,
`tests/test_deploy_staging_services.py`, `tests/test_gitlab_deployment_settings.py`,
plus minimal `deploy.sh`/`rollback.sh` callers for safe retained-file errors.

- After generating protected role files, construct validation input using
  `gitlab_bundle_from_env_files`; CI and host must validate identical post-generator
  bytes, including trailing-newline normalization and multiline JSON. Host admission
  preserves already-generated role-file values exactly; it must not normalize a
  retained trailing newline a second time. CI normalizes raw source values once. Name the
  complete application bundle and filtered GitLab bundle distinctly. Derive
  container accepted-name input from the shell routing keys instead of maintaining
  a second Python literal; retain duplicate/unknown-name refusal. Test literal
  expected keys against routing and validator input, plus current/legacy outcome
  parity, so future key additions cannot silently drift.
- Retained files remain strict generator-format data, never shell-sourced. Expose
  only a static allowlisted basename (`api.env` or `gitlab.env`) and error category
  for read/format failures; do not forward paths, lines, values or raw stderr.
  Test unquoted and missing-final-newline records on GitHub-only retained files as
  well as malformed GitLab files, through deploy and rollback. R5 documents it.
- Remove the unused module CLI and its subprocess-only tests; production and direct
  unit tests both exercise `validate_environment`, while host/CI tests exercise the
  real heredoc and fallback. This removes the cwd-dependent unused `-m` path.
  Simplify the redundant accepted-key union/comment; extract one synthetic keyring
  helper per test module; rename the acceptance-only private-origin test; move sys
  import to module scope where still needed; parse the fake Docker command boundary
  instead of the magic `${@:1:9}` count. RED/GREEN parity examples include an active
  key ending in newline, multiline JSON, default-off and invalid values; all secret
  canaries remain absent from stdout/stderr/argv. Covers P3.1, P3.2, P3.6, P3.7,
  and the remaining P3.8 items.

### R4 — Restore safe actionable registry diagnostics (M)

Dependencies: R1. Files: `deploy.sh`, `rollback.sh`, `env-file.sh` if a shared
helper is justified, and `tests/test_deploy_staging_services.py`.

- Assess stderr rather than assuming all registry output is safe: capture it in a
  protected temporary file, classify recognized unauthorized/denied, missing
  manifest, disk-full and network failures into static actionable messages, and
  use a generic static fallback. Do not forward raw CLI stderr, credentials or
  token-bearing URLs; remove capture on every exit. Apply deploy/rollback parity.
- RED/GREEN recognized categories and unknown stderr with credential canaries;
  retain password-stdin and pre-mutation refusal. Fix obsolete `pull_policy: always`
  cleanup comments to describe the pinned `--pull missing` startup; remove the
  extra blank line. Covers P3.4 and P3.5.

Checkpoint R3–R4: focused deployment/settings regressions and secret-canary checks
pass, shared key knowledge and production validator path agree, diagnostics help
the operator without exposing raw external output.

### R5 — Complete the protected project-hook runbook (S)

Dependencies: R3–R4 and published #340 notice. Files: `docs/SECRETS.md`,
`docs/CICD.md` (preserve #283 sections).

- End initial setup commands at inspect; use interactive-safe error handling, and
  obtain/validate generation before the single separately gated activate command.
  Place project-hook setup after create/bind/inspect, then authorized activation,
  API admission if first onboarding, and real MR delivery. Existing installations
  keep their process flag; no broad disable or unrelated-workspace operation.
- Specify Settings → Webhooks, URL
  `https://<staging-api>/webhooks/gitlab/<installation_id>` using the create UUID,
  protected create JSON's `webhook_secret` as Secret token (`X-Gitlab-Token`), only
  Merge request events and enabled SSL verification. Require stable `webhook-id`
  or `Idempotency-Key` (both agree if supplied); verify test-instance support.
  Explain 202 in Recent events and matching installation receipt as intake proof
  only; replay that original event and compare run/publication counts. Revoke the
  specific installation before deleting its project hook, then verify shutdown.
- Document strict retained generator format and safe filename diagnostics; update
  quote/trailing-backslash refusal to the early CI preflight (before SSH and no
  rollback), and rename the CI step to reflect whole-bundle representation plus
  GitLab runtime validation. Verify shell example syntax, actual CLI flags,
  generation ordering and repo webhook contract; no live claim. Covers P2 thread
  `4387180613`, P3.2 documentation, P3.9 and P3.10 (step rename in R3/R5 handoff).

Provider UI/header/replay details checked against
[GitLab Webhooks](https://docs.gitlab.com/user/project/integrations/webhooks/).
Use the repository's existing secret-token authentication; newer GitLab signing
token advice does not authorize an authentication redesign. Do not substitute a
Test-generated event for identical original delivery replay or live MR acceptance.

### R6 — Make CLI help usable in the deployed image (M)

Dependencies: R0 and published #340 notice for env-example formatting.
Files: `app/gitlab_admin.py`, `tests/test_gitlab_admin.py`, `.env.example`.

- Use `python -m app.gitlab_admin` in help, show protected stdin/FD 0 for the
  container and distinguish local-process additional descriptors. Show
  `python -m app.bootstrap.seed_prompts` without uv in image instructions; preserve
  permission and deployment prerequisites. Wrap the long env-example comment.
- RED/GREEN help contract assertions; inspect all subcommand help and reconcile
  with runbook examples/image Python command. No new installation/credential flow.
  Covers P3.11 and P3.12.

### R7 — Freeze, verify, review and update the existing PR (M)

Dependencies: R0–R6. Files: existing plan/todo and existing PR metadata only.

- Run focused deployment/settings/admin suites, shell syntax and affected checks
  during each slice; retain RED/GREEN and legacy mutation evidence privately.
  Runnable focused command for reviewers:
  `uv run pytest tests/test_deploy_staging_services.py tests/test_gitlab_deployment_settings.py tests/test_gitlab_admin.py`.
  Freeze the final rebased candidate and source fingerprint, then execute the four
  required gates once: `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy .`, `uv run pytest`. Use a fresh disposable Linux runner, isolated
  real PG/Rabbit services, read-only source and a writable executable `/src/.venv`:
  nested uv fixture environments drop `UV_PROJECT_ENVIRONMENT`. Record zero-skip
  integration evidence; previous resources were cleaned up. Repeat a full run only
  for a changed candidate or unresolved failure, not for every slice.
- Fresh cumulative Standards/Spec review must account for all 18 findings and the
  rebase. Update existing PR #339 with `What / Why / How to verify / Refs`, #340
  coordination/order, literal runnable focused test command, historical-vs-current
  evidence and an honest current-main checkbox. Publish authorized fixes and
  inspect new CI; reply to each reviewer thread with the fix SHA and let its opener
  resolve it. New push requires approval on that head. No merge/issue closure.
- Preserve separately pending real staging acceptance (Task 7); local regression,
  hook docs and 202 receipts do not complete AC3/AC4/AC5. Covers P3.13–P3.15 final
  evidence and publication. Original successful gates/CI remain historical facts.
