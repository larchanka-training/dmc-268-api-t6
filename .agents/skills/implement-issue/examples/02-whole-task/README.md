<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/implement-issue/examples/02-whole-task/README.md -->

# 02 — an issue without parts: take the whole task

**Source:** a real snapshot of api#287, recorded on 2026-10-29, unedited (no pull request
cross-references it, so there was nothing to trim).

## Input

```sh
preflight.py api#287
```

api#287 is OPEN, Todo on board 12, no assignee. Its only blocker, api#262, is CLOSED. `## Scope`
has no «Ч.N» bullet, so the issue is one task and one pull request. `## Acceptance Criteria` has
four criteria and no part prefix.

## Expected

- `verdict: go`, exit code 0, `selected: {part: null, reason: whole-task}`.
- `parts` and `task_prs` are empty; the four criteria are `common_acceptance_criteria`.
- The Kickoff is present, no warnings.
- Next: the skill reads the Kickoff and its read-first files, cuts the branch the Kickoff names
  (`fix/<n>-batch-robustness`, `<n>` = 287), moves the card to In Progress, and hands over to
  the implementation loop.
