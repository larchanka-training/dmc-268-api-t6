"""Snapshot review evidence and policy behavior."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from dataclasses import replace
from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest

from app.modules.reviews.application.conventions import GeneratedConventions
from app.modules.reviews.application.execute_review import (
    ExecuteReviewRun,
    ReviewPromptInput,
    ReviewReply,
)
from app.modules.reviews.application.policy_errors import (
    PolicyError,
    PolicyErrorCode,
)
from app.modules.reviews.application.policy_snapshot import (
    CallManifest,
    ManifestBlock,
    PolicySnapshot,
    SourceRole,
)
from app.modules.reviews.application.prompt_builder import (
    ChangedFile,
    DiffLine,
    PromptBuilder,
    PullRequestMeta,
    RepoConventions,
    ReviewContext,
    ReviewRule,
    file_block_id,
)
from app.modules.reviews.application.reflection_pass import (
    ReflectionPass,
)
from app.modules.reviews.application.review_context import ReviewContextPayload
from app.modules.reviews.application.review_output import (
    EvidenceRef,
    ReviewFindingV2,
    VerifiedReviewOutput,
)
from app.modules.reviews.application.review_path import ReviewBinding
from app.modules.reviews.application.run_failures import RunFailure
from app.modules.reviews.application.verdict import SeverityThresholdValue, severity_counts
from tests.execute_review_fakes import (
    MAIN_BLOCK_ID,
    RUN_ID,
    FakeVerifierModel,
    _AcceptingCheckpointPublisher,
    _make_test_snapshot,
    conventions,
)
from tests.policy_snapshot_fakes import (
    make_policy_document_present,
)


def test_execute_review_empty_after_filters_verifier_call_count_zero() -> None:
    call_count = 0

    class Processor:
        async def prepare(self, run_id: UUID) -> GeneratedConventions:
            return conventions()

    class Repository:
        async def get_review_prompt_input(
            self, run_id: UUID, saved: GeneratedConventions
        ) -> ReviewPromptInput:
            return ReviewPromptInput(
                system="Review carefully.",
                rules=(),
                agents_md=None,
                conventions=RepoConventions((), ()),
                changed_files=(
                    ChangedFile("app/main.py", "modified", (DiffLine(10, "added", "x = 1"),)),
                ),
                omitted_files=(),
                policy_snapshot=_make_test_snapshot(),
            )

    class Model:
        async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
            return PullRequestMeta(
                "PR", None, "a", "f", "main", (), 1, 1, 0, False, False, head_sha="head_123"
            )

        async def draft_review(self, *, context: ReviewContext) -> dict[str, Any]:
            return {
                "findings": [
                    {
                        "path": "app/unknown.py",
                        "line": 10,
                        "start_line": None,
                        "severity": "critical",
                        "category": "security",
                        "title": "Bad path",
                        "body": "Does not exist",
                        "suggestion": None,
                        "confidence": 0.95,
                        "rule_name": None,
                        "evidence_refs": [
                            {
                                "block_id": file_block_id("app/main.py"),
                                "start_line": 10,
                                "end_line": 10,
                            }
                        ],
                    }
                ],
                "summary": {"problem": "p", "done_well": "d", "effort": "none"},
            }

    class Publisher(_AcceptingCheckpointPublisher):
        async def execute(self, run_id: UUID, raw_output: Any) -> bool:
            return True

    class TrackingVerifierModel:
        async def verify(
            self, *, system_prompt: str, user_prompt: str, **_: object
        ) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            return {"decisions": []}

    pipeline = ExecuteReviewRun(
        Processor(),
        Repository(),
        Model(),
        Publisher(),
        reflection_pass=ReflectionPass(TrackingVerifierModel()),
    )
    success = asyncio.run(pipeline.execute(RUN_ID))
    assert success is True
    assert call_count == 0


def test_execute_review_retrieved_excerpt_accepted_by_reviewer_and_verifier() -> None:
    published: dict[str, Any] = {}

    class Processor:
        async def prepare(self, run_id: UUID) -> GeneratedConventions:
            return conventions()

    class Repository:
        async def get_review_prompt_input(
            self, run_id: UUID, saved: GeneratedConventions
        ) -> ReviewPromptInput:
            return ReviewPromptInput(
                system="Review carefully.",
                rules=(),
                agents_md=None,
                conventions=RepoConventions((), ()),
                changed_files=(
                    ChangedFile("app/main.py", "modified", (DiffLine(10, "added", "x = 1"),)),
                ),
                omitted_files=(),
                policy_snapshot=_make_test_snapshot(),
            )

    class Model:
        async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
            return PullRequestMeta(
                "PR", None, "a", "f", "main", (), 1, 1, 0, False, False, head_sha="head_123"
            )

        async def draft_review(self, *, context: ReviewContext) -> dict[str, Any]:
            return {
                "findings": [
                    {
                        "path": "app/main.py",
                        "line": 10,
                        "start_line": None,
                        "severity": "high",
                        "category": "security",
                        "title": "Retrieved excerpt finding",
                        "body": "Cross-reference check",
                        "suggestion": None,
                        "confidence": 0.9,
                        "rule_name": None,
                        "evidence_refs": [
                            {"block_id": "retrieved:app/helper.py", "start_line": 1, "end_line": 5}
                        ],
                    }
                ],
                "summary": {"problem": "p", "done_well": "d", "effort": "none"},
            }

    class Publisher(_AcceptingCheckpointPublisher):
        async def execute(self, run_id: UUID, raw_output: Any) -> bool:
            nonlocal published
            published = dict(raw_output)
            return True

    # Inject retrieved_excerpt block into reviewer manifest build
    orig_build_manifest = PromptBuilder.build_manifest

    def patched_build_manifest(self: Any, context: Any, **kwargs: Any) -> CallManifest:
        manifest = orig_build_manifest(self, context, **kwargs)
        extra_block = ManifestBlock(
            block_id="retrieved:app/helper.py",
            source_role=SourceRole.RETRIEVED_EXCERPT,
            source_kind="source_code",
            path="app/helper.py",
            start_line=1,
            end_line=5,
            shown_line_count=5,
            shown_ranges=((1, 5),),
            revision="head_123",
            side="RIGHT",
            # A RAG block names the run's repository (D7, #144).
            repository_id=context.policy_snapshot.repository_id,
        )
        return CallManifest(
            call_id=manifest.call_id,
            run_id=manifest.run_id,
            stage=manifest.stage,
            recovery_kind=manifest.recovery_kind,
            blocks=(*manifest.blocks, extra_block),
        )

    class DynamicVerifierModel:
        async def verify(
            self, *, system_prompt: str, user_prompt: str, **_: object
        ) -> dict[str, Any]:
            match = re.search(r'candidate_id="([^"]+)"', user_prompt)
            cand_id = match.group(1) if match else "dummy"
            return {
                "decisions": [
                    {
                        "candidate_id": cand_id,
                        "status": "supported",
                        "reason": "Verified via retrieved excerpt",
                        "evidence_refs": [
                            {"block_id": "retrieved:app/helper.py", "start_line": 1, "end_line": 5}
                        ],
                        "requirement_refs": [],
                    }
                ]
            }

    with patch.object(PromptBuilder, "build_manifest", patched_build_manifest):
        pipeline = ExecuteReviewRun(
            Processor(),
            Repository(),
            Model(),
            Publisher(),
            reflection_pass=ReflectionPass(DynamicVerifierModel()),
        )
        success = asyncio.run(pipeline.execute(RUN_ID))
        assert success is True
        assert len(published["findings"]) == 1
        assert published["findings"][0]["title"] == "Retrieved excerpt finding"


def test_execute_review_drops_evidence_source_disallowed_and_revision_mismatch() -> None:
    published: Any = {}
    verifier_received: list[str] = []

    class Processor:
        async def prepare(self, run_id: UUID) -> GeneratedConventions:
            return conventions()

    class Repository:
        async def get_review_prompt_input(
            self, run_id: UUID, saved: GeneratedConventions
        ) -> ReviewPromptInput:
            line = DiffLine(10, "added", "x = 1")
            return ReviewPromptInput(
                system="Review.",
                rules=(),
                agents_md=None,
                conventions=RepoConventions((), ()),
                changed_files=(ChangedFile("app/main.py", "modified", (line,)),),
                omitted_files=(),
                policy_snapshot=_make_test_snapshot(),
            )

    class Model:
        async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
            return PullRequestMeta(
                "Title",
                None,
                "author",
                "feature",
                "main",
                (),
                1,
                1,
                0,
                False,
                False,
                head_sha="sha_head",
                diff_old_sha=None,
            )

        async def draft_review(self, *, context: ReviewContext) -> dict[str, Any]:
            q_id = file_block_id("app/main.py")
            return {
                "findings": [
                    # 1. Attributes service-defaults, but no defaults in manifest
                    {
                        "path": "app/main.py",
                        "line": 10,
                        "start_line": None,
                        "severity": "high",
                        "category": "correctness",
                        "title": "Disallowed rule attribution",
                        "body": "Attributes service defaults without defaults block",
                        "suggestion": None,
                        "confidence": 0.9,
                        "rule_name": "service-defaults:N1",
                        "evidence_refs": [{"block_id": q_id, "start_line": 10, "end_line": 10}],
                    },
                    # 2. Evidence block has mismatched revision on LEFT side
                    {
                        "path": "app/main.py",
                        "line": 10,
                        "start_line": None,
                        "severity": "high",
                        "category": "correctness",
                        "title": "Mismatched revision",
                        "body": "LEFT block has wrong sha",
                        "suggestion": None,
                        "confidence": 0.9,
                        "rule_name": None,
                        "evidence_refs": [
                            {"block_id": "left_block", "start_line": 10, "end_line": 10}
                        ],
                    },
                    # 3. Valid candidate
                    {
                        "path": "app/main.py",
                        "line": 10,
                        "start_line": None,
                        "severity": "critical",
                        "category": "security",
                        "title": "Valid candidate",
                        "body": "Valid body",
                        "suggestion": None,
                        "confidence": 0.95,
                        "rule_name": None,
                        "evidence_refs": [{"block_id": q_id, "start_line": 10, "end_line": 10}],
                    },
                ],
                "summary": {"problem": "p", "done_well": "d", "effort": "none"},
            }

    class Publisher(_AcceptingCheckpointPublisher):
        async def execute(self, run_id: UUID, raw_output: Any) -> bool:
            nonlocal published
            published = raw_output
            return True

    def patched_build_manifest(
        self: Any,
        context: ReviewContext,
        *,
        call_id: UUID,
        stage: Any = "review",
        recovery_kind: Any = "none",
        counter: Any = None,
    ) -> CallManifest:
        manifest = orig_build_manifest(
            self,
            context,
            call_id=call_id,
            stage=stage,
            recovery_kind=recovery_kind,
            counter=counter,
        )
        if stage != "review":
            return manifest
        # Add a LEFT block with wrong revision to test revision_mismatch
        left_block = ManifestBlock(
            block_id="left_block",
            source_role=SourceRole.DIFF,
            source_kind="source_code",
            path="app/main.py",
            start_line=10,
            end_line=10,
            shown_line_count=1,
            shown_ranges=((10, 10),),
            revision="wrong_sha",
            side="LEFT",
        )
        return CallManifest(
            call_id=manifest.call_id,
            run_id=manifest.run_id,
            stage=manifest.stage,
            recovery_kind=manifest.recovery_kind,
            blocks=(*manifest.blocks, left_block),
        )

    orig_build_manifest = PromptBuilder.build_manifest

    class VerifierModel:
        async def verify(
            self, *, system_prompt: str, user_prompt: str, **_: object
        ) -> dict[str, Any]:
            cand_ids = re.findall(r'candidate_id="([^"]+)"', user_prompt)
            verifier_received.extend(cand_ids)
            return {
                "decisions": [
                    {
                        "candidate_id": cid,
                        "status": "supported",
                        "reason": "OK",
                        "evidence_refs": [
                            {
                                "block_id": file_block_id("app/main.py"),
                                "start_line": 10,
                                "end_line": 10,
                            }
                        ],
                        "requirement_refs": [],
                    }
                    for cid in cand_ids
                ]
            }

    with (
        patch.object(PromptBuilder, "build_manifest", patched_build_manifest),
    ):
        pipeline = ExecuteReviewRun(
            Processor(),
            Repository(),
            Model(),
            Publisher(),
            reflection_pass=ReflectionPass(VerifierModel()),
        )
        success = asyncio.run(pipeline.execute(RUN_ID))
        assert success is True
        assert len(verifier_received) == 1
        assert len(published["findings"]) == 1
        assert len(published.dropped_findings) == 2
        reasons = {d.drop_reason for d in published.dropped_findings}
        assert "evidence_source_disallowed" in reasons
        assert "revision_mismatch" in reasons


def test_execute_review_drops_attribution_to_a_whitespace_only_agents_document() -> None:
    # The prompt shows the blank lines of a whitespace-only AGENTS.md, so its manifest block
    # has lines; only the run's snapshot, passed by the use case, says it is empty (#130).
    published: Any = {}
    verifier_received: list[str] = []
    snapshot = replace(
        _make_test_snapshot(),
        agents=make_policy_document_present(path="AGENTS.md", text="  \n\t\n"),
    )

    class Processor:
        async def prepare(self, run_id: UUID) -> GeneratedConventions:
            return conventions()

    class Repository:
        async def get_review_prompt_input(
            self, run_id: UUID, saved: GeneratedConventions
        ) -> ReviewPromptInput:
            line = DiffLine(10, "added", "x = 1")
            return ReviewPromptInput(
                system="Review.",
                rules=(),
                agents_md=None,
                conventions=RepoConventions((), ()),
                changed_files=(ChangedFile("app/main.py", "modified", (line,)),),
                omitted_files=(),
                policy_snapshot=snapshot,
            )

    class Model:
        async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
            return PullRequestMeta(
                "Title", None, "author", "feature", "main", (), 1, 1, 0, False, False
            )

        async def draft_review(self, *, context: ReviewContext) -> dict[str, Any]:
            q_id = file_block_id("app/main.py")
            finding = {
                "path": "app/main.py",
                "line": 10,
                "start_line": None,
                "severity": "high",
                "category": "correctness",
                "suggestion": None,
                "confidence": 0.9,
                "evidence_refs": [{"block_id": q_id, "start_line": 10, "end_line": 10}],
            }
            return {
                "findings": [
                    {
                        **finding,
                        "title": "Cites a blank AGENTS.md",
                        "body": "Attributed to a document with no content",
                        "rule_name": "AGENTS.md",
                    },
                    {
                        **finding,
                        "title": "Valid candidate",
                        "body": "Valid body",
                        "rule_name": None,
                    },
                ],
                "summary": {"problem": "p", "done_well": "d", "effort": "none"},
            }

    class Publisher(_AcceptingCheckpointPublisher):
        async def execute(self, run_id: UUID, raw_output: Any) -> bool:
            nonlocal published
            published = raw_output
            return True

    class VerifierModel:
        async def verify(
            self, *, system_prompt: str, user_prompt: str, **_: object
        ) -> dict[str, Any]:
            cand_ids = re.findall(r'candidate_id="([^"]+)"', user_prompt)
            verifier_received.extend(cand_ids)
            return {
                "decisions": [
                    {
                        "candidate_id": cid,
                        "status": "supported",
                        "reason": "OK",
                        "evidence_refs": [
                            {
                                "block_id": file_block_id("app/main.py"),
                                "start_line": 10,
                                "end_line": 10,
                            }
                        ],
                        # Cites the shown blank AGENTS.md line, so a candidate that
                        # reached the verifier with that attribution would be confirmed.
                        "requirement_refs": [
                            {
                                "block_id": "policy-agents",
                                "start_line": 1,
                                "end_line": 1,
                                "check_id": None,
                            }
                        ],
                    }
                    for cid in cand_ids
                ]
            }

    pipeline = ExecuteReviewRun(
        Processor(),
        Repository(),
        Model(),
        Publisher(),
        reflection_pass=ReflectionPass(VerifierModel()),
    )

    assert asyncio.run(pipeline.execute(RUN_ID)) is True
    assert len(verifier_received) == 1
    assert [f["title"] for f in published["findings"]] == ["Valid candidate"]
    assert [(d.finding.rule_name, d.drop_reason) for d in published.dropped_findings] == [
        ("AGENTS.md", "evidence_source_disallowed")
    ]


def test_execute_review_run_multiple_files_snapshot_fits_without_overflow() -> None:
    class Processor:
        async def prepare(self, run_id: UUID) -> GeneratedConventions:
            return conventions()

    class Repository:
        async def get_review_prompt_input(
            self, run_id: UUID, saved: GeneratedConventions
        ) -> ReviewPromptInput:
            line_content = "x = 100 + 200 # line calculation " + "y" * 55
            lines = tuple(DiffLine(i, "added", line_content) for i in range(1, 801))
            files = tuple(ChangedFile(f"app/module_{idx}.py", "added", lines) for idx in range(3))
            return ReviewPromptInput(
                system="Review carefully.",
                rules=(),
                agents_md=None,
                conventions=RepoConventions(("Use services.",), ("Test paths.",)),
                changed_files=files,
                omitted_files=(),
                policy_snapshot=_make_test_snapshot(),
            )

    class Model:
        async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
            return PullRequestMeta(
                "Large PR",
                None,
                "author",
                "feature",
                "main",
                (),
                3,
                2400,
                0,
                False,
                False,
                head_sha="sha_head",
            )

        async def draft_review(self, *, context: ReviewContext) -> dict[str, Any]:
            return {
                "findings": [],
                "summary": {
                    "problem": "Clean code.",
                    "done_well": "Well written.",
                    "effort": "none",
                },
            }

    published_output: dict[str, Any] = {}

    class Publisher(_AcceptingCheckpointPublisher):
        async def execute(self, run_id: UUID, raw_output: Any) -> bool:
            nonlocal published_output
            published_output = dict(raw_output) if isinstance(raw_output, Mapping) else {}
            return True

    pipeline = ExecuteReviewRun(
        Processor(),
        Repository(),
        Model(),
        Publisher(),
        reflection_pass=ReflectionPass(FakeVerifierModel({"decisions": []})),
    )
    success = asyncio.run(pipeline.execute(RUN_ID))
    assert success is True
    assert published_output["findings"] == []


def test_execute_review_verifier_mandatory_budget_exceeded_aborts_before_draft_review() -> None:
    # max_input_tokens=8_500 -> send_budget = 8_500 - 8_000 = 500.
    # Verifier mandatory envelope (~1,100 tokens) > 500 while reviewer prompt (~350 tokens) fits.
    snapshot = _make_test_snapshot(max_input_tokens=8_500, max_policy_tokens=12_000)

    class Processor:
        async def prepare(self, run_id: UUID) -> GeneratedConventions:
            return conventions()

    class Repository:
        async def get_review_prompt_input(
            self, run_id: UUID, saved: GeneratedConventions
        ) -> ReviewPromptInput:
            return ReviewPromptInput(
                system="Review carefully.",
                rules=(),
                agents_md=None,
                conventions=RepoConventions((), ()),
                changed_files=(
                    ChangedFile("app/main.py", "modified", (DiffLine(1, "added", "x = 1"),)),
                ),
                omitted_files=(),
                policy_snapshot=snapshot,
            )

    draft_calls = 0

    class Model:
        async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
            return PullRequestMeta("Title", None, "u", "b", "m", (), 1, 1, 0, False, False)

        async def draft_review(self, *, context: ReviewContext) -> dict[str, Any]:
            nonlocal draft_calls
            draft_calls += 1
            return {"findings": [], "summary": {"problem": "p", "done_well": "d", "effort": "none"}}

    class Publisher(_AcceptingCheckpointPublisher):
        async def execute(self, run_id: UUID, raw_output: Any) -> bool:
            return True

    pipeline = ExecuteReviewRun(
        Processor(),
        Repository(),
        Model(),
        Publisher(),
        reflection_pass=ReflectionPass(FakeVerifierModel({"decisions": []})),
    )
    with pytest.raises(PolicyError) as exc_info:
        asyncio.run(pipeline.execute(RUN_ID))
    assert exc_info.value.code == PolicyErrorCode.PROMPT_BUDGET_EXCEEDED
    assert draft_calls == 0


def test_execute_review_counter_version_and_diff_old_sha_propagation() -> None:
    from dataclasses import replace

    # 39,390-char rules file: 11,965 tokens under counter/v1 (passes 12,000 limit),
    # 15,953 tokens under counter/v2 (exceeds 12,000 limit).
    lines = ["x" * 97 + "\n"] * 210 + ["x" * 98 + "\n"] * 190
    rules_content = "".join(lines)
    base_snapshot = _make_test_snapshot(max_policy_tokens=12_000)
    snapshot_v1 = replace(
        base_snapshot,
        rules=make_policy_document_present(".review/rules.md", rules_content, blob_oid="r" * 40),
        execution_profile=replace(base_snapshot.execution_profile, counter_version="counter/v1"),
    )

    captured_diff_old_sha: list[str | None] = []

    class ContextFilterWithOldSha:
        diff_old_sha = "old_base_sha_777"
        head_sha: str | None = None
        snapshot_for_llm: PolicySnapshot | None = None
        payload: ReviewContextPayload | None = None
        ignored_file_paths: frozenset[str] = frozenset()

        def excluded(self, path: str) -> bool:
            return False

    class Processor:
        async def prepare(self, run_id: UUID) -> GeneratedConventions:
            return conventions()

    class RepositoryV1:
        async def get_review_prompt_input(
            self, run_id: UUID, saved: GeneratedConventions
        ) -> ReviewPromptInput:
            return ReviewPromptInput(
                system="Review carefully.",
                rules=(),
                agents_md=None,
                conventions=RepoConventions((), ()),
                changed_files=(
                    ChangedFile("app/main.py", "modified", (DiffLine(1, "added", "x = 1"),)),
                ),
                omitted_files=(),
                policy_snapshot=snapshot_v1,
            )

    class Model:
        async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
            return PullRequestMeta(
                "Title",
                None,
                "u",
                "b",
                "m",
                (),
                1,
                1,
                0,
                False,
                False,
                head_sha="head_sha_888",
            )

        async def draft_review(self, *, context: ReviewContext) -> dict[str, Any]:
            captured_diff_old_sha.append(context.pr_meta.diff_old_sha)
            return {"findings": [], "summary": {"problem": "p", "done_well": "d", "effort": "none"}}

    class Publisher(_AcceptingCheckpointPublisher):
        async def execute(self, run_id: UUID, raw_output: Any) -> bool:
            return True

    pipeline = ExecuteReviewRun(
        Processor(),
        RepositoryV1(),
        Model(),
        Publisher(),
        context_filter=ContextFilterWithOldSha(),
        reflection_pass=ReflectionPass(FakeVerifierModel({"decisions": []})),
    )
    assert asyncio.run(pipeline.execute(RUN_ID)) is True
    assert captured_diff_old_sha == ["old_base_sha_777"]

    # Unknown counter version fails closed with SNAPSHOT_INTEGRITY_ERROR
    snapshot_bad_counter = replace(
        snapshot_v1,
        execution_profile=replace(snapshot_v1.execution_profile, counter_version="estimated/v2"),
    )

    class RepositoryBadCounter:
        async def get_review_prompt_input(
            self, run_id: UUID, saved: GeneratedConventions
        ) -> ReviewPromptInput:
            return ReviewPromptInput(
                system="Review carefully.",
                rules=(),
                agents_md=None,
                conventions=RepoConventions((), ()),
                changed_files=(
                    ChangedFile("app/main.py", "modified", (DiffLine(1, "added", "x = 1"),)),
                ),
                omitted_files=(),
                policy_snapshot=snapshot_bad_counter,
            )

    pipeline_bad = ExecuteReviewRun(
        Processor(),
        RepositoryBadCounter(),
        Model(),
        Publisher(),
        reflection_pass=ReflectionPass(FakeVerifierModel({"decisions": []})),
    )
    with pytest.raises(PolicyError) as exc_info:
        asyncio.run(pipeline_bad.execute(RUN_ID))
    assert exc_info.value.code == PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR


# F1-3: the attempt's snapshot reaches the review prompt only on the snapshot path.
_V4_REVIEW = UUID("00000000-0000-0000-0000-000000000041")
_PROVIDER_AGENTS = "Provider AGENTS.md, read outside the snapshot."
_SNAPSHOT_AGENTS = "# Agents\nPrefer small functions.\n"


def _v4_snapshot(review_system_id: UUID = _V4_REVIEW) -> PolicySnapshot:
    from dataclasses import replace

    snapshot = _make_test_snapshot()
    return replace(
        snapshot,
        agents=make_policy_document_present(path="AGENTS.md", text=_SNAPSHOT_AGENTS),
        prompt_bindings=replace(
            snapshot.prompt_bindings,
            review_system_version="v4",
            review_system_id=review_system_id,
        ),
    )


class _StageSnapshot:
    """The context stage as execute_review sees it: exclusions and the attempt's snapshot."""

    diff_old_sha: str | None = None
    head_sha: str | None = None
    payload: ReviewContextPayload | None = None
    ignored_file_paths: frozenset[str] = frozenset()

    def __init__(self, snapshot: PolicySnapshot | None) -> None:
        self.snapshot_for_llm = snapshot

    def excluded(self, path: str) -> bool:
        return False


class _BoundPipeline(_AcceptingCheckpointPublisher):
    """Processor, prompt source, model and publisher of a run bound to ``binding``."""

    def __init__(self, binding: ReviewBinding) -> None:
        self.binding = binding
        self.contexts: list[ReviewContext] = []
        self.published: list[object] = []

    async def prepare(self, run_id: UUID) -> GeneratedConventions:
        return conventions()

    async def get_review_prompt_input(
        self, run_id: UUID, saved: GeneratedConventions
    ) -> ReviewPromptInput:
        return ReviewPromptInput(
            system="Review carefully.",
            rules=(),
            agents_md=_PROVIDER_AGENTS,
            conventions=RepoConventions((), ()),
            changed_files=(
                ChangedFile("app/main.py", "modified", (DiffLine(10, "added", "x = 1"),)),
            ),
            omitted_files=(),
            review_binding=self.binding,
        )

    async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
        return PullRequestMeta("Title", None, "u", "b", "m", (), 1, 1, 0, False, False)

    async def draft_review(self, *, context: ReviewContext) -> dict[str, Any]:
        self.contexts.append(context)
        return {"findings": [], "summary": {"problem": "p", "done_well": "d", "effort": "none"}}

    async def execute(self, run_id: UUID, raw_output: object) -> bool:
        self.published.append(raw_output)
        return True


def _run_bound(
    binding: ReviewBinding,
    stage_snapshot: PolicySnapshot | None,
    *,
    verifier: FakeVerifierModel | None = None,
) -> _BoundPipeline:
    pipeline = _BoundPipeline(binding)
    asyncio.run(
        ExecuteReviewRun(
            pipeline,
            pipeline,
            pipeline,
            pipeline,
            context_filter=_StageSnapshot(stage_snapshot),
            reflection_pass=None if verifier is None else ReflectionPass(verifier),
        ).execute(RUN_ID)
    )
    return pipeline


def test_snapshot_path_review_shows_the_snapshot_documents_not_the_provider_agents_md() -> None:
    snapshot = _v4_snapshot()
    verifier = FakeVerifierModel({"decisions": []})

    pipeline = _run_bound(
        ReviewBinding(_V4_REVIEW, "review.system", "v4", "github"), snapshot, verifier=verifier
    )

    [context] = pipeline.contexts
    assert context.policy_snapshot is snapshot
    user = PromptBuilder().build_prompt(context).user
    assert (
        '<repository_rules block_id="policy-rules" path=".review/rules.md">\n'
        '<line n="1"># Rules</line>\n'
        '<line n="2">Always sanitize input.</line>\n'
        "</repository_rules>"
    ) in user
    assert ('<project_conventions_document block_id="policy-agents" path="AGENTS.md">') in user
    assert "Prefer small functions." in user
    # ReviewPromptInput.agents_md (the provider read) is not shown on this path.
    assert _PROVIDER_AGENTS not in user
    assert "<agents_md>" not in user
    assert verifier.call_count == 0
    assert len(pipeline.published) == 1


def test_snapshot_path_review_without_a_reflection_pass_still_fails_closed() -> None:
    pipeline = _BoundPipeline(ReviewBinding(_V4_REVIEW, "review.system", "v4", "github"))
    run = ExecuteReviewRun(
        pipeline, pipeline, pipeline, pipeline, context_filter=_StageSnapshot(_v4_snapshot())
    )

    with pytest.raises(RunFailure) as raised:
        asyncio.run(run.execute(RUN_ID))

    assert raised.value.error_code == "internal_error"
    assert "Reflection pass is required" in raised.value.message
    assert pipeline.contexts == []
    assert pipeline.published == []


@pytest.mark.parametrize(
    "binding",
    [
        ReviewBinding(_V4_REVIEW, "review.system", "v3", "github"),
        ReviewBinding(_V4_REVIEW, "review.system", "v3", "gitlab"),
    ],
    ids=["v3", "v3-gitlab"],
)
def test_legacy_review_never_gets_the_stage_snapshot(binding: ReviewBinding) -> None:
    # Even a ready snapshot from the stage stays out of a legacy run's prompt.
    pipeline = _run_bound(binding, _v4_snapshot())

    [context] = pipeline.contexts
    assert context.policy_snapshot is None
    user = PromptBuilder().build_prompt(context).user
    assert f"<agents_md>\n{_PROVIDER_AGENTS}\n</agents_md>" in user
    assert "policy-rules" not in user and "Prefer small functions." not in user


def test_stage_snapshot_pinning_another_review_prompt_is_refused_before_the_model() -> None:
    pipeline = _BoundPipeline(ReviewBinding(_V4_REVIEW, "review.system", "v4", "github"))
    other = _v4_snapshot(review_system_id=UUID("00000000-0000-0000-0000-000000000042"))
    run = ExecuteReviewRun(
        pipeline,
        pipeline,
        pipeline,
        pipeline,
        context_filter=_StageSnapshot(other),
        reflection_pass=ReflectionPass(FakeVerifierModel({"decisions": []})),
    )

    with pytest.raises(PolicyError) as raised:
        asyncio.run(run.execute(RUN_ID))

    assert raised.value.code == PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR
    assert "pins another review prompt" in raised.value.message
    assert pipeline.contexts == []


# F1-8 (#130 R12, #247 ч.3): attribution is repaired before the verifier; no verifier call,
# no v2 rule allow-list.


def _f18_finding(line: int, rule_name: str | None, title: str) -> dict[str, Any]:
    return {
        "path": "app/main.py",
        "line": line,
        "start_line": None,
        "severity": "medium",
        "category": "correctness",
        "title": title,
        "body": f"{title} body.",
        "suggestion": None,
        "confidence": 0.9,
        "rule_name": rule_name,
        "evidence_refs": [{"block_id": MAIN_BLOCK_ID, "start_line": line, "end_line": line}],
    }


def _f18_quoted(finding: dict[str, Any]) -> dict[str, Any]:
    """The finding with a claim of a custom instruction the run does not have."""
    return {
        **finding,
        "body": f"According to custom instructions in 'No print' (rules): {finding['body']}",
    }


def _f18_published(output: Mapping[str, object]) -> list[dict[str, Any]]:
    findings = output["findings"]
    assert isinstance(findings, list)
    return findings


class _F18Processor:
    async def prepare(self, run_id: UUID) -> GeneratedConventions:
        return conventions()


class _F18Repository:
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
            policy_snapshot=_make_test_snapshot(),  # rules present, AGENTS.md missing
        )


class _F18Model:
    def __init__(self, findings: list[dict[str, Any]]) -> None:
        self.findings = findings

    async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
        return PullRequestMeta("Title", None, "u", "b", "m", (), 1, 1, 0, False, False)

    async def draft_review(self, *, context: ReviewContext) -> dict[str, Any]:
        return {
            "findings": self.findings,
            "summary": {"problem": "p.", "done_well": "d.", "effort": "none"},
        }


class _F18Publisher(_AcceptingCheckpointPublisher):
    def __init__(self) -> None:
        self.outputs: list[Any] = []

    async def execute(self, run_id: UUID, raw_output: Any) -> bool:
        self.outputs.append(raw_output)
        return True


class _F18Pass:
    """Captures the candidates; supports the ones whose title is in ``supported``."""

    def __init__(self, supported: frozenset[str] = frozenset()) -> None:
        self.supported = supported
        self.seen: list[tuple[str | None, str]] = []

    def max_prompt_tokens(self) -> int | None:
        return None

    async def verify_recorded(self, *, candidates: Any, context: Any) -> Any:
        from app.modules.reviews.application.reflection_pass import (
            VerificationDecision,
            VerificationRecord,
        )

        self.seen.extend((c.finding.rule_name, c.finding.body) for c in candidates)
        decisions = tuple(
            VerificationDecision(
                candidate_id=c.candidate_id,
                status="supported",
                reason="Confirmed on the diff.",
                evidence_refs=[
                    EvidenceRef(
                        block_id=MAIN_BLOCK_ID, start_line=c.finding.line, end_line=c.finding.line
                    )
                ],
                requirement_refs=[],
            )
            if c.finding.title in self.supported
            else VerificationDecision(
                candidate_id=c.candidate_id,
                status="insufficient_context",
                reason="Not shown.",
                evidence_refs=[],
                requirement_refs=[],
            )
            for c in candidates
        )
        return VerificationRecord(decisions, tuple(c.candidate_id for c in candidates))


@pytest.mark.parametrize(
    "findings",
    [[], [_f18_finding(99, None, "Off the diff")]],
    ids=["no-candidates", "all-dropped-before-the-verifier"],
)
def test_without_a_verifier_call_the_output_carries_no_v2_rule_allow_list(
    findings: list[dict[str, Any]],
) -> None:
    from app.modules.reviews.application.review_output import VerifiedReviewOutput

    publisher = _F18Publisher()
    reflection = _F18Pass()
    pipeline = ExecuteReviewRun(
        _F18Processor(),
        _F18Repository(),
        _F18Model(findings),
        publisher,
        reflection_pass=reflection,  # type: ignore[arg-type]
    )

    assert asyncio.run(pipeline.execute(RUN_ID)) is True

    assert reflection.seen == []
    [output] = publisher.outputs
    assert isinstance(output, VerifiedReviewOutput)
    assert _f18_published(output) == []
    assert output.allowed_v2_rules is None


@pytest.mark.parametrize("supported", [True, False], ids=["supported", "rejected"])
def test_a_wrong_attribution_is_repaired_before_the_verifier_decides(supported: bool) -> None:
    from app.modules.reviews.application.review_output import VerifiedReviewOutput

    publisher = _F18Publisher()
    titles = {"Rules finding"} | ({"Invented finding"} if supported else set())
    reflection = _F18Pass(frozenset(titles))
    pipeline = ExecuteReviewRun(
        _F18Processor(),
        _F18Repository(),
        _F18Model(
            [
                _f18_finding(10, ".review/rules.md", "Rules finding"),
                _f18_quoted(_f18_finding(11, None, "Invented finding")),
            ]
        ),
        publisher,
        reflection_pass=reflection,  # type: ignore[arg-type]
    )

    assert asyncio.run(pipeline.execute(RUN_ID)) is True

    # The v2 parse refuses any other rule name, so the wrong attribution is the body's claim
    # of a rule the run does not have: the verifier sees the candidate without it.
    assert reflection.seen == [
        (".review/rules.md", "Rules finding body."),
        (None, "Invented finding body."),
    ]
    [output] = publisher.outputs
    assert isinstance(output, VerifiedReviewOutput)
    assert output.allowed_v2_rules == frozenset({".review/rules.md"})
    published = [(f["title"], f["rule_name"]) for f in _f18_published(output)]
    if supported:
        assert published == [
            ("Rules finding", ".review/rules.md"),
            ("Invented finding", None),
        ]
        assert output.dropped_findings == ()
    else:
        assert published == [("Rules finding", ".review/rules.md")]
        from app.modules.reviews.application.review_output import DroppedCandidateRecord

        [dropped] = output.dropped_findings
        assert isinstance(dropped, DroppedCandidateRecord)
        assert dropped.drop_reason == "insufficient_context"
        assert dropped.position == 1
        assert dropped.finding.rule_name is None  # type: ignore[union-attr]


def _snapshot_publication_candidate(
    title: str, *, rule_name: str | None = None, evidence_line: int = 10
) -> dict[str, Any]:
    return {
        "path": "app/main.py",
        "line": 10,
        "start_line": None,
        "severity": "high",
        "category": "correctness",
        "title": title,
        "body": "This operation can corrupt stored data.",
        "suggestion": None,
        "confidence": 0.9,
        "rule_name": rule_name,
        "evidence_refs": [
            {"block_id": MAIN_BLOCK_ID, "start_line": evidence_line, "end_line": evidence_line}
        ],
    }


def _publish_snapshot_candidates(
    findings: list[dict[str, Any]],
    decisions_by_title: dict[str, dict[str, Any]],
    *,
    snapshot: PolicySnapshot | None = None,
    rules: tuple[ReviewRule, ...] = (),
    severity_threshold: SeverityThresholdValue = "all",
) -> tuple[VerifiedReviewOutput, list[str]]:
    published: list[VerifiedReviewOutput] = []
    verifier_titles: list[str] = []

    class Processor:
        async def prepare(self, run_id: UUID) -> GeneratedConventions:
            return conventions()

    class Repository:
        async def get_review_prompt_input(
            self, run_id: UUID, saved: GeneratedConventions
        ) -> ReviewPromptInput:
            return ReviewPromptInput(
                system="Review carefully.",
                rules=rules,
                agents_md=None,
                conventions=RepoConventions((), ()),
                changed_files=(
                    ChangedFile("app/main.py", "modified", (DiffLine(10, "added", "save(data)"),)),
                ),
                omitted_files=(),
                policy_snapshot=snapshot or _make_test_snapshot(),
                severity_threshold=severity_threshold,
            )

    class Model:
        async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
            return PullRequestMeta(
                "PR",
                None,
                "author",
                "feature",
                "main",
                (),
                1,
                1,
                0,
                False,
                False,
                head_sha="head_216",
            )

        async def draft_review(self, *, context: ReviewContext) -> dict[str, object]:
            return {
                "findings": findings,
                "summary": {
                    "problem": "Unverified claim",
                    "done_well": "Raw praise",
                    "effort": "none",
                },
            }

    class Verifier:
        async def verify(
            self,
            *,
            system_prompt: str,
            user_prompt: str,
            manifest: CallManifest,
            context: ReviewContext,
            **_: object,
        ) -> dict[str, object]:
            candidates = re.findall(
                r'<candidate candidate_id="([^"]+)"[^>]*>\s*<title>([^<]+)</title>', user_prompt
            )
            verifier_titles.extend(title for _, title in candidates)
            return {
                "decisions": [
                    {"candidate_id": candidate_id, **decisions_by_title[title]}
                    for candidate_id, title in candidates
                ]
            }

    class Publisher(_AcceptingCheckpointPublisher):
        async def execute(
            self, run_id: UUID, raw_output: Mapping[str, object] | str | bytes
        ) -> bool:
            assert run_id == RUN_ID
            assert isinstance(raw_output, VerifiedReviewOutput)
            published.append(raw_output)
            return True

    pipeline = ExecuteReviewRun(
        Processor(), Repository(), Model(), Publisher(), reflection_pass=ReflectionPass(Verifier())
    )
    assert asyncio.run(pipeline.execute(RUN_ID)) is True
    assert len(published) == 1
    return published[0], verifier_titles


def test_execute_review_snapshot_never_publishes_insufficient_context() -> None:
    published, verifier_titles = _publish_snapshot_candidates(
        [
            _snapshot_publication_candidate("Confirmed corruption"),
            _snapshot_publication_candidate("Unknown behavior"),
        ],
        {
            "Confirmed corruption": {
                "status": "supported",
                "reason": "The shown operation corrupts data.",
                "evidence_refs": [{"block_id": MAIN_BLOCK_ID, "start_line": 10, "end_line": 10}],
                "requirement_refs": [],
            },
            "Unknown behavior": {
                "status": "insufficient_context",
                "reason": "The helper implementation is absent.",
                "evidence_refs": [],
                "requirement_refs": [],
            },
        },
    )
    assert isinstance(published["findings"], list)
    assert [finding["title"] for finding in published["findings"]] == ["Confirmed corruption"]
    assert len(published.dropped_findings) == 1
    dropped = published.dropped_findings[0]
    assert not isinstance(dropped, Mapping)
    assert isinstance(dropped.finding, ReviewFindingV2)
    assert dropped.finding.title == "Unknown behavior"
    assert dropped.drop_reason == "insufficient_context"
    assert dropped.position == 1
    assert verifier_titles == ["Confirmed corruption", "Unknown behavior"]
    assert published["summary"] == {
        "problem": "Identified 1 verified issue (correctness) in the reviewed changes.",
        "done_well": (
            "Code analysis and policy verification were completed across all evaluated changes."
        ),
        "effort": "medium",
    }


def test_execute_review_snapshot_checks_attribution_after_first_candidate() -> None:
    published, verifier_titles = _publish_snapshot_candidates(
        [
            _snapshot_publication_candidate("Visible evidence"),
            _snapshot_publication_candidate("Unshown evidence", evidence_line=99),
        ],
        {
            title: {
                "status": "supported",
                "reason": "The shown operation corrupts data.",
                "evidence_refs": [{"block_id": MAIN_BLOCK_ID, "start_line": 10, "end_line": 10}],
                "requirement_refs": [],
            }
            for title in ("Visible evidence", "Unshown evidence")
        },
    )
    assert isinstance(published["findings"], list)
    assert [finding["title"] for finding in published["findings"]] == ["Visible evidence"]
    assert len(published.dropped_findings) == 1
    dropped = published.dropped_findings[0]
    assert not isinstance(dropped, Mapping)
    assert isinstance(dropped.finding, ReviewFindingV2)
    assert dropped.finding.title == "Unshown evidence"
    assert dropped.drop_reason == "evidence_not_shown"
    assert dropped.position == 1
    assert verifier_titles == ["Visible evidence"]


@pytest.mark.parametrize(
    "rule_name", [".review/rules.md", "AGENTS.md", "service-defaults:E2", "service-defaults:FSD1"]
)
def test_execute_review_snapshot_preserves_supported_policy_attribution(rule_name: str) -> None:
    snapshot = _make_test_snapshot()
    rules: tuple[ReviewRule, ...] = ()
    if rule_name == ".review/rules.md":
        requirement = {
            "block_id": "policy-rules",
            "start_line": 2,
            "end_line": 2,
            "check_id": None,
        }
    elif rule_name == "AGENTS.md":
        snapshot = replace(
            snapshot,
            agents=make_policy_document_present(
                path="AGENTS.md", text="Preserve stored data.\n", blob_oid="a" * 40
            ),
        )
        requirement = {
            "block_id": "policy-agents",
            "start_line": 1,
            "end_line": 1,
            "check_id": None,
        }
    elif rule_name == "service-defaults:E2":
        rules = (
            ReviewRule(
                "Error Handling Standards",
                ("*.py",),
                (),
                ("Handle failures.", "Never swallow errors.", "Retain causes.", "Report failures."),
            ),
        )
        requirement = {
            "block_id": "service-defaults",
            "start_line": 2,
            "end_line": 2,
            "check_id": "E2",
        }
    else:
        snapshot = replace(
            snapshot,
            defaults_checksum="32673f59b6f0f3f96155e4012dcc42718b0814c473cc255c70e69f63660dcb4a",
        )
        rules = (
            ReviewRule(
                "FSD Layer Boundaries",
                ("*.py",),
                (),
                (
                    "Keep layers separate.",
                    "Check imports.",
                    "Use slices.",
                    "Hide internals.",
                    "Use APIs.",
                ),
            ),
        )
        requirement = {
            "block_id": "service-defaults",
            "start_line": 1,
            "end_line": 1,
            "check_id": "FSD1",
        }
    published, verifier_titles = _publish_snapshot_candidates(
        [_snapshot_publication_candidate("Policy violation", rule_name=rule_name)],
        {
            "Policy violation": {
                "status": "supported",
                "reason": "The operation violates the cited policy.",
                "evidence_refs": [{"block_id": MAIN_BLOCK_ID, "start_line": 10, "end_line": 10}],
                "requirement_refs": [requirement],
            }
        },
        snapshot=snapshot,
        rules=rules,
    )
    assert isinstance(published["findings"], list)
    assert len(published["findings"]) == 1
    assert published["findings"][0]["title"] == "Policy violation"
    assert published["findings"][0]["rule_name"] == rule_name
    if rule_name == "AGENTS.md":
        assert published.allowed_v2_rules == frozenset({".review/rules.md", "AGENTS.md"})
    else:
        assert published.allowed_v2_rules == frozenset({".review/rules.md"})
    assert published.dropped_findings == ()
    assert verifier_titles == ["Policy violation"]


def test_execute_review_snapshot_warning_keeps_supported_low_and_drops_info() -> None:
    low = _snapshot_publication_candidate("Possible corruption")
    low["severity"] = "low"
    info = _snapshot_publication_candidate("Informational risk")
    info["severity"] = "info"
    published, verifier_titles = _publish_snapshot_candidates(
        [low, info],
        {
            title: {
                "status": "supported",
                "reason": "The shown operation demonstrates this issue.",
                "evidence_refs": [{"block_id": MAIN_BLOCK_ID, "start_line": 10, "end_line": 10}],
                "requirement_refs": [],
            }
            for title in ("Possible corruption", "Informational risk")
        },
        severity_threshold="warning",
    )
    assert isinstance(published["findings"], list)
    assert [(item["title"], item["severity"]) for item in published["findings"]] == [
        ("Possible corruption", "low")
    ]
    assert severity_counts(item["severity"] for item in published["findings"]) == {
        "critical": 0,
        "high": 0,
        "medium": 0,
        "low": 1,
        "info": 0,
    }
    assert len(published.dropped_findings) == 1
    dropped = published.dropped_findings[0]
    assert not isinstance(dropped, Mapping)
    assert isinstance(dropped.finding, ReviewFindingV2)
    assert dropped.finding.title == "Informational risk"
    assert dropped.drop_reason == "severity_threshold"
    assert dropped.position == 1
    assert published.severity_threshold == "warning"
    assert verifier_titles == ["Possible corruption", "Informational risk"]
    assert published["summary"] == {
        "problem": "Identified 1 verified issue (correctness) in the reviewed changes.",
        "done_well": (
            "Code analysis and policy verification were completed across all evaluated changes."
        ),
        "effort": "small",
    }


def test_snapshot_review_fails_closed_without_candidate_checkpoint() -> None:
    class Processor:
        async def prepare(self, run_id: UUID) -> GeneratedConventions:
            return conventions()

    class Repository:
        async def get_review_prompt_input(
            self, run_id: UUID, saved: GeneratedConventions
        ) -> ReviewPromptInput:
            return ReviewPromptInput(
                system="Review carefully.",
                rules=(),
                agents_md=None,
                conventions=RepoConventions((), ()),
                changed_files=(
                    ChangedFile("app/main.py", "modified", (DiffLine(10, "added", "query = ..."),)),
                ),
                omitted_files=(),
                policy_snapshot=_make_test_snapshot(),
            )

    class Model:
        async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
            return PullRequestMeta(
                "Title", None, "author", "feature", "main", (), 1, 1, 0, False, False
            )

        async def draft_review(self, *, context: ReviewContext) -> ReviewReply:
            output = {
                "findings": [
                    {
                        "path": "app/main.py",
                        "line": 10,
                        "start_line": None,
                        "severity": "high",
                        "category": "security",
                        "title": "SQL injection",
                        "body": "Raw query concatenation",
                        "suggestion": None,
                        "confidence": 0.9,
                        "rule_name": None,
                        "evidence_refs": [
                            {"block_id": MAIN_BLOCK_ID, "start_line": 10, "end_line": 10}
                        ],
                    }
                ],
                "summary": {"problem": "A bug.", "done_well": "Small change.", "effort": "small"},
            }
            return ReviewReply(
                output,
                context,
                PromptBuilder().build_manifest(context, call_id=UUID(int=435), stage="review"),
            )

    class Publisher:
        async def execute(self, run_id: UUID, raw_output: object) -> bool:
            raise AssertionError("publication must not run without a checkpoint")

    class Verifier:
        async def verify(
            self, *, system_prompt: str, user_prompt: str, **_: object
        ) -> Mapping[str, Any]:
            raise AssertionError("verifier must not run without a checkpoint")

    pipeline = ExecuteReviewRun(
        Processor(), Repository(), Model(), Publisher(), reflection_pass=ReflectionPass(Verifier())
    )
    with pytest.raises(RunFailure, match="checkpoint"):
        asyncio.run(pipeline.execute(RUN_ID))
