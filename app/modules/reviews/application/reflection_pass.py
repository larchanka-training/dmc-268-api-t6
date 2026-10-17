"""The Reflection Pass verifier engine (Issue #120).

Performs a mandatory secondary LLM verification pass over candidate review findings
prior to publication, as specified in CONTEXT_AND_VERIFICATION_SPEC.md §7.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files
from typing import Annotated, Any, Literal, Protocol
from xml.sax.saxutils import escape

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.policy_snapshot import (
    CallManifest,
    ManifestBlock,
)
from app.modules.reviews.application.prompt_budget import (
    EstimatedTokenCounter,
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
from app.modules.reviews.application.run_failures import RunFailure

type VerificationStatus = Literal["supported", "contradicted", "insufficient_context"]

_STRICT = ConfigDict(extra="forbid", strict=True)


def _attribute(value: str) -> str:
    """Escape XML attributes, including double and single quotes."""
    return escape(value, {'"': "&quot;", "'": "&apos;"})


def _text(value: str) -> str:
    """Escape XML text content."""
    return escape(value)


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


class VerificationError(Exception):
    """Base error for reflection pass verifier failures."""

    error_code: str = "verification_invalid_output"


class VerificationInvalidOutputError(ValueError, VerificationError):
    """The verifier model output did not satisfy the VerificationOutput schema or constraints."""

    error_code: str = "verification_invalid_output"


class VerificationUnavailableError(RuntimeError, VerificationError):
    """The verifier model call failed due to timeout or provider unavailability."""

    error_code: str = "verification_unavailable"


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
            self._raw_prompt = system_prompt
        else:
            prompt_file = files("review.prompts").joinpath("review.verifier.v1.md")
            raw = prompt_file.read_text(encoding="utf-8")
            self._raw_prompt = raw
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

        # Check prompt checksum if snapshot binds verifier prompt
        if context.policy_snapshot is not None:
            expected_checksum = context.policy_snapshot.prompt_bindings.verifier_checksum
            if expected_checksum:
                actual_checksum = sha256(self._raw_prompt.encode("utf-8")).hexdigest()
                if actual_checksum != expected_checksum:
                    raise PolicyError(
                        PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR,
                        f"Verifier prompt checksum mismatch: expected {expected_checksum}, "
                        f"got {actual_checksum}",
                    )

        builder = PromptBuilder()
        prompt_builder_manifest = builder.build_manifest(context, call_id=call_id, stage="verifier")

        user_blocks: list[str] = []

        # 1. Policy documents and service defaults
        if context.policy_snapshot is not None:
            rules_part = builder._render_policy_rules(context.policy_snapshot)
            agents_part = builder._render_policy_agents(context.policy_snapshot)
            user_blocks.extend([rules_part, agents_part])
            if context.rules:
                user_blocks.append(builder._render_service_defaults(context.rules))

        # 2. Code context (diff) with explicit block_id
        code_context = builder._render_changed_files(context.changed_files, include_block_id=True)
        user_blocks.append(f"<code_context>\n{code_context}\n</code_context>")

        # 3. Candidate findings
        cand_xml_list: list[str] = []
        candidate_blocks: list[ManifestBlock] = []
        for cand in candidates:
            f = cand.finding
            sugg_tag = (
                f"<suggestion>{_text(f.suggestion)}</suggestion>\n"
                if f.suggestion is not None
                else ""
            )
            rule_tag = (
                f'<rule_attribution name="{_attribute(f.rule_name)}" />\n'
                if f.rule_name is not None
                else ""
            )
            cand_xml_list.append(
                f'<candidate candidate_id="{_attribute(cand.candidate_id)}" '
                f'path="{_attribute(f.path)}" '
                f'line="{f.line}" '
                f'severity="{_attribute(f.severity)}">\n'
                f"<title>{_text(f.title)}</title>\n"
                f"<body>{_text(f.body)}</body>\n"
                f"{sugg_tag}"
                f"{rule_tag}"
                f"</candidate>"
            )
            start_l = f.start_line or f.line
            candidate_blocks.append(
                ManifestBlock(
                    block_id=cand.candidate_id,
                    source_role="candidate",
                    source_kind="candidate_finding",
                    path=f.path,
                    start_line=start_l,
                    end_line=f.line,
                    shown_line_count=1,
                    shown_ranges=((start_l, f.line),),
                )
            )

        candidates_xml = (
            f'<candidate_findings count="{len(candidates)}">\n'
            + "\n".join(cand_xml_list)
            + "\n</candidate_findings>"
        )
        user_blocks.append(candidates_xml)

        user_message = "\n".join(user_blocks)
        prompt = VerifierPrompt(system=self._system_prompt, user=user_message)

        all_blocks = prompt_builder_manifest.blocks + tuple(candidate_blocks)
        verifier_manifest = CallManifest(
            call_id=call_id,
            run_id=prompt_builder_manifest.run_id,
            stage="verifier",
            blocks=all_blocks,
        )
        return prompt, verifier_manifest


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

        # Pre-send guard on verifier prompt tokens
        if context.policy_snapshot is not None:
            counter = EstimatedTokenCounter()
            total_tokens = counter.count(prompt.system) + counter.count(prompt.user)
            max_input = context.policy_snapshot.execution_profile.max_input_tokens
            if total_tokens > max_input:
                raise RunFailure(
                    "verification_context_overflow",
                    f"Verifier prompt is {total_tokens} tokens, limit is {max_input}",
                )

        try:
            raw_output = await self._model.verify(
                system_prompt=prompt.system, user_prompt=prompt.user
            )
        except TimeoutError as exc:
            raise VerificationUnavailableError("Verifier model timed out") from exc
        except Exception as exc:
            raise VerificationUnavailableError(f"Verifier model call failed: {exc}") from exc

        try:
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
        cand_by_id = {c.candidate_id: c for c in candidates}
        decision_ids = {d.candidate_id for d in parsed.decisions}

        if len(parsed.decisions) != len(candidates) or decision_ids != set(cand_by_id.keys()):
            raise VerificationInvalidOutputError(
                f"Verifier returned decisions for {decision_ids}, expected {set(cand_by_id.keys())}"
            )

        # Build allowed check IDs for service defaults
        valid_check_ids: set[str] = set()
        if context.rules:
            for r in context.rules:
                valid_check_ids.add(r.name)
                valid_check_ids.add(re.sub(r"[^a-zA-Z0-9_\-]+", "-", r.name).strip("-"))
                for idx in range(1, len(r.checks) + 1):
                    valid_check_ids.add(str(idx))

        # Validate evidence and requirement references against manifest
        for decision in parsed.decisions:
            cand = cand_by_id[decision.candidate_id]

            # 1. Validate evidence_refs
            for ref in decision.evidence_refs:
                block = manifest.get_block(ref.block_id)
                if block is None:
                    raise VerificationInvalidOutputError(
                        f"Decision {decision.candidate_id} evidence_ref {ref.block_id} "
                        "not found in manifest"
                    )
                if block.source_role not in {"diff", "source_excerpt", "ast_excerpt"}:
                    raise VerificationInvalidOutputError(
                        f"Decision {decision.candidate_id} evidence_ref {ref.block_id} "
                        f"has non-code role '{block.source_role}'"
                    )
                if not manifest.is_range_shown(ref.block_id, ref.start_line, ref.end_line):
                    raise VerificationInvalidOutputError(
                        f"Decision {decision.candidate_id} evidence_ref {ref.block_id}:"
                        f"{ref.start_line}-{ref.end_line} is not shown in verifier manifest"
                    )

            # 2. Validate requirement_refs
            for req_ref in decision.requirement_refs:
                block = manifest.get_block(req_ref.block_id)
                if block is None:
                    raise VerificationInvalidOutputError(
                        f"Decision {decision.candidate_id} requirement_ref block "
                        f"'{req_ref.block_id}' not found in manifest"
                    )
                if block.source_role not in {"policy_document", "service_defaults"}:
                    raise VerificationInvalidOutputError(
                        f"Decision {decision.candidate_id} requirement_ref block "
                        f"'{req_ref.block_id}' has invalid role '{block.source_role}'"
                    )
                if not manifest.is_range_shown(
                    req_ref.block_id, req_ref.start_line, req_ref.end_line
                ):
                    raise VerificationInvalidOutputError(
                        f"Decision {decision.candidate_id} requirement_ref range "
                        f"{req_ref.start_line}-{req_ref.end_line} not shown in '{req_ref.block_id}'"
                    )
                if req_ref.check_id is not None:
                    if block.source_role != "service_defaults":
                        raise VerificationInvalidOutputError(
                            f"Decision {decision.candidate_id} check_id is only allowed "
                            f"for service_defaults, got '{block.source_role}'"
                        )
                    rule_id = (
                        cand.finding.rule_name.removeprefix("service-defaults:")
                        if cand.finding.rule_name
                        and cand.finding.rule_name.startswith("service-defaults:")
                        else ""
                    )
                    if (
                        valid_check_ids
                        and req_ref.check_id not in valid_check_ids
                        and req_ref.check_id != rule_id
                    ):
                        raise VerificationInvalidOutputError(
                            f"Decision {decision.candidate_id} check_id '{req_ref.check_id}' "
                            "does not match service defaults rules"
                        )

            # 3. For supported/contradicted with rule_name, require a matching requirement_ref
            if cand.finding.rule_name is not None and decision.status in {
                "supported",
                "contradicted",
            }:
                rname = cand.finding.rule_name
                matched = False
                for req_ref in decision.requirement_refs:
                    block = manifest.get_block(req_ref.block_id)
                    if block is None:
                        continue
                    if (
                        rname == ".review/rules.md"
                        and block.source_kind == "repository_rules"
                        or rname == "AGENTS.md"
                        and block.source_kind == "project_conventions_document"
                        or rname.startswith("service-defaults:")
                        and block.source_role == "service_defaults"
                    ):
                        matched = True
                        break
                if not matched:
                    raise VerificationInvalidOutputError(
                        f"Decision {decision.candidate_id} for candidate with rule_name '{rname}' "
                        f"requires at least one matching requirement_ref for this rule"
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
