"""Verifier IDs and claimed call context in the review-result pipeline (#315)."""

from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from types import TracebackType
from typing import Any, cast
from uuid import UUID

from app.modules.reviews.application.conventions import (
    CachedConventions,
    GeneratedConventions,
)
from app.modules.reviews.application.execute_review import (
    ExecuteReviewRun,
    ReviewPromptInput,
)
from app.modules.reviews.application.handle_review_run import (
    ClaimedAttempt,
    DeliveryOutcome,
    HandleReviewRun,
    RunGuardSnapshot,
)
from app.modules.reviews.application.llm import (
    RunCallContext,
)
from app.modules.reviews.application.policy_snapshot import (
    POLICY_SNAPSHOT_SCHEMA_VERSION,
    CallManifest,
    PolicySnapshot,
)
from app.modules.reviews.application.prompt_builder import (
    ChangedFile,
    DiffLine,
    PullRequestMeta,
    RepoConventions,
    ReviewContext,
)
from app.modules.reviews.application.reflection_pass import (
    ReflectionPass,
    VerifierReply,
)
from app.modules.reviews.infrastructure.llm.gateway import (
    GatewayResult,
    LlmGateway,
)
from app.worker import attempt_model_provider
from tests.policy_snapshot_fakes import (
    make_complete_execution_profile,
    make_complete_prompt_bindings,
    make_policy_document_missing,
    make_policy_document_present,
)
from tests.review_result_cache_fakes import _REPO_A

_RUN_ID = UUID("00000000-0000-0000-0000-000000031503")
_BLOCK_ID = "file-app-main-py-990f7e41"
_STABLE_TITLE = "Unclear name"
# Hand-written canonical JSON of {"index": 0, "finding": <ReviewFindingV2 dump>}.
_INDEX_ZERO_CANONICAL_JSON = (
    '{"finding":{"body":"Имя функции неясно.","category":"security","confidence":0.9,'
    '"evidence_refs":[{"block_id":"file-app-main-py-990f7e41","end_line":10,"start_line":10}],'
    '"line":10,"path":"app/main.py","rule_name":null,"severity":"critical",'
    '"start_line":null,"suggestion":null,"title":"Unclear name"},"index":0}'
)
_CANDIDATE_RE = re.compile(r'<candidate candidate_id="([^"]+)"[^>]*>\s*<title>([^<]*)</title>')


def _finding(*, line: int, title: str) -> dict[str, object]:
    return {
        "path": "app/main.py",
        "line": line,
        "start_line": None,
        "severity": "critical",
        "category": "security",
        "title": title,
        "body": "Имя функции неясно.",
        "suggestion": None,
        "confidence": 0.9,
        "rule_name": None,
        "evidence_refs": [{"block_id": _BLOCK_ID, "start_line": line, "end_line": line}],
    }


def _snapshot() -> PolicySnapshot:
    profile = make_complete_execution_profile(
        "fast", review_model="model-a", verifier_model="model-b"
    )
    return PolicySnapshot(
        schema_version=POLICY_SNAPSHOT_SCHEMA_VERSION,
        snapshot_id=UUID("00000000-0000-0000-0000-000000031504"),
        run_id=_RUN_ID,
        repository_id=_REPO_A,
        policy_base_sha="b" * 40,
        rules=make_policy_document_present(
            path=".review/rules.md", text="# Rules\nAlways sanitize input.\n", blob_oid="r" * 40
        ),
        agents=make_policy_document_missing("AGENTS.md"),
        ignore_patterns=(),
        ignore_digest="0" * 64,
        parser_profile="rules-markdown/v1",
        parser_version="4.0.0",
        matcher_version="review-ignore/v1",
        limits_version="policy-limits/v1",
        builtin_exclusions_version="builtin-exclusions/v1",
        priority_matcher_version="priority-matcher/v1",
        service_defaults_rule_version_id=UUID("00000000-0000-0000-0000-000000031505"),
        defaults_checksum="282d61c2da897152046db742fbd9bfda061ba1c71ce32759d47e042c06b0ff52",
        prompt_bindings=make_complete_prompt_bindings(),
        execution_profile=profile,
        policy_digest="3" * 64,
    )


class _Processor:
    async def prepare(self, run_id: UUID) -> GeneratedConventions:
        return GeneratedConventions(
            CachedConventions(
                repository_id=run_id,
                agents_md_sha=None,
                prompt_version_id=run_id,
                key_patterns=(),
                recommendations=(),
                languages={},
            ),
            None,
            True,
        )


class _Repository:
    async def get_review_prompt_input(
        self, run_id: UUID, saved: GeneratedConventions
    ) -> ReviewPromptInput:
        return ReviewPromptInput(
            system="Review carefully.",
            rules=(),
            agents_md=None,
            conventions=RepoConventions((), ()),
            changed_files=(
                ChangedFile(
                    "app/main.py",
                    "modified",
                    (DiffLine(10, "added", "x = 1"), DiffLine(11, "added", "y = 2")),
                ),
            ),
            omitted_files=(),
            policy_snapshot=_snapshot(),
        )


class _Model:
    def __init__(self, findings: list[dict[str, object]]) -> None:
        self._findings = findings

    async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
        return PullRequestMeta("Title", None, "u", "b", "m", (), 1, 1, 0, False, False)

    async def draft_review(self, *, context: ReviewContext) -> dict[str, object]:
        return {"findings": self._findings, "summary": {"problem": "A bug.", "effort": "small"}}


class _Publisher:
    async def execute(self, run_id: UUID, raw_output: object) -> bool:
        return True


class _CapturingVerifier:
    def __init__(self) -> None:
        self.user_prompt = ""

    async def verify(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        manifest: CallManifest,
        context: ReviewContext,
        check: object | None = None,
    ) -> VerifierReply:
        self.user_prompt = user_prompt
        decisions = [
            {
                "candidate_id": candidate_id,
                "status": "supported",
                "reason": "Visible in the diff.",
                "evidence_refs": [{"block_id": _BLOCK_ID, "start_line": 10, "end_line": 10}],
                "requirement_refs": [],
            }
            for candidate_id in re.findall(r'candidate candidate_id="([^"]+)"', user_prompt)
        ]
        return VerifierReply(output={"decisions": decisions})


def _verifier_user(findings: list[dict[str, object]]) -> str:
    verifier = _CapturingVerifier()
    pipeline = ExecuteReviewRun(
        _Processor(),
        _Repository(),
        _Model(findings),
        _Publisher(),
        reflection_pass=ReflectionPass(verifier),
    )
    assert asyncio.run(pipeline.execute(_RUN_ID)) is True
    assert verifier.user_prompt != ""
    return verifier.user_prompt


def _ids_by_title(user: str) -> dict[str, str]:
    return {title: candidate_id for candidate_id, title in _CANDIDATE_RE.findall(user)}


def test_verifier_candidate_id_is_stable_for_the_same_finding() -> None:
    base = _finding(line=10, title=_STABLE_TITLE)
    first = _verifier_user([base])
    second = _verifier_user([base])

    assert first == second
    stable_id = _ids_by_title(first)[_STABLE_TITLE]
    assert stable_id == _ids_by_title(second)[_STABLE_TITLE]
    assert len(stable_id) == 64
    assert stable_id == sha256(_INDEX_ZERO_CANONICAL_JSON.encode("utf-8")).hexdigest()

    changed = _finding(line=10, title="Unclear names")
    changed_id = _ids_by_title(_verifier_user([changed]))["Unclear names"]
    assert changed_id != stable_id
    assert len(changed_id) == 64

    shifted = _verifier_user([_finding(line=11, title="Other issue"), base])
    shifted_id = _ids_by_title(shifted)[_STABLE_TITLE]
    assert shifted_id != stable_id
    assert len(shifted_id) == 64


_CALL_RUN = UUID("55555555-5555-4555-8555-555555555555")
_CALL_WORKSPACE = UUID("44444444-4444-4444-8444-444444444444")
_CALL_PROMPT = UUID("66666666-6666-4666-8666-666666666666")
_CALL_RULE = UUID("77777777-7777-4777-8777-777777777777")
_CALL_NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
_CALL_HEAD = "a" * 40


class _ClaimStore:
    """In-memory claim store: the guard snapshot is the only state the handler reads."""

    def __init__(self, snapshot: RunGuardSnapshot) -> None:
        self.snapshot = snapshot

    async def lock_for_claim(self, run_id: UUID) -> RunGuardSnapshot:
        assert run_id == self.snapshot.run_id
        return self.snapshot

    async def claim(
        self, run_id: UUID, *, worker_id: str, now: datetime, lease_until: datetime
    ) -> int:
        assert run_id == self.snapshot.run_id
        assert worker_id == "worker-315"
        return 1

    async def finish(
        self,
        run_id: UUID,
        *,
        from_state: str,
        worker_id: str | None,
        state: str,
        error_code: str | None,
        error_message: str | None,
        now: datetime,
    ) -> bool:
        raise AssertionError((run_id, from_state, worker_id, state, error_code, error_message, now))

    async def extend_lease(self, run_id: UUID, worker_id: str, lease_until: datetime) -> bool:
        raise AssertionError((run_id, worker_id, lease_until))

    async def requeue(self, run_id: UUID, *, worker_id: str | None, available_at: datetime) -> bool:
        raise AssertionError((run_id, worker_id, available_at))

    async def cancellation_reason(self, run_id: UUID) -> str | None:
        raise AssertionError(run_id)

    async def run_message(self, run_id: UUID) -> None:
        raise AssertionError(run_id)

    async def provider_origin_active(self, run_id: UUID) -> bool:
        raise AssertionError(run_id)

    async def check_run_report(self, run_id: UUID) -> None:
        raise AssertionError(run_id)


class _ClaimUnit:
    def __init__(self, store: _ClaimStore) -> None:
        self.runs = store

    async def __aenter__(self) -> _ClaimUnit:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class _NoRetry:
    async def publish_retry(self, message: object, delay_key: str) -> None:
        raise AssertionError(delay_key)


class _TokenCounter:
    def count(self, text: str) -> int:
        return max(1, len(text) // 4)

    def count_length(self, characters: int) -> int:
        return max(1, characters // 4)


class _RecordingGateway:
    """Records the call context ``draft_review`` hands to ``generate``."""

    def __init__(self) -> None:
        self.contexts: list[RunCallContext] = []

    def token_counter(self, profile: object = None) -> _TokenCounter:
        return _TokenCounter()

    def max_prompt_tokens(self, engine: str, route: str = "review") -> int:
        return 100_000

    async def generate(self, task: object, context: RunCallContext) -> GatewayResult:
        self.contexts.append(context)
        return GatewayResult(
            output={"summary": "ok", "findings": []},
            provider="openai",
            model="gpt-test",
            usage=(),
            calls=0,
        )


def _guard_snapshot(
    *, include_repository: bool, repository_id: UUID | None = None
) -> RunGuardSnapshot:
    snapshot = RunGuardSnapshot(
        run_id=_CALL_RUN,
        workspace_id=_CALL_WORKSPACE,
        state="queued",
        attempt=0,
        available_at=_CALL_NOW,
        cancel_requested=False,
        head_sha=_CALL_HEAD,
        pr_head_sha=_CALL_HEAD,
        pr_open=True,
        repository_enabled=True,
        daily_budget_usd=Decimal("0"),
        spent_today_usd=Decimal("0"),
        engine="fast",
        prompt_version_id=_CALL_PROMPT,
        rule_version_id=_CALL_RULE,
    )
    if include_repository:
        return replace(snapshot, repository_id=repository_id)
    return snapshot


def _legacy_review_context() -> ReviewContext:
    return ReviewContext(
        system="Review the diff.",
        rules=(),
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=PullRequestMeta(
            "Title", None, "author", "feature", "main", (), 1, 1, 0, False, False
        ),
        changed_files=(
            ChangedFile("app/main.py", "modified", (DiffLine(1, "added", "return 1"),)),
        ),
        omitted_files=(),
    )


async def _context_from_claim(
    *, include_repository: bool, repository_id: UUID | None = None
) -> RunCallContext:
    gateway = _RecordingGateway()
    snapshot = _guard_snapshot(repository_id=repository_id, include_repository=include_repository)

    async def pipeline(claimed: ClaimedAttempt) -> bool:
        provider = attempt_model_provider(
            cast(LlmGateway, gateway),
            cast(Any, object()),
            claimed,
        )
        await provider.draft_review(context=_legacy_review_context())
        return True

    outcome = await HandleReviewRun(
        uow_factory=lambda: _ClaimUnit(_ClaimStore(snapshot)),
        pipeline=pipeline,
        retry_queue=_NoRetry(),
        check_runs=None,
        worker_id="worker-315",
        now=lambda: _CALL_NOW,
    ).execute(_CALL_RUN)
    assert outcome is DeliveryOutcome.ACK
    assert len(gateway.contexts) == 1
    return gateway.contexts[0]


def test_call_context_receives_repository_id_from_the_claimed_attempt() -> None:
    repository_id = UUID("33333333-3333-4333-8333-333333333333")

    async def scenario() -> tuple[RunCallContext, RunCallContext]:
        carried = await _context_from_claim(include_repository=True, repository_id=repository_id)
        omitted = await _context_from_claim(include_repository=False)
        return carried, omitted

    carried, omitted = asyncio.run(scenario())
    assert carried.repository_id == repository_id
    assert omitted.repository_id is None

    bare = RunCallContext(
        run_id=_CALL_RUN,
        workspace_id=_CALL_WORKSPACE,
        attempt=1,
        engine="fast",
        deadline=_CALL_NOW + timedelta(minutes=8),
    )
    assert bare.repository_id is None
