"""Explicit checkpoints and fakes of the provider and the verifier model for standalone LLM
tests without persisted Runs."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx

from app.modules.reviews.application.llm import RunCallContext
from app.modules.reviews.application.reflection_pass import VerifierPrompt, VerifierReply
from app.modules.reviews.infrastructure.llm.gateway import LlmGateway
from app.modules.reviews.infrastructure.llm.memory import InMemoryLlmCallTrace, InMemoryUsageLedger
from app.modules.reviews.infrastructure.llm.settings import LlmSettings
from app.modules.reviews.infrastructure.llm.transport import OpenAICompatibleTransport

type Reply = httpx.Response | Exception


async def context_checkpoint(context: RunCallContext) -> None:
    """Trust the standalone test context; production uses its persisted Run."""


def completion(content: str) -> httpx.Response:
    """A provider answer with ``content`` and a small usage block."""
    return httpx.Response(
        200,
        json={
            "model": "answering-model",
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    )


class FakeClock:
    """Wall-clock and monotonic time that only ``sleep`` advances."""

    def __init__(self, start: datetime) -> None:
        self._start = start
        self.current = start

    def now(self) -> datetime:
        return self.current

    def monotonic(self) -> float:
        return (self.current - self._start).total_seconds()

    async def sleep(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


@dataclass
class ScriptedProvider:
    """An ``httpx.MockTransport`` handler that answers ``replies`` in order and records requests.

    An ``Exception`` reply is raised as the transport error of that request.
    """

    replies: list[Reply]
    requests: list[httpx.Request] = field(default_factory=list)
    ledger: InMemoryUsageLedger = field(default_factory=InMemoryUsageLedger)
    trace: InMemoryLlmCallTrace = field(default_factory=InMemoryLlmCallTrace)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.replies:
            raise AssertionError("unexpected provider call")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def gateway(
        self, settings: LlmSettings, client: httpx.AsyncClient, clock: FakeClock
    ) -> LlmGateway:
        """A gateway on this provider's ledger and trace: fake time, no jitter."""
        return LlmGateway(
            settings,
            OpenAICompatibleTransport(client),
            self.ledger,
            self.trace,
            checkpoint=context_checkpoint,
            clock=clock,
            sleep=clock.sleep,
            jitter=lambda: 0.0,
            monotonic=clock.monotonic,
        )

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(request.content) for request in self.requests]

    def models(self) -> list[str]:
        return [body["model"] for body in self.bodies()]

    def kinds(self) -> list[str]:
        return [record.kind.value for _, record in self.trace.records]


@dataclass
class AbstainingVerifier:
    """A verifier model that answers insufficient_context for every candidate it is shown.

    It records every prompt it is sent; it knows no route window, so the pass fits the prompt
    to the execution profile alone.
    """

    prompts: list[VerifierPrompt] = field(default_factory=list)

    async def verify(self, *, system_prompt: str, user_prompt: str, **_: object) -> VerifierReply:
        self.prompts.append(VerifierPrompt(system_prompt, user_prompt))
        decisions = [
            {
                "candidate_id": candidate,
                "status": "insufficient_context",
                "reason": "Not enough code.",
                "evidence_refs": [],
                "requirement_refs": [],
            }
            for candidate in re.findall(r'candidate candidate_id="([^"]+)"', user_prompt)
        ]
        return VerifierReply(output={"decisions": decisions})


@dataclass
class SupportingVerifier:
    """A verifier model that supports every candidate it is shown, citing one line of a block.

    It records every prompt it is sent, as ``AbstainingVerifier`` does.
    """

    block_id: str
    line: int
    prompts: list[VerifierPrompt] = field(default_factory=list)

    async def verify(self, *, system_prompt: str, user_prompt: str, **_: object) -> VerifierReply:
        self.prompts.append(VerifierPrompt(system_prompt, user_prompt))
        evidence = {"block_id": self.block_id, "start_line": self.line, "end_line": self.line}
        decisions = [
            {
                "candidate_id": candidate,
                "status": "supported",
                "reason": "Shown in the diff.",
                "evidence_refs": [evidence],
                "requirement_refs": [],
            }
            for candidate in re.findall(r'candidate candidate_id="([^"]+)"', user_prompt)
        ]
        return VerifierReply(output={"decisions": decisions})
