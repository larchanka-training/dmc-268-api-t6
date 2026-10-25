"""Behavioural contract for the ordinary review-worker completion pipeline."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest

from app.modules.reviews.application.conventions import CachedConventions, GeneratedConventions
from app.modules.reviews.application.execute_review import (
    ExecuteReviewRun,
    ReviewPromptInput,
)
from app.modules.reviews.application.policy_errors import (
    PolicyError,
    PolicyErrorCode,
)
from app.modules.reviews.application.policy_snapshot import (
    POLICY_SNAPSHOT_SCHEMA_VERSION,
    CallManifest,
    ExecutionProfile,
    ManifestBlock,
    PolicySnapshot,
    PromptBindings,
    SourceRole,
    make_complete_execution_profile,
    make_complete_prompt_bindings,
    make_policy_document_missing,
    make_policy_document_present,
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
    VerificationInvalidOutputError,
    VerificationUnavailableError,
)
from app.modules.reviews.application.review_context import ReviewContextPayload
from app.modules.reviews.application.review_output import (
    EvidenceRef,
    InvalidReviewOutput,
    RequirementRef,
)
from app.modules.reviews.application.review_path import ReviewBinding
from app.modules.reviews.application.run_failures import RunFailure

RUN_ID = UUID("00000000-0000-0000-0000-000000000200")
MAIN_BLOCK_ID = file_block_id("app/main.py")


def conventions() -> GeneratedConventions:
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


def _make_test_snapshot(
    *,
    is_ready: bool = True,
    max_input_tokens: int = 60_000,
    max_policy_tokens: int = 12_000,
) -> PolicySnapshot:
    from dataclasses import replace

    rules = make_policy_document_present(
        path=".review/rules.md",
        text="# Rules\nAlways sanitize input.\n",
        blob_oid="r" * 40,
    )
    agents = make_policy_document_missing("AGENTS.md")
    bindings = (
        make_complete_prompt_bindings()
        if is_ready
        else PromptBindings("a" * 64, "b" * 64, "c" * 64, review_system_id=None)
    )
    profile = (
        make_complete_execution_profile("fast", review_model="model-a", verifier_model="model-b")
        if is_ready
        else ExecutionProfile.fast("model-a", "model-b")
    )
    if is_ready:
        profile = replace(
            profile,
            max_input_tokens=max_input_tokens,
            max_policy_tokens=max_policy_tokens,
        )
    return PolicySnapshot(
        schema_version=POLICY_SNAPSHOT_SCHEMA_VERSION,
        snapshot_id=UUID("00000000-0000-0000-0000-000000000003"),
        run_id=UUID("00000000-0000-0000-0000-000000000002"),
        repository_id=UUID("00000000-0000-0000-0000-000000000001"),
        policy_base_sha="b" * 40,
        rules=rules,
        agents=agents,
        ignore_patterns=(),
        ignore_digest="0" * 64,
        parser_profile="rules-markdown/v1",
        parser_version="4.0.0",
        matcher_version="review-ignore/v1",
        limits_version="policy-limits/v1",
        builtin_exclusions_version="builtin-exclusions/v1",
        priority_matcher_version="priority-matcher/v1",
        service_defaults_rule_version_id=UUID("00000000-0000-0000-0000-000000000004"),
        defaults_checksum="282d61c2da897152046db742fbd9bfda061ba1c71ce32759d47e042c06b0ff52",
        prompt_bindings=bindings,
        execution_profile=profile,
        policy_digest="3" * 64,
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

        async def draft_review(self, *, context: ReviewContext) -> dict[str, object]:
            prompt = PromptBuilder().build(context)
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

        async def draft_review(self, *, context: ReviewContext) -> dict[str, object]:
            raise AssertionError("no provider call after failed processing")

    class Publisher:
        async def execute(self, run_id: UUID, raw_output: dict[str, object]) -> bool:
            raise AssertionError("no publication after failed processing")

    pipeline = ExecuteReviewRun(Processor(), Repository(), Model(), Publisher())  # type: ignore[arg-type]

    assert asyncio.run(pipeline.execute(RUN_ID)) is False


class FakeVerifierModel:
    def __init__(self, decisions_payload: Mapping[str, Any]) -> None:
        self.decisions_payload = decisions_payload
        self.call_count = 0

    async def verify(
        self, *, system_prompt: str, user_prompt: str, **_: object
    ) -> Mapping[str, Any]:
        self.call_count += 1
        cand_ids = re.findall(r'candidate candidate_id="([^"]+)"', user_prompt)
        payload = dict(self.decisions_payload)
        if "decisions" in payload and cand_ids:
            new_decisions = []
            for idx, d in enumerate(payload["decisions"]):
                d_copy = dict(d)
                if idx < len(cand_ids):
                    d_copy["candidate_id"] = cand_ids[idx]
                new_decisions.append(d_copy)
            payload["decisions"] = new_decisions
        return payload


def test_execute_review_run_with_reflection_pass_filters_unsupported_findings() -> None:
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
                conventions=RepoConventions(("Use services.",), ("Test paths.",)),
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

        async def draft_review(self, *, context: ReviewContext) -> dict[str, Any]:
            return {
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
                    },
                    {
                        "path": "app/main.py",
                        "line": 10,
                        "start_line": None,
                        "severity": "medium",
                        "category": "correctness",
                        "title": "Hallucinated issue",
                        "body": "Nonexistent variable leak",
                        "suggestion": None,
                        "confidence": 0.7,
                        "rule_name": None,
                        "evidence_refs": [
                            {"block_id": MAIN_BLOCK_ID, "start_line": 10, "end_line": 10}
                        ],
                    },
                ],
                "summary": {
                    "problem": "Found security flaw.",
                    "done_well": "Clean architecture.",
                    "effort": "small",
                },
            }

    published_output: dict[str, Any] = {}

    class Publisher:
        async def execute(
            self, run_id: UUID, raw_output: Mapping[str, object] | str | bytes
        ) -> bool:
            nonlocal published_output
            published_output = dict(raw_output) if isinstance(raw_output, Mapping) else {}
            return True

    fake_verifier_model = FakeVerifierModel(
        {
            "decisions": [
                {
                    "candidate_id": "cand-1",
                    "status": "supported",
                    "reason": "Direct unparameterized SQL query execution.",
                    "evidence_refs": [
                        {"block_id": MAIN_BLOCK_ID, "start_line": 10, "end_line": 10}
                    ],
                    "requirement_refs": [],
                },
                {
                    "candidate_id": "cand-2",
                    "status": "contradicted",
                    "reason": "Variable does not exist and no leak occurs in visible context.",
                    "evidence_refs": [
                        {"block_id": MAIN_BLOCK_ID, "start_line": 10, "end_line": 10}
                    ],
                    "requirement_refs": [],
                },
            ]
        }
    )
    reflection_pass = ReflectionPass(fake_verifier_model)
    pipeline = ExecuteReviewRun(
        Processor(),
        Repository(),
        Model(),
        Publisher(),
        reflection_pass=reflection_pass,
    )

    success = asyncio.run(pipeline.execute(RUN_ID))
    assert success is True
    assert fake_verifier_model.call_count == 1

    # Only supported candidate (SQL injection) is published!
    published_findings = published_output["findings"]
    assert len(published_findings) == 1
    assert published_findings[0]["title"] == "SQL injection"


def test_execute_review_run_with_reflection_pass_zero_findings_skips_verifier() -> None:
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
                conventions=RepoConventions(("Use services.",), ("Test paths.",)),
                changed_files=(
                    ChangedFile("app/main.py", "modified", (DiffLine(10, "added", "x = 1"),)),
                ),
                omitted_files=(),
            )

    class Model:
        async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
            return PullRequestMeta(
                "Title", None, "author", "feature", "main", (), 1, 1, 0, False, False
            )

        async def draft_review(self, *, context: ReviewContext) -> dict[str, Any]:
            # Zero findings
            return {
                "findings": [],
                "summary": {
                    "problem": "No issues found.",
                    "done_well": "Clean architecture.",
                    "effort": "small",
                },
            }

    published_output: dict[str, Any] = {}

    class Publisher:
        async def execute(
            self, run_id: UUID, raw_output: Mapping[str, object] | str | bytes
        ) -> bool:
            nonlocal published_output
            published_output = dict(raw_output) if isinstance(raw_output, Mapping) else {}
            return True

    fake_verifier_model = FakeVerifierModel({"decisions": []})
    reflection_pass = ReflectionPass(fake_verifier_model)
    pipeline = ExecuteReviewRun(
        Processor(),
        Repository(),
        Model(),
        Publisher(),
        reflection_pass=reflection_pass,
    )

    success = asyncio.run(pipeline.execute(RUN_ID))
    assert success is True
    # Verifier MUST be skipped (0 calls)
    assert fake_verifier_model.call_count == 0
    assert published_output["findings"] == []


def test_execute_review_run_excluded_file_does_not_reach_verifier() -> None:
    captured_verifier_prompts: list[str] = []

    class CapturingVerifierModel:
        async def verify(
            self, *, system_prompt: str, user_prompt: str, **_: object
        ) -> Mapping[str, Any]:
            captured_verifier_prompts.append(user_prompt)
            cand_ids = re.findall(r'candidate candidate_id="([^"]+)"', user_prompt)
            return {
                "decisions": [
                    {
                        "candidate_id": cand_id,
                        "status": "supported",
                        "reason": "OK",
                        "evidence_refs": [
                            {"block_id": MAIN_BLOCK_ID, "start_line": 10, "end_line": 10}
                        ],
                        "requirement_refs": [],
                    }
                    for cand_id in cand_ids
                ]
            }

    class ContextFilter:
        diff_old_sha: str | None = None
        head_sha: str | None = None
        snapshot_for_llm: PolicySnapshot | None = None
        payload: ReviewContextPayload | None = None

        def excluded(self, path: str) -> bool:
            return path == "app/secret_credentials.env"

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
                conventions=RepoConventions(("Use services.",), ("Test paths.",)),
                changed_files=(
                    ChangedFile("app/main.py", "modified", (DiffLine(10, "added", "x = 1"),)),
                    ChangedFile(
                        "app/secret_credentials.env",
                        "added",
                        (DiffLine(1, "added", "SECRET=supersecret"),),
                    ),
                ),
                omitted_files=("app/secret_credentials.env",),
                policy_snapshot=_make_test_snapshot(),
            )

    class Model:
        async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
            return PullRequestMeta(
                "Title", None, "author", "feature", "main", (), 1, 1, 0, False, False
            )

        async def draft_review(self, *, context: ReviewContext) -> dict[str, Any]:
            # Ensure reviewer context never got the excluded file
            assert not any(f.path == "app/secret_credentials.env" for f in context.changed_files)
            assert "app/secret_credentials.env" not in context.omitted_files
            return {
                "findings": [
                    {
                        "path": "app/main.py",
                        "line": 10,
                        "start_line": None,
                        "severity": "high",
                        "category": "security",
                        "title": "Finding on main.py",
                        "body": "Issue description",
                        "suggestion": None,
                        "confidence": 0.9,
                        "rule_name": None,
                        "evidence_refs": [
                            {"block_id": MAIN_BLOCK_ID, "start_line": 10, "end_line": 10}
                        ],
                    }
                ],
                "summary": {"problem": "Issue", "done_well": "OK", "effort": "small"},
            }

    class Publisher:
        async def execute(self, run_id: UUID, raw_output: Any) -> bool:
            return True

    pipeline = ExecuteReviewRun(
        Processor(),
        Repository(),
        Model(),
        Publisher(),
        context_filter=ContextFilter(),
        reflection_pass=ReflectionPass(CapturingVerifierModel()),
    )

    success = asyncio.run(pipeline.execute(RUN_ID))
    assert success is True
    assert len(captured_verifier_prompts) == 1
    # Excluded file must never appear in verifier prompt
    assert "secret_credentials.env" not in captured_verifier_prompts[0]
    assert "supersecret" not in captured_verifier_prompts[0]


def test_execute_review_fail_closed_when_snapshot_present_without_reflection_pass() -> None:
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
                policy_snapshot=_make_test_snapshot(),
            )

    class Model:
        def __init__(self) -> None:
            self.draft_calls = 0

        async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
            return PullRequestMeta("Title", None, "u", "b", "m", (), 1, 1, 0, False, False)

        async def draft_review(self, *, context: ReviewContext) -> dict[str, Any]:
            self.draft_calls += 1
            return {"findings": [], "summary": {"problem": "p", "done_well": "d", "effort": "none"}}

    class Publisher:
        def __init__(self) -> None:
            self.publish_calls = 0

        async def execute(self, run_id: UUID, raw_output: Any) -> bool:
            self.publish_calls += 1
            return True

    model = Model()
    publisher = Publisher()
    pipeline = ExecuteReviewRun(
        Processor(),
        Repository(),
        model,
        publisher,
        reflection_pass=None,  # Missing reflection_pass with snapshot present
    )

    with pytest.raises(PolicyError) as exc_info:
        asyncio.run(pipeline.execute(RUN_ID))
    assert exc_info.value.code == PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR
    assert "Reflection pass is required" in exc_info.value.message
    assert model.draft_calls == 0
    assert publisher.publish_calls == 0


def test_execute_review_fail_closed_when_snapshot_not_ready() -> None:
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
                policy_snapshot=_make_test_snapshot(is_ready=False),  # Incomplete bindings
            )

    class Model:
        def __init__(self) -> None:
            self.draft_calls = 0

        async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
            return PullRequestMeta("Title", None, "u", "b", "m", (), 1, 1, 0, False, False)

        async def draft_review(self, *, context: ReviewContext) -> dict[str, Any]:
            self.draft_calls += 1
            return {"findings": [], "summary": {"problem": "p", "done_well": "d", "effort": "none"}}

    class Publisher:
        def __init__(self) -> None:
            self.publish_calls = 0

        async def execute(self, run_id: UUID, raw_output: Any) -> bool:
            self.publish_calls += 1
            return True

    model = Model()
    publisher = Publisher()
    fake_verifier_model = FakeVerifierModel({"decisions": []})
    pipeline = ExecuteReviewRun(
        Processor(),
        Repository(),
        model,
        publisher,
        reflection_pass=ReflectionPass(fake_verifier_model),
    )

    with pytest.raises(PolicyError) as exc_info:
        asyncio.run(pipeline.execute(RUN_ID))
    assert exc_info.value.code == PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR
    assert "Policy snapshot is not ready" in exc_info.value.message
    assert model.draft_calls == 0
    assert publisher.publish_calls == 0


def test_execute_review_summary_cleansed_when_all_candidates_rejected() -> None:
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
            return PullRequestMeta("Title", None, "u", "b", "m", (), 1, 1, 0, False, False)

        async def draft_review(self, *, context: ReviewContext) -> dict[str, Any]:
            return {
                "findings": [
                    {
                        "path": "app/main.py",
                        "line": 10,
                        "start_line": None,
                        "severity": "high",
                        "category": "security",
                        "title": "Imagined SQL injection",
                        "body": "Hallucinated issue body",
                        "suggestion": None,
                        "confidence": 0.8,
                        "rule_name": None,
                        "evidence_refs": [
                            {"block_id": MAIN_BLOCK_ID, "start_line": 10, "end_line": 10}
                        ],
                    }
                ],
                "summary": {
                    "problem": "Severe security flaw found in SQL construction.",
                    "done_well": "Good code style and structure.",
                    "effort": "large",
                },
            }

    published_output: dict[str, Any] = {}

    class Publisher:
        async def execute(self, run_id: UUID, raw_output: Any) -> bool:
            nonlocal published_output
            published_output = dict(raw_output)
            return True

    fake_verifier_model = FakeVerifierModel(
        {
            "decisions": [
                {
                    "candidate_id": "cand-1",
                    "status": "contradicted",
                    "reason": "Variable is an integer constant, cannot inject SQL.",
                    "evidence_refs": [
                        {"block_id": MAIN_BLOCK_ID, "start_line": 10, "end_line": 10}
                    ],
                    "requirement_refs": [],
                }
            ]
        }
    )
    pipeline = ExecuteReviewRun(
        Processor(),
        Repository(),
        Model(),
        Publisher(),
        reflection_pass=ReflectionPass(fake_verifier_model),
    )

    success = asyncio.run(pipeline.execute(RUN_ID))
    assert success is True
    assert published_output["findings"] == []
    # Summary problem must be cleansed of hallucinated defects!
    summary = published_output["summary"]
    assert "Severe security flaw" not in summary["problem"]
    assert "No verified issues were found" in summary["problem"]
    assert (
        summary["done_well"]
        == "Code analysis and policy verification were completed across all evaluated changes."
    )
    assert summary["effort"] == "none"


def test_execute_review_json_rules_compatibility_with_reflection_pass() -> None:
    class Processor:
        async def prepare(self, run_id: UUID) -> GeneratedConventions:
            return conventions()

    class Repository:
        async def get_review_prompt_input(
            self, run_id: UUID, saved: GeneratedConventions
        ) -> ReviewPromptInput:
            return ReviewPromptInput(
                system="Review carefully.",
                rules=(ReviewRule("No print", ("app/**",), (), ("Do not print.",)),),
                agents_md=None,
                conventions=RepoConventions((), ()),
                changed_files=(
                    ChangedFile("app/main.py", "modified", (DiffLine(5, "added", "print(1)"),)),
                ),
                omitted_files=(),
                policy_snapshot=None,  # Legacy JSON rules mode without snapshot
            )

    class Model:
        async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
            return PullRequestMeta("Title", None, "u", "b", "m", (), 1, 1, 0, False, False)

        async def draft_review(self, *, context: ReviewContext) -> dict[str, Any]:
            # Legacy v1 model output with rule_name="No print" and NO evidence_refs
            return {
                "findings": [
                    {
                        "path": "app/main.py",
                        "line": 5,
                        "start_line": None,
                        "severity": "medium",
                        "category": "readability",
                        "title": "Disallowed print",
                        "body": (
                            "According to custom instructions in 'No print': Print statement found."
                        ),
                        "suggestion": None,
                        "confidence": 0.95,
                        "rule_name": "No print",
                    }
                ],
                "summary": {
                    "problem": "Print statement found.",
                    "done_well": "Clean.",
                    "effort": "small",
                },
            }

    published_output: dict[str, Any] = {}

    class Publisher:
        async def execute(self, run_id: UUID, raw_output: Any) -> bool:
            nonlocal published_output
            published_output = dict(raw_output)
            return True

    verifier_call_count = 0

    class TrackingVerifierModel:
        async def verify(
            self, *, system_prompt: str, user_prompt: str, **_: object
        ) -> Mapping[str, Any]:
            nonlocal verifier_call_count
            verifier_call_count += 1
            return {"decisions": []}

    pipeline = ExecuteReviewRun(
        Processor(),
        Repository(),
        Model(),
        Publisher(),
        reflection_pass=ReflectionPass(TrackingVerifierModel()),
    )

    # Legacy run without snapshot must bypass verifier and publish without crashing
    success = asyncio.run(pipeline.execute(RUN_ID))
    assert success is True
    assert verifier_call_count == 0
    assert len(published_output["findings"]) == 1
    assert published_output["findings"][0]["title"] == "Disallowed print"
    assert published_output["findings"][0]["rule_name"] == "No print"


@pytest.mark.parametrize(
    "verifier_exc",
    [
        VerificationUnavailableError("Verifier downstream failure"),
        VerificationInvalidOutputError("Verifier schema failure"),
        RunFailure("verification_context_overflow", "Tokens exceeded"),
        RunFailure("verification_deadline_exceeded", "Time limit reached"),
    ],
)
def test_execute_review_verifier_failure_prevents_publication(verifier_exc: Exception) -> None:
    class FailingVerifierModel:
        async def verify(
            self, *, system_prompt: str, user_prompt: str, **_: object
        ) -> Mapping[str, Any]:
            raise verifier_exc

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
            return PullRequestMeta("Title", None, "u", "b", "m", (), 1, 1, 0, False, False)

        async def draft_review(self, *, context: ReviewContext) -> dict[str, Any]:
            return {
                "findings": [
                    {
                        "path": "app/main.py",
                        "line": 10,
                        "start_line": None,
                        "severity": "high",
                        "category": "security",
                        "title": "Finding 1",
                        "body": "Body",
                        "suggestion": None,
                        "confidence": 0.9,
                        "rule_name": None,
                        "evidence_refs": [
                            {"block_id": MAIN_BLOCK_ID, "start_line": 10, "end_line": 10}
                        ],
                    }
                ],
                "summary": {"problem": "Issue found.", "done_well": "Clean.", "effort": "small"},
            }

    publish_called = False

    class Publisher:
        async def execute(self, run_id: UUID, raw_output: Any) -> bool:
            nonlocal publish_called
            publish_called = True
            return True

    pipeline = ExecuteReviewRun(
        Processor(),
        Repository(),
        Model(),
        Publisher(),
        reflection_pass=ReflectionPass(FailingVerifierModel()),
    )

    with pytest.raises(type(verifier_exc)):
        asyncio.run(pipeline.execute(RUN_ID))

    assert publish_called is False


def test_execute_review_candidate_count_limit() -> None:
    from app.modules.reviews.application.review_output import InvalidReviewOutput

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
            return PullRequestMeta("Title", None, "u", "b", "m", (), 1, 1, 0, False, False)

        async def draft_review(self, *, context: ReviewContext) -> dict[str, Any]:
            # 11 candidates (exceeds limit of 10)
            return {
                "findings": [
                    {
                        "path": "app/main.py",
                        "line": 10,
                        "start_line": None,
                        "severity": "low",
                        "category": "style",
                        "title": f"Finding {i}",
                        "body": "Body",
                        "suggestion": None,
                        "confidence": 0.8,
                        "rule_name": None,
                        "evidence_refs": [
                            {"block_id": MAIN_BLOCK_ID, "start_line": 10, "end_line": 10}
                        ],
                    }
                    for i in range(11)
                ],
                "summary": {"problem": "Issues.", "done_well": "Clean.", "effort": "medium"},
            }

    publish_called = False

    class Publisher:
        async def execute(self, run_id: UUID, raw_output: Any) -> bool:
            nonlocal publish_called
            publish_called = True
            return True

    pipeline = ExecuteReviewRun(
        Processor(),
        Repository(),
        Model(),
        Publisher(),
        reflection_pass=ReflectionPass(FakeVerifierModel({"decisions": []})),
    )

    with pytest.raises(InvalidReviewOutput, match="maximum of 10"):
        asyncio.run(pipeline.execute(RUN_ID))

    assert publish_called is False


def test_execute_review_summary_cleansed_when_partially_rejected() -> None:
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
            return PullRequestMeta("Title", None, "u", "b", "m", (), 1, 1, 0, False, False)

        async def draft_review(self, *, context: ReviewContext) -> dict[str, Any]:
            return {
                "findings": [
                    {
                        "path": "app/main.py",
                        "line": 10,
                        "start_line": None,
                        "severity": "critical",
                        "category": "security",
                        "title": "Critical flaw",
                        "body": "Body 1",
                        "suggestion": None,
                        "confidence": 0.9,
                        "rule_name": None,
                        "evidence_refs": [
                            {"block_id": MAIN_BLOCK_ID, "start_line": 10, "end_line": 10}
                        ],
                    },
                    {
                        "path": "app/main.py",
                        "line": 10,
                        "start_line": None,
                        "severity": "medium",
                        "category": "correctness",
                        "title": "Medium flaw",
                        "body": "Body 2",
                        "suggestion": None,
                        "confidence": 0.8,
                        "rule_name": None,
                        "evidence_refs": [
                            {"block_id": MAIN_BLOCK_ID, "start_line": 10, "end_line": 10}
                        ],
                    },
                ],
                "summary": {
                    "problem": "Severe critical vulnerability found.",
                    "done_well": "Well documented code.",
                    "effort": "large",
                },
            }

    published_output: dict[str, Any] = {}

    class Publisher:
        async def execute(self, run_id: UUID, raw_output: Any) -> bool:
            nonlocal published_output
            published_output = dict(raw_output)
            return True

    fake_verifier_model = FakeVerifierModel(
        {
            "decisions": [
                {
                    "candidate_id": "c-1",
                    "status": "contradicted",
                    "reason": "Not a critical flaw.",
                    "evidence_refs": [
                        {"block_id": MAIN_BLOCK_ID, "start_line": 10, "end_line": 10}
                    ],
                    "requirement_refs": [],
                },
                {
                    "candidate_id": "c-2",
                    "status": "supported",
                    "reason": "Confirmed correctness flaw.",
                    "evidence_refs": [
                        {"block_id": MAIN_BLOCK_ID, "start_line": 10, "end_line": 10}
                    ],
                    "requirement_refs": [],
                },
            ]
        }
    )

    pipeline = ExecuteReviewRun(
        Processor(),
        Repository(),
        Model(),
        Publisher(),
        reflection_pass=ReflectionPass(fake_verifier_model),
    )

    success = asyncio.run(pipeline.execute(RUN_ID))
    assert success is True
    assert len(published_output["findings"]) == 1
    assert published_output["findings"][0]["title"] == "Medium flaw"

    summary = published_output["summary"]
    # Severe vulnerability text was cleansed
    assert "Severe critical vulnerability" not in summary["problem"]
    assert "Identified 1 verified issue (correctness)" in summary["problem"]
    # Exactly one sentence (no internal period)
    assert summary["problem"].count(".") == 1
    # Effort is small/medium based on remaining finding
    assert summary["effort"] == "small"
    # Done well was cleansed of unverified praise
    assert (
        summary["done_well"]
        == "Code analysis and policy verification were completed across all evaluated changes."
    )


def test_execute_review_wraps_candidate_validation_error_in_invalid_review_output() -> None:
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
            return PullRequestMeta("Title", None, "u", "b", "m", (), 1, 1, 0, False, False)

        async def draft_review(self, *, context: ReviewContext) -> dict[str, Any]:
            # Invalid candidate finding: confidence 1.5 exceeds 1.0 limit
            return {
                "findings": [
                    {
                        "path": "app/main.py",
                        "line": 10,
                        "start_line": None,
                        "severity": "high",
                        "category": "security",
                        "title": "Finding 1",
                        "body": "Body",
                        "suggestion": None,
                        "confidence": 1.5,
                        "rule_name": None,
                        "evidence_refs": [
                            {"block_id": MAIN_BLOCK_ID, "start_line": 10, "end_line": 10}
                        ],
                    }
                ],
                "summary": {"problem": "Issue.", "done_well": "OK.", "effort": "small"},
            }

    class Publisher:
        async def execute(self, run_id: UUID, raw_output: Any) -> bool:
            return True

    pipeline = ExecuteReviewRun(
        Processor(),
        Repository(),
        Model(),
        Publisher(),
        reflection_pass=ReflectionPass(FakeVerifierModel({"decisions": []})),
    )

    with pytest.raises(InvalidReviewOutput, match="Malformed candidate finding at index 0"):
        asyncio.run(pipeline.execute(RUN_ID))


@pytest.mark.parametrize(
    ("severities_categories", "expected_effort", "expected_problem_part"),
    [
        # critical -> large, multiple findings -> plural "issues" with sorted categories
        (
            [("critical", "security"), ("high", "correctness")],
            "large",
            "Identified 2 verified issues (correctness, security)",
        ),
        # high -> medium, single finding -> singular "issue"
        (
            [("high", "performance")],
            "medium",
            "Identified 1 verified issue (performance)",
        ),
        # info -> none, single finding -> singular "issue"
        (
            [("info", "readability")],
            "none",
            "Identified 1 verified issue (readability)",
        ),
    ],
)
def test_execute_review_summary_cleansing_effort_categories_and_plural(
    severities_categories: list[tuple[str, str]],
    expected_effort: str,
    expected_problem_part: str,
) -> None:
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

    findings_list = [
        # One finding that will be rejected to trigger cleansing
        {
            "path": "app/main.py",
            "line": 10,
            "start_line": None,
            "severity": "medium",
            "category": "readability",
            "title": "Rejected flaw",
            "body": "Body reject",
            "suggestion": None,
            "confidence": 0.8,
            "rule_name": None,
            "evidence_refs": [{"block_id": MAIN_BLOCK_ID, "start_line": 10, "end_line": 10}],
        }
    ]
    for idx, (sev, cat) in enumerate(severities_categories, start=1):
        findings_list.append(
            {
                "path": "app/main.py",
                "line": 10,
                "start_line": None,
                "severity": sev,
                "category": cat,
                "title": f"Supported flaw {idx}",
                "body": f"Body {idx}",
                "suggestion": None,
                "confidence": 0.8,
                "rule_name": None,
                "evidence_refs": [{"block_id": MAIN_BLOCK_ID, "start_line": 10, "end_line": 10}],
            }
        )

    class Model:
        async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
            return PullRequestMeta("Title", None, "u", "b", "m", (), 1, 1, 0, False, False)

        async def draft_review(self, *, context: ReviewContext) -> dict[str, Any]:
            return {
                "findings": findings_list,
                "summary": {
                    "problem": "Severe flaw.",
                    "done_well": "Model praise.",
                    "effort": "large",
                },
            }

    published_output: dict[str, Any] = {}

    class Publisher:
        async def execute(self, run_id: UUID, raw_output: Any) -> bool:
            nonlocal published_output
            published_output = dict(raw_output)
            return True

    class DynamicVerifierModel:
        async def verify(
            self, *, system_prompt: str, user_prompt: str, **_: object
        ) -> Mapping[str, Any]:
            import re

            cand_ids = re.findall(r'<candidate candidate_id="([^"]+)"', user_prompt)
            decisions = [
                {
                    "candidate_id": cand_ids[0],
                    "status": "contradicted",
                    "reason": "Not valid",
                    "evidence_refs": [
                        {"block_id": MAIN_BLOCK_ID, "start_line": 10, "end_line": 10}
                    ],
                    "requirement_refs": [],
                }
            ]
            for cid in cand_ids[1:]:
                decisions.append(
                    {
                        "candidate_id": cid,
                        "status": "supported",
                        "reason": "Confirmed",
                        "evidence_refs": [
                            {"block_id": MAIN_BLOCK_ID, "start_line": 10, "end_line": 10}
                        ],
                        "requirement_refs": [],
                    }
                )
            return {"decisions": decisions}

    pipeline = ExecuteReviewRun(
        Processor(),
        Repository(),
        Model(),
        Publisher(),
        reflection_pass=ReflectionPass(DynamicVerifierModel()),
    )

    success = asyncio.run(pipeline.execute(RUN_ID))
    assert success is True
    summary = published_output["summary"]
    assert expected_problem_part in summary["problem"]
    assert summary["effort"] == expected_effort
    assert (
        summary["done_well"]
        == "Code analysis and policy verification were completed across all evaluated changes."
    )


def test_execute_review_missing_evidence_refs_not_double_wrapped() -> None:
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
            return PullRequestMeta("Title", None, "u", "b", "m", (), 1, 1, 0, False, False)

        async def draft_review(self, *, context: ReviewContext) -> dict[str, Any]:
            return {
                "findings": [
                    {
                        "path": "app/main.py",
                        "line": 10,
                        "severity": "critical",
                        "category": "security",
                        "title": "SQL Injection",
                        "body": "Raw query used",
                        "confidence": 0.9,
                        # missing evidence_refs
                    }
                ],
                "summary": {"problem": "p", "done_well": "d", "effort": "none"},
            }

    class Publisher:
        async def execute(self, run_id: UUID, raw_output: Any) -> bool:
            return True

    pipeline = ExecuteReviewRun(
        Processor(),
        Repository(),
        Model(),
        Publisher(),
        reflection_pass=ReflectionPass(FakeVerifierModel({"decisions": []})),
    )

    with pytest.raises(InvalidReviewOutput) as exc_info:
        asyncio.run(pipeline.execute(RUN_ID))
    # Must NOT have nested "Malformed candidate finding at index 0: Candidate finding at index 0..."
    assert str(exc_info.value) == "Candidate finding at index 0 missing required evidence_refs"


def test_execute_review_supports_model_dump_candidate_item() -> None:
    class DumpableFinding:
        def model_dump(self) -> dict[str, Any]:
            return {
                "path": "app/main.py",
                "line": 10,
                "start_line": None,
                "severity": "critical",
                "category": "security",
                "title": "SQL Injection",
                "body": "Raw query used",
                "suggestion": None,
                "confidence": 0.9,
                "rule_name": None,
                "evidence_refs": [{"block_id": MAIN_BLOCK_ID, "start_line": 10, "end_line": 10}],
            }

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
            return PullRequestMeta("Title", None, "u", "b", "m", (), 1, 1, 0, False, False)

        async def draft_review(self, *, context: ReviewContext) -> dict[str, Any]:
            return {
                "findings": [DumpableFinding()],
                "summary": {"problem": "p", "done_well": "d", "effort": "none"},
            }

    class DynamicVerifierModel:
        async def verify(
            self, *, system_prompt: str, user_prompt: str, **_: object
        ) -> dict[str, Any]:
            import re

            m = re.search(r'candidate_id="([^"]+)"', user_prompt)
            assert m is not None
            cand_id = m.group(1)
            return {
                "decisions": [
                    {
                        "candidate_id": cand_id,
                        "status": "supported",
                        "reason": "OK",
                        "evidence_refs": [
                            {"block_id": MAIN_BLOCK_ID, "start_line": 10, "end_line": 10}
                        ],
                        "requirement_refs": [],
                    }
                ]
            }

    class Publisher:
        def __init__(self) -> None:
            self.output: Any = None

        async def execute(self, run_id: UUID, raw_output: Any) -> bool:
            self.output = raw_output
            return True

    publisher = Publisher()
    pipeline = ExecuteReviewRun(
        Processor(),
        Repository(),
        Model(),
        publisher,
        reflection_pass=ReflectionPass(DynamicVerifierModel()),
    )
    assert asyncio.run(pipeline.execute(RUN_ID)) is True
    assert len(publisher.output["findings"]) == 1


def test_execute_review_malformed_json_and_non_dict_draft_output() -> None:
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

    class Publisher:
        async def execute(self, run_id: UUID, raw_output: Any) -> bool:
            return True

    # 1. Malformed JSON string
    class MalformedJsonModel:
        async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
            return PullRequestMeta("Title", None, "u", "b", "m", (), 1, 1, 0, False, False)

        async def draft_review(self, *, context: ReviewContext) -> str:
            return "not a valid json {"

    p1 = ExecuteReviewRun(
        Processor(),
        Repository(),
        MalformedJsonModel(),
        Publisher(),
        reflection_pass=ReflectionPass(FakeVerifierModel({"decisions": []})),
    )
    with pytest.raises(InvalidReviewOutput, match="Malformed review model JSON output"):
        asyncio.run(p1.execute(RUN_ID))

    # 2. JSON parses to non-dict (e.g. list)
    class NonDictJsonModel:
        async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
            return PullRequestMeta("Title", None, "u", "b", "m", (), 1, 1, 0, False, False)

        async def draft_review(self, *, context: ReviewContext) -> str:
            return "[]"

    p2 = ExecuteReviewRun(
        Processor(),
        Repository(),
        NonDictJsonModel(),
        Publisher(),
        reflection_pass=ReflectionPass(FakeVerifierModel({"decisions": []})),
    )
    with pytest.raises(InvalidReviewOutput, match="Expected dict for review model output"):
        asyncio.run(p2.execute(RUN_ID))

    # 3. Dict missing findings
    class MissingFindingsModel:
        async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
            return PullRequestMeta("Title", None, "u", "b", "m", (), 1, 1, 0, False, False)

        async def draft_review(self, *, context: ReviewContext) -> dict[str, Any]:
            return {"summary": {}}

    p3 = ExecuteReviewRun(
        Processor(),
        Repository(),
        MissingFindingsModel(),
        Publisher(),
        reflection_pass=ReflectionPass(FakeVerifierModel({"decisions": []})),
    )
    with pytest.raises(InvalidReviewOutput, match="Review model output missing 'findings' list"):
        asyncio.run(p3.execute(RUN_ID))


def test_execute_review_rejects_finding_with_invalid_attribution() -> None:
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

    class InvalidAttrModel:
        async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
            return PullRequestMeta("Title", None, "u", "b", "m", (), 1, 1, 0, False, False)

        async def draft_review(self, *, context: ReviewContext) -> dict[str, Any]:
            return {
                "findings": [
                    {
                        "path": "app/main.py",
                        "line": 10,
                        "start_line": None,
                        "severity": "critical",
                        "category": "security",
                        "title": "T",
                        "body": "B",
                        "suggestion": None,
                        "confidence": 0.9,
                        "rule_name": None,
                        "evidence_refs": [
                            {"block_id": "non-existent-block", "start_line": 10, "end_line": 10}
                        ],
                    }
                ],
                "summary": {"problem": "p", "done_well": "d", "effort": "none"},
            }

    published_output: dict[str, Any] = {}

    class Publisher:
        async def execute(self, run_id: UUID, raw_output: Any) -> bool:
            nonlocal published_output
            published_output = dict(raw_output)
            return True

    pipeline = ExecuteReviewRun(
        Processor(),
        Repository(),
        InvalidAttrModel(),
        Publisher(),
        reflection_pass=ReflectionPass(FakeVerifierModel({"decisions": []})),
    )
    # CVS §6.2 & Issue #219 Part 5: unshown evidence ref drops only
    # this candidate (evidence_not_shown), without failing the entire run.
    success = asyncio.run(pipeline.execute(RUN_ID))
    assert success is True
    assert published_output["findings"] == []
    assert "No verified issues were found" in published_output["summary"]["problem"]


def test_execute_review_legacy_run_with_reflection_pass_logs_no_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

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
                policy_snapshot=None,  # Legacy run
            )

    class Model:
        async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
            return PullRequestMeta("Title", None, "u", "b", "m", (), 1, 1, 0, False, False)

        async def draft_review(self, *, context: ReviewContext) -> dict[str, Any]:
            return {"findings": [], "summary": {"problem": "p", "done_well": "d", "effort": "none"}}

    class Publisher:
        async def execute(self, run_id: UUID, raw_output: Any) -> bool:
            return True

    pipeline = ExecuteReviewRun(
        Processor(),
        Repository(),
        Model(),
        Publisher(),
        reflection_pass=ReflectionPass(FakeVerifierModel({"decisions": []})),
    )

    with caplog.at_level(logging.WARNING):
        success = asyncio.run(pipeline.execute(RUN_ID))
    assert success is True
    # The worker passes a reflection pass to every attempt (#130 R14): a legacy run logs nothing.
    assert [r.message for r in caplog.records if r.levelno >= logging.WARNING] == []

    # Without reflection pass, no warning is logged
    caplog.clear()
    pipeline_no_reflection = ExecuteReviewRun(
        Processor(),
        Repository(),
        Model(),
        Publisher(),
        reflection_pass=None,
    )
    with caplog.at_level(logging.WARNING):
        success2 = asyncio.run(pipeline_no_reflection.execute(RUN_ID))
    assert success2 is True
    assert not any(
        "execute_review_legacy_run_without_snapshot" in r.message for r in caplog.records
    )


def test_execute_review_reflection_pass_logs_run_id_call_id_and_counters(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

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
            return PullRequestMeta("Title", None, "u", "b", "m", (), 1, 1, 0, False, False)

        async def draft_review(self, *, context: ReviewContext) -> dict[str, Any]:
            return {
                "findings": [
                    {
                        "path": "app/main.py",
                        "line": 10,
                        "start_line": None,
                        "severity": "critical",
                        "category": "security",
                        "title": "SQL Injection",
                        "body": "Fix it.",
                        "suggestion": None,
                        "confidence": 0.9,
                        "rule_name": None,
                        "evidence_refs": [
                            {"block_id": MAIN_BLOCK_ID, "start_line": 10, "end_line": 10}
                        ],
                    }
                ],
                "summary": {"problem": "Issue.", "done_well": "Fine.", "effort": "none"},
            }

    class Publisher:
        async def execute(self, run_id: UUID, raw_output: Any) -> bool:
            return True

    class FakePassVerifier:
        def max_prompt_tokens(self) -> int | None:
            return None

        async def verify_recorded(self, *, candidates: Any, context: Any) -> Any:
            from app.modules.reviews.application.reflection_pass import VerificationRecord

            decisions = await self.verify(candidates=candidates, context=context)
            return VerificationRecord(decisions, tuple(c.candidate_id for c in candidates))

        async def verify(self, *, candidates: Any, context: Any) -> Any:
            from app.modules.reviews.application.reflection_pass import VerificationDecision

            return (
                VerificationDecision(
                    candidate_id=candidates[0].candidate_id,
                    status="supported",
                    reason="Confirmed defect.",
                    evidence_refs=[EvidenceRef(block_id=MAIN_BLOCK_ID, start_line=10, end_line=10)],
                    requirement_refs=[],
                ),
            )

    pipeline = ExecuteReviewRun(
        Processor(),
        Repository(),
        Model(),
        Publisher(),
        reflection_pass=FakePassVerifier(),  # type: ignore[arg-type]
    )

    with caplog.at_level(logging.INFO):
        success = asyncio.run(pipeline.execute(RUN_ID))
    assert success is True

    reflection_logs = [
        r for r in caplog.records if "Reflection pass completed for run" in r.message
    ]
    assert len(reflection_logs) == 1
    log_msg = reflection_logs[0].message
    assert str(RUN_ID) in log_msg
    assert "total=1, supported=1, rejected=0" in log_msg


def test_execute_review_validates_candidate_rule_name_against_manifest() -> None:
    class Processor:
        async def prepare(self, run_id: UUID) -> GeneratedConventions:
            return conventions()

    class RepositoryAgentsMissing:
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
                policy_snapshot=_make_test_snapshot(),  # AGENTS.md is missing
            )

    class ModelWithRuleName:
        def __init__(self, rule_name: str) -> None:
            self.rule_name = rule_name

        async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
            return PullRequestMeta("Title", None, "u", "b", "m", (), 1, 1, 0, False, False)

        async def draft_review(self, *, context: ReviewContext) -> dict[str, Any]:
            return {
                "findings": [
                    {
                        "path": "app/main.py",
                        "line": 10,
                        "start_line": None,
                        "severity": "medium",
                        "category": "correctness",
                        "title": "Rule check",
                        "body": "Body.",
                        "suggestion": None,
                        "confidence": 0.9,
                        "rule_name": self.rule_name,
                        "evidence_refs": [
                            {"block_id": MAIN_BLOCK_ID, "start_line": 10, "end_line": 10}
                        ],
                    }
                ],
                "summary": {"problem": "p.", "done_well": "d.", "effort": "none"},
            }

    class Publisher:
        async def execute(self, run_id: UUID, raw_output: Any) -> bool:
            return True

    published1: dict[str, Any] = {}

    class Publisher1:
        async def execute(self, run_id: UUID, raw_output: Any) -> bool:
            nonlocal published1
            published1 = dict(raw_output)
            return True

    # 1. Candidate attributes AGENTS.md, but AGENTS.md is absent in snapshot
    # -> dropped per candidate (CVS §6.2)
    pipeline1 = ExecuteReviewRun(
        Processor(),
        RepositoryAgentsMissing(),
        ModelWithRuleName("AGENTS.md"),
        Publisher1(),
        reflection_pass=ReflectionPass(FakeVerifierModel({"decisions": []})),
    )
    assert asyncio.run(pipeline1.execute(RUN_ID)) is True
    assert published1["findings"] == []

    # 2. Candidate attributes .review/rules.md, but rules.md is absent
    # in snapshot -> dropped per candidate
    from dataclasses import replace

    snapshot_no_rules = replace(
        _make_test_snapshot(),
        rules=make_policy_document_missing(".review/rules.md"),
    )

    class RepositoryRulesMissing:
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
                policy_snapshot=snapshot_no_rules,
            )

    published2: dict[str, Any] = {}

    class Publisher2:
        async def execute(self, run_id: UUID, raw_output: Any) -> bool:
            nonlocal published2
            published2 = dict(raw_output)
            return True

    pipeline2 = ExecuteReviewRun(
        Processor(),
        RepositoryRulesMissing(),
        ModelWithRuleName(".review/rules.md"),
        Publisher2(),
        reflection_pass=ReflectionPass(FakeVerifierModel({"decisions": []})),
    )
    assert asyncio.run(pipeline2.execute(RUN_ID)) is True
    assert published2["findings"] == []

    # 3. Candidate attributes service-defaults:fastapi, but context.rules is empty
    # -> dropped per candidate
    published3: dict[str, Any] = {}

    class Publisher3:
        async def execute(self, run_id: UUID, raw_output: Any) -> bool:
            nonlocal published3
            published3 = dict(raw_output)
            return True

    pipeline3 = ExecuteReviewRun(
        Processor(),
        RepositoryAgentsMissing(),
        ModelWithRuleName("service-defaults:fastapi"),
        Publisher3(),
        reflection_pass=ReflectionPass(FakeVerifierModel({"decisions": []})),
    )
    assert asyncio.run(pipeline3.execute(RUN_ID)) is True
    assert published3["findings"] == []

    # 4. Positive case: rules.md is present in snapshot, candidate with .review/rules.md
    # passes attribution validation
    class RepositoryRulesPresent:
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
                policy_snapshot=_make_test_snapshot(),  # rules is present
            )

    class FakePassWithRulesVerifier:
        def max_prompt_tokens(self) -> int | None:
            return None

        async def verify_recorded(self, *, candidates: Any, context: Any) -> Any:
            from app.modules.reviews.application.reflection_pass import VerificationRecord

            decisions = await self.verify(candidates=candidates, context=context)
            return VerificationRecord(decisions, tuple(c.candidate_id for c in candidates))

        async def verify(self, *, candidates: Any, context: Any) -> Any:
            from app.modules.reviews.application.reflection_pass import VerificationDecision

            return (
                VerificationDecision(
                    candidate_id=candidates[0].candidate_id,
                    status="supported",
                    reason="Rules violation confirmed.",
                    evidence_refs=[EvidenceRef(block_id=MAIN_BLOCK_ID, start_line=10, end_line=10)],
                    requirement_refs=[
                        RequirementRef(
                            block_id="policy-rules",
                            start_line=1,
                            end_line=2,
                            check_id=None,
                        )
                    ],
                ),
            )

    pipeline4 = ExecuteReviewRun(
        Processor(),
        RepositoryRulesPresent(),
        ModelWithRuleName(".review/rules.md"),
        Publisher(),
        reflection_pass=FakePassWithRulesVerifier(),  # type: ignore[arg-type]
    )
    success = asyncio.run(pipeline4.execute(RUN_ID))
    assert success is True


def test_execute_review_run_snapshot_zero_findings_deterministic_summary() -> None:
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
                conventions=RepoConventions(("Use services.",), ("Test paths.",)),
                changed_files=(
                    ChangedFile("app/main.py", "modified", (DiffLine(10, "added", "x = 1"),)),
                ),
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
            )

        async def draft_review(self, *, context: ReviewContext) -> dict[str, Any]:
            # Model returns 0 candidates and an arbitrary raw summary
            return {
                "findings": [],
                "summary": {
                    "problem": "Raw model summary problem.",
                    "done_well": "Raw model summary done well.",
                    "effort": "large",
                },
            }

    published_output: dict[str, Any] = {}

    class Publisher:
        async def execute(
            self, run_id: UUID, raw_output: Mapping[str, object] | str | bytes
        ) -> bool:
            nonlocal published_output
            published_output = dict(raw_output) if isinstance(raw_output, Mapping) else {}
            return True

    fake_verifier_model = FakeVerifierModel({"decisions": []})
    reflection_pass = ReflectionPass(fake_verifier_model)
    pipeline = ExecuteReviewRun(
        Processor(),
        Repository(),
        Model(),
        Publisher(),
        reflection_pass=reflection_pass,
    )

    success = asyncio.run(pipeline.execute(RUN_ID))
    assert success is True
    # Verifier MUST be skipped (0 calls)
    assert fake_verifier_model.call_count == 0
    assert published_output["findings"] == []
    # Raw summary must NEVER be published (CVS §8.2)
    assert "Raw model summary problem." not in str(published_output["summary"])
    assert (
        published_output["summary"]["problem"]
        == "No verified issues were found in the evaluated changes."
    )
    assert published_output["summary"]["effort"] == "none"
    assert (
        published_output["summary"]["done_well"]
        == "Code analysis and policy verification were completed across all evaluated changes."
    )


def test_execute_review_run_severity_threshold_critical_drops_medium_clean_summary() -> None:
    """Probe from Issue #219 Part 4:
    Severity threshold is critical, model returns 1 medium finding (supported by verifier)
    and 1 contradicted finding.
    Pass 6 filters out the medium finding because severity is below critical.
    Outcome: verdict clean, 0 findings, and summary built exclusively from post-threshold findings.
    """

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
                conventions=RepoConventions(("Use services.",), ("Test paths.",)),
                changed_files=(
                    ChangedFile("app/main.py", "modified", (DiffLine(10, "added", "x = 1"),)),
                ),
                omitted_files=(),
                policy_snapshot=_make_test_snapshot(),
                severity_threshold="critical",
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
            )

        async def draft_review(self, *, context: ReviewContext) -> dict[str, Any]:
            return {
                "findings": [
                    {
                        "path": "app/main.py",
                        "line": 10,
                        "start_line": None,
                        "severity": "medium",
                        "category": "correctness",
                        "title": "Medium finding",
                        "body": "Minor correctness defect.",
                        "suggestion": None,
                        "confidence": 0.8,
                        "rule_name": None,
                        "evidence_refs": [
                            {"block_id": MAIN_BLOCK_ID, "start_line": 10, "end_line": 10}
                        ],
                    },
                    {
                        "path": "app/main.py",
                        "line": 10,
                        "start_line": None,
                        "severity": "low",
                        "category": "correctness",
                        "title": "Contradicted finding",
                        "body": "False positive.",
                        "suggestion": None,
                        "confidence": 0.5,
                        "rule_name": None,
                        "evidence_refs": [
                            {"block_id": MAIN_BLOCK_ID, "start_line": 10, "end_line": 10}
                        ],
                    },
                ],
                "summary": {
                    "problem": "Model claims 2 issues.",
                    "done_well": "Clean style.",
                    "effort": "small",
                },
            }

    published_output: Any = None

    class Publisher:
        async def execute(
            self, run_id: UUID, raw_output: Mapping[str, object] | str | bytes
        ) -> bool:
            nonlocal published_output
            published_output = raw_output
            return True

    class FakeVerifierModelWithDecisions:
        async def verify(
            self, *, system_prompt: str, user_prompt: str, **_: object
        ) -> Mapping[str, Any]:
            cand_ids = re.findall(r'candidate candidate_id="([^"]+)"', user_prompt)
            return {
                "decisions": [
                    {
                        "candidate_id": cand_ids[0],
                        "status": "supported",
                        "reason": "Medium defect confirmed.",
                        "evidence_refs": [
                            {"block_id": MAIN_BLOCK_ID, "start_line": 10, "end_line": 10}
                        ],
                        "requirement_refs": [],
                    },
                    {
                        "candidate_id": cand_ids[1],
                        "status": "contradicted",
                        "reason": "False positive refuted.",
                        "evidence_refs": [
                            {"block_id": MAIN_BLOCK_ID, "start_line": 10, "end_line": 10}
                        ],
                        "requirement_refs": [],
                    },
                ]
            }

    reflection_pass = ReflectionPass(FakeVerifierModelWithDecisions())
    pipeline = ExecuteReviewRun(
        Processor(),
        Repository(),
        Model(),
        Publisher(),
        reflection_pass=reflection_pass,
    )

    success = asyncio.run(pipeline.execute(RUN_ID))
    assert success is True
    # All post-threshold findings are dropped
    assert published_output["findings"] == []
    assert len(published_output.dropped_findings) == 2
    assert {d.drop_reason for d in published_output.dropped_findings} == {
        "contradicted",
        "severity_threshold",
    }
    assert published_output.severity_threshold == "critical"
    # Summary MUST reflect 0 post-threshold issues (NOT "Identified 1 verified issue")
    assert (
        published_output["summary"]["problem"]
        == "No verified issues meeting the severity threshold were found (1 below threshold)."
    )
    assert published_output["summary"]["effort"] == "none"
    assert (
        published_output["summary"]["done_well"]
        == "Code analysis and policy verification were completed across all evaluated changes."
    )


def test_execute_review_multi_files_snapshot_path_no_overflow() -> None:
    draft_calls = 0

    class Processor:
        async def prepare(self, run_id: UUID) -> GeneratedConventions:
            return conventions()

    class Repository:
        async def get_review_prompt_input(
            self, run_id: UUID, saved: GeneratedConventions
        ) -> ReviewPromptInput:
            line_content = "x = 100 + 200 # line calculation " + "y" * 55
            lines = tuple(DiffLine(i, "added", line_content) for i in range(1, 801))
            files = tuple(ChangedFile(f"app/file_{i}.py", "added", lines) for i in range(1, 4))
            return ReviewPromptInput(
                system="Review carefully.",
                rules=(),
                agents_md=None,
                conventions=RepoConventions(("Use services.",), ()),
                changed_files=files,
                omitted_files=(),
                policy_snapshot=_make_test_snapshot(),
            )

    class Model:
        async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
            return PullRequestMeta(
                "PR",
                None,
                "author",
                "feat",
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
            nonlocal draft_calls
            draft_calls += 1
            return {
                "findings": [],
                "summary": {"problem": "p", "done_well": "d", "effort": "none"},
            }

    class Publisher:
        async def execute(self, run_id: UUID, raw_output: Any) -> bool:
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
    assert draft_calls == 1


def test_execute_review_all_supported_never_publishes_raw_summary() -> None:
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
                        "title": "Auth issue",
                        "body": "Missing check",
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
                "summary": {
                    "problem": "RAW SUMMARY THAT MUST BE DISCARDED",
                    "done_well": "RAW WELL",
                    "effort": "none",
                },
            }

    class Publisher:
        async def execute(self, run_id: UUID, raw_output: Any) -> bool:
            nonlocal published
            published = dict(raw_output)
            return True

    fake_verifier = FakeVerifierModel(
        {
            "decisions": [
                {
                    "candidate_id": "dummy",
                    "status": "supported",
                    "reason": "Verified",
                    "evidence_refs": [
                        {"block_id": file_block_id("app/main.py"), "start_line": 10, "end_line": 10}
                    ],
                    "requirement_refs": [],
                }
            ]
        }
    )

    async def dynamic_verify(
        *, system_prompt: str, user_prompt: str, **_: object
    ) -> dict[str, Any]:
        match = re.search(r'candidate_id="([^"]+)"', user_prompt)
        cand_id = match.group(1) if match else "dummy"
        return {
            "decisions": [
                {
                    "candidate_id": cand_id,
                    "status": "supported",
                    "reason": "Verified",
                    "evidence_refs": [
                        {"block_id": file_block_id("app/main.py"), "start_line": 10, "end_line": 10}
                    ],
                    "requirement_refs": [],
                }
            ]
        }

    fake_verifier.verify = dynamic_verify  # type: ignore

    pipeline = ExecuteReviewRun(
        Processor(),
        Repository(),
        Model(),
        Publisher(),
        reflection_pass=ReflectionPass(fake_verifier),
    )
    success = asyncio.run(pipeline.execute(RUN_ID))
    assert success is True
    assert len(published["findings"]) == 1
    # Raw summary was not published; deterministic summary was used
    assert published["summary"]["problem"] != "RAW SUMMARY THAT MUST BE DISCARDED"
    assert "Identified 1 verified issue (security)" in published["summary"]["problem"]


def test_execute_review_two_candidates_one_invalid_ref_valid_reaches_verifier() -> None:
    published: Any = {}
    verifier_received_candidates: list[Any] = []

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
                        "severity": "critical",
                        "category": "security",
                        "title": "Invalid ref issue",
                        "body": "Bad ref",
                        "suggestion": None,
                        "confidence": 0.9,
                        "rule_name": None,
                        "evidence_refs": [
                            {"block_id": "non-existent-block", "start_line": 10, "end_line": 10}
                        ],
                    },
                    {
                        "path": "app/main.py",
                        "line": 10,
                        "start_line": None,
                        "severity": "high",
                        "category": "security",
                        "title": "Valid ref issue",
                        "body": "Good ref",
                        "suggestion": None,
                        "confidence": 0.85,
                        "rule_name": None,
                        "evidence_refs": [
                            {
                                "block_id": file_block_id("app/main.py"),
                                "start_line": 10,
                                "end_line": 10,
                            }
                        ],
                    },
                ],
                "summary": {"problem": "p", "done_well": "d", "effort": "none"},
            }

    class Publisher:
        async def execute(self, run_id: UUID, raw_output: Any) -> bool:
            nonlocal published
            published = raw_output
            return True

    class InspectingVerifierModel:
        async def verify(
            self, *, system_prompt: str, user_prompt: str, **_: object
        ) -> dict[str, Any]:
            cand_ids = re.findall(r'candidate_id="([^"]+)"', user_prompt)
            verifier_received_candidates.extend(cand_ids)
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

    pipeline = ExecuteReviewRun(
        Processor(),
        Repository(),
        Model(),
        Publisher(),
        reflection_pass=ReflectionPass(InspectingVerifierModel()),
    )
    success = asyncio.run(pipeline.execute(RUN_ID))
    assert success is True
    # Only 1 candidate reached the verifier (the valid one)
    assert len(verifier_received_candidates) == 1
    assert len(published["findings"]) == 1
    assert published["findings"][0]["title"] == "Valid ref issue"
    assert len(published.dropped_findings) == 1
    assert published.dropped_findings[0].drop_reason == "evidence_not_shown"


def test_execute_review_verifier_input_free_of_lint_duplicates_and_unknown_path() -> None:
    published: Any = None
    verifier_received_candidates: list[str] = []

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
                    # 1. Valid
                    {
                        "path": "app/main.py",
                        "line": 10,
                        "start_line": None,
                        "severity": "critical",
                        "category": "security",
                        "title": "Legitimate security vulnerability",
                        "body": "Unsanitized input execution",
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
                    },
                    # 2. Unknown path
                    {
                        "path": "app/ghost.py",
                        "line": 10,
                        "start_line": None,
                        "severity": "high",
                        "category": "security",
                        "title": "Ghost path issue",
                        "body": "File does not exist",
                        "suggestion": None,
                        "confidence": 0.9,
                        "rule_name": None,
                        "evidence_refs": [
                            {
                                "block_id": file_block_id("app/main.py"),
                                "start_line": 10,
                                "end_line": 10,
                            }
                        ],
                    },
                    # 3. Line absent
                    {
                        "path": "app/main.py",
                        "line": 9999,
                        "start_line": None,
                        "severity": "high",
                        "category": "security",
                        "title": "Line absent issue",
                        "body": "Line does not exist in hunk",
                        "suggestion": None,
                        "confidence": 0.9,
                        "rule_name": None,
                        "evidence_refs": [
                            {
                                "block_id": file_block_id("app/main.py"),
                                "start_line": 10,
                                "end_line": 10,
                            }
                        ],
                    },
                    # 4. Duplicate of #1
                    {
                        "path": "app/main.py",
                        "line": 10,
                        "start_line": None,
                        "severity": "critical",
                        "category": "security",
                        "title": "Legitimate security vulnerability",
                        "body": "Unsanitized input execution",
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
                    },
                    # 5. Lint pattern
                    {
                        "path": "app/main.py",
                        "line": 10,
                        "start_line": None,
                        "severity": "low",
                        "category": "readability",
                        "title": "Whitespace nit",
                        "body": "Extra whitespace formatting nit",
                        "suggestion": None,
                        "confidence": 0.9,
                        "rule_name": None,
                        "evidence_refs": [
                            {
                                "block_id": file_block_id("app/main.py"),
                                "start_line": 10,
                                "end_line": 10,
                            }
                        ],
                    },
                ],
                "summary": {"problem": "p", "done_well": "d", "effort": "none"},
            }

    class Publisher:
        async def execute(self, run_id: UUID, raw_output: Any) -> bool:
            nonlocal published
            published = raw_output
            return True

    class InspectingVerifierModel:
        async def verify(
            self, *, system_prompt: str, user_prompt: str, **_: object
        ) -> dict[str, Any]:
            cand_ids = re.findall(r'candidate_id="([^"]+)"', user_prompt)
            verifier_received_candidates.extend(cand_ids)
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

    pipeline = ExecuteReviewRun(
        Processor(),
        Repository(),
        Model(),
        Publisher(),
        reflection_pass=ReflectionPass(InspectingVerifierModel()),
    )
    success = asyncio.run(pipeline.execute(RUN_ID))
    assert success is True
    # Verifier received exactly 1 candidate (the valid, non-duplicate, existing-path candidate)
    assert len(verifier_received_candidates) == 1
    assert len(published["findings"]) == 1
    assert len(published.dropped_findings) == 4
    reasons = {d.drop_reason for d in published.dropped_findings}
    assert "unknown_path" in reasons
    assert "line_absent" in reasons
    assert "duplicate" in reasons
    assert "lint_pattern" in reasons
    assert [d.position for d in published.dropped_findings] == [1, 2, 4, 3]


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

    class Publisher:
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

    class Publisher:
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

    class Publisher:
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

    class Publisher:
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

    class Publisher:
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

    class Publisher:
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

    def __init__(self, snapshot: PolicySnapshot | None) -> None:
        self.snapshot_for_llm = snapshot

    def excluded(self, path: str) -> bool:
        return False


class _BoundPipeline:
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

    with pytest.raises(PolicyError) as raised:
        asyncio.run(run.execute(RUN_ID))

    assert raised.value.code == PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR
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


class _F18Publisher:
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
