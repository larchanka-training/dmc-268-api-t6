"""Fakes of the snapshot-path pipeline around the verifier: ``ExecuteReviewRun`` and its store."""

from __future__ import annotations

from dataclasses import replace
from types import TracebackType
from typing import Any
from uuid import UUID

from app.modules.reviews.application.conventions import CachedConventions, GeneratedConventions
from app.modules.reviews.application.execute_review import ReviewPromptInput
from app.modules.reviews.application.policy_snapshot import (
    POLICY_SNAPSHOT_SCHEMA_VERSION,
    PolicySnapshot,
)
from app.modules.reviews.application.prompt_builder import (
    ChangedFile,
    DiffLine,
    PullRequestMeta,
    RepoConventions,
    ReviewContext,
    file_block_id,
)
from tests.policy_snapshot_fakes import (
    make_complete_execution_profile,
    make_complete_prompt_bindings,
    make_policy_document_missing,
    make_policy_document_present,
)

RUN_ID = UUID("00000000-0000-0000-0000-000000013901")
MAIN_BLOCK_ID = file_block_id("app/main.py")
SUMMARY = {"problem": "A bug.", "done_well": "Small change.", "effort": "small"}


def verifier_snapshot() -> PolicySnapshot:
    profile = make_complete_execution_profile(
        "fast", review_model="model-a", verifier_model="model-b"
    )
    return PolicySnapshot(
        schema_version=POLICY_SNAPSHOT_SCHEMA_VERSION,
        snapshot_id=UUID("00000000-0000-0000-0000-000000013903"),
        run_id=RUN_ID,
        repository_id=UUID("00000000-0000-0000-0000-000000013904"),
        policy_base_sha="b" * 40,
        rules=make_policy_document_present(
            path=".review/rules.md", text="# Rules\nAlways sanitize input.\n", blob_oid="r" * 40
        ),
        agents=make_policy_document_missing("AGENTS.md"),
        ignore_patterns=(),
        ignore_digest="0" * 64,
        parser_profile="rules-markdown/v1",
        parser_version="4.0.0",
        matcher_version="review-ignore/v1",
        limits_version="policy-limits/v1",
        builtin_exclusions_version="builtin-exclusions/v1",
        priority_matcher_version="priority-matcher/v1",
        service_defaults_rule_version_id=UUID("00000000-0000-0000-0000-000000013905"),
        defaults_checksum="282d61c2da897152046db742fbd9bfda061ba1c71ce32759d47e042c06b0ff52",
        prompt_bindings=make_complete_prompt_bindings(),
        execution_profile=replace(profile, max_input_tokens=60_000, max_policy_tokens=12_000),
        policy_digest="3" * 64,
    )


class FakeProcessor:
    async def prepare(self, run_id: UUID) -> GeneratedConventions:
        return GeneratedConventions(
            CachedConventions(
                repository_id=RUN_ID,
                agents_md_sha=None,
                prompt_version_id=RUN_ID,
                key_patterns=("Use services.",),
                recommendations=("Test paths.",),
                languages={},
            ),
            None,
            True,
        )


class FakeRepository:
    async def get_review_prompt_input(
        self, run_id: UUID, saved: GeneratedConventions
    ) -> ReviewPromptInput:
        return ReviewPromptInput(
            system="Review carefully.",
            rules=(),
            agents_md=None,
            conventions=RepoConventions((), ()),
            changed_files=(
                ChangedFile(
                    "app/main.py",
                    "modified",
                    (DiffLine(10, "added", "x = 1"), DiffLine(11, "added", "y = 2")),
                ),
            ),
            omitted_files=(),
            policy_snapshot=verifier_snapshot(),
        )


class FakeModel:
    def __init__(self, findings: list[dict[str, Any]]) -> None:
        self._findings = findings

    async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
        return PullRequestMeta("Title", None, "u", "b", "m", (), 1, 1, 0, False, False)

    async def draft_review(self, *, context: ReviewContext) -> dict[str, Any]:
        return {"findings": self._findings, "summary": SUMMARY}


class FakePublisher:
    def __init__(self) -> None:
        self.outputs: list[Any] = []

    async def execute(self, run_id: UUID, raw_output: Any) -> bool:
        self.outputs.append(raw_output)
        return True


class FakeUnitOfWork:
    def __init__(self, reviews: Any) -> None:
        self.reviews = reviews

    async def __aenter__(self) -> FakeUnitOfWork:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        pass

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass


class FakeQueue:
    async def publish_review(self, pointer: Any) -> None:
        pass
