# When to Mock

Mock at **system boundaries** only: third-party HTTP calls (GitHub API, LLM
provider), wall-clock time/UUIDs/other non-deterministic globals, third-party
SDKs you don't control. Don't mock your own use cases, domain code,
repositories, or any other internal collaborator you control.

Use `unittest.mock.patch`/`MagicMock` for third-party boundaries only — but
prefer a **fake port** (an in-memory `Protocol` implementation) over mocking
each call, for anything shaped as a domain port.

## Designing for Mockability

At system boundaries, design interfaces that are easy to fake.

**1. Use dependency injection**

Pass external dependencies in rather than constructing them internally:

```python
# Easy to fake
class SubmitFinding:
    def __init__(self, repo: FindingRepository) -> None:
        self._repo = repo

    async def __call__(self, run_id: str, line: int, body: str) -> Finding: ...


# Hard to fake
class SubmitFinding:
    async def __call__(self, run_id: str, line: int, body: str) -> Finding:
        repo = PostgresFindingRepository(get_session())
        ...
```

**2. Define ports as `Protocol`** — a small, method-shaped interface (e.g.
`FindingRepository`, `VcsProvider`) the use case depends on; the production
adapter and the test fake both implement it independently.

## Fake ports over mocks

When code is written against a port, prefer an in-memory fake (a `Protocol`
implementation in the test module or a shared `tests/fakes.py`) over mocking
each call with `unittest.mock`:

```python
# tests/fakes.py
from app.modules.reviews.domain.ports import Finding  # illustrative path


class InMemoryFindingRepository:
    def __init__(self) -> None:
        self._rows: dict[str, Finding] = {}

    def add(self, finding: Finding) -> None:
        self._rows[finding.id] = finding

    def get(self, finding_id: str) -> Finding:
        return self._rows[finding_id]
```

A fake keeps the test asserting on behavior ("the finding I added comes
back") rather than on call shape, and it is reusable across every test that
needs the port — one fake, many tests, versus re-patching per call site.

## Where each tool fits

| Tool                          | Use for                                    |
| ------------------------------ | -------------------------------------------- |
| `unittest.mock.MagicMock`     | Standalone stub for a third-party boundary |
| `monkeypatch`                 | Env vars, module-level attributes          |
| `unittest.mock.patch`         | Replacing a third-party call (last resort) |
| Fake port (`Protocol` class)  | Anything shaped as a domain port/repository |
