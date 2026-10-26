"""Application admission races preserve ordering and typed authentication results."""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest

from app.modules.integrations.gitlab.application.accept_webhook import (
    AcceptGitLabWebhook,
    GitLabWebhookAuthenticationFailed,
)
from app.modules.integrations.gitlab.application.authenticate_webhook import GitLabWebhookAuthStatus
from app.modules.integrations.gitlab.application.webhook_receipts import (
    GitLabReceiptError,
    GitLabReceiptOriginInactive,
    prepare_gitlab_receipt,
)
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    InstallationDeliveryDispatchStatus,
)
from app.modules.integrations.webhooks.application.receive_github_delivery import WebhookReceipt

ORIGIN = UUID(int=71)


@pytest.mark.parametrize("status", list(GitLabWebhookAuthStatus))
def test_inactive_admission_reauthenticates_only_after_accept_exit(
    status: GitLabWebhookAuthStatus,
) -> None:
    calls: list[str] = []
    failure = GitLabReceiptOriginInactive()
    delivery = prepare_gitlab_receipt(
        connection_id=ORIGIN, event_name="Push Hook", payload_json="{}", webhook_id="stable"
    )

    class Ports:
        async def accept(self, receipt: WebhookReceipt) -> InstallationDeliveryDispatchStatus:
            calls.append("accept")
            try:
                raise failure
            finally:
                calls.append("exit")

        async def authenticate(
            self, *, connection_id: UUID, secret: str | None
        ) -> GitLabWebhookAuthStatus:
            assert connection_id == ORIGIN
            assert secret == "synthetic-secret"
            assert calls == ["accept", "exit"]
            calls.append("reauthenticate")
            return status

    ports = Ports()
    coordinator = AcceptGitLabWebhook(authenticator=ports, receiver=ports)
    expected = (
        GitLabReceiptOriginInactive
        if status == GitLabWebhookAuthStatus.AUTHENTICATED
        else GitLabWebhookAuthenticationFailed
    )
    with pytest.raises(expected) as caught:
        asyncio.run(coordinator.execute(delivery, secret="synthetic-secret"))
    if isinstance(caught.value, GitLabWebhookAuthenticationFailed):
        assert caught.value.status == status
    else:
        assert caught.value is failure
    assert calls == ["accept", "exit", "reauthenticate"]


@pytest.mark.parametrize(
    "failure", [GitLabReceiptError("inactive_origin"), asyncio.CancelledError()]
)
def test_untyped_error_and_cancellation_do_not_trigger_reauthentication(
    failure: BaseException,
) -> None:
    class Ports:
        async def accept(self, receipt: WebhookReceipt) -> InstallationDeliveryDispatchStatus:
            raise failure

        async def authenticate(
            self, *, connection_id: UUID, secret: str | None
        ) -> GitLabWebhookAuthStatus:
            pytest.fail("unexpected reauthentication")

    ports = Ports()
    delivery = prepare_gitlab_receipt(
        connection_id=ORIGIN, event_name="Push Hook", payload_json="{}", webhook_id="stable"
    )
    with pytest.raises(type(failure)) as caught:
        asyncio.run(
            AcceptGitLabWebhook(authenticator=ports, receiver=ports).execute(delivery, secret=None)
        )
    assert caught.value is failure


@pytest.mark.parametrize(
    "result",
    [
        InstallationDeliveryDispatchStatus.PENDING,
        InstallationDeliveryDispatchStatus.DUPLICATE,
    ],
)
def test_accepted_receipt_returns_original_status_without_reauthentication(
    result: InstallationDeliveryDispatchStatus,
) -> None:
    delivery = prepare_gitlab_receipt(
        connection_id=ORIGIN, event_name="Push Hook", payload_json="{}", webhook_id="stable"
    )

    class Ports:
        async def accept(self, receipt: WebhookReceipt) -> InstallationDeliveryDispatchStatus:
            assert receipt is delivery
            return result

        async def authenticate(
            self, *, connection_id: UUID, secret: str | None
        ) -> GitLabWebhookAuthStatus:
            pytest.fail("accepted receipt must not reauthenticate")

    ports = Ports()
    actual = asyncio.run(
        AcceptGitLabWebhook(authenticator=ports, receiver=ports).execute(delivery, secret=None)
    )
    assert actual is result


def test_reauthentication_preserves_cancellation() -> None:
    cancellation = asyncio.CancelledError()
    delivery = prepare_gitlab_receipt(
        connection_id=ORIGIN, event_name="Push Hook", payload_json="{}", webhook_id="stable"
    )

    class Ports:
        async def accept(self, receipt: WebhookReceipt) -> InstallationDeliveryDispatchStatus:
            raise GitLabReceiptOriginInactive()

        async def authenticate(
            self, *, connection_id: UUID, secret: str | None
        ) -> GitLabWebhookAuthStatus:
            raise cancellation

    ports = Ports()
    with pytest.raises(asyncio.CancelledError) as caught:
        asyncio.run(
            AcceptGitLabWebhook(authenticator=ports, receiver=ports).execute(delivery, secret=None)
        )
    assert caught.value is cancellation
