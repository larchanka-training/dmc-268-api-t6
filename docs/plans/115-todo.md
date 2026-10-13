# Tasks #115

## 1. SYSTEM_DESIGN (S, 1 file)

**Description:** Update the two relevant §7.3 bullets using the symbols in
[115-plan.md](115-plan.md).

**Acceptance criteria:**

- [x] Seven adapter examples, `authorized_run` and the delegating
      `ReviewsApiResources.run_repository` factory follow the same fail-closed rule:
      construction or invocation without scope raises `ValueError`; bypass requires
      explicit `allow_unscoped=True` from a trusted internal caller.
- [x] All three cache creation sites use file/symbol links without line numbers;
      production `github_adapters` composition is distinct from the obsolete
      `review_worker` prototype. App/client prerequisites, process-local semantics,
      the 60-second skew and Redis/#62 are preserved.
- [x] SYSTEM_DESIGN changes stay within the two bullets; neighboring documentation
      and scoped rerun's shared authorization predicate are preserved.

**Verification:** Compare signatures, constructors and functions using `rg` and
source reads; resolve links and inspect the diff.
**Dependencies:** Plan approval, overlapping PR diff checks and required scope notices.
**File:** `docs/SYSTEM_DESIGN.md`.

## 2. Backend rule (S, 1 file)

**Description:** Align the fail-closed rule in backend §3 with SYSTEM_DESIGN.

**Acceptance criteria:**

- [x] The rule covers adapters, authorization helpers and factories accepting
      `scope: AuthScope | None`, with seven adapter examples and explicit internal bypass.
- [x] Both documents use matching example wording, adapter order and helper/factory roles.
- [x] Mirror requirements are checked against README/SYNC: backend is per-repo;
      AGENTS needs no edit when its mirrored rules are unchanged.

**Verification:** Compare documentation, code, the sync map and the final diff.
**Dependencies:** Task 1. **File:** `.agents/rules/backend.md`.

## Checkpoint after tasks 1–2

- [x] Both #115 acceptance criteria are supported by code. Links and symbols resolve;
      the scope excludes runtime/OpenAPI changes and synthetic documentation tests.
- [x] Plan artifacts are in English and contain no transient PR, reviewer or issue status.

## 3. Verification and review (S, checks and evidence)

**Description:** Run the required gates and submit the diff for independent
Standards/Spec review. Record dated verification evidence below; review and
publication requirements follow the process in the plan.

**Verification:**

- [x] `uv run ruff check .`
- [x] `uv run ruff format --check .`
- [x] `uv run mypy .`
- [x] `uv run pytest`
- [x] `git diff --check`, final diff read and link/symbol verification.

**Dependencies:** Tasks 1–2. **File:** `docs/plans/115-todo.md` (verification evidence).

## Verification evidence

Snapshot: **2026-10-12**, against the documentation tree rebased onto main commit
`46a90772f65c04aa3cc4db11b198e6d052bf1207`.

- Exactly four documentation files changed: the two §7.3 bullets, backend §3 and
  these plan artifacts. Runtime, OpenAPI, dependencies and test code are unchanged.
- Source search and AST checks confirm three cache creation sites and nine optional
  AuthScope signatures: seven adapter constructors, `authorized_run` and the factory
  `ReviewsApiResources.run_repository`. All seven guards, helper enforcement, factory
  delegation and production/prototype composition were checked against the code.
- All local links in the edited rule, bullets and plan artifacts resolve;
  linked authorization symbols exist and both adapter example lists have the same order.
  `git diff --check` is clean. The mirror rationale was checked against README/SYNC,
  the `.claude` symlinks and `CLAUDE.md`'s AGENTS import.
- `uv run ruff check .`: **All checks passed!**
- `uv run ruff format --check .`: **353 files already formatted**.
- `uv run mypy .`: **Success: no issues found in 350 source files**.
- `uv run pytest`: **2444 passed, 0 skipped, 2 warnings in 210.71s**.
  Both `TEST_DATABASE_URL` and `TEST_RABBITMQ_URL` were configured against isolated,
  disposable PostgreSQL 17 and RabbitMQ 4 containers. Existing containers were not used.
  The warnings were the Starlette `httpx` TestClient deprecation and the AnyIO
  `BlockingPortal` alias deprecation.
