# Use case (application layer, Unit of Work)

Requires: api #4 (layout, SQLAlchemy)

## When to use

A write operation that touches persistence: an application-layer use case owns the transaction, talks to infrastructure only through a `Protocol` port, and commits once at the end — never call a repository or a session from a router directly.

## File placement

- `app/modules/<module>/application/create_foo.py`
- `app/modules/<module>/domain/ports.py`
- `tests/test_create_foo.py`

## Code

<!-- proof: app/use_case.py -->

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.common.application.unit_of_work import UnitOfWork


@dataclass(frozen=True)
class CreateFooCommand:
    name: str


@dataclass(frozen=True)
class CreateFooResult:
    id: UUID
    name: str


class IdGenerator(Protocol):
    def new_id(self) -> UUID: ...


class FooRepository(Protocol):
    async def get(self, foo_id: UUID) -> CreateFooResult | None: ...
    async def add(self, foo_id: UUID, name: str) -> None: ...


class FooUnitOfWork(UnitOfWork, Protocol):
    foos: FooRepository


class CreateFoo:
    def __init__(self, uow: FooUnitOfWork, id_generator: IdGenerator) -> None:
        self._uow = uow
        self._id_generator = id_generator

    async def __call__(self, command: CreateFooCommand) -> CreateFooResult:
        foo_id = self._id_generator.new_id()
        async with self._uow as uow:  # repositories flush(); only the use case commits
            await uow.foos.add(foo_id, command.name)
            await uow.commit()
        return CreateFooResult(id=foo_id, name=command.name)
```

## Test

<!-- proof: tests/test_proof_use_case.py -->

```python
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import TracebackType
from typing import Self
from uuid import UUID

from app.__proof__.use_case.use_case import (
    CreateFoo,
    CreateFooCommand,
    CreateFooResult,
    FooRepository,
)

LITERAL_ID = UUID("11111111-1111-4111-8111-000000000002")


@dataclass
class FakeIdGenerator:
    value: UUID = LITERAL_ID

    def new_id(self) -> UUID:
        return self.value


@dataclass
class FakeFooRepository:
    added: list[tuple[UUID, str]] = field(default_factory=list)

    async def get(self, foo_id: UUID) -> CreateFooResult | None:
        return None

    async def add(self, foo_id: UUID, name: str) -> None:
        self.added.append((foo_id, name))


@dataclass
class FakeUnitOfWork:
    foos: FooRepository
    committed: bool = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.committed = False


def test_create_foo_adds_and_commits() -> None:
    repo = FakeFooRepository()
    uow = FakeUnitOfWork(foos=repo)
    use_case = CreateFoo(uow, FakeIdGenerator())

    result = asyncio.run(use_case(CreateFooCommand(name="widget")))

    assert result.id == LITERAL_ID
    assert result.name == "widget"
    assert repo.added == [(LITERAL_ID, "widget")]
    assert uow.committed is True
```

## Checklist

- Ports (`IdGenerator`, `FooRepository`, `FooUnitOfWork`) are `Protocol`s, never ABCs — infrastructure implements them by shape, not inheritance; in production they live in `domain/ports.py` and the use case imports them from there (kept inline above for the proof run only). Command and result are frozen `dataclass`es.
- The use case calls `uow.commit()` exactly once, inside the `async with` block; a repository never calls `commit()`, only `flush()`.
- The use case gets the new id from the injected `IdGenerator` port, never from `uuid4()` directly — that keeps the id deterministic under test.
- Production placement is `app/modules/<module>/application/`; the marker file names above are proof-run paths only, not the production layout.
- The test uses an in-memory fake repository, fake unit of work, and a fake `IdGenerator` returning a literal id, driven with `asyncio.run`, and asserts that literal — never a value echoed back from the use case.
