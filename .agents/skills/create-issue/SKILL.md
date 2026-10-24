---
name: create-issue
description: This skill should be used when the user wants to file, open, raise or write up a GitHub issue, bug report or task from a conversation — resolving the repo from the git remote, enforcing a Definition-of-Ready gate, previewing the full body for explicit approval, guarding against duplicates and putting the issue on the course board (larchanka-training project 12). Also when asked to turn something just discussed into a tracked task, to slice a large task into PR-sized parts, or to file a batch of linked issues.
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
without the user's explicit **Create** («Создать»), so the worst a stray trigger can do is
offer a draft.

## Preconditions

- `gh` authenticated with scopes `repo` and `project` (`gh auth status`); without `project`
  the run stops before the create (exit 3) unless `--no-board` — `gh auth refresh -s project`.
- bash 4+ (`mapfile`), GNU `find`, `curl` and python3 ≥ 3.10 (stdlib only) on `PATH`.
- Run from the clone the issue belongs to. Paths in this file are from the repo root.

## Scripts — the agent never calls the REST APIs directly; the scripts do

All in `.agents/skills/create-issue/scripts/`:

| Script                                                                     | Does                                                                                 |
| -------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| `issue-preflight.sh [--repo OWNER/REPO] [--remote NAME]`                   | Resolve the repo from git remotes; prove it is reachable, authenticated and fileable |
| `build_issue_body.py <spec.json> [--emit-args \| --emit-links]`            | Spec → markdown body, the `gh` flag vector, or the resolved native links             |
| `issue-create.sh <spec.json> [--dry-run] [--allow-duplicate] [--no-board]` | Validate → duplicate guard → create once → read back → board + Status → links        |
| `issue-link.sh OWNER/REPO#N [--parent REF] [--blocked-by REF]...`          | Set native links on an existing issue, idempotently, each read back                  |
| `issue-link.sh OWNER/REPO --check [--parent REF] [--blocked-by REF]...`    | Look the link targets up for an issue not created yet; writes nothing                |
| `issue-batch.py <batch.json> [--dry-run] [--no-board]`                     | A batch of issues: validate, preview, create in order once, resume on a rerun        |
| `issue-batch.py --compare-shape GOLDEN CANDIDATE`                          | Compare two batch files by shape — how the golden `examples/` are checked            |

Creating labels or milestones, comments, closing: plain `gh` calls — `references/hosts.md`.

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
which parts were invented. Dependencies go by direction: "this issue is blocked by X" →
`blocked_by`; an issue this one blocks has no key — after the create run
`issue-link.sh OWNER/REPO#<other> --blocked-by <this>`, `<this>` as its full `OWNER/REPO#N`
(a short `#N` resolves against `<other>`'s repo); "related, not blocking" stays text.

### 4. Language

Team issues are Russian: set `"lang": "ru"` **explicitly** (the emitter's default is `en`;
headings stay English — `references/templates.md` § Language). For a repo with another
convention, follow its recent issues (`gh issue list --repo OWNER/REPO --limit 5 --json title`).

### 5. Definition of Ready

Three hard blockers — do not create until resolved, ask the user:

- a clear title describing **one** piece of work, in the form `[component] description`;
- at least one acceptance criterion — with `parts`, each part's own, its PR boundary and `done_when`;
- a `kickoff` block with `repo`, one `read_first` entry and `problem_shape`, when the
  issue is technical (a Bug or Task, or anything naming a service or a path). Purely
  non-technical issues (a decision to record) are exempt.

Two soft warnings — mention, then proceed: dependencies unstated ("None" is fine); no
obvious way to test it. An issue without acceptance criteria is a note to self.

**Slicing** — more than one PR or repo, or independent parts: slice before the spec, into
parts of one issue or sub-issues (`references/slicing.md`, goldens in `examples/`).
**Batch** — several issues go through `issue-batch.py` (`references/batch.md`).

### 6. Build the spec and the body

Write the spec under `$(mktemp -d)` — **not** `$TMPDIR/…` (unset on Linux: writes to `/`):

```bash
SPEC="$(mktemp -d)/spec.json"
cat > "$SPEC" <<'JSON'
{ "repo": "<OWNER/REPO from preflight>", "type": "Task", "lang": "ru",
  "summary": "[component] Короткий заголовок в повелительном наклонении",
  "background": "…", "scope": ["…"], "acceptance_criteria": ["…"],
  "dependencies": {"Блокирующие": ["…"], "Связано, не блокирует": ["…"]}, "references": ["…"],
  "kickoff": {"repo": "…", "branch": "fix/<n>-<slug> — `main` защищён, через PR",
              "problem_shape": "…", "read_first": ["…"],
              "before_writing_code": ["…"], "mines": ["…"]},
  "parent": "#139", "blocked_by": ["#130", "larchanka-training/dmc-268-ui-t6#93"],
  "milestone": "Спринт 3", "status": "Todo", "assignees": ["<github-login>"] }
JSON
.agents/skills/create-issue/scripts/build_issue_body.py "$SPEC"
```

Never hand-write the body. The emitter fixes the section order, neutralises markdown that
would let user text forge a heading, and always emits `Dependencies` and `DoD Checklist`.
It refuses (exit 2) rather than drops: an unknown key, a value of the wrong type, a Bug
with story fields (or the reverse), a newline in the title, an unclosed code fence, a ref
that is not `#N` / `OWNER/REPO#N`, a `parent` of another owner. `parent` / `blocked_by`
become native links after the create; the body is unchanged. Field contents, dependency
shapes, the default DoD, titles, language and reference hygiene: `references/templates.md`.

`issue-create.sh "$SPEC" --dry-run` prints the body, `milestone`, `status`, resolved `links`
and the exact-title `duplicate` (or null), creating nothing. It is **not offline**: it runs
preflight, the validation (milestone open in the repo, Status a board option, every link
target an existing issue) and the duplicate search, so a bad value fails the preview.

### 7. Preview, then confirm

Print the **full rendered body as ordinary text** — an approval widget truncates. Above
it, a short header:

```
Repo:       <OWNER/REPO from preflight>
Type:       Task  (label: enhancement)
Assignee:   <login>
Language:   ru
Milestone:  <title, or —>
Parent:     <OWNER/REPO#N, or —>
Blocked by: <OWNER/REPO#N, …, or —>
Board:      projects/12, Status: Todo   (the default; --no-board skips both)
Duplicate:  <url (state, author) from the dry-run, or —>
```

Then ask for confirmation. The preview alone is never a green light. A batch gets one
preview and ONE confirmation for the whole set (`references/batch.md` § 3).

**Confirmation across harnesses** (same idea as "Sub-agents across harnesses" in
`.agents/README.md` — the step must finish in every harness):

- **Claude Code** — call `AskUserQuestion`: question "Create this issue?", options
  **Create** / **Edit fields** / **Cancel**.
- **Codex and any harness without a structured-question tool** — print the same preview,
  then ask: "Reply `Create` / «Создать» to post this issue, `Edit` / «Изменить» to change
  something, or `Cancel` / «Отмена»." Only `Create` or «Создать» posts; "ok", "да" or
  silence do not.

On **Edit**, ask what to change, rebuild, re-print the full preview, ask again. On
**Cancel** or no answer, stop and create nothing.

### 8. Create

```bash
.agents/skills/create-issue/scripts/issue-create.sh "$SPEC"      # add --no-board to skip the board
```

After the validation: an exact-title duplicate search (exit 4 unless `--allow-duplicate`;
**fail-open** — a failed search warns and the create proceeds), one create, a REST
read-back. A create that returns nothing useful is never retried: the newest issues are
listed (a plain listing — the search index lags) and only a same-title issue that was not
there before and was opened by you is adopted, never an older duplicate or a teammate's.
If that lookup fails the outcome is unknown (exit 5): check the repo by hand before
retrying. Then board 12 with its Status (read back, re-set once if the board's automation
overwrote it) and the native links through `issue-link.sh`, each read back. A failed link
never undoes the issue: exit 7, stderr ends with `repair: <issue-link.sh command>` — run
only that; a rerun skips what is in place and never replaces a parent.

Report the URL, `board` (`added` / `skipped` / `failed`), `status`, `links` and `verified`.
A `verified: false` means the title or milestone read back differs from the spec — say so
and look at the issue before anything else. Success is the read-back, not create's exit.

## Exit codes

| Code | Meaning                                                                                                                                                                          |
| ---- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 0    | created — read `verified` (it can be `false`), `board`, `status` and `links` in the JSON                                                                                         |
| 2    | usage, bad spec or ref, unknown key, non-GitHub remote, attribution guard; milestone not open, unknown Status, `status` with `--no-board`, link target missing or a pull request |
| 3    | not authenticated (`gh auth login`), or no `project` scope with the board on                                                                                                     |
| 4    | 4xx, issue tracker disabled or repo not visible, or an exact-title duplicate blocked (its `url`, `state`, `author` on stdout)                                                    |
| 5    | 5xx, network/TLS failure or a rate-limited 403, or a failed create whose outcome could not be checked                                                                            |
| 6    | **issue created**, board step (item-add or Status) failed — never re-run the create; run only the `gh` commands stderr names                                                     |
| 7    | **issue created**, a native link failed (wins over 6) — never re-run the create; run only the `repair:` command from stderr                                                      |

## Gate before creating

Every line PASS, or stop. No partial.

- [ ] Target resolved by `issue-preflight.sh`, exit 0
- [ ] DoR hard blockers cleared (title, ≥1 acceptance criterion, kickoff when technical)
- [ ] Local-path sweep over the spec — run it, do not eyeball it:
      `python3 -m json.tool "$SPEC" | grep -nE '~/|/home/|/Users/|/tmp/|\$TMPDIR|\.\./'`
      Every hit is rewritten per reference hygiene or keeps its precondition in the same line.
- [ ] Body produced by `build_issue_body.py`, not written by hand
- [ ] The user confirmed **Create**, by the structured question or the word `Create` / «Создать»

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
uv run mypy .agents/skills/create-issue/scripts/*.py .agents/skills/create-issue/tests
# ui — no Python project; pytest is fetched ephemerally, nothing is added to dependencies
uv run --no-project --with pytest pytest -p no:cacheprovider .agents/skills/create-issue/tests
```

**Not in the required CI job, by decision:** api's `testpaths = ["tests"]` never collects
them and ui has no Python in CI — a CI step must name the path, or it is green because
nothing ran. Run them by hand whenever the skill changes (api's `ruff check .` and
`ruff format --check .` do cover the `.py` files; `mypy .` skips dot-directories, hence
the explicit line), then `diff -r` against the other repo (`.agents/README.md`).

## Limits

- GitHub only, no GitLab. Several issues: one batch, one approval (`references/batch.md`).
- The repo is the one resolved by preflight; to file elsewhere put it in `repo` and run
  `issue-preflight.sh --repo OWNER/REPO` first. The script puts **every** issue on board
  12, including one filed into another repo — pass `--no-board` when filing outside the
  team repos (`dmc-268-api-t6`, `dmc-268-ui-t6`).
- The board Status is set (default `Todo`); labels and milestones must exist, never created.
- Effort estimates are out of scope; the emitter has no estimate field.
