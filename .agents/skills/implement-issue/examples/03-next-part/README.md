<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/implement-issue/examples/03-next-part/README.md -->

# 03 — Part 1 is merged: take Part 2

**Source:** synthetic, built from the real api#266, recorded on 2026-10-29. Edits: its blocker
api#272 is removed (the live issue is blocked, which would stop the run; the CLOSED api#262
stays), and the body of each cross-referencing pull request is reduced to the first line of its
`## What` and its `## Refs` lines.

## Input

```sh
preflight.py api#266
```

`## Scope` has «Ч.1 (PR в ui → копия в api)» and three bullets for «Ч.2», which are one part. Four
pull requests cross-reference the issue:

- ui#213, MERGED, `Refs larchanka-training/dmc-268-api-t6#266`; its What starts «Part 1 of
  larchanka-training/dmc-268-api-t6#266».
- api#382, MERGED, `Refs #266`; its What is the SYNC-copy form «SYNC copy of
  larchanka-training/dmc-268-ui-t6#213 (Part 1 of #266)».
- ui#177 and ui#184, MERGED, name «Part 1» and «Part 2», but their `Refs` line is
  api#268, so they do not count.

## Expected

- `verdict: go`, exit code 0, `selected: {part: 2, reason: first-todo}`.
- Ч.1 is `done`, with api#382 and ui#213 as its pull requests; Ч.2 is `todo` with none.
- The criterion without a prefix is `common_acceptance_criteria`; the five with «Ч.N:» are split
  between the parts.
- No warnings: every pull request with `Refs` names a part.
- Next (SKILL.md step 3): the Kickoff names no branch, so the candidate is `<type>/266-<kebab>`.
  A name is taken if it exists locally, on origin, or has any pull request; the first free one of
  `<name>`, `<name>-p2`, `<name>-2`, `<name>-3`, … is cut with
  `git switch --no-track -c <name> origin/main`. Then In Progress, then the loop (Part 2 of
  api#269).
