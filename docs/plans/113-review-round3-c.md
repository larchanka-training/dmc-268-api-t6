# Developer C round-three documentation evidence (#113 / PR #132)

Worktree: `<worktree>`, branch `fix/113-pr132-round2`.
Initial published HEAD: `83a36290192369197f6f72f5b84a65f1add6d1d1`.
Base main: `834dd24c554e62d725060ecbaf19901dc709542e`.
Scope: Task C1 in [round-three plan](113-review-round3-plan.md), supplementing
[original plan](113-plan.md), [original todo](113-todo.md),
[review plan](113-review-plan.md) and [review todo](113-review-todo.md).
The coordinator posted narrow ownership comments before these edits to
#148/#137/#127/#124/#121/#126/#125. No application, migration, test, dependency,
A/B proof or shared round-three plan/checklist edits were made by C.

## Contract clarifications

- SYSTEM_DESIGN §6.9 now accepts the incomplete-comment receipt race: a successful
  review POST retains its `review_id`; cancellation before comments GET leaves an
  empty `comment_ids` tuple that does not distinguish unknown comments from none.
  A remove → re-add before `complete_publication` can yield `succeeded` and
  `published=true` findings without local `Comment` rows despite external inline
  comments. No new request starts while the origin is unavailable. This documents
  existing behavior and does not expand receipt identity/unknown-comment contracts.
- WEBHOOK_WORKER's archive deferral paragraph applies to PostgreSQL onboarding only;
  the GitHub tree GET and labels POST may already have happened.
- CICD's first-0030 rollout requires all old API/webhook-worker/worker and independent
  Run writers to stop **before merge #132 into main**, with timing agreed with tech
  lead @axyi. Automatic Deploy staging on main push explains that ordering; writers
  remain stopped through bootstrap and only new-image services resume.

Source checks: `GitHubPullRequestReviewGateway.submit_review` retains
`SubmittedReview(review_id, ())` on `RunCancelled` after POST; `SubmittedReview`
requires a tuple, and `SqlAlchemyRunLifecycleStore.complete_publication` rechecks origin,
conditionally inserts comments only for matching inline/comment counts, then marks
findings published. The installation projector builds inputs through its provider
phase before `SyncInstallationRepositories.execute` decides whether upsert defers.
`.github/workflows/ci-cd.yml` job `deploy-staging` permits non-PR main events and
runs after `push-image`. These files were read, not changed by C.

## Portable historical evidence and PR body

Personal worktree paths and local DSNs in `113-review-a.md`, `113-review-b.md`,
`113-review-plan.md` and `113-todo.md` became `<worktree>`, `<dev-db>` and
`<dev-broker>`. Historical SHAs, commands, results, counts and durations are preserved.
The original plan/todo and other historical 113 documents were swept; they had no
additional personal paths/DSNs requiring replacement.

A proposed full existing-PR body is prepared for the coordinator. It retains
What / Why / How to verify / Refs, all original/review/round-three plan/checklist
paths, explicitly historical 83a3629 gate results, and pending new tested/published
SHAs, final gates, A/B mutation evidence and independent review. It keeps companion
order #127/#125/#126/#121, #121 `RunTriggerOutcome` plus mandatory keyword-only
`expected_installation_external_id`, #127 fail-closed `AuthScope`, and the waiting
old-CI-receipt combined-head check. UI follow-up remains a separate prepared draft;
no issue creation is claimed. No commit, push, PR-body update, merge, service stop,
thread resolution or external write was performed by C.

## Historical rebase primary evidence

The current body snapshot was read and checked against git history and these
executed commands (all exit 0):

```sh
git range-diff 8cf3608..d6820df 46a9077..69505ab
git range-diff 46a9077..69505ab 9aedfa8..ed62aad
git range-diff 9aedfa8..76f8a1d 834dd24..83a3629
```

- #108 / main 46a9077: the feature patch changes SYSTEM_DESIGN conflict resolution
  to preserve the new Run/context/Finding/Comment and API contracts; installation
  identity/availability/origin additions coexist with those contracts.
- #134 / main 9aedfa8: the feature patch adds mandatory merge-base checkpoint
  parameters, fences token acquisition and compare GET, and adapts provider tests.
- #138 / main 834dd24: `vcs_diff.py` keeps the shared `HunkRow`, `parse_hunks` and
  `split_lf` imports alongside `RunCancelled`. The final range-diff shows the
  docs-publication and review-fix commit patches identical, but the feature patch
  changed. The PR body no longer describes the entire integration as import-only.

## Executed documentation verification

```sh
git diff --check -- docs/SYSTEM_DESIGN.md docs/WEBHOOK_WORKER.md docs/CICD.md docs/plans/113-review-a.md docs/plans/113-review-b.md docs/plans/113-review-plan.md docs/plans/113-todo.md
rg -n '/User[s]/|127[.]0[.]0[.]1|localhos[t]|postgresq[l][^ `]*://|amq[p][^ `]*://' docs/plans/113* <proposed-pr-body>
```

Diff check: exit 0, no output. Portable-reference sweep: exit 1, no matches.
Before adding this proof, the seven assigned tracked files had 34 insertions and
14 deletions; the diff was inspected to confirm only assigned sections and
placeholder replacements changed. This is prose-only scope with no new behavior
or source mutation; no additional test was needed for C's edits.

## Final combined verification — pending coordinator

C did not run tests concurrently with A/B temporary mutations. After both owners
restore production bytes, the coordinator must record actual results for:

- `uv run ruff check .` — pending.
- `uv run ruff format --check .` — pending.
- `uv run mypy .` — pending.
- `TEST_DATABASE_URL='<dev-db>' TEST_RABBITMQ_URL='<dev-broker>' uv run pytest -rs`
  — pending real counts/duration and zero-skip confirmation.
- Fresh independent Standards and Spec review — pending.

Companion integration and current-head human approval remain before-merge gates;
this document does not certify their completion.
