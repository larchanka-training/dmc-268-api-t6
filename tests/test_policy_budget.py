"""Unit tests for Policy Token Budget and Pre-Send Guards (Issue #120)."""

from __future__ import annotations

import uuid

import pytest

from app.modules.reviews.application.policy_snapshot import (
    ExecutionProfile,
    PolicySnapshot,
    PromptBindings,
    make_policy_document_missing,
    make_policy_document_present,
)
from app.modules.reviews.application.prompt_budget import (
    PolicyBudgetExceededError,
    PreSendGuardError,
    policy_tokens,
    verify_policy_budget,
    verify_pre_send_guard,
)
from app.modules.reviews.application.prompt_builder import (
    ChangedFile,
    DiffLine,
    PullRequestMeta,
    RepoConventions,
    ReviewContext,
)


class CharCounter:
    """1 char = 1 token for predictable assertions."""

    def count(self, text: str) -> int:
        return len(text)


def make_snapshot(rules_text: str = "", agents_text: str = "") -> PolicySnapshot:
    return PolicySnapshot(
        schema_version=1,
        snapshot_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        repository_id=uuid.uuid4(),
        policy_base_sha="sha_base",
        rules=(
            make_policy_document_present(".review/rules.md", rules_text)
            if rules_text
            else make_policy_document_missing(".review/rules.md")
        ),
        agents=(
            make_policy_document_present("AGENTS.md", agents_text)
            if agents_text
            else make_policy_document_missing("AGENTS.md")
        ),
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
        prompt_bindings=PromptBindings("cs1", "cs2", "cs3"),
        execution_profile=ExecutionProfile.fast("m1", "m2"),
        policy_digest="digest_base",
    )


def test_policy_tokens_counts_rendered_documents() -> None:
    counter = CharCounter()
    snapshot = make_snapshot(
        rules_text="# Security\n1. Validate input.\n",
        agents_text="# Architecture\nUse UoW.\n",
    )
    tokens = policy_tokens(snapshot, counter)
    # Exact rendered character length of policy XML blocks per specification
    expected_rules = (
        '<repository_rules block_id="policy-rules" path=".review/rules.md">\n'
        '<line n="1"># Security</line>\n'
        '<line n="2">1. Validate input.</line>\n'
        "</repository_rules>"
    )
    expected_agents = (
        '<project_conventions_document block_id="policy-agents" path="AGENTS.md">\n'
        '<line n="1"># Architecture</line>\n'
        '<line n="2">Use UoW.</line>\n'
        "</project_conventions_document>"
    )
    expected_tokens = counter.count(expected_rules) + counter.count(expected_agents)
    assert tokens == expected_tokens


def test_verify_policy_budget_passes_within_limit() -> None:
    counter = CharCounter()
    snapshot = make_snapshot(rules_text="Short rule\n")
    # Default limit is 12,000 tokens
    verify_policy_budget(snapshot, counter, max_policy_tokens=12_000)


def test_verify_policy_budget_exceeded_raises_error() -> None:
    counter = CharCounter()
    # Create rule text that renders to > 100 characters, with max_policy_tokens=100
    large_rule = "Rule line " * 20 + "\n"
    snapshot = make_snapshot(rules_text=large_rule)
    with pytest.raises(PolicyBudgetExceededError) as exc_info:
        verify_policy_budget(snapshot, counter, max_policy_tokens=100)
    assert "policy_prompt_budget_exceeded" in str(exc_info.value)


def test_pre_send_guard_verifies_policy_and_total_budget() -> None:
    counter = CharCounter()
    snapshot = make_snapshot(rules_text="Small rule\n")
    context = ReviewContext(
        system="SYSTEM",
        rules=(),
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=PullRequestMeta("Title", None, "dev", "b", "main", (), 1, 1, 0, False, False),
        changed_files=(ChangedFile("app/a.py", "modified", (DiffLine(1, "added", "x = 1"),)),),
        omitted_files=(),
        policy_snapshot=snapshot,
    )

    # Should pass with generous limit
    verify_pre_send_guard(context, counter, max_input_tokens=10_000)

    # Should fail if total exceeds limit
    with pytest.raises(PreSendGuardError) as exc_info:
        verify_pre_send_guard(context, counter, max_input_tokens=50)
    assert "llm_context_overflow" in str(exc_info.value)
