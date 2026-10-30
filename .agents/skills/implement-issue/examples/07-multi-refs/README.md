<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/implement-issue/examples/07-multi-refs/README.md -->

# 07 — a pull request with several `Refs` is read per issue

**Source:** synthetic. The issue is a skeleton in the shape of api#168 (six parts, `Часть N` there,
relabelled `Part N` here so that the Scope reads as parts), OPEN, with no blockers. The two pull
requests are the real api#346 and api#353 (merged 2026-10-27 and 2026-10-28), with the body reduced
to the first line of `## What` and the `## Refs` lines.

## Input

```sh
preflight.py api#168
```

- api#346, `Refs #168`, `Refs #130`, `Refs larchanka-training/dmc-268-ui-t6#199`: three issues. Its
  What opens «The trace and output items of #168 part 6 (PR F3 of the #130 remainder)».
- api#353, `Refs #130`, `Refs #168`: two issues. Its title ends «#168 part 4 (#130)».

## Expected

- `verdict: go`, exit code 0, `selected: {part: 1, reason: first-todo}`.
- Ч.6 is `done` with api#346 and Ч.4 is `done` with api#353: with several issues in `Refs`, a part
  counts for the issue it is tied to, and `#168 part 6` ties it to api#168. Parts 1, 2, 3 and 5 are
  `todo`. No warnings.
- The forms that tie a part to an issue are `Part K of <ref>` and, in front of it, `<ref> part K`
  (`Ч.K`, `часть K`); the ref may be wrapped in `**…**`, backticks or a markdown link.
- Case `08` reads the same two pull requests for api#130.
