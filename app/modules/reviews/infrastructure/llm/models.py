"""Implementations of the review and conventions model ports on top of ``LlmGateway``.

The worker (#34) creates them per claimed attempt, binding ``RunCallContext``; the
use cases keep their narrow ports and know nothing about providers or retries.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from uuid import UUID, uuid4

from app.modules.reviews.application.conventions import (
    ConventionsRequest,
    build_conventions_manifest,
)
from app.modules.reviews.application.conventions_prompt import (
    build_conventions_prompt,
    fit_conventions_request,
)
from app.modules.reviews.application.execute_review import PullRequestMetaSource
from app.modules.reviews.application.llm import RunCallContext
from app.modules.reviews.application.prompt_budget import (
    TokenCounter,
    fit_review_context,
    send_budget_for,
    token_counter_for,
    verify_provenance_pre_send_guard,
)
from app.modules.reviews.application.prompt_builder import (
    PromptBuilder,
    PullRequestMeta,
    ReviewContext,
    ReviewPrompt,
)
from app.modules.reviews.infrastructure.llm.answers import (
    conventions_answer_validator,
    conventions_schema,
    provider_schema,
    review_output_schema,
    validate_review_answer,
)
from app.modules.reviews.infrastructure.llm.gateway import (
    GatewayResult,
    LlmGateway,
    StructuredTask,
)
from app.modules.reviews.infrastructure.llm.transport import ChatMessage, ResponseSchema

REVIEW_OPERATION = "review"
CONVENTIONS_OPERATION = "conventions"


class GatewayReviewModel:
    """``ReviewModel``: fit the L1 context, render it, call the gateway."""

    def __init__(
        self,
        gateway: LlmGateway,
        run: RunCallContext,
        meta_source: PullRequestMetaSource,
    ) -> None:
        self._gateway = gateway
        self._run = run
        self._meta_source = meta_source
        self.last_result: GatewayResult | None = None

    def max_prompt_tokens(self) -> int:
        return self._gateway.max_prompt_tokens(self._run.engine)

    async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta | None:
        return await self._meta_source.get_pull_request_meta(run_id)

    async def draft_review(self, *, context: ReviewContext) -> Mapping[str, object]:
        result = await review_with_gateway(self._gateway, context, self._run)
        self.last_result = result
        return result.output


class GatewayConventionsModel:
    """``ConventionsModel``: the same transport, keys, limits and usage as the review."""

    def __init__(self, gateway: LlmGateway, run: RunCallContext) -> None:
        self._gateway = gateway
        self._run = run

    async def draft_conventions(self, *, request: ConventionsRequest) -> Mapping[str, object]:
        counter: TokenCounter = (
            token_counter_for(request.snapshot.execution_profile.counter_version)
            if request.snapshot is not None
            else self._gateway.token_counter()
        )
        fitted = fit_conventions_request(
            request,
            max_prompt_tokens=self._gateway.max_prompt_tokens(self._run.engine),
            counter=counter,
        )
        base_manifest = build_conventions_manifest(
            fitted,
            run_id=self._run.run_id,
            snapshot=request.snapshot,
            agents_md_sha=request.agents_md_sha,
            recovery_kind="none",
        )
        verify_provenance_pre_send_guard(base_manifest, snapshot=request.snapshot)
        prompt = build_conventions_prompt(fitted)
        result = await self._gateway.generate(
            StructuredTask(
                operation=CONVENTIONS_OPERATION,
                messages=_messages(prompt),
                schema=ResponseSchema("RepoConventionsDraft", conventions_schema()),
                validate=conventions_answer_validator(request.traced_files),
                base_manifest=base_manifest,
                policy_snapshot=request.snapshot,
                counter=counter if request.snapshot is not None else None,
            ),
            self._run,
        )
        return result.output


async def review_with_gateway(
    gateway: LlmGateway, context: ReviewContext, run: RunCallContext
) -> GatewayResult:
    """The whole review call: token budget, rendering, structured generation."""
    # The fitting renders the prompt per candidate (CPU-bound on large diffs): keep it
    # off the event loop, where the worker's heartbeat and lease renewals run.
    counter: TokenCounter = (
        token_counter_for(context.policy_snapshot.execution_profile.counter_version)
        if context.policy_snapshot is not None
        else gateway.token_counter()
    )
    max_tokens = gateway.max_prompt_tokens(run.engine)
    if context.policy_snapshot is not None:
        max_tokens = send_budget_for(
            context.policy_snapshot.execution_profile,
            stage="review",
            model_max_prompt_tokens=max_tokens,
        )
    fitted = await asyncio.to_thread(
        fit_review_context,
        context,
        max_prompt_tokens=max_tokens,
        counter=counter,
    )
    prompt = PromptBuilder().build_prompt(fitted)
    base_manifest = PromptBuilder().build_manifest(
        fitted,
        call_id=uuid4(),
        stage="review",
        recovery_kind="none",
    )
    verify_provenance_pre_send_guard(base_manifest, context=fitted, snapshot=fitted.policy_snapshot)
    return await gateway.generate(
        StructuredTask(
            operation=REVIEW_OPERATION,
            messages=_messages(prompt),
            schema=ResponseSchema("ReviewOutput", provider_schema(review_output_schema())),
            validate=validate_review_answer,
            base_manifest=base_manifest,
            review_context=fitted,
            policy_snapshot=fitted.policy_snapshot,
            counter=counter if context.policy_snapshot is not None else None,
        ),
        run,
    )


def _messages(prompt: ReviewPrompt) -> tuple[ChatMessage, ...]:
    return (ChatMessage("system", prompt.system), ChatMessage("user", prompt.user))
