# Task list: AST resolver and parse errors (#168, part 2)

See [168-ast-parse-errors-plan.md](168-ast-parse-errors-plan.md) for the design. That plan is the
source of truth; this list adds no scope. AC1–AC3h and D1–D6 are named there. Baseline
`origin/main` `22c621ab`, branch `fix/168-go-parse-errors`, one PR.

- Each task is one Conventional commit whose subject ends with `(#168)`.
- Tasks 1, 2 and 5 are test-first: write the named tests, record the RED run, then write
  the fix.
- Tasks 3–4 are characterization tests and are expected green on the baseline. A red run
  is a defect outside the plan: stop and report it.
- Every code-writing task is delegated through a brief file that says: never read `.env`
  of any clone.
- Fixture sources below are the ones the planning probes confirmed on `22c621ab`.

**Common verification** (each task, before its commit):

```
uv run ruff check .
uv run ruff format --check .
uv run mypy . --exclude '^docs/reports/'
TEST_DATABASE_URL=postgresql+psycopg://app:app@localhost:5432/imp168a \
TEST_RABBITMQ_URL=amqp://app:app@localhost:5672/imp168a uv run pytest
```

### Task 0: Plan documents

**Commit:** `docs(plans): plan the ast parse-error fixes (#168)`

**Files:** `docs/plans/168-ast-parse-errors-plan.md`, `docs/plans/168-ast-parse-errors-todo.md`.

**Done when:** the tech lead's answers to Q1–Q4 are written into the plan; only the two
files are committed (`git add -f`: `docs/plans/` is in the local `.git/info/exclude:14`).

**Dependencies:** none.

### Task 1: Refuse resolution through a target with parse errors

**Commit:** `fix(reviews): refuse to resolve through a target with parse errors (#168)`

**Files:** `app/modules/reviews/application/ast_resolution.py` (`_load`, `:638-647`);
`tests/test_ast_resolution.py`; `tests/test_review_context.py` (mid-file; see Coordination).

**What to do:** D1 with the reason from D2 (Q1). In `_load`, loaded facts with
`has_errors` return `FileUnavailable(AstFailureReason.AST_FAILED.value)`. Keep the comment
at `:643-644` true for both causes.

**Tests to add (RED on the baseline unless marked):**

- Go, in or beside `test_go_resolution_is_refused_when_the_package_is_not_fully_known`
  (`tests/test_ast_resolution.py:417-462`). The fixture is the `store` package, and
  `cmd/main.go` calls `store.Open()`. Each case is expected to give
  `(UNSUPPORTED, "ast_failed")` with `resolved_symbols == ()`:
  - no duplicate: `internal/store/broken.go` = `"package store\n\nfunc Close( {\n}\n"`
    (today `resolved`);
  - declaration hidden in the ERROR region: `broken.go` =
    `"package store\n\nvar x = (\nfunc Open() error { return nil }\n"`. First assert the
    premise on its facts: `has_errors is True` and no symbol is named `Open` (today
    `resolved` to `open.go`);
  - a broken package clause beside a valid member: `"packag store\n\nfunc Close() {}\n"`
    (today `several_package_names`);
  - the only member with a broken clause: `open.go` = `"packag store\n\nfunc Open() error
    { return nil }\n"` (today no ref at all);
  - pin, green on the baseline: `unavailable={"internal/store/open.go":
    "source_decode_failed"}` gives `(UNSUPPORTED, "source_decode_failed")`.
- Python and TS target, file level (D3, Q2), both RED today with `resolved`:
  - an error elsewhere in the target: Python `"class User:\n    pass\n\ndef broken(:\n
    pass\n"`, TS `"export class User {}\nexport function broken( { }\n"`;
  - an error over the symbol: Python `"class User(:\n    pass\n"`, TS
    `"export class User { x = ; }\n"`.

  Both variants are expected to give `(UNSUPPORTED, "ast_failed")`.
- AC3e, "not parsed" for each language. Suggested name
  `test_import_target_that_is_not_parsed_keeps_the_parse_reason`, parametrized over a target
  of each of `.py`, `.ts`, `.tsx`, `.js`, `.jsx` and `.go`:
  - the target has a syntax error: `(UNSUPPORTED, "ast_failed")`, RED;
  - `unavailable={target: "ast_timeout"}`: `(UNSUPPORTED, "ast_timeout")`, green on the
    baseline.
- Context level, AC2a, in `tests/test_review_context.py`. Use `_service_repository` with
  `pkg/models.py` replaced by a `User` class that has a syntax error over it. Expected:
  - `[(r.resolution, r.reason) for r in payload.imports] == [(UNSUPPORTED, "ast_failed")]`;
  - no `ContextOmission("ast_limit", "pkg/models.py")`;
  - `ContextLevel.L4 not in payload.levels_present`.

  Today the ref is `resolved` with that `ast_limit` omission.

**Done when:**

- [x] RED and GREEN runs are recorded;
- [x] `go_package_target`, `go_package_name` and `review_context.py` are unchanged;
- [x] common verification is green.

**Dependencies:** Task 0.

### Task 2: Report wildcard and dot imports the changed code uses

**Commit:** `fix(reviews): report wildcard and dot imports the changed code uses (#168)`

**Files:** `app/modules/reviews/application/ast_resolution.py` (`resolve`, `:510-583`);
`tests/test_ast_resolution.py`.

**What to do:** D4 (Q3).
- An alias-less `WILDCARD` binding that the symbol "uses" goes through the limit check and
  `_resolve`, which returns `no_named_symbol` at `:610-611`. Nothing is loaded.
- `SIDE_EFFECT` stays skipped.
- Suggested helper: a private `_uses_wildcard(facts, symbol)`.

**Tests to add (RED on the baseline):**

- Flip `tests/test_ast_resolution.py:270`. `from .models import *` with `return User()`
  now gives exactly one `ImportRef` with `module=".models"`, `names=()`,
  `resolved_symbols=()` and `(UNSUPPORTED, "no_named_symbol")`. Keep `:271`:
  `provider.asked == []`.
- Go dot-import: `import . "example.com/m/internal/store"` with `return Open()` gives the
  same shape, and `provider.asked == []`.
- Negatives, green on the baseline and expected to stay green. Each gives `refs == ()`:
  - a Python wildcard with `def f():\n    return 1\n`;
  - a Go dot-import with `func Run() int {\n\treturn 1\n}` (`int` is not exported);
  - Go `import _ "…/store"`;
  - TS `export * from "./x";` with a changed `export function f() { return g(); }`.
- `test_unused_import_is_never_loaded` (`:274-283`) stays green.

**Done when:**

- [x] RED and GREEN runs are recorded;
- [x] no target of a wildcard is read;
- [x] common verification is green.

**Dependencies:** Task 1.

### Checkpoint: after Task 2

Full verification. Spot-check that no `ImportRef` is `resolved` when a file it reaches
has `has_errors`.

### Task 3: TS, JS, TSX and JSX in the AST matrix

**Commit:** `test(reviews): cover ts, js, tsx and jsx in the ast matrix (#168)`

**Files:** `tests/test_ast_resolution.py`, `tests/test_tree_sitter_facts.py`.

**Tests to add (characterization, green):**

- AC3a: `affected_symbols` for a `.ts` and a `.js` file. An added line inside a method
  selects that method; a change between declarations is module level. `_diff_file`
  (`tests/test_ast_resolution.py:102-130`) gets a `path` parameter, default `"app.py"`.
  It hardcodes the path at `:122`.
- AC3b: signatures for JS and TSX. Use the existing `_lines` helper
  (`tests/test_tree_sitter_facts.py:100-106`) on multi-line declarations, so that the signature
  differs from the declaration:
  - JS: a function, and a class with a method;
  - TSX: a component with typed props.

  The expected line pairs are literals read off the fixture.
- AC3c: `ImportResolver` resolution for TSX and JSX:
  - `src/App.tsx` imports `{ Button }` from `"./Button"`, and the target is
    `src/Button.tsx`;
  - `src/App.jsx` imports the default from `"./Button"`, and the target is
    `src/Button.jsx`.

  Both give `resolved`, with the expected path and name.
- AC3d: an `excluded` target for TS (`./models` → `src/models.ts`) and for JS
  (`./models.js`). Each gives `(EXCLUDED, "excluded")` with no symbols, as in
  `test_excluded_python_target_is_reported_and_not_shown` (`:483-491`).

**Done when:** the tests are green on the first run (otherwise stop and report), and
common verification is green.

**Dependencies:** Task 2.

### Task 4: L4, fragment and source size limits of the context

**Commit:** `test(reviews): cover the l4, fragment and source size limits of the context (#168)`

**Files:** `tests/test_review_context.py`. Insert after `:401`; the only other edit is
`:376`; no new imports.

**Tests to add (characterization, green; the `Words` counter makes one word one token):**

- AC3f, L4 total. A changed function imports five names from one target. Each signature
  plus docstring is exactly 2000 tokens and at most 200 lines. Expected:
  - four L4 blocks whose tokens sum to 8000;
  - the fifth gives `ContextOmission("ast_limit", <target>)`.

  Keep `budget` at its default of 100 000, so that the `ast_budget` branch
  (`review_context.py:502`) cuts and not `take`.
- AC3f, the fragment branch (`_symbol_block`, `:704`):
  - an imported signature plus docstring of 201 lines, and one of 2001 tokens: each gives
    `ast_limit` and no L4 block;
  - 200 lines and 2000 tokens: each is shown.
- AC3g, 256 KiB at the context level (`_Files.text`, `:229`):
  - a changed `.py` file of 262 145 bytes gives `ContextOmission("source_too_large",
    path)`, keeps its L1 diff and is not parsed;
  - the same file at 262 144 bytes is parsed;
  - an imported target of 262 145 bytes gives `(UNSUPPORTED, "source_too_large")`.
- AC3h: at `:376`, replace `!= [ImportResolution.RESOLVED]` with
  `[(r.resolution, r.reason) for r in payload.imports] == [(ImportResolution.UNSUPPORTED,
  "ast_limit")]`.

**Done when:** the tests are green on the first run (otherwise stop and report), and
common verification is green.

**Dependencies:** Task 2. Tasks 3 and 4 are independent of each other.

### Task 5: Name a refused Go package by the clauses that can be read

**Commit:** `fix(reviews): name a refused go package by a clause that can be read (#168)`

**Files:** `app/modules/reviews/application/ast_resolution.py` (`_go_package_name` only);
`tests/test_ast_resolution.py`; this plan and task list.

**What to do:** D6. When `_load` refuses the members of a Go package, walk them in member
order through the provider and take the clause of the first member whose facts carry one.
Only when no member yields a clause does the path tail stay. The shared `go_package_name`,
code_index and `RESOLVER_VERSION` stay unchanged.

**Tests to add (RED on `76fb6f73` for clause `db`, green for the clause `store` twin):** one
test parametrized over the clause (`db` in `internal/store/`, and `store` as the twin) and the
member failure. Each case expects the ref for `<clause>.Open()` with the reason the twin
gives:
- a parse error in the only member: `(UNSUPPORTED, "ast_failed")`;
- a member without a clause (`packag db`) before a valid one: `(UNSUPPORTED, "ast_failed")`;
- a decode failure before a readable member: `(UNSUPPORTED, "source_decode_failed")`. A later
  member `zz.go` is never asked;
- an excluded member before a readable one: `(EXCLUDED, "excluded")`;
- the file limit hit after a member was read: `(UNSUPPORTED, "ast_limit")`.

**Done when:**

- [x] RED and GREEN runs are recorded;
- [x] `go_package_target`, `go_package_name`, `review_context.py` and code_index are unchanged;
- [x] common verification is green.

**Dependencies:** Task 4.

### Task 6: Pin the L4 total of 8000 tokens from above

**Commit:** `test(reviews): pin the l4 total of 8000 tokens from above (#168)`

**Files:** `tests/test_review_context.py`
(`test_imported_signatures_stop_at_the_l4_total_of_8000_tokens`); the status line of the plan
and the boxes of this list.

**What to do:** the Task 4 test still passed with a total of 9000, because its fifth fragment
(2000 tokens) did not fit in 1000 either. Parametrize it:
- `exact`: four fragments of 2000, then one of 3 (`def f4():` and a one-word docstring),
  which is cut with nothing left;
- `one-short`: 2000, 2000, 2000 and 1999, then a bare `def f4():` of 2 tokens, which is cut
  with 1 token left.

A single fixture cannot fail at both 7999 and 8001: the smallest fragment is 2 tokens,
and four fragments of 2000 leave 1 token at 8001.

**Done when:**

- [x] with the total at 7999, 8001, 8002 and 9000 at least one case fails, and at 8000 both
  pass;
- [x] common verification is green.

**Dependencies:** Task 4.

### Checkpoint and publication

- Full verification (`df -h /tmp` first).
- Review in a clean subagent context.
- Rebase on `main`, then push.
- Open the PR (title and body per the plan, `Refs #168`).
- The coordinator comments in #220 about `tests/test_review_context.py` and reports the
  open answers to Q1–Q4 in the issue.
