# Backend rules

## 1. Non-negotiables

- Always use uv for dependencies and running tools (never pip/poetry).
- Never `--no-verify`.
- Run the gates before claiming done: `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy .`, `uv run pytest`.
- No new dependency without a line in the PR body (`uv add`, commit `uv.lock`).
- Never edit files owned by another open PR without a comment there.

## 2. Commands

<!-- SYNC: commands table mirrored in AGENTS.md § Commands — change both together -->

| Task           | Command                                            | Source          |
| -------------- | --------------------------------------------------- | ---------------- |
| Env setup      | `cp .env.example .env`                             | main             |
| Install        | `uv sync`                                          | main             |
| Dev server     | `uv run uvicorn app.main:app --reload`             | main             |
| Lint           | `uv run ruff check .`                              | main             |
| Format         | `uv run ruff format .`                             | main             |
| Format check   | `uv run ruff format --check .`                     | main             |
| Typecheck      | `uv run mypy .`                                    | main             |
| Test           | `uv run pytest`                                    | main             |
| Local infra    | `docker compose up -d`                             | main             |
| Migrate        | `uv run alembic upgrade head`                      | main             |
| New migration  | `uv run alembic revision --autogenerate -m "…"`    | main             |

`uv.lock` is committed; `requires-python = "==3.13.*"`. Pre-commit checks the
root layout: generic hooks cover repository files, Ruff lint covers `app/`,
`tests/` and `alembic/`, Ruff format and mypy cover `app/` and `tests/`.
Migrations are frozen, so they are not reformatted by a hook. Run the commands
above directly before claiming done as the final gates.

## 3. Layout & boundaries

All of this section describes the target layout of `docs/BACKEND_ARCHITECTURE.md`;
on `main` today `app/` holds `bootstrap/`, `common/` and the modules' `infrastructure/` code.

Clean Architecture, module-first: `app/bootstrap/`, `app/common/{application,domain,
infrastructure}`, `app/modules/<m>/{domain,application,infrastructure}`,
`app/entrypoints/{api,webhook,worker,…}`.

1. Dependency rule: `entrypoints → application → domain`, `infrastructure →
   application/domain` — never the other way round.
2. Ports (interfaces) live in the module that consumes them, not the module that
   implements them.
3. `UnitOfWork`: repositories call `flush`, never `commit` — the use case commits.
4. No DB transaction spans an LLM or GitHub call; network calls happen outside the
   transaction boundary.
5. Alembic revisions are frozen: a merged revision is never edited, not even by a
   formatter or an import sorter — changes go into a new revision. Explicit
   `op.create_table` and friends, never `Base.metadata.create_all/drop_all`;
   revision ids are `YYYYMMDD_NNNN`.
6. `StrEnum` maps to a PostgreSQL native `ENUM` type.
7. CHECK constraints and indexes (partial unique ones included) get hand-written
   `ck_/ix_/uq_<table>_…` names. There is no `MetaData` naming convention, so PK,
   FK and most unique constraints keep PostgreSQL's default names; introducing a
   convention is a decision of its own (it would re-prefix the existing `ck_` names).

## 4. Language rules

- Python 3.13 (`requires-python = "==3.13.*"`); full type annotations are mandatory
  (mypy `strict`). Every module starts with `from __future__ import annotations`
  (forward references; matches the api #4 models).
- SQLAlchemy 2 typed models: `Mapped[...]` / `mapped_column`, no SQLModel.
- FastAPI endpoints are `async def`.
- Pydantic DTOs sit at the boundary; wire payloads use camelCase aliases
  (`alias_generator=to_camel` — pending role 6 decision).
- No bare `except`; never silently swallow an error.
- No business logic in routers — routers decode the transport contract and call a
  use case.
- Enums are `StrEnum`.
- Ruff: `select = ["E", "F", "I", "UP", "B", "SIM"]`, line length 100.

## 5. Testing

- One file per feature: `tests/test_<feature>.py`.
- Plain `pytest`, no `pytest-asyncio` plugin — drive async code with `asyncio.run`.
- Unit tests exercise use cases against fakes of the ports (Protocols), not real
  infrastructure.
- Integration tests are marked `@pytest.mark.integration` (registered in
  `pyproject.toml`) and skip when `TEST_DATABASE_URL` is unset; the skip lives in
  each DB test file's fixture — there is no shared `conftest.py` yet.
- Assert literal values taken from the spec — never derive an expected value from
  the implementation under test.

## 6. Review focus

Order: `security → correctness → performance → readability`. What linters already
enforce (formatting, import order, unused vars, quotes, semicolons) is not review
material — leave it to the linters and the formatter.

## 7. References

- [README.md](../../README.md) — setup, run, lint/format/typecheck/test commands
  (this repo, `main`).
- `docs/BACKEND_ARCHITECTURE.md` — Clean Architecture layout in
  full.
- `docs/SYSTEM_DESIGN.md` — product architecture (this repo, `main`).
- `.agents/skills/` — skill catalog (frontmatter contract in `.agents/README.md`).
- `review/README.md` — the AI reviewer product's own
  prompts and rule sets, not covered by this file.
- `AGENTS.md` — cross-tool entry point (decided in [dmc-268-ui-t6#32](https://github.com/larchanka-training/dmc-268-ui-t6/issues/32)).
