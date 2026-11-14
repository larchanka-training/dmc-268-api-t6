"""A backend result without a model answer is stored and queued like any result (#130 F5).

DD6: a change of technical skips only ends with no model call. Its result goes through
the common ``StoreReviewOutput`` (the T8 guard, ``review.postprocess``, ``publishing`` and
the queue pointer) with an explicit marker instead of an answer: nothing is parsed, no
``llm.review_output`` is written, and the post-processor's backend summary is the only one.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, cast
from uuid import UUID

import pytest

from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.review_output import (
    InvalidReviewOutput,
    PublishReviewOutput,
    VerifiedReviewOutput,
)
from app.modules.reviews.application.run_failures import classify_failure
from app.modules.reviews.application.store_review_output import NoModelAnswer, StoreReviewOutput
from tests.review_publication_fakes import (
    PublicationQueueFake,
    PublicationRepositoryFake,
    PublicationUowFake,
)
from tests.test_clean_without_text_result import answer
from tests.test_coverage_none_classification import project, unsupported_path

_RUN = UUID(int=213)
_TOO_LARGE = DiffSnapshot("large.txt", None, "d" * 40, omission_reason="too_large")


def _store(
    repository: PublicationRepositoryFake,
    queue: PublicationQueueFake,
    committed: list[str],
) -> StoreReviewOutput:
    return StoreReviewOutput(
        lambda: PublicationUowFake(repository, committed),
        queue,
        worker_id="worker",
        run_url=lambda _: None,
    )


def _technical(kind: str) -> PublicationRepositoryFake:
    files = [_TOO_LARGE] if kind == "too_large" else [unsupported_path()]
    return PublicationRepositoryFake(files, project(files, snapshot_path=True))


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
    repository, queue = _technical(kind), PublicationQueueFake()
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
    repository, queue = _technical("unsupported_path"), PublicationQueueFake()
    assert repository.review_event == "REQUEST_CHANGES"

    assert asyncio.run(_store(repository, queue, []).execute(_RUN, NoModelAnswer())) is True

    [pointer] = queue.pointers
    assert pointer.review_event == "COMMENT"
    assert pointer.head_sha == "a" * 40


def test_a_second_backend_result_is_refused_by_the_publishing_guard() -> None:
    repository, queue = _technical("too_large"), PublicationQueueFake()
    store = _store(repository, queue, [])
    assert asyncio.run(store.execute(_RUN, NoModelAnswer())) is True
    response, body = repository.response.copy(), repository.body

    assert asyncio.run(store.execute(_RUN, NoModelAnswer())) is False

    assert repository.answers == [None]
    assert (repository.response, repository.body) == (response, body)
    assert len(queue.pointers) == 1


def test_a_model_answer_is_still_given_to_the_repository_as_it_was() -> None:
    repository = _technical("too_large")

    queue = PublicationQueueFake()
    assert asyncio.run(_store(repository, queue, []).execute(_RUN, answer())) is True

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
    repository, queue = _technical("too_large"), PublicationQueueFake()

    with pytest.raises(InvalidReviewOutput, match="invalid review output") as raised:
        asyncio.run(_store(repository, queue, []).execute(_RUN, empty))

    assert classify_failure(raised.value).error_code == "llm_invalid_output"
    assert repository.answers == []
    assert repository.response == {}
    assert queue.pointers == []


def test_the_inline_publisher_accepts_and_publishes_the_marker() -> None:
    class _FakeProvider:
        def __init__(self) -> None:
            self.published: list[tuple[str, str, Any, str]] = []

        async def publish_review(
            self,
            *,
            commit_sha: str,
            body: str,
            findings: tuple[Any, ...],
            idempotency_key: str,
        ) -> None:
            self.published.append((commit_sha, body, findings, idempotency_key))

    files = [
        DiffSnapshot(
            "src/file.py",
            "@@ -0,0 +1,2 @@\n+a = 1",
            "a" * 40,
            status="added",
            additions=1,
            changes=1,
        )
    ]
    repository = PublicationRepositoryFake(
        files, project(files, snapshot_path=True), review_event="REQUEST_CHANGES"
    )
    provider = _FakeProvider()
    publisher = PublishReviewOutput(
        lambda: PublicationUowFake(repository), cast(Any, provider)
    )

    result = asyncio.run(publisher.execute(_RUN, NoModelAnswer(incomplete_paths=("src/file.py",))))

    assert result is True
    assert repository.answers == [None]
    assert len(provider.published) == 1
    assert "src/file.py" in provider.published[0][1]
    assert provider.published[0][2] == ()


def test_the_marker_is_an_empty_mapping() -> None:
    marker = NoModelAnswer()

    assert dict(marker) == {}
    assert len(marker) == 0
    assert "summary" not in marker


def test_no_model_answer_with_incomplete_paths_publishes_partial_bounded_list() -> None:
    files = [
        DiffSnapshot(
            f"src/file_{idx}.py",
            "@@ -0,0 +1,2 @@\n+a = 1\n+b = 2",
            "a" * 40,
            status="added",
            additions=2,
            changes=2,
            review_patch="@@ -0,0 +1,2 @@\n+a = 1\n+b = 2",
        )
        for idx in range(1, 8)
    ]
    repository = PublicationRepositoryFake(
        files, project(files, snapshot_path=True), review_event="REQUEST_CHANGES"
    )
    queue = PublicationQueueFake()
    committed: list[str] = []
    omitted = tuple(item.filename for item in files)

    marker = NoModelAnswer(incomplete_paths=omitted)
    assert dict(marker) == {}
    assert len(marker) == 0

    assert asyncio.run(_store(repository, queue, committed).execute(_RUN, marker)) is True

    assert repository.answers == [None]
    assert committed == ["commit"]
    assert repository.response["verdict"] is None
    assert repository.response["coverage"]["status"] == "partial"
    assert repository.response["coverage"]["reviewed_files"] == 0
    assert repository.response["coverage"]["reason_counts"] == {"review_input_incomplete": 7}
    assert [pointer.review_event for pointer in queue.pointers] == ["COMMENT"]
    assert "модель получила не весь patch" in repository.body
    assert "`src/file_1.py`" in repository.body
    assert "- Ещё причин: 2" in repository.body
    assert "и ещё 2" in repository.body
