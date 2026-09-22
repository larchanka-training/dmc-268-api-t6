# .agents

## Layout decision

`.agents/` is the source of truth for agent rules, skills, and agent definitions
in both `dmc-268-ui-t6` and `dmc-268-api-t6`. Harness-specific paths are symlinks
onto it, not copies: `.claude/skills -> ../.agents/skills` and
`.claude/agents -> ../.agents/agents` (committed as symlink objects).

Rationale: the course board's Definition of Ready/Done, `docs/SYSTEM_DESIGN.md`
§15 (OQ-4) of `dmc-268-ui-t6`, and the 2026-08-17 lecture all converge on
`.agents/` over `docs/agents/`. Codex reads `.agents/skills` natively; Claude
Code reads `.claude/skills` — the symlink lets one file tree serve both without
duplication. This is role 7's decision, pending ratification by role 1
(tech lead); see `proposals/agents-md-draft.md`.

## Harness matrix

| Harness     | Reads                        | Notes                                |
| ----------- | ----------------------------- | ------------------------------------ |
| Claude Code | `.claude/skills` (symlink)   | native skill discovery               |
| Codex       | `.agents/skills`             | native, no symlink needed            |
| Cursor      | `AGENTS.md` (pending role 1) | manual `@`-reference to `.agents/**` |
| Gemini CLI  | `AGENTS.md` (pending role 1) | manual `@`-reference to `.agents/**` |
| Copilot     | `AGENTS.md` (pending role 1) | manual reference — verify per tool   |

## Directory map

- `rules/` — stack and workflow rules (`backend.md`, `git-workflow.md`).
- `skills/` — agent skills, one `SKILL.md` per directory (7 here — no
  `manual-automation`).
- `agents/` — agent definitions (`name`, `description`, `model` frontmatter).
- `templates/backend/` — code/test templates with proof blocks. Fenced blocks
  preceded by `<!-- proof: <file> -->` are extracted and run through
  lint/typecheck/tests by role 7's local gate before merge; ui: file name
  under one root; api: `app/…` or `tests/…` prefix selects the root.
- `proposals/` — drafts owned by other roles (e.g. `agents-md-draft.md`).

## Sync map (ui ↔ api)

These files are byte-identical between the two repos, marked with a
`<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/<path> -->` line:

- `rules/git-workflow.md`
- `skills/agent-loop/**`, `skills/code-review/**`, `skills/pull-request/**`,
  `skills/planning-and-task-breakdown/**`, `skills/qa/**`
- `agents/*.md`

Everything else (`rules/frontend.md` / `rules/backend.md`, stack-specific
skills, `templates/`) is per-repo.

Shared files: edit in `dmc-268-ui-t6`, `cp` to `dmc-268-api-t6`, then
`diff -r` the two paths to confirm byte-identity before committing either.

## How to add a skill

Each `skills/<dir>/SKILL.md` needs frontmatter: `name` (matches the directory),
`description` (third person, "This skill should be used when…"), and a
`metadata` block (`version: 1.0.0`, `source: instructor-pack`,
`adapted-for: frontend|backend|any`). Budget scales with the source: about
1.3× the instructor-pack original plus frontmatter; see existing `SKILL.md`
files for precedent (templates ≤150 lines). Write it in English.

## Product review prompts

The review prompts and rule sets for the AI reviewer product live in
`review/` of THIS repo (role 7) — see `review/README.md`. `.agents/` covers
how _we_ build, not what the bot reviews.

## AGENTS.md

`AGENTS.md` is owned by role 1 — see `proposals/agents-md-draft.md` for the
draft this repo contributes.
