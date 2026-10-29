<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/implement-issue/examples/05-explicit-part/README.md -->

# 05 — explicit part: take Part 2 although Part 1 is open

**Source:** the same fixture as `04-part-pr-open` (synthetic, from api#266).

## Input

```sh
preflight.py api#266 --part 2
```

## Expected

- `verdict: go`, exit code 0, `selected: {part: 2, reason: explicit}`.
- The same `parts` as case 04: Ч.1 `open_pr` with ui#213, Ч.2 `todo`.
- No warnings: an explicit part is always taken, and Ч.2 has no pull request of its own. (A part
  with a merged or open pull request would add a warning naming it.)
- Next: as in case 03, for Part 2.
