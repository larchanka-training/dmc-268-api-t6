---
name: implement-issue
description: This skill should be used when the user asks to implement, take, start or work on a GitHub issue or one part of it ("implement api#269", "take issue 42, part 2", "do the next part of an issue") — it checks that the issue can be started (open, native blockers closed, a part chosen), reads the Kickoff, cuts the branch per git-workflow.md and moves the board card to In Progress. Also when asked whether an issue is free to take, or which part comes next.
metadata:
  version: 1.0.0
  source: team
  adapted-for: any
---

# Implement an issue, one part at a time

<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/implement-issue/SKILL.md -->

One run takes **one issue → one part → one pull request**. The skill is a wrapper: it decides
whether the work may start and where, then hands the work to the existing skills
(planning-and-task-breakdown, tdd, code-review, pull-request) and never copies their text.
A weak model can follow it: every decision in the pre-start is made by a script, the model
reads the answer.

**Version 1.0.0 covers the pre-start (steps 1–4).** The implementation loop (step 5) is Part 2
of api#269 and is not in this version.

## Preconditions

- `gh` authenticated with scopes `repo` and `project` (`gh auth status`); python3 ≥ 3.10
  (stdlib only). Run from the repo root; paths below are from it.
- gh 2.46 in this org crashes on `gh issue view` without `--json` and on `gh pr edit`
  (Projects classic): read issues through the scripts, or `gh api`.
- A clean work tree. Anything uncommitted: stop and ask what to do with it.

## Invocation

`implement-issue REF [--part K]` — `REF` is `api#N`, `ui#N`, `OWNER/NAME#N` or a bare `N` (api).
Without `--part`, the first part with no merged pull request is taken (and the run stops if that
part has an open one); with it, that part is taken as given, even when it already has a pull
request (the script warns).

## Scripts

In `.agents/skills/implement-issue/scripts/`; `preflight.py` is read-only, `board_status.py` makes the
one write (the Status).

| Script                        | Does                                                                         |
| ----------------------------- | ---------------------------------------------------------------------------- |
| `preflight.py REF [--part K]` | Issue state, blockers, parts and their PRs, Kickoff, board Status → verdict  |
| `board_status.py REF STATUS`  | Set the Status on board 12 by option name, read it back; never adds the card |

Exit codes: `0` go · `1` stop (the JSON is still on stdout) · `2` usage, bad REF or a `--part`
the issue does not have · `3` gh or API error. On 2 and 3 stderr holds `{"error": "..."}`.

## The run

### 1. Preflight

```bash
python3 .agents/skills/implement-issue/scripts/preflight.py REF [--part K]
```

Read `verdict`, `reasons`, `warnings`, `selected`.

- **`stop` (exit 1):** report `reasons` verbatim and end the run. Never work around a stop:
  do not pass `--part` to get past a blocker or an open pull request, do not edit the issue,
  do not drop a blocker. The reasons are one of: the issue is CLOSED; a native blocker is OPEN
  (`ref — title`, one line each; "closed" means `state: CLOSED` literally, an issue in AC Check
  is still open); the parts are not numbered 1..k; the first unfinished part has an open pull
  request (finish it, or the user passes `--part`); every part has a merged pull request
  (acceptance is next); the task has no parts but already has a pull request with `Refs` (a
  rework PR is the user's decision).
- **exit 2 or 3:** report the error and end.
- **`go` (exit 0):** state `selected.part` (null = the whole task) and why (`first-todo`,
  `explicit`, `whole-task`), show every `warnings` line, continue. A missing Kickoff is a
  warning, not a stop: older issues have none.

How the parts are found: `## Scope` bullets that start `Ч.N` / `Part N` are the parts; a part is
**done** if a MERGED pull request names it, **open_pr** if an OPEN one does, else **todo**
(closed-unmerged pull requests never count). A pull request counts only with a `Refs` line
naming this issue (`Refs #N` in the same repo, `Refs owner/repo#N` from any repo) and names the
parts in its title, else in the first line of its `## What`. Checkboxes play no part: the tech
lead ticks them at acceptance.

### 2. Read the briefing

From the preflight JSON, not from memory:

- `kickoff.text` and every file it lists under "read first" (Что прочитать первым / Read first);
  open them. The "before writing code" items and the traps ("Мины" / Mines) bind the whole run.
- The selected part: `scope`, `boundary` (where the PR stops), `done_when` (the stop criterion),
  `acceptance_criteria`, plus `common_acceptance_criteria`. No parts: the issue as a whole.
- The parts that are not selected are out of this run — do not start them "while here".

### 3. Branch

Rules: `.agents/rules/git-workflow.md`. Work from a fresh `origin/main`.

1. `git fetch origin`.
2. **Name.** If the Kickoff names a branch ("Ветка" / "Branch"), take it with `<n>` replaced by
   the issue number. Otherwise `<type>/<n>-<kebab-slug>`: `feat`, `fix`, `docs` or `chore`
   (`chore` also for refactor, test, ci, perf, style) and a slug of the title without its
   `[tags]`, a few words.
3. **Collisions.** `git ls-remote --heads origin <name>`: a branch that already exists on origin
   belongs to an earlier part (or to someone else) — append `-p<K>` (`K` = the selected part).
   Never reuse the branch of a pull request that was merged or closed
   (`gh pr list --state all --head <name> --json number,state` must be empty); suffix again.
4. `git switch -c <name> origin/main`.

A **SYNC part** (shared files, `.agents/README.md` § Sync map) is two pull requests with the
same `Refs`: ui first, then the copy in api (`cp`, then `diff -r` empty). This run starts in the
repo the Kickoff names first (ui); the api copy comes after the ui change is final, on a branch
with the same name.

### 4. Board

```bash
python3 .agents/skills/implement-issue/scripts/board_status.py REF "In Progress"
```

It prints `{"board", "ref", "status"}` as read back. The Status, project and option ids are looked
up by name. A card that is not on board 12 is not added by the script (exit 3): report it and
stop — a person adds the card. Any other non-zero exit: report the error and stop.

### 5. The loop — Part 2 of api#269, not in this version

Planned: planning → tdd → code-review (clean context) → pull-request → On Review. **In 1.0.0,
stop here and report:** the preflight verdict and `selected`, the `warnings`, the branch name,
and the Status as read back.

## The pull-request convention that keeps detection working

The next preflight finds this run's part only through its pull request, so:

- The first line of `## What` starts `Part K of owner/repo#N: …` (`Part K of #N` in the same repo).
  A SYNC copy names the part too: `SYNC copy of owner/repo#M (Part K of #N): …`. The title may
  name it instead; if it does, the title wins.
- `## Refs` has `Refs #N` (same repo) or `Refs owner/repo#N` — the full form in the ui copy of an
  api issue. A bare `Refs #N` in the other repo is that repo's own issue and does not count.
- Never a closing keyword, never the Development panel (`pull-request` skill, git-workflow.md).
- A whole-task issue (no parts) needs no part in `What`, only the `Refs` line.

A pull request with `Refs` that names no part does not move any part and shows up as a warning.

## Harness notes

- `git push` and `gh pr create` need a human's confirmation in some harnesses: stop and ask,
  never bypass it (no `--no-verify`, no flag or setting that skips the prompt).
- Independent checks run in a clean context per `.agents/README.md` § Sub-agents across harnesses
  (Claude Code `Agent`, Codex `spawn_agent`, else a fresh `codex exec --ephemeral` /
  `claude -p`); never in the context that wrote the code.
- Do not rely on `.agents/agents/*.md`: Codex does not load them.

## Tests and examples

`examples/NN-<case>/` hold the golden cases: `README.md` (the invocation and what the skill
does), `fixtures/` (GraphQL pages for the offline seam `IMPLEMENT_ISSUE_FIXTURE_DIR`) and
`expected.json` (the exact preflight output); `tests/` runs every one of them, plus the edge
cases, offline. From the repo root:

```bash
# api
uv run pytest .agents/skills/implement-issue/tests
uv run mypy .agents/skills/implement-issue/scripts/*.py .agents/skills/implement-issue/tests
# ui — no Python project; pytest is fetched ephemerally, nothing is added to dependencies
uv run --no-project --with pytest pytest -p no:cacheprovider .agents/skills/implement-issue/tests
```

**Not in the required CI job, by decision** (as for `create-issue`): api's `testpaths` never
collects them and ui has no Python in CI. Run them by hand whenever the skill changes, then
`diff -r` against the other repo (`.agents/README.md`). api's `ruff check .` and
`ruff format --check .` do cover the `.py` files; `mypy .` skips dot-directories, hence the
explicit line.

## Limits

- GitHub only. Blockers are the native `blockedBy` links only; a "blocked by #N" in prose is not
  read (the Kickoff may say it — read it).
- The preflight and the board script are the only code; the pull request, its review and the
  hand-over to acceptance belong to the loop (Part 2).
