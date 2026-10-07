"""Application seam tests for persisting and publishing parsed review outputs."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, cast
from uuid import UUID

import pytest

from app.modules.reviews.application.findings_post_processor import ProcessedReviewOutput
from app.modules.reviews.application.review_output import (
    FindingPostProcessingInput,
    InvalidReviewOutput,
    PublishedFinding,
    PublishReviewOutput,
    ReviewOutput,
    ReviewPublication,
)

RUN_ID = UUID("00000000-0000-0000-0000-000000000401")


def output() -> dict[str, object]:
    return {
        "findings": [
            {
                "path": "app/example.py",
                "line": 12,
                "start_line": None,
                "severity": "high",
                "category": "correctness",
                "title": "Missing transaction boundary",
                "body": "The provider call can leave the transaction open.",
                "suggestion": None,
                "confidence": 0.9,
                "rule_name": None,
            }
        ],
        "summary": {
            "problem": "A transaction can span a provider call.",
            "done_well": "The repository adapter is small.",
            "effort": "small",
        },
    }


@dataclass
class FakeRepository:
    stored: list[tuple[UUID, ReviewOutput]]
    publication: ReviewPublication | None
    marked: int = 0
    processed: ProcessedReviewOutput | None = None

    async def get_post_processing_input(self, run_id: UUID) -> FindingPostProcessingInput | None:
        assert run_id == RUN_ID
        return FindingPostProcessingInput({"app/example.py": frozenset({12})}, frozenset())

    async def store_review_output(
        self,
        run_id: UUID,
        model_output: dict[str, object],
        parsed: ReviewOutput,
        processed: ProcessedReviewOutput,
    ) -> ReviewPublication | None:
        assert model_output["findings"]
        if self.publication is None:
            return None
        if not self.stored:
            self.stored.append((run_id, parsed))
        self.processed = processed
        return self.publication

    async def mark_review_published(self, run_id: UUID) -> None:
        assert run_id == RUN_ID
        self.marked += 1


@dataclass
class FakeUnitOfWork:
    reviews: FakeRepository
    commits: int = 0

    async def __aenter__(self) -> FakeUnitOfWork:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None


@dataclass
class FakeUnitOfWorkFactory:
    repository: FakeRepository
    units: list[FakeUnitOfWork]

    def __call__(self) -> FakeUnitOfWork:
        unit = FakeUnitOfWork(self.repository)
        self.units.append(unit)
        return unit


@dataclass
class FakeProvider:
    calls: list[tuple[str, str, tuple[PublishedFinding, ...], str]]
    fail_after_first_call: bool = False

    async def publish_review(
        self,
        *,
        commit_sha: str,
        body: str,
        findings: tuple[PublishedFinding, ...],
        idempotency_key: str,
    ) -> None:
        self.calls.append((commit_sha, body, findings, idempotency_key))
        if self.fail_after_first_call and len(self.calls) == 1:
            raise RuntimeError("worker crashed after provider call")


@dataclass
class ProcessingRepository:
    processed: ProcessedReviewOutput | None = None
    marked: int = 0

    async def get_post_processing_input(self, run_id: UUID) -> FindingPostProcessingInput | None:
        assert run_id == RUN_ID
        return FindingPostProcessingInput({"app/example.py": frozenset({12})}, frozenset())

    async def store_review_output(
        self,
        run_id: UUID,
        model_output: dict[str, object],
        parsed: ReviewOutput,
        processed: ProcessedReviewOutput,
    ) -> ReviewPublication | None:
        assert run_id == RUN_ID
        assert model_output == output_with_body_only()
        assert parsed.summary.effort == "small"
        self.processed = processed
        findings = tuple(
            PublishedFinding(
                path=item.finding.path,
                line=item.finding.line,
                start_line=item.finding.start_line,
                severity=item.finding.severity,
                category=item.finding.category,
                title=item.finding.title,
                body=item.finding.body,
                suggestion=item.finding.suggestion,
                confidence=item.finding.confidence,
                rule_name=item.finding.rule_name,
            )
            for item in processed.inline
        )
        return ReviewPublication("a" * 40, findings, processed.review_body, "stable-key")

    async def mark_review_published(self, run_id: UUID) -> None:
        assert run_id == RUN_ID
        self.marked += 1


def test_valid_output_is_committed_before_network_publish_with_run_head_sha() -> None:
    repository = FakeRepository(
        [], ReviewPublication("a" * 40, (), "review body", idempotency_key="stable-key")
    )
    factory = FakeUnitOfWorkFactory(repository, [])
    provider = FakeProvider([])

    assert asyncio.run(PublishReviewOutput(factory, provider).execute(RUN_ID, output())) is True

    assert len(repository.stored) == 1
    assert [unit.commits for unit in factory.units] == [0, 1, 1]
    assert provider.calls == [("a" * 40, "review body", (), "stable-key")]


def test_crash_after_provider_call_retries_with_the_same_durable_idempotency_key() -> None:
    repository = FakeRepository(
        [], ReviewPublication("a" * 40, (), "review body", idempotency_key="stable-key")
    )
    factory = FakeUnitOfWorkFactory(repository, [])
    provider = FakeProvider([], fail_after_first_call=True)
    use_case = PublishReviewOutput(factory, provider)

    with pytest.raises(RuntimeError, match="worker crashed"):
        asyncio.run(use_case.execute(RUN_ID, output()))
    assert repository.marked == 0

    assert asyncio.run(use_case.execute(RUN_ID, output())) is True
    assert len(repository.stored) == 1
    assert repository.marked == 1
    assert [call[3] for call in provider.calls] == ["stable-key", "stable-key"]


def test_invalid_output_does_not_persist_or_publish() -> None:
    repository = FakeRepository([], ReviewPublication("a" * 40, (), "review body"))
    factory = FakeUnitOfWorkFactory(repository, [])
    provider = FakeProvider([])
    invalid = output()
    invalid["unexpected"] = True

    with pytest.raises(ValueError, match="invalid review output"):
        asyncio.run(PublishReviewOutput(factory, provider).execute(RUN_ID, invalid))

    assert repository.stored == []
    assert provider.calls == []
    assert factory.units == []


def output_with_body_only() -> dict[str, object]:
    value = output()
    findings = cast(list[dict[str, object]], value["findings"])
    value["findings"] = [
        findings[0],
        {
            **findings[0],
            "title": "Low confidence risk",
            "confidence": 0.4,
            "suggestion": "validate the low confidence risk before publishing",
        },
    ]
    return value


def test_publisher_receives_only_inline_findings_and_renders_body_only_findings() -> None:
    repository = ProcessingRepository()
    factory = FakeUnitOfWorkFactory(repository, [])  # type: ignore[arg-type]
    provider = FakeProvider([])
    assert (
        asyncio.run(PublishReviewOutput(factory, provider).execute(RUN_ID, output_with_body_only()))
        is True
    )

    assert repository.processed is not None
    assert [item.finding.title for item in repository.processed.body_only] == [
        "Low confidence risk"
    ]
    assert repository.processed.body_only[0].finding.suggestion == (
        "validate the low confidence risk before publishing"
    )
    assert provider.calls[0][1] == repository.processed.review_body
    assert [item.title for item in provider.calls[0][2]] == ["Missing transaction boundary"]
    assert "validate the low confidence risk before publishing" not in provider.calls[0][1]


def test_publisher_uses_durable_context_when_callers_do_not_supply_it() -> None:
    repository = FakeRepository(
        [], ReviewPublication("a" * 40, (), "review body", idempotency_key="stable-key")
    )
    factory = FakeUnitOfWorkFactory(repository, [])
    provider = FakeProvider([])
    value = output()
    finding = cast(list[dict[str, object]], value["findings"])[0]
    finding["line"] = 999
    finding["rule_name"] = "Missing Rule"
    finding["body"] = "According to custom instructions in 'Missing Rule' (details): Unsafe use."

    assert asyncio.run(PublishReviewOutput(factory, provider).execute(RUN_ID, value)) is True

    assert repository.processed is not None
    processed = repository.processed
    assert [item.finding.title for item in processed.body_only] == ["Missing transaction boundary"]
    assert processed.body_only[0].finding.rule_name is None
    assert processed.body_only[0].finding.body == "Unsafe use."


def test_publisher_does_not_allow_callers_to_override_durable_anchor_context() -> None:
    repository = FakeRepository(
        [], ReviewPublication("a" * 40, (), "review body", idempotency_key="stable-key")
    )
    factory = FakeUnitOfWorkFactory(repository, [])
    provider = FakeProvider([])
    untrusted_context = FindingPostProcessingInput({}, frozenset({"Model supplied rule"}))
    execute = cast(Any, PublishReviewOutput(factory, provider).execute)

    with pytest.raises(TypeError):
        asyncio.run(
            execute(
                RUN_ID,
                output(),
                untrusted_context,
            )
        )


@pytest.mark.parametrize("encode", [lambda text: text, lambda text: text.encode("utf-8")])
def test_gateway_json_text_is_parsed_and_stored_as_the_exact_object(
    encode: Any,
) -> None:
    @dataclass
    class RecordingRepository(FakeRepository):
        raw: list[dict[str, object]] = field(default_factory=list)

        async def store_review_output(
            self,
            run_id: UUID,
            model_output: dict[str, object],
            parsed: ReviewOutput,
            processed: ProcessedReviewOutput,
        ) -> ReviewPublication | None:
            self.raw.append(model_output)
            return await super().store_review_output(run_id, model_output, parsed, processed)

    repository = RecordingRepository(
        [], ReviewPublication("a" * 40, (), "review body", idempotency_key="stable-key")
    )
    provider = FakeProvider([])
    # key order and spacing as the provider sent them
    text = json.dumps(output(), indent=1)

    published = asyncio.run(
        PublishReviewOutput(FakeUnitOfWorkFactory(repository, []), provider).execute(
            RUN_ID, encode(text)
        )
    )

    assert published is True
    assert repository.raw == [json.loads(text)]
    assert list(repository.raw[0]) == ["findings", "summary"]
    assert repository.stored[0][1] == ReviewOutput.model_validate(output())


def test_json_text_that_is_not_an_object_is_rejected_before_any_write() -> None:
    repository = FakeRepository([], ReviewPublication("a" * 40, (), "review body"))
    factory = FakeUnitOfWorkFactory(repository, [])

    with pytest.raises(InvalidReviewOutput):
        asyncio.run(PublishReviewOutput(factory, FakeProvider([])).execute(RUN_ID, "[1]"))

    assert factory.units == []


def test_run_without_post_processing_context_is_not_published() -> None:
    @dataclass
    class MissingContext(FakeRepository):
        async def get_post_processing_input(
            self, run_id: UUID
        ) -> FindingPostProcessingInput | None:
            return None

    repository = MissingContext([], ReviewPublication("a" * 40, (), "review body"))
    provider = FakeProvider([])

    assert (
        asyncio.run(
            PublishReviewOutput(FakeUnitOfWorkFactory(repository, []), provider).execute(
                RUN_ID, output()
            )
        )
        is False
    )
    assert provider.calls == []


def test_run_that_cannot_store_its_output_is_not_published() -> None:
    repository = FakeRepository([], None)
    factory = FakeUnitOfWorkFactory(repository, [])
    provider = FakeProvider([])

    assert asyncio.run(PublishReviewOutput(factory, provider).execute(RUN_ID, output())) is False
    assert provider.calls == []
    assert [unit.commits for unit in factory.units] == [0, 0]
