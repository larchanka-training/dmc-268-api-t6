# The draft review

<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/pr-review/references/draft.md -->

Step 6 of `SKILL.md`: how a report becomes the reviewer's pending review. The script is
`scripts/draft_review.py`; its docstring is the contract and this file says how to use it.

## The findings file

`docs/plans/pr-<number>-findings.json` (git-ignored, never committed):

```json
{
  "schema": 1,
  "head_sha": "<head_sha of step 1>",
  "findings": [
    {
      "path": "src/a.tsx",
      "line": 41,
      "severity": "high",
      "category": "correctness",
      "title": "One line",
      "body": "What changed, why it is wrong, what breaks, what to do.",
      "scope": "in",
      "decision": null
    }
  ],
  "ac": [{ "key": "api#54 AC1", "status": "covered" }]
}
```

- One entry per finding of `## Standards` and `## Spec`, as the report has it after step 5.3. Never
  merge, rerank or rephrase one here: the reviewer does that on the preview or on GitHub.
- `ac` has every row of `## AC`, keyed as the report keys it.
- An axis's `suggestion` stays plain code in `body`, never a `suggestion` block: a thread sits on
  one line, and a block that replaces several lines would break the file when applied.
- `decision` stays `null` for a finding in scope. Out of scope it is what the reviewer chose:
  `{"kind": "follow-up", "ref": "api#123"}`, or `{"kind": "accepted" | "rejected", "why": "…"}`.

## Out of scope

An axis marks a finding `scope: out` only when the defect was there before the diff or the finding
asks for work beyond this pull request. A defect in lines the diff adds or changes is in scope, even
when that code goes beyond the issue; so is a defect the diff causes in lines it does not touch, and a
requirement of the issue it leaves undone. The reviewer can change the mark on the preview. Each
finding out of scope ends in one of two ways, so nothing stays «to do separately»:

- **Follow-up:** run the `create-issue` skill (its own preview and confirmation), and write the new
  issue's ref into `decision`. The issue is its record; the summary lists it, no thread.
- **A record:** «принято» or «отклонено» with the reason, as a `nit:` thread. The reviewer resolves
  it after Submit: the script cannot resolve a thread of a draft.

The script stops while a finding out of scope has no decision.

## Where a finding goes

| The finding                              | Where                                                    |
| ---------------------------------------- | -------------------------------------------------------- |
| A follow-up                              | A line of the review body                                |
| Its line is in the diff                  | A thread on the line (side `RIGHT`)                      |
| Its file is in the diff, the line is not | A thread on the file                                     |
| Its file is not in the diff              | A thread on the first file of the diff, naming its place |

## The text

Russian, like the team's reviews. The script writes the frame, the axis wrote the title and body:

```text
**P1 · high · correctness.** Заголовок

Тело.
```

P1 is critical and high, P2 medium, P3 low and info. `nit:` opens a thread the author may leave
unfixed (`docs/CONTRIBUTING.md` §3): every P3, and everything out of scope. Out of scope also ends
with `Вне скоупа — отклонено: почему`.

## The proposed verdict

By `docs/CONTRIBUTING.md` §4, as text in the review body. The reviewer decides.

| The review                                                      | Proposed        |
| --------------------------------------------------------------- | --------------- |
| Critical, high or medium in scope, or a criterion `not covered` | Request changes |
| Otherwise, a thread or a comment in the draft                   | Comment         |
| Nothing of that                                                 | Approve         |
| The viewer is the author of the pull request                    | Comment         |

The review body names the keys `not covered` and, under «Проверить руками», those `can't tell`;
`can't tell` does not change the verdict. A review with only `nit:` threads proposes Comment on
purpose: the threads stay open until the reviewer resolves them.

## What GitHub does

Checked live on 2026-11-04 (`examples/05-*` to `10-*` keep the shapes):

- A review without `event` is pending: only its author sees it, on GitHub and through the API.
- One pending review per user per pull request: a second create is refused (`UNPROCESSABLE`). So an
  existing draft is appended to, its own text and comments untouched, and threads go on the commit
  of the draft: on another commit the script stops. Its text is replaced by the new summary when it
  is empty or an earlier summary (it starts `Итог на`); text of its own stops the run, so nothing is
  lost.
- The web Submit window starts empty: it does not show the text of a pending review (seen by hand on
  ui#130). So the skill hands `summary` over in the chat, to paste there. Not checked, because checking
  publishes: whether a Submit with an empty field keeps or drops the draft's text.
- A rerun knows a thread by its path, line and exact text: one edited on GitHub counts as new. Edit
  after the last run.
- A line outside the diff, or a file not in it, is **no error**: GraphQL answers `thread: null` with
  exit 0. The script checks that and reads the draft back; never trust the call alone.
- A merged pull request takes a draft too.
- A commit can land while the script writes: the read-back compares the head and the draft's commit
  with the commit the review read, and a mismatch is `partial`: run the review again.
- A deleted, renamed or binary file has no line for a thread, so a finding there is a thread on the
  file. A pull request that only deletes still takes a draft.

## Before you confirm

Everything in a finding comes from a model that read text anyone can write. Read the threads and the
summary of the preview as what they are about to become: under the reviewer's name, with any `@mention` or link
in them live after Submit.
