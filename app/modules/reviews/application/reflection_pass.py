"""The Reflection Pass verifier engine (CONTEXT_AND_VERIFICATION_SPEC §7).

Performs a mandatory secondary LLM verification pass over candidate review findings
prior to publication, as specified in CONTEXT_AND_VERIFICATION_SPEC.md §7.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import timedelta
from functools import cached_property
from hashlib import sha256
from importlib.resources import files
from typing import TYPE_CHECKING, Annotated, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.modules.reviews.application.context_provenance import SourceRole
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.policy_snapshot import (
    CallManifest,
    ManifestBlock,
)
from app.modules.reviews.application.prompt_budget import (
    TokenCounter,
    _priority,
    send_budget_for,
    token_counter_for,
    validate_execution_profile_versions,
    verify_provenance_pre_send_guard,
)
from app.modules.reviews.application.prompt_builder import (
    ChangedFile,
    PromptBuilder,
    ReviewContext,
    ReviewRule,
    file_block_id,
    get_rule_check_ids,
    render_context_excerpts,
    render_retrieved_excerpts,
    xml_attribute,
    xml_text,
)
from app.modules.reviews.application.review_output import (
    EvidenceRef,
    RequirementRef,
    ReviewFindingV2,
)
from app.modules.reviews.application.run_failures import RunCancelled, RunFailure

if TYPE_CHECKING:
    from app.modules.reviews.application.review_context import ContextBlock, RetrievedBlock

type VerificationStatus = Literal["supported", "contradicted", "insufficient_context"]

_STRICT = ConfigDict(extra="forbid", strict=True)
# The longest provider Retry-After a verifier failure carries: one day, as the other
# Retry-After caps (code_index, rules-status).
_MAX_RETRY_AFTER_S = 86_400.0


class VerificationDecision(BaseModel):
    """The verifier's judgment on one candidate review finding."""

    model_config = _STRICT

    candidate_id: Annotated[str, Field(min_length=1, max_length=64)]
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


@dataclass(frozen=True)
class VerifierReply:
    """A verifier answer plus the manifest ``call_id`` of the call that produced it."""

    output: Mapping[str, Any] | str | bytes
    accepted_call_id: uuid.UUID | None = None
    # That call's stored manifest (``context_manifests``); None without a manifest store.
    manifest_id: uuid.UUID | None = None


@dataclass(frozen=True)
class VerificationRecord:
    """The verifier's decisions and the ``llm.verify`` request fields (PIPELINE_SPEC:177)."""

    decisions: tuple[VerificationDecision, ...]
    # The candidate ids sent to the verifier, in prompt order.
    candidate_ids: tuple[str, ...]
    accepted_call_id: uuid.UUID | None = None
    manifest_id: uuid.UUID | None = None


class VerifierModel(Protocol):
    """Protocol for the injected verifier model provider.

    ``manifest`` describes the blocks of ``user_prompt``; ``context`` is the review context
    the candidates came from, for the pre-send guard of repair and fallback calls.
    """

    async def verify(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        manifest: CallManifest,
        context: ReviewContext,
    ) -> VerifierReply | Mapping[str, Any] | str | bytes: ...


@runtime_checkable
class VerifierWindow(Protocol):
    """A verifier model that knows its route's prompt window (R10).

    The window caps every verification send budget; a model without it (eval, fakes) is
    fitted to the execution profile alone.
    """

    def max_prompt_tokens(self) -> int: ...


def _verifier_code_context(
    files: tuple[ChangedFile, ...],
    excerpts: tuple[ContextBlock, ...] = (),
    retrieved: tuple[RetrievedBlock, ...] = (),
) -> str:
    """The verifier's code: the changed files, then the excerpts and the RAG blocks the
    candidates cite."""
    parts = [PromptBuilder.render_changed_files(files, include_block_id=True)]
    if excerpts:
        parts.append(render_context_excerpts(excerpts))
    if retrieved:
        parts.append(render_retrieved_excerpts(retrieved))
    return "<code_context>\n" + "\n".join(parts) + "\n</code_context>"


def _protected_lines(
    files: tuple[ChangedFile, ...], candidates: tuple[CandidateFindingItem, ...]
) -> dict[str, set[int]]:
    """The changed-file lines each candidate stands on or cites, by path."""
    protected_lines: dict[str, set[int]] = {}
    for cand in candidates:
        s = protected_lines.setdefault(cand.finding.path, set())
        s.add(cand.finding.line)
        if cand.finding.start_line is not None:
            for line_num in range(cand.finding.start_line, cand.finding.line + 1):
                s.add(line_num)
        for ref in cand.finding.evidence_refs:
            for f in files:
                if file_block_id(f.path) == ref.block_id:
                    ref_set = protected_lines.setdefault(f.path, set())
                    for line_num in range(ref.start_line, ref.end_line + 1):
                        ref_set.add(line_num)
    return protected_lines


def _cited_excerpts(
    blocks: tuple[ContextBlock, ...], candidates: tuple[CandidateFindingItem, ...]
) -> tuple[ContextBlock, ...]:
    """The L2-L4 blocks a candidate cites as evidence, in payload order (CVS §7.1)."""
    cited = {ref.block_id for cand in candidates for ref in cand.finding.evidence_refs}
    return tuple(block for block in blocks if block.block_id in cited)


def _cited_retrieved(
    blocks: tuple[RetrievedBlock, ...], candidates: tuple[CandidateFindingItem, ...]
) -> tuple[RetrievedBlock, ...]:
    """The RAG blocks a candidate cites, each place with the definitions it read (CVS §3.5),
    in payload order: the verifier is shown again what the review was (ADR 0001 §9)."""
    cited = {ref.block_id for cand in candidates for ref in cand.finding.evidence_refs}
    read = {source for block in blocks if block.block_id in cited for source in block.definitions}
    return tuple(block for block in blocks if block.block_id in cited or block.source in read)


# Whether a choice of diff files, cited excerpts and cited RAG blocks fits the verifier prompt.
type _CodeFits = Callable[
    [tuple[ChangedFile, ...], tuple[ContextBlock, ...], tuple[RetrievedBlock, ...]], bool
]


def _with_lines(file: ChangedFile, indices: tuple[int, ...]) -> ChangedFile:
    """``file`` showing only the lines at ``indices``; the file itself when it shows them all."""
    if len(indices) == len(file.lines):
        return file
    return ChangedFile(
        file.path,
        file.status,
        tuple(file.lines[i] for i in indices),
        total_lines=file.total_lines or len(file.lines),
    )


def _fit_verifier_code(
    files: tuple[ChangedFile, ...],
    excerpts: tuple[ContextBlock, ...],
    rules: tuple[ReviewRule, ...],
    candidates: tuple[CandidateFindingItem, ...],
    fits: _CodeFits,
    *,
    retrieved: tuple[RetrievedBlock, ...] = (),
) -> tuple[tuple[ChangedFile, ...], tuple[ContextBlock, ...], tuple[RetrievedBlock, ...]]:
    """The diff lines, the cited excerpts and the cited RAG blocks the verifier is shown.

    Fit order, decided by the tech lead on 2026-10-25 (CVS §7.1): the own diff lines of
    every candidate, across all files; then the cited excerpts, each whole or not at all,
    in payload order; then the other diff lines, file by file. A candidate whose excerpt
    is left out gets ``insufficient_context`` from the verifier. Among the excerpts, as the
    review fit keeps them (CVS §4.1): L4 first, as the payload has it, then each RAG place
    with the definitions it read (CVS §3.5), then L2 and L3.
    """
    if fits(files, excerpts, retrieved):
        return files, excerpts, retrieved
    protected = _protected_lines(files, candidates)
    order = sorted(
        range(len(files)),
        key=lambda idx: (
            bool(protected.get(files[idx].path)),
            _priority(files[idx], rules),
            files[idx].path,
        ),
        reverse=True,
    )
    # The shown line indices of each kept file, and the shown excerpts and RAG blocks.
    kept: dict[int, tuple[int, ...]] = {}
    shown: tuple[ContextBlock, ...] = ()
    shown_retrieved: tuple[RetrievedBlock, ...] = ()

    def chosen(index: int | None = None, indices: tuple[int, ...] = ()) -> tuple[ChangedFile, ...]:
        picks = kept if index is None else {**kept, index: indices}
        return tuple(_with_lines(files[i], picks[i]) for i in sorted(picks))

    def widest(index: int, base: tuple[int, ...], extra: tuple[int, ...]) -> tuple[int, ...]:
        """``base`` and the longest prefix of ``extra`` that fits, in file order."""
        best = 0
        if extra and fits(chosen(index, tuple(sorted((*base, *extra)))), shown, shown_retrieved):
            best = len(extra)
        else:
            low, high = 1, len(extra) - 1
            while low <= high:
                mid = (low + high) // 2
                if fits(
                    chosen(index, tuple(sorted((*base, *extra[:mid])))), shown, shown_retrieved
                ):
                    best, low = mid, mid + 1
                else:
                    high = mid - 1
        return tuple(sorted((*base, *extra[:best])))

    # 1. The candidates' own lines, in every file, before anything else.
    for index in order:
        own_numbers = protected.get(files[index].path, set())
        own = tuple(i for i, line in enumerate(files[index].lines) if line.number in own_numbers)
        if picked := widest(index, (), own):
            kept[index] = picked

    # 2. The cited excerpts next to them: L4, then each RAG place with the definitions it
    #    read (one shown already costs nothing), then L2 and L3. One that does not fit
    #    leaves room for the next; what is shown keeps the payload order.
    read = {source for block in retrieved for source in block.definitions}

    def group(place: RetrievedBlock) -> tuple[RetrievedBlock, ...]:
        return tuple(
            block for block in retrieved if block is place or block.source in place.definitions
        )

    takes: list[tuple[tuple[ContextBlock, ...], tuple[RetrievedBlock, ...]]] = [
        *(((block,), ()) for block in excerpts if block.level.value == "L4"),
        *(((), group(place)) for place in retrieved if place.source not in read),
        *(((block,), ()) for block in excerpts if block.level.value != "L4"),
    ]
    for more, more_retrieved in takes:
        picks = tuple(block for block in excerpts if block in shown or block in more)
        picks_retrieved = tuple(
            block for block in retrieved if block in shown_retrieved or block in more_retrieved
        )
        if fits(chosen(), picks, picks_retrieved):
            shown, shown_retrieved = picks, picks_retrieved

    # 3. The other lines: own lines step 1 had no room for first, then the rest in file order.
    for index in order:
        lines = files[index].lines
        own_numbers = protected.get(files[index].path, set())
        base = kept.get(index, ())
        taken = set(base)
        left = [i for i in range(len(lines)) if i not in taken]
        extra = (
            *(i for i in left if lines[i].number in own_numbers),
            *(i for i in left if lines[i].number not in own_numbers),
        )
        if picked := widest(index, base, extra):
            kept[index] = picked

    return chosen(), shown, shown_retrieved


def _fit_changed_files_for_verifier(
    files: tuple[ChangedFile, ...],
    rules: tuple[ReviewRule, ...],
    max_tokens: int,
    counter: TokenCounter,
    candidates: tuple[CandidateFindingItem, ...] = (),
) -> tuple[ChangedFile, ...]:
    """The diff part of the fit, against a budget for the ``<code_context>`` block alone.

    Test-only seam: no production code calls it (the builder fits through
    ``_fit_verifier_code``); its cases live in the shared ``tests/test_reflection_pass.py``,
    so it goes once #303 lands.
    """

    def fits(
        chosen: tuple[ChangedFile, ...],
        shown: tuple[ContextBlock, ...],
        _retrieved: tuple[RetrievedBlock, ...],
    ) -> bool:
        return counter.count(_verifier_code_context(chosen, shown)) <= max_tokens

    fitted, _, _ = _fit_verifier_code(files, (), rules, candidates, fits)
    return fitted


def verify_verifier_mandatory_budget(
    context: ReviewContext,
    counter: TokenCounter | None = None,
    *,
    max_send_budget: int | None = None,
) -> None:
    """Ensure verifier mandatory blocks (system, rules, agents, defaults, conventions) fit budget.

    CVS §4.1 :493-495: "Обязательные system/defaults/policy должны вмещаться также в verifier
    и разрешённый fallback; иначе явная policy_prompt_budget_exceeded до первого вызова."
    """
    if context.policy_snapshot is None:
        return
    profile = context.policy_snapshot.execution_profile
    validate_execution_profile_versions(profile)
    budget = send_budget_for(profile, stage="verification", model_max_prompt_tokens=max_send_budget)

    builder = PromptBuilder()
    mandatory_blocks: list[str] = []
    rules_part = builder.render_policy_rules(context.policy_snapshot)
    agents_part = builder.render_policy_agents(context.policy_snapshot)
    mandatory_blocks.extend([rules_part, agents_part])
    if context.rules:
        mandatory_blocks.append(
            builder.render_service_defaults(
                context.rules,
                defaults_checksum=context.policy_snapshot.defaults_checksum,
            )
        )
    if context.conventions and (
        context.conventions.key_patterns or context.conventions.recommendations
    ):
        mandatory_blocks.append(builder.render_conventions(context.conventions))

    bindings = context.policy_snapshot.prompt_bindings
    version = bindings.verifier_version or "v2"
    filename = f"review.verifier.{version}.md"
    try:
        raw = files("review.prompts").joinpath(filename).read_text(encoding="utf-8")
        if raw.startswith("---"):
            parts = raw.split("---", 2)
            system_prompt = parts[2].strip() if len(parts) >= 3 else raw
        else:
            system_prompt = raw
    except (FileNotFoundError, ModuleNotFoundError, OSError) as exc:
        raise PolicyError(
            PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR,
            f"Unable to read verifier prompt resource '{filename}': {exc}",
        ) from exc

    if counter is None:
        counter_version = profile.counter_version
        counter = token_counter_for(counter_version)

    mandatory_text = system_prompt + "\n".join(mandatory_blocks)
    mandatory_tokens = counter.count(mandatory_text)

    if mandatory_tokens > budget:
        raise PolicyError(
            PolicyErrorCode.PROMPT_BUDGET_EXCEEDED,
            f"Verifier mandatory prompt is {mandatory_tokens} tokens, limit is {budget}",
            limit=budget,
            actual=mandatory_tokens,
        )


class VerifierPromptBuilder:
    """Constructs the prompt and CallManifest for the Reflection Pass verifier."""

    def __init__(self, system_prompt: str | None = None, *, verifier_version: str = "v2") -> None:
        self._verifier_version = verifier_version
        if system_prompt is not None:
            self._system_prompt = system_prompt
            self._raw_prompt = system_prompt
        else:
            filename = f"review.verifier.{verifier_version}.md"
            try:
                prompt_file = files("review.prompts").joinpath(filename)
                raw = prompt_file.read_text(encoding="utf-8")
            except (FileNotFoundError, ModuleNotFoundError, OSError) as exc:
                raise PolicyError(
                    PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR,
                    f"Unable to read verifier prompt resource '{filename}': {exc}",
                ) from exc
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
        max_prompt_tokens: int | None = None,
    ) -> tuple[VerifierPrompt, CallManifest]:
        """The verifier prompt and its manifest; ``max_prompt_tokens`` is the route window."""
        call_id = call_id or uuid.uuid4()

        # Check prompt checksum if snapshot binds verifier prompt
        if context.policy_snapshot is not None:
            bindings = context.policy_snapshot.prompt_bindings
            if bindings.verifier_version and bindings.verifier_version != self._verifier_version:
                filename = f"review.verifier.{bindings.verifier_version}.md"
                try:
                    v_file = files("review.prompts").joinpath(filename)
                    raw = v_file.read_text(encoding="utf-8")
                except (FileNotFoundError, ModuleNotFoundError, OSError) as exc:
                    raise PolicyError(
                        PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR,
                        f"Unable to read verifier prompt resource '{filename}': {exc}",
                    ) from exc
                self._verifier_version = bindings.verifier_version
                self._raw_prompt = raw
                if raw.startswith("---"):
                    parts = raw.split("---", 2)
                    self._system_prompt = parts[2].strip() if len(parts) >= 3 else raw
                else:
                    self._system_prompt = raw

            expected_checksum = bindings.verifier_checksum
            actual_checksum = sha256(self._raw_prompt.encode("utf-8")).hexdigest()
            if not expected_checksum or actual_checksum != expected_checksum:
                raise PolicyError(
                    PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR,
                    f"Verifier prompt checksum mismatch: expected {expected_checksum!r}, "
                    f"got {actual_checksum!r}",
                )

        builder = PromptBuilder()
        counter_version = (
            context.policy_snapshot.execution_profile.counter_version
            if context.policy_snapshot is not None
            else None
        )
        counter = token_counter_for(counter_version)
        # The snapshot path counts every manifest block with the pinned counter (R19).
        manifest_counter = counter if context.policy_snapshot is not None else None

        # 1. Policy documents and service defaults
        mandatory_blocks: list[str] = []
        if context.policy_snapshot is not None:
            validate_execution_profile_versions(context.policy_snapshot.execution_profile)
            rules_part = builder.render_policy_rules(context.policy_snapshot)
            agents_part = builder.render_policy_agents(context.policy_snapshot)
            mandatory_blocks.extend([rules_part, agents_part])
            if context.rules:
                mandatory_blocks.append(
                    builder.render_service_defaults(
                        context.rules,
                        defaults_checksum=context.policy_snapshot.defaults_checksum,
                    )
                )

        # 2. Low-trust conventions (CVS §7.1 :782)
        conventions_rendered: str | None = None
        if context.conventions and (
            context.conventions.key_patterns or context.conventions.recommendations
        ):
            conventions_rendered = builder.render_conventions(context.conventions)
            mandatory_blocks.append(conventions_rendered)

        # 3. Candidate findings
        cand_xml_list: list[str] = []
        candidate_blocks: list[ManifestBlock] = []
        for cand in candidates:
            f = cand.finding
            sugg_tag = (
                f"<suggestion>{xml_text(f.suggestion)}</suggestion>\n"
                if f.suggestion is not None
                else ""
            )
            rule_tag = (
                f'<rule_attribution name="{xml_attribute(f.rule_name)}" />\n'
                if f.rule_name is not None
                else ""
            )
            evidence_tag = ""
            if f.evidence_refs:
                refs_xml = "\n".join(
                    f'  <ref block_id="{xml_attribute(ref.block_id)}" '
                    f'start_line="{ref.start_line}" end_line="{ref.end_line}" />'
                    for ref in f.evidence_refs
                )
                evidence_tag = f"<evidence_refs>\n{refs_xml}\n</evidence_refs>\n"

            start_attr = f'start_line="{f.start_line}" ' if f.start_line is not None else ""
            category_attr = f'category="{xml_attribute(f.category)}" '
            cand_xml = (
                f'<candidate candidate_id="{xml_attribute(cand.candidate_id)}" '
                f'path="{xml_attribute(f.path)}" '
                f"{start_attr}"
                f'line="{f.line}" '
                f"{category_attr}"
                f'severity="{xml_attribute(f.severity)}">\n'
                f"<title>{xml_text(f.title)}</title>\n"
                f"<body>{xml_text(f.body)}</body>\n"
                f"{sugg_tag}"
                f"{rule_tag}"
                f"{evidence_tag}"
                f"</candidate>"
            )
            cand_xml_list.append(cand_xml)
            start_l = f.start_line or f.line
            candidate_blocks.append(
                ManifestBlock(
                    block_id=cand.candidate_id,
                    source_role=SourceRole.CANDIDATE,
                    source_kind="candidate_findings",
                    path=f.path,
                    start_line=start_l,
                    end_line=f.line,
                    shown_line_count=1,
                    shown_ranges=((start_l, f.line),),
                    tokens=None if manifest_counter is None else manifest_counter.count(cand_xml),
                )
            )

        candidates_xml = (
            f'<candidate_findings count="{len(candidates)}">\n'
            + "\n".join(cand_xml_list)
            + "\n</candidate_findings>"
        )
        mandatory_blocks.append(candidates_xml)

        # Context fitting per CVS §7.1 (:782): reserve room for IDs, candidates, and policy docs
        mandatory_text = self._system_prompt + "\n".join(mandatory_blocks)
        mandatory_tokens = counter.count(mandatory_text)

        # 4. Code context: the diff, the cited excerpts and RAG blocks, each with its block_id
        def user_message(
            files: tuple[ChangedFile, ...],
            excerpts: tuple[ContextBlock, ...],
            retrieved: tuple[RetrievedBlock, ...],
        ) -> str:
            code = _verifier_code_context(files, excerpts, retrieved)
            return "\n".join([*mandatory_blocks[:-1], code, candidates_xml])

        fitted_files = context.changed_files
        shown_excerpts = _cited_excerpts(context.context_blocks, candidates)
        # The RAG blocks follow the prompt's retrieved section, as the manifest lists them.
        shown_retrieved = (
            _cited_retrieved(context.retrieved, candidates) if context.retrieved_section else ()
        )
        if context.policy_snapshot is not None:
            max_send_budget = send_budget_for(
                context.policy_snapshot.execution_profile,
                stage="verification",
                model_max_prompt_tokens=max_prompt_tokens,
            )
            if mandatory_tokens > max_send_budget:
                raise RunFailure(
                    "verification_context_overflow",
                    f"Verifier mandatory prompt is {mandatory_tokens} tokens, "
                    f"limit is {max_send_budget}",
                )
            system_tokens = counter.count(self._system_prompt)

            # CONTRACT: the same sum as the pre-send guard in ReflectionPass.verify_recorded
            # (system and user message counted apart, the user message as sent), so a fitted
            # prompt never fails the guard.
            def prompt_tokens(
                files: tuple[ChangedFile, ...],
                excerpts: tuple[ContextBlock, ...],
                retrieved: tuple[RetrievedBlock, ...],
            ) -> int:
                return system_tokens + counter.count(user_message(files, excerpts, retrieved))

            if prompt_tokens((), (), ()) > max_send_budget:
                raise RunFailure(
                    "verification_context_overflow",
                    f"Verifier prompt is {prompt_tokens((), (), ())} tokens, "
                    f"limit is {max_send_budget}",
                )
            fitted_files, shown_excerpts, shown_retrieved = _fit_verifier_code(
                context.changed_files,
                shown_excerpts,
                context.rules,
                candidates,
                lambda files, excerpts, retrieved: (
                    prompt_tokens(files, excerpts, retrieved) <= max_send_budget
                ),
                retrieved=shown_retrieved,
            )

        prompt = VerifierPrompt(
            system=self._system_prompt,
            user=user_message(fitted_files, shown_excerpts, shown_retrieved),
        )

        # The manifest names exactly the shown code: never an uncited or dropped excerpt or
        # RAG block.
        fitted_context = replace(
            context,
            changed_files=fitted_files,
            context_blocks=shown_excerpts,
            retrieved=shown_retrieved,
        )
        prompt_builder_manifest = builder.build_manifest(
            fitted_context, call_id=call_id, stage="verification", counter=manifest_counter
        )
        conventions_blocks: list[ManifestBlock] = []
        if conventions_rendered is not None:
            conv_lines = len(conventions_rendered.splitlines()) or 1
            conventions_blocks.append(
                ManifestBlock(
                    block_id="repo-conventions",
                    source_role=SourceRole.CONVENTIONS,
                    source_kind="generated_conventions",
                    start_line=1,
                    end_line=conv_lines,
                    shown_line_count=conv_lines,
                    shown_ranges=((1, conv_lines),),
                    tokens=None
                    if manifest_counter is None
                    else manifest_counter.count(conventions_rendered),
                )
            )
        all_blocks = (
            prompt_builder_manifest.blocks + tuple(conventions_blocks) + tuple(candidate_blocks)
        )
        verifier_manifest = CallManifest(
            call_id=call_id,
            run_id=prompt_builder_manifest.run_id,
            stage="verification",
            recovery_kind="none",
            blocks=all_blocks,
        )
        return prompt, verifier_manifest

    # Pre-send guard on verifier prompt tokens is performed in ReflectionPass.verify.


class ReflectionPass:
    """Orchestrates candidate verification against visible code and policy requirements."""

    def __init__(
        self,
        model: VerifierModel,
        *,
        system_prompt: str | None = None,
    ) -> None:
        self._model = model
        self._system_prompt = system_prompt

    @cached_property
    def _prompt_builder(self) -> VerifierPromptBuilder:
        """Built when the pass first verifies: the worker gives every attempt a pass, a legacy
        one too, which never verifies, so a missing verifier prompt file must not fail it."""
        return VerifierPromptBuilder(system_prompt=self._system_prompt)

    def max_prompt_tokens(self) -> int | None:
        """The verifier route's prompt window, when the model knows it (R10)."""
        return self._model.max_prompt_tokens() if isinstance(self._model, VerifierWindow) else None

    async def verify(
        self,
        *,
        candidates: tuple[CandidateFindingItem, ...],
        context: ReviewContext,
    ) -> tuple[VerificationDecision, ...]:
        record = await self.verify_recorded(candidates=candidates, context=context)
        return record.decisions

    async def verify_recorded(
        self,
        *,
        candidates: tuple[CandidateFindingItem, ...],
        context: ReviewContext,
    ) -> VerificationRecord:
        """``verify`` plus what the ``llm.verify`` run action records (R13)."""
        if not candidates:
            return VerificationRecord(decisions=(), candidate_ids=())

        window = self.max_prompt_tokens()
        prompt, manifest = await asyncio.to_thread(
            self._prompt_builder.build_verifier_prompt,
            candidates=candidates,
            context=context,
            max_prompt_tokens=window,
        )

        # Pre-send guard on verifier prompt tokens
        if context.policy_snapshot is not None:
            profile = context.policy_snapshot.execution_profile
            counter = token_counter_for(profile.counter_version)
            total_tokens = counter.count(prompt.system) + counter.count(prompt.user)
            max_send_budget = send_budget_for(
                profile, stage="verification", model_max_prompt_tokens=window
            )
            if total_tokens > max_send_budget:
                raise RunFailure(
                    "verification_context_overflow",
                    f"Verifier prompt is {total_tokens} tokens, limit is {max_send_budget}",
                )
            verify_provenance_pre_send_guard(
                manifest, context=context, snapshot=context.policy_snapshot
            )

        try:
            reply = await self._model.verify(
                system_prompt=prompt.system,
                user_prompt=prompt.user,
                manifest=manifest,
                context=context,
            )
        except TimeoutError as exc:
            raise VerificationUnavailableError("Verifier model timed out") from exc
        except (
            VerificationInvalidOutputError,
            VerificationUnavailableError,
            PolicyError,
        ):
            raise
        except Exception as exc:
            error_code = getattr(exc, "error_code", None)
            if error_code in {"llm_invalid_output", "verification_invalid_output"}:
                raise VerificationInvalidOutputError("Verifier model output invalid") from exc
            if error_code in {"llm_timeout", "llm_unavailable", "verification_unavailable"}:
                raise VerificationUnavailableError("Verifier model unavailable") from exc
            if error_code in {"budget_exceeded", "verification_budget_exceeded"}:
                raise RunFailure(
                    "verification_budget_exceeded", "Verifier budget exceeded"
                ) from exc
            if error_code in {"deadline_exceeded", "verification_deadline_exceeded"}:
                raise RunFailure(
                    "verification_deadline_exceeded", "Verifier deadline exceeded"
                ) from exc
            if error_code in {"llm_context_overflow", "verification_context_overflow"}:
                raise RunFailure(
                    "verification_context_overflow", "Verifier prompt context overflow"
                ) from exc
            if error_code == "llm_payment_required":
                # PIPELINE_SPEC §6: a 402 is not retried by the run either.
                raise RunFailure(
                    "llm_payment_required", "Verifier provider payment required"
                ) from exc
            if error_code == "llm_rate_limited":
                # RetryDelays waits at least this retry_after (2m at least, the 10m queue
                # above two minutes, up to one day); the cap keeps a huge provider header
                # from overflowing timedelta.
                retry_after_s = getattr(exc, "retry_after_s", None) or 0
                raise RunFailure(
                    "verification_unavailable",
                    "Verifier model rate limited",
                    retry_after=timedelta(seconds=min(retry_after_s, _MAX_RETRY_AFTER_S)),
                ) from exc
            if isinstance(exc, (RunFailure, RunCancelled)):
                # Not a provider error: a manifest write that failed keeps internal_error
                # (PIPELINE_SPEC:174) and a cancellation stays a cancellation.
                raise
            raise VerificationUnavailableError("Verifier model call failed") from exc

        manifest_id: uuid.UUID | None = None
        if isinstance(reply, VerifierReply):
            raw_output, accepted_call_id = reply.output, reply.accepted_call_id
            manifest_id = reply.manifest_id
        else:
            raw_output, accepted_call_id = reply, None
        try:
            if isinstance(raw_output, (str, bytes)):
                parsed = VerificationOutput.model_validate_json(raw_output)
            elif isinstance(raw_output, Mapping):
                parsed = VerificationOutput.model_validate(dict(raw_output))
            else:
                raise VerificationInvalidOutputError(
                    f"Unexpected verifier output type: {type(raw_output)}"
                )
        except VerificationInvalidOutputError:
            raise
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            raise VerificationInvalidOutputError(
                "Malformed verifier response schema or JSON"
            ) from exc

        # Ensure exact 1:1 match of decisions to candidate IDs
        cand_by_id = {c.candidate_id: c for c in candidates}
        decision_ids = {d.candidate_id for d in parsed.decisions}

        if len(parsed.decisions) != len(candidates) or decision_ids != set(cand_by_id.keys()):
            raise VerificationInvalidOutputError(
                f"Verifier returned decisions for {len(parsed.decisions)} items, "
                f"expected {len(candidates)}"
            )

        # Build check lines mapping for service defaults:
        # shown stable check_id -> line_number
        service_defaults_check_lines: dict[str, int] = {}
        line_offset = 1
        if context.rules:
            defaults_checksum = (
                context.policy_snapshot.defaults_checksum
                if context.policy_snapshot is not None
                else None
            )
            for r in context.rules:
                check_ids = get_rule_check_ids(r, defaults_checksum=defaults_checksum)
                for c_id in check_ids:
                    if c_id in service_defaults_check_lines:
                        raise PolicyError(
                            PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR,
                            f"Duplicate service defaults check ID {c_id!r}",
                        )
                    service_defaults_check_lines[c_id] = line_offset
                    line_offset += 1

        # Validate evidence and requirement references against manifest
        for decision in parsed.decisions:
            cand = cand_by_id[decision.candidate_id]
            cand_id_clean = decision.candidate_id[:64]

            # 1. Validate evidence_refs
            for ref in decision.evidence_refs:
                block_id_clean = ref.block_id[:64]
                block = manifest.get_block(ref.block_id)
                if block is None:
                    raise VerificationInvalidOutputError(
                        f"Decision {cand_id_clean} evidence_ref {block_id_clean} "
                        "not found in manifest"
                    )
                if block.source_role not in {
                    "diff",
                    "source_excerpt",
                    "ast_excerpt",
                    "retrieved_excerpt",
                }:
                    raise VerificationInvalidOutputError(
                        f"Decision {cand_id_clean} evidence_ref {block_id_clean} "
                        f"has non-code role '{block.source_role}'"
                    )
                if block.revision is not None and context.pr_meta is not None:
                    if block.side == "LEFT":
                        expected_rev = context.pr_meta.diff_old_sha
                        if not expected_rev or block.revision != expected_rev:
                            raise VerificationInvalidOutputError(
                                f"Decision {cand_id_clean} evidence_ref {block_id_clean} "
                                f"LEFT revision '{block.revision}' does not match "
                                f"PR diff_old_sha '{expected_rev}'"
                            )
                    else:
                        expected_rev = context.pr_meta.head_sha
                        # Fail closed like LEFT (D7, #144): without a head no revision passes.
                        if not expected_rev or block.revision != expected_rev:
                            raise VerificationInvalidOutputError(
                                f"Decision {cand_id_clean} evidence_ref {block_id_clean} "
                                f"revision '{block.revision}' does not match PR revision"
                            )
                if not manifest.is_range_shown(ref.block_id, ref.start_line, ref.end_line):
                    raise VerificationInvalidOutputError(
                        f"Decision {cand_id_clean} evidence_ref {block_id_clean}:"
                        f"{ref.start_line}-{ref.end_line} is not shown in verifier manifest"
                    )

            # 2. Validate requirement_refs
            for req_ref in decision.requirement_refs:
                req_block_id_clean = req_ref.block_id[:64]
                block = manifest.get_block(req_ref.block_id)
                if block is None:
                    raise VerificationInvalidOutputError(
                        f"Decision {cand_id_clean} requirement_ref block "
                        f"'{req_block_id_clean}' not found in manifest"
                    )
                if block.source_role not in {"policy_document", "service_defaults"}:
                    raise VerificationInvalidOutputError(
                        f"Decision {cand_id_clean} requirement_ref block "
                        f"'{req_block_id_clean}' has invalid role '{block.source_role}'"
                    )
                if not manifest.is_range_shown(
                    req_ref.block_id, req_ref.start_line, req_ref.end_line
                ):
                    raise VerificationInvalidOutputError(
                        f"Decision {cand_id_clean} requirement_ref range "
                        f"{req_ref.start_line}-{req_ref.end_line} "
                        f"not shown in '{req_block_id_clean}'"
                    )
                if block.source_role == "service_defaults":
                    if req_ref.check_id is None:
                        raise VerificationInvalidOutputError(
                            f"Decision {cand_id_clean} check_id is required for service_defaults"
                        )
                    req_check_id_clean = req_ref.check_id[:64]
                    if cand.finding.rule_name and cand.finding.rule_name.startswith(
                        "service-defaults:"
                    ):
                        rule_id = cand.finding.rule_name.removeprefix("service-defaults:")
                        expected_line = (
                            service_defaults_check_lines.get(rule_id)
                            if req_ref.check_id == rule_id
                            else None
                        )
                        if expected_line is None:
                            raise VerificationInvalidOutputError(
                                f"Decision {cand_id_clean} check_id '{req_check_id_clean}' "
                                f"does not match service defaults rule '{rule_id}'"
                            )
                        if not (req_ref.start_line <= expected_line <= req_ref.end_line):
                            raise VerificationInvalidOutputError(
                                f"Decision {cand_id_clean} requirement_ref range "
                                f"{req_ref.start_line}-{req_ref.end_line} does not cover check "
                                f"'{req_check_id_clean}' line {expected_line}"
                            )
                    else:
                        expected_line = service_defaults_check_lines.get(req_ref.check_id)
                        if expected_line is None:
                            raise VerificationInvalidOutputError(
                                f"Decision {cand_id_clean} check_id '{req_check_id_clean}' "
                                "does not match any service defaults rule"
                            )
                        if not (req_ref.start_line <= expected_line <= req_ref.end_line):
                            raise VerificationInvalidOutputError(
                                f"Decision {cand_id_clean} requirement_ref range "
                                f"{req_ref.start_line}-{req_ref.end_line} does not cover check "
                                f"'{req_check_id_clean}' line {expected_line}"
                            )
                else:
                    if req_ref.check_id is not None:
                        raise VerificationInvalidOutputError(
                            f"Decision {cand_id_clean} check_id is only allowed "
                            f"for service_defaults, got '{block.source_role}'"
                        )

            # 3. For supported/contradicted with rule_name, require at least one matching
            # requirement_ref (RULES_FORMAT_SPEC §8.2)
            if cand.finding.rule_name is not None and decision.status in {
                "supported",
                "contradicted",
            }:
                rname = cand.finding.rule_name
                matched = False
                for req_ref in decision.requirement_refs:
                    block = manifest.get_block(req_ref.block_id)
                    if block is not None and (
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

        return VerificationRecord(
            decisions=tuple(parsed.decisions),
            candidate_ids=tuple(c.candidate_id for c in candidates),
            accepted_call_id=accepted_call_id,
            manifest_id=manifest_id,
        )


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
