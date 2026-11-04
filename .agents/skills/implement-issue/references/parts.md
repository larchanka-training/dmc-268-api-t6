# How the parts and their pull requests are found

<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/implement-issue/references/parts.md -->

The rules of `scripts/preflight.py`, in prose. The script decides; this is for reading its
`reasons` and `warnings`, and for writing a pull request that the next preflight attributes
correctly (the convention is in `SKILL.md`). The module docstring of the script is the contract.

## The parts of the issue

`## Scope` bullets that start `Ч.N` / `Часть N` / `Part N` / `Teil N` (`Ч.N`, `Part N` and `Teil N`
are what create-issue writes in ru / en / de; `Часть N` is what people write by hand) are the parts;
bullets that repeat a number are one part. An optional `(label)` follows, then `:` or a long dash
(`—`, `–`) with whitespace on both sides. A digit right after the dash is a range (`Часть 1 – 2: …`)
and the ASCII hyphen is no separator: such a bullet is a stop. Only the singular `Часть` is a label:
«Части 1–2 правят …» is prose. The numbers must be 1..k. A number longer than 9 digits names
nothing, here and everywhere below.

**The criteria.** A criterion under `## Acceptance Criteria` belongs to a part when it starts with
that prefix (`Часть 1: x`, `Ч.1 — x`) or with a bold one: `**Ч.1.** x`, `**Часть 1:** x`, `**Ч.1**: x`,
and `**Ч.1, state.** x`, whose qualifier stays (`state. x`). Everything else is common, which every
part's run reads. So a criterion of several parts, `Ч.1, Ч.2: x` or `**Ч.1, Ч.2.** x`, stays common
and never goes to the first part alone. So does a bold prefix with a second number and no label, a
range or a list (`**Ч.1–2.** x`, `**Ч.1, 2.** x`, `**Ч.1 и 2.** x`, `**Ч.1** – 2: x`); a qualifier
that starts with a digit (`**Ч.1, 2FA.** x`) reads the same, which is safe. A number with no mark
before it starts the text: `**Ч.1.** 2 теста` is Part 1, `2 теста`.

**Unreadable parts.** The label must be the very start of the bullet: `- **Ч.1 (P2). Title.** …`
(bold) parses no part. When no part is parsed but a criterion starts with a part number in any form
(the ones above, or one the script does not read: `Ч.1. x`, `*Ч.1* x`, `Part 1 of …`), the issue is
not a whole task: it has parts the script cannot read, and implementing it as one would leave the
criteria out. That is a **stop** (the reason lists the numbers): the issue's author or the tech lead
rewrites the Scope parts as `- Ч.N (…): …` (the create-issue format), numbered from 1; the run never
edits the issue. It comes after the closed, epic, blocker, numbering and board stops and before any
whole-task `go` or `accept`; `--part K` there still exits 2. With parsed parts a criterion for a part
the Scope lacks is only a warning.

**Not on board 12.** A **stop**: a person adds the card, and the run never does. It comes after the
closed, epic, blocker and numbering stops and before `--part K`, whole-task `go` and `accept`, so
that no branch is cut for a Status step that cannot work. A card without a Status is on the board.

## A pull request counts

Only with a `Refs` line naming this issue: `Refs #N` in the same repo, `Refs owner/repo#N` from
any repo (a bare `#N` is the pull request's own repo). Pull requests come from the issue's
timeline, both repos. Status of a part: **done** if a MERGED pull request names it, **open_pr** if
an OPEN one does, else **todo**. Closed-unmerged pull requests never count.

## Which parts it names

- In its **title**: every part named there counts (a warning when there are several).
- Else in the first non-empty line of its `## What` — a heading `What`, also `What — …` or
  `What - …` (any dash) — the parts of the **first mention**. A later «Part 1 is merged» is context,
  not scope. A body without that heading is read from its first line.
- The forms: `Part K`, `Ч.K`, `Teil K`, `Часть K` (also `Части K`, `Частью K`), any case, any
  horizontal whitespace after the label; `Part 3a` is Part 3.
- A **plural label with a list** — `Parts 2 and 3`, `Parts 2, 3`, `Parts 2 & 3`, `Части 1, 2 и 3`,
  `Teile 2 und 3` — is one mention and names every listed part. A plural label alone (`Parts 1`)
  names nothing.
- A **range** — `Parts 1–3`, `Part 1-3` (the dash is `-`, `–` or `—`) — names no part when its
  lower end is below its upper end, and nothing after it stands in: a title with a range anywhere
  names none; a `What` line whose first mention is a range names none
  (`Parts 1–2 of #13: both. Part 3 is next.` is not Part 3). The upper end must be a whole number
  not followed by a letter,
  a digit, `-`, or `.` / `:` / `/` and a digit: `Part 2 — 28.10` and `Part 1 — 2026-10-29` are
  not ranges (they name Part 2 and Part 1); `Part 2 - 3 files` is one (it names nothing, which is
  safe); `Part 3 - 2 files` is Part 3. A range with an end of more than 9 digits is one too.

## Whose part it is

- `Part K of <ref>` (`von`, `из` too; `<ref>` is `#N`, `api#N`, `ui#N` or `owner/repo#N`, a bare
  `#N` being the pull request's own repo; the ref may be wrapped in `**…**`, backticks or a
  markdown link) belongs to `<ref>`. When that is not the issue under check the mention is
  skipped as if absent: a title goes on to its next mention, a `What` line whose first mention is
  skipped names nothing.
- When the `Refs` of a pull request name **two or more issues** (all its `Refs` lines together;
  `#13` and `owner/repo#13` are one), a mention counts for an issue only if it is **tied** to it:
  `Part K of <ref>`, or `<ref> part K` / `<ref> Ч.K` / `<ref> часть K` right in front of it (the
  ref wrapped as above). An untied mention is skipped like a mention of another issue. With one
  issue in `Refs` an untied mention counts, as ever.
- A pull request with `Refs` that names no part of the issue is a **stray**: the next preflight
  stops while it is OPEN (no `--part`), and warns once it is MERGED or CLOSED.
- Checkboxes play no part: the tech lead ticks them at acceptance.

## When acceptance is next

`accept` is the verdict when **all** of this holds; any other state is a `stop` with its reason:

- Every part has a merged pull request (a task without parts: its pull request) and none is open.
- The issue is no epic. An **epic** has sub-issues, **no parts of its own** (no Scope parts) and a
  marker: the word `Эпик` / `Epic` in its title, or a Scope bullet that starts with an issue
  (`- #140 — …`, `- ui#102 — …`): stop, implement the sub-issues (`issue.sub_issues` has the counts).
  Sub-issues alone do not make one: accept-issue files a follow-up as a sub-issue of its task. With
  no marker the issue is read as one task, with a warning that may mean an epic without the marker.
  An issue with its own parts and sub-issues is read by the parts rules; its sub-issues are not
  counted.
- The card on board 12 is In Progress, On Review or AC Check (the statuses accept-issue sets AC
  Check from). In Todo, Done or on no Status the work was never started here: stop, the user takes
  the task.
- The issue is not held back (below).

## Returned at acceptance

accept-issue marks its comments at their start: `<!-- accept-issue:table v1 -->`,
`<!-- accept-issue:accepted v1 -->`, `<!-- accept-issue:returned v1 -->`. The latest of those
comments holds the issue back when it is `returned`, or when it is a `table` with a line that
starts `Итог: не выполнено` (the self-check found unmet AC; the all-met line is «Итог: «не
выполнено» нет…»), and no pull request with `Refs` was merged after it. When acceptance would be
next that is a **stop** (the reason links the comment); a newer table, or a pull request merged
after the comment, lifts it, and the verdict is `accept`. Whenever the issue is held back — also
with `--part K` or a part still todo — `held` (`kind` `returned` | `unmet_table`, `url`) names the
comment, and the run reads it first: the rework covers what it lists.
