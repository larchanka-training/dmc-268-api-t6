<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/pr-review/examples/07-existing-draft/README.md -->

# 07 — the viewer's own draft is appended to

**Source:** the answers GitHub gave on ui#130 in a live probe on 2026-11-04 (a draft created, threads added, read back, deleted). The shapes and the ids are theirs; the viewer and the texts are this case's.

## Input

```sh
draft_review.py ui#130 --findings findings.json --diff ../ui130.diff --apply
```

The viewer already has a draft from an earlier run, with another summary and verdict, a handwritten comment and the thread of that run.

## Expected

- No second `addPullRequestReview`: one pending review per user per pull request.
- The thread already there is `exists` and not sent again; the new one is `created`.
- `updatePullRequestReview` replaces the earlier summary, no `event`; the read-back holds the new one. The handwritten comment is not touched.
- The earlier summary is known by the hidden checksum that ends it: a text changed by hand has no valid one and stops the run, with no write.
