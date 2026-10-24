# Implementation plan: AST resolver and parse errors (#168, part 2)

## Status and baseline

Status: implemented on this branch (Tasks 0–6). This is the English brief
for the executor of #168 part 2 (P2, the AC7 remainder of #130). Where the issue leaves a
name or a shape open, the brief says **suggested** (the executor may pick another name)
or **derived** (it follows from the cited code or spec, not from a new decision).
Issue: https://github.com/larchanka-training/dmc-268-api-t6/issues/168. Baseline:
`origin/main` `22c621ab` (2026-10-23). Worktree `projects/dmc-268-api-t6-168a`, branch
`fix/168-go-parse-errors` from `22c621ab`. Test database and vhost: `imp168a`. Task list:
[168-ast-parse-errors-todo.md](168-ast-parse-errors-todo.md).

Every `file:line` below was checked against `22c621ab` on 2026-10-23. The red/green
claims come from probes run against that baseline (the scripts stay outside the repo).
Part 1 is a separate PR and goes first (issue Kickoff). The two parts share no files.

## Context

The review resolver (`ImportResolver`, `app/modules/reviews/application/ast_resolution.py:499-691`)
follows one hop from a changed symbol. Its only caller is `BuildReviewContext`
(`app/modules/reviews/application/review_context.py:456`).

- The extractor records syntax errors. `_Extractor` collects `ERROR`/`MISSING` nodes
  (`app/modules/reviews/infrastructure/tree_sitter_facts.py:136-140`) and sets
  `AstFacts.has_errors` (`:169`). A symbol is `complete=False` only when an error overlaps
  it (`:214-218`). `has_errors` is stored (`app/modules/reviews/application/ast_symbols.py:130`)
  and survives JSON (`:195`, `:235`), so the facts cache already carries it.
- Nothing in the review path reads `has_errors`.
  - `_load` (`ast_resolution.py:638-647`) gives up only on a `FileUnavailable`.
  - `_lookup` (`:649-691`) checks only `build_constrained` (`:659-660`) and differing
    package names (`:661-662`). It then counts declarations on the members it could read
    (`:670-671`) and returns `RESOLVED` (`:691`).
  - `go_package_target` (`:333-383`) works by path only, and `go_package_name`
    (`:406-417`) ignores errors.
- Probe results on `22c621ab`:
  - A Go member with an error and no duplicate (`func Close( {`): `resolved`.
  - A member whose `func Open` sits inside the error region (`var x = (\nfunc Open()…`):
    no symbol is extracted from that member, and the import is `resolved` to the sibling.
    This is the "uniqueness inferred from a subset" case that CONTEXT §3.3 forbids.
  - A sibling member with a broken package clause (`packag store`): `ambiguous` /
    `several_package_names`.
  - The *only* member with a broken package clause: no `ImportRef` at all.
    `go_package_name` returns `None` (`package_name` is `None`), so `resolve` skips the
    import (`:521-524`).
  - A Python or TS target with an error: `resolved`. If the error overlaps the imported
    symbol, L4 then refuses it in `_symbol_block` (`review_context.py:704-705`), and the
    context records the omission `ast_limit` (`:502-503`). This is the AC2 symptom.
- Wildcard bindings are dropped before resolution. Python `from x import *`
  (`tree_sitter_facts.py:410-411`) and the Go dot-import (`:714-715`) are `WILDCARD`
  bindings without an alias. `resolve` skips every alias-less binding (`ast_resolution.py:523-524`),
  so the existing branch `WILDCARD → unsupported / no_named_symbol` (`:610-611`) is
  unreachable. `tests/test_ast_resolution.py:265-271` pins today's `refs == ()`.

Spec: `docs/CONTEXT_AND_VERIFICATION_SPEC.md` §3.3 (`:376-378`) says: "Decode/parse failure
члена пакета также запрещает `resolved`; сохраняются причина и `unsupported`. Уникальность
имени нельзя выводить по оставшемуся подмножеству". Also "dot import → `unsupported`"
(`:361`). The limits are in the §3.4 table (`:400-406`).

## Issue acceptance criteria (part 2)

AC numbers follow the part 2 checkboxes of #168. AC3 is split into rows. "Task n" points
at the todo.

| AC | Requirement (issue #168, part 2) | Covered by |
| --- | --- | --- |
| AC1 | A decode/parse failure of any member of a Go package forbids `resolved` and gives `unsupported` with a reason. Regressions: a member with an ERROR node and no duplicate; a member with a declaration of the same name inside the ERROR region | D1, D2; Task 1. The no-duplicate and hidden-declaration fixtures, the two broken package clause fixtures, and the decode pin (`source_decode_failed`). D6; Task 5: a clause that differs from the directory keeps the reason of each member failure (parse, decode, excluded, file limit) |
| AC2a | An import target (Python/TS) with a parse error gets the parse status and reason, not `resolved` with an L4 refusal by `ast_limit` | D1–D3; Task 1. Resolver tests for an error elsewhere in the target and an error over the imported symbol; one `BuildReviewContext` test: `unsupported`/`ast_failed`, no `ast_limit` omission for the target, no L4 |
| AC2b | Python `from x import *` and Go dot-import used by the changed code give `ImportRef` `unsupported` with a reason | D4; Task 2. The flipped assertion at `tests/test_ast_resolution.py:270`, a Go dot-import test, negatives for an unused wildcard and for `_` |
| AC3a | Affected symbols for TS/JS | Task 3 |
| AC3b | Signatures for JS/TSX | Task 3 |
| AC3c | Resolution for TSX/JSX | Task 3 |
| AC3d | `excluded` for TS/JS | Task 3 |
| AC3e | "Not parsed" for each language | Task 1. A test parametrized over `.py`, `.ts`, `.tsx`, `.js`, `.jsx`, `.go`: a syntax error gives `ast_failed`, an unavailable `ast_timeout` keeps `ast_timeout` |
| AC3f | L4 limit of 8000 and the L4 fragment branch | Task 4; Task 6 pins the 8000 total from both sides |
| AC3g | 256 KiB at the context level | Task 4 |
| AC3h | `test_go_mod_counts_toward_the_limit_of_imported_files` checks the reason | Task 4. `tests/test_review_context.py:376` asserts `(unsupported, "ast_limit")`; the probe confirms this value on the baseline |

## Decisions

- **D1 (derived). One check in `ImportResolver._load`** (`ast_resolution.py:638-647`).
  Loaded facts with `has_errors` count as an unusable member: `_load` returns
  `FileUnavailable(AstFailureReason.AST_FAILED.value)` exactly as it does for a failed read.
  The existing comment at `:643-644` already states the rule. This one spot covers:
  - `_lookup`: the refusal comes before the `build_constrained` and package-name checks,
    so a broken package clause yields the parse reason, not `several_package_names`.
  - `_go_package_name` (`:585-595`): it falls back to the path tail (`:593-594`), so the
    single broken-clause member is attributed and then refused instead of dropped. A
    check placed only in `_lookup` would miss this case: `_lookup` is never reached.
  - Python/TS/JS targets and the Python submodule fallback (`:623-635`). An `__init__.py`
    with errors gives `unsupported`, not `missing`, so the submodule is not guessed.

  Not changed: `go_package_target` and `go_package_name`. Both are shared with code_index
  (`app/modules/code_index/application/index_resolution.py:285`, `:320`).

  Correction (found in review): D1 does not make the binding name irrelevant for a refused
  package. The name decides which use the refusal is reported on. With the path-tail
  fallback, a package whose clause differs from its directory (`package db` in
  `internal/store/`) and has one unusable member gave no `ImportRef` at all for `db.Open()`.
  This was already the behaviour on `22c621ab` for a decode failure, an excluded member and
  `ast_limit`. D6 closes it.

  A derived consequence: `_load` stops at the first unusable member in sorted order, as it
  already does for decode and `ast_limit` failures. So a member with errors that sorts
  before an excluded member gives `unsupported`/`ast_failed`, not `excluded`. Privacy holds,
  because the excluded member is never read, but it departs from the §3.3 wording "an
  excluded member gives `excluded` for the whole resolution". The existing order is kept.
- **D2 (suggested, Q1). Reason `ast_failed`.** It is the existing
  `AstFailureReason.AST_FAILED` (`ast_symbols.py:81`). No production code emits it with
  another meaning today, and the code_index tests already use it for a member parse
  failure (`tests/test_code_index_resolution.py:549-550`). Because no code is new, no spec
  edit is needed. Decode failures keep `source_decode_failed` (already through `_load`).
- **D3 (derived, Q2). File level for every language.** Any `has_errors` in a target refuses
  resolution, even when the imported symbol itself is `complete`. This follows the AC's
  wording ("a target with a parse error") and the §3.3 subset rule. An error region can
  hide a second `def`, an `export { … as Name }` or a package clause in Python, TS and JS
  alike. The consequence: a target with any syntax error gives no L4 for any of its names.
  After D1, `not symbol.complete` in `_symbol_block` (`review_context.py:704`) cannot be
  reached through the resolver. It stays in place as defense in depth.
- **D4 (suggested, Q3). Wildcard and dot-import refs.** `resolve` stops skipping an
  alias-less `WILDCARD` binding. When the changed symbol "uses" it, the binding goes
  through the existing path:
  - the limit check gives `ast_limit`;
  - otherwise `_resolve` returns `unsupported` / `no_named_symbol` (`:610-611`) without
    loading the target, so the target never takes a slot of the 20-file limit.

  The ref has `names=()` and `resolved_symbols=()`. The suggested "uses" rule: the symbol
  references a name (`references`) that is not
  - a name it binds itself (`local_bindings`),
  - its own name,
  - the alias of another import binding of the file,
  - a top-level declaration of the importer.

  For Go only exported names count (first character upper case). Go dot-imports bind only
  exported identifiers, so this drops predeclared `error`, `int` and `len`. The rule
  over-approximates: Python builtins (`print`) and attribute names (`sep` of `os.sep`)
  count. That is acceptable, because the ref loads and shows nothing, and the defect being
  fixed is silent under-reporting. `SIDE_EFFECT` (Go `_`, TS side-effect imports) stays
  silent, since such an import cannot be used by name.

  The rule also requires `ImportForm.STATIC`. The probe shows that TS/JS `export * from`
  is a `REEXPORT` import with no bindings at all, so it never reaches this path. The form
  check keeps it that way if the extractor changes: a re-export binds nothing in the
  importer.
- **D5 (derived). `RESOLVER_VERSION` stays `resolver/v1`** (`ast_resolution.py:32`). Nothing
  caches refs by it: the Redis caches key facts by the parser profile and trees by revision
  (`app/modules/reviews/infrastructure/redis_context_cache.py:43-45`, `:70`). Bumping it would
  also change `INDEX_RESOLVER_VERSION` (`index_resolution.py:59`) and force a reindex
  without any index change.
- **D6 (Task 5). A refused Go package is named by a clause that can be read.** Only the
  review-side `ImportResolver._go_package_name` changes. When `_load` refuses the members, it
  walks them in member order through the provider and takes the clause of the first member
  whose facts carry one. A member with syntax errors counts. The shared `go_package_name`,
  code_index and `RESOLVER_VERSION` stay as they are; a resolved package is named exactly as
  before.
  - Why a member with errors may name it: the extractor takes `package_name` only from a
    `package_clause` that is a direct child of the root (`tree_sitter_facts.py:619-634`).
    When recovery wraps the clause in an ERROR node, the name is `None`. Probed shapes:
    `packag db`, `package db extra`, `package db.x`, `package`, `package func` and
    `package "db"`. One shape keeps a name: `package 1db` gives `db`, from a root-level clause
    with an ERROR child. The facts carry no clause position, so the resolver cannot tell it
    apart. The package is refused either way: the name only decides which use gets the
    `unsupported` ref, and it never leads to `resolved`.
  - Budget: members `_load` has already asked are cached by the provider and are not
    counted again. The walk adds no file when a member at or before the first refusal
    carries a clause (a parse error, a cap hit after a read). Otherwise it adds one file per
    member after the refusal, up to the first clause. A package never counts more files than
    a resolved package of the same size, so a later import hits `ast_limit` no earlier.
  - Exclusion: the provider decides `excluded` by path before any read (`_Files.text` checks
    `_denied(path)` before `read_blob`, and `_load_facts` calls `text` before the facts
    cache). The walk skips such a member without reading it.
  - Residue, kept: when no member can be read at all, the path tail stays. That happens when
    every member is undecodable, excluded or binary, or when the file limit is reached
    before the first read. It also stays for refusals decided by path, before any member is
    read: a target that is not `RESOLVED` (a platform-named member, a nested module,
    `go.work`, an incomplete tree).

## Out of scope

- Parts 1 and 3–6 of #168, including OS-level parser isolation, and the #130 integration.
- TEST_PLAN §2.7 mapping of the new tests: that is part 4.
- The sibling defect in code_index: `IndexImportResolver._go_members`
  (`index_resolution.py:288-321`) and the Python package path (`:263-268`) read facts without
  `has_errors`. See Q4.

## Coordination

- `tests/test_review_context.py` is also changed by open PR #220 (`feat/169-gitlab-intake`,
  head `b26076eb`, review required). It edits the import block (`:5-12`) and the tail
  (`test_optional_source_failure_remains_an_omission` at `:758` and a new test after it).
  Tasks 1 and 4 add tests to the middle of that file: after
  `test_changed_file_past_the_ast_limit_is_not_parsed_through_an_import` (`:381-401`),
  plus the one-line assertion at `:376`. They add no imports, because the expected values
  are literals. The coordinator posts a comment in #220 before the first edit.
- PR #220 also changes `app/modules/reviews/application/review_context.py`. This plan does
  not edit it.
- `docs/CONTEXT_AND_VERIFICATION_SPEC.md` (PRs #229, #227) and `docs/TEST_PLAN.md` (#227)
  are not touched: D2 adds no reason code.
- Other files touched: `ast_resolution.py`, `tests/test_ast_resolution.py` and
  `tests/test_tree_sitter_facts.py`. No open PR owns them.

## Questions decided by the tech lead

- **Q1. Reason code.** Decided by the tech lead on 2026-10-23: reuse `ast_failed` (D2); no new reason code and no spec edit.
- **Q2. File level or symbol level for Python/TS/JS targets.** Decided by the tech lead on 2026-10-23: file level, any parse error in the target refuses resolution (D3).
- **Q3. How precise "used by the changed code" must be for a wildcard.** Decided by the tech lead on 2026-10-23: the over-approximating "used name" rule as written in D4.
- **Q4. The code_index sibling defect.** Decided by the tech lead on 2026-10-23: `index_resolution.py` is not touched here; the `has_errors` hole there becomes a follow-up issue.

## Process

- Seven commits, one per task: Task 0 (the plan documents), then Tasks 1–6. Each subject
  ends with `(#168)`. Tasks 5 and 6 come from the review of Tasks 0–4.
- Tasks 1, 2 and 5 are test-first: record the RED run, then the GREEN run.
- Tasks 3 and 4 are characterization tests and are expected green on `22c621ab`. A red
  run there is a defect outside this plan: stop and report it, do not fix it in place.
- Code-writing tasks are delegated through a brief file (`standards/workflow.md` §5.1). Every
  brief says: never read `.env` of any clone. Review runs in a clean subagent context.
- The tests follow `.agents/rules/backend.md` §5:
  - plain pytest with `asyncio.run`;
  - unit tests against the `FactsProvider`, `RevisionSource` and `SourceParser` fakes;
  - expected values are spec literals (8000, 262144/262145, 20, 200 lines, 2000 tokens)
    and are never imported from `app`.
- PR: title `fix(reviews): refuse ast resolution through parse errors (#168)`. The body has
  What / Why / How to verify / Refs, with `Refs #168` and no closing keyword. Rebase on
  `main` before review and again before merge. Part 1's PR is opened first.

## Verification

Before every commit, and in full at each checkpoint (`df -h /tmp` first):

```
uv run ruff check .
uv run ruff format --check .
uv run mypy . --exclude '^docs/reports/'
TEST_DATABASE_URL=postgresql+psycopg://app:app@localhost:5432/imp168a \
TEST_RABBITMQ_URL=amqp://app:app@localhost:5672/imp168a uv run pytest
```

Focused run while iterating: `uv run pytest tests/test_ast_resolution.py
tests/test_review_context.py tests/test_tree_sitter_facts.py` (74 passed on `22c621ab`).
Use only the database and vhost `imp168a`. Parallel sessions pick up each other's NOTIFY
(#131).
