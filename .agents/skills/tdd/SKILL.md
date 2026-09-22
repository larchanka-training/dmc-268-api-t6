---
name: tdd
description: This skill should be used when building FastAPI/Python backend features or fixing bugs test-first with pytest, when the user mentions "red-green-refactor", or when use-case/repository tests are needed.
metadata:
  version: 1.0.0
  source: instructor-pack
  adapted-for: backend
---

# Test-Driven Development

TDD is the red → green loop. This skill makes that loop produce tests worth
keeping: what a good test is, where tests go, the anti-patterns, the rules.

Tests are pytest, one file per feature: `tests/test_<feature>.py`. No
`pytest-asyncio` plugin — drive async code with `asyncio.run`. Every test
function is annotated `-> None` (mypy `strict` applies to `tests` too). See
the stack rules file in `.agents/rules/` for full conventions.

## What a good test is

Tests verify behavior through public interfaces, not implementation details.
A good test reads like a specification — "a use case rejects a duplicate
delivery id" — and survives refactors because it doesn't care about internal
structure. See [tests.md](tests.md) for examples and [mocking.md](mocking.md)
for mocking guidelines.

## Seams — where tests go

A **seam** is the public boundary you test at: a use case's return value and
raised exceptions, a router's HTTP response via `TestClient`, a repository's
persisted rows (integration test only). Tests live at seams, never against
internals. Confirm the seams under test with the user before writing any
test: "What's the public interface, and which seams should we test?"

## Anti-patterns

- **Implementation-coupled** — mocks internal collaborators, tests private
  helpers, or reaches into a repository's session instead of its return value.
- **Tautological** — the assertion recomputes the expected value the way the
  code does; expected values must come from an independent source of truth.
- **Horizontal slicing** — all tests first, then all implementation. Work in
  **vertical slices**: one test → one implementation → repeat.

## Rules of the loop

- **Red before green.** Write the failing test first, then only enough code
  to pass it — don't anticipate future tests.
- **One slice at a time.** One seam, one test, one minimal implementation.
- **Refactoring is not part of the loop** — that's the `code-review` skill.
