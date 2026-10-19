"""Public queue-identity admission, independent of AMQP and SQLAlchemy."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest

from app.modules.reviews.application.handle_review_run import DeliveryOutcome
from app.modules.reviews.application.run_delivery import (
    HandleRunDelivery,
    PersistedRunIdentity,
    RunDeliveryPointer,
    RunDeliveryUnavailable,
)

RUN = UUID("00000000-0000-0000-0000-000000000001")
WORKSPACE = UUID("00000000-0000-0000-0000-000000000002")
REPOSITORY = UUID("00000000-0000-0000-0000-000000000003")
ORIGIN = UUID("00000000-0000-0000-0000-000000000004")
RULE = UUID("00000000-0000-0000-0000-000000000005")
PROMPT = UUID("00000000-0000-0000-0000-000000000006")


def pointer() -> RunDeliveryPointer:
    return RunDeliveryPointer(
        RUN,
        "gitlab",
        WORKSPACE,
        REPOSITORY,
        17,
        71,
        12,
        "a" * 40,
        "b" * 40,
        "fast",
        RULE,
        PROMPT,
        "rerun",
    )


class Reader:
    def __init__(self, identity: PersistedRunIdentity | None) -> None:
        self.identity = identity

    async def read(self, run_id: UUID) -> PersistedRunIdentity | None:
        assert run_id == RUN
        return self.identity


class Handler:
    def __init__(self) -> None:
        self.runs: list[UUID] = []

    async def __call__(self, run_id: UUID) -> DeliveryOutcome:
        self.runs.append(run_id)
        return DeliveryOutcome.ACK


def test_mismatched_workspace_is_dead_lettered_before_uuid_handler() -> None:
    incoming = replace(pointer(), workspace_id=UUID("00000000-0000-0000-0000-000000000099"))
    handler = Handler()
    outcome = asyncio.run(
        HandleRunDelivery(Reader(PersistedRunIdentity(pointer(), ORIGIN)), handler).execute(
            incoming
        )
    )
    assert outcome is DeliveryOutcome.DEAD_LETTER
    assert handler.runs == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider", "github"),
        ("workspace_id", UUID("00000000-0000-0000-0000-000000000012")),
        ("repository_id", UUID("00000000-0000-0000-0000-000000000013")),
        ("repository_external_id", 18),
        ("installation_id", 72),
        ("pr_number", 13),
        ("head_sha", "c" * 40),
        ("base_sha", "d" * 40),
        ("engine", "deep"),
        ("rule_version_id", UUID("00000000-0000-0000-0000-000000000015")),
        ("prompt_version_id", UUID("00000000-0000-0000-0000-000000000016")),
        ("trigger", "manual"),
    ],
)
def test_each_forged_immutable_claim_is_permanently_refused(field: str, value: Any) -> None:
    handler = Handler()
    incoming = replace(pointer(), **{field: value})
    outcome = asyncio.run(
        HandleRunDelivery(Reader(PersistedRunIdentity(pointer(), ORIGIN)), handler).execute(
            incoming
        )
    )
    assert outcome is DeliveryOutcome.DEAD_LETTER
    assert handler.runs == []


@pytest.mark.parametrize("provider", ["github", "gitlab"])
@pytest.mark.parametrize("outcome", [DeliveryOutcome.ACK, DeliveryOutcome.DEAD_LETTER])
def test_exact_supported_identity_returns_actual_handler_outcome(
    provider: str,
    outcome: DeliveryOutcome,
) -> None:
    calls: list[UUID] = []

    async def handler(run_id: UUID) -> DeliveryOutcome:
        calls.append(run_id)
        return outcome

    incoming = replace(pointer(), provider=provider)
    assert (
        asyncio.run(
            HandleRunDelivery(
                Reader(PersistedRunIdentity(incoming, ORIGIN)),
                handler,
            ).execute(incoming)
        )
        is outcome
    )
    assert calls == [RUN]


def test_absent_or_retired_identity_is_acknowledged_without_handler() -> None:
    handler = Handler()
    assert (
        asyncio.run(HandleRunDelivery(Reader(None), handler).execute(pointer()))
        is DeliveryOutcome.ACK
    )
    assert handler.runs == []


def test_even_equal_unsupported_provider_claims_never_invoke_handler() -> None:
    handler = Handler()
    incoming = replace(pointer(), provider="unknown")
    assert (
        asyncio.run(
            HandleRunDelivery(
                Reader(PersistedRunIdentity(incoming, ORIGIN)),
                handler,
            ).execute(incoming)
        )
        is DeliveryOutcome.DEAD_LETTER
    )
    assert handler.runs == []


@pytest.mark.parametrize("cancelled", [False, True])
def test_reader_failure_is_distinct_from_missing_and_preserves_cancellation(
    cancelled: bool,
) -> None:
    failure = asyncio.CancelledError() if cancelled else RunDeliveryUnavailable()

    class FailedReader:
        async def read(self, run_id: UUID) -> PersistedRunIdentity | None:
            raise failure

    handler = Handler()
    with pytest.raises(type(failure)) as caught:
        asyncio.run(HandleRunDelivery(FailedReader(), handler).execute(pointer()))
    assert caught.value is failure
    assert handler.runs == []


def test_handler_error_is_not_converted_to_an_admission_outcome() -> None:
    failure = ValueError("handler failure")

    async def handler(run_id: UUID) -> DeliveryOutcome:
        raise failure

    with pytest.raises(ValueError) as caught:
        asyncio.run(
            HandleRunDelivery(
                Reader(PersistedRunIdentity(pointer(), ORIGIN)),
                handler,
            ).execute(pointer())
        )
    assert caught.value is failure
