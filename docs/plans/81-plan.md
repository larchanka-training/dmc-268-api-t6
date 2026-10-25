# Implementation Plan: remove unused reviewer intent / timeline (#81)

## Overview

Choose **A: remove the unused reviewer-intent / timeline code** from the backend.
The user requested deletion if this path cannot be implemented. Connecting it is
technically possible, but would add a product behavior without a requirement:
SYSTEM_DESIGN Р-10 / §6.1 use `ai-review` label plus successful CI, and Р-10 records
that requesting the App bot as reviewer does not produce the necessary event.
Issue #81 permits B only when a new product need exists; none is present in the
issue or current request. Removal fits the documented product behavior.

Source: https://github.com/larchanka-training/dmc-268-api-t6/issues/81.
Planning date: 2026-10-14. Human approved the entire plan, public test seams, ownership comments, and draft PR publication on 2026-10-14.

## Evidence and baseline

- PR #69 merged 2026-10-07T14:19:23Z as
  `dcae98372c696a662a90c6f58e40fbc8a70b44c8`; it is an ancestor of this baseline.
- Fetched `origin/main`; isolated worktree HEAD and remote main are
  `362f7586f797ae29b4f367a3bcbd745657924976` at planning time.
- Worktree: `<isolated-checkout>`.
  Original checkout is `fix/111-worker-portal-url`, with untracked plans for #113
  and #117. Keep its branch, files, and committed changes out of this work.
- `github_installation_dispatch.py` returns `IGNORED_IRRELEVANT_EVENT` for
  `review_requested` / `review_request_removed` before calling the projector.
- `ReviewsApiResources` constructs `ProjectGitHubPullRequest` with the current PR
  provider and projection lock, without a reviewer timeline provider.
- `HttpGitHubReviewerTimelineProvider` is instantiated only by tests.
- `_advance_reviewer_barrier` still runs on production metadata/lifecycle paths.
  Thus A also removes legacy clearing and barrier writes; this is a deliberate
  change to obsolete metadata, while active product behavior stays intact.

## Architecture decisions

1. Delete the HTTP timeline adapter, protocol, timeline value types, constructor
   argument, provider fetch phases, intent stale checks, `_apply_reviewer_intent`,
   timeline reconciliation, and legacy reviewer barrier state machine.
2. Ignore review-request actions at projector entry with `IGNORED_UNRELATED`,
   before acquiring a projection lock, opening a UOW, or reading GitHub. Preserve
   the existing dispatcher outcome and transport decoding of those actions.
   `requested_reviewer_login` can remain transport compatibility; removing it is
   outside this narrow cleanup.
3. Keep all seven `reviewer_*` model columns, `PullRequestRecord` fields, defaults,
   and `github_pull_request_projection.py` load/save mapping unchanged. No
   migration. Existing historical values are preserved by normal projection;
   labels, lifecycle, and metadata no longer modify legacy reviewer fields.
4. Preserve the current provider checkpoint, projection lock, source retirement
   checks, metadata/head stale handling, label reconciliation, cancellation
   reasons, and post-commit cancellation signaling. No DB transaction may span
   a current GitHub provider call.
5. Delete tests that specify removed reviewer/timeline behavior. Where a test also
   proves live guarantees, rewrite it around current snapshots or labels rather
   than dropping that coverage. Retain schema/migration tests and their historic
   field mappings; seed legacy data directly where assignment events were used.
6. Update only the `reviewer_requested` row in SD §16: code removed in #81,
   columns retained indefinitely by @axyi, historical values preserved, and
   production remains on the Р-10 / §6.1 label trigger.
7. No dependencies, schema changes, trigger changes, or frontend work.

## Ordered work and checkpoints

Tasks and acceptance criteria are in [81-todo.md](81-todo.md).

1. Add failing behavior tests for ignored reviewer deliveries and preserved legacy
   values; retain dispatcher coverage.
2. Remove application state machine and adapter; retire obsolete tests while
   retaining metadata, label, lifecycle, locks, cancellation, and migration coverage.
3. Adapt PostgreSQL retirement/provider tests to live current-provider behavior.
4. Remove the timeline request-path sink entry and document the decision in SD.
5. Run targeted PostgreSQL verification and all gates; review and publish only the
   #81 diff from a branch based on fresh main.

Checkpoint after tasks 1–2: failing tests now pass; no timeline symbols in app/tests;
existing live projection tests pass. Checkpoint after tasks 3–4: current-provider
retirement tests and path safety remain covered, schema/mappers unchanged, SD
matches the active trigger. Final checkpoint: every issue AC and gate verified.

## Open PR ownership coordination

Read-only inspection on 2026-10-14 found these open PR overlaps:

| PR | Owned files intersecting this plan | Required boundary |
| --- | --- | --- |
| [#154](https://github.com/larchanka-training/dmc-268-api-t6/pull/154) | `tests/test_github_repository_path.py`, `docs/SYSTEM_DESIGN.md` | Remove only timeline import/sink registration; SD §16 reviewer row only |
| [#148](https://github.com/larchanka-training/dmc-268-api-t6/pull/148) | `docs/SYSTEM_DESIGN.md` | SD §16 reviewer row only |
| [#124](https://github.com/larchanka-training/dmc-268-api-t6/pull/124) | `docs/SYSTEM_DESIGN.md` | SD §16 reviewer row only |
| [#133](https://github.com/larchanka-training/dmc-268-api-t6/pull/133) | None | No coordination comment needed |

Backend rules forbid editing another open PR's files without a comment there.
Authorized coordination comments were posted: #154 issuecomment-6176814769,
#148 issuecomment-6176815788, and #124 issuecomment-6176816780. Recheck
ownership if the PRs change or merge. Posting comments does not authorize editing
unrelated portions of their files.

**Proposed comment for #154:**

> Coordination for #81: after approval, we plan to remove the unused reviewer-intent / timeline path (option A). In `tests/test_github_repository_path.py` we will remove only the timeline adapter import and the `reviewer-timeline` sink registration, keeping all remaining request-path checks. In `docs/SYSTEM_DESIGN.md` we will update only the §16 `reviewer_requested` row to record removal and link #81. The seven legacy columns and mapper stay unchanged, and the `ai-review`/CI trigger remains as documented. We will preserve your commit-context and other changes when rebasing.

**Proposed comment for #148:**

> Coordination for #81: after approval, we plan to remove the unused reviewer-intent / timeline path (option A). The only planned change in `docs/SYSTEM_DESIGN.md` is the §16 `reviewer_requested` row: record code removal and link #81 while retaining the seven legacy columns and the `ai-review`/CI trigger. Your token-cache documentation changes will be preserved when rebasing.

**Proposed comment for #124:**

> Coordination for #81: after approval, we plan to remove the unused reviewer-intent / timeline path (option A). The only planned change in `docs/SYSTEM_DESIGN.md` is the §16 `reviewer_requested` row: record code removal and link #81 while retaining the seven legacy columns and the `ai-review`/CI trigger. Your documentation corrections will be preserved when rebasing.

## Risks and mitigations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Broad deletion loses live lifecycle/retirement tests | High | Rewrite mixed tests using current snapshots; retain labels, cancellation, serialization, source retirement, and post-token checkpoints |
| Legacy field writes survive in lifecycle helpers | Medium | Remove barrier logic and test literal non-default historical values across close/reopen, sync, metadata, and labels |
| Schema removal accidentally expands scope | High | Compare model, mapper, and all migrations against base; retain migration tests and no schema diff |
| Shared files conflict with active PRs | Medium | Authorized coordination comments, narrow edits, fresh-main rebase and ownership recheck |
| PostgreSQL tests silently skip | Medium | Run with `TEST_DATABASE_URL` and record explicit passes; default skipped run alone is insufficient proof for changed DB tests |

## Approval and completion

The user approved this entire plan, the public test seams, coordination comments,
and draft PR publication before development on 2026-10-14. The required comments
were posted before shared-file edits. Implementation and gate evidence is recorded
in [81-verification.md](81-verification.md). The tech lead checks issue acceptance and closes
#81; PR/commit text uses `Refs #81`, never closing keywords.


## Final verification and publication readiness

All five tasks and both implementation checkpoints are complete. Required gates
passed, including the full default suite (2796 passed, 617 opt-in integration
skips); the affected PostgreSQL cases passed separately (78 passed, zero skipped).
Independent final reviews returned Standards 0 and Spec 0 actionable findings;
reports are recorded in [81-review.md](81-review.md).

The publisher fetched current main and rebased `chore/81-reviewer-intent-path`
on `362f7586f797ae29b4f367a3bcbd745657924976` on 2026-10-14; the branch was
already current and source was unchanged. A fresh open-PR file inventory found
only the documented #154/#148/#124 overlaps, and all three ownership comments
were verified. The user-authorized publication is a draft with no reviewer
assignment. GitHub approval on the published head and resolved review threads
remain requirements before merge; internal agent reviews do not replace them.
