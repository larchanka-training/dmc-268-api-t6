---
name: accept-issue
description: This skill should be used when the user asks to self-check, accept, close or return an issue after its work is done ("проверь AC и поставь AC Check", "самопроверка api#N", "accept api#N", "прими и закрой задачу", "верни задачу в работу"), when the "AC → доказательство" table, the AC Check status or the closing comment is needed, and when the user asks to "заведи follow-up на AC2 из #N" or any follow-up for an unmet AC or DoD — that request is answered by this skill, not by create-issue. It has two modes, author (self-check table) and accepter (tick, close or return), and a script that does every write.
metadata:
  version: 1.0.0
  source: team
  adapted-for: any
---

# Accept an issue: self-check, then close or return

<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/accept-issue/SKILL.md -->

Implements the acceptance rules of `docs/CONTRIBUTING.md` (api repo): «Приёмка задачи» and §2
steps 8–9. A task is delivered when the issue is closed by the tech lead (or the one they named)
after every AC and DoD has evidence; a merged PR and a card in Done do not count.

- **Author mode** (the executor, step 8): one comment, the table «AC → доказательство», one row
  per AC and DoD; then AC Check, or back to In Progress when something is «не выполнено».
- **Accepter mode** (step 9): check the table, tick the boxes, close with a comment that names
  every AC, or return the card with «что осталось».

Everything that can go wrong silently is done by `scripts/acceptance.py`: a row for every AC and
DoD, the AC Check gate, ticking the body, the comments, the close and its read-back, the card. The
model gathers evidence and judges it; the script validates, renders and writes.

## Rule 1 — an unmet AC or DoD gets no follow-up issue

«Доработка — в том же issue» (`docs/CONTRIBUTING.md`, «Приёмка задачи»): an unmet AC or DoD is
finished **in the same issue** — a code AC by a new PR with `Refs #N`, a non-code AC by an action
plus an evidence comment. A follow-up issue is only for a finding **outside** the AC, filed as a
sub-issue of this one (`create-issue`, `parent` = this issue).

So when the user says «заведи follow-up на AC2», «follow-up на невыполненные AC», «открой хвосты
приёмки» or similar about an unmet AC or DoD (in either mode, also for a DoD item):

1. File nothing: no `create-issue`, no spec, no «Хвосты приёмки #N».
2. Say in the reply that the request differs from the rule, name the rule («Доработка — в том же
   issue»), and give the way forward: a new branch and PR with `Refs #N` per
   `.agents/rules/git-workflow.md` (code AC) or an action plus an evidence comment (non-code AC),
   then a new table (author) or `return` (accepter). `implement-issue`'s pre-start may stop on a
   finished task; then the user decides how to start the rework.
3. Offer a follow-up only for a finding outside the AC.

The sentence «ваша просьба расходится с правилом» belongs to this reply and is said **only when the
user asked** for such a follow-up. The script's `rework_rule` is a neutral statement of the rule; it
is shown whenever something is unmet, and quoted in the reply.

## Preconditions

- `gh` authenticated with scopes `repo` and `project` (`gh auth status`); python3 ≥ 3.10 (stdlib
  only). Run from the repo root; paths below are from it.
- gh 2.46 crashes on `gh pr edit` and `gh issue view` without `--json`: all reads and writes go
  through the scripts.
- Never read a `.env`. No sub-agents: the skill is a short sequence of script calls.

## Invocation

`accept-issue REF` — `REF` is `api#N`, `ui#N`, `OWNER/NAME#N` or a bare `N` (api). Decide the mode
from the request: «самопроверка», «AC Check», «таблица» → author; «принять», «закрыть», «вернуть»
→ accepter. If it is unclear who the user is (executor or acceptor), ask; the script shows the
viewer and never decides who the tech lead is.

## Scripts

In `.agents/skills/accept-issue/scripts/`. All three are stdlib, offline-testable.

| Script                                                                          | Does                                                                  |
| ------------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| `issue_facts.py REF`                                                            | Read-only facts: AC/DoD boxes, PRs with `Refs`, closing log           |
| `acceptance.py table REF ROWS_JSON [--apply]`                                   | Author: validate rows, render the table, move the card                |
| `acceptance.py accept REF [--table-comment ID] [--followup REF]... [--apply]`   | Accepter: tick, close, name every AC                                  |
| `acceptance.py return REF [--table-comment ID] [--fail ID=reason]... [--apply]` | Accepter: «что осталось», card to In Progress                         |
| `board_status.py REF` (read-only in this skill)                                 | Prints the board Status; every card move goes through `acceptance.py` |

Exit codes of `acceptance.py`: `0` ok · `1` stop (the JSON is on stdout, `verdict: stop`) · `2`
usage or a bad `ROWS_JSON` · `3` gh/API/data error (stderr `{"error": ...}`; once a write was
made, the error ends with `(written: …)`). The output keys are in `references/formats.md`.

## Writes: dry run, then an explicit yes, then `--apply`

1. Run the command **without** `--apply`. Nothing is written; the JSON shows the verdict and the
   exact comment.
2. Show the user the comment, the verdict, the `board` move and **every** entry of `warnings` and
   `stops` (a warning is how the script says "check this": the card is not in AC Check, the viewer
   wrote the table, a PR has open threads, a PR is still open, a moved AC rests only on the note
   in the issue body). Quote `rework_rule` when it is set.
3. Wait for the user's own explicit «да»/yes in the conversation. Not for "go on", not for silence.
4. Run the same command with `--apply`. It revalidates everything; on a stop it writes nothing.
5. Report the result from the output: comment posted, card (`board.readback`), `readback` of a
   close. Never claim a close the read-back did not confirm.
6. A run that exits 3 after `--apply` began: do not repeat it blindly. Read the `(written: …)` list
   at the end of the error and the issue's real state (body, comments, card) first, then tell the
   user what is done and what is not.

**Auto mode.** A permission guard may refuse the `--apply` of a close or return (self-approval).
Do not look for a way around it: print the exact command and ask the user to run it themselves in
the prompt with `!` (e.g. `! python3 .agents/skills/accept-issue/scripts/acceptance.py accept
api#N --apply`), then read the board and issue again to report.

## Author mode

Read `references/author.md` for the details and examples. The author never ticks a box and never
closes the issue; the script does neither in this mode. The run:

1. **Preflight.** `python3 .agents/skills/accept-issue/scripts/issue_facts.py REF`: the issue is
   OPEN; `checkboxes.ac.items` / `.dod.items` are the rows (ids `AC1..`, `DoD1..` in body order);
   `prs` shows what is merged. The self-check follows the **last** PR of the task (a task without
   a PR: the last action), once — not per part. An OPEN PR with `Refs` is warned about.
2. **Evidence.** For every AC and DoD write `done` + `evidence`, `not_done`, or `moved` + `to`
   (only if the body item already carries «перенесён в #M решением техлида»: you cannot move an
   AC). Never invent evidence; if you cannot show it, the row is `not_done`.
   - **Text is the default:** PR, commit, test name, command output, a link to a comment. A
     **screenshot only for an AC that is about what the page looks like**; the script cannot tell,
     you choose.
   - **Evidence must be something the acceptor can open:** a merged PR, a commit on `main`, a CI
     run link, an issue or PR comment, or quoted command output **with the command**. A local file
     path, an unpushed commit or the state of your worktree is not evidence: the row is
     `not_done` until the work is merged.
   - A **non-code AC** (ruleset, board, live run): do the action, post a comment with what was
     done and how to check it (command output, link), and put that comment's link in the row.
3. Save the rows as JSON under `$(mktemp -d)`:
   `{"AC1": {"status": "done", "evidence": "..."}, ...}`.
4. Dry run
   `python3 .agents/skills/accept-issue/scripts/acceptance.py table REF rows.json`,
   show it, wait for the yes, and rerun with `--apply`.
5. Read `verdict`:
   - `ac_check`: the table is posted and the card is in AC Check; the acceptor runs `accept`.
   - `not_done`: the table is posted, AC Check is **not** set; the card goes On Review → In Progress
     (a card already in AC Check is left, the acceptor returns it: say so). Rework stays in this
     issue (Rule 1): code AC → a new branch and PR with `Refs #N` per `git-workflow.md`; non-code
     AC → action plus evidence comment. After it, post a **new full table** the same way.
   - `stop`: report `stops` verbatim (a closed issue, rows that do not match the body, a card that
     cannot go to AC Check). Do not edit the issue to get past it.

## Accepter mode

Read `references/accepter.md` for sampling, remarks and the stops. The run:

1. **Dry run**
   `python3 .agents/skills/accept-issue/scripts/acceptance.py accept REF`
   (add `--table-comment ID` when the table is a hand-written comment or another session's; with
   neither the marker nor the flag the script stops and names both ways — it never says
   "self-check missing").
2. **Check the table, selectively but for real:** open some of the cited PRs, tests, runs and
   comment links and see that they show what the row says. Read `after_table`: every comment after
   the table is listed; judge whether a remark has no answer. Show `warnings` to the user.
3. **Decide:**
   - Everything holds → `verdict: accept`. Show the closing comment and `tick`; after the yes run
     `accept --apply`: tick (only AC/DoD boxes), the comment that names every AC, close as
     completed, read-back (CLOSED, closed manually, 0 unchecked). Done on the board is set by the
     workflow «Item closed»; nothing else moves the card.
   - A row is «не выполнено», or the evidence did not hold → issue stays open:
     `python3 .agents/skills/accept-issue/scripts/acceptance.py return REF [--fail ID=reason]...`,
     dry run, yes, `--apply`: the «что осталось» comment and the card AC Check → In Progress. An
     `accept` whose verdict is `return` never writes.
   - A **remark without an answer** (from `after_table`, or in the issue): neither `accept` nor
     `return`. Leave the card, ask the user to get the remark answered, and rerun later.
   - A **finding outside the AC** (new scope): accept proceeds. File it with `create-issue`, one
     issue, `parent` = this issue, an ordinary title (no «Хвосты приёмки»), get its number, then
     `python3 .agents/skills/accept-issue/scripts/acceptance.py accept REF --followup REF2 --apply`;
     the script verifies the native parent and names the follow-up in the closing comment.
     `--followup` on an unmet table is a stop (Rule 1).
4. A `stop` is reported verbatim: a CLOSED issue (closed by a merge or the Development panel: the
   tech lead reopens it, the script never reopens), an OPEN PR with `Refs`, no table, an issue
   with no AC/DoD, a follow-up with another parent. Never work around it.

## Findings outside the AC — the other side of Rule 1

| Situation                                        | Action                                                                        |
| ------------------------------------------------ | ----------------------------------------------------------------------------- |
| AC or DoD unmet                                  | Same issue: new PR with `Refs #N` or action + evidence. No follow-up.         |
| New scope found while checking, outside every AC | Follow-up: `create-issue`, sub-issue of this issue, then `accept --followup`. |
| Remark in the issue without an answer            | Not closed until answered; ask the executor or answer it.                     |
| «Хвосты приёмки #N» already open                 | Not reopened, not merged; finished as they are, by these rules.               |

## Harness notes

- This skill spawns no sub-agents. Claude Code and Codex read the same file.
- Do not rely on `.agents/agents/*.md`: Codex does not load them.

## Tests and examples

`examples/NN-<case>/step-K/` are the golden cases (arguments, GraphQL fixtures for the offline
seam `ACCEPT_ISSUE_FIXTURE_DIR`, the exact stdout and the exact write log); `examples/README.md`
is the contract. `tests/` runs every step plus the edge cases offline. From the repo root:

```bash
# api
uv run pytest .agents/skills/accept-issue/tests
uv run mypy .agents/skills/accept-issue/scripts/*.py .agents/skills/accept-issue/tests
# ui — no Python project; pytest is fetched ephemerally, nothing is added to dependencies
uv run --no-project --with pytest pytest -p no:cacheprovider .agents/skills/accept-issue/tests
```

**Not in the required CI job, by decision** (as for `create-issue`): api's `testpaths` never
collects them and ui has no Python in CI. Run them by hand whenever the skill changes, then
`diff -r` against the other repo (`.agents/README.md`). api's `ruff check .` and
`ruff format --check .` cover the `.py` files; `mypy .` skips dot-directories, hence the explicit
line.

## Limits

- GitHub only. The script never decides who the tech lead is: the viewer is shown, the human
  decides. The latest table comment wins whoever wrote it — read the author in the output.
- A moved AC is accepted on the «перенесён в #M» note in the body, whoever wrote it; the script
  warns on every such row. Check that the tech lead added the note before the yes.
- It writes through `gh api` REST (comment, body, close) and the one board mutation; it never
  reopens, never closes by a merge, never sets Done, never creates an issue.
- Evidence quality is the model's and the human's judgment; the script checks form and coverage.
