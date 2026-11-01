<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/implement-issue/examples/01-open-blocker/README.md -->

# 01 — open blocker: stop and name it

**Source:** a live snapshot of api#269 (this skill's own issue), recorded on 2026-10-29 by
running `preflight.py` against GitHub and saving every response. The only edit: the body of each
cross-referencing pull request is reduced to the first line of its `## What` and its `## Refs`
lines, the only parts the script reads. Added in 1.1.0, when the query gained them: the `mergedAt`
of each merged pull request (read from GitHub) and an empty `comments` page — no accept-issue
comment existed at the recording.

**A dated snapshot.** api#265 was closed later the same day, so the live api#269 now stops on
api#266 alone (one line in `reasons`). The fixture and `expected.json` keep the state at the
recording on purpose: the case is there to show a stop that names two blockers and skips a
closed one. Do not re-record it to follow the live issue.

## Input

```sh
preflight.py api#269
```

api#269 is OPEN, In Progress on board 12, assigned to `axyi`. Its native `blockedBy` links are
api#262 (CLOSED), api#265 (OPEN) and api#266 (OPEN). `## Scope` has «Ч.1» and «Ч.2». Four pull
requests cross-reference the issue (ui#177, ui#179, ui#184, api#285), but none has a `Refs` line
naming it, so none counts.

## Expected

- `verdict: stop`, exit code 1, `selected: null`.
- `reasons` name api#265 and api#266 with their titles, and not api#262: a CLOSED blocker does
  not block.
- `parts` are still reported: Ч.1 and Ч.2, both `todo`, no pull requests.
- The Kickoff is present, so there are no warnings.
- The skill reports the reasons verbatim and ends. No branch is cut and the board is not touched.
