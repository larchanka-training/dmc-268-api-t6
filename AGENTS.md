# AGENTS.md — dmc-268-api-t6

## Project

AI Code Reviewer, backend (`dmc-268-api-t6`) — FastAPI service for the code-review
product; serves the Vite/React frontend (`dmc-268-ui-t6`).

## Stack

FastAPI, SQLAlchemy 2 (async), Alembic, Pydantic 2, httpx, pytest, uv, Python 3.13.

## Non-negotiables

<!-- SYNC: non-negotiables mirror .agents/rules/backend.md §1 (transaction rule: §3.4) -->

- Always use uv for dependencies and running tools (never pip/poetry).
- Never `--no-verify`.
- Run the gates before claiming done: `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy .`, `uv run pytest`.
- No new dependency without a line in the PR body (`uv add`, commit `uv.lock`).
- Never edit files owned by another open PR without a comment there.
- No DB transaction spans an LLM or GitHub call; network calls happen outside the
  transaction boundary.

## Commands

<!-- SYNC: commands table mirrors .agents/rules/backend.md §2 -->

| Task          | Command                                         | Source |
| ------------- | ----------------------------------------------- | ------ |
| Env setup     | `cp .env.example .env`                          | main   |
| Install       | `uv sync`                                       | main   |
| Dev server    | `uv run uvicorn app.main:app --reload`          | main   |
| Lint          | `uv run ruff check .`                           | main   |
| Format        | `uv run ruff format .`                          | main   |
| Format check  | `uv run ruff format --check .`                  | main   |
| Typecheck     | `uv run mypy .`                                 | main   |
| Test          | `uv run pytest`                                 | main   |
| Local infra   | `docker compose up -d`                          | main   |
| Migrate       | `uv run alembic upgrade head`                   | main   |
| New migration | `uv run alembic revision --autogenerate -m "…"` | main   |

## Layout

Clean Architecture, module-first (`app/bootstrap`, `app/common`,
`app/modules/<m>/{domain,application,infrastructure}`); dependency rule
`entrypoints → application → domain`; `app/entrypoints/` is the target layout per
`docs/BACKEND_ARCHITECTURE.md` (not on `main` yet); details in
`.agents/rules/backend.md`.

## Conventions

- Branches: `feat/<slug>`, `fix/<slug>`, `docs/<slug>`, `chore/<slug>` (also for
  `refactor`/`test`/`ci`/`perf`/`style` work), `deps-update-YYYY-MM-DD`, slug
  preferably `<issue>-<kebab-case>`.
- Commits: Conventional Commits, reference the issue (`(#N)` in the subject
  or `Refs #N` in the footer).
- PR title: conventional, ≤72 characters.
- PR body: `What` / `Why` / `How to verify` / `Refs`, in that order.
- One approving review required before merge.
- Rebase on `main` before requesting review.

## Where the details live

- `.agents/README.md` — layout, harness matrix, sync map.
- `.agents/rules/` — stack and git-workflow rules.
- `.agents/skills/` — agent skills (agent-loop, code-review, tdd,
  pull-request, planning-and-task-breakdown, qa, e2e-test).
- `.agents/agents/` — agent definitions.
- `.agents/templates/` — code/test templates with proof blocks.
- `docs/BACKEND_ARCHITECTURE.md` — Clean Architecture layout in full.
- `docs/SYSTEM_DESIGN.md` — product architecture (this repo, `main`).
- `review/` — the AI reviewer product's own prompts and rule sets, not covered here.

## What NOT to do

- Do not add a dependency without a line in the PR body.
- Do not let a repository call `commit()` — only the use case commits;
  repositories only `flush()`.
- Do not put business logic in a router — decode the transport contract and
  call a use case.
- Do not skip the gates (`uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy .`, `uv run pytest`) before claiming a task done.
