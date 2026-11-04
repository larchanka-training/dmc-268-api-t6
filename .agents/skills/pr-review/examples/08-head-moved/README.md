<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/pr-review/examples/08-head-moved/README.md -->

# 08 — the head moved after the review: stop

**Source:** the answers GitHub gave on ui#130 in a live probe on 2026-11-04 (a draft created, threads added, read back, deleted). The shapes and the ids are theirs; the viewer and the texts are this case's.

## Input

```sh
draft_review.py ui#130 --findings findings.json --diff ../ui130.diff --apply
```

The findings are about `21b8d99b`; the pull request's head is now another commit.

## Expected

- `verdict: stop`, exit code 1, the reason names both commits.
- No write, even with `--apply`.
