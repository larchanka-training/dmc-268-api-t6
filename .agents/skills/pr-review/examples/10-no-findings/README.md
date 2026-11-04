<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/pr-review/examples/10-no-findings/README.md -->

# 10 — no findings: Approve is proposed, never set

**Source:** the answers GitHub gave on ui#130 in a live probe on 2026-11-04 (a draft created, threads added, read back, deleted). The shapes and the ids are theirs; the viewer and the texts are this case's.

## Input

```sh
draft_review.py ui#130 --findings findings.json --diff ../ui130.diff --apply
```

No findings, every criterion covered.

## Expected

- The proposed verdict is Approve, in the text only.
- The one write is the draft itself, with no `event`: Submit stays the reviewer's.
