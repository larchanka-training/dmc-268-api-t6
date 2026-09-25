"""Strict model-output contract and publication use case for a review run."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.common.application.unit_of_work import UnitOfWork

type Severity = Literal["critical", "high", "medium", "low", "info"]
type Category = Literal["security", "correctness", "performance", "readability"]
type Effort = Literal["none", "small", "medium", "large"]

_STRICT = ConfigDict(extra="forbid", strict=True)
_ATTRIBUTION_PREFIX = "According to custom instructions in '"


class ReviewFinding(BaseModel):
    """One schema-checked finding produced by the review model."""

    model_config = _STRICT

    path: Annotated[str, Field(min_length=1)]
    line: Annotated[int, Field(ge=1)]
    start_line: Annotated[int | None, Field(ge=1)]
    severity: Severity
    category: Category
    title: Annotated[str, Field(min_length=1, max_length=80)]
    body: Annotated[str, Field(min_length=1, max_length=1200)]
    suggestion: str | None
    confidence: Annotated[float, Field(ge=0, le=1)]
    rule_name: str | None

    @model_validator(mode="after")
    def validate_range(self) -> ReviewFinding:
        """A multi-line anchor always has a strictly earlier start line."""
        if self.start_line is not None and self.start_line >= self.line:
            raise ValueError("start_line must be smaller than line")
        if self.title.endswith("."):
            raise ValueError("title must not end with a period")
        title_lines = self.title.splitlines()
        if len(title_lines) != 1 or title_lines[0] != self.title:
            raise ValueError("title must be one line")
        if self.severity != "critical" and len(self.body.split()) > 120:
            raise ValueError("body must have at most 120 words unless severity is critical")
        if self.rule_name is None and self.body.startswith(_ATTRIBUTION_PREFIX):
            raise ValueError("attributed body requires rule_name")
        if self.rule_name is not None:
            expected_prefix = f"{_ATTRIBUTION_PREFIX}{self.rule_name}' ("
            if not self.body.startswith(expected_prefix):
                raise ValueError("rule_name requires a matching attribution prefix")
        return self


class ReviewSummary(BaseModel):
    """The required summary that becomes the review body."""

    model_config = _STRICT

    problem: Annotated[str, Field(min_length=1)]
    done_well: Annotated[str, Field(min_length=1)]
    effort: Effort

    @model_validator(mode="after")
    def validate_sentence_limit(self) -> ReviewSummary:
        """Enforce the exact one- and two-sentence summary contract."""
        problem_sentences = _sentence_count(self.problem)
        done_well_sentences = _sentence_count(self.done_well)
        if problem_sentences != 1:
            raise ValueError("problem must contain exactly one sentence")
        if not 1 <= done_well_sentences <= 2:
            raise ValueError("done_well must contain one or two sentences")
        return self


class ReviewOutput(BaseModel):
    """The complete closed ReviewOutput response contract."""

    model_config = _STRICT

    findings: Annotated[list[ReviewFinding], Field(max_length=10)]
    summary: ReviewSummary

    @model_validator(mode="after")
    def validate_finding_order(self) -> ReviewOutput:
        """Require the documented severity/confidence order from the model."""
        rank = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
        ordering = [(rank[item.severity], -item.confidence) for item in self.findings]
        if ordering != sorted(ordering):
            raise ValueError("findings must be severity then confidence ordered")
        return self


class InvalidReviewOutput(ValueError):
    """A provider answer did not satisfy the immutable ReviewOutput contract."""


def parse_review_output(raw_output: Mapping[str, object] | str | bytes) -> ReviewOutput:
    """Parse provider JSON without letting malformed output reach persistence."""
    try:
        if isinstance(raw_output, (str, bytes)):
            return ReviewOutput.model_validate_json(raw_output)
        return ReviewOutput.model_validate(raw_output)
    except (ValidationError, ValueError, json.JSONDecodeError) as exc:
        raise InvalidReviewOutput("invalid review output") from exc


@dataclass(frozen=True)
class PublishedFinding:
    """A persisted finding ready for the provider's inline-comment adapter."""

    path: str
    line: int
    start_line: int | None
    severity: Severity
    category: Category
    title: str
    body: str
    suggestion: str | None
    confidence: float
    rule_name: str | None


@dataclass(frozen=True)
class ReviewPublication:
    """The durable publication payload reconstructed from one immutable run."""

    head_sha: str
    findings: tuple[PublishedFinding, ...]
    review_body: str
    idempotency_key: str = ""


class ReviewOutputRepository(Protocol):
    """Transaction-owning persistence boundary for raw output and findings."""

    async def store_review_output(
        self, run_id: UUID, raw_output: dict[str, object], parsed: ReviewOutput
    ) -> ReviewPublication | None: ...

    async def mark_review_published(self, run_id: UUID) -> None: ...


class ReviewOutputUnitOfWork(UnitOfWork, Protocol):
    """The use case owns both durable transaction boundaries."""

    @property
    def reviews(self) -> ReviewOutputRepository: ...


class ReviewOutputUnitOfWorkFactory(Protocol):
    """Create a fresh UoW after a provider crash or successful publication."""

    def __call__(self) -> ReviewOutputUnitOfWork: ...


class ReviewProvider(Protocol):
    """Network boundary. Implementations must use the supplied immutable SHA."""

    async def publish_review(
        self,
        *,
        commit_sha: str,
        body: str,
        findings: tuple[PublishedFinding, ...],
        idempotency_key: str,
    ) -> None: ...


class PublishReviewOutput:
    """Persist a validated model answer, then publish outside database transactions."""

    def __init__(
        self, uow_factory: ReviewOutputUnitOfWorkFactory, provider: ReviewProvider
    ) -> None:
        self._uow_factory = uow_factory
        self._provider = provider

    async def execute(self, run_id: UUID, raw_output: Mapping[str, object] | str | bytes) -> bool:
        parsed = parse_review_output(raw_output)
        raw_json = _as_json_object(raw_output)
        async with self._uow_factory() as uow:
            publication = await uow.reviews.store_review_output(run_id, raw_json, parsed)
            if publication is None:
                return False
            await uow.commit()

        await self._provider.publish_review(
            commit_sha=publication.head_sha,
            body=publication.review_body,
            findings=publication.findings,
            idempotency_key=publication.idempotency_key,
        )
        async with self._uow_factory() as uow:
            await uow.reviews.mark_review_published(run_id)
            await uow.commit()
        return True


def _as_json_object(raw_output: Mapping[str, object] | str | bytes) -> dict[str, object]:
    """Store the exact JSON object instead of a lossy model serialization."""
    if isinstance(raw_output, Mapping):
        return dict(raw_output)
    decoded: Any = json.loads(raw_output)
    if not isinstance(decoded, dict):
        raise InvalidReviewOutput("invalid review output")
    return decoded


def render_review_body(summary: ReviewSummary) -> str:
    """Render the baseline body; the post-processor later appends body-only findings."""
    return "\n".join(
        (
            "## Review summary",
            "",
            f"**Main issue:** {summary.problem}",
            "",
            f"**Done well:** {summary.done_well}",
            "",
            f"**Effort:** {summary.effort}",
        )
    )


def _sentence_count(text: str) -> int:
    """Count terminal prose sentences while rejecting sentence fragments."""
    return len(re.findall(r"[^.!?]+[.!?](?:\s|$)", text))
