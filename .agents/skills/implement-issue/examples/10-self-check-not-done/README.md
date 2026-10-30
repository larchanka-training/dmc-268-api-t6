<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/implement-issue/examples/10-self-check-not-done/README.md -->

# 10 — the self-check found unmet AC: stop, rework first

**Source:** synthetic: the fixture of `06-accept` plus one comment, the table that accept-issue's
author mode posts. Its text is what `render_table` of `accept-issue/scripts/acceptance.py` prints
for two rows (AC1 done, AC2 «не выполнено», card On Review); the comment id and the time
(2026-10-30T21:40Z, after the last merge at 21:35Z) are made up.

## Input

```sh
preflight.py api#266
```

The latest accept-issue comment is `<!-- accept-issue:table v1 -->` and ends with the line
«Итог: не выполнено — AC2. AC Check не ставлю, доработка идёт в этом же issue.» A table whose
last line is «Итог: «не выполнено» нет, ставлю AC Check.» does not hold anything back (case 06).

## Expected

- `verdict: stop`, exit code 1, `selected: null`, not `accept`.
- One reason: the self-check found unmet AC, with the link to the table comment, and no pull
  request with `Refs` has been merged since — «rework first: --part K for a part».
- `held` is `{kind: unmet_table, url: <the table comment>}`: the rework covers the unmet rows.
- The skill reports the reason and ends. A code AC is reworked with a new pull request:
  `implement-issue api#266 --part K` for the part it belongs to (a task without parts: by hand).
  Once a pull request with `Refs` is merged after the table, or a newer table without unmet rows
  is posted (the usual end of a non-code rework), the verdict is `accept` again.
