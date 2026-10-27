"""A backend result without a model answer is stored and queued like any result (#130 F5).

DD6: a change of technical skips only ends with no model call. Its result goes through
the common ``StoreReviewOutput`` (the T8 guard, ``review.postprocess``, ``publishing`` and
the queue pointer) with an explicit marker instead of an answer: nothing is parsed, no
``llm.review_output`` is written, and the post-processor's backend summary is the only one.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import TracebackType
from typing import Any, cast
from uuid import UUID

import pytest

from app.modules.reviews.application.findings_post_processor import ProcessedReviewOutput
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.post_processing_diff_map import diff_map_from_snapshots
from app.modules.reviews.application.queue_messages import ReviewPublishPointer
from app.modules.reviews.application.review_output import (
    FindingPostProcessingInput,
    InvalidReviewOutput,
    PublishReviewOutput,
    ReviewOutput,
    ReviewPublication,
    VerifiedReviewOutput,
)
from app.modules.reviews.application.run_coverage import RunCoverage
from app.modules.reviews.application.run_failures import classify_failure
from app.modules.reviews.application.store_review_output import (
    NoModelAnswer,
    PublishingGuard,
    StoreReviewOutput,
)
from tests.test_clean_without_text_result import answer
from tests.test_coverage_none_classification import project, unsupported_path

_RUN = UUID(int=213)
_TOO_LARGE = DiffSnapshot("large.txt", None, "d" * 40, omission_reason="too_large")


@dataclass
class Repository:
    """The publishing boundary: every answer it was asked to store, and the last result."""

    snapshots: list[DiffSnapshot]
    coverage: RunCoverage
    review_event: str = "REQUEST_CHANGES"
    state: str = "running"
    answers: list[dict[str, object] | None] = field(default_factory=list)
    response: dict[str, Any] = field(default_factory=dict)
    body: str = ""

    async def get_post_processing_input(self, run_id: UUID) -> FindingPostProcessingInput:
        return FindingPostProcessingInput(
            diff_map_from_snapshots(
                repository_id=UUID(int=1),
                code_change_number=7,
                head_sha="a" * 40,
                base_sha="b" * 40,
                diff_old_sha="c" * 40,
                snapshots=self.snapshots,
            ),
            frozenset(),
            frozenset(),
            coverage=self.coverage,
        )

    async def lock_for_publishing(self, run_id: UUID) -> PublishingGuard:
        return PublishingGuard(self.state, "worker", False, "a" * 40, True, self.review_event)

    async def store_review_output(
        self,
        run_id: UUID,
        model_output: dict[str, object] | None,
        parsed: ReviewOutput,
        processed: ProcessedReviewOutput,
    ) -> ReviewPublication:
        self.answers.append(model_output)
        self.body = processed.review_body
        self.state = "publishing"
        return ReviewPublication("a" * 40, (), self.body)

    async def enter_publishing(
        self,
        run_id: UUID,
        *,
        lease_until: datetime,
        postprocess_request: dict[str, Any],
        postprocess_response: dict[str, Any],
        started_at: datetime,
        duration_ms: int,
        verify_request: dict[str, Any] | None = None,
        verify_response: dict[str, Any] | None = None,
    ) -> None:
        self.response = postprocess_response

    async def mark_review_published(self, run_id: UUID) -> None:
        raise AssertionError("finalization only queues publication")


@dataclass
class Uow:
    reviews: Repository
    committed: list[str]

    async def __aenter__(self) -> Uow:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        self.committed.append("commit")

    async def rollback(self) -> None:
        return None


@dataclass
class Queue:
    pointers: list[ReviewPublishPointer] = field(default_factory=list)

    async def publish_review(self, pointer: ReviewPublishPointer) -> None:
        self.pointers.append(pointer)


def _store(repository: Repository, queue: Queue, committed: list[str]) -> StoreReviewOutput:
    return StoreReviewOutput(
        lambda: Uow(repository, committed), queue, worker_id="worker", run_url=lambda _: None
    )


def _technical(kind: str) -> Repository:
    files = [_TOO_LARGE] if kind == "too_large" else [unsupported_path()]
    return Repository(files, project(files, snapshot_path=True))


@pytest.mark.parametrize(
    ("kind", "problem"),
    [
        (
            "too_large",
            "Подтверждённых замечаний нет; Текстовые изменения не проверены: "
            "Превышен лимит размера, содержимое не проверено (1).",
        ),
        (
            "unsupported_path",
            "Подтверждённых замечаний нет; Текстовые изменения не проверены: "
            "неподдерживаемый путь (1).",
        ),
    ],
    ids=["too_large", "unsupported_path"],
)
def test_a_backend_result_is_stored_with_the_backend_summary_and_no_model_answer(
    kind: str, problem: str
) -> None:
    repository, queue = _technical(kind), Queue()
    committed: list[str] = []

    assert asyncio.run(_store(repository, queue, committed).execute(_RUN, NoModelAnswer())) is True

    assert repository.response["verdict"] is None
    assert repository.response["coverage"]["status"] == "none"
    assert repository.response["inline"] == 0
    assert repository.response["body_only"] == 0
    assert repository.response["dropped"] == []
    assert repository.response["summary"] == {
        "problem": problem,
        "done_well": "Проанализировано файлов с текстовыми изменениями: 0; исключено файлов: 0.",
        "effort": "none",
    }
    assert "**Verdict:** нет доступных для анализа изменений" in repository.body
    # No llm.review_output: the repository is given no model answer to record.
    assert repository.answers == [None]
    assert committed == ["commit"]
    assert len(queue.pointers) == 1


def test_a_backend_result_is_a_comment_even_where_the_repository_requests_changes() -> None:
    repository, queue = _technical("unsupported_path"), Queue()
    assert repository.review_event == "REQUEST_CHANGES"

    assert asyncio.run(_store(repository, queue, []).execute(_RUN, NoModelAnswer())) is True

    [pointer] = queue.pointers
    assert pointer.review_event == "COMMENT"
    assert pointer.head_sha == "a" * 40


def test_a_second_backend_result_is_refused_by_the_publishing_guard() -> None:
    repository, queue = _technical("too_large"), Queue()
    store = _store(repository, queue, [])
    assert asyncio.run(store.execute(_RUN, NoModelAnswer())) is True
    response, body = repository.response.copy(), repository.body

    assert asyncio.run(store.execute(_RUN, NoModelAnswer())) is False

    assert repository.answers == [None]
    assert (repository.response, repository.body) == (response, body)
    assert len(queue.pointers) == 1


def test_a_model_answer_is_still_given_to_the_repository_as_it_was() -> None:
    repository = _technical("too_large")

    assert asyncio.run(_store(repository, Queue(), []).execute(_RUN, answer())) is True

    assert repository.answers == [answer()]
    assert repository.response["coverage"]["status"] == "none"


@pytest.mark.parametrize(
    "empty", [{}, VerifiedReviewOutput({})], ids=["mapping", "verified-output"]
)
def test_an_empty_model_answer_is_invalid_and_never_a_backend_result(
    empty: Mapping[str, object],
) -> None:
    # Only the marker type means "no model call". An empty answer of the model is an invalid
    # answer (llm_invalid_output), not published as a review with coverage none.
    repository, queue = _technical("too_large"), Queue()

    with pytest.raises(InvalidReviewOutput, match="invalid review output") as raised:
        asyncio.run(_store(repository, queue, []).execute(_RUN, empty))

    assert classify_failure(raised.value).error_code == "llm_invalid_output"
    assert repository.answers == []
    assert repository.response == {}
    assert queue.pointers == []


def test_the_inline_publisher_refuses_the_marker_as_an_invalid_answer() -> None:
    # Not composed with the context stage in production: it never gets the marker, and a
    # marker that reached it would not be published as an empty review.
    publisher = PublishReviewOutput(cast(Any, None), cast(Any, None))

    with pytest.raises(InvalidReviewOutput, match="invalid review output"):
        asyncio.run(publisher.execute(_RUN, NoModelAnswer()))


def test_the_marker_is_an_empty_mapping() -> None:
    marker = NoModelAnswer()

    assert dict(marker) == {}
    assert len(marker) == 0
    assert "summary" not in marker
