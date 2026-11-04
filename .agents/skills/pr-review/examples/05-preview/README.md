<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/pr-review/examples/05-preview/README.md -->

# 05 — preview: nothing is written

**Source:** the answers GitHub gave on ui#130 in a live probe on 2026-11-04 (a draft created, threads added, read back, deleted). The shapes and the ids are theirs; the viewer and the texts are this case's.

## Input

```sh
draft_review.py ui#130 --findings findings.json --diff ../ui130.diff
```

No `--apply`. One finding on a line of the diff, three criteria.

## Expected

- `verdict: go`, exit code 0, `applied: false`, `review: null`, the thread with `status: new`.
- The proposed verdict is Request changes: a high finding.
- No write: the case has no `expected-writes.jsonl` and no mutation fixture.
