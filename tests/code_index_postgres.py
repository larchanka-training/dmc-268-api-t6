"""Shared PostgreSQL helpers of the index purge tests (#143).

They build on the store harness of `test_code_index_store_postgres.py`: publish an index,
count its stored rows and run the GC leader task as the worker leader runs it.
"""

from __future__ import annotations

from functools import partial
from uuid import UUID

from app.modules.code_index.application.leader_tasks import CollectIndexGarbage
from app.modules.code_index.infrastructure.maintenance import (
    SqlAlchemyCodeIndexMaintenanceUnitOfWork,
)
from tests.test_code_index_store_postgres import (
    _COUNT_ROWS,
    C1,
    _build,
    _enable,
    _importer,
    _Index,
    _module,
    _rows,
)

# A published index: two files, each with one symbol, one edge and one import record.
GRAPH_ROWS = (2, 2, 2, 2)


async def publish_index(index: _Index, *repository_ids: UUID) -> None:
    """Enable each index and publish rows in every graph table."""
    for repository_id in repository_ids:
        await _enable(index, repository_id)
        await _build(index, repository_id, C1, _module("aa01", 1), _importer("ab01", 1))


async def graph_rows(index: _Index, repository_id: UUID) -> tuple[int, ...]:
    """The stored files, symbols, edges and imports of `repository_id`, visible or not."""
    [rows] = await _rows(index, _COUNT_ROWS, repository=repository_id)
    return rows


async def collect_garbage(index: _Index) -> None:
    """One run of the GC leader task, as the worker leader runs it."""
    await CollectIndexGarbage(
        uow=partial(SqlAlchemyCodeIndexMaintenanceUnitOfWork, index.factory), batch_rows=100
    ).run(10.0)
