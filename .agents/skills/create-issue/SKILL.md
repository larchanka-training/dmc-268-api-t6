---
name: create-issue
description: This skill should be used when the user wants to file, open, raise or write up a GitHub issue, bug report or task from a conversation — resolving the repo from the git remote, enforcing a Definition-of-Ready gate, previewing the full body for explicit approval, guarding against duplicates and putting the issue on the course board (larchanka-training project 12). Also when asked to turn something just discussed into a tracked task.
metadata:
  version: 1.0.0
  source: team
  adapted-for: any
---

# Create an issue

<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/create-issue/SKILL.md -->

Turn a conversation into a properly formed GitHub issue and post it exactly once: a
Russian body in the layout of `larchanka-training/dmc-268-api-t6#33` and
`larchanka-training/dmc-268-api-t6#46`, a full preview with explicit approval, a
duplicate guard, a read-back, and placement on the course board. Nothing is created
without the user's explicit **Create**, so the worst a stray trigger can do is offer a
draft.

## Preconditions

- `gh` authenticated with scopes `repo` and `project` (`gh auth status`). Without
  `project` the issue is still created but the board step fails (exit 6, below); fix with
  `gh auth refresh -s project`.
- bash 4+ (`mapfile`), GNU `find`, `curl` and python3 (stdlib only) on `PATH`.
- Run from the clone the issue belongs to. Paths in this file are from the repo root.

## Scripts — never call the REST APIs directly

All in `.agents/skills/create-issue/scripts/`:

| Script                                                                     | Does                                                                                 |
| -------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| `issue-preflight.sh [--repo OWNER/REPO] [--remote NAME]`                   | Resolve the repo from git remotes; prove it is reachable, authenticated and fileable |
| `build_issue_body.py <spec.json> [--emit-args]`                            | Spec → markdown body, or → the `gh` flag vector                                      |
| `issue-create.sh <spec.json> [--dry-run] [--allow-duplicate] [--no-board]` | Duplicate guard → create once → read back → board                                    |

Labels, milestones, comments and closing are plain `gh` calls — `references/hosts.md`.

Every script follows one convention: **stdout carries the result, `{"error": …}` goes to
stderr, the exit code carries the class** — so `$?` is trustworthy.

## Workflow

### 1. Resolve the target

```bash
.agents/skills/create-issue/scripts/issue-preflight.sh
```

It reads the git remotes of the current directory and returns
`{repo, user, reachable, authed, can_write, nested_repos}`. GitHub only: a remote on
another host exits 2 — pass `--repo OWNER/REPO`. If `nested_repos` is non-empty the
resolved repo _contains_ other repositories (a lab folder of checkouts): the request may
belong to a nested one. Decide from what the request is about (a file it names → the repo
owning that file) and **name the choice in the preview** with the alternatives.

### 2. Detect the type

`Story` by default; `Bug` for something broken ("ошибка", "crashes", "regression"); `Task`
for technical work with no user-facing story; `Docs` for documentation work. The type
picks the layout and one repo label (`bug`, `enhancement`, `documentation`) — see
`references/templates.md`. There are no `type:` labels and none are created.

### 3. Gather the content

Everything discussed so far is material (title, background, scope, acceptance criteria,
dependencies, references; for a bug environment, steps, expected, actual). Tag each part
for the preview — `[✓ high]` stated, `[⚠ low]` inferred, `[✗ missing]` — so the user sees
which parts were invented.

### 4. Language

Team issues are Russian: set `"lang": "ru"` **explicitly** (the emitter's default is
`en`) — see Body conventions. For a repo with a different convention, look at its recent
issues (`gh issue list --repo OWNER/REPO --limit 5 --json title`) and follow them.

### 5. Definition of Ready

Three hard blockers — do not create until resolved, ask the user:

- a clear title describing **one** piece of work, in the form `[component] description`;
- at least one acceptance criterion;
- a `kickoff` block with `repo`, one `read_first` entry and `problem_shape`, when the
  issue is technical (a Bug or Task, or anything naming a service or a path). Purely
  non-technical issues (a decision to record) are exempt.

Two soft warnings — mention, then proceed: dependencies unstated ("None" is fine); no
obvious way to test it. An issue without acceptance criteria is a note to self.

### 6. Build the spec and the body

Write the spec under `$(mktemp -d)` — **not** `$TMPDIR/…`, which is normally unset on
Linux and writes to `/`:

```bash
SPEC="$(mktemp -d)/spec.json"
cat > "$SPEC" <<'JSON'
{ "repo": "<OWNER/REPO from preflight>", "type": "Task", "lang": "ru",
  "summary": "[component] Короткий заголовок в повелительном наклонении",
  "background": "…", "scope": ["…"], "acceptance_criteria": ["…"],
  "dependencies": {"Блокирует": ["…"], "Связано, не блокирует": ["…"]},
  "references": ["…"],
  "kickoff": {"repo": "…", "branch": "fix/<n>-<slug> — `main` защищён, через PR",
              "problem_shape": "…", "read_first": ["…"],
              "before_writing_code": ["…"], "mines": ["…"]},
  "assignees": ["<github-login>"] }
JSON
.agents/skills/create-issue/scripts/build_issue_body.py "$SPEC"
```

Never hand-write the body. The emitter fixes the section order, neutralises markdown that
would let user text forge a heading, and always emits `Dependencies` and `DoD Checklist`.
It refuses (exit 2) rather than drops: an unknown top-level key, a value of the wrong type
(a string where a list belongs), a Bug carrying story fields (or the reverse), a newline
in the title, an unclosed code fence in `background`, a dependency or a Kickoff prose
field. Field contents, dependency shapes, the default DoD, titles, reference hygiene and
Kickoff: `references/templates.md`.

`issue-create.sh "$SPEC" --dry-run` prints the final body without creating anything. It is
**not offline** (it still runs preflight: network and `gh` auth), but the attribution
guard runs before it, so a dry run already catches an authorship marker.

### 7. Preview, then confirm

Print the **full rendered body as ordinary text** — an approval widget truncates. Above
it, a short header:

```
Repo:      <OWNER/REPO from preflight>
Type:      Task  (label: enhancement)
Assignee:  <login>
Language:  ru
Board:     projects/12   (--no-board to skip)
```

Then ask for confirmation. The preview alone is never a green light.

**Confirmation across harnesses** (same idea as "Sub-agents across harnesses" in
`.agents/README.md` — the step must finish in every harness):

- **Claude Code** — call `AskUserQuestion`: question "Create this issue?", options
  **Create** / **Edit fields** / **Cancel**.
- **Codex and any harness without a structured-question tool** — print the same preview,
  then ask in plain text: "Reply `Create` to post this issue, `Edit` to change something,
  or `Cancel`." Only the word `Create` posts; "ok", "да" or silence do not.

On **Edit**, ask what to change, rebuild, re-print the full preview, ask again. On
**Cancel** or no answer, stop and create nothing.

### 8. Create

```bash
.agents/skills/create-issue/scripts/issue-create.sh "$SPEC"      # add --no-board to skip the board
```

It searches for an exact title match first (exit 4 unless `--allow-duplicate`; the search
is **fail-open** — if the call itself fails, a `{"warning": …}` goes to stderr and the
create proceeds), creates once, reads the issue back through the REST API, then runs
`gh project item-add 12 --owner larchanka-training --url <url>`. A create that returns
nothing useful is never retried: the script lists the newest issues (a plain listing — the
search index lags) and accepts only a same-title issue that was not there before the
create and was opened by the authenticated user — never an older duplicate or a
teammate's. If that lookup fails the outcome is unknown (exit 5): check the repo by hand.

Report the URL, the `board` value (`added` / `skipped` / `failed`) and `verified`. A
`verified: false` means the title read back differs from the one posted — say so and look
at the issue before anything else. Success is the read-back, not the exit code of create.

## Exit codes

| Code | Meaning                                                                                  |
| ---- | ---------------------------------------------------------------------------------------- |
| 0    | created — read `verified` (it can be `false`) and `board` in the JSON                    |
| 2    | usage, bad spec, unknown key, non-GitHub remote, or the attribution guard refused        |
| 3    | not authenticated (`gh auth login`)                                                      |
| 4    | 4xx, issue tracker disabled or repo not visible, or an exact-title duplicate blocked     |
| 5    | 5xx or network/TLS failure, or a failed create whose outcome could not be checked        |
| 6    | **issue created**, board step failed — stdout still has the url; never re-run the create |

On **6**: if stderr says `run gh auth refresh -s project`, do that; then run only
`gh project item-add 12 --owner larchanka-training --url <url>` for the printed URL.

## Body conventions

- **Language:** title and prose in Russian (`"lang": "ru"`), even if the request came in
  English. Section headings stay English (`Background`, `Scope`, `Acceptance Criteria`,
  `Dependencies`, `DoD Checklist`, `References`) as in
  `larchanka-training/dmc-268-api-t6#33` and `…#46`; Kickoff is rendered in Russian.
- **Title:** `[component] description` — imperative for Story, Task and Docs, "what is
  wrong" for a Bug.
- **Type → label:** `Bug` → `bug`, `Story` / `Task` → `enhancement`, `Docs` →
  `documentation`; added to `--label` by the emitter, de-duplicated against `labels`.
- **DoD:** the default is the team merge gate (one approval, all threads resolved);
  override with `dod` only for issue-specific gates.
- **References:** nothing may resolve only on your machine (the repos are public) —
  `references/templates.md`.

## Gate before creating

Every line PASS, or stop. No partial.

- [ ] Target resolved by `issue-preflight.sh`, exit 0
- [ ] DoR hard blockers cleared (title, ≥1 acceptance criterion, kickoff when technical)
- [ ] Local-path sweep over the spec — run it, do not eyeball it:
      `python3 -m json.tool "$SPEC" | grep -nE '~/|/home/|/Users/|/tmp/|\$TMPDIR|\.\./'`
      Every hit is rewritten per reference hygiene or keeps its precondition in the same line.
- [ ] Body produced by `build_issue_body.py`, not written by hand
- [ ] The user confirmed **Create**, by the structured question or by the word

## Attribution guard

`issue-create.sh` refuses (exit 2) to post an issue that carries a sign of assistant
authorship, checked case-insensitively over title, body and every argument: a line
starting `Co-authored-by:`, the text `Generated with`, `noreply@anthropic.com`, or a
`claude.ai/code/session` link. It is unconditional, and prose that merely names a file or
a tool (`CLAUDE.md`, `.claude/skills`, "Claude Code") passes; the tests assert both
directions. Remove the offending line from the spec rather than working around the guard.

## Tests and gates

The tests are offline (`ISSUE_FIXTURE_DIR` fixtures, plus a recording stand-in `gh` / `curl`
on `PATH` that pins the real command lines). From the repo root:

```bash
# api
uv run pytest .agents/skills/create-issue/tests
uv run mypy .agents/skills/create-issue/scripts/build_issue_body.py .agents/skills/create-issue/tests
# ui — no Python project; pytest is fetched ephemerally, nothing is added to dependencies
uv run --no-project --with pytest pytest -p no:cacheprovider .agents/skills/create-issue/tests
```

**Not part of the required CI job — a decision, not an omission.** api's
`testpaths = ["tests"]` means a plain `uv run pytest` (the CI step) never collects these
tests, and ui has no Python in CI. A CI step added later must name the path explicitly,
or it is "green" because nothing ran. Run the commands above by hand whenever the skill
changes. api's `ruff check .` / `ruff format --check .` already cover the skill's `.py`
files; `mypy .` does not walk dot-directories, hence the explicit mypy line. After any
edit, `diff -r` the skill against the other repo's copy (`.agents/README.md`, "Sync map").

## Limits

- GitHub only. No GitLab, and no bulk mode: one issue per run, each with its own approval.
- The repo is the one resolved by preflight; to file elsewhere put it in `repo` and run
  `issue-preflight.sh --repo OWNER/REPO` first. The script puts **every** issue on board
  12, including one filed into another repo — pass `--no-board` when filing outside the
  team repos (`dmc-268-api-t6`, `dmc-268-ui-t6`).
- No board Status is set; labels and milestones must already exist (never created).
- Effort estimates are out of scope; the emitter has no estimate field.
