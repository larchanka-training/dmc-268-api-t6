"""Public contract tests for strict model review output parsing."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from types import TracebackType
from typing import cast
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.modules.reviews.application.findings_post_processor import ProcessedReviewOutput
from app.modules.reviews.application.review_output import (
    FindingPostProcessingInput,
    InvalidReviewOutput,
    PublishedFinding,
    PublishReviewOutput,
    ReviewOutput,
    ReviewPublication,
    parse_review_output,
)


def valid_output() -> dict[str, object]:
    return {
        "findings": [
            {
                "path": "app/example.py",
                "line": 12,
                "start_line": 10,
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


def test_review_output_accepts_the_exact_contract() -> None:
    output = ReviewOutput.model_validate(valid_output())

    assert output.findings[0].line == 12
    assert output.summary.effort == "small"


def test_review_output_accepts_a_body_over_the_prompt_word_target() -> None:
    """The schema enforces the character ceiling, not prompt-level brevity."""
    data = valid_output()
    data["findings"][0]["body"] = " ".join(["word"] * 121)  # type: ignore[index]

    output = ReviewOutput.model_validate(data)

    assert len(output.findings[0].body.split()) == 121


def test_review_output_rejects_a_body_over_the_character_ceiling() -> None:
    data = valid_output()
    data["findings"][0]["body"] = "x" * 1201  # type: ignore[index]

    with pytest.raises(ValidationError):
        ReviewOutput.model_validate(data)


@pytest.mark.parametrize("start_line", [12, 13], ids=["equal", "after"])
def test_parser_rejects_non_strict_multiline_anchor_start(start_line: int) -> None:
    data = valid_output()
    data["findings"][0]["start_line"] = start_line  # type: ignore[index]

    with pytest.raises(InvalidReviewOutput):
        parse_review_output(data)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(unexpected=True),
        lambda value: value["findings"][0].update(extra="no"),
        lambda value: value["findings"][0].update(line=0),
        lambda value: value["findings"][0].update(severity="urgent"),
        lambda value: value["findings"].extend([value["findings"][0]] * 10),
        lambda value: value["findings"][0].update(title="x" * 81),
        lambda value: value["findings"][0].update(title="Ends with a period."),
        lambda value: value["findings"][0].update(title="Two\nlines"),
        lambda value: value["findings"][0].update(title="Two\u2028lines"),
        lambda value: value["summary"].update(effort="tiny"),
        lambda value: value["summary"].update(problem="First issue. Second issue."),
        lambda value: value["summary"].update(done_well="One good thing. Another. A third."),
    ],
)
def test_review_output_rejects_invalid_contract(mutate: object) -> None:
    data = valid_output()
    assert callable(mutate)
    mutate(data)

    with pytest.raises(ValidationError):
        ReviewOutput.model_validate(data)


RUN_ID = UUID("00000000-0000-0000-0000-000000000408")


@dataclass
class RecordingRepository:
    context: FindingPostProcessingInput | None
    publication: ReviewPublication | None
    raw_objects: list[dict[str, object]] = field(default_factory=list)
    marked: int = 0

    async def get_post_processing_input(self, run_id: UUID) -> FindingPostProcessingInput | None:
        assert run_id == RUN_ID
        return self.context

    async def store_review_output(
        self,
        run_id: UUID,
        model_output: dict[str, object],
        parsed: ReviewOutput,
        processed: ProcessedReviewOutput,
    ) -> ReviewPublication | None:
        assert run_id == RUN_ID
        assert parsed.findings[0].title == "Missing transaction boundary"
        self.raw_objects.append(model_output)
        return self.publication

    async def mark_review_published(self, run_id: UUID) -> None:
        assert run_id == RUN_ID
        self.marked += 1


@dataclass
class RecordingUnitOfWork:
    reviews: RecordingRepository
    commits: int = 0
    active: bool = False

    async def __aenter__(self) -> RecordingUnitOfWork:
        self.active = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.active = False

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None


@dataclass
class RecordingFactory:
    repository: RecordingRepository
    units: list[RecordingUnitOfWork] = field(default_factory=list)

    def __call__(self) -> RecordingUnitOfWork:
        unit = RecordingUnitOfWork(self.repository)
        self.units.append(unit)
        return unit


@dataclass
class RecordingProvider:
    calls: list[tuple[str, str, tuple[PublishedFinding, ...], str]] = field(default_factory=list)
    units: list[RecordingUnitOfWork] | None = None

    async def publish_review(
        self,
        *,
        commit_sha: str,
        body: str,
        findings: tuple[PublishedFinding, ...],
        idempotency_key: str,
    ) -> None:
        if self.units is not None:
            assert len(self.units) == 2  # context read and output write have completed
            assert all(not unit.active for unit in self.units)
        self.calls.append((commit_sha, body, findings, idempotency_key))


def _publication() -> ReviewPublication:
    return ReviewPublication("a" * 40, (), "review body", "stable-key")


def _context() -> FindingPostProcessingInput:
    return FindingPostProcessingInput({"app/example.py": frozenset({10, 12})}, frozenset())


@pytest.mark.parametrize("encoded", ("str", "bytes"))
def test_publish_valid_json_text_preserves_the_raw_object(encoded: str) -> None:
    repository = RecordingRepository(_context(), _publication())
    factory = RecordingFactory(repository)
    provider = RecordingProvider(units=factory.units)
    value = valid_output()
    cast(list[dict[str, object]], value["findings"])[0]["confidence"] = 1
    text = json.dumps(value, indent=1)
    raw: str | bytes = text if encoded == "str" else text.encode("utf-8")

    assert asyncio.run(PublishReviewOutput(factory, provider).execute(RUN_ID, raw)) is True

    assert repository.raw_objects == [value]
    stored_finding = cast(list[dict[str, object]], repository.raw_objects[0]["findings"])[0]
    assert type(stored_finding["confidence"]) is int
    assert stored_finding["confidence"] == 1
    assert repository.marked == 1
    assert [unit.commits for unit in factory.units] == [0, 1, 1]
    assert provider.calls == [("a" * 40, "review body", (), "stable-key")]


def test_publish_mapping_preserves_the_raw_object() -> None:
    repository = RecordingRepository(_context(), _publication())
    factory = RecordingFactory(repository)
    provider = RecordingProvider(units=factory.units)
    value = valid_output()

    assert asyncio.run(PublishReviewOutput(factory, provider).execute(RUN_ID, value))

    assert repository.raw_objects == [value]
    assert provider.calls[0][0] == "a" * 40


@pytest.mark.parametrize("raw", ('{"findings":', b'{"findings":'))
def test_parse_rejects_malformed_json_text(raw: str | bytes) -> None:
    with pytest.raises(InvalidReviewOutput):
        parse_review_output(raw)


def test_publish_without_durable_context_returns_false() -> None:
    repository = RecordingRepository(None, _publication())
    factory = RecordingFactory(repository)
    provider = RecordingProvider()

    assert (
        asyncio.run(PublishReviewOutput(factory, provider).execute(RUN_ID, valid_output())) is False
    )

    assert repository.raw_objects == []
    assert provider.calls == []
    assert [unit.commits for unit in factory.units] == [0]


def test_publish_without_a_storable_result_returns_false() -> None:
    repository = RecordingRepository(_context(), None)
    factory = RecordingFactory(repository)
    provider = RecordingProvider()

    assert (
        asyncio.run(PublishReviewOutput(factory, provider).execute(RUN_ID, valid_output())) is False
    )

    assert repository.raw_objects == [valid_output()]
    assert provider.calls == []
    assert [unit.commits for unit in factory.units] == [0, 0]


def test_review_output_rejects_out_of_order_findings() -> None:
    data = valid_output()
    finding = cast(list[dict[str, object]], data["findings"])[0]
    data["findings"] = [finding, {**finding, "severity": "critical"}]

    with pytest.raises(ValidationError, match="severity then confidence ordered"):
        ReviewOutput.model_validate(data)


def test_review_output_rejects_title_with_trailing_newline() -> None:
    data = valid_output()
    cast(list[dict[str, object]], data["findings"])[0]["title"] = "Trailing\n"

    with pytest.raises(ValidationError, match="title must be one line"):
        ReviewOutput.model_validate(data)
