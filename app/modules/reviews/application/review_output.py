"""Strict model-output contract and publication use case for a review run."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.common.application.unit_of_work import UnitOfWork
from app.modules.reviews.application.verdict import SeverityThresholdValue

if TYPE_CHECKING:
    from app.modules.reviews.application.findings_post_processor import ProcessedReviewOutput
    from app.modules.reviews.application.policy_snapshot import CallManifest

type Severity = Literal["critical", "high", "medium", "low", "info"]
type Category = Literal["security", "correctness", "performance", "readability"]
type Effort = Literal["none", "small", "medium", "large"]

_STRICT = ConfigDict(extra="forbid", strict=True)
_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


def finding_order_key(severity: Severity, confidence: float) -> tuple[int, float]:
    """The shared severity and confidence order for review findings."""
    return _SEVERITY_RANK[severity], -confidence


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
            raise ValueError("start_line must be strictly earlier than line")
        if self.title.endswith("."):
            raise ValueError("title must not end with a period")
        title_lines = self.title.splitlines()
        if len(title_lines) != 1 or title_lines[0] != self.title:
            raise ValueError("title must be one line")
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
        ordering = [finding_order_key(item.severity, item.confidence) for item in self.findings]
        if ordering != sorted(ordering):
            raise ValueError("findings must be severity then confidence ordered")
        return self


class EvidenceRef(BaseModel):
    """A direct coordinate reference to code shown in the call manifest."""

    model_config = _STRICT

    block_id: Annotated[str, Field(min_length=1, max_length=128)]
    start_line: Annotated[int, Field(ge=1)]
    end_line: Annotated[int, Field(ge=1)]

    @model_validator(mode="after")
    def validate_range(self) -> EvidenceRef:
        if self.start_line > self.end_line:
            raise ValueError("start_line must be <= end_line")
        return self


class RequirementRef(BaseModel):
    """A reference to an existing rule requirement or service default check."""

    model_config = _STRICT

    block_id: Annotated[str, Field(min_length=1, max_length=128)]
    start_line: Annotated[int, Field(ge=1)]
    end_line: Annotated[int, Field(ge=1)]
    check_id: Annotated[str | None, Field(min_length=1, max_length=128)] = None

    @model_validator(mode="after")
    def validate_range(self) -> RequirementRef:
        if self.start_line > self.end_line:
            raise ValueError("start_line must be <= end_line")
        return self


ALLOWED_V2_RULE_NAMES = frozenset({".review/rules.md", "AGENTS.md"})
_SERVICE_DEFAULTS_ID_RE = re.compile(r"\A[a-zA-Z0-9_\-]+\Z")


class ReviewFindingV2(BaseModel):
    """ReviewOutput v2 finding with required evidence references and strict rule attribution."""

    model_config = _STRICT

    path: Annotated[str, Field(min_length=1, max_length=1024)]
    line: Annotated[int, Field(ge=1)]
    start_line: Annotated[int | None, Field(ge=1)]
    severity: Severity
    category: Category
    title: Annotated[str, Field(min_length=1, max_length=80)]
    body: Annotated[str, Field(min_length=1, max_length=1200)]
    suggestion: str | None
    confidence: Annotated[float, Field(ge=0, le=1)]
    rule_name: Annotated[str | None, Field(max_length=128)]
    evidence_refs: Annotated[list[EvidenceRef], Field(min_length=1, max_length=8)]

    @model_validator(mode="after")
    def validate_range_and_rule(self) -> ReviewFindingV2:
        if self.start_line is not None and self.start_line >= self.line:
            raise ValueError("start_line must be strictly earlier than line")
        if self.title.endswith("."):
            raise ValueError("title must not end with a period")
        title_lines = self.title.splitlines()
        if len(title_lines) != 1 or title_lines[0] != self.title:
            raise ValueError("title must be one line")
        if self.rule_name is not None:
            if self.rule_name in ALLOWED_V2_RULE_NAMES:
                pass
            elif self.rule_name.startswith("service-defaults:"):
                rule_id = self.rule_name.removeprefix("service-defaults:")
                if not rule_id or not _SERVICE_DEFAULTS_ID_RE.fullmatch(rule_id):
                    raise ValueError(
                        "rule_name format for service-defaults must be 'service-defaults:<ID>' "
                        "where <ID> contains only letters, digits, underscores, or hyphens"
                    )
            else:
                raise ValueError(
                    "rule_name must be '.review/rules.md', 'AGENTS.md', "
                    "'service-defaults:<ID>', or null"
                )
        return self


class ReviewOutputV2(BaseModel):
    """The closed ReviewOutput v2 response contract with evidence refs."""

    model_config = _STRICT

    findings: Annotated[list[ReviewFindingV2], Field(max_length=10)]
    summary: ReviewSummary

    @model_validator(mode="after")
    def validate_finding_order(self) -> ReviewOutputV2:
        ordering = [finding_order_key(item.severity, item.confidence) for item in self.findings]
        if ordering != sorted(ordering):
            raise ValueError("findings must be severity then confidence ordered")
        return self


def validate_v2_attribution(
    finding: ReviewFindingV2, manifest: CallManifest
) -> tuple[bool, str | None]:
    """Validate that finding evidence and policy references exist in the call manifest."""
    for ref in finding.evidence_refs:
        block = manifest.get_block(ref.block_id)
        if block is None:
            return False, f"evidence block '{ref.block_id}' not found in manifest"
        if block.source_role not in {"diff", "source_excerpt", "ast_excerpt"}:
            return False, f"evidence block '{ref.block_id}' has invalid role '{block.source_role}'"
        if not manifest.is_range_shown(ref.block_id, ref.start_line, ref.end_line):
            return (
                False,
                f"lines {ref.start_line}-{ref.end_line} not shown in block '{ref.block_id}'",
            )

    if finding.rule_name == ".review/rules.md":
        has_rules = any(b.source_kind == "repository_rules" for b in manifest.blocks)
        if not has_rules:
            return False, "rule_name is '.review/rules.md' but no repository_rules in manifest"
    elif finding.rule_name == "AGENTS.md":
        has_agents = any(b.source_kind == "project_conventions_document" for b in manifest.blocks)
        if not has_agents:
            return (
                False,
                "rule_name is 'AGENTS.md' but no project_conventions_document in manifest",
            )
    elif finding.rule_name and finding.rule_name.startswith("service-defaults:"):
        has_defaults = any(b.source_role == "service_defaults" for b in manifest.blocks)
        if not has_defaults:
            return (
                False,
                f"rule_name is '{finding.rule_name}' but no service_defaults in manifest",
            )

    return True, None


class InvalidReviewOutput(ValueError):
    """A provider answer did not satisfy the immutable ReviewOutput contract."""


def parse_review_output(raw_output: Mapping[str, object] | str | bytes) -> ReviewOutput:
    """Parse provider JSON without letting malformed output reach persistence."""
    try:
        data: object = (
            json.loads(raw_output) if isinstance(raw_output, (str, bytes)) else dict(raw_output)
        )
        if isinstance(data, dict) and isinstance(data.get("findings"), list):
            cleaned = []
            has_v2 = False
            for item in data["findings"]:
                if isinstance(item, dict) and "evidence_refs" in item:
                    has_v2 = True
                    cleaned.append({k: v for k, v in item.items() if k != "evidence_refs"})
                else:
                    cleaned.append(item)
            if has_v2:
                data = {**data, "findings": cleaned}
        return ReviewOutput.model_validate(data)
    except (ValidationError, ValueError, json.JSONDecodeError) as exc:
        raise InvalidReviewOutput("invalid review output") from exc


def parse_review_output_v2(raw_output: Mapping[str, object] | str | bytes) -> ReviewOutputV2:
    """Parse provider JSON against the v2 ReviewOutput schema with evidence_refs."""
    try:
        if isinstance(raw_output, (str, bytes)):
            return ReviewOutputV2.model_validate_json(raw_output)
        return ReviewOutputV2.model_validate(raw_output)
    except (ValidationError, ValueError, json.JSONDecodeError) as exc:
        raise InvalidReviewOutput("invalid review output v2") from exc


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


def comment_body(title: str, body: str, suggestion: str | None) -> str:
    """Inline comment text shared by GitHub reviews and GitLab discussions."""
    text = f"**{title}**\n\n{body}"
    if suggestion:
        text += f"\n\n```suggestion\n{suggestion}\n```"
    return text


@dataclass(frozen=True)
class ReviewPublication:
    """The durable publication payload reconstructed from one immutable run."""

    head_sha: str
    findings: tuple[PublishedFinding, ...]
    review_body: str
    idempotency_key: str = ""


@dataclass(frozen=True)
class FindingPostProcessingInput:
    """Immutable run context required to safely anchor post-processed findings."""

    hunk_lines: dict[str, frozenset[int]]
    rule_names: frozenset[str]
    repository_max_inline: int | None = None
    severity_threshold: SeverityThresholdValue = "all"
    allowed_v2_rules: frozenset[str] | None = None


class ReviewOutputRepository(Protocol):
    """Transaction-owning persistence boundary for raw output and findings."""

    async def get_post_processing_input(
        self, run_id: UUID
    ) -> FindingPostProcessingInput | None: ...

    async def store_review_output(
        self,
        run_id: UUID,
        model_output: dict[str, object],
        parsed: ReviewOutput,
        processed: ProcessedReviewOutput,
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


class ReviewOutputHandler(Protocol):
    """What the review pipeline does with an accepted model answer."""

    async def execute(
        self,
        run_id: UUID,
        raw_output: Mapping[str, object] | str | bytes,
    ) -> bool: ...


class PublishReviewOutput:
    """Persist a validated model answer, then publish outside database transactions."""

    def __init__(
        self,
        uow_factory: ReviewOutputUnitOfWorkFactory,
        provider: ReviewProvider,
    ) -> None:
        self._uow_factory = uow_factory
        self._provider = provider

    async def execute(
        self,
        run_id: UUID,
        raw_output: Mapping[str, object] | str | bytes,
    ) -> bool:
        parsed = parse_review_output(raw_output)
        answer = _as_json_object(raw_output)
        from app.modules.reviews.application.findings_post_processor import FindingsPostProcessor

        # Anchoring and custom-rule attribution are an immutable run boundary:
        # accepting caller data here would let a model response choose its own
        # hunk lines or valid rule names.
        async with self._uow_factory() as uow:
            context = await uow.reviews.get_post_processing_input(run_id)
        if context is None:
            return False
        processed = FindingsPostProcessor.from_default_patterns().process(
            parsed,
            hunk_lines=context.hunk_lines,
            rule_names=context.rule_names,
            repository_max_inline=context.repository_max_inline,
            severity_threshold=context.severity_threshold,
            allowed_v2_rules=context.allowed_v2_rules,
        )
        async with self._uow_factory() as uow:
            publication = await uow.reviews.store_review_output(run_id, answer, parsed, processed)
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
    """Store the exact JSON object instead of a lossy model serialization.

    Called after ``parse_review_output`` accepted the same value, so a text is already
    known to decode to one JSON object.
    """
    if isinstance(raw_output, Mapping):
        return dict(raw_output)
    decoded: dict[str, object] = json.loads(raw_output)
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
