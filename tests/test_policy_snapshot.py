"""Policy snapshot contract: an empty document never shares an identity with a missing one."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, replace
from uuid import UUID

import pytest

from app.modules.reviews.application.policy_snapshot import (
    AGENTS_DOCUMENT_PATH,
    POLICY_SNAPSHOT_SCHEMA_VERSION,
    RULES_DOCUMENT_PATH,
    DocumentPresence,
    ExecutionProfile,
    PolicyDocument,
    PolicySnapshot,
    PromptBindings,
)

_EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _missing(path: str) -> PolicyDocument:
    return PolicyDocument(path, DocumentPresence.MISSING, None, None, None, None, None, None)


def _empty(path: str) -> PolicyDocument:
    return PolicyDocument(
        path, DocumentPresence.PRESENT, "f" * 40, _EMPTY_SHA256, _EMPTY_SHA256, "", 0, 0
    )


def _snapshot(rules: PolicyDocument, agents: PolicyDocument) -> PolicySnapshot:
    return PolicySnapshot(
        schema_version=POLICY_SNAPSHOT_SCHEMA_VERSION,
        snapshot_id=UUID("00000000-0000-0000-0000-000000000003"),
        run_id=UUID("00000000-0000-0000-0000-000000000002"),
        repository_id=UUID("00000000-0000-0000-0000-000000000001"),
        policy_base_sha="b" * 40,
        rules=rules,
        agents=agents,
        ignore_patterns=("generated/**",),
        ignore_digest="1" * 64,
        parser_profile="rules-markdown/v1",
        parser_version="4.0.0",
        matcher_version="review-ignore/v1",
        limits_version="policy-limits/v1",
        builtin_exclusions_version="builtin-exclusions/v1",
        priority_matcher_version="priority-matcher/v1",
        service_defaults_rule_version_id=UUID("00000000-0000-0000-0000-000000000004"),
        defaults_checksum="2" * 64,
        prompt_bindings=PromptBindings("4" * 64, "5" * 64, "6" * 64),
        execution_profile=ExecutionProfile.fast("model-a", "model-b"),
        policy_digest="3" * 64,
    )


def test_empty_document_is_present_and_differs_from_a_missing_one() -> None:
    empty, missing = _empty(RULES_DOCUMENT_PATH), _missing(RULES_DOCUMENT_PATH)

    assert empty.presence is DocumentPresence.PRESENT
    assert (empty.text, empty.raw_sha256) == ("", _EMPTY_SHA256)
    assert (missing.text, missing.raw_sha256, missing.blob_oid) == (None, None, None)
    assert empty != missing


def test_document_content_must_match_its_presence() -> None:
    with pytest.raises(ValueError, match="missing policy document"):
        replace(_missing(RULES_DOCUMENT_PATH), text="")
    with pytest.raises(ValueError, match="present policy document"):
        replace(_empty(RULES_DOCUMENT_PATH), text_sha256=None)


def test_snapshot_binds_exactly_the_two_policy_documents() -> None:
    snapshot = _snapshot(_empty(RULES_DOCUMENT_PATH), _missing(AGENTS_DOCUMENT_PATH))

    assert snapshot.schema_version == 1
    assert (snapshot.rules.path, snapshot.agents.path) == (".review/rules.md", "AGENTS.md")
    with pytest.raises(ValueError, match="fixed paths"):
        _snapshot(_missing(AGENTS_DOCUMENT_PATH), _missing(RULES_DOCUMENT_PATH))


def test_prompt_bindings_are_complete_only_when_all_three_prompts_are_pinned() -> None:
    complete = PromptBindings("4" * 64, "5" * 64, "6" * 64)

    assert complete.is_complete is True
    assert (complete.review_system_key, complete.conventions_key, complete.verifier_key) == (
        "review.system",
        "review.conventions",
        "review.verifier",
    )
    assert PromptBindings("4" * 64, "5" * 64, "").is_complete is False


def test_execution_profiles_carry_the_budgets_of_the_specification() -> None:
    fast = ExecutionProfile.fast("model-a", "model-b")
    deep = ExecutionProfile.deep("model-a", "model-b", "model-c")

    assert (fast.profile_name, fast.max_policy_tokens, fast.max_input_tokens) == (
        "fast",
        12_000,
        60_000,
    )
    assert (deep.profile_name, deep.max_policy_tokens, deep.max_input_tokens) == (
        "deep",
        12_000,
        150_000,
    )
    assert (fast.max_calls_per_run, deep.max_calls_per_run) == (4, 4)
    assert (fast.conventions_model, deep.conventions_model) == (None, "model-c")


def test_pinned_versions_cannot_change_after_the_snapshot_is_built() -> None:
    snapshot = _snapshot(_empty(RULES_DOCUMENT_PATH), _missing(AGENTS_DOCUMENT_PATH))

    for pinned in (snapshot.prompt_bindings, snapshot.execution_profile):
        # Scalars only: nothing inside can be mutated through a shared reference.
        assert all(
            isinstance(getattr(pinned, item.name), str | int | None) for item in fields(pinned)
        )
    with pytest.raises(FrozenInstanceError):
        snapshot.prompt_bindings.verifier_checksum = "0" * 64  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        snapshot.execution_profile.max_policy_tokens = 1  # type: ignore[misc]
