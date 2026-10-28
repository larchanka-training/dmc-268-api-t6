"""A Bitbucket run reaches the ordinary review and verifier without other VCS ports."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.worker as worker
from app.bootstrap.bitbucket_reviews import BitbucketAttemptBundle
from app.modules.integrations.bitbucket.infrastructure.run_http import BitbucketRunHttp
from app.modules.reviews.application.bitbucket_status import BitbucketStatusSnapshot
from app.modules.reviews.application.diff_map import VcsProviderName, build_diff_map
from app.modules.reviews.application.findings_post_processor import ProcessedReviewOutput
from app.modules.reviews.application.handle_review_run import ClaimedAttempt
from app.modules.reviews.application.policy_errors import PolicyError
from app.modules.reviews.application.prepare_policy_snapshot import PreparePolicySnapshot
from app.modules.reviews.application.process_run import RunVcsInput
from app.modules.reviews.application.publish_run_review import (
    PublicationIdentity,
    PublishContext,
)
from app.modules.reviews.application.queue_messages import ReviewPublishPointer
from app.modules.reviews.application.review_context import BuildReviewContext, ContextLevel
from app.modules.reviews.application.review_context_stage import (
    EstimatedTokenCounter,
    ReviewContextStage,
    vcs_file_from_snapshot,
)
from app.modules.reviews.application.review_output import (
    FindingPostProcessingInput,
    ReviewPublication,
)
from app.modules.reviews.application.review_path import ReviewBinding, require_served_provider
from app.modules.reviews.application.run_coverage import restore_run_coverage
from app.modules.reviews.application.run_failures import RunFailure
from app.modules.reviews.application.store_review_output import PublishingGuard
from app.modules.reviews.application.vcs_diff import PullRequestLocator
from app.modules.reviews.infrastructure.bitbucket_ci import BitbucketBuildStatusPublisher
from app.modules.reviews.infrastructure.bitbucket_policy_source import BitbucketPolicyDocumentSource
from app.modules.reviews.infrastructure.bitbucket_publication_resources import (
    BitbucketPublicationHttp,
)
from app.modules.reviews.infrastructure.bitbucket_repository_tree import BitbucketRepositoryTree
from app.modules.reviews.infrastructure.bitbucket_review_publication import (
    BitbucketPullRequestCommentGateway,
)
from app.modules.reviews.infrastructure.bitbucket_revision_source import BitbucketRevisionSource
from app.modules.reviews.infrastructure.bitbucket_run_source import BitbucketRunSource
from app.modules.reviews.infrastructure.bitbucket_vcs import HttpBitbucketVcsProvider
from app.modules.reviews.infrastructure.blob_cache import InMemoryBlobCache
from app.modules.reviews.infrastructure.llm.gateway import LlmGateway
from app.modules.reviews.infrastructure.llm.memory import InMemoryLlmCallTrace, InMemoryUsageLedger
from app.modules.reviews.infrastructure.llm.settings import LlmSettings
from tests.llm_fakes import context_checkpoint
from tests.policy_snapshot_fakes import make_complete_prompt_bindings
from tests.test_bitbucket_vcs import BASE, DEST, HEAD, PREFIX, TARGET, Credentials, _locator, _pr
from tests.test_gateway_review_model import SUMMARY, _finding, _profile
from tests.test_gitlab_snapshot_path import GitLab, NoGitHub, Output, Runs, Tree, _checkpoint
from tests.test_prepare_policy_snapshot import Source, Uow
from tests.test_review_context_stage import Parser, Trace
from tests.test_review_path import RecordingConventions, _binding, _store
from tests.test_worker_reflection_pass import _Answers, _Manifests
from tests.trigger_uow import processing_uow

RUN_ID = UUID(int=2)
REPO_ID = UUID(int=1)
LOCATOR = PullRequestLocator(0, "team/project", 7, "bitbucket", UUID(int=169), 19, DEST)
RUN = RunVcsInput(UUID(int=5), REPO_ID, "a" * 40, "b" * 40, LOCATOR)


@pytest.mark.parametrize("provider", ["github", "gitlab", "bitbucket"])
def test_snapshot_review_provider_gate_serves_known_providers(provider: str) -> None:
    require_served_provider(ReviewBinding(UUID(int=1), "review.system", "v4", provider))
    with pytest.raises(PolicyError):
        require_served_provider(ReviewBinding(UUID(int=1), "review.system", "v4", "unknown"))


class BitbucketRuns(Runs):
    async def get_run_vcs_input(self, run_id: UUID) -> RunVcsInput:
        assert run_id == RUN_ID
        return RUN

    async def get_run_conventions_input(self, run_id: UUID):  # type: ignore[no-untyped-def]
        return replace(
            await super().get_run_conventions_input(run_id),
            review_binding=_binding(provider="bitbucket"),
        )

    async def get_review_prompt_input(self, run_id: UUID, conventions):  # type: ignore[no-untyped-def]
        return replace(
            await super().get_review_prompt_input(run_id, conventions),
            review_binding=_binding(provider="bitbucket"),
        )


@pytest.mark.parametrize("with_verifier", [True, False])
def test_bitbucket_full_mocked_review_never_uses_github_or_gitlab(
    monkeypatch: pytest.MonkeyPatch, with_verifier: bool
) -> None:
    runs, vcs, forbidden = BitbucketRuns(), GitLab(), NoGitHub()
    store, trace, output = _store(), Trace(), Output()
    assert store.pins is not None
    store.pins = replace(
        store.pins,
        prompt_bindings=replace(
            make_complete_prompt_bindings(
                review_system_id=UUID(int=41),
                conventions_id=UUID(int=43),
                verifier_id=UUID(int=44),
            ),
            review_system_version="v4",
        ),
    )
    conventions, cache = RecordingConventions([]), InMemoryBlobCache()
    stage = ReviewContextStage(
        runs=runs,
        checkpoint=_checkpoint,
        vcs=vcs,
        merge_base=vcs,
        prepare_policy=PreparePolicySnapshot(
            uow_factory=lambda: Uow(store), source_factory=lambda pins: Source()
        ),
        context_builder=lambda run: BuildReviewContext(
            source=Tree(), parser=Parser(), counter=EstimatedTokenCounter()
        ),
        trace=trace,
    )
    transport = _Answers(json.dumps({"findings": [_finding(1)], "summary": SUMMARY}))
    manifests, llm_trace = _Manifests(), InMemoryLlmCallTrace()
    gateway = LlmGateway(
        LlmSettings(primary=_profile(), fallback=None),
        transport,
        InMemoryUsageLedger(),
        llm_trace,
        checkpoint=context_checkpoint,
        manifest_store=manifests,
    )
    claimed = ClaimedAttempt(
        RUN_ID,
        UUID(int=170),
        2,
        "fast",
        datetime.now(UTC) + timedelta(minutes=5),
        UUID(int=41),
        UUID(int=4),
        "bitbucket-worker",
    )
    provider = worker.attempt_model_provider(gateway, cast(Any, vcs), claimed)
    monkeypatch.setattr(worker, "SqlAlchemyRunRepository", lambda *a, **kw: runs)
    monkeypatch.setattr(worker, "SqlAlchemyReviewPromptRepository", lambda *a: runs)
    monkeypatch.setattr(worker, "SqlAlchemyBlobCache", lambda *a: cache)
    monkeypatch.setattr(worker, "GenerateRepoConventions", lambda *a: conventions)
    monkeypatch.setattr(
        worker,
        "SqlAlchemyRunProcessingUnitOfWork",
        lambda *a: processing_uow(runs, cache)(),
    )

    async def process() -> bool:
        return await worker.process_review_run(
            RUN_ID,
            provider,
            cast(async_sessionmaker[AsyncSession], object()),
            cast(Any, forbidden),
            output=output,
            context_stage=stage,
            reflection_pass=provider.reflection_pass if with_verifier else None,
            vcs_provider_selector=worker.BoundVcsProviderSelector(LOCATOR, vcs),
        )

    if not with_verifier:
        with pytest.raises(RunFailure):
            asyncio.run(process())
        assert output.outputs == []
    else:
        assert asyncio.run(process()) is True
        assert len(output.outputs) == 1
        assert stage.head_sha == RUN.head_sha
        assert stage.snapshot_for_llm is store.snapshot
    assert forbidden.calls == 0


@pytest.mark.parametrize("technical_only", [False, True])
def test_composed_bitbucket_review_uses_pinned_cloud_reads_and_writes(
    monkeypatch: pytest.MonkeyPatch, technical_only: bool
) -> None:
    """The worker's VCS, policy, source and context adapters share one pinned Cloud route."""
    source_bytes = b"def compute():\n    return 2\n"
    file_path = "src/app.py"
    agents_bytes = b"Review Python changes with the repository rules.\n"
    rules_bytes = b"# Repository rules\n\nCheck changed function behavior.\n"
    seen: list[httpx.Request] = []
    comments: list[dict[str, object]] = []
    statuses: list[dict[str, str]] = []
    run = RunVcsInput(UUID(int=5), TARGET.repository_id, HEAD, BASE, _locator())

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.scheme == "https"
        assert request.url.host == "api.bitbucket.org"
        assert request.headers["Authorization"] == "Bearer secret-token"
        path = request.url.path
        if path == f"{PREFIX}/pullrequests/7":
            return httpx.Response(200, json=_pr(head=HEAD[:12], base=BASE[:12]))
        if path == f"{PREFIX}/commit/{HEAD[:12]}":
            return httpx.Response(200, json={"hash": HEAD})
        if path == f"{PREFIX}/commit/{BASE[:12]}":
            return httpx.Response(200, json={"hash": BASE})
        if path == f"{PREFIX}/pullrequests/7/commits":
            return httpx.Response(200, json={"values": [{"hash": HEAD, "message": "Fix parser"}]})
        if path == f"{PREFIX}/diffstat/{HEAD}..{BASE}":
            return httpx.Response(
                200,
                json={
                    "size": 1,
                    "page": 1,
                    "pagelen": 100,
                    "values": [
                        {
                            "status": "modified",
                            "lines_added": 1,
                            "lines_removed": 1,
                            "old": {"path": file_path},
                            "new": {"path": file_path, "size": len(source_bytes)},
                        }
                    ],
                },
            )
        if path == f"{PREFIX}/diff/{HEAD}..{BASE}":
            if technical_only:
                # A bounded Bitbucket patch read records this file as too_large.
                return httpx.Response(200, content=b"x" * (1024 * 1024 + 1))
            return httpx.Response(
                200,
                text=(
                    f"diff --git a/{file_path} b/{file_path}\n"
                    f"--- a/{file_path}\n+++ b/{file_path}\n"
                    "@@ -1,2 +1,2 @@\n def compute():\n-    return 1\n+    return 2\n"
                ),
            )
        if path == f"{PREFIX}/merge-base/{HEAD}..{BASE}":
            return httpx.Response(200, json={"hash": BASE})
        if path == f"{PREFIX}/src/{BASE}/":
            return httpx.Response(
                200,
                json={
                    "size": 2,
                    "values": [
                        {
                            "type": "commit_file",
                            "path": "AGENTS.md",
                            "size": len(agents_bytes),
                            "commit": {"hash": BASE},
                        },
                        {"type": "commit_directory", "path": ".review", "commit": {"hash": BASE}},
                    ],
                },
            )
        if path == f"{PREFIX}/src/{BASE}/.review/":
            return httpx.Response(
                200,
                json={
                    "size": 1,
                    "values": [
                        {
                            "type": "commit_file",
                            "path": ".review/rules.md",
                            "size": len(rules_bytes),
                            "commit": {"hash": BASE},
                        },
                    ],
                },
            )
        if path == f"{PREFIX}/src/{BASE}/AGENTS.md":
            return httpx.Response(200, content=agents_bytes)
        if path == f"{PREFIX}/src/{BASE}/.review/rules.md":
            return httpx.Response(200, content=rules_bytes)
        if path == f"{PREFIX}/src/{HEAD}/":
            return httpx.Response(
                200,
                json={
                    "size": 1,
                    "values": [
                        {"type": "commit_directory", "path": "src", "commit": {"hash": HEAD}}
                    ],
                },
            )
        if path == f"{PREFIX}/src/{HEAD}/src/":
            return httpx.Response(
                200,
                json={
                    "size": 1,
                    "values": [
                        {
                            "type": "commit_file",
                            "path": "src/app.py",
                            "size": len(source_bytes),
                            "commit": {"hash": HEAD},
                        }
                    ],
                },
            )
        if path == f"{PREFIX}/src/{HEAD}/src/app.py":
            return httpx.Response(200, content=source_bytes)
        if path == f"{PREFIX}/pullrequests/7/comments" and request.method == "GET":
            return httpx.Response(200, json={"values": comments})
        if path == f"{PREFIX}/pullrequests/7/comments" and request.method == "POST":
            payload = json.loads(request.content)
            comment = {"id": 100 + len(comments), **payload}
            comments.append(comment)
            return httpx.Response(201, json=comment)
        if path == f"{PREFIX}/commit/{HEAD}/statuses/build" and request.method == "POST":
            payload = json.loads(request.content)
            statuses.append(payload)
            return httpx.Response(201, json=payload)
        raise AssertionError(f"unexpected Bitbucket request: {request.method} {request.url}")

    class CloudCredentials(Credentials):
        async def checkpoint(self, target: object) -> None:
            assert target == TARGET

    class CloudRuns(BitbucketRuns):
        async def get_run_vcs_input(self, run_id: UUID) -> RunVcsInput:
            assert run_id == RUN_ID
            return run

        async def get_run_conventions_input(self, run_id: UUID):  # type: ignore[no-untyped-def]
            return replace(
                await super().get_run_conventions_input(run_id), repository_id=TARGET.repository_id
            )

    async def scenario() -> None:
        credentials = CloudCredentials()
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            http = BitbucketRunHttp(client, credentials, TARGET)
            vcs = HttpBitbucketVcsProvider(http=http, locator=run.locator)
            trees = BitbucketRepositoryTree(
                http=http, expected_input=run, vcs=vcs, checkpoint=_checkpoint
            )
            source = BitbucketRunSource(trees=trees, vcs=vcs, run_id=RUN_ID)

            class WorkerTrace(Trace):
                def __init__(self) -> None:
                    super().__init__()
                    self.payload_rows: list[dict[str, Any]] = []

                async def save_context_payload(self, run_id: UUID, summary: dict[str, Any]) -> None:
                    assert run_id == RUN_ID
                    self.payload_rows.append(summary)

            runs, store, trace = CloudRuns(), _store(), WorkerTrace()
            assert store.pins is not None
            store.pins = replace(
                store.pins,
                repository_id=TARGET.repository_id,
                prompt_bindings=replace(
                    make_complete_prompt_bindings(
                        review_system_id=UUID(int=41),
                        conventions_id=UUID(int=43),
                        verifier_id=UUID(int=44),
                    ),
                    review_system_version="v4",
                ),
            )
            cache = InMemoryBlobCache()
            stage = ReviewContextStage(
                runs=runs,
                checkpoint=_checkpoint,
                vcs=vcs,
                merge_base=vcs,
                prepare_policy=PreparePolicySnapshot(
                    uow_factory=lambda: Uow(store),
                    source_factory=lambda pins: BitbucketPolicyDocumentSource(
                        trees=trees, policy_base_sha=pins.policy_base_sha
                    ),
                ),
                context_builder=lambda selected: BuildReviewContext(
                    source=BitbucketRevisionSource(trees=trees, locator=selected.locator),
                    parser=Parser(),
                    counter=EstimatedTokenCounter(),
                ),
                trace=trace,
                payloads=trace,
            )
            answers = _Answers(json.dumps({"findings": [_finding(1)], "summary": SUMMARY}))
            gateway = LlmGateway(
                LlmSettings(primary=_profile(), fallback=None),
                answers,
                InMemoryUsageLedger(),
                InMemoryLlmCallTrace(),
                checkpoint=context_checkpoint,
                manifest_store=_Manifests(),
            )
            claimed = ClaimedAttempt(
                RUN_ID,
                TARGET.workspace_id,
                2,
                "fast",
                datetime.now(UTC) + timedelta(minutes=5),
                UUID(int=41),
                UUID(int=4),
                "bitbucket-worker",
            )

            class MemoryRun:
                state = "running"
                findings_hash = ""
                review_body = "Review summary"
                coverage_status: str | None = None

                async def cancellation_reason(self, run_id: UUID) -> None:
                    assert run_id == RUN_ID
                    return None

                async def get_post_processing_input(
                    self, run_id: UUID
                ) -> FindingPostProcessingInput:
                    assert run_id == RUN_ID
                    assert runs.snapshots is not None
                    pulled = await vcs.get_pull_request(run.locator, checkpoint=_checkpoint)
                    diff_map = build_diff_map(
                        pulled,
                        tuple(vcs_file_from_snapshot(item) for item in runs.snapshots),
                        provider=VcsProviderName.BITBUCKET,
                        repository_id=run.repository_id,
                        diff_old_sha=BASE,
                    )
                    coverage = None
                    if technical_only:
                        assert len(trace.payload_rows) == 1
                        coverage = restore_run_coverage(
                            trace.payload_rows[0]["coverage"],
                            snapshots=runs.snapshots,
                            diff_map=diff_map,
                            run_id=RUN_ID,
                        )
                    return FindingPostProcessingInput(
                        diff_map,
                        frozenset({"src/app.py"}),
                        frozenset({"AGENTS.md", ".review/rules.md"}),
                        coverage=coverage,
                    )

                async def lock_for_publishing(self, run_id: UUID) -> PublishingGuard:
                    assert run_id == RUN_ID
                    return PublishingGuard(
                        "running", claimed.worker_id, False, HEAD, True, "COMMENT"
                    )

                async def store_review_output(
                    self,
                    run_id: UUID,
                    answer: object,
                    parsed: object,
                    processed: ProcessedReviewOutput,
                ) -> ReviewPublication:
                    assert run_id == RUN_ID
                    assert processed.inline == ()
                    self.review_body = processed.review_body
                    self.coverage_status = processed.coverage.status.value
                    return ReviewPublication(HEAD, (), self.review_body)

                async def enter_publishing(self, run_id: UUID, **kwargs: Any) -> None:
                    assert run_id == RUN_ID
                    self.state = "publishing"
                    self.findings_hash = kwargs["postprocess_response"]["findings_hash"]

                async def publish_context(self, run_id: UUID) -> PublishContext:
                    assert run_id == RUN_ID
                    return PublishContext(
                        run_id=RUN_ID,
                        state=self.state,
                        cancel_requested=False,
                        head_sha=HEAD,
                        pr_head_sha=HEAD,
                        pr_open=True,
                        origin_active=True,
                        installation_id=run.locator.installation_external_id,
                        repository_full_name=run.locator.repository_full_name,
                        pr_number=7,
                        review_body=self.review_body,
                        findings=(),
                        findings_hash=self.findings_hash,
                        provider="bitbucket",
                        project_id=99,
                        workspace_id=TARGET.workspace_id,
                        vcs_input=run,
                        publication_identity=PublicationIdentity(
                            TARGET.installation_id,
                            TARGET.workspace_id,
                            run.locator.installation_external_id,
                            "bitbucket",
                            run.repository_id,
                            run.code_change_id,
                            BASE,
                        ),
                    )

                async def provider_origin_active(self, run_id: UUID) -> bool:
                    assert run_id == RUN_ID
                    return True

                async def complete_publication(
                    self,
                    run_id: UUID,
                    *,
                    review_id: int,
                    findings_hash: str,
                    **kwargs: Any,
                ) -> str:
                    assert run_id == RUN_ID and review_id == 100
                    assert findings_hash == self.findings_hash
                    self.state = "succeeded"
                    return self.state

            memory = MemoryRun()

            class MemoryUow:
                runs = memory
                reviews = memory

                async def __aenter__(self) -> MemoryUow:
                    return self

                async def __aexit__(self, *args: object) -> None:
                    return None

                async def commit(self) -> None:
                    return None

                async def rollback(self) -> None:
                    return None

            class Queue:
                def __init__(self) -> None:
                    self.pointers: list[object] = []

                async def publish_review(self, pointer: object) -> None:
                    self.pointers.append(pointer)

            queue = Queue()

            class StatusSource:
                async def current(self, run_id: UUID) -> BitbucketStatusSnapshot:
                    assert run_id == RUN_ID
                    return BitbucketStatusSnapshot(
                        RUN_ID,
                        TARGET,
                        7,
                        HEAD,
                        "feature",
                        memory.state,
                        coverage_status=memory.coverage_status,
                    )

            statuses = BitbucketBuildStatusPublisher(
                StatusSource(),
                credentials,
                transport_factory=lambda: httpx.MockTransport(respond),
            )

            class Publications:
                @asynccontextmanager
                async def open(
                    self, context: PublishContext, *, checkpoint: Any
                ) -> AsyncIterator[BitbucketPullRequestCommentGateway]:
                    assert context.run_id == RUN_ID and context.vcs_input == run
                    publication_http = BitbucketPublicationHttp(
                        client, credentials, TARGET, pr_number=7, checkpoint=checkpoint
                    )
                    yield BitbucketPullRequestCommentGateway(
                        publication_http, run_id=RUN_ID, repository_uuid=DEST
                    )

            class Resources:
                def __init__(self) -> None:
                    self.publications = Publications()
                    self.statuses = statuses
                    self.status_reconciler = None

                @asynccontextmanager
                async def attempt(self, **kwargs: Any) -> AsyncIterator[BitbucketAttemptBundle]:
                    assert kwargs["expected_input"] == run
                    assert kwargs["claimed"] == claimed
                    yield BitbucketAttemptBundle(vcs, source, stage)

            monkeypatch.setattr(worker, "SqlAlchemyRunRepository", lambda *a, **kw: runs)
            monkeypatch.setattr(worker, "SqlAlchemyReviewPromptRepository", lambda *a: runs)
            monkeypatch.setattr(worker, "SqlAlchemyBlobCache", lambda *a: cache)
            monkeypatch.setattr(
                worker, "GenerateRepoConventions", lambda *a: RecordingConventions([])
            )
            monkeypatch.setattr(
                worker,
                "SqlAlchemyRunProcessingUnitOfWork",
                lambda *a: processing_uow(runs, cache)(),
            )
            monkeypatch.setattr(worker, "SqlAlchemyReviewOutputUnitOfWork", lambda *a: MemoryUow())
            monkeypatch.setattr(worker, "SqlAlchemyRunLifecycleUnitOfWork", lambda *a: MemoryUow())
            monkeypatch.setattr(worker, "TransactionalRunTrace", lambda *a: trace)
            process = worker.compose_worker_process(
                settings=worker.WorkerSettings.from_environment(
                    {
                        "DATABASE_URL": "postgresql+psycopg://localhost/test",
                        "RABBITMQ_URL": "amqp://localhost",
                        "PORTAL_URL": "https://review.example",
                    }
                ),
                session_factory=cast(async_sessionmaker[AsyncSession], object()),
                queue=cast(Any, queue),
                github=None,
                bitbucket=cast(Any, Resources()),
                gateway=gateway,
            )
            assert await process.handle_run._pipeline(claimed)
            assert len(queue.pointers) == 1
            pointer = queue.pointers[0]
            assert isinstance(pointer, ReviewPublishPointer)
            assert (pointer.run_id, pointer.head_sha, pointer.findings_hash) == (
                RUN_ID,
                HEAD,
                memory.findings_hash,
            )
            assert memory.state == "publishing"
            assert stage.head_sha == HEAD
            assert store.snapshot is not None
            assert store.snapshot.agents.text == agents_bytes.decode()
            assert store.snapshot.rules.text == rules_bytes.decode()
            prompt_text = "\n".join(
                message.content for request in answers.requests for message in request.messages
            )
            if technical_only:
                assert prompt_text == ""
                assert stage.payload is None
                assert answers.requests == []
                assert [tool for tool, _, _ in trace.steps if tool == "llm.review_output"] == []
                context_steps = [
                    response for tool, _, response in trace.steps if tool == "context.build"
                ]
                assert len(context_steps) == 1
                assert len(trace.payload_rows) == 1
                assert context_steps[0] == trace.payload_rows[0]
                assert trace.payload_rows[0]["status"] == "no_reviewable_text"
                assert trace.payload_rows[0]["coverage"]["status"] == "none"
                assert trace.payload_rows[0]["coverage"]["reason_counts"] == {"too_large": 1}
            else:
                assert agents_bytes.decode().strip() in prompt_text
                assert "Check changed function behavior." in prompt_text
                assert stage.payload is not None
                assert ContextLevel.L3 in stage.payload.levels_present
            await process.publish_review.execute(pointer)
            assert memory.state == "succeeded"
            assert memory.review_body in cast(dict[str, str], comments[0]["content"])["raw"]

    asyncio.run(scenario())
    assert any(request.url.path == f"{PREFIX}/diff/{HEAD}..{BASE}" for request in seen)
    assert any(request.url.path == f"{PREFIX}/src/{BASE}/" for request in seen)
    if not technical_only:
        assert any(request.url.path == f"{PREFIX}/src/{HEAD}/src/app.py" for request in seen)
    assert len(comments) == 1
    assert len(statuses) == 1 and statuses[0]["state"] == "SUCCESSFUL"
    assert statuses[0]["url"] == f"https://review.example/runs/{RUN_ID}"
    if technical_only:
        assert statuses[0]["description"] == "AI review: nothing reviewable"
        assert "**Coverage:** none" in cast(dict[str, str], comments[0]["content"])["raw"]
