# Tests and examples

<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/implement-issue/references/tests.md -->

`examples/NN-<case>/` hold the golden cases: `README.md` (invocation, what the skill does),
`fixtures/` (GraphQL pages for the offline seam `IMPLEMENT_ISSUE_FIXTURE_DIR`) and `expected.json`
(the exact preflight output); `tests/` runs them and the edge cases offline. From the repo root:

```bash
# api
uv run pytest .agents/skills/implement-issue/tests
uv run mypy .agents/skills/implement-issue/scripts/*.py .agents/skills/implement-issue/tests
# ui — no Python project; pytest is fetched ephemerally, nothing is added to dependencies
uv run --no-project --with pytest pytest -p no:cacheprovider .agents/skills/implement-issue/tests
```

**Not in the required CI job, by decision** (as for `create-issue`): api's `testpaths` never
collects them and ui has no Python in CI. Run them by hand whenever the skill changes, then
`diff -r` against the other repo (`.agents/README.md`). api's `ruff` covers the `.py` files;
`mypy .` skips dot-directories, hence the explicit line.
