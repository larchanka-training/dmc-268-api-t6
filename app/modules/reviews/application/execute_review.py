"""Compose the durable inputs, model request, and review publication for one run."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.modules.reviews.application.conventions import GeneratedConventions
from app.modules.reviews.application.prompt_builder import (
    ChangedFile,
    PromptBuilder,
    PullRequestMeta,
    RepoConventions,
    ReviewContext,
    ReviewRule,
)
from app.modules.reviews.application.review_output import PublishReviewOutput


@dataclass(frozen=True)
class ReviewPromptInput:
    """All persisted, revision-pinned data required for a model prompt."""

    system: str
    rules: tuple[ReviewRule, ...]
    agents_md: str | None
    conventions: RepoConventions
    changed_files: tuple[ChangedFile, ...]
    omitted_files: tuple[str, ...]


class ReviewPromptRepository(Protocol):
    """Read the immutable review context after diff and conventions are durable."""

    async def get_review_prompt_input(
        self, run_id: UUID, conventions: GeneratedConventions
    ) -> ReviewPromptInput | None: ...


class ReviewModel(Protocol):
    """Injected model boundary; no vendor SDK belongs in the worker pipeline."""

    async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta | None: ...

    async def draft_review(self, *, prompt: str) -> Mapping[str, object] | str | bytes: ...


class ReviewInputProcessor(Protocol):
    async def prepare(self, run_id: UUID) -> GeneratedConventions | bool | None: ...


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
        publisher: PublishReviewOutput,
    ) -> None:
        self._processor = processor
        self._repository = repository
        self._model = model
        self._publisher = publisher

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
        prompt = PromptBuilder().build(
            ReviewContext(
                system=prompt_input.system,
                rules=prompt_input.rules,
                agents_md=prompt_input.agents_md,
                conventions=prompt_input.conventions,
                pr_meta=meta,
                changed_files=prompt_input.changed_files,
                omitted_files=prompt_input.omitted_files,
            )
        )
        raw_output = await self._model.draft_review(prompt=prompt)
        return await self._publisher.execute(run_id, raw_output)
