"""Implementations of the review and conventions model ports on top of ``LlmGateway``.

The worker (#34) creates them per claimed attempt, binding ``RunCallContext``; the
use cases keep their narrow ports and know nothing about providers or retries.
"""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from app.modules.reviews.application.conventions import ConventionsRequest
from app.modules.reviews.application.conventions_prompt import build_conventions_prompt
from app.modules.reviews.application.execute_review import PullRequestMetaSource
from app.modules.reviews.application.llm import RunCallContext
from app.modules.reviews.application.prompt_budget import fit_review_context
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
        prompt = build_conventions_prompt(request)
        result = await self._gateway.generate(
            StructuredTask(
                operation=CONVENTIONS_OPERATION,
                messages=_messages(prompt),
                schema=ResponseSchema("RepoConventionsDraft", conventions_schema()),
                validate=conventions_answer_validator(request.changed_files),
            ),
            self._run,
        )
        return result.output


async def review_with_gateway(
    gateway: LlmGateway, context: ReviewContext, run: RunCallContext
) -> GatewayResult:
    """The whole review call: token budget, rendering, structured generation."""
    fitted = fit_review_context(
        context,
        max_prompt_tokens=gateway.max_prompt_tokens(run.engine),
        counter=gateway.token_counter(),
    )
    prompt = PromptBuilder().build_prompt(fitted)
    return await gateway.generate(
        StructuredTask(
            operation=REVIEW_OPERATION,
            messages=_messages(prompt),
            schema=ResponseSchema("ReviewOutput", provider_schema(review_output_schema())),
            validate=validate_review_answer,
        ),
        run,
    )


def _messages(prompt: ReviewPrompt) -> tuple[ChatMessage, ...]:
    return (ChatMessage("system", prompt.system), ChatMessage("user", prompt.user))
