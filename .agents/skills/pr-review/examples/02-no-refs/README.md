<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/pr-review/examples/02-no-refs/README.md -->

# 02 — no `Refs`: stop with the reason

**Source:** a live snapshot of ui#227, recorded on 2026-11-03 by saving the output of the
`gh api … --jq` call `pr_context.py` makes for the pull request, with no edits.

## Input

```sh
pr_context.py ui#227
```

ui#227 is merged. Its body is the pull request template left unfilled: one sentence above it, empty
`What` / `Why` / `How to verify`, and a bare `Refs #` with no number.

## Expected

- `verdict: stop`, exit code 1, `issues: []`.
- `reasons` says that the pull request has no `Refs` line naming an issue and what to add. A
  `Refs #` without a number names nothing.
- No issue is fetched: the fixture directory holds only `pr.json`, and any other call fails.
- One warning: the pull request is merged.

## Expected review

The skill reports the reason verbatim and ends. No axis runs and nothing is fetched or written.
