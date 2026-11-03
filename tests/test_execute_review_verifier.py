"""Verifier decisions and final review output behavior."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from typing import Any
from uuid import UUID

import pytest

from app.modules.reviews.application.conventions import GeneratedConventions
from app.modules.reviews.application.execute_review import (
    ExecuteReviewRun,
    ReviewPromptInput,
)
from app.modules.reviews.application.prompt_builder import (
    ChangedFile,
    DiffLine,
    PullRequestMeta,
    RepoConventions,
    ReviewContext,
    file_block_id,
)
from app.modules.reviews.application.reflection_pass import (
    ReflectionPass,
)
from app.modules.reviews.application.review_output import (
    EvidenceRef,
    InvalidReviewOutput,
    RequirementRef,
)
from tests.execute_review_fakes import (
    MAIN_BLOCK_ID,
    RUN_ID,
    FakeVerifierModel,
    _make_test_snapshot,
    conventions,
)
from tests.policy_snapshot_fakes import (
    make_policy_document_missing,
)


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
