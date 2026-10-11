"""Policy snapshot contract: an empty document never shares an identity with a missing one."""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import pytest

from app.modules.reviews.application.policy_snapshot import (
    AGENTS_DOCUMENT_PATH,
    POLICY_SNAPSHOT_SCHEMA_VERSION,
    RULES_DOCUMENT_PATH,
    DocumentPresence,
    PolicyDocument,
    PolicySnapshot,
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
        prompt_bindings={},
        execution_profile={},
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
