"""Fakes of the snapshot-path pipeline around the verifier: ``ExecuteReviewRun`` and its store."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from types import TracebackType
from typing import Any
from uuid import UUID

from app.modules.reviews.application.ast_symbols import (
    SuggestionValidationRequest,
    SuggestionValidationResult,
)
from app.modules.reviews.application.conventions import CachedConventions, GeneratedConventions
from app.modules.reviews.application.execute_review import ReviewPromptInput
from app.modules.reviews.application.findings_post_processor import ProcessedReviewOutput
from app.modules.reviews.application.get_run_diff import DiffSnapshot
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
from app.modules.reviews.application.review_output import (
    FindingPostProcessingInput,
    ReviewOutput,
    ReviewPublication,
)
from app.modules.reviews.application.run_coverage import snapshot_vcs_file
from app.modules.reviews.application.store_review_output import PublishingGuard
from app.modules.reviews.infrastructure.suggestion_ast import validate_suggestion_tree
from tests.findings_post_processor_fakes import _diff_map_from_vcs
from tests.policy_snapshot_fakes import (
    make_complete_execution_profile,
    make_complete_prompt_bindings,
    make_policy_document_missing,
    make_policy_document_present,
)

RUN_ID = UUID("00000000-0000-0000-0000-000000013901")
WORKER_ID = "test-worker"
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


class NullSuggestionParser:
    """No-op ``SuggestionParser`` double for pipeline tests not asserting AST filtering."""

    async def validate_suggestions(
        self, requests: tuple[SuggestionValidationRequest, ...]
    ) -> dict[str, SuggestionValidationResult]:
        return {req.key: SuggestionValidationResult(is_valid=True) for req in requests}

    async def aclose(self) -> None:
        return None


class InProcessSuggestionParser:
    """In-process ``SuggestionParser`` evaluating suggestions with Tree-sitter and AST.

    Accepts an optional hook ``on_validate(requests)`` called before parsing,
    e.g. to assert UoW session closure or connection pool checkout state.
    """

    def __init__(
        self,
        on_validate: Callable[[tuple[SuggestionValidationRequest, ...]], None] | None = None,
    ) -> None:
        self._on_validate = on_validate
        self.seen_requests: list[SuggestionValidationRequest] = []
        self.closed = False

    async def validate_suggestions(
        self, requests: tuple[SuggestionValidationRequest, ...]
    ) -> dict[str, SuggestionValidationResult]:
        if self._on_validate is not None:
            self._on_validate(requests)
        self.seen_requests.extend(requests)
        return {
            req.key: validate_suggestion_tree(
                req.language, req.source, req.start_line, req.line, req.suggestion
            )
            for req in requests
        }

    async def aclose(self) -> None:
        self.closed = True


class FakeUnitOfWork:
    def __init__(self, reviews: Any) -> None:
        self.reviews = reviews

    @property
    def blob_cache(self) -> None:
        return None

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
    def __init__(self) -> None:
        self.pointers: list[Any] = []

    async def publish_review(self, pointer: Any) -> None:
        self.pointers.append(pointer)


class FakePublishingRepository:
    """``StoreReviewOutput``'s repository for the run of ``FakeRepository``: ``app/main.py``
    with lines 10 and 11 added, held by ``WORKER_ID``. It keeps what the store wrote."""

    def __init__(self) -> None:
        self.stored: list[ProcessedReviewOutput] = []
        self.entered: list[dict[str, Any]] = []

    async def get_post_processing_input(self, run_id: UUID) -> FindingPostProcessingInput:
        patch = "@@ -9,0 +10,2 @@\n+x = 1\n+y = 2"
        main = DiffSnapshot("app/main.py", patch, "d" * 40, review_patch=patch)
        return FindingPostProcessingInput(
            diff_map=_diff_map_from_vcs(snapshot_vcs_file(main)),
            checked_paths=frozenset({"app/main.py"}),
            rule_names=frozenset(),
            allowed_v2_rules=frozenset(),
        )

    async def lock_for_publishing(self, run_id: UUID) -> PublishingGuard:
        return PublishingGuard(
            state="running",
            worker_id=WORKER_ID,
            cancel_requested=False,
            head_sha="c" * 40,
            head_current=True,
            repository_review_event="APPROVE",
        )

    async def store_review_output(
        self,
        run_id: UUID,
        answer: dict[str, Any] | None,
        parsed: ReviewOutput,
        processed: ProcessedReviewOutput,
    ) -> ReviewPublication:
        self.stored.append(processed)
        return ReviewPublication(head_sha="c" * 40, findings=(), review_body=processed.review_body)

    async def enter_publishing(self, run_id: UUID, **kwargs: Any) -> None:
        self.entered.append(kwargs)
