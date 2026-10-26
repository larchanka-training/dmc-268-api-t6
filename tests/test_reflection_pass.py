"""Unit tests for Reflection Pass and verifier engine (Issue #120)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from app.modules.reviews.application.llm import LlmCallFailed, LlmErrorCode
from app.modules.reviews.application.policy_errors import PolicyError
from app.modules.reviews.application.policy_snapshot import (
    CallManifest,
    PolicySnapshot,
)
from app.modules.reviews.application.prompt_builder import (
    DEFAULT_BACKEND_V1_CHECKSUM,
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
    CandidateFindingItem,
    ReflectionPass,
    VerificationDecision,
    VerificationInvalidOutputError,
    VerificationStatus,
    VerificationUnavailableError,
    VerifierPromptBuilder,
    VerifierReply,
    filter_verified_candidates,
    verify_verifier_mandatory_budget,
)
from app.modules.reviews.application.review_output import (
    EvidenceRef,
    RequirementRef,
    ReviewFindingV2,
)
from app.modules.reviews.application.run_failures import (
    RETRYABLE_ERROR_CODES,
    RetryDelays,
    RunFailure,
    classify_failure,
)
from tests.policy_snapshot_fakes import (
    make_complete_execution_profile,
    make_complete_prompt_bindings,
    make_policy_document_missing,
    make_policy_document_present,
)


class CustomModel:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def verify(
        self, *, system_prompt: str, user_prompt: str, **_: object
    ) -> Mapping[str, Any] | str:
        raise self._exc


def make_snapshot() -> PolicySnapshot:
    return PolicySnapshot(
        schema_version=1,
        snapshot_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        repository_id=uuid.uuid4(),
        policy_base_sha="base_123",
        rules=make_policy_document_present(
            ".review/rules.md", "# Rules\n1. Check SQL injection.\n2. Handle errors.\n"
        ),
        agents=make_policy_document_missing("AGENTS.md"),
        ignore_patterns=(),
        ignore_digest="id_0",
        parser_profile="rules-markdown/v1",
        parser_version="4.0.0",
        matcher_version="review-ignore/v1",
        limits_version="policy-limits/v1",
        builtin_exclusions_version="builtin-exclusions/v1",
        priority_matcher_version="priority-matcher/v1",
        service_defaults_rule_version_id=uuid.uuid4(),
        defaults_checksum=DEFAULT_BACKEND_V1_CHECKSUM,
        prompt_bindings=make_complete_prompt_bindings(),
        execution_profile=make_complete_execution_profile(),
        policy_digest="digest_123",
    )


def make_candidate(
    candidate_id: str,
    *,
    path: str = "app/query.py",
    rule_name: str | None = ".review/rules.md",
    title: str = "SQL injection",
    evidence_refs: list[EvidenceRef] | None = None,
) -> CandidateFindingItem:
    if evidence_refs is None:
        evidence_refs = [EvidenceRef(block_id=file_block_id(path), start_line=10, end_line=15)]
    finding = ReviewFindingV2(
        path=path,
        line=15,
        start_line=10,
        severity="high",
        category="security",
        title=title,
        body="Raw query formatting",
        suggestion=None,
        confidence=0.9,
        rule_name=rule_name,
        evidence_refs=evidence_refs,
    )
    return CandidateFindingItem(candidate_id=candidate_id, finding=finding)


class FakeVerifierModel:
    def __init__(self, response_payload: Mapping[str, Any] | str | bytes) -> None:
        self.response_payload = response_payload
        self.call_count = 0
        self.last_system_prompt = ""
        self.last_user_prompt = ""

    async def verify(
        self, *, system_prompt: str, user_prompt: str, **_: object
    ) -> Mapping[str, Any] | str | bytes:
        self.call_count += 1
        self.last_system_prompt = system_prompt
        self.last_user_prompt = user_prompt
        return self.response_payload


def test_verification_decision_validates_fields() -> None:
    cand_id = str(uuid.uuid4())
    decision = VerificationDecision(
        candidate_id=cand_id,
        status="supported",
        reason="Demonstrated violation in the diff.",
        evidence_refs=[EvidenceRef(block_id="code-1", start_line=10, end_line=15)],
        requirement_refs=[
            RequirementRef(block_id="policy-rules", start_line=1, end_line=2, check_id=None)
        ],
    )
    assert decision.candidate_id == cand_id
    assert decision.status == "supported"

    # reason cannot exceed 600 chars
    with pytest.raises(ValidationError):
        VerificationDecision(
            candidate_id=cand_id,
            status="supported",
            reason="x" * 601,
            evidence_refs=[EvidenceRef(block_id="code-1", start_line=1, end_line=1)],
            requirement_refs=[],
        )

    # status supported requires at least 1 evidence_ref
    with pytest.raises(ValidationError, match="requires at least 1 evidence_ref"):
        VerificationDecision(
            candidate_id=cand_id,
            status="supported",
            reason="valid reason",
            evidence_refs=[],
            requirement_refs=[],
        )

    # status contradicted requires at least 1 evidence_ref
    with pytest.raises(ValidationError, match="requires at least 1 evidence_ref"):
        VerificationDecision(
            candidate_id=cand_id,
            status="contradicted",
            reason="valid reason",
            evidence_refs=[],
            requirement_refs=[],
        )


def test_verifier_prompt_builder_custom_prompts() -> None:
    builder_custom = VerifierPromptBuilder(system_prompt="Custom prompt")
    assert builder_custom._system_prompt == "Custom prompt"
    assert builder_custom._raw_prompt == "Custom prompt"

    builder_default = VerifierPromptBuilder()
    assert builder_default._raw_prompt.startswith("---")
    assert not builder_default._system_prompt.startswith("---")
    assert "Reflection Pass" in builder_default._system_prompt


def test_verifier_prompt_builder_renders_candidates_and_policy() -> None:
    snapshot = make_snapshot()
    context = ReviewContext(
        system="SYSTEM",
        rules=(),
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=PullRequestMeta("T", None, "u", "b", "main", (), 1, 1, 0, False, False),
        changed_files=(
            ChangedFile("app/query.py", "modified", (DiffLine(10, "added", "query = f'SELECT'"),)),
        ),
        omitted_files=(),
        policy_snapshot=snapshot,
    )
    cand = make_candidate("cand-1")

    builder = VerifierPromptBuilder()
    call_id = uuid.uuid4()
    prompt, manifest = builder.build_verifier_prompt(
        candidates=(cand,),
        context=context,
        call_id=call_id,
    )

    assert '<candidate_findings count="1">' in prompt.user
    assert 'candidate_id="cand-1"' in prompt.user
    assert "<repository_rules" in prompt.user
    assert "<code_context>" in prompt.user
    assert manifest.call_id == call_id
    assert manifest.stage == "verification"
    assert manifest.get_block("policy-rules") is not None
    diff_block = manifest.get_block(file_block_id("app/query.py"))
    assert diff_block is not None
    assert diff_block.source_kind == "source_code"
    cand_block = manifest.get_block("cand-1")
    assert cand_block is not None
    assert cand_block.source_kind == "candidate_findings"


@pytest.mark.anyio
async def test_reflection_pass_skips_when_no_candidates() -> None:
    fake_model = FakeVerifierModel({})
    verifier = ReflectionPass(fake_model)
    snapshot = make_snapshot()
    context = ReviewContext(
        system="SYSTEM",
        rules=(),
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=PullRequestMeta("T", None, "u", "b", "main", (), 0, 0, 0, False, False),
        changed_files=(),
        omitted_files=(),
        policy_snapshot=snapshot,
    )

    decisions = await verifier.verify(candidates=(), context=context)
    assert decisions == ()
    assert fake_model.call_count == 0


@pytest.mark.anyio
async def test_reflection_pass_verifies_and_filters_candidates() -> None:
    cand1 = make_candidate("c-1", title="True Defect")
    cand2 = make_candidate("c-2", title="Hallucinated Defect")
    cand3 = make_candidate("c-3", title="Unclear Assertion", rule_name=None)
    query_block_id = file_block_id("app/query.py")

    fake_response = {
        "decisions": [
            {
                "candidate_id": "c-1",
                "status": "supported",
                "reason": "Direct SQL injection visible on line 10.",
                "evidence_refs": [{"block_id": query_block_id, "start_line": 10, "end_line": 10}],
                "requirement_refs": [
                    {
                        "block_id": "policy-rules",
                        "start_line": 1,
                        "end_line": 2,
                        "check_id": None,
                    }
                ],
            },
            {
                "candidate_id": "c-2",
                "status": "contradicted",
                "reason": "Parameterization is handled in helper function.",
                "evidence_refs": [{"block_id": query_block_id, "start_line": 10, "end_line": 10}],
                "requirement_refs": [
                    {
                        "block_id": "policy-rules",
                        "start_line": 1,
                        "end_line": 2,
                        "check_id": None,
                    }
                ],
            },
            {
                "candidate_id": "c-3",
                "status": "insufficient_context",
                "reason": "External database schema not available.",
                "evidence_refs": [],
                "requirement_refs": [],
            },
        ]
    }
    fake_model = FakeVerifierModel(fake_response)
    verifier = ReflectionPass(fake_model)

    snapshot = make_snapshot()
    context = ReviewContext(
        system="SYSTEM",
        rules=(),
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=PullRequestMeta("T", None, "u", "b", "main", (), 1, 1, 0, False, False),
        changed_files=(
            ChangedFile("app/query.py", "modified", (DiffLine(10, "added", "query = f'SELECT'"),)),
        ),
        omitted_files=(),
        policy_snapshot=snapshot,
    )

    decisions = await verifier.verify(candidates=(cand1, cand2, cand3), context=context)
    assert len(decisions) == 3
    assert fake_model.call_count == 1

    supported, rejected = filter_verified_candidates(
        candidates=(cand1, cand2, cand3), decisions=decisions
    )
    # Only c-1 is supported
    assert [c.candidate_id for c in supported] == ["c-1"]
    # c-2 and c-3 are rejected with reasons
    assert len(rejected) == 2
    assert rejected[0][0].candidate_id == "c-2"
    assert rejected[0][1] == "contradicted"
    assert rejected[1][0].candidate_id == "c-3"
    assert rejected[1][1] == "insufficient_context"


@pytest.mark.anyio
async def test_reflection_pass_raises_on_missing_candidate_decision() -> None:
    cand1 = make_candidate("c-1")
    cand2 = make_candidate("c-2")
    query_block_id = file_block_id("app/query.py")

    # Missing decision for c-2
    fake_response = {
        "decisions": [
            {
                "candidate_id": "c-1",
                "status": "supported",
                "reason": "OK",
                "evidence_refs": [{"block_id": query_block_id, "start_line": 10, "end_line": 10}],
                "requirement_refs": [],
            }
        ]
    }
    fake_model = FakeVerifierModel(fake_response)
    verifier = ReflectionPass(fake_model)

    snapshot = make_snapshot()
    context = ReviewContext(
        system="SYSTEM",
        rules=(),
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=PullRequestMeta("T", None, "u", "b", "main", (), 1, 1, 0, False, False),
        changed_files=(ChangedFile("app/query.py", "modified", (DiffLine(10, "added", "x = 1"),)),),
        omitted_files=(),
        policy_snapshot=snapshot,
    )

    with pytest.raises(
        VerificationInvalidOutputError,
        match="Verifier returned decisions for 1 items, expected 2",
    ):
        await verifier.verify(candidates=(cand1, cand2), context=context)


@pytest.mark.anyio
async def test_reflection_pass_rejects_duplicate_candidate_decisions() -> None:
    cand1 = make_candidate("c-1")
    cand2 = make_candidate("c-2")
    query_block_id = file_block_id("app/query.py")

    # 1. 2 candidates, 2 decisions with same candidate_id "c-1" (missing c-2)
    fake_response = {
        "decisions": [
            {
                "candidate_id": "c-1",
                "status": "supported",
                "reason": "OK 1",
                "evidence_refs": [{"block_id": query_block_id, "start_line": 10, "end_line": 10}],
                "requirement_refs": [],
            },
            {
                "candidate_id": "c-1",
                "status": "supported",
                "reason": "OK 2",
                "evidence_refs": [{"block_id": query_block_id, "start_line": 10, "end_line": 10}],
                "requirement_refs": [],
            },
        ]
    }
    verifier = ReflectionPass(FakeVerifierModel(fake_response))

    snapshot = make_snapshot()
    context = ReviewContext(
        system="SYSTEM",
        rules=(),
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=PullRequestMeta("T", None, "u", "b", "main", (), 1, 1, 0, False, False),
        changed_files=(ChangedFile("app/query.py", "modified", (DiffLine(10, "added", "x = 1"),)),),
        omitted_files=(),
        policy_snapshot=snapshot,
    )

    with pytest.raises(
        VerificationInvalidOutputError,
        match="Verifier returned decisions for 2 items, expected 2",
    ):
        await verifier.verify(candidates=(cand1, cand2), context=context)

    # 2. 1 candidate, 2 duplicate decisions for that candidate
    with pytest.raises(
        VerificationInvalidOutputError,
        match="Verifier returned decisions for 2 items, expected 1",
    ):
        await verifier.verify(candidates=(cand1,), context=context)

    # 3. 2 candidates ("c-1", "c-2"), 3 decisions ("c-1", "c-1", "c-2")
    three_decisions_response = {
        "decisions": [
            {
                "candidate_id": "c-1",
                "status": "supported",
                "reason": "OK 1",
                "evidence_refs": [{"block_id": query_block_id, "start_line": 10, "end_line": 10}],
                "requirement_refs": [],
            },
            {
                "candidate_id": "c-1",
                "status": "supported",
                "reason": "OK 2",
                "evidence_refs": [{"block_id": query_block_id, "start_line": 10, "end_line": 10}],
                "requirement_refs": [],
            },
            {
                "candidate_id": "c-2",
                "status": "supported",
                "reason": "OK 3",
                "evidence_refs": [{"block_id": query_block_id, "start_line": 10, "end_line": 10}],
                "requirement_refs": [],
            },
        ]
    }
    verifier_three = ReflectionPass(FakeVerifierModel(three_decisions_response))
    with pytest.raises(
        VerificationInvalidOutputError,
        match="Verifier returned decisions for 3 items, expected 2",
    ):
        await verifier_three.verify(candidates=(cand1, cand2), context=context)


@pytest.mark.anyio
async def test_reflection_pass_allows_evidence_from_other_diff_file() -> None:
    cand1 = make_candidate("c-1", path="app/query.py", rule_name=None)
    other_block_id = file_block_id("app/other.py")

    fake_response = {
        "decisions": [
            {
                "candidate_id": "c-1",
                "status": "supported",
                "reason": "Found defect in different file",
                "evidence_refs": [{"block_id": other_block_id, "start_line": 5, "end_line": 5}],
                "requirement_refs": [],
            }
        ]
    }
    fake_model = FakeVerifierModel(fake_response)
    verifier = ReflectionPass(fake_model)

    snapshot = make_snapshot()
    context = ReviewContext(
        system="SYSTEM",
        rules=(),
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=PullRequestMeta(
            "T", None, "u", "b", "main", (), 2, 2, 0, False, False, head_sha="head_123"
        ),
        changed_files=(
            ChangedFile("app/query.py", "modified", (DiffLine(10, "added", "x = 1"),)),
            ChangedFile("app/other.py", "modified", (DiffLine(5, "added", "y = 2"),)),
        ),
        omitted_files=(),
        policy_snapshot=snapshot,
    )

    decisions = await verifier.verify(candidates=(cand1,), context=context)
    assert len(decisions) == 1
    assert decisions[0].status == "supported"
    assert decisions[0].evidence_refs[0].block_id == other_block_id


@pytest.mark.anyio
async def test_reflection_pass_validates_service_defaults_check_id() -> None:
    from app.modules.reviews.application.prompt_builder import ReviewRule

    cand1 = make_candidate("c-1", path="app/query.py", rule_name="service-defaults:N1")
    query_block_id = file_block_id("app/query.py")

    rules = (
        ReviewRule(
            name="Naming Consistency",
            include=(),
            exclude=(),
            checks=("Check 1", "Check 2", "Check 3", "Check 4"),
        ),
        ReviewRule(
            name="Error Handling Standards",
            include=(),
            exclude=(),
            checks=("Err 1", "Err 2", "Err 3", "Err 4"),
        ),
    )
    snapshot = make_snapshot()
    context = ReviewContext(
        system="SYSTEM",
        rules=rules,
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=PullRequestMeta("T", None, "u", "b", "main", (), 1, 1, 0, False, False),
        changed_files=(ChangedFile("app/query.py", "modified", (DiffLine(10, "added", "x = 1"),)),),
        omitted_files=(),
        policy_snapshot=snapshot,
    )

    # 1. check_id is not in service defaults rules at all
    bad_id_response = {
        "decisions": [
            {
                "candidate_id": "c-1",
                "status": "supported",
                "reason": "Defect",
                "evidence_refs": [{"block_id": query_block_id, "start_line": 10, "end_line": 10}],
                "requirement_refs": [
                    {
                        "block_id": "service-defaults",
                        "start_line": 1,
                        "end_line": 1,
                        "check_id": "completely-invalid",
                    }
                ],
            }
        ]
    }
    verifier_bad = ReflectionPass(FakeVerifierModel(bad_id_response))
    with pytest.raises(
        VerificationInvalidOutputError, match="does not match service defaults rule 'N1'"
    ):
        await verifier_bad.verify(candidates=(cand1,), context=context)

    # 2. check_id exists in rules ("E1" in Error Handling), but candidate rule_id is "N1"
    mismatched_id_response = {
        "decisions": [
            {
                "candidate_id": "c-1",
                "status": "supported",
                "reason": "Defect",
                "evidence_refs": [{"block_id": query_block_id, "start_line": 10, "end_line": 10}],
                "requirement_refs": [
                    {
                        "block_id": "service-defaults",
                        "start_line": 5,
                        "end_line": 5,
                        "check_id": "E1",
                    }
                ],
            }
        ]
    }
    verifier_mismatch = ReflectionPass(FakeVerifierModel(mismatched_id_response))
    with pytest.raises(
        VerificationInvalidOutputError, match="does not match service defaults rule 'N1'"
    ):
        await verifier_mismatch.verify(candidates=(cand1,), context=context)

    # 3. Line offset mismatch: candidate is E1 (line 5),
    # but range 1-1 covers line 1 (N1)
    cand2 = make_candidate("c-2", path="app/query.py", rule_name="service-defaults:E1")
    mismatched_line_response = {
        "decisions": [
            {
                "candidate_id": "c-2",
                "status": "supported",
                "reason": "Defect",
                "evidence_refs": [{"block_id": query_block_id, "start_line": 10, "end_line": 10}],
                "requirement_refs": [
                    {
                        "block_id": "service-defaults",
                        "start_line": 1,
                        "end_line": 1,
                        "check_id": "E1",
                    }
                ],
            }
        ]
    }
    verifier_line_mismatch = ReflectionPass(FakeVerifierModel(mismatched_line_response))
    with pytest.raises(VerificationInvalidOutputError, match="does not cover check 'E1' line 5"):
        await verifier_line_mismatch.verify(candidates=(cand2,), context=context)

    # 3b. Lower bound range mismatch: check N1 is at line 1, but range is 2-2
    mismatched_lower_bound_response = {
        "decisions": [
            {
                "candidate_id": "c-1",
                "status": "supported",
                "reason": "Defect",
                "evidence_refs": [{"block_id": query_block_id, "start_line": 10, "end_line": 10}],
                "requirement_refs": [
                    {
                        "block_id": "service-defaults",
                        "start_line": 2,
                        "end_line": 2,
                        "check_id": "N1",
                    }
                ],
            }
        ]
    }
    verifier_lower_mismatch = ReflectionPass(FakeVerifierModel(mismatched_lower_bound_response))
    with pytest.raises(VerificationInvalidOutputError, match="does not cover check 'N1' line 1"):
        await verifier_lower_mismatch.verify(candidates=(cand1,), context=context)

    # 4. Positive case: check_id matches candidate rule_id and covers line offset
    valid_id_response = {
        "decisions": [
            {
                "candidate_id": "c-1",
                "status": "supported",
                "reason": "Valid defect",
                "evidence_refs": [{"block_id": query_block_id, "start_line": 10, "end_line": 10}],
                "requirement_refs": [
                    {
                        "block_id": "service-defaults",
                        "start_line": 1,
                        "end_line": 1,
                        "check_id": "N1",
                    }
                ],
            }
        ]
    }
    verifier_valid = ReflectionPass(FakeVerifierModel(valid_id_response))
    decisions = await verifier_valid.verify(candidates=(cand1,), context=context)
    assert len(decisions) == 1
    assert decisions[0].status == "supported"


@pytest.mark.anyio
async def test_reflection_pass_translates_llm_and_system_errors() -> None:
    cand = make_candidate("c-1")
    snapshot = make_snapshot()
    context = ReviewContext(
        system="SYSTEM",
        rules=(),
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=PullRequestMeta("T", None, "u", "b", "main", (), 1, 1, 0, False, False),
        changed_files=(ChangedFile("app/query.py", "modified", (DiffLine(10, "added", "x = 1"),)),),
        omitted_files=(),
        policy_snapshot=snapshot,
    )

    # TimeoutError -> VerificationUnavailableError
    v1 = ReflectionPass(CustomModel(TimeoutError("Timeout")))
    with pytest.raises(VerificationUnavailableError, match="Verifier model timed out"):
        await v1.verify(candidates=(cand,), context=context)

    # llm_timeout -> VerificationUnavailableError
    v2 = ReflectionPass(CustomModel(RunFailure("llm_timeout", "Slow")))
    with pytest.raises(VerificationUnavailableError, match="Verifier model unavailable"):
        await v2.verify(candidates=(cand,), context=context)

    # llm_unavailable -> VerificationUnavailableError
    v3 = ReflectionPass(CustomModel(RunFailure("llm_unavailable", "503")))
    with pytest.raises(VerificationUnavailableError, match="Verifier model unavailable"):
        await v3.verify(candidates=(cand,), context=context)

    # llm_invalid_output -> VerificationInvalidOutputError
    v4 = ReflectionPass(CustomModel(RunFailure("llm_invalid_output", "Bad output")))
    with pytest.raises(VerificationInvalidOutputError, match="Verifier model output invalid"):
        await v4.verify(candidates=(cand,), context=context)

    # budget_exceeded -> RunFailure(verification_budget_exceeded)
    v5 = ReflectionPass(CustomModel(RunFailure("budget_exceeded", "Cost")))
    with pytest.raises(RunFailure) as exc_budget:
        await v5.verify(candidates=(cand,), context=context)
    assert exc_budget.value.error_code == "verification_budget_exceeded"

    # deadline_exceeded -> RunFailure(verification_deadline_exceeded)
    v6 = ReflectionPass(CustomModel(RunFailure("deadline_exceeded", "Time")))
    with pytest.raises(RunFailure) as exc_deadline:
        await v6.verify(candidates=(cand,), context=context)
    assert exc_deadline.value.error_code == "verification_deadline_exceeded"

    # Unknown exception -> VerificationUnavailableError
    v7 = ReflectionPass(CustomModel(RuntimeError("Unexpected crash")))
    with pytest.raises(VerificationUnavailableError, match="Verifier model call failed"):
        await v7.verify(candidates=(cand,), context=context)


@pytest.mark.anyio
async def test_reflection_pass_unexpected_output_type() -> None:
    cand = make_candidate("c-1")
    snapshot = make_snapshot()
    context = ReviewContext(
        system="SYSTEM",
        rules=(),
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=PullRequestMeta("T", None, "u", "b", "main", (), 1, 1, 0, False, False),
        changed_files=(),
        omitted_files=(),
        policy_snapshot=snapshot,
    )
    v = ReflectionPass(FakeVerifierModel(12345))  # type: ignore[arg-type]
    with pytest.raises(VerificationInvalidOutputError, match="Unexpected verifier output type"):
        await v.verify(candidates=(cand,), context=context)


@pytest.mark.anyio
async def test_reflection_pass_verification_context_overflow() -> None:
    from dataclasses import replace

    # Profile with tiny send budget = 5 (max_input_tokens=8005 minus 8000 output reserve)
    tiny_profile = replace(make_complete_execution_profile(), max_input_tokens=8005)
    cand = make_candidate("c-1")
    snapshot = make_snapshot()
    snapshot_tiny = PolicySnapshot(
        schema_version=snapshot.schema_version,
        snapshot_id=snapshot.snapshot_id,
        run_id=snapshot.run_id,
        repository_id=snapshot.repository_id,
        policy_base_sha=snapshot.policy_base_sha,
        rules=snapshot.rules,
        agents=snapshot.agents,
        ignore_patterns=snapshot.ignore_patterns,
        ignore_digest=snapshot.ignore_digest,
        parser_profile=snapshot.parser_profile,
        parser_version=snapshot.parser_version,
        matcher_version=snapshot.matcher_version,
        limits_version=snapshot.limits_version,
        builtin_exclusions_version=snapshot.builtin_exclusions_version,
        priority_matcher_version=snapshot.priority_matcher_version,
        service_defaults_rule_version_id=snapshot.service_defaults_rule_version_id,
        defaults_checksum=snapshot.defaults_checksum,
        prompt_bindings=snapshot.prompt_bindings,
        execution_profile=tiny_profile,
        policy_digest=snapshot.policy_digest,
    )
    context = ReviewContext(
        system="SYSTEM",
        rules=(),
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=PullRequestMeta("T", None, "u", "b", "main", (), 1, 1, 0, False, False),
        changed_files=(ChangedFile("app/query.py", "modified", (DiffLine(10, "added", "x = 1"),)),),
        omitted_files=(),
        policy_snapshot=snapshot_tiny,
    )

    verifier = ReflectionPass(FakeVerifierModel({"decisions": []}))
    with pytest.raises(RunFailure) as exc_overflow:
        await verifier.verify(candidates=(cand,), context=context)
    assert exc_overflow.value.error_code == "verification_context_overflow"
    assert "tokens, limit is 5" in exc_overflow.value.message


@pytest.mark.anyio
async def test_reflection_pass_malformed_json_and_schema_validation() -> None:
    from types import MappingProxyType

    cand = make_candidate("c-1")
    query_block_id = file_block_id("app/query.py")
    snapshot = make_snapshot()
    context = ReviewContext(
        system="SYSTEM",
        rules=(),
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=PullRequestMeta("T", None, "u", "b", "main", (), 1, 1, 0, False, False),
        changed_files=(ChangedFile("app/query.py", "modified", (DiffLine(10, "added", "x = 1"),)),),
        omitted_files=(),
        policy_snapshot=snapshot,
    )

    # 1. Non-JSON string
    v1 = ReflectionPass(FakeVerifierModel("not json at all"))
    with pytest.raises(
        VerificationInvalidOutputError, match="Malformed verifier response schema or JSON"
    ):
        await v1.verify(candidates=(cand,), context=context)

    # 2. Schema violation: missing required status
    bad_schema = {"decisions": [{"candidate_id": "c-1", "reason": "No status"}]}
    v2 = ReflectionPass(FakeVerifierModel(bad_schema))
    with pytest.raises(
        VerificationInvalidOutputError, match="Malformed verifier response schema or JSON"
    ):
        await v2.verify(candidates=(cand,), context=context)

    # 3. Valid bytes response
    valid_dict = {
        "decisions": [
            {
                "candidate_id": "c-1",
                "status": "supported",
                "reason": "OK",
                "evidence_refs": [{"block_id": query_block_id, "start_line": 10, "end_line": 10}],
                "requirement_refs": [
                    {"block_id": "policy-rules", "start_line": 1, "end_line": 1, "check_id": None}
                ],
            }
        ]
    }
    import json

    v3 = ReflectionPass(FakeVerifierModel(json.dumps(valid_dict).encode("utf-8")))
    d3 = await v3.verify(candidates=(cand,), context=context)
    assert len(d3) == 1
    assert d3[0].status == "supported"

    # 4. Valid MappingProxyType response
    v4 = ReflectionPass(FakeVerifierModel(MappingProxyType(valid_dict)))
    d4 = await v4.verify(candidates=(cand,), context=context)
    assert len(d4) == 1
    assert d4[0].status == "supported"


@pytest.mark.anyio
async def test_reflection_pass_validates_requirement_refs_and_evidence_refs() -> None:
    cand = make_candidate("c-1", rule_name=".review/rules.md")
    query_block_id = file_block_id("app/query.py")
    snapshot = make_snapshot()
    context = ReviewContext(
        system="SYSTEM",
        rules=(),
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=PullRequestMeta("T", None, "u", "b", "main", (), 1, 1, 0, False, False),
        changed_files=(ChangedFile("app/query.py", "modified", (DiffLine(10, "added", "x = 1"),)),),
        omitted_files=(),
        policy_snapshot=snapshot,
    )

    # 1. Evidence ref: unknown block
    v1 = ReflectionPass(
        FakeVerifierModel(
            {
                "decisions": [
                    {
                        "candidate_id": "c-1",
                        "status": "supported",
                        "reason": "OK",
                        "evidence_refs": [
                            {"block_id": "unknown-diff", "start_line": 1, "end_line": 1}
                        ],
                        "requirement_refs": [
                            {
                                "block_id": "policy-rules",
                                "start_line": 1,
                                "end_line": 1,
                                "check_id": None,
                            }
                        ],
                    }
                ]
            }
        )
    )
    with pytest.raises(
        VerificationInvalidOutputError, match="evidence_ref unknown-diff not found in manifest"
    ):
        await v1.verify(candidates=(cand,), context=context)

    # 2. Evidence ref: non-code role (policy_document)
    v2 = ReflectionPass(
        FakeVerifierModel(
            {
                "decisions": [
                    {
                        "candidate_id": "c-1",
                        "status": "supported",
                        "reason": "OK",
                        "evidence_refs": [
                            {"block_id": "policy-rules", "start_line": 1, "end_line": 1}
                        ],
                        "requirement_refs": [
                            {
                                "block_id": "policy-rules",
                                "start_line": 1,
                                "end_line": 1,
                                "check_id": None,
                            }
                        ],
                    }
                ]
            }
        )
    )
    with pytest.raises(VerificationInvalidOutputError, match="has non-code role 'policy_document'"):
        await v2.verify(candidates=(cand,), context=context)

    # 3. Evidence ref: range not shown
    v3 = ReflectionPass(
        FakeVerifierModel(
            {
                "decisions": [
                    {
                        "candidate_id": "c-1",
                        "status": "supported",
                        "reason": "OK",
                        "evidence_refs": [
                            {"block_id": query_block_id, "start_line": 50, "end_line": 60}
                        ],
                        "requirement_refs": [
                            {
                                "block_id": "policy-rules",
                                "start_line": 1,
                                "end_line": 1,
                                "check_id": None,
                            }
                        ],
                    }
                ]
            }
        )
    )
    with pytest.raises(VerificationInvalidOutputError, match="is not shown in verifier manifest"):
        await v3.verify(candidates=(cand,), context=context)

    # 4. Requirement ref: unknown block
    v4 = ReflectionPass(
        FakeVerifierModel(
            {
                "decisions": [
                    {
                        "candidate_id": "c-1",
                        "status": "supported",
                        "reason": "OK",
                        "evidence_refs": [
                            {"block_id": query_block_id, "start_line": 10, "end_line": 10}
                        ],
                        "requirement_refs": [
                            {
                                "block_id": "unknown-req",
                                "start_line": 1,
                                "end_line": 1,
                                "check_id": None,
                            }
                        ],
                    }
                ]
            }
        )
    )
    with pytest.raises(
        VerificationInvalidOutputError,
        match="requirement_ref block 'unknown-req' not found in manifest",
    ):
        await v4.verify(candidates=(cand,), context=context)

    # 5. Requirement ref: invalid role (diff block)
    v5 = ReflectionPass(
        FakeVerifierModel(
            {
                "decisions": [
                    {
                        "candidate_id": "c-1",
                        "status": "supported",
                        "reason": "OK",
                        "evidence_refs": [
                            {"block_id": query_block_id, "start_line": 10, "end_line": 10}
                        ],
                        "requirement_refs": [
                            {
                                "block_id": query_block_id,
                                "start_line": 10,
                                "end_line": 10,
                                "check_id": None,
                            }
                        ],
                    }
                ]
            }
        )
    )
    with pytest.raises(VerificationInvalidOutputError, match="has invalid role 'diff'"):
        await v5.verify(candidates=(cand,), context=context)

    # 6. Requirement ref: range not shown
    v6 = ReflectionPass(
        FakeVerifierModel(
            {
                "decisions": [
                    {
                        "candidate_id": "c-1",
                        "status": "supported",
                        "reason": "OK",
                        "evidence_refs": [
                            {"block_id": query_block_id, "start_line": 10, "end_line": 10}
                        ],
                        "requirement_refs": [
                            {
                                "block_id": "policy-rules",
                                "start_line": 50,
                                "end_line": 60,
                                "check_id": None,
                            }
                        ],
                    }
                ]
            }
        )
    )
    with pytest.raises(VerificationInvalidOutputError, match="not shown in 'policy-rules'"):
        await v6.verify(candidates=(cand,), context=context)

    # 7. Candidate has rule_name=".review/rules.md" but requirement_refs is empty
    v7 = ReflectionPass(
        FakeVerifierModel(
            {
                "decisions": [
                    {
                        "candidate_id": "c-1",
                        "status": "supported",
                        "reason": "OK",
                        "evidence_refs": [
                            {"block_id": query_block_id, "start_line": 10, "end_line": 10}
                        ],
                        "requirement_refs": [],
                    }
                ]
            }
        )
    )
    with pytest.raises(
        VerificationInvalidOutputError,
        match="requires at least one matching requirement_ref for this rule",
    ):
        await v7.verify(candidates=(cand,), context=context)


def test_verifier_prompt_envelope_contains_block_id_and_defaults_and_candidate_attrs() -> None:
    cand = CandidateFindingItem(
        candidate_id="cand-123",
        finding=ReviewFindingV2(
            path="app/query.py",
            line=10,
            start_line=5,
            severity="critical",
            category="security",
            title="SQL Injection",
            body="Raw formatting used",
            suggestion="Use params",
            confidence=0.95,
            rule_name=".review/rules.md",
            evidence_refs=[
                EvidenceRef(block_id="file-app-query-py-12345678", start_line=5, end_line=10)
            ],
        ),
    )
    snapshot = make_snapshot()
    rule = ReviewRule(
        "Naming Consistency",
        ("*.py",),
        (),
        ("Never format SQL", "Check 2", "Check 3", "Check 4"),
    )
    context = ReviewContext(
        system="SYSTEM",
        rules=(rule,),
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=PullRequestMeta("T", None, "u", "b", "main", (), 1, 1, 0, False, False),
        changed_files=(ChangedFile("app/query.py", "modified", (DiffLine(10, "added", "x = 1"),)),),
        omitted_files=(),
        policy_snapshot=snapshot,
    )
    builder = VerifierPromptBuilder()
    prompt, manifest = builder.build_verifier_prompt(candidates=(cand,), context=context)

    # 1. <file> carries explicit block_id
    query_block_id = file_block_id("app/query.py")
    assert f'<file block_id="{query_block_id}" path="app/query.py"' in prompt.user

    # 2. <service_defaults> block is rendered
    assert '<service_defaults block_id="service-defaults">' in prompt.user
    assert "Never format SQL" in prompt.user

    # 3. Candidate attributes rendered correctly in XML
    assert (
        '<candidate candidate_id="cand-123" path="app/query.py" start_line="5" line="10" '
        'category="security" severity="critical">'
    ) in prompt.user
    assert "<title>SQL Injection</title>" in prompt.user
    assert "<body>Raw formatting used</body>" in prompt.user
    assert "<suggestion>Use params</suggestion>" in prompt.user
    assert '<rule_attribution name=".review/rules.md" />' in prompt.user


@pytest.mark.anyio
async def test_reflection_pass_guard_rejects_prompt_exceeding_token_limit() -> None:
    cand = make_candidate("c-1")
    # execution_profile with very low send budget = 10 (max_input_tokens=8010 minus 8000 reserve)
    import dataclasses

    profile = dataclasses.replace(make_complete_execution_profile(), max_input_tokens=8010)
    snapshot = dataclasses.replace(make_snapshot(), execution_profile=profile)
    context = ReviewContext(
        system="SYSTEM",
        rules=(),
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=PullRequestMeta("T", None, "u", "b", "main", (), 1, 1, 0, False, False),
        changed_files=(ChangedFile("app/query.py", "modified", (DiffLine(10, "added", "x = 1"),)),),
        omitted_files=(),
        policy_snapshot=snapshot,
    )
    reflection = ReflectionPass(FakeVerifierModel({"decisions": []}))
    with pytest.raises(RunFailure) as exc_info:
        await reflection.verify(candidates=(cand,), context=context)
    assert exc_info.value.error_code == "verification_context_overflow"
    assert "limit is 10" in exc_info.value.message


@pytest.mark.anyio
async def test_reflection_pass_maps_foreign_exception_error_codes() -> None:
    cand = make_candidate("c-1")
    snapshot = make_snapshot()
    context = ReviewContext(
        system="SYSTEM",
        rules=(),
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=PullRequestMeta("T", None, "u", "b", "main", (), 1, 1, 0, False, False),
        changed_files=(ChangedFile("app/query.py", "modified", (DiffLine(10, "added", "x = 1"),)),),
        omitted_files=(),
        policy_snapshot=snapshot,
    )

    class ForeignError(Exception):
        def __init__(self, code: str) -> None:
            super().__init__(code)
            self.error_code = code

    # Foreign exception with verification_invalid_output maps to VerificationInvalidOutputError
    v1 = ReflectionPass(CustomModel(ForeignError("verification_invalid_output")))
    with pytest.raises(VerificationInvalidOutputError):
        await v1.verify(candidates=(cand,), context=context)

    # Foreign exception with verification_unavailable maps to VerificationUnavailableError
    v2 = ReflectionPass(CustomModel(ForeignError("verification_unavailable")))
    with pytest.raises(VerificationUnavailableError):
        await v2.verify(candidates=(cand,), context=context)


@pytest.mark.anyio
async def test_reflection_pass_requirement_ref_validation_details() -> None:
    query_block_id = file_block_id("app/query.py")
    rule = ReviewRule("Naming Consistency", ("*.py",), (), ("Check sql", "c2", "c3", "c4"))
    snapshot = make_snapshot()
    context = ReviewContext(
        system="SYSTEM",
        rules=(rule,),
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=PullRequestMeta("T", None, "u", "b", "main", (), 1, 1, 0, False, False),
        changed_files=(ChangedFile("app/query.py", "modified", (DiffLine(10, "added", "x = 1"),)),),
        omitted_files=(),
        policy_snapshot=snapshot,
    )

    # 1. service_defaults requirement_ref without check_id is rejected
    cand1 = CandidateFindingItem(
        candidate_id="c-1",
        finding=ReviewFindingV2(
            path="app/query.py",
            line=10,
            start_line=None,
            severity="critical",
            category="security",
            title="T",
            body="B",
            suggestion=None,
            confidence=0.9,
            rule_name="service-defaults:N1",
            evidence_refs=[EvidenceRef(block_id=query_block_id, start_line=10, end_line=10)],
        ),
    )
    v1 = ReflectionPass(
        FakeVerifierModel(
            {
                "decisions": [
                    {
                        "candidate_id": "c-1",
                        "status": "supported",
                        "reason": "OK",
                        "evidence_refs": [
                            {"block_id": query_block_id, "start_line": 10, "end_line": 10}
                        ],
                        "requirement_refs": [
                            {
                                "block_id": "service-defaults",
                                "start_line": 1,
                                "end_line": 1,
                                "check_id": None,
                            }
                        ],
                    }
                ]
            }
        )
    )
    with pytest.raises(
        VerificationInvalidOutputError, match="check_id is required for service_defaults"
    ):
        await v1.verify(candidates=(cand1,), context=context)

    # 2. Candidate with rule_name=".review/rules.md" without matching policy-rules ref is rejected
    cand_rules = CandidateFindingItem(
        candidate_id="c-2",
        finding=ReviewFindingV2(
            path="app/query.py",
            line=10,
            start_line=None,
            severity="critical",
            category="security",
            title="T",
            body="B",
            suggestion=None,
            confidence=0.9,
            rule_name=".review/rules.md",
            evidence_refs=[EvidenceRef(block_id=query_block_id, start_line=10, end_line=10)],
        ),
    )
    v2_missing_rules = ReflectionPass(
        FakeVerifierModel(
            {
                "decisions": [
                    {
                        "candidate_id": "c-2",
                        "status": "supported",
                        "reason": "OK",
                        "evidence_refs": [
                            {"block_id": query_block_id, "start_line": 10, "end_line": 10}
                        ],
                        "requirement_refs": [
                            {
                                "block_id": "service-defaults",
                                "start_line": 1,
                                "end_line": 1,
                                "check_id": "N1",
                            }
                        ],
                    }
                ]
            }
        )
    )
    with pytest.raises(
        VerificationInvalidOutputError,
        match="requires at least one matching requirement_ref for this rule",
    ):
        await v2_missing_rules.verify(candidates=(cand_rules,), context=context)

    # 3. Candidate with rule_name="AGENTS.md" without matching policy-agents ref is rejected
    snapshot_agents = replace(
        snapshot,
        agents=make_policy_document_present("AGENTS.md", "# Conventions\n1. Be explicit\n"),
    )
    context_agents = replace(context, policy_snapshot=snapshot_agents)
    cand_agents = CandidateFindingItem(
        candidate_id="c-3",
        finding=ReviewFindingV2(
            path="app/query.py",
            line=10,
            start_line=None,
            severity="critical",
            category="security",
            title="T",
            body="B",
            suggestion=None,
            confidence=0.9,
            rule_name="AGENTS.md",
            evidence_refs=[EvidenceRef(block_id=query_block_id, start_line=10, end_line=10)],
        ),
    )
    v3_missing_agents = ReflectionPass(
        FakeVerifierModel(
            {
                "decisions": [
                    {
                        "candidate_id": "c-3",
                        "status": "supported",
                        "reason": "OK",
                        "evidence_refs": [
                            {"block_id": query_block_id, "start_line": 10, "end_line": 10}
                        ],
                        "requirement_refs": [
                            {
                                "block_id": "policy-rules",
                                "start_line": 1,
                                "end_line": 2,
                                "check_id": None,
                            }
                        ],
                    }
                ]
            }
        )
    )
    with pytest.raises(
        VerificationInvalidOutputError,
        match="requires at least one matching requirement_ref for this rule",
    ):
        await v3_missing_agents.verify(candidates=(cand_agents,), context=context_agents)

    # 4. Candidate with rule_name="service-defaults:N1"
    # without service-defaults ref is rejected
    cand_defaults = CandidateFindingItem(
        candidate_id="c-4",
        finding=ReviewFindingV2(
            path="app/query.py",
            line=10,
            start_line=None,
            severity="critical",
            category="security",
            title="T",
            body="B",
            suggestion=None,
            confidence=0.9,
            rule_name="service-defaults:N1",
            evidence_refs=[EvidenceRef(block_id=query_block_id, start_line=10, end_line=10)],
        ),
    )
    v4_missing_defaults = ReflectionPass(
        FakeVerifierModel(
            {
                "decisions": [
                    {
                        "candidate_id": "c-4",
                        "status": "supported",
                        "reason": "OK",
                        "evidence_refs": [
                            {"block_id": query_block_id, "start_line": 10, "end_line": 10}
                        ],
                        "requirement_refs": [
                            {
                                "block_id": "policy-rules",
                                "start_line": 1,
                                "end_line": 2,
                                "check_id": None,
                            }
                        ],
                    }
                ]
            }
        )
    )
    with pytest.raises(
        VerificationInvalidOutputError,
        match="requires at least one matching requirement_ref for this rule",
    ):
        await v4_missing_defaults.verify(candidates=(cand_defaults,), context=context)

    # 5. Multiple requirement_ref sources accepted per RULES_FORMAT_SPEC §8.2
    v5_multi_source = ReflectionPass(
        FakeVerifierModel(
            {
                "decisions": [
                    {
                        "candidate_id": "c-2",
                        "status": "supported",
                        "reason": "OK",
                        "evidence_refs": [
                            {"block_id": query_block_id, "start_line": 10, "end_line": 10}
                        ],
                        "requirement_refs": [
                            {
                                "block_id": "policy-rules",
                                "start_line": 1,
                                "end_line": 2,
                                "check_id": None,
                            },
                            {
                                "block_id": "service-defaults",
                                "start_line": 1,
                                "end_line": 1,
                                "check_id": "N1",
                            },
                        ],
                    }
                ]
            }
        )
    )
    decisions_multi = await v5_multi_source.verify(candidates=(cand_rules,), context=context)
    assert len(decisions_multi) == 1
    assert decisions_multi[0].status == "supported"

    # 6. Positive case for AGENTS.md
    v6_agents = ReflectionPass(
        FakeVerifierModel(
            {
                "decisions": [
                    {
                        "candidate_id": "c-3",
                        "status": "supported",
                        "reason": "OK",
                        "evidence_refs": [
                            {"block_id": query_block_id, "start_line": 10, "end_line": 10}
                        ],
                        "requirement_refs": [
                            {
                                "block_id": "policy-agents",
                                "start_line": 1,
                                "end_line": 2,
                                "check_id": None,
                            }
                        ],
                    }
                ]
            }
        )
    )
    decisions_agents = await v6_agents.verify(candidates=(cand_agents,), context=context_agents)
    assert len(decisions_agents) == 1
    assert decisions_agents[0].status == "supported"


@pytest.mark.anyio
async def test_reflection_pass_service_defaults_slug_matching_for_real_rule_names() -> None:
    query_block_id = file_block_id("app/query.py")
    rule = ReviewRule("Naming Consistency", ("*.py",), (), ("Check snake_case", "c2", "c3", "c4"))
    snapshot = make_snapshot()
    context = ReviewContext(
        system="SYSTEM",
        rules=(rule,),
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=PullRequestMeta("T", None, "u", "b", "main", (), 1, 1, 0, False, False),
        changed_files=(ChangedFile("app/query.py", "modified", (DiffLine(10, "added", "x = 1"),)),),
        omitted_files=(),
        policy_snapshot=snapshot,
    )

    # 1. Stable check ID "N1" is accepted
    cand_stable = CandidateFindingItem(
        candidate_id="c-stable",
        finding=ReviewFindingV2(
            path="app/query.py",
            line=10,
            start_line=None,
            severity="medium",
            category="readability",
            title="Naming violation",
            body="Variable must be snake_case",
            suggestion=None,
            confidence=0.9,
            rule_name="service-defaults:N1",
            evidence_refs=[EvidenceRef(block_id=query_block_id, start_line=10, end_line=10)],
        ),
    )
    response = {
        "decisions": [
            {
                "candidate_id": "c-stable",
                "status": "supported",
                "reason": "Variable name violates Naming Consistency rule.",
                "evidence_refs": [{"block_id": query_block_id, "start_line": 10, "end_line": 10}],
                "requirement_refs": [
                    {
                        "block_id": "service-defaults",
                        "start_line": 1,
                        "end_line": 1,
                        "check_id": "N1",
                    }
                ],
            }
        ]
    }
    verifier = ReflectionPass(FakeVerifierModel(response))
    decisions = await verifier.verify(candidates=(cand_stable,), context=context)
    assert len(decisions) == 1
    assert decisions[0].status == "supported"

    # 2. Unshown lowercase rule slug "naming-consistency" is rejected
    cand_slug = CandidateFindingItem(
        candidate_id="c-slug",
        finding=ReviewFindingV2(
            path="app/query.py",
            line=10,
            start_line=None,
            severity="medium",
            category="readability",
            title="Naming violation",
            body="Variable must be snake_case",
            suggestion=None,
            confidence=0.9,
            rule_name="service-defaults:naming-consistency",
            evidence_refs=[EvidenceRef(block_id=query_block_id, start_line=10, end_line=10)],
        ),
    )
    response_slug = {
        "decisions": [
            {
                "candidate_id": "c-slug",
                "status": "supported",
                "reason": "Slug attempt",
                "evidence_refs": [{"block_id": query_block_id, "start_line": 10, "end_line": 10}],
                "requirement_refs": [
                    {
                        "block_id": "service-defaults",
                        "start_line": 1,
                        "end_line": 1,
                        "check_id": "N1",
                    }
                ],
            }
        ]
    }
    with pytest.raises(
        VerificationInvalidOutputError,
        match=r"does not match service defaults rule 'naming-consistency'",
    ):
        await ReflectionPass(FakeVerifierModel(response_slug)).verify(
            candidates=(cand_slug,), context=context
        )


@pytest.mark.anyio
async def test_reflection_pass_secondary_source_defaults_validation_branches() -> None:
    query_block_id = file_block_id("app/query.py")
    rule = ReviewRule(
        "Naming Consistency", ("*.py",), (), ("Check something", "Check another", "c3", "c4")
    )
    snapshot = make_snapshot()
    context = ReviewContext(
        system="SYSTEM",
        rules=(rule,),
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=PullRequestMeta("T", None, "u", "b", "main", (), 1, 1, 0, False, False),
        changed_files=(ChangedFile("app/query.py", "modified", (DiffLine(10, "added", "x = 1"),)),),
        omitted_files=(),
        policy_snapshot=snapshot,
    )
    cand_rules = CandidateFindingItem(
        candidate_id="c-multi",
        finding=ReviewFindingV2(
            path="app/query.py",
            line=10,
            start_line=None,
            severity="high",
            category="security",
            title="Security issue",
            body="Security issue body",
            suggestion=None,
            confidence=0.9,
            rule_name=".review/rules.md",
            evidence_refs=[EvidenceRef(block_id=query_block_id, start_line=10, end_line=10)],
        ),
    )

    # 1. Secondary ref check_id does not match any service defaults rule
    unknown_check_response = {
        "decisions": [
            {
                "candidate_id": "c-multi",
                "status": "supported",
                "reason": "OK",
                "evidence_refs": [{"block_id": query_block_id, "start_line": 10, "end_line": 10}],
                "requirement_refs": [
                    {"block_id": "policy-rules", "start_line": 1, "end_line": 2, "check_id": None},
                    {
                        "block_id": "service-defaults",
                        "start_line": 1,
                        "end_line": 1,
                        "check_id": "99",
                    },
                ],
            }
        ]
    }
    v_unknown = ReflectionPass(FakeVerifierModel(unknown_check_response))
    with pytest.raises(
        VerificationInvalidOutputError, match="does not match any service defaults rule"
    ):
        await v_unknown.verify(candidates=(cand_rules,), context=context)

    # 2. Secondary ref range does not cover check line (lower bound: range is 2-2)
    range_mismatch_response = {
        "decisions": [
            {
                "candidate_id": "c-multi",
                "status": "supported",
                "reason": "OK",
                "evidence_refs": [{"block_id": query_block_id, "start_line": 10, "end_line": 10}],
                "requirement_refs": [
                    {"block_id": "policy-rules", "start_line": 1, "end_line": 2, "check_id": None},
                    {
                        "block_id": "service-defaults",
                        "start_line": 2,
                        "end_line": 2,
                        "check_id": "N1",
                    },
                ],
            }
        ]
    }
    v_range = ReflectionPass(FakeVerifierModel(range_mismatch_response))
    with pytest.raises(VerificationInvalidOutputError, match="does not cover check 'N1'"):
        await v_range.verify(candidates=(cand_rules,), context=context)

    # 3. Secondary ref range upper bound mismatch: check N2 is at line 2, but range is 1-1
    range_upper_mismatch = {
        "decisions": [
            {
                "candidate_id": "c-multi",
                "status": "supported",
                "reason": "OK",
                "evidence_refs": [{"block_id": query_block_id, "start_line": 10, "end_line": 10}],
                "requirement_refs": [
                    {"block_id": "policy-rules", "start_line": 1, "end_line": 2, "check_id": None},
                    {
                        "block_id": "service-defaults",
                        "start_line": 1,
                        "end_line": 1,
                        "check_id": "N2",
                    },
                ],
            }
        ]
    }
    v_upper = ReflectionPass(FakeVerifierModel(range_upper_mismatch))
    with pytest.raises(VerificationInvalidOutputError, match="does not cover check 'N2'"):
        await v_upper.verify(candidates=(cand_rules,), context=context)

    # 4. Secondary ref positive case: check N1 is covered by range 1-2
    positive_multi_response = {
        "decisions": [
            {
                "candidate_id": "c-multi",
                "status": "supported",
                "reason": "OK",
                "evidence_refs": [{"block_id": query_block_id, "start_line": 10, "end_line": 10}],
                "requirement_refs": [
                    {"block_id": "policy-rules", "start_line": 1, "end_line": 2, "check_id": None},
                    {
                        "block_id": "service-defaults",
                        "start_line": 1,
                        "end_line": 2,
                        "check_id": "N1",
                    },
                ],
            }
        ]
    }
    v_pos = ReflectionPass(FakeVerifierModel(positive_multi_response))
    decisions_pos = await v_pos.verify(candidates=(cand_rules,), context=context)
    assert len(decisions_pos) == 1
    assert decisions_pos[0].status == "supported"


def test_reflection_pass_service_defaults_stable_id_and_legacy_slug_rejection() -> None:
    query_block_id = file_block_id("app/query.py")
    rule_errors = ReviewRule(
        "Error Handling Standards",
        ("*.py",),
        (),
        ("Err 1", "Err 2", "Err 3", "Err 4"),
    )
    snapshot = make_snapshot()
    context = ReviewContext(
        system="SYSTEM",
        rules=(rule_errors,),
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=PullRequestMeta("T", None, "u", "b", "main", (), 1, 1, 0, False, False),
        changed_files=(ChangedFile("app/query.py", "modified", (DiffLine(10, "added", "x = 1"),)),),
        omitted_files=(),
        policy_snapshot=snapshot,
    )

    # 1. Stable check ID E2 (at line 2) is accepted
    cand_e2 = CandidateFindingItem(
        candidate_id="c-e2",
        finding=ReviewFindingV2(
            path="app/query.py",
            line=10,
            start_line=None,
            severity="medium",
            category="readability",
            title="Error handling violation",
            body="Do not swallow errors",
            suggestion=None,
            confidence=0.9,
            rule_name="service-defaults:E2",
            evidence_refs=[EvidenceRef(block_id=query_block_id, start_line=10, end_line=10)],
        ),
    )
    response_e2 = {
        "decisions": [
            {
                "candidate_id": "c-e2",
                "status": "supported",
                "reason": "Violates E2.",
                "evidence_refs": [{"block_id": query_block_id, "start_line": 10, "end_line": 10}],
                "requirement_refs": [
                    {
                        "block_id": "service-defaults",
                        "start_line": 2,
                        "end_line": 2,
                        "check_id": "E2",
                    }
                ],
            }
        ]
    }
    verifier = ReflectionPass(FakeVerifierModel(response_e2))
    decisions = asyncio.run(verifier.verify(candidates=(cand_e2,), context=context))
    assert len(decisions) == 1
    assert decisions[0].status == "supported"

    # Canonical candidate E2 must not accept a case-folded requirement ID.
    response_lowercase = {
        "decisions": [
            {
                **response_e2["decisions"][0],
                "requirement_refs": [
                    {
                        "block_id": "service-defaults",
                        "start_line": 2,
                        "end_line": 2,
                        "check_id": "e2",
                    }
                ],
            }
        ]
    }
    with pytest.raises(
        VerificationInvalidOutputError,
        match="check_id 'e2' does not match service defaults rule 'E2'",
    ):
        asyncio.run(
            ReflectionPass(FakeVerifierModel(response_lowercase)).verify(
                candidates=(cand_e2,), context=context
            )
        )

    # 2. Exact-case rule slug "Error-Handling-Standards" is rejected (only stable ID is allowed)
    cand_slug = CandidateFindingItem(
        candidate_id="c-slug-exact",
        finding=ReviewFindingV2(
            path="app/query.py",
            line=10,
            start_line=None,
            severity="medium",
            category="readability",
            title="Error handling violation",
            body="Do not swallow errors",
            suggestion=None,
            confidence=0.9,
            rule_name="service-defaults:Error-Handling-Standards",
            evidence_refs=[EvidenceRef(block_id=query_block_id, start_line=10, end_line=10)],
        ),
    )
    response_slug = {
        "decisions": [
            {
                "candidate_id": "c-slug-exact",
                "status": "supported",
                "reason": "Violates E2.",
                "evidence_refs": [{"block_id": query_block_id, "start_line": 10, "end_line": 10}],
                "requirement_refs": [
                    {
                        "block_id": "service-defaults",
                        "start_line": 2,
                        "end_line": 2,
                        "check_id": "E2",
                    }
                ],
            }
        ]
    }
    with pytest.raises(
        VerificationInvalidOutputError,
        match=r"does not match service defaults rule 'Error-Handling-Standards'",
    ):
        asyncio.run(
            ReflectionPass(FakeVerifierModel(response_slug)).verify(
                candidates=(cand_slug,), context=context
            )
        )

    # 3. Duplicate check ID across rules in context raises SNAPSHOT_INTEGRITY_ERROR
    context_dup = replace(context, rules=(rule_errors, rule_errors))
    duplicate_model = FakeVerifierModel(response_e2)
    with pytest.raises(PolicyError) as exc_dup:
        asyncio.run(
            ReflectionPass(duplicate_model).verify(candidates=(cand_e2,), context=context_dup)
        )
    assert exc_dup.value.error_code == "policy_snapshot_integrity_error"
    assert exc_dup.value.message == "Duplicate service defaults check ID 'E1'"
    assert duplicate_model.call_count == 0


@pytest.mark.anyio
async def test_reflection_pass_service_defaults_stable_id_and_foreign_rule_rejection() -> None:
    query_block_id = file_block_id("app/query.py")
    rule_naming = ReviewRule(
        "Naming Consistency",
        ("*.py",),
        (),
        ("check 1", "check 2", "check 3", "check 4"),
    )
    rule_errors = ReviewRule(
        "Error Handling Standards",
        ("*.py",),
        (),
        ("check 1", "check 2", "check 3", "check 4"),
    )
    snapshot = make_snapshot()
    context = ReviewContext(
        system="SYSTEM",
        rules=(rule_naming, rule_errors),
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=PullRequestMeta("T", None, "u", "b", "main", (), 1, 1, 0, False, False),
        changed_files=(ChangedFile("app/query.py", "modified", (DiffLine(10, "added", "x = 1"),)),),
        omitted_files=(),
        policy_snapshot=snapshot,
    )

    # 1. Matching stable check ID: rule_name="service-defaults:N1" and check_id="N1" -> OK
    cand_ok = CandidateFindingItem(
        candidate_id="c-ok",
        finding=ReviewFindingV2(
            path="app/query.py",
            line=10,
            start_line=None,
            severity="medium",
            category="readability",
            title="Naming violation",
            body="Variable must be snake_case",
            suggestion=None,
            confidence=0.9,
            rule_name="service-defaults:N1",
            evidence_refs=[EvidenceRef(block_id=query_block_id, start_line=10, end_line=10)],
        ),
    )
    resp_ok = {
        "decisions": [
            {
                "candidate_id": "c-ok",
                "status": "supported",
                "reason": "OK",
                "evidence_refs": [{"block_id": query_block_id, "start_line": 10, "end_line": 10}],
                "requirement_refs": [
                    {
                        "block_id": "service-defaults",
                        "start_line": 1,
                        "end_line": 1,
                        "check_id": "N1",
                    }
                ],
            }
        ]
    }
    v_ok = ReflectionPass(FakeVerifierModel(resp_ok))
    res = await v_ok.verify(candidates=(cand_ok,), context=context)
    assert len(res) == 1
    assert res[0].status == "supported"

    # 2. Foreign check ID: candidate is N1,
    # but verifier references E1 (from Error Handling)
    cand_foreign = CandidateFindingItem(
        candidate_id="c-foreign",
        finding=ReviewFindingV2(
            path="app/query.py",
            line=10,
            start_line=None,
            severity="medium",
            category="readability",
            title="Naming violation",
            body="Variable must be snake_case",
            suggestion=None,
            confidence=0.9,
            rule_name="service-defaults:N1",
            evidence_refs=[EvidenceRef(block_id=query_block_id, start_line=10, end_line=10)],
        ),
    )
    resp_foreign = {
        "decisions": [
            {
                "candidate_id": "c-foreign",
                "status": "supported",
                "reason": "Foreign check",
                "evidence_refs": [{"block_id": query_block_id, "start_line": 10, "end_line": 10}],
                "requirement_refs": [
                    {
                        "block_id": "service-defaults",
                        "start_line": 5,
                        "end_line": 5,
                        "check_id": "E1",
                    }
                ],
            }
        ]
    }
    v_foreign = ReflectionPass(FakeVerifierModel(resp_foreign))
    with pytest.raises(
        VerificationInvalidOutputError, match=r"does not match service defaults rule 'N1'"
    ):
        await v_foreign.verify(candidates=(cand_foreign,), context=context)

    # 3. Bogus rule: candidate has rule_name="service-defaults:BOGUS"
    cand_bogus = CandidateFindingItem(
        candidate_id="c-bogus",
        finding=ReviewFindingV2(
            path="app/query.py",
            line=10,
            start_line=None,
            severity="medium",
            category="readability",
            title="Naming violation",
            body="Variable must be snake_case",
            suggestion=None,
            confidence=0.9,
            rule_name="service-defaults:BOGUS",
            evidence_refs=[EvidenceRef(block_id=query_block_id, start_line=10, end_line=10)],
        ),
    )
    resp_bogus = {
        "decisions": [
            {
                "candidate_id": "c-bogus",
                "status": "supported",
                "reason": "Bogus rule",
                "evidence_refs": [{"block_id": query_block_id, "start_line": 10, "end_line": 10}],
                "requirement_refs": [
                    {
                        "block_id": "service-defaults",
                        "start_line": 1,
                        "end_line": 1,
                        "check_id": "N1",
                    }
                ],
            }
        ]
    }
    v_bogus = ReflectionPass(FakeVerifierModel(resp_bogus))
    with pytest.raises(
        VerificationInvalidOutputError,
        match=r"does not match service defaults rule 'BOGUS'",
    ):
        await v_bogus.verify(candidates=(cand_bogus,), context=context)


def test_fit_changed_files_for_verifier_preserves_candidate_anchor_lines() -> None:
    from app.modules.reviews.application.prompt_budget import EstimatedTokenCounter
    from app.modules.reviews.application.reflection_pass import _fit_changed_files_for_verifier

    counter = EstimatedTokenCounter()
    lines = tuple(DiffLine(i, "added", f"x_{i} = {i} * 2") for i in range(1, 101))
    file = ChangedFile("app/main.py", "modified", lines)
    f_block_id = file_block_id("app/main.py")

    # Anchor is at lines 90..95, far past the 35-line prefix budget
    cand = CandidateFindingItem(
        candidate_id="c1",
        finding=ReviewFindingV2(
            path="app/main.py",
            line=95,
            start_line=90,
            severity="high",
            category="correctness",
            title="Logic flaw",
            body="Invalid multiplication",
            suggestion=None,
            confidence=0.9,
            rule_name=None,
            evidence_refs=[EvidenceRef(block_id=f_block_id, start_line=90, end_line=95)],
        ),
    )

    builder = PromptBuilder()
    full_rendered = builder.render_changed_files((file,), include_block_id=True)
    full_tokens = counter.count(f"<code_context>\n{full_rendered}\n</code_context>")

    lines_35_rendered = builder.render_changed_files(
        (ChangedFile("app/main.py", "modified", lines[:35], total_lines=100),),
        include_block_id=True,
    )
    budget = counter.count(f"<code_context>\n{lines_35_rendered}\n</code_context>")
    assert budget < full_tokens

    fitted = _fit_changed_files_for_verifier(
        (file,),
        rules=(),
        max_tokens=budget,
        counter=counter,
        candidates=(cand,),
    )
    assert len(fitted) == 1
    assert len(fitted[0].lines) <= 35
    line_nums = {line.number for line in fitted[0].lines}
    assert set(range(90, 96)).issubset(line_nums)
    assert fitted[0].total_lines == 100


def test_fit_changed_files_for_verifier_prioritizes_candidate_files() -> None:
    from app.modules.reviews.application.prompt_budget import EstimatedTokenCounter
    from app.modules.reviews.application.reflection_pass import _fit_changed_files_for_verifier

    counter = EstimatedTokenCounter()
    builder = PromptBuilder()

    lines_a = tuple(DiffLine(i, "added", f"val_a_{i} = {i}") for i in range(1, 51))
    file_a = ChangedFile("app/unrelated.py", "modified", lines_a)

    lines_b = tuple(DiffLine(i, "added", f"val_b_{i} = {i}") for i in range(1, 51))
    file_b = ChangedFile("app/target.py", "modified", lines_b)
    b_block_id = file_block_id("app/target.py")

    cand = CandidateFindingItem(
        candidate_id="c1",
        finding=ReviewFindingV2(
            path="app/target.py",
            line=10,
            start_line=None,
            severity="critical",
            category="security",
            title="Security flaw",
            body="Insecure operation",
            suggestion=None,
            confidence=0.95,
            rule_name=None,
            evidence_refs=[EvidenceRef(block_id=b_block_id, start_line=10, end_line=10)],
        ),
    )

    one_file_rendered = builder.render_changed_files((file_b,), include_block_id=True)
    budget = counter.count(f"<code_context>\n{one_file_rendered}\n</code_context>")

    fitted = _fit_changed_files_for_verifier(
        (file_a, file_b),
        rules=(),
        max_tokens=budget,
        counter=counter,
        candidates=(cand,),
    )
    paths = [f.path for f in fitted]
    assert "app/target.py" in paths


def test_fit_changed_files_for_verifier_retains_total_lines() -> None:
    from app.modules.reviews.application.prompt_budget import EstimatedTokenCounter
    from app.modules.reviews.application.reflection_pass import _fit_changed_files_for_verifier

    counter = EstimatedTokenCounter()
    builder = PromptBuilder()

    lines = tuple(DiffLine(i, "added", f"line_{i} = {i}") for i in range(1, 101))
    file = ChangedFile("app/cut.py", "modified", lines, total_lines=250)

    cut_rendered = builder.render_changed_files(
        (ChangedFile("app/cut.py", "modified", lines[:40]),), include_block_id=True
    )
    budget = counter.count(f"<code_context>\n{cut_rendered}\n</code_context>")

    fitted = _fit_changed_files_for_verifier(
        (file,),
        rules=(),
        max_tokens=budget,
        counter=counter,
        candidates=(),
    )
    assert len(fitted) == 1
    assert len(fitted[0].lines) < 100
    assert fitted[0].total_lines == 250

    file_no_total = ChangedFile("app/no_total.py", "modified", lines)
    fitted_no_total = _fit_changed_files_for_verifier(
        (file_no_total,),
        rules=(),
        max_tokens=budget,
        counter=counter,
        candidates=(),
    )
    assert len(fitted_no_total) == 1
    assert fitted_no_total[0].total_lines == 100


def test_build_verifier_prompt_mandatory_envelope_overflow_and_missing_v9_asset() -> None:
    from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode

    # 1. Missing prompt asset v9 via __init__
    with pytest.raises(PolicyError) as exc_init:
        VerifierPromptBuilder(verifier_version="v9")
    assert exc_init.value.code == PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR
    assert "Unable to read verifier prompt resource" in str(exc_init.value)

    # 2. Missing prompt asset v9 via pinned bindings in build_verifier_prompt
    snapshot = make_snapshot()
    bindings_v9 = replace(snapshot.prompt_bindings, verifier_version="v9")
    snapshot_v9 = replace(snapshot, prompt_bindings=bindings_v9)
    cand = make_candidate("c1")
    context_v9 = ReviewContext(
        system="SYSTEM",
        rules=(),
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=PullRequestMeta("T", None, "u", "b", "main", (), 1, 1, 0, False, False),
        changed_files=(ChangedFile("app/query.py", "modified", (DiffLine(10, "added", "x = 1"),)),),
        omitted_files=(),
        policy_snapshot=snapshot_v9,
    )
    with pytest.raises(PolicyError) as exc_build:
        VerifierPromptBuilder().build_verifier_prompt(candidates=(cand,), context=context_v9)
    assert exc_build.value.code == PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR
    assert "Unable to read verifier prompt resource" in str(exc_build.value)

    # 3. Mandatory envelope overflow in build_verifier_prompt (even with 0 changed_files)
    tiny_profile = replace(make_complete_execution_profile(), max_input_tokens=8500)
    snapshot_tiny = replace(snapshot, execution_profile=tiny_profile)
    context_overflow = ReviewContext(
        system="SYSTEM",
        rules=(),
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=PullRequestMeta("T", None, "u", "b", "main", (), 0, 0, 0, False, False),
        changed_files=(),
        omitted_files=(),
        policy_snapshot=snapshot_tiny,
    )
    with pytest.raises(RunFailure) as exc_overflow:
        VerifierPromptBuilder().build_verifier_prompt(candidates=(cand,), context=context_overflow)
    assert exc_overflow.value.error_code == "verification_context_overflow"


@pytest.mark.anyio
async def test_reflection_pass_verify_pre_send_guard_and_counter_v1() -> None:
    # 1. Call-level provenance pre-send guard in ReflectionPass.verify:
    # disallowed path "../secret.py" raises policy_context_violation with 0 verifier calls
    bad_path = "../secret.py"
    cand_bad = make_candidate("c-bad", path=bad_path, rule_name=None)
    snapshot = make_snapshot()
    context_bad = ReviewContext(
        system="SYSTEM",
        rules=(),
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=PullRequestMeta(
            "T", None, "u", "b", "main", (), 1, 1, 0, False, False, head_sha="head_1"
        ),
        changed_files=(ChangedFile(bad_path, "modified", (DiffLine(10, "added", "x = 1"),)),),
        omitted_files=(),
        policy_snapshot=snapshot,
    )
    model_bad = FakeVerifierModel({"decisions": []})
    verifier_bad = ReflectionPass(model_bad)
    with pytest.raises(RunFailure) as exc_guard:
        await verifier_bad.verify(candidates=(cand_bad,), context=context_bad)
    assert exc_guard.value.error_code == "policy_context_violation"
    assert model_bad.call_count == 0

    # 2. ReflectionPass.verify with counter/v1 on 4,000-line file + 1 candidate succeeds
    profile_v1 = replace(make_complete_execution_profile(), counter_version="counter/v1")
    snapshot_v1 = replace(make_snapshot(), execution_profile=profile_v1)
    big_lines = tuple(
        DiffLine(i, "added", f"value_{i} = compute_something({i}) + 100") for i in range(1, 4001)
    )
    big_file = ChangedFile("app/big.py", "added", big_lines)
    big_block_id = file_block_id("app/big.py")
    cand_v1 = make_candidate(
        "c-v1",
        path="app/big.py",
        rule_name=None,
        evidence_refs=[EvidenceRef(block_id=big_block_id, start_line=10, end_line=15)],
    )
    context_v1 = ReviewContext(
        system="SYSTEM",
        rules=(),
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=PullRequestMeta(
            "T", None, "u", "b", "main", (), 1, 4000, 0, False, False, head_sha="head_1"
        ),
        changed_files=(big_file,),
        omitted_files=(),
        policy_snapshot=snapshot_v1,
    )
    model_v1 = FakeVerifierModel(
        {
            "decisions": [
                {
                    "candidate_id": "c-v1",
                    "status": "supported",
                    "reason": "Confirmed",
                    "evidence_refs": [{"block_id": big_block_id, "start_line": 10, "end_line": 15}],
                    "requirement_refs": [],
                }
            ]
        }
    )
    decisions_v1 = await ReflectionPass(model_v1).verify(candidates=(cand_v1,), context=context_v1)
    assert model_v1.call_count == 1
    assert len(decisions_v1) == 1
    assert decisions_v1[0].status == "supported"


@pytest.mark.anyio
async def test_reflection_pass_verifier_evidence_revision_mismatch_right_and_left() -> None:
    from unittest.mock import patch

    from app.modules.reviews.application.policy_snapshot import ManifestBlock, SourceRole

    cand = make_candidate("c-rev", path="app/query.py", rule_name=None)
    query_block_id = file_block_id("app/query.py")
    snapshot = make_snapshot()
    base_context = ReviewContext(
        system="SYSTEM",
        rules=(),
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=PullRequestMeta(
            "T",
            None,
            "u",
            "b",
            "main",
            (),
            1,
            1,
            0,
            False,
            False,
            head_sha="head_good",
            diff_old_sha="old_good",
        ),
        changed_files=(ChangedFile("app/query.py", "modified", (DiffLine(10, "added", "x = 1"),)),),
        omitted_files=(),
        policy_snapshot=snapshot,
    )

    builder = VerifierPromptBuilder()
    real_prompt, real_manifest = builder.build_verifier_prompt(
        candidates=(cand,), context=base_context
    )

    # 1. RIGHT block with foreign revision -> VerificationInvalidOutputError
    bad_right_block = ManifestBlock(
        block_id="retrieved-right",
        source_role=SourceRole.RETRIEVED_EXCERPT,
        source_kind="source_code",
        path="app/query.py",
        revision="foreign_head",
        side="RIGHT",
        start_line=1,
        end_line=20,
        shown_line_count=20,
        shown_ranges=((1, 20),),
    )
    manifest_bad_right = replace(real_manifest, blocks=(*real_manifest.blocks, bad_right_block))
    resp_right = {
        "decisions": [
            {
                "candidate_id": "c-rev",
                "status": "supported",
                "reason": "OK",
                "evidence_refs": [
                    {"block_id": "retrieved-right", "start_line": 10, "end_line": 10}
                ],
                "requirement_refs": [],
            }
        ]
    }
    verifier_right = ReflectionPass(FakeVerifierModel(resp_right))
    with (
        patch.object(
            verifier_right._prompt_builder,
            "build_verifier_prompt",
            return_value=(real_prompt, manifest_bad_right),
        ),
        patch("app.modules.reviews.application.reflection_pass.verify_provenance_pre_send_guard"),
        pytest.raises(
            VerificationInvalidOutputError,
            match="revision 'foreign_head' does not match PR revision",
        ),
    ):
        await verifier_right.verify(candidates=(cand,), context=base_context)

    # 2. LEFT block with foreign revision -> VerificationInvalidOutputError
    bad_left_block = ManifestBlock(
        block_id="retrieved-left",
        source_role=SourceRole.RETRIEVED_EXCERPT,
        source_kind="source_code",
        path="app/query.py",
        revision="foreign_old",
        side="LEFT",
        start_line=1,
        end_line=20,
        shown_line_count=20,
        shown_ranges=((1, 20),),
    )
    manifest_bad_left = replace(real_manifest, blocks=(*real_manifest.blocks, bad_left_block))
    resp_left = {
        "decisions": [
            {
                "candidate_id": "c-rev",
                "status": "supported",
                "reason": "OK",
                "evidence_refs": [{"block_id": "retrieved-left", "start_line": 10, "end_line": 10}],
                "requirement_refs": [],
            }
        ]
    }
    verifier_left = ReflectionPass(FakeVerifierModel(resp_left))
    with (
        patch.object(
            verifier_left._prompt_builder,
            "build_verifier_prompt",
            return_value=(real_prompt, manifest_bad_left),
        ),
        patch("app.modules.reviews.application.reflection_pass.verify_provenance_pre_send_guard"),
        pytest.raises(
            VerificationInvalidOutputError,
            match="LEFT revision 'foreign_old' does not match PR diff_old_sha 'old_good'",
        ),
    ):
        await verifier_left.verify(candidates=(cand,), context=base_context)

    # 3. LEFT block when diff_old_sha is None -> fail-closed VerificationInvalidOutputError
    good_left_block = replace(bad_left_block, revision="old_good")
    manifest_good_left = replace(real_manifest, blocks=(*real_manifest.blocks, good_left_block))
    context_no_old_sha = replace(
        base_context,
        pr_meta=replace(base_context.pr_meta, diff_old_sha=None),
    )
    verifier_no_old = ReflectionPass(FakeVerifierModel(resp_left))
    with (
        patch.object(
            verifier_no_old._prompt_builder,
            "build_verifier_prompt",
            return_value=(real_prompt, manifest_good_left),
        ),
        patch("app.modules.reviews.application.reflection_pass.verify_provenance_pre_send_guard"),
        pytest.raises(
            VerificationInvalidOutputError,
            match="LEFT revision 'old_good' does not match PR diff_old_sha 'None'",
        ),
    ):
        await verifier_no_old.verify(candidates=(cand,), context=context_no_old_sha)

    # 4. LEFT block when diff_old_sha matches ("old_good") -> accepted!
    verifier_ok_left = ReflectionPass(FakeVerifierModel(resp_left))
    with patch.object(
        verifier_ok_left._prompt_builder,
        "build_verifier_prompt",
        return_value=(real_prompt, manifest_good_left),
    ):
        decisions_ok = await verifier_ok_left.verify(candidates=(cand,), context=base_context)
    assert len(decisions_ok) == 1
    assert decisions_ok[0].status == "supported"
    assert decisions_ok[0].evidence_refs[0].block_id == "retrieved-left"
    assert query_block_id in real_prompt.user


def test_build_verifier_prompt_counter_v1_fitting() -> None:
    snapshot = make_snapshot()
    exec_v1 = replace(
        snapshot.execution_profile, counter_version="counter/v1", max_input_tokens=12000
    )
    snapshot_v1 = replace(snapshot, execution_profile=exec_v1)

    lines = tuple(
        DiffLine(i, "added", f"line_{i:04d} = 'sample payload content for verifier'")
        for i in range(1, 1500)
    )
    cand = make_candidate("c1", path="app/query.py")
    context_v1 = ReviewContext(
        system="SYSTEM",
        rules=(),
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=PullRequestMeta("T", None, "u", "b", "main", (), 1, 1, 0, False, False),
        changed_files=(ChangedFile("app/query.py", "modified", lines),),
        omitted_files=(),
        policy_snapshot=snapshot_v1,
    )
    prompt_v1, _ = VerifierPromptBuilder().build_verifier_prompt(
        candidates=(cand,), context=context_v1
    )
    line_count_v1 = prompt_v1.user.count("<line n=")

    exec_v2 = replace(
        snapshot.execution_profile, counter_version="counter/v2", max_input_tokens=12000
    )
    snapshot_v2 = replace(snapshot, execution_profile=exec_v2)
    context_v2 = replace(context_v1, policy_snapshot=snapshot_v2)
    prompt_v2, _ = VerifierPromptBuilder().build_verifier_prompt(
        candidates=(cand,), context=context_v2
    )
    line_count_v2 = prompt_v2.user.count("<line n=")

    assert line_count_v1 > line_count_v2
    # Verify that prompt_v1 line count strictly matches v1 line count and would fail under v2
    assert line_count_v1 == 140
    assert line_count_v2 == 92


@pytest.fixture
def reflection_boundary_context() -> ReviewContext:
    return ReviewContext(
        system="SYSTEM",
        rules=(),
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=PullRequestMeta("T", None, "u", "b", "main", (), 1, 6, 0, False, False),
        changed_files=(
            ChangedFile(
                "app/query.py",
                "modified",
                tuple(DiffLine(n, "added", "x = 1") for n in range(10, 16)),
            ),
        ),
        omitted_files=(),
        policy_snapshot=make_snapshot(),
    )


@pytest.mark.parametrize("status", ["contradicted", "insufficient_context"])
@pytest.mark.parametrize(
    ("requirement", "expected_error"),
    [
        pytest.param(
            RequirementRef(block_id="hidden-policy", start_line=1, end_line=1, check_id=None),
            "requirement_ref block 'hidden-policy' not found in manifest",
            id="unknown-block",
        ),
        pytest.param(
            RequirementRef(block_id="policy-rules", start_line=4, end_line=4, check_id=None),
            "requirement_ref range 4-4 not shown in 'policy-rules'",
            id="hidden-line",
        ),
        pytest.param(
            RequirementRef(
                block_id=file_block_id("app/query.py"), start_line=10, end_line=10, check_id=None
            ),
            "has invalid role 'diff'",
            id="non-policy-block",
        ),
    ],
)
def test_reflection_pass_validates_supplied_refs_for_non_supported_statuses(
    reflection_boundary_context: ReviewContext,
    status: VerificationStatus,
    requirement: RequirementRef,
    expected_error: str,
) -> None:
    candidate = make_candidate("c-ref", rule_name=None)
    model = FakeVerifierModel(
        {
            "decisions": [
                {
                    "candidate_id": "c-ref",
                    "status": status,
                    "reason": "The claim is not supported.",
                    "evidence_refs": [
                        {
                            "block_id": file_block_id("app/query.py"),
                            "start_line": 10,
                            "end_line": 15,
                        }
                    ],
                    "requirement_refs": [requirement.model_dump()],
                }
            ]
        }
    )
    with pytest.raises(VerificationInvalidOutputError) as error:
        asyncio.run(
            ReflectionPass(model).verify(
                candidates=(candidate,), context=reflection_boundary_context
            )
        )
    assert error.value.error_code == "verification_invalid_output"
    assert expected_error in str(error.value)
    assert model.call_count == 1


@pytest.mark.parametrize("rule_name", [".review/rules.md", "AGENTS.md"])
def test_reflection_pass_rejects_attributed_insufficient_context_without_requirement_refs(
    reflection_boundary_context: ReviewContext, rule_name: str
) -> None:
    snapshot = reflection_boundary_context.policy_snapshot
    assert snapshot is not None
    context = replace(
        reflection_boundary_context,
        policy_snapshot=replace(
            snapshot,
            agents=make_policy_document_present("AGENTS.md", "Preserve stored data.\n"),
        ),
    )
    candidate = make_candidate("c-unknown", rule_name=rule_name)
    model = FakeVerifierModel(
        {
            "decisions": [
                {
                    "candidate_id": "c-unknown",
                    "status": "insufficient_context",
                    "reason": "The cited policy does not establish this claim.",
                    "evidence_refs": [],
                    "requirement_refs": [],
                }
            ]
        }
    )
    decisions = asyncio.run(ReflectionPass(model).verify(candidates=(candidate,), context=context))
    assert len(decisions) == 1
    assert decisions[0].candidate_id == "c-unknown"
    assert decisions[0].status == "insufficient_context"
    assert decisions[0].requirement_refs == []
    supported, rejected = filter_verified_candidates(candidates=(candidate,), decisions=decisions)
    assert supported == []
    assert rejected == [
        (candidate, "insufficient_context", "The cited policy does not establish this claim.")
    ]
    assert model.call_count == 1


@pytest.mark.parametrize("send_budget", [152, 151], ids=["exact", "one-over"])
def test_reflection_pass_final_budget_counts_system_and_user(
    reflection_boundary_context: ReviewContext,
    send_budget: int,
) -> None:
    # counter/v1: SYSTEM is 2 tokens; this fixed user envelope is 150 tokens.
    # Empty diff context prevents fitting from hiding the final one-token overflow.
    snapshot = make_snapshot()
    snapshot = replace(
        snapshot,
        rules=make_policy_document_missing(".review/rules.md"),
        prompt_bindings=replace(
            snapshot.prompt_bindings,
            verifier_checksum="d621c1a7169f2ca51bc8674da52e9572178a66a1dde88da24da78fe4951703f9",
        ),
        execution_profile=replace(
            snapshot.execution_profile,
            counter_version="counter/v1",
            # The pinned mistral-small-4 reserves 8,000 output tokens.
            max_input_tokens=8_000 + send_budget,
        ),
    )
    context = replace(reflection_boundary_context, changed_files=(), policy_snapshot=snapshot)
    candidate = make_candidate("c-budget", rule_name=None)
    model = FakeVerifierModel(
        {
            "decisions": [
                {
                    "candidate_id": "c-budget",
                    "status": "insufficient_context",
                    "reason": "The diff is unavailable.",
                    "evidence_refs": [],
                    "requirement_refs": [],
                }
            ]
        }
    )
    verifier = ReflectionPass(model, system_prompt="SYSTEM")
    if send_budget == 152:
        decisions = asyncio.run(verifier.verify(candidates=(candidate,), context=context))
        assert tuple(decision.status for decision in decisions) == ("insufficient_context",)
        assert model.call_count == 1
        assert model.last_system_prompt == "SYSTEM"
        assert "<code_context>" in model.last_user_prompt
    else:
        with pytest.raises(RunFailure) as error:
            asyncio.run(verifier.verify(candidates=(candidate,), context=context))
        assert error.value.error_code == "verification_context_overflow"
        assert error.value.message == "Verifier prompt is 152 tokens, limit is 151"
        assert model.call_count == 0


@pytest.mark.parametrize("send_budget", [954, 953], ids=["exact", "one-over"])
def test_verifier_mandatory_budget_accepts_exact_limit(
    reflection_boundary_context: ReviewContext,
    monkeypatch: pytest.MonkeyPatch,
    send_budget: int,
) -> None:
    # Pinned v2 system prompt plus the two absent-policy envelopes: 954 counter/v1 tokens.
    monkeypatch.setenv("LLM_CONTEXT_WINDOW", "1000000")
    monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", "0")
    snapshot = make_snapshot()
    snapshot = replace(
        snapshot,
        rules=make_policy_document_missing(".review/rules.md"),
        execution_profile=replace(snapshot.execution_profile, counter_version="counter/v1"),
    )
    context = replace(reflection_boundary_context, policy_snapshot=snapshot)
    if send_budget == 954:
        verify_verifier_mandatory_budget(context, max_send_budget=send_budget)
    else:
        with pytest.raises(PolicyError) as error:
            verify_verifier_mandatory_budget(context, max_send_budget=send_budget)
        assert error.value.error_code == "policy_prompt_budget_exceeded"
        assert error.value.limit == 953
        assert error.value.actual == 954


def test_reflection_pass_rejects_blank_verifier_checksum_before_model_call(
    reflection_boundary_context: ReviewContext,
) -> None:
    snapshot = make_snapshot()
    snapshot = replace(
        snapshot, prompt_bindings=replace(snapshot.prompt_bindings, verifier_checksum="")
    )
    context = replace(reflection_boundary_context, policy_snapshot=snapshot)
    candidate = make_candidate("c-empty-checksum", rule_name=None)
    model = FakeVerifierModel(
        {
            "decisions": [
                {
                    "candidate_id": "c-empty-checksum",
                    "status": "insufficient_context",
                    "reason": "No supported violation.",
                    "evidence_refs": [],
                    "requirement_refs": [],
                }
            ]
        }
    )
    with pytest.raises(PolicyError) as error:
        asyncio.run(ReflectionPass(model).verify(candidates=(candidate,), context=context))
    assert error.value.error_code == "policy_snapshot_integrity_error"
    assert "Verifier prompt checksum mismatch: expected ''" in error.value.message
    assert model.call_count == 0


# ---------- #130 PR E: the verifier port gets its manifest and context ----------


def _supported_c1_answer() -> dict[str, Any]:
    return {
        "decisions": [
            {
                "candidate_id": "c-1",
                "status": "supported",
                "reason": "Direct SQL injection visible on line 10.",
                "evidence_refs": [
                    {"block_id": file_block_id("app/query.py"), "start_line": 10, "end_line": 10}
                ],
                "requirement_refs": [
                    {"block_id": "policy-rules", "start_line": 1, "end_line": 2, "check_id": None}
                ],
            }
        ]
    }


def _query_context() -> ReviewContext:
    return ReviewContext(
        system="SYSTEM",
        rules=(),
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=PullRequestMeta("T", None, "u", "b", "main", (), 1, 1, 0, False, False),
        changed_files=(
            ChangedFile("app/query.py", "modified", (DiffLine(10, "added", "query = f'SELECT'"),)),
        ),
        omitted_files=(),
        policy_snapshot=make_snapshot(),
    )


class RecordingVerifierModel:
    """Keeps what ``ReflectionPass`` hands the model besides the two prompts."""

    def __init__(self, reply: Any) -> None:
        self.reply = reply
        self.manifests: list[CallManifest] = []
        self.contexts: list[ReviewContext] = []

    async def verify(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        manifest: CallManifest,
        context: ReviewContext,
    ) -> Any:
        self.manifests.append(manifest)
        self.contexts.append(context)
        return self.reply


@pytest.mark.anyio
async def test_reflection_pass_hands_the_model_its_manifest_and_context() -> None:
    model = RecordingVerifierModel(_supported_c1_answer())
    context = _query_context()

    decisions = await ReflectionPass(model).verify(
        candidates=(make_candidate("c-1"),), context=context
    )

    assert [d.candidate_id for d in decisions] == ["c-1"]
    assert len(model.manifests) == 1
    manifest = model.manifests[0]
    assert manifest.stage == "verification"
    assert manifest.recovery_kind == "none"
    candidate = manifest.get_block("c-1")
    assert candidate is not None
    assert candidate.source_role == "candidate"
    assert candidate.source_kind == "candidate_findings"
    assert candidate.path == "app/query.py"
    assert (candidate.start_line, candidate.end_line) == (10, 15)
    assert manifest.get_block(file_block_id("app/query.py")) is not None
    assert len(model.contexts) == 1
    assert model.contexts[0] is context


@pytest.mark.anyio
async def test_reflection_pass_unwraps_a_verifier_reply() -> None:
    context = _query_context()
    plain = await ReflectionPass(RecordingVerifierModel(_supported_c1_answer())).verify(
        candidates=(make_candidate("c-1"),), context=context
    )
    reply = VerifierReply(
        output=_supported_c1_answer(),
        accepted_call_id=uuid.UUID("00000000-0000-0000-0000-000000013061"),
    )

    wrapped = await ReflectionPass(RecordingVerifierModel(reply)).verify(
        candidates=(make_candidate("c-1"),), context=context
    )

    assert wrapped == plain
    assert [(d.candidate_id, d.status) for d in wrapped] == [("c-1", "supported")]


# ---------- #130 PR E: verifier payment, overflow and rate-limit failures ----------


async def _verify_with_failure(exc: Exception) -> None:
    await ReflectionPass(CustomModel(exc)).verify(
        candidates=(make_candidate("c-1"),), context=_query_context()
    )


@pytest.mark.anyio
async def test_verifier_payment_required_fails_the_run_without_a_retry() -> None:
    with pytest.raises(RunFailure) as caught:
        await _verify_with_failure(LlmCallFailed(LlmErrorCode.PAYMENT_REQUIRED, "402"))

    assert caught.value.error_code == "llm_payment_required"
    assert caught.value.retry_after is None
    assert "llm_payment_required" not in RETRYABLE_ERROR_CODES


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failure",
    [
        LlmCallFailed(LlmErrorCode.CONTEXT_OVERFLOW, "too long"),
        # The code a verifier model raised before the gateway adapter.
        RunFailure("verification_context_overflow", "too long"),
    ],
    ids=["gateway-overflow", "verifier-overflow"],
)
async def test_verifier_context_overflow_is_a_verification_overflow(failure: Exception) -> None:
    with pytest.raises(RunFailure) as caught:
        await _verify_with_failure(failure)

    assert caught.value.error_code == "verification_context_overflow"
    assert caught.value.message == "Verifier prompt context overflow"
    assert "verification_context_overflow" not in RETRYABLE_ERROR_CODES


@pytest.mark.parametrize(
    ("retry_after_s", "retry_after", "delay"),
    [
        (45.0, timedelta(seconds=45), ("2m", timedelta(minutes=2))),
        (300.0, timedelta(seconds=300), ("10m", timedelta(minutes=10))),
        (None, timedelta(0), ("2m", timedelta(minutes=2))),
        # A huge header must not overflow timedelta: the delay is capped at one day.
        (86_401.0, timedelta(days=1), ("10m", timedelta(minutes=10))),
        (1e20, timedelta(days=1), ("10m", timedelta(minutes=10))),
    ],
    ids=["retry-after-45s", "retry-after-300s", "no-retry-after", "over-one-day", "1e20"],
)
@pytest.mark.anyio
async def test_verifier_rate_limit_carries_the_provider_retry_after(
    retry_after_s: float | None, retry_after: timedelta, delay: tuple[str, timedelta]
) -> None:
    with pytest.raises(RunFailure) as caught:
        await _verify_with_failure(
            LlmCallFailed(LlmErrorCode.RATE_LIMITED, "429", retry_after_s=retry_after_s)
        )

    assert caught.value.error_code == "verification_unavailable"
    assert caught.value.retry_after == retry_after
    assert "verification_unavailable" in RETRYABLE_ERROR_CODES
    assert RetryDelays().for_failure(1, caught.value) == delay


@pytest.mark.parametrize(
    "message",
    ["503 from the provider", "attempt 1 already made 4 provider calls"],
    ids=["provider-503", "call-cap"],
)
@pytest.mark.anyio
async def test_verifier_unavailable_stays_a_short_retry(message: str) -> None:
    with pytest.raises(VerificationUnavailableError, match="Verifier model unavailable") as caught:
        await _verify_with_failure(LlmCallFailed(LlmErrorCode.UNAVAILABLE, message))

    failure = classify_failure(caught.value)
    assert failure.error_code == "verification_unavailable"
    assert failure.retry_after is None
    assert RetryDelays().for_failure(1, failure) == ("30s", timedelta(seconds=30))


@pytest.mark.anyio
async def test_verifier_invalid_output_stays_terminal() -> None:
    with pytest.raises(VerificationInvalidOutputError, match="Verifier model output invalid"):
        await _verify_with_failure(LlmCallFailed(LlmErrorCode.INVALID_OUTPUT, "bad"))

    assert "verification_invalid_output" not in RETRYABLE_ERROR_CODES
