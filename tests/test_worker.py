import asyncio
from typing import cast
from uuid import UUID

from pytest import MonkeyPatch
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import app.worker as worker
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.process_run import RunDiffProvider


def test_process_review_run_composes_repository_and_processor_from_a_shared_factory(
    monkeypatch: MonkeyPatch,
) -> None:
    run_id = UUID("00000000-0000-0000-0000-000000000100")
    factory = cast(async_sessionmaker[AsyncSession], object())

    class Provider(RunDiffProvider):
        async def fetch_diff(self, *, code_change_id: UUID, head_sha: str) -> list[DiffSnapshot]:
            raise AssertionError("the composition test does not call the provider")

    class Repository:
        def __init__(self, received_factory: async_sessionmaker[AsyncSession]) -> None:
            assert received_factory is factory

    class Processor:
        def __init__(self, repository: Repository, provider: Provider) -> None:
            assert isinstance(repository, Repository)
            assert isinstance(provider, Provider)

        async def execute(self, received_run_id: UUID) -> bool:
            assert received_run_id == run_id
            return True

    monkeypatch.setattr(worker, "SqlAlchemyRunRepository", Repository)
    monkeypatch.setattr(worker, "ReviewRunProcessor", Processor)

    assert asyncio.run(worker.process_review_run(run_id, Provider(), factory)) is True


def test_review_worker_disposes_its_single_engine_at_shutdown(monkeypatch: MonkeyPatch) -> None:
    class Provider(RunDiffProvider):
        async def fetch_diff(self, *, code_change_id: UUID, head_sha: str) -> list[DiffSnapshot]:
            raise AssertionError("the lifecycle test does not call the provider")

    class Engine:
        def __init__(self) -> None:
            self.disposed = False

        async def dispose(self) -> None:
            self.disposed = True

    engine = Engine()
    received: list[tuple[str, bool]] = []

    def create_engine(database_url: str, *, pool_pre_ping: bool) -> AsyncEngine:
        received.append((database_url, pool_pre_ping))
        return cast(AsyncEngine, engine)

    monkeypatch.setattr(worker, "create_async_engine", create_engine)

    async def use_worker() -> None:
        async with worker.review_worker(Provider(), database_url="postgresql+psycopg://test"):
            assert engine.disposed is False

    asyncio.run(use_worker())

    assert received == [("postgresql+psycopg://test", True)]
    assert engine.disposed is True
