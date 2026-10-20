"""Behavioural contract for the ordinary review-worker completion pipeline."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from typing import Any
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
    ExecutionProfile,
    PolicySnapshot,
    PromptBindings,
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
from app.modules.reviews.application.review_output import (
    EvidenceRef,
    InvalidReviewOutput,
    RequirementRef,
)
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
        defaults_checksum="2" * 64,
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

    async def verify(self, *, system_prompt: str, user_prompt: str) -> Mapping[str, Any]:
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
        async def verify(self, *, system_prompt: str, user_prompt: str) -> Mapping[str, Any]:
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


def test_execute_review_fail_closed_when_budget_exceeded() -> None:
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
                policy_snapshot=_make_test_snapshot(
                    max_policy_tokens=1
                ),  # Tiny policy limit triggers budget error
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
    assert exc_info.value.code == PolicyErrorCode.PROMPT_BUDGET_EXCEEDED
    assert exc_info.value.limit == 1
    assert exc_info.value.actual is not None and exc_info.value.actual > 1
    assert "допустимо 1" in str(exc_info.value)
    assert model.draft_calls == 0
    assert publisher.publish_calls == 0


def test_execute_review_fail_closed_when_context_overflow() -> None:
    from app.modules.reviews.application.run_failures import RunFailure

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
                policy_snapshot=_make_test_snapshot(
                    max_input_tokens=10
                ),  # Tiny limit triggers overflow
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

    with pytest.raises(RunFailure) as exc_info:
        asyncio.run(pipeline.execute(RUN_ID))
    assert exc_info.value.error_code == "llm_context_overflow"
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
        async def verify(self, *, system_prompt: str, user_prompt: str) -> Mapping[str, Any]:
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
        async def verify(self, *, system_prompt: str, user_prompt: str) -> Mapping[str, Any]:
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
        async def verify(self, *, system_prompt: str, user_prompt: str) -> Mapping[str, Any]:
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
        async def verify(self, *, system_prompt: str, user_prompt: str) -> dict[str, Any]:
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


def test_execute_review_legacy_run_with_reflection_pass_logs_warning(
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
    warning_records = [
        r for r in caplog.records if "execute_review_legacy_run_without_snapshot" in r.message
    ]
    assert len(warning_records) == 1
    assert str(RUN_ID) in warning_records[0].message

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

    published_output: dict[str, Any] = {}

    class Publisher:
        async def execute(
            self, run_id: UUID, raw_output: Mapping[str, object] | str | bytes
        ) -> bool:
            nonlocal published_output
            published_output = dict(raw_output) if isinstance(raw_output, Mapping) else {}
            return True

    class FakeVerifierModelWithDecisions:
        async def verify(self, *, system_prompt: str, user_prompt: str) -> Mapping[str, Any]:
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


def test_execute_review_run_2500_lines_diff_fits_without_premature_overflow() -> None:
    """Probe from Issue #219 Part 2:
    A 2,500-line diff file produces ~74k raw chars/tokens.
    Previously, pre-send guard evaluated before fit_review_context and aborted with overflow.
    Now, post-fit pre-send guard ensures the context is fitted first and passes within budget.
    """

    class Processor:
        async def prepare(self, run_id: UUID) -> GeneratedConventions:
            return conventions()

    class Repository:
        async def get_review_prompt_input(
            self, run_id: UUID, saved: GeneratedConventions
        ) -> ReviewPromptInput:
            line_content = "x = 100 + 200 # line calculation " + "y" * 55  # ~88 chars
            lines = tuple(DiffLine(i, "added", line_content) for i in range(1, 2501))
            return ReviewPromptInput(
                system="Review carefully.",
                rules=(),
                agents_md=None,
                conventions=RepoConventions(("Use services.",), ("Test paths.",)),
                changed_files=(ChangedFile("app/large.py", "modified", lines),),
                omitted_files=(),
                policy_snapshot=_make_test_snapshot(max_input_tokens=52_000),
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
                1,
                2500,
                0,
                False,
                False,
                head_sha="sha_head",
            )

        async def draft_review(self, *, context: ReviewContext) -> dict[str, Any]:
            assert len(context.changed_files[0].lines) < 2500
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
    assert published_output["findings"] == []
    assert (
        published_output["summary"]["problem"]
        == "No verified issues were found in the evaluated changes."
    )
