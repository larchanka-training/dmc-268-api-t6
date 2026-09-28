"""Persistent RabbitMQ review.run/v1 publisher with broker confirms."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

import aio_pika
from aio_pika.abc import AbstractExchange
from pydantic import BaseModel, Field, field_serializer

from app.modules.reviews.application.try_enqueue_webhook_run import (
    PendingRunMessage,
    RunPublicationKind,
)

_ROUTES = {"fast": "review.run.fast", "deep": "review.run.deep"}
_PRIORITIES = {RunPublicationKind.QUEUED: 0, RunPublicationKind.CANCELLATION: 9}


class _RepositoryPayload(BaseModel):
    id: UUID
    provider: Literal["github"] = "github"
    external_id: int = Field(gt=0)
    full_name: str


class _PullRequestPayload(BaseModel):
    number: int = Field(gt=0)
    head_sha: str
    base_sha: str
    base_ref: str


class _ReviewRunV1Payload(BaseModel):
    schema_name: Literal["review.run/v1"] = Field(default="review.run/v1", alias="schema")
    message_id: UUID
    run_id: UUID
    workspace_id: UUID
    installation_id: int = Field(gt=0)
    repo: _RepositoryPayload
    pr: _PullRequestPayload
    engine: str
    rule_version_id: UUID
    prompt_version_id: UUID
    trigger: Literal["webhook"] = "webhook"
    attempt: int = Field(gt=0)
    requested_at: datetime

    @field_serializer("requested_at")
    def serialize_requested_at(self, value: datetime) -> str:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _encode_review_run_v1(message: PendingRunMessage) -> bytes:
    return (
        _ReviewRunV1Payload(
            message_id=message.run_id,
            run_id=message.run_id,
            workspace_id=message.workspace_id,
            installation_id=message.installation_id,
            repo=_RepositoryPayload(
                id=message.repository_id,
                external_id=message.repository_external_id,
                full_name=message.repository_full_name,
            ),
            pr=_PullRequestPayload(
                number=message.pr_number,
                head_sha=message.head_sha,
                base_sha=message.base_sha,
                base_ref=message.base_ref,
            ),
            engine=message.engine,
            rule_version_id=message.rule_version_id,
            prompt_version_id=message.prompt_version_id,
            attempt=message.attempt,
            requested_at=message.requested_at,
        )
        .model_dump_json(by_alias=True)
        .encode("utf-8")
    )


class RabbitMqRunPublisher:
    def __init__(self, exchange: AbstractExchange) -> None:
        self._exchange = exchange

    async def publish_confirmed(
        self, message: PendingRunMessage, *, kind: RunPublicationKind = RunPublicationKind.QUEUED
    ) -> None:
        try:
            routing_key = _ROUTES[message.engine]
        except KeyError as exc:
            raise ValueError("unsupported review.run engine") from exc
        try:
            priority = _PRIORITIES[kind]
        except KeyError as exc:
            raise ValueError("unsupported run publication kind") from exc
        body = _encode_review_run_v1(message)
        confirmation = await self._exchange.publish(
            aio_pika.Message(
                body,
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                message_id=str(message.run_id),
                type="review.run/v1",
                priority=priority,
            ),
            routing_key=routing_key,
            mandatory=True,
            timeout=10.0,
        )
        if not confirmation:
            raise RuntimeError("review.run/v1 publish lacked broker confirmation")


@asynccontextmanager
async def open_rabbitmq_run_publisher(url: str) -> AsyncIterator[RabbitMqRunPublisher]:
    """Declare durable topology and return a publisher that requires broker confirms."""
    if not url:
        raise ValueError("RABBITMQ_URL is required")
    connection = await aio_pika.connect_robust(url, timeout=10.0)
    async with connection:
        channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
        dead_letters = await channel.declare_exchange(
            "reviews.dlx", aio_pika.ExchangeType.FANOUT, durable=True
        )
        dead_letter_queue = await channel.declare_queue("reviews.dlq", durable=True)
        await dead_letter_queue.bind(dead_letters)
        exchange = await channel.declare_exchange(
            "reviews", aio_pika.ExchangeType.DIRECT, durable=True
        )
        for routing_key in _ROUTES.values():
            queue = await channel.declare_queue(
                routing_key,
                durable=True,
                arguments={"x-max-priority": 10, "x-dead-letter-exchange": "reviews.dlx"},
            )
            await queue.bind(exchange, routing_key=routing_key)
        yield RabbitMqRunPublisher(exchange)
