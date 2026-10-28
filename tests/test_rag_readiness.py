"""The RAG readiness a review opens at the gate (#144)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.modules.code_index.domain.enums import RagIndexStatus
from app.modules.code_index.domain.rag_index import RagIndex
from app.modules.reviews.application import rag_index
from app.modules.reviews.application.rag_index import (
    RETRIEVED_PROMPT_VERSIONS,
    OpenReviewIndex,
    RagReadiness,
    ReviewIndexAccess,
    has_retrieved_section,
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


def test_only_review_system_v5_has_a_retrieved_section() -> None:
    assert frozenset({("review.system", "v5")}) == RETRIEVED_PROMPT_VERSIONS
    assert has_retrieved_section("review.system", "v5") is True
    assert has_retrieved_section("review.system", "v4") is False
    assert has_retrieved_section("review.system", "v3") is False


def test_the_capability_set_is_read_when_the_gate_asks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        rag_index, "RETRIEVED_PROMPT_VERSIONS", frozenset({("review.system", "v6")})
    )

    assert has_retrieved_section("review.system", "v6") is True
    assert has_retrieved_section("review.system", "v5") is False
    # The key and the version are one pin: neither matches alone.
    assert has_retrieved_section("review.system", "v3") is False
    assert has_retrieved_section("review.verifier", "v6") is False
