"""Unit tests for Reflection Pass and verifier engine (Issue #120)."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

import pytest
from pydantic import ValidationError

from app.modules.reviews.application.policy_snapshot import (
    PolicySnapshot,
    make_complete_execution_profile,
    make_complete_prompt_bindings,
    make_policy_document_missing,
    make_policy_document_present,
)
from app.modules.reviews.application.prompt_builder import (
    ChangedFile,
    DiffLine,
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
    VerificationUnavailableError,
    VerifierPromptBuilder,
    filter_verified_candidates,
)
from app.modules.reviews.application.review_output import (
    EvidenceRef,
    RequirementRef,
    ReviewFindingV2,
)
from app.modules.reviews.application.run_failures import RunFailure


class CustomModel:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def verify(self, *, system_prompt: str, user_prompt: str) -> Mapping[str, Any] | str:
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
        defaults_checksum="defaults_0",
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
        self, *, system_prompt: str, user_prompt: str
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
    assert manifest.stage == "verifier"
    assert manifest.get_block("policy-rules") is not None
    assert manifest.get_block(file_block_id("app/query.py")) is not None


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
async def test_reflection_pass_rejects_evidence_with_mismatched_path() -> None:
    cand1 = make_candidate("c-1", path="app/query.py")
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
        pr_meta=PullRequestMeta("T", None, "u", "b", "main", (), 2, 2, 0, False, False),
        changed_files=(
            ChangedFile("app/query.py", "modified", (DiffLine(10, "added", "x = 1"),)),
            ChangedFile("app/other.py", "modified", (DiffLine(5, "added", "y = 2"),)),
        ),
        omitted_files=(),
        policy_snapshot=snapshot,
    )

    with pytest.raises(VerificationInvalidOutputError, match="does not match candidate path"):
        await verifier.verify(candidates=(cand1,), context=context)


@pytest.mark.anyio
async def test_reflection_pass_validates_service_defaults_check_id() -> None:
    from app.modules.reviews.application.prompt_builder import ReviewRule

    cand1 = make_candidate("c-1", path="app/query.py", rule_name="service-defaults:rule-1")
    query_block_id = file_block_id("app/query.py")

    rules = (
        ReviewRule(name="rule-1", include=(), exclude=(), checks=("Check 1",)),
        ReviewRule(name="rule-2", include=(), exclude=(), checks=("Check 2",)),
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
        VerificationInvalidOutputError, match="does not match service defaults rules"
    ):
        await verifier_bad.verify(candidates=(cand1,), context=context)

    # 2. check_id exists in rules ("rule-2"), but candidate rule_id is "rule-1"
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
                        "start_line": 1,
                        "end_line": 1,
                        "check_id": "rule-2",
                    }
                ],
            }
        ]
    }
    verifier_mismatch = ReflectionPass(FakeVerifierModel(mismatched_id_response))
    with pytest.raises(VerificationInvalidOutputError, match="does not match rule_id 'rule-1'"):
        await verifier_mismatch.verify(candidates=(cand1,), context=context)

    # 3. Positive case: check_id matches candidate rule_id
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
                        "check_id": "rule-1",
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

    # Profile with tiny max_input_tokens = 5
    tiny_profile = replace(make_complete_execution_profile(), max_input_tokens=5)
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
    rule = ReviewRule("sec-rule", ("*.py",), (), ("Never format SQL",))
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
    # execution_profile with very low max_input_tokens
    import dataclasses

    profile = dataclasses.replace(make_complete_execution_profile(), max_input_tokens=10)
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
    rule = ReviewRule("service-rule", ("*.py",), (), ("Check sql",))
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
            rule_name="service-defaults:service-rule",
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

    # 2. requirement_ref block does not match finding rule_name
    cand2 = CandidateFindingItem(
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
    v2 = ReflectionPass(
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
                                "check_id": "service-rule",
                            }
                        ],
                    }
                ]
            }
        )
    )
    with pytest.raises(VerificationInvalidOutputError, match="does not match finding rule_name"):
        await v2.verify(candidates=(cand2,), context=context)
