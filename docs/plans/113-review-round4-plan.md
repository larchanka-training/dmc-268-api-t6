# PR #132 round-four correction plan (#113)

## Scope and decisions

Address all four P3 observations from the 2026-10-12 19:08 UTC review on
`00b6178ce75f09734bd552b3ec442c50843491e9`, including the optional assertion.
No new production behavior, dependency, migration or frontend implementation.
Keep earlier plans and truthful historical test counts; make proof documents portable
and reader-facing.

## Historical integration checkpoint

Round-four main was `620c81d927cebf3deb0721e5dacb7b70b721ad7e`: #125 merged;
#121 and #126 were OPEN at that checkpoint.
Rebased onto current main before development: `e417c2ec5af2f7e5556614c38474743e31ebaa98`. #125 changes configuration and tests,
so run full gates on the resulting head; previous fingerprints/counts are historical.
Keep both CICD bootstrap paragraphs, including LLM and `PORTAL_URL`, and the first
0030 rollout/shutdown contract. Preserve #127 AuthScope and #108/#134/#138 behavior.

## Task 1 — integrate current main (dependency: none; medium)

Likely files: `docs/CICD.md` conflict, plus upstream #125 deployment/env/tests.
Acceptance: preserve both branches' documented contracts and all upstream files;
record actual base/head and range-diff; distinguish inherited changes from corrections.
Verification: conflict markers absent, `git diff --check`, focused #125 tests,
then full gates after tasks 2–3. If #121/#126 merge later, integrate actual combined main.

## Task 2 — make guard ordering observable (dependency: 1; small; developer A)

File: `tests/test_run_origin_guard_postgres.py`; test
`test_inactive_origin_cancellation_takes_priority_over_user_pause`.
Acceptance: its five literal origin cases keep their persisted outcomes and additionally
assert the `update_check_run` invocation: inactive origin cancellation reaches the
update boundary; active user pause does not. Keep no pipeline/retry/provider work.
Verification: deliberately move repository-disabled handling before origin-active:
the strengthened test must fail even though `finish` still rewrites persisted outcomes.
Restore exact source bytes, run the original implementation green with real PostgreSQL.
Record actual RED/GREEN commands/counts without machine-local evidence paths.

## Task 3 — proof and rollout instructions (dependency: 1; medium; developer B)

Files: historical `docs/plans/113*.md`, `docs/CICD.md`.
Acceptance: remove every private home/temp/log path and local service address from
113 proof documents, replacing them with portable descriptions/placeholders while
retaining historical commands, source scope, SHAs, counts and timings.
Make round-three final proof reader-facing. Preserve relevant verified source equivalence
and distinguish the historic checkpoint from the current correction.
CICD states that stopped API rejects webhook delivery; GitHub does not retry it
automatically; operator redelivers from App Advanced → Recent Deliveries after rollout, for deliveries from the past three days. Cite the official
[retry policy](https://docs.github.com/en/webhooks/using-webhooks/handling-failed-webhook-deliveries) and
[redelivery procedure](https://docs.github.com/en/webhooks/testing-and-troubleshooting-webhooks/redelivering-webhooks).
CICD states that an image lacking 0030 fails `alembic upgrade head` with an
unknown revision after that migration, blocking all three application services on
bootstrap success. Historical pre-#126 rollback could replace containers and cause
an outage; merged #126 rejects this target before application-container changes.
Recovery requires a fix-forward image knowing 0030. NULL-origin insertion applies
only to independent old writers outside Compose; the new origin-aware worker
cancels eligible nonterminal Runs as `installation_unavailable`, leaving terminal
history unchanged.
Keep before-merge writer shutdown, #125 LLM/PORTAL_URL and #126 rollback guards.
Verification: private-reference sweep, read runbook against scripts/workflow and
primary GitHub webhook documentation; `git diff --check`. No fabricated execution.

## Checkpoint — current-head correctness (dependencies: 1–3)

Freeze all mutation windows, then run `uv run ruff check .`,
`uv run ruff format --check .`, `uv run mypy .`, `uv run pytest -rs` with real
disposable PostgreSQL and RabbitMQ. Record actual collected integration cases,
counts and timings; zero skips required for configured execution. Fresh independent
Standards and Spec reviewers assess the full base-to-head change and new requirements.

## Task 4 — UI follow-up and publication (independent preparation; publish after gates)

Verified follow-up: [UI #105](https://github.com/larchanka-training/dmc-268-ui-t6/issues/105),
on course board 12 with Todo status; [API #106 tracker reference](https://github.com/larchanka-training/dmc-268-api-t6/issues/106#issuecomment-6131037958).
Link UI #105 in PR #132. No UI source edits in this round.

Update PR body What / Why / How to verify / Refs to remove work-session diary,
retain accepted product limitations, name all plan/todo documents, and give actual
head/base verification. Commit intended files with normal hooks and Refs #113;
use verified remote head plus explicit lease, abort on drift. Publish one concise response with commit/evidence to all four P3 observations
in the general review body and the UI follow-up. Reviewers resolve their own threads.
Monitor required CI.

## Before-merge prerequisites and risks

After #121/#126 merge, rebase #132 last on actual combined main. Preserve #121
`_enqueue` returning `RunTriggerOutcome` together with keyword-only
`expected_installation_external_id`; verify waiting CI receipt for the old head.
Repeat combined-head gates/CI and obtain approval on that head. Do not claim this
future integration or old-receipt behavior verified while companions remain open.
Stopping old API/webhook-worker/worker and independent writers before merge and
coordinating merge time with axyi remain operational prerequisites, not actions here.
Writer shutdown, deployment, final-head approval and merge remain operational prerequisites.
