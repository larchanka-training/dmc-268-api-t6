"""Rerun adapters reject missing authorization before any database operation."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.reviews.infrastructure.rerun_store import (
    SqlAlchemyRerunStore,
    SqlAlchemyRerunUnitOfWork,
)


def test_rerun_store_rejects_missing_scope_at_construction() -> None:
    with pytest.raises(ValueError, match="AuthScope"):
        SqlAlchemyRerunStore(AsyncSession(), None)


def test_rerun_unit_of_work_rejects_missing_scope_at_construction() -> None:
    with pytest.raises(ValueError, match="AuthScope"):
        SqlAlchemyRerunUnitOfWork(async_sessionmaker())


def test_rerun_adapters_accept_scope_or_explicit_internal_bypass() -> None:
    from app.modules.auth.application.scope import AuthScope

    SqlAlchemyRerunStore(AsyncSession(), AuthScope(42, ()))
    SqlAlchemyRerunUnitOfWork(async_sessionmaker(), AuthScope(42, ()))
    SqlAlchemyRerunStore(AsyncSession(), None, allow_unscoped=True)
    SqlAlchemyRerunUnitOfWork(async_sessionmaker(), allow_unscoped=True)
