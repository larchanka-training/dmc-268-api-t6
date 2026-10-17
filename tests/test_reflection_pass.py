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
)
from app.modules.reviews.application.reflection_pass import (
    CandidateFindingItem,
    ReflectionPass,
    VerificationDecision,
    VerificationInvalidOutputError,
    VerifierPromptBuilder,
    filter_verified_candidates,
)
from app.modules.reviews.application.review_output import (
    EvidenceRef,
    RequirementRef,
    ReviewFindingV2,
)


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
    rule_name: str | None = ".review/rules.md",
    title: str = "SQL injection",
) -> CandidateFindingItem:
    finding = ReviewFindingV2(
        path="app/query.py",
        line=15,
        start_line=10,
        severity="high",
        category="security",
        title=title,
        body="Raw query formatting",
        suggestion=None,
        confidence=0.9,
        rule_name=rule_name,
        evidence_refs=[EvidenceRef(block_id="file-app-query-py", start_line=10, end_line=15)],
    )
    return CandidateFindingItem(candidate_id=candidate_id, finding=finding)


class FakeVerifierModel:
    def __init__(self, response_payload: Mapping[str, Any] | str) -> None:
        self.response_payload = response_payload
        self.call_count = 0
        self.last_system_prompt = ""
        self.last_user_prompt = ""

    async def verify(self, *, system_prompt: str, user_prompt: str) -> Mapping[str, Any] | str:
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
            evidence_refs=[],
            requirement_refs=[],
        )


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
    assert manifest.get_block("file-app-query-py") is not None


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

    fake_response = {
        "decisions": [
            {
                "candidate_id": "c-1",
                "status": "supported",
                "reason": "Direct SQL injection visible on line 10.",
                "evidence_refs": [
                    {"block_id": "file-app-query-py", "start_line": 10, "end_line": 10}
                ],
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
                "evidence_refs": [
                    {"block_id": "file-app-query-py", "start_line": 10, "end_line": 10}
                ],
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

    # Missing decision for c-2
    fake_response = {
        "decisions": [
            {
                "candidate_id": "c-1",
                "status": "supported",
                "reason": "OK",
                "evidence_refs": [
                    {"block_id": "file-app-query-py", "start_line": 10, "end_line": 10}
                ],
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

    with pytest.raises(VerificationInvalidOutputError):
        await verifier.verify(candidates=(cand1, cand2), context=context)
