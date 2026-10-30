# Author mode — the self-check table

<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/accept-issue/references/author.md -->

The executor of the issue (the author, `docs/CONTRIBUTING.md` §1) checks every AC and DoD after the
last PR of the task is merged (a task without a PR: after the last action) and posts one table
«AC → доказательство». Checkboxes are not ticked by the executor. AC Check is set only when no row
is «не выполнено». A task in parts is self-checked once, after the last part.

## 1. Know the rows

`issue_facts.py REF` prints `checkboxes.ac.items` and `checkboxes.dod.items`. The row ids are
`AC1..`, `DoD1..` in that order. The item text in the table always comes from the body: do not
retype it. Only the evidence is yours. Rows are required for every AC and DoD and for nothing else.

## 2. Pick the evidence

| Row                                    | Use                                 | Example                                                        |
| -------------------------------------- | ----------------------------------- | -------------------------------------------------------------- |
| Code AC                                | PR, commit, test name, a run        | `PR #901, тест test_health_returns_version`                    |
| DoD «CI зелёный», «PR одобрены»        | The PR and the fact                 | `CI зелёный на голове PR #901; approve ndovnar, треды закрыты` |
| Non-code AC (ruleset, board, live run) | A **link to your evidence comment** | `комментарий <url>: правило включено, вывод gh api …`          |
| Visual AC (what a page looks like)     | A screenshot link — only here       | `скриншот: ![статус](https://…)`                               |
| Moved AC                               | `{"status": "moved", "to": "#285"}` | the body item must say «перенесён в #285 решением техлида»     |
| Not shown                              | `{"status": "not_done"}`            |                                                                |

- Evidence must be something the acceptor can open: a merged PR, a commit on `main`, a CI run
  link, an issue or PR comment, or quoted command output together with the command. A local file
  path (`/home/…/SKILL.md:112`), an unpushed commit or the state of your worktree is not evidence;
  the row is `not_done` until the work is merged. Then post a new table.
- Text is the default. A screenshot is for an AC about appearance; for everything else it proves
  less than a command output. The script cannot tell the difference — you decide.
- Evidence that starts with «не выполнено» or «перенесён в #M» is read as that status by the table
  reader, so a `done` row with such evidence is a stop. Write another sentence.
- A non-code AC needs the action done and a comment posted **before** the row cites it. If you
  need admin rights, ask the tech lead and wait; the row stays `not_done` meanwhile.
- `|` in a cell is escaped by the script; keep each evidence to one or two lines.

## 3. `rows.json`

```json
{
  "AC1": { "status": "done", "evidence": "PR #901, тест `test_health_returns_version`" },
  "AC2": { "status": "not_done" },
  "AC3": { "status": "moved", "to": "#285" },
  "DoD1": { "status": "done", "evidence": "CI зелёный на голове PR #901" }
}
```

Write it under `$(mktemp -d)`, never in the repo.

## 4. Dry run, yes, apply

```bash
python3 .agents/skills/accept-issue/scripts/acceptance.py table REF "$TMP/rows.json"
python3 .agents/skills/accept-issue/scripts/acceptance.py table REF "$TMP/rows.json" --apply
```

The dry run prints the exact comment and the card move; show it with all `warnings`. The second
line only after the user's explicit yes.

| Verdict    | Meaning                                                                            | Card                                                                                     |
| ---------- | ---------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| `ac_check` | no «не выполнено»                                                                  | On Review / In Progress → AC Check (already AC Check: stays)                             |
| `not_done` | at least one row                                                                   | On Review → In Progress; In Progress: stays; AC Check: **left**, the acceptor returns it |
| `stop`     | closed issue, rows do not match the body, no AC/DoD in the body, card in Todo/Done | none                                                                                     |

## 5. After `not_done`

Rework stays in the same issue. Code AC: a new PR with `Refs #N`. (`implement-issue`'s
pre-start stops today on a task whose parts are all merged or that already has a PR: a rework PR
is the user's decision there, so tell the user and let them start it.) Non-code AC: the action and
an evidence comment. When the
PR is merged or the action done, post a **new full table** (the latest table comment wins) and
the AC Check follows from the same command. No follow-up issue, no «Хвосты приёмки» — Rule 1.

## 6. Mistakes that cost a round

- Self-checking after each part of a task in parts; check once after the last.
- Ticking boxes yourself, or setting AC Check by hand when a row is «не выполнено».
- A row for an item the body does not have, or a missing row: the script stops; fix the rows, not
  the issue.
- Citing a comment that does not exist yet.
