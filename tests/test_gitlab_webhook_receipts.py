"""Stable GitLab delivery admission is explicit and sanitized."""

from __future__ import annotations

from uuid import UUID

import pytest

from app.modules.integrations.gitlab.application.webhook_receipts import (
    GitLabReceiptError,
    prepare_gitlab_receipt,
)

ORIGIN = UUID("00000000-0000-0000-0000-000000000002")


def test_admission_namespaces_stable_identity_once_and_keeps_trusted_origin() -> None:
    receipt = prepare_gitlab_receipt(
        connection_id=ORIGIN,
        webhook_id="abc",
        event_name="Merge Request Hook",
        payload_json='{"object_kind":"merge_request","project":{"id":17}}',
    )
    assert receipt.delivery_id == (
        "gitlab:00000000-0000-0000-0000-000000000002:"
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )
    assert receipt.provider == "gitlab"
    assert receipt.origin_provider_installation_id == ORIGIN
    assert (
        prepare_gitlab_receipt(
            connection_id=ORIGIN,
            idempotency_key="abc",
            webhook_id="abc",
            event_name=receipt.event_name,
            payload_json=receipt.payload_json,
        )
        == receipt
    )


@pytest.mark.parametrize(
    ("webhook_id", "idempotency_key"),
    [(None, None), ("", None), ("a", "b"), ("a\nsecret", None), ("gitlab:abc", None)],
)
def test_missing_conflicting_or_already_normalized_identity_is_rejected(
    webhook_id: str | None, idempotency_key: str | None
) -> None:
    with pytest.raises(GitLabReceiptError, match="^invalid_delivery_identity$"):
        prepare_gitlab_receipt(
            connection_id=ORIGIN,
            webhook_id=webhook_id,
            idempotency_key=idempotency_key,
            event_name="System Hook",
            payload_json="{}",
        )


@pytest.mark.parametrize("payload", ["[]", "null", '{"project":{"id":true}}', '{"project":{}}'])
def test_admission_rejects_invalid_project_identity_without_echoing_payload(payload: str) -> None:
    with pytest.raises(GitLabReceiptError, match="^invalid_payload$"):
        prepare_gitlab_receipt(
            connection_id=ORIGIN, webhook_id="abc", event_name="Push Hook", payload_json=payload
        )


def test_non_project_unsupported_event_does_not_invent_a_project() -> None:
    receipt = prepare_gitlab_receipt(
        connection_id=ORIGIN,
        idempotency_key="abc",
        event_name="System Hook",
        payload_json='{"event_name":"user_create"}',
    )
    assert receipt.payload_json == '{"event_name":"user_create"}'


@pytest.mark.parametrize(
    "payload",
    [
        '{"number":NaN}',
        '{"number":Infinity}',
        '{"object_attributes":{"action":"' + "x" * 101 + '"}}',
    ],
)
def test_invalid_json_numbers_and_unbounded_stored_action_fail_safely(payload: str) -> None:
    with pytest.raises(GitLabReceiptError, match="^invalid_payload$"):
        prepare_gitlab_receipt(
            connection_id=ORIGIN, webhook_id="abc", event_name="System Hook", payload_json=payload
        )
