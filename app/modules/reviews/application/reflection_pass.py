"""The Reflection Pass verifier engine (Issue #120).

Performs a mandatory secondary LLM verification pass over candidate review findings
prior to publication, as specified in CONTEXT_AND_VERIFICATION_SPEC.md §7.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from typing import Annotated, Any, Literal, Protocol
from xml.sax.saxutils import escape

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.modules.reviews.application.policy_snapshot import (
    CallManifest,
)
from app.modules.reviews.application.prompt_builder import (
    PromptBuilder,
    ReviewContext,
)
from app.modules.reviews.application.review_output import (
    EvidenceRef,
    RequirementRef,
    ReviewFindingV2,
)

type VerificationStatus = Literal["supported", "contradicted", "insufficient_context"]

_STRICT = ConfigDict(extra="forbid", strict=True)


class VerificationDecision(BaseModel):
    """The verifier's judgment on one candidate review finding."""

    model_config = _STRICT

    candidate_id: Annotated[str, Field(min_length=1)]
    status: VerificationStatus
    reason: Annotated[str, Field(min_length=1, max_length=600)]
    evidence_refs: Annotated[list[EvidenceRef], Field(max_length=8)]
    requirement_refs: Annotated[list[RequirementRef], Field(max_length=8)]

    @model_validator(mode="after")
    def validate_status_evidence(self) -> VerificationDecision:
        if self.status in {"supported", "contradicted"} and len(self.evidence_refs) < 1:
            raise ValueError(f"status '{self.status}' requires at least 1 evidence_ref")
        return self


class VerificationOutput(BaseModel):
    """Closed output of the reflection verifier model."""

    model_config = _STRICT

    decisions: Annotated[list[VerificationDecision], Field(min_length=1, max_length=10)]


class VerificationInvalidOutputError(ValueError):
    """The verifier model output did not satisfy the VerificationOutput schema or constraints."""


@dataclass(frozen=True)
class CandidateFindingItem:
    """One candidate finding paired with its durable candidate ID."""

    candidate_id: str
    finding: ReviewFindingV2


@dataclass(frozen=True)
class VerifierPrompt:
    """The system message and input envelope for the verifier model."""

    system: str
    user: str


class VerifierModel(Protocol):
    """Protocol for the injected verifier model provider."""

    async def verify(
        self, *, system_prompt: str, user_prompt: str
    ) -> Mapping[str, Any] | str | bytes: ...


class VerifierPromptBuilder:
    """Constructs the prompt and CallManifest for the Reflection Pass verifier."""

    def __init__(self, system_prompt: str | None = None) -> None:
        if system_prompt is not None:
            self._system_prompt = system_prompt
        else:
            prompt_file = files("review.prompts").joinpath("review.verifier.v1.md")
            raw = prompt_file.read_text(encoding="utf-8")
            # Strip frontmatter if present
            if raw.startswith("---"):
                parts = raw.split("---", 2)
                self._system_prompt = parts[2].strip() if len(parts) >= 3 else raw
            else:
                self._system_prompt = raw

    def build_verifier_prompt(
        self,
        *,
        candidates: tuple[CandidateFindingItem, ...],
        context: ReviewContext,
        call_id: uuid.UUID | None = None,
    ) -> tuple[VerifierPrompt, CallManifest]:
        call_id = call_id or uuid.uuid4()

        builder = PromptBuilder()
        prompt_builder_manifest = builder.build_manifest(context, call_id=call_id, stage="verifier")

        user_blocks: list[str] = []

        # 1. Policy documents
        if context.policy_snapshot is not None:
            rules_part = builder._render_policy_rules(context.policy_snapshot)
            agents_part = builder._render_policy_agents(context.policy_snapshot)
            user_blocks.extend([rules_part, agents_part])

        # 2. Code context (diff)
        code_context = builder._render_changed_files(context.changed_files)
        user_blocks.append(f"<code_context>\n{code_context}\n</code_context>")

        # 3. Candidate findings
        cand_xml_list: list[str] = []
        for cand in candidates:
            f = cand.finding
            sugg_tag = (
                f"<suggestion>{escape(f.suggestion)}</suggestion>\n"
                if f.suggestion is not None
                else ""
            )
            rule_tag = (
                f'<rule_attribution name="{escape(f.rule_name)}" />\n'
                if f.rule_name is not None
                else ""
            )
            cand_xml_list.append(
                f'<candidate candidate_id="{escape(cand.candidate_id)}" path="{escape(f.path)}" '
                f'line="{f.line}" severity="{f.severity}">\n'
                f"<title>{escape(f.title)}</title>\n"
                f"<body>{escape(f.body)}</body>\n"
                f"{sugg_tag}"
                f"{rule_tag}"
                f"</candidate>"
            )

        candidates_xml = (
            f'<candidate_findings count="{len(candidates)}">\n'
            + "\n".join(cand_xml_list)
            + "\n</candidate_findings>"
        )
        user_blocks.append(candidates_xml)

        user_message = "\n".join(user_blocks)
        prompt = VerifierPrompt(system=self._system_prompt, user=user_message)
        return prompt, prompt_builder_manifest


class ReflectionPass:
    """Orchestrates candidate verification against visible code and policy requirements."""

    def __init__(
        self,
        model: VerifierModel,
        *,
        system_prompt: str | None = None,
    ) -> None:
        self._model = model
        self._prompt_builder = VerifierPromptBuilder(system_prompt=system_prompt)

    async def verify(
        self,
        *,
        candidates: tuple[CandidateFindingItem, ...],
        context: ReviewContext,
    ) -> tuple[VerificationDecision, ...]:
        if not candidates:
            return ()

        prompt, manifest = self._prompt_builder.build_verifier_prompt(
            candidates=candidates, context=context
        )

        try:
            raw_output = await self._model.verify(
                system_prompt=prompt.system, user_prompt=prompt.user
            )
            if isinstance(raw_output, (str, bytes)):
                parsed = VerificationOutput.model_validate_json(raw_output)
            elif isinstance(raw_output, dict):
                parsed = VerificationOutput.model_validate(raw_output)
            else:
                raise VerificationInvalidOutputError(
                    f"Unexpected verifier output type: {type(raw_output)}"
                )
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            raise VerificationInvalidOutputError(f"Malformed verifier response: {exc}") from exc

        # Ensure exact 1:1 match of decisions to candidate IDs
        input_ids = {c.candidate_id for c in candidates}
        decision_ids = {d.candidate_id for d in parsed.decisions}

        if len(parsed.decisions) != len(candidates) or decision_ids != input_ids:
            raise VerificationInvalidOutputError(
                f"Verifier returned decisions for {decision_ids}, expected {input_ids}"
            )

        # Validate evidence references against manifest
        for decision in parsed.decisions:
            for ref in decision.evidence_refs:
                if not manifest.is_range_shown(ref.block_id, ref.start_line, ref.end_line):
                    raise VerificationInvalidOutputError(
                        f"Decision {decision.candidate_id} evidence_ref {ref.block_id}:"
                        f"{ref.start_line}-{ref.end_line} is not shown in verifier manifest"
                    )

        return tuple(parsed.decisions)


def filter_verified_candidates(
    *,
    candidates: tuple[CandidateFindingItem, ...],
    decisions: tuple[VerificationDecision, ...],
) -> tuple[list[CandidateFindingItem], list[tuple[CandidateFindingItem, VerificationStatus, str]]]:
    """Filter candidates, returning only supported ones for publication."""
    decision_map = {d.candidate_id: d for d in decisions}
    supported: list[CandidateFindingItem] = []
    rejected: list[tuple[CandidateFindingItem, VerificationStatus, str]] = []

    for cand in candidates:
        dec = decision_map.get(cand.candidate_id)
        if dec is not None and dec.status == "supported":
            supported.append(cand)
        else:
            status = dec.status if dec is not None else "insufficient_context"
            reason = dec.reason if dec is not None else "Missing verifier decision"
            rejected.append((cand, status, reason))

    return supported, rejected
