"""Compose the durable inputs, model request, and review publication for one run."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from pydantic import ValidationError

from app.modules.reviews.application.conventions import GeneratedConventions
from app.modules.reviews.application.findings_post_processor import (
    FindingsPostProcessor,
    ProcessedFinding,
)
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.policy_snapshot import PolicySnapshot
from app.modules.reviews.application.prompt_budget import (
    EstimatedTokenCounter,
    PolicyBudgetExceededError,
    PreSendGuardError,
    fit_review_context,
    verify_pre_send_guard,
    verify_provenance_pre_send_guard,
)
from app.modules.reviews.application.prompt_builder import (
    ChangedFile,
    PromptBuilder,
    PullRequestMeta,
    RepoConventions,
    ReviewContext,
    ReviewRule,
)
from app.modules.reviews.application.reflection_pass import (
    CandidateFindingItem,
    ReflectionPass,
    filter_verified_candidates,
)
from app.modules.reviews.application.review_output import (
    InvalidReviewOutput,
    ReviewFinding,
    ReviewFindingV2,
    ReviewOutputHandler,
    validate_v2_attribution,
)
from app.modules.reviews.application.run_failures import RunFailure
from app.modules.reviews.application.verdict import SeverityThresholdValue

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReviewPromptInput:
    """All persisted, revision-pinned data required for a model prompt."""

    system: str
    rules: tuple[ReviewRule, ...]
    agents_md: str | None
    conventions: RepoConventions
    changed_files: tuple[ChangedFile, ...]
    omitted_files: tuple[str, ...]
    policy_snapshot: PolicySnapshot | None = None
    severity_threshold: SeverityThresholdValue = "all"


def build_deterministic_summary(
    final_findings: Sequence[ReviewFinding | ReviewFindingV2],
    *,
    changed_files: tuple[ChangedFile, ...] | list[ChangedFile],
    omitted_files: tuple[str, ...] | list[str] = (),
    below_threshold_count: int = 0,
) -> dict[str, object]:
    """Construct deterministic ReviewSummary per CONTEXT_AND_VERIFICATION_SPEC §8.2.

    Raw model summary is never published under any circumstances.
    """
    done_well = "Code analysis and policy verification were completed across all evaluated changes."

    if not final_findings:
        if below_threshold_count > 0:
            problem = (
                f"No verified issues meeting the severity threshold were found "
                f"({below_threshold_count} below threshold)."
            )
        else:
            problem = "No verified issues were found in the evaluated changes."
        effort = "none"
    else:
        severities = {f.severity for f in final_findings}
        if "critical" in severities:
            effort = "large"
        elif "high" in severities:
            effort = "medium"
        elif "medium" in severities or "low" in severities:
            effort = "small"
        else:
            effort = "none"

        categories = sorted({f.category for f in final_findings})
        cat_desc = ", ".join(categories)
        issue_word = "issue" if len(final_findings) == 1 else "issues"
        problem = (
            f"Identified {len(final_findings)} verified {issue_word} ({cat_desc}) "
            "in the reviewed changes."
        )

    return {
        "problem": problem,
        "done_well": done_well,
        "effort": effort,
    }


class ReviewPromptRepository(Protocol):
    """Read the immutable review context after diff and conventions are durable."""

    async def get_review_prompt_input(
        self, run_id: UUID, conventions: GeneratedConventions
    ) -> ReviewPromptInput | None: ...


class PullRequestMetaSource(Protocol):
    """Provider-neutral pull request metadata, including the commit context (#11)."""

    async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta | None: ...


class ReviewModel(PullRequestMetaSource, Protocol):
    """Request the review findings from the configured provider.

    ``draft_review`` receives the immutable context, not a rendered string: the LLM
    gateway (#33) fits it to its model's token budget, renders it with
    ``PromptBuilder`` as a system and a user message and returns the accepted JSON
    object. Run-attempt data (deadline, attempt, cost limit) is bound to the model
    adapter by the worker when it claims the run.
    """

    async def draft_review(
        self, *, context: ReviewContext
    ) -> Mapping[str, object] | str | bytes: ...


class ReviewInputProcessor(Protocol):
    async def prepare(self, run_id: UUID) -> GeneratedConventions | bool | None: ...


class ReviewContextFilter(Protocol):
    """Which paths the policy stage of this attempt withheld from model inputs."""

    def excluded(self, path: str) -> bool: ...


class ExecuteReviewRun:
    """Finish an ordinary run after its diff and conventions have been saved.

    Database reads are short-lived and every model/provider call occurs after
    they complete. ``PublishReviewOutput`` owns its own two write transactions.
    """

    def __init__(
        self,
        processor: ReviewInputProcessor,
        repository: ReviewPromptRepository,
        model: ReviewModel,
        publisher: ReviewOutputHandler,
        context_filter: ReviewContextFilter | None = None,
        reflection_pass: ReflectionPass | None = None,
    ) -> None:
        self._processor = processor
        self._repository = repository
        self._model = model
        self._publisher = publisher
        self._context_filter = context_filter
        self._reflection_pass = reflection_pass

    async def execute(self, run_id: UUID) -> bool:
        conventions = await self._processor.prepare(run_id)
        if not isinstance(conventions, GeneratedConventions):
            return False
        prompt_input = await self._repository.get_review_prompt_input(run_id, conventions)
        if prompt_input is None:
            return False
        meta = await self._model.get_pull_request_meta(run_id)
        if meta is None:
            return False
        changed_files, omitted_files = prompt_input.changed_files, prompt_input.omitted_files
        if self._context_filter is not None:
            # An ignored file is neither shown nor named: it is dropped from both lists.
            excluded = self._context_filter.excluded
            changed_files = tuple(file for file in changed_files if not excluded(file.path))
            omitted_files = tuple(path for path in omitted_files if not excluded(path))
        context = ReviewContext(
            system=prompt_input.system,
            rules=prompt_input.rules,
            agents_md=prompt_input.agents_md,
            conventions=prompt_input.conventions,
            pr_meta=meta,
            changed_files=changed_files,
            omitted_files=omitted_files,
            policy_snapshot=prompt_input.policy_snapshot,
            severity_threshold=prompt_input.severity_threshold,
        )

        if prompt_input.policy_snapshot is not None:
            if self._reflection_pass is None:
                raise PolicyError(
                    PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR,
                    "Reflection pass is required when policy snapshot is present",
                )
            if not prompt_input.policy_snapshot.is_ready_for_llm:
                raise PolicyError(
                    PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR,
                    "Policy snapshot is not ready for LLM call",
                )
            counter = EstimatedTokenCounter()
            max_input = prompt_input.policy_snapshot.execution_profile.max_input_tokens
            context = fit_review_context(context, max_prompt_tokens=max_input, counter=counter)
            try:
                verify_pre_send_guard(
                    context,
                    counter,
                    max_input_tokens=max_input,
                )
            except PolicyBudgetExceededError as exc:
                raise PolicyError(
                    PolicyErrorCode.PROMPT_BUDGET_EXCEEDED,
                    str(exc),
                    limit=exc.limit,
                    actual=exc.actual,
                ) from exc
            except PreSendGuardError as exc:
                raise RunFailure("llm_context_overflow", str(exc)) from exc

            call_id = uuid.uuid4()
            reviewer_manifest = PromptBuilder().build_manifest(
                context, call_id=call_id, stage="review"
            )
            try:
                verify_provenance_pre_send_guard(
                    reviewer_manifest, context=context, snapshot=prompt_input.policy_snapshot
                )
            except PolicyError as exc:
                if exc.code == PolicyErrorCode.CONTEXT_VIOLATION:
                    raise RunFailure("policy_context_violation", str(exc)) from exc
                raise
        elif self._reflection_pass is not None:
            _LOGGER.warning(
                "execute_review_legacy_run_without_snapshot: reflection pass provided but "
                "bypassed because policy snapshot is absent for run %s",
                run_id,
            )

        model_output = await self._model.draft_review(context=context)
        if self._reflection_pass is not None and context.policy_snapshot is not None:
            model_output = await self._run_reflection_pass(model_output, context, run_id=run_id)
        return await self._publisher.execute(run_id, model_output)

    async def _run_reflection_pass(
        self,
        model_output: Mapping[str, object] | str | bytes,
        context: ReviewContext,
        *,
        run_id: UUID,
    ) -> Mapping[str, object] | str | bytes:
        assert self._reflection_pass is not None
        assert context.policy_snapshot is not None

        if isinstance(model_output, (str, bytes)):
            try:
                data = json.loads(model_output)
            except json.JSONDecodeError as exc:
                raise InvalidReviewOutput(f"Malformed review model JSON output: {exc}") from exc
        elif isinstance(model_output, Mapping):
            data = dict(model_output)
        else:
            raise InvalidReviewOutput(f"Unexpected review model output type: {type(model_output)}")

        if not isinstance(data, dict):
            raise InvalidReviewOutput(f"Expected dict for review model output, got {type(data)}")

        raw_findings = data.get("findings")
        if raw_findings is None or not isinstance(raw_findings, list):
            raise InvalidReviewOutput("Review model output missing 'findings' list")

        if not raw_findings:
            # Zero candidates: Reflection verifier is skipped (0 calls)
            data["findings"] = []
            data["summary"] = build_deterministic_summary(
                (),
                changed_files=context.changed_files,
                omitted_files=context.omitted_files,
            )
            return data

        if len(raw_findings) > 10:
            raise InvalidReviewOutput("Draft review exceeded maximum of 10 findings")

        reviewer_manifest = PromptBuilder().build_manifest(
            context, call_id=uuid.uuid4(), stage="review"
        )
        candidates: list[CandidateFindingItem] = []
        for idx, item in enumerate(raw_findings):
            cand_id = str(uuid.uuid4())
            try:
                if isinstance(item, ReviewFindingV2):
                    finding = item
                elif isinstance(item, dict):
                    if "evidence_refs" not in item:
                        raise InvalidReviewOutput(
                            f"Candidate finding at index {idx} missing required evidence_refs"
                        )
                    finding = ReviewFindingV2.model_validate(item)
                elif hasattr(item, "model_dump"):
                    d = item.model_dump()
                    if "evidence_refs" not in d:
                        raise InvalidReviewOutput(
                            f"Candidate finding at index {idx} missing required evidence_refs"
                        )
                    finding = ReviewFindingV2.model_validate(d)
                else:
                    raise InvalidReviewOutput(
                        f"Unsupported finding type in draft review: {type(item)}"
                    )
            except InvalidReviewOutput:
                raise
            except ValidationError as err:
                sanitized_errors = "; ".join(
                    f"{'.'.join(str(loc) for loc in e['loc'])}: {e['msg']}" for e in err.errors()
                )
                raise InvalidReviewOutput(
                    f"Malformed candidate finding at index {idx}: {sanitized_errors}"
                ) from err
            except ValueError as err:
                raise InvalidReviewOutput(
                    f"Malformed candidate finding at index {idx}: {str(err)[:200]}"
                ) from err

            # Pass 3: Validate attribution against manifest.
            # Unshown evidence refs drop only this candidate (CVS §6.2 :702, :729-730)
            valid_attr, attr_err = validate_v2_attribution(finding, reviewer_manifest)
            if not valid_attr:
                _LOGGER.info(
                    "Dropping candidate finding %s due to unshown evidence (%s)",
                    cand_id,
                    attr_err,
                )
                continue

            candidates.append(CandidateFindingItem(candidate_id=cand_id, finding=finding))

        # Pass 4: Coordinates, lint filter, and dedup BEFORE verifier (CVS §6.2 :704)
        post_processor = FindingsPostProcessor.from_default_patterns()
        processed_candidates = [
            ProcessedFinding(c.finding, idx, "inline") for idx, c in enumerate(candidates)
        ]
        # Filter unknown paths
        valid_paths = {f.path for f in context.changed_files}
        valid_path_candidates = [p for p in processed_candidates if p.finding.path in valid_paths]
        kept_candidates, _ = post_processor._drop_lint_patterns(valid_path_candidates)
        kept_candidates, _ = post_processor._deduplicate(kept_candidates)

        kept_findings_set = {id(p.finding) for p in kept_candidates}
        filtered_candidates = [c for c in candidates if id(c.finding) in kept_findings_set]

        # If empty after filters, bypass verifier (0 calls)
        if not filtered_candidates:
            data["findings"] = []
            data["summary"] = build_deterministic_summary(
                (),
                changed_files=context.changed_files,
                omitted_files=context.omitted_files,
            )
            return data

        # Pass 5: Verifier invocation
        decisions = await self._reflection_pass.verify(
            candidates=tuple(filtered_candidates), context=context
        )
        supported, rejected = filter_verified_candidates(
            candidates=tuple(filtered_candidates), decisions=decisions
        )
        _LOGGER.info(
            "Reflection pass completed for run %s (call %s): "
            "total=%d, supported=%d, rejected=%d. Decisions: %s",
            run_id,
            reviewer_manifest.call_id,
            len(filtered_candidates),
            len(supported),
            len(rejected),
            [(d.candidate_id, d.status) for d in decisions],
        )

        # Pass 6: Severity threshold after verifier (CVS §6.2 :705)
        severity_threshold: SeverityThresholdValue = getattr(context, "severity_threshold", "all")
        supported_processed = [
            ProcessedFinding(cand.finding, idx, "inline") for idx, cand in enumerate(supported)
        ]
        surviving_processed, below_threshold_processed = post_processor._drop_below_threshold(
            supported_processed, severity_threshold
        )
        final_findings = [p.finding for p in surviving_processed]

        data["findings"] = [f.model_dump() for f in final_findings]
        # Deterministic summary is ALWAYS built from post-threshold supported findings (CVS §8.2)
        data["summary"] = build_deterministic_summary(
            final_findings,
            changed_files=context.changed_files,
            omitted_files=context.omitted_files,
            below_threshold_count=len(below_threshold_processed),
        )

        return data
