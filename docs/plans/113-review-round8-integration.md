# PR #132 integration proof after policy/context merge (#113)

## Base and preserved review fixes

Main: `c9966270abb093207c4ace43b0fc4b7422921013` (merged #136).
Historical published PR checkpoint: `075004e996db907b19787c91322ff8b62e43825e`.
Rebased checkpoint: `e4c343e2298c01cb16aec9781183b11ba45b8f57`.

The thirteen staged review-fix files were restored with the index intact after the
rebase. Their file bytes matched the preserved pre-rebase snapshot exactly before
new integration changes. Historical round-seven counts apply to checkpoint
`075004e` plus that then-unpublished review-fix delta; they are not current-base
results. The evidence below covers the rebased checkpoint plus the migration,
fake and rollout corrections. Final source verification and publication must record
the eventual exact commit and CI head.

## Semantic conflicts

The rebase had conflicts in three files. `github_vcs.py` retains main's typed
`MergeBaseUnavailable` classification plus the mandatory origin checkpoints before
token acquisition and comparison HTTP. `test_github_vcs_provider.py` retains both
typed failure cases and the cancellation/live controls. `test_review_run_worker.py`
retains policy failure and skipped-outcome tests together with cancellation logging.
The GitHub cancellation exception remains distinct from typed merge-base failures.

Existing per-request GitHub VCS/blob/LLM/publication fences, successful GitHub review
receipts, installation-specific retry outcomes, GitLab routes and pipeline step
events remain integration requirements. New policy and AST network/persistence
seams require separate guarded-path proof before the complete-tree gates.

## Migration RED/GREEN

Alembic initially reported two heads: `20261010_0030` and `20261012_0030`.
The graph/upgrade selection ran before the graph correction: **five failed in
1.58 s**, all due to the two-head ambiguity (including the explicit graph check).

The single chain is now:
`20261007_0028` → `20261011_0029` → `20261012_0030` →
`20261010_0029` → `20261010_0030`.
Only the unmerged `20261010_0029` identity migration's `down_revision` and matching
header changed. Both merged policy migrations and public reinstall revision IDs
remain unchanged. Alembic reports exactly `20261010_0030 (head)`.

The graph and migration regressions, immutable-origin backfill/round-trip tests and
policy snapshot persistence tests passed: **19 passed, zero skips in 12.89 s**.
The data-bearing upgrade test now starts from both deployed main
`20261012_0030` and pre-policy `20261007_0028`, preserving pause, availability,
unknown account identities and workspace budget. Origin round-trip tests compare
all other Run fields and metadata against the integrated schema.

## Inherited unit RED/GREEN

The seven-file unit selection initially produced **six failed, 130 passed in
1.48 s**. All six failures were the newly merged context-stage VCS fake lacking
the mandatory checkpoint keyword. Its pull-request, diff and blob methods now
require and execute that checkpoint. No production API was weakened.
The same selection then passed: **136 passed in 1.03 s**.

The selection covers GitHub VCS/source, review worker, worker composition,
GitHub/GitLab publication and the policy context-stage processor.

## Focused PostgreSQL verification

The fifteen-file selection covers reinstall schema/HTTP/OAuth behavior, immutable
origin guards, VCS/context/diff/LLM/publication retirement, installation and deferred
receipt retries, webhook retry controls, PostgreSQL/broker worker queues and policy
snapshot persistence. It uses `uv run pytest -q --tb=short -rs` with dedicated
`TEST_DATABASE_URL` and `TEST_RABBITMQ_URL` infrastructure configured.
This selection started before the additional policy-write and network-origin
adaptations. It verifies the inherited rebased behavior plus the migration/fake
corrections, and does not claim verification of those later adaptations.
Result: **three failed, 335 passed, zero skips in 322.24 s**; two dependency
deprecation warnings were reported.

All three failures are the main-composition cases in
`test_worker_queue_postgres.py`: review through the gateway with and without
AGENTS.md, and the missing-LLM-configuration path. New context calls omit the
mandatory provider checkpoint (`ClassifiedVcsProvider.get_pull_request` or
`HttpGitHubVcsProvider.get_blob`), producing `internal_error` instead of the
expected publication or configuration outcome. These are recorded integration
failures for the separate context/network adaptations; this proof does not claim
a green combined PostgreSQL selection. They subsequently passed with zero skips in 15.21s (13 deselected); the
[context proof](113-review-round8-context.md) records the request-boundary correction.
The complete-tree gates remain separate verification.

Focused Ruff lint passes for the six modified Python files, format checks pass for
the five modified source/test files, and mypy reports no issues in five source files.
`git diff --check` passes. No full-suite result is claimed here.

## Rollout and remaining conditions

The current CICD runbook specifies the chain and full revision IDs. The
`20261010_0030` origin backfill still requires all old writers stopped **before
merge** and kept stopped until bootstrap finishes. The #126 revision admission
guard and fix-forward recovery remain mandatory. Downgrade of `20261010_0030`
loses origins; downgrade of `20261010_0029` loses account identity. A deeper
return to `20261007_0028` also drops policy snapshots/documents and deletes
conventions with a policy key. Historical proof counts were not rewritten.

The complete-tree four gates, fresh independent Standards/Spec reviews, exact-head
publication and required CI remain separate conditions. The inherited #136
dependency additions must be identified in the PR body. Latest-main verification,
current-head approval and agreed writer shutdown remain merge prerequisites.
