"""Bitbucket webhook run pointers retain immutable provider authority (#333)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from app.modules.reviews.application.determine_ci_eligibility import WebhookRunIdentity
from app.modules.reviews.application.try_enqueue_webhook_run import PendingRunMessage
from app.modules.reviews.infrastructure.amqp_messages import ReviewRunMessage


def test_bitbucket_run_identity_and_queue_message_keep_provider_and_workspace() -> None:
    origin = WebhookRunIdentity(
        "bitbucket",
        UUID(int=1),
        UUID(int=2),
        UUID(int=3),
        17,
        credential_generation=4,
        binding_generation=5,
        webhook_generation=6,
        repository_uuid=UUID(int=6),
    )
    assert origin.provider == "bitbucket"
    assert origin.repository_uuid == UUID(int=6)
    pending = PendingRunMessage(
        run_id=UUID(int=7),
        workspace_id=UUID(int=2),
        installation_id=11,
        repository_id=UUID(int=3),
        repository_external_id=17,
        repository_full_name="team/repo",
        pr_number=23,
        head_sha="a" * 40,
        base_sha="b" * 40,
        base_ref="main",
        engine="fast",
        rule_version_id=UUID(int=8),
        prompt_version_id=UUID(int=9),
        attempt=1,
        requested_at=datetime(2026, 10, 26, tzinfo=UTC),
        provider="bitbucket",
    )
    wire = ReviewRunMessage.from_pending(pending).wire()
    assert wire["repo"]["provider"] == "bitbucket"
    assert wire["workspace_id"] == str(UUID(int=2))
    assert wire["repo"]["id"] == str(UUID(int=3))
