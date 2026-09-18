# DRAFT proposed by role 7 (issue #18) — role 1 owns `AGENTS.md`; this file is NOT `AGENTS.md`

Backend variant — the ui repo carries its own draft at the same path. Role 1 can adopt
this content as-is for `dmc-268-api-t6/AGENTS.md`.

## Project

AI Code Reviewer, backend (`dmc-268-api-t6`) — FastAPI service for the code-review
product; serves the Vite/React frontend (`dmc-268-ui-t6`).

## Stack

FastAPI, SQLAlchemy 2 (async), Alembic, Pydantic 2, httpx, pytest, uv, Python 3.13.

## Non-negotiables

- Always use uv for dependencies and running tools (never pip/poetry).
- Never `--no-verify`.
- No new dependency without a line in the PR body (`uv add`, commit `uv.lock`).
- No DB transaction spans an LLM or GitHub call; network calls happen outside the
  transaction boundary.

## Commands

| Task          | Command                                         | Source          |
| ------------- | ------------------------------------------------ | ---------------- |
| Install       | `uv sync`                                       | main             |
| Dev server    | `uv run uvicorn app.main:app --reload`          | main             |
| Lint          | `uv run ruff check .`                           | main             |
| Format check  | `uv run ruff format --check .`                  | main             |
| Typecheck     | `uv run mypy .`                                 | main             |
| Test          | `uv run pytest`                                 | main             |
| Migrate       | `uv run alembic upgrade head`                   | pending api #4   |

## Layout

Clean Architecture, module-first (`app/bootstrap`, `app/common`, `app/modules/<m>/
{domain,application,infrastructure}`); dependency rule `entrypoints → application →
domain`; details in the stack rules file in `.agents/rules/`.

## Conventions

- Branches: `feat/<slug>`, `fix/<slug>`, `docs/<slug>`, `chore/<slug>`,
  slug preferably `<issue>-<kebab-case>`.
- Commits: Conventional Commits, reference the issue (`(#N)` in the subject
  or `Refs #N` in the footer).
- PR title: conventional, ≤72 characters.
- PR body: `What` / `Why` / `How to verify` / `Refs`, in that order.
- One approving review required before merge.
- Rebase on `main` before requesting review.

## Where the details live

- `.agents/rules/` — stack and git-workflow rules.
- `.agents/skills/` — agent skills (agent-loop, code-review, tdd,
  pull-request, planning-and-task-breakdown, qa).
- `.agents/templates/` — code/test templates with proof blocks.
- `review/` — the AI reviewer product's own prompts and rule sets, not covered here.

## What NOT to do

- Do not add a dependency without a line in the PR body.
- Do not let a repository call `commit()` — only the use case commits;
  repositories only `flush()`.
- Do not put business logic in a router — decode the transport contract and
  call a use case.
- Do not skip the gates (`uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy .`, `uv run pytest`) before claiming a task done.

## Note for role 1

`CLAUDE.md` should be the single line `@AGENTS.md`.
