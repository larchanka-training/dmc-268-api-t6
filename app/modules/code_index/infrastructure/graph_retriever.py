"""PostgreSQL adapter of the reviews module's `GraphRetriever` port (#144, ADR 0001 §5, §6.2).

The store's reads are consistent one statement at a time; the state row and the edges of
all targets are one snapshot only inside a REPEATABLE READ transaction, so the adapter
reads them in a short read-only one of its own and closes it before it returns, ahead of
any blob read. Every read takes the run's repository id.

The review's gate found RAG on just before, and this read sees only the state row, so the
index is derived with the toggle on: a toggle-off or a purge in between shows as the
`empty` row the purge left, never as `disabled`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Self
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.code_index.application.index_store import IndexState
from app.modules.code_index.domain.rag_index import RagIndex, rag_index
from app.modules.code_index.infrastructure.index_store import SqlAlchemyCodeIndexStore
from app.modules.reviews.application.graph_retriever import GraphHint, GraphHints, GraphTarget

_SNAPSHOT = text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")


def _rag_index(state: IndexState | None) -> RagIndex:
    if state is None:
        return rag_index(
            rag_enabled=True,
            status=None,
            indexed_commit_sha=None,
            target_commit_sha=None,
            published_at=None,
            file_count=None,
            symbol_count=None,
            error_code=None,
            last_error_code=None,
        )
    return rag_index(
        rag_enabled=True,
        status=state.status,
        indexed_commit_sha=state.indexed_commit_sha,
        target_commit_sha=state.target_commit_sha,
        published_at=state.published_at,
        file_count=state.file_count,
        symbol_count=state.symbol_count,
        error_code=state.error_code,
        last_error_code=state.last_error_code,
    )


class SqlAlchemyCodeIndexSnapshotUnitOfWork(SqlAlchemyUnitOfWork):
    """A REPEATABLE READ read-only transaction: its first statement sets the level, and
    the snapshot is taken by the first read after it. Nothing to commit; the exit rolls
    back."""

    async def __aenter__(self) -> Self:
        await super().__aenter__()
        try:
            await self.session.execute(_SNAPSHOT)
        except BaseException as error:
            await self.__aexit__(type(error), error, error.__traceback__)
            raise
        return self

    @property
    def index(self) -> SqlAlchemyCodeIndexStore:
        return SqlAlchemyCodeIndexStore(self.session)


class SqlAlchemyGraphRetriever:
    """`GraphRetriever` in a short snapshot session of its own per read."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def read_hints(self, repository_id: UUID, targets: Sequence[GraphTarget]) -> GraphHints:
        hints: list[GraphHint] = []
        async with SqlAlchemyCodeIndexSnapshotUnitOfWork(self._session_factory) as uow:
            index = _rag_index(await uow.index.get_state(repository_id))
            if not index.usable:
                return GraphHints(index, ())
            for target in dict.fromkeys(targets):
                edges = await uow.index.incoming_edges(repository_id, target.path, target.name)
                hints.extend(
                    GraphHint(edge.path, edge.kind, edge.target_path, edge.target_name)
                    for edge in edges
                )
        return GraphHints(index, tuple(hints))
