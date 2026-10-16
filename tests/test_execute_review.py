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
)
from app.modules.reviews.application.reflection_pass import ReflectionPass

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
                            {"block_id": "file-app-main-py", "start_line": 10, "end_line": 10}
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
                            {"block_id": "file-app-main-py", "start_line": 10, "end_line": 10}
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
                        {"block_id": "file-app-main-py", "start_line": 10, "end_line": 10}
                    ],
                    "requirement_refs": [],
                },
                {
                    "candidate_id": "cand-2",
                    "status": "contradicted",
                    "reason": "Variable does not exist and no leak occurs in visible context.",
                    "evidence_refs": [
                        {"block_id": "file-app-main-py", "start_line": 10, "end_line": 10}
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
                            {"block_id": "file-app-main-py", "start_line": 10, "end_line": 10}
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
                            {"block_id": "file-app-main-py", "start_line": 10, "end_line": 10}
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
        reflection_pass=None,  # Missing reflection_pass with snapshot present
    )

    with pytest.raises(PolicyError) as exc_info:
        asyncio.run(pipeline.execute(RUN_ID))
    assert exc_info.value.code == PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR
    assert "Reflection pass is required" in exc_info.value.message


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
        async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
            return PullRequestMeta("Title", None, "u", "b", "m", (), 1, 1, 0, False, False)

        async def draft_review(self, *, context: ReviewContext) -> dict[str, Any]:
            return {"findings": [], "summary": {"problem": "p", "done_well": "d", "effort": "none"}}

    class Publisher:
        async def execute(self, run_id: UUID, raw_output: Any) -> bool:
            return True

    fake_verifier_model = FakeVerifierModel({"decisions": []})
    pipeline = ExecuteReviewRun(
        Processor(),
        Repository(),
        Model(),
        Publisher(),
        reflection_pass=ReflectionPass(fake_verifier_model),
    )

    with pytest.raises(PolicyError) as exc_info:
        asyncio.run(pipeline.execute(RUN_ID))
    assert exc_info.value.code == PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR
    assert "Policy snapshot is not ready" in exc_info.value.message


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
        async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
            return PullRequestMeta("Title", None, "u", "b", "m", (), 1, 1, 0, False, False)

        async def draft_review(self, *, context: ReviewContext) -> dict[str, Any]:
            return {"findings": [], "summary": {"problem": "p", "done_well": "d", "effort": "none"}}

    class Publisher:
        async def execute(self, run_id: UUID, raw_output: Any) -> bool:
            return True

    fake_verifier_model = FakeVerifierModel({"decisions": []})
    pipeline = ExecuteReviewRun(
        Processor(),
        Repository(),
        Model(),
        Publisher(),
        reflection_pass=ReflectionPass(fake_verifier_model),
    )

    with pytest.raises(PolicyError) as exc_info:
        asyncio.run(pipeline.execute(RUN_ID))
    assert exc_info.value.code == PolicyErrorCode.PROMPT_BUDGET_EXCEEDED


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
        async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
            return PullRequestMeta("Title", None, "u", "b", "m", (), 1, 1, 0, False, False)

        async def draft_review(self, *, context: ReviewContext) -> dict[str, Any]:
            return {"findings": [], "summary": {"problem": "p", "done_well": "d", "effort": "none"}}

    class Publisher:
        async def execute(self, run_id: UUID, raw_output: Any) -> bool:
            return True

    fake_verifier_model = FakeVerifierModel({"decisions": []})
    pipeline = ExecuteReviewRun(
        Processor(),
        Repository(),
        Model(),
        Publisher(),
        reflection_pass=ReflectionPass(fake_verifier_model),
    )

    with pytest.raises(RunFailure) as exc_info:
        asyncio.run(pipeline.execute(RUN_ID))
    assert exc_info.value.error_code == "llm_context_overflow"


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
                            {"block_id": "file-app-main-py", "start_line": 10, "end_line": 10}
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
                        {"block_id": "file-app-main-py", "start_line": 10, "end_line": 10}
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
    assert summary["done_well"] == "Good code style and structure."
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
            return {
                "findings": [
                    {
                        "path": "app/main.py",
                        "line": 5,
                        "start_line": None,
                        "severity": "medium",
                        "category": "readability",
                        "title": "Disallowed print",
                        "body": "Print statement found.",
                        "suggestion": None,
                        "confidence": 0.95,
                        "rule_name": None,
                        "evidence_refs": [
                            {"block_id": "file-app-main-py", "start_line": 5, "end_line": 5}
                        ],
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

    fake_verifier_model = FakeVerifierModel(
        {
            "decisions": [
                {
                    "candidate_id": "cand-1",
                    "status": "supported",
                    "reason": "Direct print call matches rule.",
                    "evidence_refs": [
                        {"block_id": "file-app-main-py", "start_line": 5, "end_line": 5}
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
    assert len(published_output["findings"]) == 1
    assert published_output["findings"][0]["title"] == "Disallowed print"
