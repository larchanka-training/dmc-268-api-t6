"""Compose the durable inputs, model request, and review publication for one run."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.modules.reviews.application.conventions import GeneratedConventions
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.policy_snapshot import PolicySnapshot
from app.modules.reviews.application.prompt_budget import (
    EstimatedTokenCounter,
    PolicyBudgetExceededError,
    PreSendGuardError,
    verify_pre_send_guard,
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
    ReviewFindingV2,
    ReviewOutputHandler,
    validate_v2_attribution,
)
from app.modules.reviews.application.run_failures import RunFailure

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
            try:
                verify_pre_send_guard(
                    context,
                    counter,
                    max_input_tokens=prompt_input.policy_snapshot.execution_profile.max_input_tokens,
                )
            except PolicyBudgetExceededError as exc:
                raise PolicyError(
                    PolicyErrorCode.PROMPT_BUDGET_EXCEEDED,
                    str(exc),
                ) from exc
            except PreSendGuardError as exc:
                raise RunFailure("llm_context_overflow", str(exc)) from exc

        model_output = await self._model.draft_review(context=context)
        if self._reflection_pass is not None:
            model_output = await self._run_reflection_pass(model_output, context)
        return await self._publisher.execute(run_id, model_output)

    async def _run_reflection_pass(
        self,
        model_output: Mapping[str, object] | str | bytes,
        context: ReviewContext,
    ) -> Mapping[str, object] | str | bytes:
        if self._reflection_pass is None:
            return model_output

        if isinstance(model_output, (str, bytes)):
            try:
                data = json.loads(model_output)
            except json.JSONDecodeError as exc:
                if context.policy_snapshot is not None:
                    raise InvalidReviewOutput(f"Malformed review model JSON output: {exc}") from exc
                return model_output
        elif isinstance(model_output, Mapping):
            data = dict(model_output)
        else:
            if context.policy_snapshot is not None:
                raise InvalidReviewOutput(
                    f"Unexpected review model output type: {type(model_output)}"
                )
            return model_output

        if not isinstance(data, dict):
            if context.policy_snapshot is not None:
                raise InvalidReviewOutput(
                    f"Expected dict for review model output, got {type(data)}"
                )
            return model_output

        raw_findings = data.get("findings")
        if raw_findings is None or not isinstance(raw_findings, list):
            if context.policy_snapshot is not None and "findings" not in data:
                raise InvalidReviewOutput("Review model output missing 'findings' list")
            return model_output

        if not raw_findings:
            # Zero candidates: Reflection verifier is skipped (0 calls)
            return model_output

        reviewer_manifest = PromptBuilder().build_manifest(
            context, call_id=uuid.uuid4(), stage="review"
        )
        candidates: list[CandidateFindingItem] = []
        for idx, item in enumerate(raw_findings):
            cand_id = str(uuid.uuid4())
            if isinstance(item, ReviewFindingV2):
                finding = item
            elif isinstance(item, dict):
                if context.policy_snapshot is not None and "evidence_refs" not in item:
                    raise InvalidReviewOutput(
                        f"Candidate finding at index {idx} missing required evidence_refs"
                    )
                finding = ReviewFindingV2.model_validate(item)
            elif hasattr(item, "model_dump"):
                d = item.model_dump()
                if context.policy_snapshot is not None and "evidence_refs" not in d:
                    raise InvalidReviewOutput(
                        f"Candidate finding at index {idx} missing required evidence_refs"
                    )
                finding = ReviewFindingV2.model_validate(d)
            else:
                raise InvalidReviewOutput(f"Unsupported finding type in draft review: {type(item)}")

            valid_attr, attr_err = validate_v2_attribution(finding, reviewer_manifest)
            if not valid_attr:
                raise InvalidReviewOutput(f"Invalid attribution in candidate finding: {attr_err}")

            candidates.append(CandidateFindingItem(candidate_id=cand_id, finding=finding))

        decisions = await self._reflection_pass.verify(
            candidates=tuple(candidates), context=context
        )
        supported, rejected = filter_verified_candidates(
            candidates=tuple(candidates), decisions=decisions
        )
        _LOGGER.info(
            "Reflection pass completed for run: total=%d, supported=%d, rejected=%d. Decisions: %s",
            len(candidates),
            len(supported),
            len(rejected),
            [(d.candidate_id, d.status) for d in decisions],
        )

        data["findings"] = [cand.finding.model_dump() for cand in supported]

        # Cleansing: never leak unverified defects through the summary
        if not supported:
            orig_done_well = "The proposed changes look clean and well-structured."
            if isinstance(data.get("summary"), dict):
                dw = data["summary"].get("done_well")
                if isinstance(dw, str) and dw:
                    orig_done_well = dw
            data["summary"] = {
                "problem": "No verified issues were found in the provided changes.",
                "done_well": orig_done_well,
                "effort": "none",
            }
        elif len(supported) < len(candidates) and isinstance(data.get("summary"), dict):
            first_title = supported[0].finding.title
            issue_word = "issue" if len(supported) == 1 else "issues"
            data["summary"]["problem"] = (
                f"Identified {len(supported)} verified {issue_word}: {first_title}."
            )

        return data
