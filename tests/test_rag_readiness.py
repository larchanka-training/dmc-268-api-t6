"""The RAG readiness a review opens at the gate (#144)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.modules.code_index.domain.enums import RagIndexStatus
from app.modules.code_index.domain.rag_index import RagIndex
from app.modules.reviews.application.rag_index import (
    OpenReviewIndex,
    RagReadiness,
    ReviewIndexAccess,
)

REPOSITORY_ID = UUID("00000000-0000-0000-0000-000000000144")
SHA_A = "a" * 40
SHA_B = "b" * 40


class StubReviewIndexAccess:
    def __init__(self, readiness: RagReadiness) -> None:
        self._readiness = readiness
        self.opened: list[UUID] = []

    async def open_for_review(self, repository_id: UUID) -> RagReadiness:
        self.opened.append(repository_id)
        return self._readiness


class FailingReviewIndexAccess:
    async def open_for_review(self, repository_id: UUID) -> RagReadiness:
        raise RuntimeError("db error")


class FakeReviewIndexUow:
    def __init__(self, review_index: ReviewIndexAccess) -> None:
        self.review_index = review_index
        self.commits = 0
        self.rolled_back = False

    async def __aenter__(self) -> FakeReviewIndexUow:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if exc_type is not None:
            self.rolled_back = True

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rolled_back = True


def test_the_use_case_commits_once_and_returns_the_ports_readiness() -> None:
    readiness = RagReadiness(
        enabled=True,
        index=RagIndex(
            RagIndexStatus.STALE,
            SHA_A,
            datetime(2026, 10, 23, 9, 30, tzinfo=UTC),
            12,
            340,
            None,
        ),
        default_head_sha=SHA_B,
    )
    access = StubReviewIndexAccess(readiness)
    uow = FakeReviewIndexUow(access)

    result = asyncio.run(OpenReviewIndex(lambda: uow).execute(REPOSITORY_ID))

    assert result is readiness
    assert access.opened == [REPOSITORY_ID]
    assert uow.commits == 1
    assert uow.rolled_back is False


def test_a_port_error_propagates_and_nothing_is_committed() -> None:
    uow = FakeReviewIndexUow(FailingReviewIndexAccess())

    with pytest.raises(RuntimeError, match="db error"):
        asyncio.run(OpenReviewIndex(lambda: uow).execute(REPOSITORY_ID))

    assert uow.commits == 0
    assert uow.rolled_back is True
