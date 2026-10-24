# Task list: OS CPU limit for the parser worker (#168, part 6)

See [168-parser-cpu-limit-plan.md](168-parser-cpu-limit-plan.md) for the design. The plan is the source of
truth and this list adds no scope.

- Baseline: `origin/main` `1ae74b1a`. Branch: `fix/168-parser-cpu-limit`, one PR.
- Each task is one Conventional commit whose subject ends with `(#168)`.
- Each code task is test-first: write the named tests, run them, record the red output, then write the fix and
  commit the test and the fix together.

**Common verification**, before every commit:

```
uv run ruff check .
uv run ruff format --check .
uv run mypy . --exclude '^docs/reports/'
TEST_DATABASE_URL=postgresql+psycopg://app:app@localhost:5432/imp168c \
TEST_RABBITMQ_URL=amqp://app:app@localhost:5672/imp168c uv run pytest tests/test_ast_worker.py \
  tests/test_subprocess_source_parser.py tests/test_code_index_indexer_postgres.py \
  tests/test_code_index_lifecycle_postgres.py
```

## Task 0 — plan

- [ ] `docs(plans): plan the parser CPU limit (#168)`: this plan and task list, staged with `git add -f`.

## Task 1 — per-request CPU limit

- [ ] Tests from the plan's "Tests" section in `tests/test_ast_worker.py` and the fake worker
  `tests/ast_workers/cpu_bound.py`; record the red run.
- [ ] `ast_worker.py`: `_CPU_SLACK_SECONDS = 20`, `_cpu_soft_limit(used)`, `_limit_cpu()`, called in `main()`
  after reading a request and before `_handle`; the module docstring names the OS CPU cap.
- [ ] `fix(reviews): cap parser worker CPU time per request (#168)`.
