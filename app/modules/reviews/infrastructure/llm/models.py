"""Implementations of the review and conventions model ports on top of ``LlmGateway``.

The worker (#34) creates them per claimed attempt, binding ``RunCallContext``; the
use cases keep their narrow ports and know nothing about providers or retries.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace
from uuid import UUID, uuid4

from app.modules.reviews.application.conventions import (
    ConventionsRequest,
    build_conventions_manifest,
)
from app.modules.reviews.application.conventions_prompt import (
    build_conventions_prompt,
    fit_conventions_request,
)
from app.modules.reviews.application.execute_review import PullRequestMetaSource, ReviewReply
from app.modules.reviews.application.llm import RunCallContext
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.policy_snapshot import CallManifest
from app.modules.reviews.application.prompt_budget import (
    PolicyBudgetExceededError,
    PreSendGuardError,
    TokenCounter,
    fit_review_context,
    send_budget_for,
    token_counter_for,
    verify_pre_send_guard,
    verify_provenance_pre_send_guard,
)
from app.modules.reviews.application.prompt_builder import (
    PromptBuilder,
    PullRequestMeta,
    ReviewContext,
    ReviewPrompt,
)
from app.modules.reviews.application.reflection_pass import VerifierReply
from app.modules.reviews.application.run_failures import RunFailure
from app.modules.reviews.infrastructure.llm.answers import (
    conventions_answer_validator,
    conventions_schema,
    provider_schema,
    review_answer_contract,
    validate_verifier_answer,
    verifier_answer_without_reasons,
    verifier_output_schema,
)
from app.modules.reviews.infrastructure.llm.gateway import (
    VERIFIER_TIME_RESERVE_S,
    GatewayResult,
    LlmGateway,
    StructuredTask,
)
from app.modules.reviews.infrastructure.llm.transport import ChatMessage, ResponseSchema

REVIEW_OPERATION = "review"
CONVENTIONS_OPERATION = "conventions"
VERIFICATION_OPERATION = "verification"
# TL-2 (CVS §7.3): provider-call slots a snapshot-path task keeps for the stages after it.
_REVIEW_RESERVED_CALLS = 1  # the verifier
_CONVENTIONS_RESERVED_CALLS = 2  # the review and the verifier


class GatewayReviewModel:
    """``ReviewModel``: fit the context, render it, call the gateway.

    The only fit point of a review (R10). A snapshot-path review answers with a
    ``ReviewReply`` (the fitted context and the accepted call's manifest come with the
    answer); a legacy review answers with the accepted mapping itself.
    """

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

    async def draft_review(self, *, context: ReviewContext) -> Mapping[str, object] | ReviewReply:
        result, fitted = await _fitted_review(self._gateway, context, self._run)
        self.last_result = result
        if context.policy_snapshot is None:
            return result.output
        # CONTRACT: a snapshot-path task always carries a base manifest, so the gateway
        # names the accepted call's manifest.
        assert result.accepted_manifest is not None
        return ReviewReply(result.output, fitted, result.accepted_manifest)


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
                # DD8 (E-1, E-12): snapshot path only; legacy keeps 0 and the old repair.
                bounded_repair=request.snapshot is not None,
                reserved_calls=0 if request.snapshot is None else _CONVENTIONS_RESERVED_CALLS,
            ),
            self._run,
        )
        return result.output


class GatewayVerifierModel:
    """``VerifierModel``: the verifier route, its schema and bounded repair (R11).

    The prompt and its manifest come from ``VerifierPromptBuilder``; the candidate-ID and
    reference checks stay with the caller. The worker builds one per attempt (R14).
    """

    def __init__(self, gateway: LlmGateway, run: RunCallContext) -> None:
        self._gateway = gateway
        self._run = run

    def max_prompt_tokens(self) -> int:
        """The verifier route's prompt window, which caps the verifier send budget (R10)."""
        return self._gateway.max_prompt_tokens(self._run.engine, route="verifier")

    async def verify(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        manifest: CallManifest,
        context: ReviewContext,
    ) -> VerifierReply:
        snapshot = context.policy_snapshot
        counter: TokenCounter | None = None
        if snapshot is not None:
            counter = token_counter_for(snapshot.execution_profile.counter_version)
        # llm.call names the verifier prompt version, never the review prompt the run
        # carries: None while no snapshot pins a verifier prompt.
        run = replace(
            self._run,
            prompt_version_id=None if snapshot is None else snapshot.prompt_bindings.verifier_id,
        )
        result = await self._gateway.generate(
            StructuredTask(
                operation=VERIFICATION_OPERATION,
                messages=(
                    ChatMessage("system", system_prompt),
                    ChatMessage("user", user_prompt),
                ),
                schema=ResponseSchema(
                    "VerificationOutput", provider_schema(verifier_output_schema())
                ),
                validate=validate_verifier_answer,
                base_manifest=manifest,
                review_context=context,
                policy_snapshot=snapshot,
                counter=counter,
                route="verifier",
                bounded_repair=True,
                redact_answer=verifier_answer_without_reasons,
            ),
            run,
        )
        return VerifierReply(result.output, result.accepted_call_id, result.accepted_manifest_id)


async def review_with_gateway(
    gateway: LlmGateway, context: ReviewContext, run: RunCallContext
) -> GatewayResult:
    """The whole review call: token budget, rendering, structured generation."""
    result, _ = await _fitted_review(gateway, context, run)
    return result


async def _fitted_review(
    gateway: LlmGateway, context: ReviewContext, run: RunCallContext
) -> tuple[GatewayResult, ReviewContext]:
    """The review call and the context it was fitted to."""
    # Before the fitting: a prompt version without a known answer schema never reaches it.
    answer = review_answer_contract(
        None
        if context.policy_snapshot is None
        else context.policy_snapshot.prompt_bindings.review_system_version
    )
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
    if fitted.policy_snapshot is not None:
        _guard_snapshot_budget(fitted, counter, max_tokens)
    prompt = PromptBuilder().build_prompt(fitted)
    base_manifest = PromptBuilder().build_manifest(
        fitted,
        call_id=uuid4(),
        stage="review",
        recovery_kind="none",
        counter=counter if context.policy_snapshot is not None else None,
    )
    verify_provenance_pre_send_guard(base_manifest, context=fitted, snapshot=fitted.policy_snapshot)
    snapshot_path = fitted.policy_snapshot is not None
    result = await gateway.generate(
        StructuredTask(
            operation=REVIEW_OPERATION,
            messages=_messages(prompt),
            schema=ResponseSchema(answer.name, provider_schema(answer.schema)),
            validate=answer.validate,
            base_manifest=base_manifest,
            review_context=fitted,
            policy_snapshot=fitted.policy_snapshot,
            counter=counter if context.policy_snapshot is not None else None,
            # DD8 (E-1, E-7 time, E-12): the verifier's slot and window are kept on the
            # snapshot path only; legacy keeps 0, 0 s and the old repair.
            bounded_repair=snapshot_path,
            reserved_calls=_REVIEW_RESERVED_CALLS if snapshot_path else 0,
            reserved_seconds=VERIFIER_TIME_RESERVE_S if snapshot_path else 0.0,
        ),
        run,
    )
    return result, fitted


def _guard_snapshot_budget(context: ReviewContext, counter: TokenCounter, max_tokens: int) -> None:
    """The pre-send guard of the fitted snapshot prompt: policy budget, then total size."""
    try:
        verify_pre_send_guard(context, counter, max_input_tokens=max_tokens)
    except PolicyBudgetExceededError as exc:
        raise PolicyError(
            PolicyErrorCode.PROMPT_BUDGET_EXCEEDED, str(exc), limit=exc.limit, actual=exc.actual
        ) from exc
    except PreSendGuardError as exc:
        raise RunFailure("llm_context_overflow", str(exc)) from exc


def _messages(prompt: ReviewPrompt) -> tuple[ChatMessage, ...]:
    return (ChatMessage("system", prompt.system), ChatMessage("user", prompt.user))
