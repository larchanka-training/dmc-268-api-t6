# SQLAlchemy 2 repository

Requires: api #4 (layout, SQLAlchemy)

## When to use

Persistence for a module's own aggregate: a typed model plus a repository that only `flush()`s — its unit of work owns the transaction.

## File placement

- `app/modules/<module>/infrastructure/{models,repository}.py`
- `tests/test_foo_repository.py`

## Code

<!-- proof: app/model.py -->

```python
from __future__ import annotations

from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import Enum, Index, String, UniqueConstraint, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class ProofBase(DeclarativeBase):
    """Local metadata for this proof run only — never joined to app.bootstrap.db_metadata."""


class FooStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"


class FooModel(ProofBase):
    __tablename__ = "proof_foos"
    __table_args__ = (
        UniqueConstraint("name", name="uq_proof_foos_name"),
        Index("ix_proof_foos_status", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[FooStatus] = mapped_column(
        Enum(FooStatus, name="foo_status", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=FooStatus.DRAFT,
    )
```

<!-- proof: app/repository.py -->

```python
from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from .model import FooModel, FooStatus


class FooRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, name: str) -> UUID:
        foo = FooModel(id=uuid4(), name=name, status=FooStatus.DRAFT)
        self._session.add(foo)
        await self._session.flush()
        return foo.id

    async def get(self, foo_id: UUID) -> FooModel | None:
        return await self._session.get(FooModel, foo_id)
```

## Test

<!-- proof: tests/test_proof_repository.py -->

```python
from __future__ import annotations

import asyncio
import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from app.__proof__.repository.model import FooModel, FooStatus, ProofBase
from app.__proof__.repository.repository import FooRepository


def test_foo_model_columns_and_status_values() -> None:
    columns = {column.name for column in inspect(FooModel).columns}
    assert columns == {"id", "name", "status"}
    assert [status.value for status in FooStatus] == ["draft", "published"]


@pytest.mark.skipif(os.getenv("TEST_DATABASE_URL") is None, reason="needs TEST_DATABASE_URL")
def test_foo_repository_add_and_get_round_trip() -> None:
    database_url = os.environ["TEST_DATABASE_URL"]
    schema = f"test_proof_repository_{uuid4().hex}"

    async def add_and_get() -> tuple[UUID, FooModel | None]:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}
        )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                foo_id = await FooRepository(session).add("widget")
                await session.commit()
                fetched = await FooRepository(session).get(foo_id)
            return foo_id, fetched
        finally:
            await engine.dispose()

    sync_engine = create_engine(database_url)
    try:
        with sync_engine.begin() as connection:
            connection.execute(CreateSchema(schema))
            connection.execute(text(f'SET search_path TO "{schema}"'))
            ProofBase.metadata.create_all(connection)
        foo_id, fetched = asyncio.run(add_and_get())
        assert fetched is not None
        assert fetched.id == foo_id
        assert fetched.name == "widget"
    finally:
        with sync_engine.begin() as connection:
            connection.execute(text(f'SET search_path TO "{schema}"'))
            ProofBase.metadata.drop_all(connection)
            connection.execute(DropSchema(schema, cascade=True))
        sync_engine.dispose()
```

## Checklist

- `ProofBase` is for the proof run only; production models extend `Base` from
  `app.common.infrastructure.db` (pending api #4).
- `StrEnum` column via `sqlalchemy.Enum(..., values_callable=...)` above; production uses `pg_enum()` from `app.common.infrastructure.db.columns` (pending api #4) instead — the inline `Enum(...)` is proof-run only. Constraint names follow `ix_/uq_<table>_<column>`; the repository only `flush()`s.
- One pure unit test (no database) plus one integration test gated on `TEST_DATABASE_URL`, isolated in a throwaway schema it drops afterwards; production also adds `@pytest.mark.integration` (registered in api #4) — omitted here because this proof runs on `main`.
