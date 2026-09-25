"""Behavioural contract for the ordinary review-worker completion pipeline."""

from __future__ import annotations

import asyncio
from uuid import UUID

from app.modules.reviews.application.conventions import CachedConventions, GeneratedConventions
from app.modules.reviews.application.execute_review import ExecuteReviewRun, ReviewPromptInput
from app.modules.reviews.application.prompt_builder import (
    ChangedFile,
    DiffLine,
    PullRequestMeta,
    RepoConventions,
    ReviewRule,
)

RUN_ID = UUID("00000000-0000-0000-0000-000000000200")


def conventions() -> GeneratedConventions:
    return GeneratedConventions(
        CachedConventions(
            repository_id=RUN_ID,
            agents_md_sha=None,
            prompt_version_id=RUN_ID,
            key_patterns=("Use services.",),
            recommendations=("Test paths.",),
            languages={},
            draft_files=(),
        ),
        None,
        True,
    )


def test_execute_review_run_builds_prompt_then_publishes_model_output() -> None:
    calls: list[str] = []

    class Processor:
        async def prepare(self, run_id: UUID) -> GeneratedConventions:
            assert run_id == RUN_ID
            calls.append("diff-and-conventions")
            return conventions()

    class Repository:
        async def get_review_prompt_input(
            self, run_id: UUID, saved: GeneratedConventions
        ) -> ReviewPromptInput:
            assert run_id == RUN_ID
            assert saved == conventions()
            calls.append("stored-context")
            return ReviewPromptInput(
                system="Review carefully.",
                rules=(ReviewRule("No print", ("app/**",), (), ("Do not print.",)),),
                agents_md=None,
                conventions=RepoConventions(("Use services.",), ("Test paths.",)),
                changed_files=(
                    ChangedFile("app/main.py", "modified", (DiffLine(4, "added", "print('x')"),)),
                ),
                omitted_files=(),
            )

    class Model:
        async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
            assert run_id == RUN_ID
            calls.append("provider-meta")
            return PullRequestMeta(
                "Title", None, "author", "feature", "main", (), 1, 1, 0, False, False
            )

        async def draft_review(self, *, prompt: str) -> dict[str, object]:
            assert "<changed_files>" in prompt
            assert "print('x')" in prompt
            calls.append("model")
            return {"summary": "raw"}

    class Publisher:
        async def execute(self, run_id: UUID, raw_output: dict[str, object]) -> bool:
            assert run_id == RUN_ID
            assert raw_output == {"summary": "raw"}
            calls.append("publish")
            return True

    pipeline = ExecuteReviewRun(Processor(), Repository(), Model(), Publisher())  # type: ignore[arg-type]

    assert asyncio.run(pipeline.execute(RUN_ID)) is True
    assert calls == ["diff-and-conventions", "stored-context", "provider-meta", "model", "publish"]


def test_execute_review_run_does_not_call_model_when_processing_fails() -> None:
    class Processor:
        async def prepare(self, run_id: UUID) -> None:
            return None

    class Repository:
        async def get_review_prompt_input(self, run_id: UUID, saved: GeneratedConventions) -> None:
            raise AssertionError("no prompt read after failed processing")

    class Model:
        async def get_pull_request_meta(self, run_id: UUID) -> None:
            raise AssertionError("no provider call after failed processing")

        async def draft_review(self, *, prompt: str) -> dict[str, object]:
            raise AssertionError("no provider call after failed processing")

    class Publisher:
        async def execute(self, run_id: UUID, raw_output: dict[str, object]) -> bool:
            raise AssertionError("no publication after failed processing")

    pipeline = ExecuteReviewRun(Processor(), Repository(), Model(), Publisher())  # type: ignore[arg-type]

    assert asyncio.run(pipeline.execute(RUN_ID)) is False
