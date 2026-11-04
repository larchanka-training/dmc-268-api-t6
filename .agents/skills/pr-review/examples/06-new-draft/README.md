<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/pr-review/examples/06-new-draft/README.md -->

# 06 — a new draft with a thread on a line

**Source:** the answers GitHub gave on ui#130 in a live probe on 2026-11-04 (a draft created, threads added, read back, deleted). The shapes and the ids are theirs; the viewer and the texts are this case's.

## Input

```sh
draft_review.py ui#130 --findings findings.json --diff ../ui130.diff --apply
```

`--apply`, no draft on the pull request yet.

## Expected

- One `addPullRequestReview` (no `event`) with the head as `commitOID`, one `addPullRequestReviewThread` on the line, side `RIGHT`.
- `applied: true`, `review.state: PENDING` from the read-back, the thread `created`.
