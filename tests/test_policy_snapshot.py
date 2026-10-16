"""Policy snapshot contract: an empty document never shares an identity with a missing one."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, replace
from uuid import UUID

import pytest

from app.modules.reviews.application.policy_snapshot import (
    AGENTS_DOCUMENT_PATH,
    POLICY_SNAPSHOT_SCHEMA_VERSION,
    RULES_DOCUMENT_PATH,
    CallManifest,
    DocumentPresence,
    ExecutionProfile,
    ManifestBlock,
    PolicyDocument,
    PolicySnapshot,
    PromptBindings,
    make_policy_document_empty,
    make_policy_document_missing,
    make_policy_document_present,
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


def _bindings(**changes: object) -> PromptBindings:
    values: dict[str, object] = {
        "review_system_checksum": "4" * 64,
        "conventions_checksum": "5" * 64,
        "verifier_checksum": "6" * 64,
        "review_system_id": UUID(int=21),
        "conventions_id": UUID(int=22),
        "verifier_id": UUID(int=23),
    }
    return PromptBindings(**{**values, **changes})  # type: ignore[arg-type]


def _profile(**changes: object) -> ExecutionProfile:
    values: dict[str, object] = {
        "profile_name": "fast",
        "review_model": "model-a",
        "verifier_model": "model-b",
        "conventions_model": "model-c",
        "fallback_review_model": "model-d",
        "fallback_verifier_model": "model-e",
        "fallback_conventions_model": "model-f",
        "primary_provider_route": "route-primary",
        "fallback_provider_route": "route-fallback",
        "tokenizer_version": "tokenizer/v1",
        "counter_version": "counter/v1",
        "temperature": "0.2",
        "response_schema_version": "review-output/v2",
        "recovery_profile_version": "recovery/v1",
        "budget_profile_version": "budget/v1",
    }
    return ExecutionProfile(**{**values, **changes})  # type: ignore[arg-type]


def test_prompt_bindings_pin_every_prompt_by_id_and_checksum() -> None:
    complete = _bindings()

    assert complete.is_complete is True
    assert (complete.review_system_key, complete.conventions_key, complete.verifier_key) == (
        "review.system",
        "review.conventions",
        "review.verifier",
    )
    assert (complete.review_system_id, complete.conventions_id, complete.verifier_id) == (
        UUID(int=21),
        UUID(int=22),
        UUID(int=23),
    )
    assert _bindings(verifier_checksum="").is_complete is False
    for missing in ("review_system_id", "conventions_id", "verifier_id"):
        assert _bindings(**{missing: None}).is_complete is False
    # Checksums alone do not pin a prompt.
    assert PromptBindings("4" * 64, "5" * 64, "6" * 64).is_complete is False


def test_review_prompt_binding_is_checked_against_the_prompt_pinned_by_the_run() -> None:
    bindings = _bindings()

    assert bindings.pins_review_prompt(UUID(int=21)) is True
    assert bindings.pins_review_prompt(UUID(int=22)) is False
    assert PromptBindings("4" * 64, "5" * 64, "6" * 64).pins_review_prompt(UUID(int=21)) is False


def test_execution_profile_holds_every_pinned_setting_of_the_specification() -> None:
    profile = _profile()

    assert profile.is_complete is True
    assert (
        profile.fallback_review_model,
        profile.fallback_verifier_model,
        profile.fallback_conventions_model,
    ) == ("model-d", "model-e", "model-f")
    assert (profile.primary_provider_route, profile.fallback_provider_route) == (
        "route-primary",
        "route-fallback",
    )
    assert (profile.tokenizer_version, profile.counter_version) == ("tokenizer/v1", "counter/v1")
    assert (profile.temperature, profile.response_schema_version) == ("0.2", "review-output/v2")
    assert (profile.recovery_profile_version, profile.budget_profile_version) == (
        "recovery/v1",
        "budget/v1",
    )
    # A stage may have no fallback model; every other setting is required.
    assert _profile(fallback_review_model=None, fallback_provider_route=None).is_complete is True
    for missing in (
        "primary_provider_route",
        "tokenizer_version",
        "counter_version",
        "temperature",
        "response_schema_version",
        "recovery_profile_version",
        "budget_profile_version",
    ):
        assert _profile(**{missing: None}).is_complete is False, missing
    assert ExecutionProfile.fast("model-a", "model-b").is_complete is False


def test_temperature_is_a_decimal_string() -> None:
    assert _profile(temperature="0").temperature == "0"
    with pytest.raises(ValueError, match="decimal string"):
        _profile(temperature="warm")


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
        # Immutable scalars only: nothing inside can be mutated through a shared reference.
        assert all(
            isinstance(getattr(pinned, item.name), str | int | UUID | None)
            for item in fields(pinned)
        )
    with pytest.raises(FrozenInstanceError):
        snapshot.prompt_bindings.verifier_checksum = "0" * 64  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        snapshot.execution_profile.max_policy_tokens = 1  # type: ignore[misc]


def test_complete_pinned_configuration_fits_the_snapshot_and_stays_out_of_its_time() -> None:
    snapshot = replace(
        _snapshot(_empty(RULES_DOCUMENT_PATH), _missing(AGENTS_DOCUMENT_PATH)),
        prompt_bindings=_bindings(),
        execution_profile=_profile(),
    )

    assert snapshot.prompt_bindings.is_complete and snapshot.execution_profile.is_complete
    assert snapshot.created_at is None
    with pytest.raises(FrozenInstanceError):
        snapshot.execution_profile.tokenizer_version = "other"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        snapshot.prompt_bindings.review_system_id = UUID(int=99)  # type: ignore[misc]


# --- Tests for Issue #120 extensions (PromptBindings, ExecutionProfile, CallManifest) ---


def test_policy_document_properties_and_helpers() -> None:
    present = make_policy_document_present(".review/rules.md", "# My Rule\n")
    assert present.is_present is True
    assert present.is_missing is False
    assert present.line_count == 1
    assert present.byte_count == len(b"# My Rule\n")

    missing = make_policy_document_missing("AGENTS.md")
    assert missing.is_present is False
    assert missing.is_missing is True
    assert missing.text is None

    empty = make_policy_document_empty("AGENTS.md")
    assert empty.is_present is True
    assert empty.is_missing is False
    assert empty.text == ""


def test_policy_snapshot_composition_and_readiness() -> None:
    rules_doc = make_policy_document_present(".review/rules.md", "# Rules\nRule 1\n")
    agents_doc = make_policy_document_missing("AGENTS.md")
    bindings = _bindings()
    profile = _profile()

    snapshot = PolicySnapshot(
        schema_version=1,
        snapshot_id=UUID("00000000-0000-0000-0000-000000000001"),
        run_id=UUID("00000000-0000-0000-0000-000000000002"),
        repository_id=UUID("00000000-0000-0000-0000-000000000003"),
        policy_base_sha="base_sha_123",
        rules=rules_doc,
        agents=agents_doc,
        ignore_patterns=("node_modules/**", "dist/**"),
        ignore_digest="ignore_digest_123",
        parser_profile="rules-markdown/v1",
        parser_version="4.0.0",
        matcher_version="review-ignore/v1",
        limits_version="policy-limits/v1",
        builtin_exclusions_version="builtin-exclusions/v1",
        priority_matcher_version="priority-matcher/v1",
        service_defaults_rule_version_id=UUID("00000000-0000-0000-0000-000000000004"),
        defaults_checksum="defaults_123",
        prompt_bindings=bindings,
        execution_profile=profile,
        policy_digest="digest_abc",
    )

    assert snapshot.is_ready_for_llm is True
    assert snapshot.has_custom_rules is True
    assert snapshot.has_agents_doc is False

    unready_bindings = replace(snapshot, prompt_bindings=_bindings(verifier_id=None))
    assert unready_bindings.is_ready_for_llm is False
    unready_profile = replace(snapshot, execution_profile=_profile(review_model=""))
    assert unready_profile.is_ready_for_llm is False


def test_call_manifest_tracks_shown_ranges() -> None:
    call_id = UUID("00000000-0000-0000-0000-000000000010")
    run_id = UUID("00000000-0000-0000-0000-000000000020")
    block1 = ManifestBlock(
        block_id="code-1",
        source_role="diff",
        source_kind="diff",
        path="app/service.py",
        start_line=10,
        end_line=25,
        shown_line_count=16,
    )
    block2 = ManifestBlock(
        block_id="policy-rules",
        source_role="policy_document",
        source_kind="repository_rules",
        path=".review/rules.md",
        start_line=1,
        end_line=20,
        shown_line_count=20,
    )

    manifest = CallManifest(
        call_id=call_id,
        run_id=run_id,
        stage="review",
        blocks=(block1, block2),
    )

    assert manifest.get_block("code-1") == block1
    assert manifest.get_block("unknown") is None
    assert manifest.is_range_shown("code-1", start_line=10, end_line=15) is True
    assert manifest.is_range_shown("code-1", start_line=5, end_line=15) is False
    assert manifest.is_range_shown("code-1", start_line=20, end_line=30) is False
    assert manifest.is_range_shown("nonexistent", start_line=1, end_line=2) is False
