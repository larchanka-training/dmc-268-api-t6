# Output and comment formats

<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/accept-issue/references/formats.md -->

The exact contract is `examples/README.md`; every key and text there is pinned by a golden case in
`examples/NN-<case>/step-K/expected.json`. This page is the short reading guide.

## stdout of `acceptance.py`

One JSON document, schema 1, keys sorted. Exit 1 when `verdict` is `stop`.

- `table`: `verdict` (`ac_check` | `not_done` | `stop`), `ac_check`, `comment`, `rows`, `board`,
  `warnings`, `stops`, `rework_rule`, `next`, `viewer`, `apply`.
- `accept` / `return`: `verdict` (`accept` | `return` | `stop`), `comment`, `rows`, `table`
  (`author`, `comment_id`, `url`, `via` = `marker` | `flag`), `tick`, `return_items`, `after_table`,
  `followups`, `board`, `readback`, `warnings`, `stops`, `rework_rule`, `next`, `viewer`, `apply`.
- `board` = `{action, from, to, readback}`: `set` (written with `--apply`), `none`, `left`
  (an unmet row while the card is in AC Check: not moved).
- `next` is one sentence for the model; `rework_rule` is the neutral rule text (Rule 1).

## The three comments

Each starts with a hidden marker that only locates it; the visible text is the truth.

| Marker                              | Heading                                | Content                                                                                   |
| ----------------------------------- | -------------------------------------- | ----------------------------------------------------------------------------------------- |
| `<!-- accept-issue:table v1 -->`    | `## Самопроверка: AC → доказательство` | `\| # \| Пункт \| Доказательство \|`, one row per AC and DoD, then `Итог: …`              |
| `<!-- accept-issue:accepted v1 -->` | `## Приёмка: issue закрыт`             | link to the table, one line per AC and DoD, optional `Follow-up вне AC (новый скоуп): #M` |
| `<!-- accept-issue:returned v1 -->` | `## Приёмка: возврат в In Progress`    | link to the table, `**Что осталось**` list, the rework paragraph                          |

- The latest table comment wins; each one is complete. A comment of the script is not posted again
  when it is the latest script comment and says the same.
- A moved AC appears in the closing comment once, as its body item reads.
- Evidence cells: the evidence, or literally `не выполнено`, or `перенесён в #M`.

## Files the script writes or reads

Only through `gh api`: `POST …/comments`, `PATCH …/issues/N` (body; state), and the board mutation
of `board_status.py`. Under the offline seam the same writes are JSON lines in
`$ACCEPT_ISSUE_WRITE_LOG`; `--apply` without that variable is exit 3.
