<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/accept-issue/examples/README.md -->

# accept-issue golden examples — the contract of `acceptance.py`

These examples are written before the script (api#266 Part 2). They are the spec for
`scripts/acceptance.py`: the test runs every `NN-name/step-K/` offline and compares stdout and
the write log byte for byte. Rules: api `docs/CONTRIBUTING.md` on `f223cf3f` — §2 steps 8–9
(L56–57), the board notes (L61–63) and «Приёмка задачи» (L66–85; «Кто закрывает» L73, the
Development trap L85). User-visible texts (comments, `rework_rule`, stops) cite rule names only,
never line numbers.

All cases use one synthetic issue, `larchanka-training/dmc-268-api-t6#500`; nothing here is a
recording of a live issue (`real-272` is recorded separately, read-only, for a unit test).

## Layout

```
NN-name/README.md          what the case shows (starts with the SYNC line, a blank line, an H1)
NN-name/step-K/args.txt    one line: the `acceptance.py` arguments, shell-split (shlex)
NN-name/step-K/fixtures/   the offline seam (below)
NN-name/step-K/rows.json   the author's input, when the command is `table`
NN-name/step-K/expected.json          stdout, byte for byte
NN-name/step-K/expected-writes.jsonl  the write log; absent = no write
```

A case has several steps when the state changes between runs (case 01) or when one case shows a
family of runs (07, 08). The test runs a step from its own directory (so `rows.json` resolves),
with `ACCEPT_ISSUE_FIXTURE_DIR=<step>/fixtures` and `ACCEPT_ISSUE_WRITE_LOG=<tmp file>`. Expected
exit code: 1 when `verdict` is `stop`, else 0. `stderr` is empty. The JSON files carry no SYNC
marker; `diff -r` covers them. `args.txt` is the source of truth; the `sh` blocks in the case
READMEs repeat it for the reader.

## Commands

```
acceptance.py table  REF ROWS_JSON [--apply]                       author: the self-check table
acceptance.py accept REF [--table-comment ID] [--followup REF]... [--apply]   accepter: tick, close
acceptance.py return REF [--table-comment ID] [--fail ID=reason]... [--apply] accepter: send back
REF := api#N | ui#N | OWNER/NAME#N | N        (a bare N is api#N)
```

Without `--apply` nothing is written: the output shows the verdict and the exact comment. With
`--apply` everything is revalidated first and a `stop` writes nothing. Exit 0 ok, 1 stop, 2 usage,
3 gh / API / data error (also `--apply` in fixture mode with `ACCEPT_ISSUE_WRITE_LOG` unset).

`rows.json` — only the evidence comes from the author; the item text always comes from the issue
body:

```json
{"AC1": {"status": "done", "evidence": "PR #901, тест ..."},
 "AC2": {"status": "not_done"},
 "AC3": {"status": "moved", "to": "#285"},
 "DoD1": {"status": "done", "evidence": "..."}}
```

A row is required for every AC and DoD of the body and for nothing else (ids `AC1..`, `DoD1..` in
body order). `done` needs a non-empty `evidence`; `moved` is allowed only when the body item
carries «перенесён в #M» with the same `#M` (otherwise a stop: only the tech lead moves an AC).

## Output (stdout, `sort_keys`, indent 2, no ASCII escaping)

`table`: `ac_check`, `apply`, `board`, `comment`, `issue`, `mode` (`author`), `next`,
`rework_rule`, `rows`, `schema` (1), `stops`, `verdict` (`ac_check` | `not_done` | `stop`),
`viewer`, `warnings`.

`accept` and `return`: `after_table`, `apply`, `board`, `comment`, `followups`, `issue`, `mode`
(`accepter`), `next`, `readback`, `return_items`, `rework_rule`, `rows`, `schema`, `stops`,
`table`, `tick`, `verdict` (`accept` | `return` | `stop`), `viewer`, `warnings`.

- `rows` — `{id, kind (ac|dod), text, status, evidence}`; `evidence` is the table cell (`не
  выполнено` / `перенесён в #M` for the other two statuses). In `table` the status is `done`,
  `not_done` or `moved`. `accept` / `return` read the rows from the table comment and match them
  to the body, so there a row can also be `missing` (no row or no evidence for the item) or
  `stale` (the item text in the body no longer starts with the text in the table); both go to
  `return_items`. The same rows apply to AC and DoD.
- `comment` — the exact text that `--apply` posts (`null` on a stop, and on `accept` with verdict
  `return`, which posts nothing).
- `board` — `{action, from, to, readback}`. `from` is the Status read before; `to` the target;
  `action` is `set` (a write is made, or made with `--apply`), `none` (already there, or no move
  is part of the command) or `left` (`table` found a «не выполнено» with the card in AC Check:
  not moved, the acceptor returns it). A dry run shows `action: set` and `readback: null`;
  `--apply` reads the Status back. `next` differs for `left` (case 01, step 3).
- `rework_rule` — the fixed text below whenever the run has a «не выполнено» / returned item;
  else `null`. It states the rule; it never says that anybody asked for anything. The sentence
  «ваша просьба расходится с правилом» belongs to SKILL.md and is said only when the user did ask
  for a follow-up on an unmet AC or DoD (case 07).
- `table` — `{author, comment_id, url, via}`, `via` = `marker` | `flag` (`--table-comment`).
- `tick` — ids of the unchecked boxes that `accept --apply` ticks (checked ones are left alone).
- `followups` — `{ref, parent}` of each `--followup`, as read from GitHub (the follow-up's native
  parent, fixture `parent-<owner>_<name>-<number>`).
- `return_items` — `{id, text, reason}`, in body order: `не выполнено`, or `доказательство не
  подтвердилось: <--fail text>`, or a table gap.
- `after_table` — `{id, author, excerpt}` for every comment after the table (not written by this
  script): the human judges «замечания без ответа».
- `readback` — after `accept --apply`: `{state, last_closer, checked, total, unchecked}` from a
  fresh facts run; the close is good only when `state` is `CLOSED`, `last_closer` is `manual`,
  `unchecked` is 0, the count of boxes outside AC / DoD is unchanged and the body reads back as
  the ticked body. GitHub may not show the close at once, so the read-back is tried up to 3 times,
  1 s apart (no pause under the offline seam). When it still fails the run exits 3 and the error
  line names the writes already made, e.g. `(written: patch-body, comment, close)`; nothing is
  rolled back, and a rerun finds the issue closed. The same holds for every exit 3 after the first write of a run (a failing gh call, a missing fixture, a card read-back that differs), in `table`, `accept` and `return`: the one JSON error line ends with `(written: comment, set-status)`-style kinds, in order (`comment`, `patch-body`, `close`, `set-status`).

`rework_rule` text (Russian):

> Невыполненный AC или DoD доделывается в том же issue: кодовый — новым PR с `Refs #N`, некодовый — действием и доказательством (правило «Доработка — в том же issue», `docs/CONTRIBUTING.md`, «Приёмка задачи»).

### Stops and warnings

A stop gives `verdict: stop`, exit 1, nothing written even with `--apply`. Checks run in this
order and the first stop returns at once; fields of later steps stay at their defaults
(`rows: []`, `table: null`, `tick: []`, `board: {none, null, null, null}`):

- `accept` / `return`: (1) the issue is CLOSED (only this stop is reported); (1b) the body has
  no AC or DoD checkbox (`… there is nothing to accept` / `… to return`: an empty body is never
  accepted, whatever table is given); (2) an OPEN PR whose `Refs` names the issue; (3) no table: no marker comment and no `--table-comment ID`; (4) the
  table is read; (4b) a returned comment is newer than that table (`таблица старше возврата #id: …`: a stop for `accept`, a warning for `return`) and the verdict is computed; (5) `--followup` with verdict `return` (a follow-up for
  an unmet AC or DoD is not filed), then each follow-up's native parent is not this issue, then a
  box that cannot be ticked safely (`cannot tick AC1: line 3 of the body is not a plain
  checkbox`, found in the dry run too); (6) the board Status is read, warnings are collected.
  Also: `return` with nothing to return, and `--fail ID` for an ID the body does not have, are
  stops; `accept --apply` on a table whose verdict is `return` is a stop («the verdict is return,
  not accept … run return»), while the dry run reports `return` with exit 0. Every validation
  runs before the first write.
- `table`: (1) the issue is CLOSED; (1b) the body has no AC or DoD checkbox; (2) the rows do not
  match the body (a missing or extra id, a `done` without evidence or whose evidence reads as
  «не выполнено» / «перенесён в #M» in the table, `moved` without «перенесён в #M» in the body
  item); (3) the board is read, and AC Check requested with the card in Todo, Done or no Status is
  a stop.

Warnings (never stops; D3: the script does not decide who the tech lead is):

- `table`: viewer is not an assignee; an OPEN PR whose `Refs` names the issue (the self-check
  follows the last PR of the task).
- `accept` / `return`: the card is not in AC Check (names the Status); the viewer wrote the table
  or is an assignee; a PR with unresolved threads or a CI state other than SUCCESS; the table
  author is not an assignee; every row accepted as `moved`, which rests on the note in the body
  (the executor can edit the body too: check that the tech lead wrote it).

### How a table comment is read

The marker path is strict: the latest comment that starts with the table marker (by anybody),
every table row must be `| id | text | evidence |` with an id of this issue, otherwise a stop. The
`--table-comment ID` path is tolerant, by this exact rule: a row belongs to the item whose id
(`AC<n>` / `DoD<n>`, any case, `*` and `_` ignored) is the whole first cell; failing that, to the
first item for which some cell other than the last has the same text (backticks and `*` removed,
blanks collapsed, case ignored), or is at least 12 characters long and a prefix of that text. The
evidence is the last cell. The first row that matches an item wins; rows that match none are
ignored; an item with no row is returned as «в таблице нет строки», never passed. An evidence
cell is `не выполнено` when it starts with those words (bold allowed), `moved` when it starts
with «перенесён в #M» (the body item must carry the same note, else it is returned), empty is a
gap, anything else is `done`.

A comment of this script is not posted again when it is the latest script comment (any of the
three markers) and has the same text (compared stripped). A newer comment of another marker, for
example a returned comment after the table, makes a repeat legitimate.

## The three comments

Each starts with a hidden marker (it only locates the comment; the visible table is the truth),
so a rerun finds and does not duplicate it. The latest marker comment wins. The exact texts are
in the `comment` fields of `expected.json`:

- `<!-- accept-issue:table v1 -->` — `## Самопроверка: AC → доказательство`, the table
  `| # | Пункт | Доказательство |` (one row per AC and DoD; `|` in a cell is `\|`, line breaks become
  blanks), then a line `Итог: …` (it says that the card stays in AC Check when it is there and a
  «не выполнено» was found).
- `<!-- accept-issue:accepted v1 -->` — `## Приёмка: issue закрыт`, a link to the table, one line
  per AC and per DoD (`- AC1 — <text> — выполнено`; a moved AC is `- AC3 — <text>`: its body item
  already says «перенесён в #M», so it is said once), and, with `--followup REF`,
  `Follow-up вне AC (новый скоуп): <ref>`.
- `<!-- accept-issue:returned v1 -->` — `## Приёмка: возврат в In Progress`, a link to the table,
  `**Что осталось**` with one line per returned item, and the rework paragraph.

## The offline seam

`ACCEPT_ISSUE_FIXTURE_DIR` — every request reads `<dir>/<name>.json` (the full `{"data": ...}`
response); gh is never called. Names: `issue-p<k>` and `threads-…` (the Part 1 facts),
`acceptance-p<k>` (viewer login, body, assignees, comments; comment nodes carry `databaseId`),
`parent-<owner>_<name>-<number>` (a follow-up's `parent`), `board`, `item`, `set-status`,
`readback` (the board). After a write the script re-reads with the prefix `after-`
(`after-issue-p1`, `after-acceptance-p1`). In fixture mode a write never reaches gh: it is
appended as a JSON line (`sort_keys`, no ASCII escaping) to `$ACCEPT_ISSUE_WRITE_LOG`:

```
{"body": ..., "issue": N, "op": "comment" | "patch-body", "repo": ...}
{"issue": N, "op": "close", "repo": ..., "state_reason": "completed"}
{"op": "set-status", "ref": ..., "status": ...}
```

Order of writes: `table` — comment, then the card; `accept` — patch-body (ticks), comment,
close; `return` — comment, then the card. Live, the same writes are `gh api` REST calls
(`PATCH`, `POST`); the card is the GraphQL mutation of `board_status.py`.

## Cases

| Case | Shows |
| --- | --- |
| `01-not-done-then-ac-check` | author: AC2 «не выполнено» → no AC Check, card On Review → In Progress; a new PR with `Refs` in the same issue, a second table, AC Check; step 3: a «не выполнено» found with the card already in AC Check — the card is left, the acceptor returns it |
| `02-no-pr-moved-and-non-code` | author on a task without a PR: a moved AC, a non-code AC with a comment link, a visual AC with a screenshot; dry run; a viewer who is not an assignee |
| `03-accept-and-close` | accepter: all done → tick, a closing comment naming every AC, close, read-back `manual` |
| `04-return-with-fail` | accepter: a «не выполнено» in the table plus a `--fail`: returned comment, card AC Check → In Progress, issue stays open |
| `05-finding-outside-ac` | a finding outside the AC during acceptance: accept proceeds, the follow-up is a create-issue spec with `parent` = the source issue and no «Хвосты» (the parent is verified), the closing comment names it |
| `06-closed-by-merge` | accepter on an issue closed by merge (the Development trap): stop, nothing written even with `--apply` |
| `07-followup-on-unmet-ac` | «заводи follow-up на AC2» for an unmet AC: author and accepter modes, no write, `rework_rule`; step 3: `accept --followup` on an unmet table is a stop |
| `08-guards-and-stops` | an OPEN PR (stop in `accept`, warning in `table`), a card outside AC Check, a viewer who wrote the table, a follow-up with another parent, `moved` without the tech lead's note, no table found, a table older than a return |
