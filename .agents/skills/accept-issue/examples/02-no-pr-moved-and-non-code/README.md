<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/accept-issue/examples/02-no-pr-moved-and-non-code/README.md -->

# 02 — no PR, a moved AC, a non-code AC, a visual AC

**Source:** synthetic (issue `larchanka-training/dmc-268-api-t6#500`, three AC, two DoD); see `../README.md` for the contract. The issue has no pull request at all (the self-check follows the last action, not a
merge) and the card is in In Progress.

## Input

```sh
acceptance.py table api#500 rows.json
```

- AC1 is visual: the evidence is a screenshot (an image link). Text evidence is the default;
  a screenshot only fits an AC that is about what the page looks like. That choice is SKILL.md's
  rule (T6); the script cannot tell a visual AC and does not enforce it.
- AC2 is non-code (a ruleset): the evidence is a link to the executor's comment that says what was
  done and how to check it.
- AC3 carries «перенесён в #285 решением техлида» in the body and is already ticked: the row is
  `moved`, the cell `перенесён в #285`.
- DoD1 is true at the moment of posting and has its own evidence (comment 2001).
- The viewer (`executor`) is not an assignee (`maintainer`).

## Expected

`verdict: ac_check`, `ac_check: true`, a dry run (`apply: false`, no write log): the card would go
In Progress → AC Check (`action: set`, `readback: null`). One warning:
`viewer executor is not an assignee (assignees: maintainer)`. No stop for the missing PR.
