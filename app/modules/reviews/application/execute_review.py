"""Compose the durable inputs, model request, and review publication for one run."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.modules.reviews.application.conventions import GeneratedConventions
from app.modules.reviews.application.policy_snapshot import PolicySnapshot
from app.modules.reviews.application.prompt_builder import (
    ChangedFile,
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
from app.modules.reviews.application.review_output import ReviewFindingV2, ReviewOutputHandler

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
    """Injected model boundary; no vendor SDK belongs in the worker pipeline.

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
            except json.JSONDecodeError:
                return model_output
        elif isinstance(model_output, Mapping):
            data = dict(model_output)
        else:
            return model_output

        if not isinstance(data, dict):
            return model_output

        raw_findings = data.get("findings")
        if not raw_findings or not isinstance(raw_findings, list):
            # Zero candidates: Reflection verifier is skipped (0 calls)
            return model_output

        candidates: list[CandidateFindingItem] = []
        for idx, item in enumerate(raw_findings):
            cand_id = f"cand-{idx + 1}"
            if isinstance(item, ReviewFindingV2):
                finding = item
            elif isinstance(item, dict):
                if "evidence_refs" in item:
                    finding = ReviewFindingV2.model_validate(item)
                else:
                    path_str = item.get("path", "unknown")
                    block_id = f"file-{path_str.replace('/', '-').replace('.', '-')}"
                    start_l = item.get("start_line") or item.get("line", 1)
                    line_l = item.get("line", 1)
                    item_copy = dict(item)
                    item_copy["evidence_refs"] = [
                        {
                            "block_id": block_id,
                            "start_line": min(start_l, line_l),
                            "end_line": max(start_l, line_l),
                        }
                    ]
                    finding = ReviewFindingV2.model_validate(item_copy)
            elif hasattr(item, "model_dump"):
                d = item.model_dump()
                if "evidence_refs" not in d:
                    path_str = d.get("path", "unknown")
                    block_id = f"file-{path_str.replace('/', '-').replace('.', '-')}"
                    start_l = d.get("start_line") or d.get("line", 1)
                    line_l = d.get("line", 1)
                    d["evidence_refs"] = [
                        {
                            "block_id": block_id,
                            "start_line": min(start_l, line_l),
                            "end_line": max(start_l, line_l),
                        }
                    ]
                finding = ReviewFindingV2.model_validate(d)
            else:
                raise ValueError(f"Unsupported finding type in draft review: {type(item)}")
            candidates.append(CandidateFindingItem(candidate_id=cand_id, finding=finding))

        decisions = await self._reflection_pass.verify(
            candidates=tuple(candidates), context=context
        )
        supported, rejected = filter_verified_candidates(
            candidates=tuple(candidates), decisions=decisions
        )
        if rejected:
            _LOGGER.info(
                "Reflection pass filtered out %d candidates: %s",
                len(rejected),
                [(r[0].candidate_id, r[1], r[2]) for r in rejected],
            )
        data["findings"] = [cand.finding.model_dump() for cand in supported]
        return data
