# .agents

## Layout decision

`.agents/` is the source of truth for agent rules, skills, and agent definitions
in both `dmc-268-ui-t6` and `dmc-268-api-t6`. Harness-specific paths are symlinks
onto it, not copies: `.claude/skills -> ../.agents/skills` and
`.claude/agents -> ../.agents/agents` (committed as symlink objects).

Rationale: the course board's Definition of Ready/Done, `docs/SYSTEM_DESIGN.md`
§15 (OQ-4), and the 2026-08-17 lecture all converge on `.agents/` over
`docs/agents/`. Codex reads `.agents/skills` natively; Claude Code reads
`.claude/skills` — the symlink lets one file tree serve both without
duplication. Decided in
[dmc-268-ui-t6#32](https://github.com/larchanka-training/dmc-268-ui-t6/issues/32)
(2026-09-24), closing SYSTEM_DESIGN §15 OQ-4.

## Harness matrix

| Harness     | Reads                                                                            | Notes                                                                                                                                            |
| ----------- | -------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| Claude Code | `CLAUDE.md` (imports `AGENTS.md`), `.claude/skills`, `.claude/agents` (symlinks) | skills + agents via symlinks                                                                                                                     |
| Codex       | `AGENTS.md`, `.agents/skills`                                                    | `agents/` is not loaded (Codex agents are `.codex/agents/*.toml`)                                                                                |
| Cursor      | `AGENTS.md` (root and nested)                                                    | manual `@`-reference to `.agents/**`                                                                                                             |
| Gemini CLI  | `GEMINI.md` only, by default                                                     | add `AGENTS.md` to `context.fileName` in project `.gemini/settings.json` or user `~/.gemini/settings.json`; manual `@`-reference to `.agents/**` |
| Copilot     | `AGENTS.md`: cloud agent, code review, VS Code Chat, Copilot CLI                 | not read by Visual Studio or by JetBrains / Eclipse Copilot Chat                                                                                 |

Copilot's cloud agent and Copilot CLI also accept a root `CLAUDE.md`; which file wins when both
exist, and whether its `@AGENTS.md` line is resolved, is not documented. `CLAUDE.md` holds no
rules of its own either way.

## Sub-agents across harnesses

Skills and agents here name Claude Code's sub-agent tools. The equivalent per harness:

- **Claude Code** — the `Agent` tool (`subagent_type: general-purpose`, or `Explore` for
  read-only exploration).
- **Codex** — `spawn_agent` with a self-contained message and no inherited history. Default
  `multi_agent` (v1, on by default): `fork_context: false` or omit it; with `multi_agent_v2`:
  `fork_turns: "none"` (its default is `all`).
- **No sub-agent tool** — run each independent check as a separate fresh session
  (`codex exec --ephemeral "<prompt>"` or `claude -p "<prompt>"`) and bring back only its
  verdict. Never run the checks one after another in the same session — that defeats the
  clean context the skills rely on.

## Directory map

- `rules/` — stack and workflow rules (`backend.md`, `git-workflow.md`).
- `skills/` — agent skills, one `SKILL.md` per directory (7 here — no
  `manual-automation`).
- `agents/` — agent definitions (`name`, `description`, `model` frontmatter).
- `templates/backend/` — code/test templates with proof blocks. Fenced blocks
  preceded by `<!-- proof: <file> -->` were proved once, at authoring time,
  against the api base `b3a3505` (lint/typecheck/tests) with role 7's local gate
  (not in this repo); ui: file name under one root; api: `app/…` or `tests/…`
  prefix selects the root. Nothing re-checks them; re-prove after dependency bumps.

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

`AGENTS.md` is the root entry point; role 1 (tech lead) owns it, changes go
through a PR. `CLAUDE.md` only imports it.
