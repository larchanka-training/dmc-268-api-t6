<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/pr-review/examples/09-out-of-scope/README.md -->

# 09 — findings out of scope, each with a decision

**Source:** the answers GitHub gave on ui#130 in a live probe on 2026-11-04 (a draft created, threads added, read back, deleted). The shapes and the ids are theirs; the viewer and the texts are this case's.

## Input

```sh
draft_review.py ui#130 --findings findings.json --diff ../ui130.diff --apply
```

Four findings out of scope with their decisions and two in scope.

## Expected

- A follow-up is one line of the review body, not a thread.
- «Принято» or «отклонено» is a `nit:` thread: on the line when it is in the diff, on the file when only the file is (`subjectType: FILE`), on the first file of the diff, naming its place, when its own file is not in the diff.
- Only the in-scope medium makes the verdict Request changes.
