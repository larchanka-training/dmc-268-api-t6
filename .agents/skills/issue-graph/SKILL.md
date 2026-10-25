---
name: issue-graph
description: This skill should be used when the user wants to see, build or publish the dependency graph of a sprint (a milestone), an epic or a list of GitHub issues — what can be taken in parallel, the critical path, blocking cycles, In Progress work waiting on an open blocker — or runs the sprint-start ritual of publishing that graph to the «Спринт N» issue (api#273). Also when asked who waits on whom across the api and ui repos.
metadata:
  version: 1.0.0
  source: team
  adapted-for: any
---

# Issue dependency graph

<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/issue-graph/SKILL.md -->

Turn GitHub's native `blocked by` / `blocking` relations across the api and ui repos into one
deterministic report — a Mermaid flowchart, cycles, In Progress issues waiting on an open blocker,
layers of what can be taken in parallel, the critical path, issues without links and the issues
outside the scope — and, only when asked, publish it as one comment that a re-run updates in place.
The script reads; the only write it can make is that one comment. Links are set by people
(create-issue), never inferred by a model.

Runs in one context: no sub-agents and no `.agents/agents/*.md` (Codex does not load them).

## Preconditions

- `gh` authenticated (`gh auth status`) with scopes `repo` and `read:project` — the board Status
  comes from project 12 (the user runs `gh auth refresh -s read:project`; the agent does not).
- python3 (stdlib only).
- Run from the root of either clone (the skill is byte-identical in both). Paths in this file are
  from the repo root.

## Script — the agent never writes to GitHub itself; the script does

`.agents/skills/issue-graph/scripts/issue_graph.py`, one mode per run:

| Mode                                        | Scope                                                                    |
| ------------------------------------------- | ------------------------------------------------------------------------ |
| `--milestone TITLE [--repo OWNER/NAME ...]` | issues of the milestone with exactly this title, in api and ui (default) |
| `--epic REF`                                | the epic's direct sub-issues                                             |
| `--issues REF [REF ...]`                    | exactly these issues                                                     |

`REF` is `api#N`, `ui#N` or `OWNER/NAME#N`. Common options: `--publish-to N|REF` (a bare `N` is
`api#N`), `--project-owner ORG` / `--project NUMBER` (the board the Status is read from, default
`larchanka-training` / `12`). Use `--repo` only when the user names a repo, and never together with
`--publish-to` unless they asked for exactly that: the repo set is not in the marker, so a narrower
run replaces the two-repo comment.

**stdout carries the report only, stderr is empty or one JSON line, the exit code carries the
class.** What each report section means: `references/report.md`. How publishing finds, updates and
refuses: `references/publishing.md`.

## Workflow

### 1. Resolve the scope

- «спринт N» → `--milestone "Спринт N"`. Milestones are separate in each repo and matched by exact
  title in both (team convention, api#273). Never substitute a similar title.
- «текущий спринт» → list the open milestones of both repos, read-only, and use the sprint title
  that fits; when more than one fits, ask which:

  ```bash
  gh api repos/larchanka-training/dmc-268-api-t6/milestones --jq '.[].title'
  gh api repos/larchanka-training/dmc-268-ui-t6/milestones --jq '.[].title'
  ```

- An epic → `--epic REF`; a list of issues → `--issues REF ...`. A scope takes no bare `#N`: ask
  which repo when the user gives only a number.
- No scope → ask: a milestone, an epic REF, or a list of REFs.

When the request names an environment `ISSUE_GRAPH_FIXTURE_DIR=<dir>` (golden examples, tests),
put exactly that assignment in front of the command: the script then reads fixtures and never calls
`gh`. `ISSUE_GRAPH_WRITE_LOG` is for the tests only — never set it. A fixture case run without the
assignment is a live run; if the request names the environment, never run the command without it.

### 2. Publish or only show

Without `--publish-to` nothing is posted — that is the default. Add `--publish-to` only when the
user asked to publish **and** named the target issue: «в #261» → `--publish-to 261` (a bare number
is `api#N`, where the sprint issues live); `ui#N` or `OWNER/NAME#N` as given. Never pick a target
yourself and never add `--publish-to` "while at it".

Sprint-start ritual (api#273): once the sprint's issues are filed, the milestone graph is published
to the «Спринт N» issue — for «Спринт 4» that is `api#261` (api#268, api#273); for another sprint
the user or the tech lead names the number, never search titles for it. A request like «опубликуй
граф спринта 4» without a number does not name a target: ask «в какой issue — в #261?» and wait. The
offer names #261 only for «Спринт 4»; for any other sprint ask «в какой issue опубликовать граф?»
and offer no number. After a show-only reply to a whole-sprint graph request, end with one line
offering it, e.g. «могу опубликовать граф в issue «Спринт 4» (#261) — `--publish-to 261`», and wait.
With a cycle there is no offer; say that publishing becomes possible once a person has removed links
and a re-run shows «Нет.» under «Циклы». No offer after a narrow question or a fixture run.

### 3. Run once

```bash
# show only (the default)
.agents/skills/issue-graph/scripts/issue_graph.py --milestone "Спринт 4"
# publish: only if the user asked and named the target
.agents/skills/issue-graph/scripts/issue_graph.py --milestone "Спринт 4" --publish-to 261
```

Run one of the two. One run per request. Never re-run to look again, without `--publish-to`, or
with a narrower scope. stdout is the report, the exit code is the class (§ Exit codes), stderr is
empty or one JSON line: `{"error": …}`, or after a publish
`{"published": "created" | "updated", "url": …}`.

### 4. Reply

Answer from the report only: quote its lines (links may be dropped), every issue number in the
reply is in the report, except the target number in the publish offer, and never invent an order it
does not give — with a cycle it gives none («Не считаются: в графе цикл.»). Sections are named by
the report's Russian headings.

- **Publish requested** → the first line is the outcome: `created` / `updated` with the URL, or «не
  опубликовано» and why (steps 5–6, § Exit codes). If the report has the `> Майлстоун … не найден в …` line, state it
  right after the outcome line. Then the reply below.
- **Whole graph** («покажи граф спринта») → in this order:
  1. Alarms: «Циклы», then «In Progress с открытым блокером»; an empty one gets one line («Циклов
     нет»).
  2. Layer 1 of «Слои: что можно брать параллельно» — «можно брать сейчас». The layers ignore the
     board Status and the scope, so add one line: a layer-1 issue whose Mermaid `class` line says
     `progress` or `review` is already underway, and one tagged «(вне выборки)» is not this scope's
     to take (read the classes from the report's Mermaid block; do not show the block).
  3. «Критический путь», the chain as the report gives it.
  4. «Без связей».
  5. Warnings: the `> Майлстоун «…» не найден в …` line under the title, and «Вне выборки».
  6. Show-only: the publish offer from step 2.
- **Narrow question** (who is blocked, is there a cycle, what is free now) → only the section that
  answers it (step 7 for In Progress), nothing else, no publish offer.

Show the Mermaid block only when the user asks for the picture, and then copy it from the report
verbatim — never redraw, simplify or relabel it.

### 5. After a publish

Relay `created` or `updated` and the `url` from stderr verbatim. Re-running with the same scope
updates that same comment (the marker on its first line), so «обнови граф» is the same command run
again — a fresh run, never the earlier report retold. «обнови граф» without a scope, in the same
session, reuses the scope of the last publish; in a new session ask. The target is still the one the
user names (§ 2). Never post, patch, delete or re-mark a comment by hand (`gh issue comment`,
`gh api -X POST|PATCH`), and never edit the target issue's body.

### 6. Cycle (exit 1)

Not a failure: the report is printed, and nothing was published — a request to publish does not
override this, and nothing works around it. The reply carries all of these, in this order:

1. With `--publish-to`: «не опубликовано» and why (the graph has a cycle).
2. The cycle line from «Циклы», as printed (`api#1 → api#2 → api#3 → api#1`).
3. A person removes a `blocked by` link in GitHub and re-runs, repeating until «Циклы» reads
   «Нет.» — name every issue on its «в том же клубке связей ещё» line with its tags, read off the
   report, never inferred from the graph (`references/report.md`).
4. Which link is wrong is the team's call.
5. The «In Progress с открытым блокером» lines, if any (step 7).

Never suggest a link, never edit links, never narrow the scope to make the cycle disappear, never
re-run — with or without `--publish-to`, the report is already printed.

### 7. In Progress with an open blocker

Name each line of the section («api#15 ждёт api#11, api#90 (вне выборки)») and say plainly when a
blocker is outside the scope (it is listed in «Вне выборки»). Then offer the options, choosing none
and recommending none — a person decides:

- finish the blockers first, then continue the issue;
- move the issue back out of «In Progress»;
- remove the link, if the blocker is stale and the issue no longer waits on it.

Never move the issue on the board, change a status, or add or remove a link.

## Exit codes

| Code | Meaning                                                                                                                                                  |
| ---- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 0    | report printed; with `--publish-to` also published (stderr `published`, `url`)                                                                           |
| 1    | report printed; the graph has a cycle, so never published (with `--publish-to` stderr says so)                                                           |
| 2    | usage or input: bad REF or target, milestone in no repo, epic or issue not found; target issue or comment 404, comment over 65536 characters             |
| 3    | `gh` / GraphQL / REST failure: non-zero exit, 120 s timeout, GraphQL `errors`, bad shape, missing fixture, fixture write without `ISSUE_GRAPH_WRITE_LOG` |

Exit 2 or 3 from the publish step (after the fetch) leaves the report on stdout: show it, say it was
not published and why, and do not re-run. After a 3 a retry is the user's call, and safe (the marker
turns it into an update, even if a timed-out write had landed); a 2 repeats until the target or the
scope changes. Exit 2 or 3 before that leaves stdout empty: relay the error; a milestone not found
is never retried under another title.

Exit 3 also covers the marker search running past 100 pages (10000 comments), an empty
`ISSUE_GRAPH_FIXTURE_DIR` (never a live run), and a marked comment this account cannot edit
(`comment <id> carries the marker but this account cannot edit it`): no second comment is posted;
its author or a maintainer re-runs, or a person removes it (`references/publishing.md`).

## Examples

Golden examples in `.agents/skills/issue-graph/examples/`: `input.md` is the request (a fixture
case adds an `ISSUE_GRAPH_FIXTURE_DIR=…` environment line), `expected.md` the checkable outcome.
They are for the people who check the skill: while serving a request, do not open `examples/` —
the reply follows from this file and the report alone.

- `01-show-sprint` — live, show only: alarms first, layer 1, critical path, «Без связей», the offer.
- `02-publish-rerun` — live, two turns: publish to `#261`, then a re-run updates the same comment.
- `03-cycle-no-publish` — fixture `cycle`: exit 1, nothing published, the cycle left to people.
- `04-in-progress-blocked` — fixture `milestone`: the one blocked In Progress issue, options only.

To run one by hand, start a fresh interactive session in the repo root — **Claude Code**: `claude`
(the skill loads from `.claude/skills`, a symlink to `.agents/skills`); **Codex**: `codex` (reads
`.agents/skills`) — and paste the body of
`.agents/skills/issue-graph/examples/<NN-name>/input.md`: everything after the SYNC line, the
environment line included; for `02`, turn 2 after the reply to turn 1.

Check the command, exit code, what was published and the reply against `expected.md`; the output of
both harnesses goes into the PR description (api#268). `01` and `02` are live, and `02` really
writes the marker comment in api#261.

## Tests and gates

The tests are offline: `ISSUE_GRAPH_FIXTURE_DIR` fixtures, byte-exact goldens in `tests/golden/`,
and an autouse stand-in `gh` first on `PATH` that exits 97, so a request that escapes the seam fails
instead of reaching GitHub. From the repo root:

```bash
# api
uv run pytest .agents/skills/issue-graph/tests
uv run mypy --strict .agents/skills/issue-graph/scripts/issue_graph.py .agents/skills/issue-graph/tests/test_issue_graph.py
# ui — no Python project; pytest is fetched ephemerally, nothing is added to dependencies
uv run --no-project --with pytest pytest -p no:cacheprovider .agents/skills/issue-graph/tests
```

**Not in the required CI job, by decision:** api's `testpaths = ["tests"]` never collects them and
ui has no Python in CI — a CI step must name the path, or it is green because nothing ran. Run them
by hand whenever the skill changes (api's `ruff check .` and `ruff format --check .` do cover the
`.py` files; `mypy .` skips dot-directories, hence the explicit line), then `diff -r` against the
other repo (`.agents/README.md`). The examples are not automated: they are checked by the hand runs
above.

## Limits

- GitHub only: native relations and sub-issues. Dependencies written as text in an issue body are
  not read.
- The Status comes from board 12 of `larchanka-training` by default (`--project-owner`,
  `--project`); an issue not on that board has no status and is never flagged In Progress.
- `--epic` takes direct sub-issues only, not their children.
- Relations are read one hop out: an issue outside the scope shows only its links to in-scope ones.
- One comment per (target issue, scope): another scope on the same issue gets its own comment; the
  `--repo` set is not part of a milestone's scope, and `owner/name` in an `epic=` / `issues=` key is
  lower-cased, the milestone title is not (`references/publishing.md`).
- A report over GitHub's 65536-character comment limit is not published (exit 2): ask the user to
  narrow the scope; the agent does not re-run.
