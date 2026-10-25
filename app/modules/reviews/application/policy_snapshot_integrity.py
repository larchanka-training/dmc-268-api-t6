"""Re-check a stored policy snapshot before a run uses it (RULES_FORMAT_SPEC §7.1, §7.2).

Each checksum is recomputed from what the stored rows hold: the raw bytes and the text
of a document, the masks and versions of the ignore policy and the stored JSON of the
pinned versions. Module constants are never used: the parser version is the installed
``markdown-it``, and a snapshot stored before an upgrade must still verify. A mismatch is
``policy_snapshot_integrity_error``; the message names the checksum, never content.
"""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256

from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.policy_snapshot import PolicyDocument, PolicySnapshot
from app.modules.reviews.application.prepare_policy_snapshot import (
    RunPolicyPins,
    canonical_policy_payload,
)
from app.modules.reviews.application.review_ignore import ignore_digest


def _integrity_error(
    message: str, *, source_path: str | None = None, policy_base_sha: str | None = None
) -> PolicyError:
    return PolicyError(
        PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR,
        message,
        source_path=source_path,
        policy_base_sha=policy_base_sha,
    )


def verify_policy_document(document: PolicyDocument, raw: bytes | None) -> None:
    """Check a staged document against the raw bytes stored with it.

    A missing document only must store no bytes: its other fields are ``None`` by
    ``PolicyDocument`` and by the database CHECK, and ``policy_digest`` covers its
    presence and its null checksums.
    """
    if document.is_missing:
        if raw is not None:
            raise _integrity_error(
                "A missing policy document stores raw bytes", source_path=document.path
            )
        return
    if raw is None or sha256(raw).hexdigest() != document.raw_sha256:
        raise _integrity_error(
            "The stored raw_sha256 does not match the stored raw bytes",
            source_path=document.path,
        )
    if len(raw) != document.byte_count:
        raise _integrity_error(
            "The stored byte_count does not match the stored raw bytes",
            source_path=document.path,
        )
    if (
        document.text is None
        or sha256(document.text.encode("utf-8")).hexdigest() != document.text_sha256
    ):
        raise _integrity_error(
            "The stored text_sha256 does not match the stored text", source_path=document.path
        )


def verify_policy_snapshot(
    snapshot: PolicySnapshot,
    *,
    prompt_bindings: Mapping[str, object],
    execution_profile: Mapping[str, object],
) -> None:
    """Recompute ``ignore_digest`` and ``policy_digest`` from the stored values.

    ``prompt_bindings`` and ``execution_profile`` are the stored JSON, not the typed
    values of the snapshot: reading them drops keys a later version stored, and the
    digest covers those keys. The documents are checked when they are read.
    """
    expected_ignore = ignore_digest(
        snapshot.ignore_patterns,
        matcher_version=snapshot.matcher_version,
        builtin_exclusions_version=snapshot.builtin_exclusions_version,
        limits_version=snapshot.limits_version,
    )
    if expected_ignore != snapshot.ignore_digest:
        raise _integrity_error(
            "The stored ignore_digest does not match the stored masks",
            policy_base_sha=snapshot.policy_base_sha,
        )
    canonical = canonical_policy_payload(
        schema_version=snapshot.schema_version,
        policy_base_sha=snapshot.policy_base_sha,
        documents=(snapshot.rules, snapshot.agents),
        ignore_digest=snapshot.ignore_digest,
        parser_profile=snapshot.parser_profile,
        parser_version=snapshot.parser_version,
        limits_version=snapshot.limits_version,
        defaults_checksum=snapshot.defaults_checksum,
        prompt_bindings=prompt_bindings,
        execution_profile=execution_profile,
        priority_matcher_version=snapshot.priority_matcher_version,
    )
    if sha256(canonical.encode("utf-8")).hexdigest() != snapshot.policy_digest:
        raise _integrity_error(
            "The stored policy_digest does not match the stored snapshot",
            policy_base_sha=snapshot.policy_base_sha,
        )


def verify_snapshot_pins(snapshot: PolicySnapshot, pins: RunPolicyPins | None) -> None:
    """Check the snapshot against the versions the run pins (RULES_FORMAT_SPEC §7.1, §7.2).

    ``pins`` is read from the run and its version rows; ``None`` means a pinned row is
    gone. Compared: the service defaults (id and checksum) and every prompt binding (id,
    checksum, key and version). The execution profile of ``pins`` is the worker's current
    one, so it is never compared: the first stored snapshot fixed it for the run.
    """
    if pins is None:
        raise _integrity_error(
            "A pinned version of the run is missing", policy_base_sha=snapshot.policy_base_sha
        )
    if snapshot.service_defaults_rule_version_id != pins.service_defaults_rule_version_id:
        raise _integrity_error(
            "The stored service_defaults_rule_version_id is not the rule version of the run",
            policy_base_sha=snapshot.policy_base_sha,
        )
    if snapshot.defaults_checksum != pins.defaults_checksum:
        raise _integrity_error(
            "The stored defaults_checksum is not the checksum of the pinned rule version",
            policy_base_sha=snapshot.policy_base_sha,
        )
    if snapshot.prompt_bindings != pins.prompt_bindings:
        raise _integrity_error(
            "The stored prompt_bindings are not the prompt versions of the run",
            policy_base_sha=snapshot.policy_base_sha,
        )
