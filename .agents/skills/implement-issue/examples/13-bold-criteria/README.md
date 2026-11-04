<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/implement-issue/examples/13-bold-criteria/README.md -->

# 13 — a bold Scope and bold criteria: stop

**Source:** synthetic, in the shape of the live ui#171 (2026-11-04): the Scope bullets are
`- **Часть N (…) — title.**`, the criteria `**Ч.1, state.** …` and `**Ч.2.** …`. The text is shortened.
ui#160 has the same Scope with criteria `Часть N: …`. The issue is OPEN, In Progress, with no
blockers, no sub-issues and no pull request.

## Input

```sh
preflight.py ui#171
```

A bold criterion names its part, but bold is not read in the Scope (as in example 11), so `parts` is
empty and the two prefixed criteria are reported in `warnings` as naming parts not in the Scope. The
criterion with no prefix stays in `common_acceptance_criteria`. Before api#420 this issue got a
whole-task `go`, all three criteria in the common list.

## Expected

- `verdict: stop`, exit code 1, `selected: null`.
- One reason: the criteria name parts (1, 2) but the Scope has no part bullets the script can read —
  the issue's author or the tech lead rewrites them as `- Ч.N (…): …`, numbered from 1; the run does
  not edit the issue.
- The skill reports the reason and ends.
