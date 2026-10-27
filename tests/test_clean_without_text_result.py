"""Finalization stores one honest coverage/verdict/summary/body result."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from types import TracebackType
from typing import Any
from uuid import UUID

import pytest

from app.modules.reviews.application.findings_post_processor import ProcessedReviewOutput
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.post_processing_diff_map import diff_map_from_snapshots
from app.modules.reviews.application.queue_messages import ReviewPublishPointer
from app.modules.reviews.application.review_output import (
    FindingPostProcessingInput,
    ReviewOutput,
    ReviewPublication,
)
from app.modules.reviews.application.run_coverage import RunCoverage
from app.modules.reviews.application.store_review_output import PublishingGuard, StoreReviewOutput
from app.modules.reviews.application.verdict import SeverityThresholdValue
from tests.test_run_coverage import code_snapshot, coverage, empty_snapshot

_RUN = UUID(int=213)


@dataclass
class Repository:
    coverage: RunCoverage | None
    state: str = "running"
    response: dict[str, Any] = field(default_factory=dict)
    body: str = ""
    diagnostic: dict[str, object] = field(default_factory=dict)
    severity_threshold: SeverityThresholdValue = "all"

    async def get_post_processing_input(self, run_id: UUID) -> FindingPostProcessingInput:
        return FindingPostProcessingInput(
            diff_map_from_snapshots(
                repository_id=UUID(int=213),
                code_change_number=7,
                head_sha="a" * 40,
                base_sha="b" * 40,
                diff_old_sha="c" * 40,
                snapshots=[code_snapshot()],
            ),
            frozenset({"src/app.py"}),
            frozenset(),
            coverage=self.coverage,
            severity_threshold=self.severity_threshold,
        )

    async def lock_for_publishing(self, run_id: UUID) -> PublishingGuard:
        return PublishingGuard(self.state, "worker", False, "a" * 40, True, "REQUEST_CHANGES")

    async def store_review_output(
        self,
        run_id: UUID,
        model_output: dict[str, object] | None,
        parsed: ReviewOutput,
        processed: ProcessedReviewOutput,
    ) -> ReviewPublication:
        self.body = processed.review_body
        if model_output is not None:
            self.diagnostic = model_output
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
    committed: list[str]
    pointers: list[ReviewPublishPointer] = field(default_factory=list)

    async def publish_review(self, pointer: ReviewPublishPointer) -> None:
        assert self.committed == ["commit"]
        self.pointers.append(pointer)


def answer() -> dict[str, object]:
    return {
        "findings": [],
        "summary": {
            "problem": "All changes were checked.",
            "done_well": "The change is clear.",
            "effort": "small",
        },
    }


def test_complete_empty_addition_result_and_body_agree_on_clean_after_commit() -> None:
    repository = Repository(coverage([code_snapshot(), empty_snapshot()]))
    committed: list[str] = []
    queue = Queue(committed)
    store = StoreReviewOutput(
        lambda: Uow(repository, committed), queue, worker_id="worker", run_url=lambda _: None
    )
    assert asyncio.run(store.execute(_RUN, answer())) is True

    assert repository.response["coverage"]["status"] == "complete"
    assert repository.response["verdict"] == "clean"
    assert "**Verdict:** clean" in repository.body
    assert queue.pointers[0].review_event == "COMMENT"


def finalize(
    result: RunCoverage | None, findings: list[dict[str, object]] | None = None
) -> tuple[Repository, Queue]:
    repository = Repository(result)
    committed: list[str] = []
    queue = Queue(committed)
    value = answer()
    value["findings"] = [] if findings is None else findings
    assert (
        asyncio.run(
            StoreReviewOutput(
                lambda: Uow(repository, committed),
                queue,
                worker_id="worker",
                run_url=lambda _: None,
            ).execute(_RUN, value)
        )
        is True
    )
    return repository, queue


@pytest.mark.parametrize(
    ("snapshot", "explanation"),
    [
        (
            DiffSnapshot("src/moved.py", None, status="renamed", previous_filename="src/old.py"),
            "перенос",
        ),
        (DiffSnapshot("src/run.sh", None, status="changed"), "прав"),
        (
            DiffSnapshot(
                "assets/gone.png", None, status="removed", omission_reason="missing_patch"
            ),
            "Удалённое содержимое",
        ),
        (
            DiffSnapshot("assets/huge.png", None, omission_reason="too_large"),
            "Превышен лимит размера, содержимое не проверено",
        ),
    ],
)
def test_partial_result_names_retained_cause_and_removes_stale_clean_summary_and_body(
    snapshot: DiffSnapshot,
    explanation: str,
) -> None:
    repository, queue = finalize(coverage([code_snapshot(), snapshot]))
    assert repository.response["verdict"] is None
    assert repository.response["coverage"]["status"] == "partial"
    assert "Проверка неполная" in repository.response["summary"]["problem"]
    assert explanation in repository.response["summary"]["problem"]
    assert explanation in repository.body
    assert snapshot.filename in repository.body
    assert "All changes were checked" not in repository.body
    assert "**Verdict:** clean" not in repository.body
    assert queue.pointers[0].review_event == "COMMENT"


@pytest.mark.parametrize(
    ("severity", "expected", "event"),
    [
        ("high", "blocking", "REQUEST_CHANGES"),
        ("medium", "attention", "COMMENT"),
    ],
)
def test_supported_findings_preserve_verdict_and_event_when_coverage_is_partial(
    severity: str,
    expected: str,
    event: str,
) -> None:
    result = coverage([code_snapshot(), DiffSnapshot("other.py", None)])
    repository, queue = finalize(
        result,
        [
            {
                "path": "src/app.py",
                "line": 1,
                "start_line": None,
                "severity": severity,
                "category": "security",
                "title": "Unchecked authorization",
                "body": "A remote caller can bypass the permission check.",
                "suggestion": None,
                "confidence": 0.9,
                "rule_name": None,
            }
        ],
    )
    assert repository.response["verdict"] == expected
    assert f"**Verdict:** {expected}" in repository.body
    assert "Проверка неполная" in repository.body
    assert queue.pointers[0].review_event == event


def test_unknown_legacy_coverage_does_not_create_clean_from_an_empty_finding_set() -> None:
    repository, _ = finalize(None)
    assert repository.response["verdict"] is None
    assert repository.response["coverage"]["status"] == "partial"
    assert "**Verdict:** clean" not in repository.body


def test_proven_technical_only_result_has_none_coverage_and_null_verdict() -> None:
    repository, _ = finalize(
        coverage([DiffSnapshot("large.txt", None, omission_reason="too_large")])
    )
    assert repository.response["coverage"]["status"] == "none"
    assert repository.response["verdict"] is None
    assert "**Verdict:** clean" not in repository.body


def test_provider_path_with_mentions_markup_and_backticks_is_inert_inline_code() -> None:
    path = "@org/team[click](evil)<img src=x>``.py"
    repository, _ = finalize(coverage([code_snapshot(), DiffSnapshot(path, None)]))
    assert "``` @org/team[click](evil)<img src=x>``.py ```" in repository.body
    assert path not in repository.response["summary"]["problem"]


def test_repeated_finalization_does_not_replace_the_stored_result_or_publish_twice() -> None:
    repository = Repository(coverage([code_snapshot(), empty_snapshot()]))
    committed: list[str] = []
    queue = Queue(committed)
    store = StoreReviewOutput(
        lambda: Uow(repository, committed), queue, worker_id="worker", run_url=lambda _: None
    )
    assert asyncio.run(store.execute(_RUN, answer())) is True
    response, body = repository.response.copy(), repository.body
    assert asyncio.run(store.execute(_RUN, answer())) is False
    assert repository.response == response
    assert repository.body == body
    assert len(queue.pointers) == 1


def test_complete_public_summary_and_body_do_not_depend_on_raw_model_summary() -> None:
    def store(value: dict[str, object]) -> Repository:
        repository = Repository(coverage([code_snapshot(), empty_snapshot()]))
        committed: list[str] = []
        assert asyncio.run(
            StoreReviewOutput(
                lambda: Uow(repository, committed),
                Queue(committed),
                worker_id="worker",
                run_url=lambda _: None,
            ).execute(_RUN, value)
        )
        return repository

    first = answer()
    second = answer() | {
        "summary": {
            "problem": "The entire project is vulnerable.",
            "done_well": "The API is clean. The parser is perfect.",
            "effort": "large",
        }
    }
    initial, changed = store(first), store(second)
    assert initial.response["summary"] == changed.response["summary"]
    assert initial.body == changed.body
    assert changed.response["summary"]["problem"] == "Подтверждённых замечаний нет."
    assert changed.response["summary"]["effort"] == "none"
    assert initial.diagnostic["summary"] == first["summary"]
    assert changed.diagnostic["summary"] == second["summary"]


@pytest.mark.parametrize(
    ("severity", "effort"),
    [
        ("critical", "large"),
        ("high", "medium"),
        ("medium", "small"),
        ("low", "small"),
        ("info", "none"),
    ],
)
def test_complete_summary_effort_and_problem_come_from_final_filtered_findings(
    severity: str, effort: str
) -> None:
    repository, _ = finalize(
        coverage([code_snapshot()]),
        [
            {
                "path": "src/app.py",
                "line": 1,
                "start_line": None,
                "severity": severity,
                "category": "security",
                "title": "Unchecked authorization",
                "body": "A remote caller can bypass the permission check.",
                "suggestion": None,
                "confidence": 0.9,
                "rule_name": None,
            }
        ],
    )
    assert repository.response["summary"]["effort"] == effort
    assert repository.response["summary"]["problem"] == "Подтверждено замечаний: 1 (security: 1)."
    assert "All changes were checked" not in repository.body


def test_threshold_filtered_finding_and_raw_claims_do_not_survive_in_public_summary() -> None:
    repository = Repository(coverage([code_snapshot()]), severity_threshold="critical")
    committed: list[str] = []
    value = answer() | {
        "findings": [
            {
                "path": "src/app.py",
                "line": 1,
                "start_line": None,
                "severity": "medium",
                "category": "security",
                "title": "Unchecked authorization",
                "body": "A remote caller can bypass the permission check.",
                "suggestion": None,
                "confidence": 0.9,
                "rule_name": None,
            }
        ]
    }
    assert asyncio.run(
        StoreReviewOutput(
            lambda: Uow(repository, committed),
            Queue(committed),
            worker_id="worker",
            run_url=lambda _: None,
        ).execute(_RUN, value)
    )
    assert repository.response["summary"]["problem"] == "Подтверждённых замечаний нет."
    assert repository.response["summary"]["effort"] == "none"
    assert repository.response["verdict"] == "clean"
    assert repository.response["dropped"] == [{"index": 0, "drop_reason": "severity_threshold"}]
    assert "Unchecked authorization" not in repository.body
