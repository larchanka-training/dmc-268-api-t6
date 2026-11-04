<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/implement-issue/examples/12-chast-parts/README.md -->

# 12 — parts written «Часть N»: the first part, not the whole task

**Source:** synthetic, in the shape of the live api#238 (2026-11-04): the Scope bullets are
`- Часть N (…): …`, the criteria `Часть N: …`. The text is shortened. api#225 has the same shape;
api#239 writes `- Часть N: …`, ui#147 and api#397 `- Часть N — …`. The issue is OPEN, Todo, with no
blockers, no sub-issues and no pull request.

## Input

```sh
preflight.py api#238
```

`Часть N` is a label like `Ч.N`, in the Scope and in the criteria. Before api#420 this issue got a
whole-task `go`: `parts` empty and the criteria of both parts in `common_acceptance_criteria`, so a
run would have implemented both parts in one pull request.

## Expected

- `verdict: go`, exit code 0, `selected: {part: 1, reason: first-todo}`.
- Two parts with their labels (`P3, tests`, `P3, api`) and one criterion each. `Вне объёма: …` is not
  a part; the criterion with no prefix is `common_acceptance_criteria`.
- Next: the skill reads the Kickoff, cuts the branch for Part 1 and hands over to the loop.
