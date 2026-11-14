"""End-to-end review-call context overflow degradation tests (Refs #461)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

import httpx
import pytest

from app.modules.reviews.application.conventions import GeneratedConventions
from app.modules.reviews.application.execute_review import ExecuteReviewRun, ReviewPromptInput
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.llm import LlmCallFailed, LlmErrorCode, RunCallContext
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.policy_snapshot import PolicySnapshot
from app.modules.reviews.application.post_processing_diff_map import diff_map_from_snapshots
from app.modules.reviews.application.prompt_builder import (
    ChangedFile,
    DiffLine,
    PullRequestMeta,
    RepoConventions,
    ReviewContext,
)
from app.modules.reviews.application.publish_run_review import PublishRunReview
from app.modules.reviews.application.reflection_pass import ReflectionPass
from app.modules.reviews.application.run_coverage import project_run_coverage
from app.modules.reviews.application.run_failures import RunFailure
from app.modules.reviews.application.store_review_output import NoModelAnswer, StoreReviewOutput
from app.modules.reviews.infrastructure.llm.models import GatewayReviewModel
from app.modules.reviews.infrastructure.llm.settings import (
    LlmSettings,
    ModelPrice,
    ModelProfile,
)
from tests.llm_fakes import AbstainingVerifier, FakeClock, ScriptedProvider
from tests.policy_snapshot_fakes import make_policy_document_present
from tests.review_publication_fakes import (
    BASE_SHA,
    DIFF_OLD_SHA,
    HEAD_SHA,
    CapturingCheckRunGateway,
    CapturingReviewGateway,
    PublicationQueueFake,
    PublicationRepositoryFake,
    PublicationUowFake,
    RecordingRunTrace,
)
from tests.verifier_pipeline_fakes import FakeProcessor, verifier_snapshot

_RUN_ID = UUID("00000000-0000-0000-0000-000000004610")
_NOW = datetime(2026, 11, 12, 7, 0, tzinfo=UTC)


def _review_snapshot(
    *,
    max_input_tokens: int = 60_000,
    max_policy_tokens: int = 12_000,
    rules_text: str | None = None,
) -> PolicySnapshot:
    base = verifier_snapshot()
    rules = (
        base.rules
        if rules_text is None
        else make_policy_document_present(".review/rules.md", rules_text, blob_oid="r" * 40)
    )
    return replace(
        base,
        rules=rules,
        prompt_bindings=replace(base.prompt_bindings, review_system_version="v4"),
        execution_profile=replace(
            base.execution_profile,
            max_input_tokens=max_input_tokens,
            max_policy_tokens=max_policy_tokens,
        ),
    )


def _settings(*, primary_window: int = 100_000, verifier_window: int | None = None) -> LlmSettings:
    price = ModelPrice(Decimal("0.000001"), Decimal("0.000001"))
    primary = ModelProfile(
        provider="router",
        base_url="https://router.test/v1",
        model="review-model",
        context_window=primary_window,
        max_output_tokens=8_000,
        price=price,
        api_keys=("sk-test-1",),
    )
    verifier = (
        None
        if verifier_window is None
        else replace(primary, model="verifier-model", context_window=verifier_window)
    )
    return LlmSettings(primary=primary, fallback=None, verifier=verifier)


def _run_call_context() -> RunCallContext:
    return RunCallContext(
        run_id=_RUN_ID,
        workspace_id=UUID("00000000-0000-0000-0000-000000004611"),
        attempt=1,
        engine="fast",
        deadline=_NOW + timedelta(hours=1),
        prompt_version_id=UUID("00000000-0000-0000-0000-000000004612"),
        rule_version_id=UUID("00000000-0000-0000-0000-000000004613"),
    )


class _PromptSource:
    def __init__(
        self,
        *,
        changed_files: tuple[ChangedFile, ...],
        omitted_files: tuple[str, ...],
        snapshot: PolicySnapshot | None,
        pr_body: str | None = None,
        total_files: int = 1,
    ) -> None:
        self._changed_files = changed_files
        self._omitted_files = omitted_files
        self._snapshot = snapshot
        self._pr_body = pr_body
        self._total_files = total_files

    async def get_review_prompt_input(
        self, run_id: UUID, saved: GeneratedConventions
    ) -> ReviewPromptInput:
        return ReviewPromptInput(
            system="Review carefully.",
            rules=(),
            agents_md=None,
            conventions=RepoConventions((), ()),
            changed_files=self._changed_files,
            omitted_files=self._omitted_files,
            policy_snapshot=self._snapshot,
        )

    async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
        return PullRequestMeta(
            "PR",
            self._pr_body,
            "author",
            "feat",
            "main",
            (),
            self._total_files,
            self._total_files,
            0,
            False,
            False,
        )


@dataclass
class StoreBundle:
    repo: PublicationRepositoryFake
    queue: PublicationQueueFake
    published_raw: list[Any]
    review_gateway: CapturingReviewGateway
    check_runs: CapturingCheckRunGateway
    store: StoreReviewOutput
    run_publisher: PublishRunReview


@dataclass
class ExecutionResult:
    repo: PublicationRepositoryFake
    queue: PublicationQueueFake
    published_raw: list[Any]
    review_gateway: CapturingReviewGateway
    check_runs: CapturingCheckRunGateway
    provider: ScriptedProvider
    outcome: bool

    def __iter__(self) -> Any:
        return iter(
            (
                self.repo,
                self.queue,
                self.published_raw,
                self.review_gateway,
                self.check_runs,
                self.provider,
                self.outcome,
            )
        )


def _make_store_bundle(
    snapshots: list[DiffSnapshot],
    *,
    snapshot_path: bool,
) -> StoreBundle:
    coverage = project_run_coverage(
        snapshots,
        diff_map_from_snapshots(
            repository_id=UUID(int=1),
            code_change_number=7,
            head_sha=HEAD_SHA,
            base_sha=BASE_SHA,
            diff_old_sha=DIFF_OLD_SHA,
            snapshots=snapshots,
        ),
        snapshot_path=snapshot_path,
    )
    repo = PublicationRepositoryFake(snapshots, coverage=coverage)
    queue = PublicationQueueFake()
    committed: list[str] = []
    store = StoreReviewOutput(
        lambda: PublicationUowFake(repo, committed),
        queue,
        worker_id="worker",
        run_url=lambda _: None,
        now=lambda: _NOW,
    )
    review_gateway = CapturingReviewGateway()
    check_runs = CapturingCheckRunGateway()
    run_publisher = PublishRunReview(
        uow_factory=lambda: PublicationUowFake(repo, committed),
        reviews=review_gateway,
        check_runs=check_runs,
        trace=RecordingRunTrace(),
        now=lambda: _NOW,
    )
    return StoreBundle(repo, queue, [], review_gateway, check_runs, store, run_publisher)


class _WindowVerifier(AbstainingVerifier):
    def __init__(self, max_tokens: int) -> None:
        super().__init__()
        self._max_tokens = max_tokens

    def max_prompt_tokens(self) -> int:
        return self._max_tokens


def _execute_and_publish(
    *,
    snapshots: list[DiffSnapshot],
    changed_files: tuple[ChangedFile, ...],
    omitted_files: tuple[str, ...] = (),
    snapshot: PolicySnapshot | None,
    replies: list[httpx.Response | Exception],
    pr_body: str | None = None,
    primary_window: int = 100_000,
    verifier_window: int | None = None,
    bundle: StoreBundle | None = None,
) -> ExecutionResult:
    b = bundle or _make_store_bundle(snapshots, snapshot_path=snapshot is not None)
    repo, queue, published_raw, review_gateway, check_runs, store, run_publisher = (
        b.repo,
        b.queue,
        b.published_raw,
        b.review_gateway,
        b.check_runs,
        b.store,
        b.run_publisher,
    )
    provider = ScriptedProvider(list(replies))
    settings = _settings(primary_window=primary_window, verifier_window=verifier_window)
    prompts = _PromptSource(
        changed_files=changed_files,
        omitted_files=omitted_files,
        snapshot=snapshot,
        pr_body=pr_body,
        total_files=len(snapshots),
    )

    class _CapturingPublisher:
        async def execute(self, run_id: UUID, raw_output: Any) -> bool:
            published_raw.append(raw_output)
            return await store.execute(run_id, raw_output)

    verifier_max = (
        None
        if verifier_window is None
        else max(0, verifier_window - settings.verifier_route()[0].max_output_tokens)
    )
    verifier: AbstainingVerifier = (
        AbstainingVerifier() if verifier_max is None else _WindowVerifier(verifier_max)
    )

    class _SpyChangeSummary:
        def __init__(self) -> None:
            self.calls: list[Any] = []

        async def summarize_change(self, *, request: Any) -> Any:
            self.calls.append(request)
            return ()

    spy_change_summary = _SpyChangeSummary()

    async def _exercise() -> bool:
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider.handle)) as client:
            model = GatewayReviewModel(
                provider.gateway(settings, client, FakeClock(_NOW)),
                _run_call_context(),
                prompts,
            )
            reflection = ReflectionPass(verifier) if snapshot is not None else None
            pipeline = ExecuteReviewRun(
                FakeProcessor(),
                prompts,
                model,
                _CapturingPublisher(),
                reflection_pass=reflection,
                change_summary=cast(Any, spy_change_summary),
            )
            stored = await pipeline.execute(_RUN_ID)
            for pointer in queue.pointers:
                await run_publisher.execute(pointer)
            return stored

    outcome = asyncio.run(_exercise())
    assert verifier.prompts == []
    assert spy_change_summary.calls == []
    return ExecutionResult(
        repo,
        queue,
        published_raw,
        review_gateway,
        check_runs,
        provider,
        outcome,
    )


@pytest.mark.parametrize(
    "pr_body",
    [None, "Overview. " * 400],
    ids=["zero-files-fit", "pr-meta-overflow"],
)
def test_execute_review_context_overflow_publishes_partial_review_end_to_end(
    pr_body: str | None,
) -> None:
    line_payload = "x = " + "a" * 6_500
    snapshots = [
        DiffSnapshot(
            f"app/mod_{idx}.py",
            f"@@ -0,0 +1,1 @@\n+{line_payload}",
            HEAD_SHA,
            status="added",
            additions=1,
            changes=1,
            review_patch=f"@@ -0,0 +1,1 @@\n+{line_payload}",
        )
        for idx in range(1, 8)
    ]
    changed_files = tuple(
        ChangedFile(
            f"app/mod_{idx}.py",
            "added",
            (DiffLine(1, "added", line_payload),),
        )
        for idx in range(1, 8)
    )
    snapshot = _review_snapshot(max_input_tokens=10_000, max_policy_tokens=12_000)

    repo, queue, published_raw, reviews, check_runs, provider, outcome = _execute_and_publish(
        snapshots=snapshots,
        changed_files=changed_files,
        snapshot=snapshot,
        replies=[],
        pr_body=pr_body,
    )

    assert outcome is True
    assert provider.requests == []
    assert len(published_raw) == 1 and isinstance(published_raw[0], NoModelAnswer)
    assert repo.answers == [None]
    assert repo.state == "succeeded"
    assert repo.error_code is None
    assert repo.response["verdict"] is None
    assert repo.response["coverage"]["status"] == "partial"
    assert repo.response["coverage"]["reviewed_files"] == 0
    assert repo.response["coverage"]["reason_counts"] == {"review_input_incomplete": 7}
    assert [pointer.review_event for pointer in queue.pointers] == ["COMMENT"]
    assert len(reviews.submissions) == 1
    assert reviews.submissions[0].event == "COMMENT"
    assert reviews.submissions[0].findings == ()
    assert "- ` app/mod_1.py `: модель получила не весь patch" in reviews.submissions[0].body
    assert "- Ещё причин: 2" in reviews.submissions[0].body
    assert "и ещё 2" in reviews.submissions[0].body
    assert len(check_runs.views) == 1
    assert check_runs.views[0].status == "completed"
    assert check_runs.views[0].conclusion == "neutral"
    assert check_runs.views[0].title == "AI-ревью: проверка неполная"


@pytest.mark.parametrize(
    ("status_code", "payload"),
    [
        (400, {"json": {"error": {"code": "context_length_exceeded", "message": "too long"}}}),
        (413, {"text": "Request Entity Too Large"}),
    ],
    ids=["http-400-length", "http-413"],
)
def test_execute_review_runtime_provider_overflow_publishes_partial_review(
    status_code: int,
    payload: dict[str, Any],
) -> None:
    snapshots = [
        DiffSnapshot(
            "app/main.py",
            "@@ -0,0 +1,1 @@\n+x = 1",
            HEAD_SHA,
            status="added",
            additions=1,
            changes=1,
            review_patch="@@ -0,0 +1,1 @@\n+x = 1",
        )
    ]
    snapshot = _review_snapshot(max_input_tokens=60_000, max_policy_tokens=12_000)

    repo, queue, published_raw, reviews, check_runs, provider, outcome = _execute_and_publish(
        snapshots=snapshots,
        changed_files=(ChangedFile("app/main.py", "added", (DiffLine(1, "added", "x = 1"),)),),
        snapshot=snapshot,
        replies=[httpx.Response(status_code, **payload)],
    )

    assert outcome is True
    assert len(provider.requests) == 1
    assert len(published_raw) == 1 and isinstance(published_raw[0], NoModelAnswer)
    assert repo.state == "succeeded"
    assert repo.error_code is None
    assert repo.response["verdict"] is None
    assert repo.response["coverage"]["status"] == "partial"
    assert [pointer.review_event for pointer in queue.pointers] == ["COMMENT"]
    assert len(reviews.submissions) == 1
    assert "- ` app/main.py `: модель получила не весь patch" in reviews.submissions[0].body
    assert check_runs.views[0].conclusion == "neutral"
    assert check_runs.views[0].title == "AI-ревью: проверка неполная"


def test_execute_review_legacy_technical_only_overflow_publishes_none_coverage() -> None:
    snapshots = [DiffSnapshot("data/huge.txt", None, "d" * 40, omission_reason="too_large")]

    repo, queue, published_raw, reviews, check_runs, _, outcome = _execute_and_publish(
        snapshots=snapshots,
        changed_files=(),
        omitted_files=("data/huge.txt",),
        snapshot=None,
        replies=[httpx.Response(413, text="Request Entity Too Large")],
    )

    assert outcome is True
    assert len(published_raw) == 1 and isinstance(published_raw[0], NoModelAnswer)
    assert repo.state == "succeeded"
    assert repo.error_code is None
    assert repo.response["verdict"] is None
    assert repo.response["coverage"]["status"] == "none"
    assert [pointer.review_event for pointer in queue.pointers] == ["COMMENT"]
    assert len(reviews.submissions) == 1
    assert check_runs.views[0].conclusion == "neutral"
    assert check_runs.views[0].title == "AI-ревью: нет доступных для анализа изменений"


@pytest.mark.parametrize(
    ("max_input_tokens", "max_policy_tokens", "rules_text", "primary_window", "verifier_window"),
    [
        (60_000, 1, None, 100_000, None),
        (9_500, 12_000, "# Rules\n" + ("Keep every boundary isolated. " * 200), 10_000, 100_000),
    ],
    ids=["over-max-policy-tokens", "policy-exceeds-review-route-window"],
)
def test_execute_review_policy_budget_exceeded_never_publishes_review(
    max_input_tokens: int,
    max_policy_tokens: int,
    rules_text: str | None,
    primary_window: int,
    verifier_window: int | None,
) -> None:
    snapshots = [
        DiffSnapshot(
            "app/main.py",
            "@@ -0,0 +1,1 @@\n+x = 1",
            HEAD_SHA,
            status="added",
            additions=1,
            changes=1,
            review_patch="@@ -0,0 +1,1 @@\n+x = 1",
        )
    ]
    bundle = _make_store_bundle(snapshots, snapshot_path=True)
    repo = bundle.repo
    queue = bundle.queue
    published_raw = bundle.published_raw
    reviews = bundle.review_gateway
    check_runs = bundle.check_runs
    snapshot = _review_snapshot(
        max_input_tokens=max_input_tokens,
        max_policy_tokens=max_policy_tokens,
        rules_text=rules_text,
    )

    with pytest.raises(PolicyError) as raised:
        _execute_and_publish(
            snapshots=snapshots,
            changed_files=(ChangedFile("app/main.py", "added", (DiffLine(1, "added", "x = 1"),)),),
            snapshot=snapshot,
            replies=[],
            primary_window=primary_window,
            verifier_window=verifier_window,
            bundle=bundle,
        )

    assert raised.value.code == PolicyErrorCode.PROMPT_BUDGET_EXCEEDED
    assert published_raw == []
    assert queue.pointers == []
    assert reviews.submissions == []
    assert check_runs.views == []
    assert repo.state == "running"


@pytest.mark.parametrize(
    ("prepare_exc", "draft_exc"),
    [
        (None, LlmCallFailed(LlmErrorCode.UNAVAILABLE, "provider down")),
        (None, RunFailure("verification_context_overflow", "verifier envelope too large")),
        (LlmCallFailed(LlmErrorCode.CONTEXT_OVERFLOW, "conventions overflow"), None),
    ],
    ids=["non-overflow-llm-failure", "verification-context-overflow", "conventions-overflow"],
)
def test_execute_review_non_overflow_and_conventions_overflow_fail_without_publishing(
    prepare_exc: Exception | None,
    draft_exc: Exception | None,
) -> None:
    published_raw: list[Any] = []

    class _Processor:
        async def prepare(self, run_id: UUID) -> Any:
            if prepare_exc is not None:
                raise prepare_exc
            return await FakeProcessor().prepare(run_id)

    class _Model(_PromptSource):
        async def draft_review(self, *, context: ReviewContext) -> dict[str, Any]:
            assert draft_exc is not None
            raise draft_exc

    class _Publisher:
        async def execute(self, run_id: UUID, raw_output: Any) -> bool:
            published_raw.append(raw_output)
            return True

    source = _Model(
        changed_files=(ChangedFile("app/main.py", "added", (DiffLine(1, "added", "x = 1"),)),),
        omitted_files=(),
        snapshot=None,
    )
    pipeline = ExecuteReviewRun(_Processor(), source, source, _Publisher())

    expected_exc = prepare_exc if prepare_exc is not None else draft_exc
    assert expected_exc is not None
    with pytest.raises((LlmCallFailed, RunFailure)) as exc_info:
        asyncio.run(pipeline.execute(_RUN_ID))

    actual_exc = exc_info.value
    expected_code = getattr(expected_exc, "error_code", None)
    actual_code = getattr(actual_exc, "error_code", None)
    assert actual_code == expected_code
    assert published_raw == []


def test_execute_review_legacy_overflow_with_reviewable_files_publishes_partial_coverage() -> None:
    snapshots = [
        DiffSnapshot(
            "app/main.py",
            "@@ -0,0 +1,2 @@\n+x = 1\n+y = 2",
            HEAD_SHA,
            status="added",
            additions=2,
            changes=2,
            review_patch="@@ -0,0 +1,2 @@\n+x = 1\n+y = 2",
        )
    ]
    res = _execute_and_publish(
        snapshots=snapshots,
        changed_files=(
            ChangedFile(
                "app/main.py",
                "added",
                (DiffLine(1, "added", "x = 1"), DiffLine(2, "added", "y = 2")),
            ),
        ),
        omitted_files=(),
        snapshot=None,
        replies=[httpx.Response(413, text="Request Entity Too Large")],
    )

    assert res.outcome is True
    assert len(res.published_raw) == 1 and isinstance(res.published_raw[0], NoModelAnswer)
    assert res.repo.state == "succeeded"
    assert res.repo.error_code is None
    assert res.repo.response["verdict"] is None
    assert res.repo.response["coverage"]["status"] == "partial"
    assert res.repo.response["coverage"]["reviewed_files"] == 0
    assert res.repo.response["coverage"]["reason_counts"] == {"review_input_incomplete": 1}
    assert [pointer.review_event for pointer in res.queue.pointers] == ["COMMENT"]
    assert len(res.review_gateway.submissions) == 1
    assert "app/main.py" in res.review_gateway.submissions[0].body
    assert res.check_runs.views[0].conclusion == "neutral"
    assert res.check_runs.views[0].title == "AI-ревью: проверка неполная"
