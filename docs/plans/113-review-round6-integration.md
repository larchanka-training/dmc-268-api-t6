# PR #132 integration proof after GitLab publication (#113)

## Tested base and scope

Main: `2251b5936822827fc75b38cfc6b352c6a5e89a6b` (merged #137).
Rebased checkpoint: `d88725771ef89a94756ea3a06befd37b44b425d4`.
The evidence below applies to that checkpoint plus the publication integration
changes and strengthened test described here. A subsequent publication commit and
required CI must identify their own exact head. Historical round-five results
remain evidence only for `b5e0cec92c24a8e702a90e75de5a10350f89cdaa`.

The rebase retains GitLab routing, stored overview, partial-coordinate fallback,
shared comment rendering, `PublishError`, run-step notifications and listeners.
It also retains immutable origins, every existing GitHub request checkpoint,
receipt retention after a successful POST, origin-fenced findings/comments writes,
mandatory #121 receipt retry outcomes and #126 rollback revision admission.

## RED and minimal correction

Inherited publication, GitLab, findings, run-step, stream and publication-origin
regressions ran first against real PostgreSQL with the test broker configured:
**15 failed, 148 passed, zero skips in 34.68 s**.

Fourteen failures exposed missing `check_run` arguments when installation origins
are unavailable or retire at a provider checkpoint. The fifteenth exposed an old
GitHub-only readiness guard that incorrectly intercepted GitLab without GitHub
configuration. Correcting these integration points yields **163 passed, zero skips
in 30.38 s** on the same inherited selection.

Inactive-origin terminalization explicitly disables check-run publication before
route/configuration selection. Checkpoint cancellation uses the selected route's
check-run policy; the GitHub report remains origin fenced. Gateway readiness now
uses the merged provider route. The GitLab success regression additionally covers
both configured and unconfigured GitHub adapters, extending the already observed
readiness failure with a positive control.

An active SQL publication context always has a provider installation external ID:
`provider_installations.external_id` is non-null, and `publish_context` supplies
`None` only when its same `origin_active` condition is false. That condition
terminalizes before submission. Retain the assertion for active GitHub and GitLab
contexts; there is no new sentinel installation identity.

## Focused verification

The expanded selection covers publication/GitLab/findings; run-step and live/replay
streams; context/diff/LLM/VCS/origin guards; installation and deferred receipt retries;
worker composition and PostgreSQL queue processing; OpenAPI, conventions and fast
engine persistence. It uses `uv run pytest -q --tb=short -rs` with both
`TEST_DATABASE_URL` and `TEST_RABBITMQ_URL` configured against dedicated infrastructure.
Result: **537 passed, zero skips in 179.65 s** across 22 test files.
Two FastAPI/Starlette dependency deprecation warnings were reported.

Focused `uv run ruff check`, `uv run ruff format --check` and `uv run mypy` pass
for `publish_run_review.py`, `github_review_publication.py` and
`test_publish_run_review.py`; mypy reports no issues in three source files.
`git diff --check` passes.

## Source preservation

Comparison with the historical verified source fingerprints shows that every
modified pre-existing production path is among #137's modified source paths.
The origin helper, GitHub run context and blob adapters, LLM gateway and transport,
receipt trigger/installation dispatch and rollback script/checker/workflow remain
byte-identical to the historical verified head. No dependency or migration changed
during this integration. Shared action insertion preserves origin-fenced context,
model-output and publication persistence around the new commit-bound notifications.
Provider calls remain outside database transaction boundaries.

## Remaining verification

The [final proof](113-review-round6-final.md) records all four repository gates and
fresh independent Standards/Spec reviews. Exact-head publication and required CI
are recorded in the PR body and checks. Current-head human approval, latest-main
verification and agreed writer shutdown remain merge prerequisites.
