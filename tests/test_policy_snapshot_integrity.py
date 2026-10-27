"""A stored policy snapshot is re-checked from its own values before a run uses it (#168)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from hashlib import sha256
from typing import Any
from uuid import UUID

import pytest

from app.modules.reviews.application import prepare_policy_snapshot, review_ignore
from app.modules.reviews.application.policy_documents import missing_document, normalize_document
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.policy_snapshot import (
    AGENTS_DOCUMENT_PATH,
    RULES_DOCUMENT_PATH,
    DocumentPresence,
    ExecutionProfile,
    PolicyDocument,
    PolicySnapshot,
    PromptBindings,
)
from app.modules.reviews.application.policy_snapshot_integrity import (
    verify_policy_document,
    verify_policy_snapshot,
    verify_snapshot_pins,
)
from app.modules.reviews.application.prepare_policy_snapshot import (
    RunPolicyPins,
    build_policy_snapshot,
    canonical_policy_payload,
    pinned_to_json,
)
from app.modules.reviews.application.review_ignore import (
    PARSER_VERSION,
    IgnorePatternLine,
    ignore_digest,
    parse_review_ignore,
)

_RAW = "# Правила\r\n\r\n```review-ignore\r\ngenerated/**\r\n```\r\n".encode()
_PINS = RunPolicyPins(
    run_id=UUID(int=2),
    repository_id=UUID(int=1),
    policy_base_sha="b" * 40,
    service_defaults_rule_version_id=UUID(int=4),
    defaults_checksum="2" * 64,
    prompt_bindings=PromptBindings("3" * 64, "", "", review_system_id=UUID(int=31)),
    execution_profile=ExecutionProfile.fast("model-a", "", "model-c"),
)


def _rules() -> PolicyDocument:
    return normalize_document(RULES_DOCUMENT_PATH, _RAW, blob_oid="3" * 40)


def _snapshot() -> PolicySnapshot:
    rules = _rules()
    return build_policy_snapshot(
        _PINS,
        rules=rules,
        agents=missing_document(AGENTS_DOCUMENT_PATH),
        ignore=parse_review_ignore(rules.text),
        snapshot_id=UUID(int=11),
    )


def _verify(
    snapshot: PolicySnapshot,
    *,
    bindings: dict[str, Any] | None = None,
    profile: dict[str, Any] | None = None,
) -> None:
    """Verify as the store does: with the JSON of the pinned versions as it is stored."""
    verify_policy_snapshot(
        snapshot,
        prompt_bindings=pinned_to_json(snapshot.prompt_bindings) if bindings is None else bindings,
        execution_profile=(
            pinned_to_json(snapshot.execution_profile) if profile is None else profile
        ),
    )


def _assert_integrity_error(error: PolicyError, checksum: str) -> None:
    assert error.error_code == PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR.value
    assert checksum in error.message
    # The message names the checksum; it never quotes the content of a document.
    assert "generated" not in error.message
    assert "Правила" not in error.message


def test_an_untouched_snapshot_and_its_documents_pass() -> None:
    snapshot = _snapshot()

    # CRLF in the raw bytes: raw_sha256 is over the bytes, text_sha256 over the LF text.
    assert snapshot.rules.raw_sha256 != snapshot.rules.text_sha256
    verify_policy_document(snapshot.rules, _RAW)
    verify_policy_document(snapshot.agents, None)
    _verify(snapshot)


def test_a_missing_document_that_stores_bytes_fails() -> None:
    with pytest.raises(PolicyError) as caught:
        verify_policy_document(missing_document(AGENTS_DOCUMENT_PATH), b"")

    _assert_integrity_error(caught.value, "missing")


@pytest.mark.parametrize(
    ("document", "raw", "checksum"),
    [
        (_rules(), _RAW + b"x", "raw_sha256"),
        (_rules(), None, "raw_sha256"),
        (normalize_document(RULES_DOCUMENT_PATH, b"", blob_oid="3" * 40), None, "raw_sha256"),
        (replace(_rules(), raw_sha256="0" * 64), _RAW, "raw_sha256"),
        (replace(_rules(), byte_count=len(_RAW) + 1), _RAW, "byte_count"),
        (replace(_rules(), text="# Правила\n\nmore\n"), _RAW, "text_sha256"),
        (replace(_rules(), text_sha256="0" * 64), _RAW, "text_sha256"),
        (replace(_rules(), line_count=99), _RAW, "line_count"),
    ],
    ids=[
        "raw-bytes",
        "raw-lost",
        "empty-raw-lost",
        "raw-sha256",
        "byte-count",
        "text",
        "text-sha256",
        "line-count",
    ],
)
def test_a_tampered_document_fails_naming_its_checksum(
    document: PolicyDocument, raw: bytes | None, checksum: str
) -> None:
    with pytest.raises(PolicyError) as caught:
        verify_policy_document(document, raw)

    _assert_integrity_error(caught.value, checksum)
    assert caught.value.source_path == RULES_DOCUMENT_PATH


_TAMPERS: list[tuple[str, Callable[[PolicySnapshot], PolicySnapshot], str]] = [
    ("ignore-patterns", lambda item: replace(item, ignore_patterns=()), "ignore_digest"),
    ("ignore-digest", lambda item: replace(item, ignore_digest="0" * 64), "ignore_digest"),
    ("matcher-version", lambda item: replace(item, matcher_version="m/v2"), "ignore_digest"),
    (
        "builtin-exclusions-version",
        lambda item: replace(item, builtin_exclusions_version="b/v2"),
        "ignore_digest",
    ),
    ("limits-version", lambda item: replace(item, limits_version="l/v2"), "ignore_digest"),
    ("policy-base-sha", lambda item: replace(item, policy_base_sha="c" * 40), "policy_digest"),
    ("defaults-checksum", lambda item: replace(item, defaults_checksum="9" * 64), "policy_digest"),
    ("schema-version", lambda item: replace(item, schema_version=2), "policy_digest"),
    ("parser-profile", lambda item: replace(item, parser_profile="p/v2"), "policy_digest"),
    ("parser-version", lambda item: replace(item, parser_version="0.0.0"), "policy_digest"),
    (
        "priority-matcher-version",
        lambda item: replace(item, priority_matcher_version="pm/v2"),
        "policy_digest",
    ),
    (
        "agents-presence",
        lambda item: replace(item, agents=replace(_rules(), path=AGENTS_DOCUMENT_PATH)),
        "policy_digest",
    ),
    ("policy-digest", lambda item: replace(item, policy_digest="0" * 64), "policy_digest"),
]


@pytest.mark.parametrize(
    ("change", "checksum"),
    [(change, checksum) for _, change, checksum in _TAMPERS],
    ids=[name for name, _, _ in _TAMPERS],
)
def test_a_tampered_snapshot_field_fails_naming_its_checksum(
    change: Callable[[PolicySnapshot], PolicySnapshot], checksum: str
) -> None:
    with pytest.raises(PolicyError) as caught:
        _verify(change(_snapshot()))

    _assert_integrity_error(caught.value, checksum)


@pytest.mark.parametrize(
    ("bindings", "profile"),
    [
        ({"review_system_checksum": "4" * 64}, {}),
        ({"review_system_id": str(UUID(int=32))}, {}),
        ({}, {"review_model": "model-b"}),
    ],
    ids=["binding-checksum", "binding-id", "profile-model"],
)
def test_tampered_stored_pins_json_fails(bindings: dict[str, Any], profile: dict[str, Any]) -> None:
    snapshot = _snapshot()

    with pytest.raises(PolicyError) as caught:
        _verify(
            snapshot,
            bindings=pinned_to_json(snapshot.prompt_bindings) | bindings,
            profile=pinned_to_json(snapshot.execution_profile) | profile,
        )

    _assert_integrity_error(caught.value, "policy_digest")


@pytest.mark.parametrize("column", ["prompt_bindings", "execution_profile"])
def test_a_key_a_later_version_stored_in_the_pins_json_passes_when_the_digest_has_it(
    column: str,
) -> None:
    snapshot = _snapshot()
    typed = {
        "prompt_bindings": pinned_to_json(snapshot.prompt_bindings),
        "execution_profile": pinned_to_json(snapshot.execution_profile),
    }
    stored_json = typed | {column: typed[column] | {"future_knob": "on"}}
    bindings, profile = stored_json["prompt_bindings"], stored_json["execution_profile"]
    canonical = canonical_policy_payload(
        schema_version=snapshot.schema_version,
        policy_base_sha=snapshot.policy_base_sha,
        documents=(snapshot.rules, snapshot.agents),
        ignore_digest=snapshot.ignore_digest,
        parser_profile=snapshot.parser_profile,
        parser_version=snapshot.parser_version,
        limits_version=snapshot.limits_version,
        defaults_checksum=snapshot.defaults_checksum,
        prompt_bindings=bindings,
        execution_profile=profile,
        priority_matcher_version=snapshot.priority_matcher_version,
    )
    stored = replace(snapshot, policy_digest=sha256(canonical.encode("utf-8")).hexdigest())

    _verify(stored, bindings=bindings, profile=profile)
    # The typed values drop the key: verifying them instead of the stored JSON fails.
    with pytest.raises(PolicyError):
        _verify(stored, bindings=typed["prompt_bindings"], profile=typed["execution_profile"])


def test_a_snapshot_stored_by_another_parser_version_still_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(prepare_policy_snapshot, "PARSER_VERSION", "0.0.1")
    stored = _snapshot()
    monkeypatch.undo()

    assert stored.parser_version == "0.0.1"
    assert stored.parser_version != PARSER_VERSION
    _verify(stored)


def test_the_policy_digest_formula_is_pinned() -> None:
    present = PolicyDocument(
        RULES_DOCUMENT_PATH, DocumentPresence.PRESENT, "a" * 40, "1" * 64, "2" * 64, "x", 1, 1
    )
    golden = (
        f'{{"defaults_checksum":"{"5" * 64}",'
        '"documents":['
        f'{{"path":".review/rules.md","presence":"present","raw_sha256":"{"1" * 64}",'
        f'"text_sha256":"{"2" * 64}"}},'
        '{"path":"AGENTS.md","presence":"missing","raw_sha256":null,"text_sha256":null}],'
        '"exact_parser_version":"4.0.0",'
        '"execution_profile":{"max_input_tokens":60000,"profile_name":"fast","temperature":null},'
        f'"ignore_digest":"{"3" * 64}",'
        '"limits_version":"policy-limits/v1",'
        '"parser_profile":"rules-markdown/v1",'
        f'"policy_base_sha":"{"b" * 40}",'
        '"priority_matcher_version":"priority-matcher/v1",'
        f'"prompt_bindings":{{"review_system_checksum":"{"6" * 64}",'
        '"review_system_id":"00000000-0000-0000-0000-00000000001f"},'
        '"schema_version":1}'
    )

    canonical = canonical_policy_payload(
        schema_version=1,
        policy_base_sha="b" * 40,
        documents=(present, missing_document(AGENTS_DOCUMENT_PATH)),
        ignore_digest="3" * 64,
        parser_profile="rules-markdown/v1",
        parser_version="4.0.0",
        limits_version="policy-limits/v1",
        defaults_checksum="5" * 64,
        prompt_bindings={
            "review_system_id": "00000000-0000-0000-0000-00000000001f",
            "review_system_checksum": "6" * 64,
        },
        execution_profile={"profile_name": "fast", "temperature": None, "max_input_tokens": 60000},
        priority_matcher_version="priority-matcher/v1",
    )

    assert canonical == golden


def test_the_built_snapshot_digest_is_the_sha256_of_its_canonical_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The parser version is the installed markdown-it version: pinned, so a dependency
    # bump does not move the golden.
    monkeypatch.setattr(prepare_policy_snapshot, "PARSER_VERSION", "4.0.0")
    golden = (
        f'{{"defaults_checksum":"{"2" * 64}",'
        '"documents":['
        '{"path":".review/rules.md","presence":"present",'
        '"raw_sha256":"1ac66841a681c698a3717859efd94bc557b097c91c76720ace1376f39f4be226",'
        '"text_sha256":"3e88e52aa850f130e9d8a5154f8822d1cff04b26239956ae5cbc0e94522858e9"},'
        '{"path":"AGENTS.md","presence":"missing","raw_sha256":null,"text_sha256":null}],'
        '"exact_parser_version":"4.0.0",'
        '"execution_profile":{"budget_profile_version":null,"conventions_model":"model-c",'
        '"counter_version":null,"fallback_conventions_model":null,'
        '"fallback_provider_route":null,"fallback_review_model":null,'
        '"fallback_verifier_model":null,"max_calls_per_run":4,"max_input_tokens":60000,'
        '"max_policy_tokens":12000,"primary_provider_route":null,"profile_name":"fast",'
        '"recovery_profile_version":null,"response_schema_version":null,'
        '"review_model":"model-a","temperature":null,"tokenizer_version":null,'
        '"verifier_model":""},'
        '"ignore_digest":"24ab4f16f70b7124f9af6fd8e6000572622dc1f868f1980616b2519fd03c9821",'
        '"limits_version":"policy-limits/v1",'
        '"parser_profile":"rules-markdown/v1",'
        f'"policy_base_sha":"{"b" * 40}",'
        '"priority_matcher_version":"priority-matcher/v1",'
        '"prompt_bindings":{"conventions_checksum":"","conventions_id":null,'
        '"conventions_key":"review.conventions","conventions_version":"v2",'
        f'"review_system_checksum":"{"3" * 64}",'
        '"review_system_id":"00000000-0000-0000-0000-00000000001f",'
        '"review_system_key":"review.system","review_system_version":"v2",'
        '"verifier_checksum":"","verifier_id":null,"verifier_key":"review.verifier",'
        '"verifier_version":"v2"},'
        '"schema_version":1}'
    )

    assert _snapshot().policy_digest == sha256(golden.encode("utf-8")).hexdigest()


def test_the_ignore_digest_formula_is_pinned() -> None:
    golden = (
        '{"builtin_exclusions_version":"builtin-exclusions/v1",'
        '"limits_version":"policy-limits/v1",'
        '"matcher_version":"review-ignore/v1",'
        '"policy_document_exemption":"base-policy-role-only/v1",'
        '"rename_copy_propagation":"both-paths/v1",'
        '"sorted_unique_patterns":["generated/**"],'
        '"version":1}'
    )
    expected = sha256(golden.encode("utf-8")).hexdigest()

    assert (
        ignore_digest(
            ("generated/**",),
            matcher_version="review-ignore/v1",
            builtin_exclusions_version="builtin-exclusions/v1",
            limits_version="policy-limits/v1",
        )
        == expected
    )
    assert parse_review_ignore("```review-ignore\ngenerated/**\n```\n").digest == expected


def test_pins_equal_to_the_snapshot_pass_whatever_the_current_execution_profile() -> None:
    snapshot = _snapshot()

    verify_snapshot_pins(snapshot, _PINS)
    # The profile of the pins is the worker's current one; the snapshot fixed its own.
    verify_snapshot_pins(
        snapshot, replace(_PINS, execution_profile=ExecutionProfile.deep("model-z", "model-y"))
    )


def test_a_pinned_version_row_that_is_gone_fails() -> None:
    with pytest.raises(PolicyError) as caught:
        verify_snapshot_pins(_snapshot(), None)

    _assert_integrity_error(caught.value, "pinned version")


_PIN_CHANGES: list[tuple[str, RunPolicyPins, str]] = [
    ("repository-id", replace(_PINS, repository_id=UUID(int=9)), "repository_id"),
    ("policy-base-sha", replace(_PINS, policy_base_sha="d" * 40), "policy_base_sha"),
    (
        "rule-version-id",
        replace(_PINS, service_defaults_rule_version_id=UUID(int=5)),
        "service_defaults_rule_version_id",
    ),
    ("defaults-checksum", replace(_PINS, defaults_checksum="9" * 64), "defaults_checksum"),
    (
        "review-checksum",
        replace(
            _PINS, prompt_bindings=replace(_PINS.prompt_bindings, review_system_checksum="4" * 64)
        ),
        "prompt_bindings",
    ),
    (
        "review-id",
        replace(
            _PINS, prompt_bindings=replace(_PINS.prompt_bindings, review_system_id=UUID(int=32))
        ),
        "prompt_bindings",
    ),
    (
        "review-version",
        replace(_PINS, prompt_bindings=replace(_PINS.prompt_bindings, review_system_version="v3")),
        "prompt_bindings",
    ),
    (
        "conventions-pinned-later",
        replace(
            _PINS,
            prompt_bindings=replace(
                _PINS.prompt_bindings, conventions_checksum="5" * 64, conventions_id=UUID(int=33)
            ),
        ),
        "prompt_bindings",
    ),
]


@pytest.mark.parametrize(
    ("pins", "field"),
    [(pins, field) for _, pins, field in _PIN_CHANGES],
    ids=[name for name, _, _ in _PIN_CHANGES],
)
def test_a_snapshot_whose_pins_differ_from_the_run_versions_fails(
    pins: RunPolicyPins, field: str
) -> None:
    with pytest.raises(PolicyError) as caught:
        verify_snapshot_pins(_snapshot(), pins)

    _assert_integrity_error(caught.value, field)


# Masks on lines 4 and 6 of the document, sorted the other way round in the snapshot.
_TWO_MASKS = "# Правила\n\n```review-ignore\nvendor/**\n\nbuild/**\n```\n".encode()


def _snapshot_of(rules: PolicyDocument) -> PolicySnapshot:
    return build_policy_snapshot(
        _PINS,
        rules=rules,
        agents=missing_document(AGENTS_DOCUMENT_PATH),
        ignore=parse_review_ignore(rules.text),
        snapshot_id=UUID(int=11),
    )


def _two_masks() -> PolicySnapshot:
    return _snapshot_of(normalize_document(RULES_DOCUMENT_PATH, _TWO_MASKS, blob_oid="3" * 40))


def test_the_ignore_policy_of_a_snapshot_gives_back_the_source_lines_of_its_masks() -> None:
    snapshot = _two_masks()

    policy = review_ignore.snapshot_ignore_policy(snapshot)

    # RFS §7.1: the lines are not a stored field; the staged rules text gives them back.
    assert policy.source_lines == (
        IgnorePatternLine("vendor/**", 4),
        IgnorePatternLine("build/**", 6),
    )
    assert policy.patterns == ("build/**", "vendor/**")
    assert policy.digest == snapshot.ignore_digest


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            lambda item: replace(item, ignore_patterns=("vendor/**",)),
            "The stored ignore_patterns do not match the masks of the stored rules text",
        ),
        (
            lambda item: replace(item, ignore_digest="0" * 64),
            "The stored ignore_digest does not match the masks of the stored rules text",
        ),
    ],
    ids=["ignore-patterns", "ignore-digest"],
)
def test_a_snapshot_whose_masks_disagree_with_its_rules_text_fails(
    change: Callable[[PolicySnapshot], PolicySnapshot], message: str
) -> None:
    with pytest.raises(PolicyError) as caught:
        review_ignore.snapshot_ignore_policy(change(_two_masks()))

    assert caught.value.diagnostic() == {
        "code": "policy_snapshot_integrity_error",
        "source_path": None,
        "policy_base_sha": "b" * 40,
        "message": message,
        "line": None,
        "column": None,
        "pattern_index": None,
        "limit": None,
        "actual": None,
        "retryable": False,
    }


def test_a_snapshot_stored_under_another_matcher_version_keeps_its_ignore_policy() -> None:
    current = _two_masks()
    # The digest of the same masks under the versions this snapshot pinned, not the current ones.
    stored = replace(
        current,
        matcher_version="review-ignore/v0",
        ignore_digest=ignore_digest(
            current.ignore_patterns,
            matcher_version="review-ignore/v0",
            builtin_exclusions_version=current.builtin_exclusions_version,
            limits_version=current.limits_version,
        ),
    )

    policy = review_ignore.snapshot_ignore_policy(stored)

    assert policy.source_lines == (
        IgnorePatternLine("vendor/**", 4),
        IgnorePatternLine("build/**", 6),
    )
    assert policy.patterns == ("build/**", "vendor/**")
    assert policy.digest == "ecf9380e9c4bee07509465c825727eadaf6143ecbaac76c4cec4e84fa6976a6a"


def test_a_snapshot_without_rules_has_the_empty_ignore_policy() -> None:
    snapshot = _snapshot_of(missing_document(RULES_DOCUMENT_PATH))

    policy = review_ignore.snapshot_ignore_policy(snapshot)

    assert (policy.patterns, policy.source_lines) == ((), ())
    assert policy.digest == snapshot.ignore_digest


def test_an_invalid_mask_of_the_stored_rules_text_fails_with_its_own_error() -> None:
    broken = normalize_document(
        RULES_DOCUMENT_PATH, b"```review-ignore\nsrc/a?.py\n```\n", blob_oid="3" * 40
    )
    snapshot = replace(_two_masks(), rules=broken)

    with pytest.raises(PolicyError) as caught:
        review_ignore.snapshot_ignore_policy(snapshot)

    # The parser's error at the pinned base, not an integrity error of the snapshot.
    error = caught.value
    assert error.code is PolicyErrorCode.IGNORE_PATTERN_INVALID
    assert (error.source_path, error.policy_base_sha, error.line, error.column) == (
        RULES_DOCUMENT_PATH,
        "b" * 40,
        2,
        6,
    )
