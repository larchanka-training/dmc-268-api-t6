"""Review-result cache key, Redis adapter, verifier ids, and gateway hit/miss (#315).

The expected digest is the SHA-256 of a literal canonical JSON string. Message
text reaches the key only through ``rendered_messages_sha256``. The adapter
tests use an in-memory Redis double: a round trip, a fail-closed read, and a
value that is too large to store. The verifier-id test drives ``ExecuteReviewRun``
and reads the user message the verifier is given. The call-context test claims a
run and reads the ``RunCallContext`` the worker passes into the gateway. The
gateway tests drive ``LlmGateway`` with one in-memory result cache and assert
hits, misses, and fail-closed reads through the public generate result. The worker
composition test starts the process gateway and reads which Redis client it holds.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from types import SimpleNamespace, TracebackType
from typing import Any, Literal, NoReturn, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.modules.reviews.application.check_runs import RunProviderCheckpoint
from app.modules.reviews.application.conventions import (
    CachedConventions,
    ConventionsRequest,
    GeneratedConventions,
)
from app.modules.reviews.application.execute_review import (
    ExecuteReviewRun,
    ReviewPromptInput,
    ReviewReply,
)
from app.modules.reviews.application.get_run_file_lines import BLOB_CACHE_TTL
from app.modules.reviews.application.handle_review_run import (
    ClaimedAttempt,
    DeliveryOutcome,
    HandleReviewRun,
    RunGuardSnapshot,
)
from app.modules.reviews.application.llm import (
    LlmCallRecord,
    LlmUsage,
    ManifestEntry,
    RunCallContext,
)
from app.modules.reviews.application.policy_snapshot import (
    AGENTS_DOCUMENT_PATH,
    POLICY_SNAPSHOT_SCHEMA_VERSION,
    RULES_DOCUMENT_PATH,
    CallManifest,
    PolicySnapshot,
)
from app.modules.reviews.application.prompt_builder import (
    ChangedFile,
    DiffLine,
    PullRequestMeta,
    RepoConventions,
    ReviewContext,
    file_block_id,
)
from app.modules.reviews.application.reflection_pass import (
    CandidateFindingItem,
    ReflectionPass,
    VerifierPromptBuilder,
    VerifierReply,
)
from app.modules.reviews.application.review_output import EvidenceRef, ReviewFindingV2
from app.modules.reviews.application.review_result_cache import (
    ReviewResultIdentity,
    ReviewResultModel,
    ReviewResultOperation,
    review_result_cache_key,
)
from app.modules.reviews.infrastructure.llm.answers import InvalidAnswer
from app.modules.reviews.infrastructure.llm.gateway import (
    GatewayResult,
    LlmGateway,
    StructuredTask,
    rendered_messages_sha256,
)
from app.modules.reviews.infrastructure.llm.memory import InMemoryLlmCallTrace, InMemoryUsageLedger
from app.modules.reviews.infrastructure.llm.models import (
    GatewayConventionsModel,
    GatewayReviewModel,
    GatewayVerifierModel,
)
from app.modules.reviews.infrastructure.llm.settings import (
    GatewayPolicy,
    LlmSettings,
    ModelPrice,
    ModelProfile,
)
from app.modules.reviews.infrastructure.llm.transport import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ResponseSchema,
    TransportPaymentRequired,
)
from app.modules.reviews.infrastructure.redis_review_result_cache import (
    REVIEW_RESULT_CACHE_SOCKET_TIMEOUT_SECONDS,
    RedisReviewResultCache,
)
from app.worker import WorkerSettings, attempt_model_provider, run_worker
from tests.llm_fakes import FakeClock, context_checkpoint
from tests.policy_snapshot_fakes import (
    make_complete_execution_profile,
    make_complete_prompt_bindings,
    make_policy_document_missing,
    make_policy_document_present,
)

_REPO_A = UUID("11111111-1111-1111-1111-111111111111")
_REPO_B = UUID("22222222-2222-2222-2222-222222222222")
_MODEL = ReviewResultModel(provider="openai", model_id="gpt-test")
_RENDERED_LITERAL = "a" * 64

# Hand-written canonical JSON: sorted keys, compact separators, raw UTF-8.
_EXACT_CANONICAL_JSON = (
    '{"model":{"model_id":"gpt-test","provider":"openai"},'
    '"namespace":"review-result-v1","operation":"review",'
    '"output_schema_version":"review-output.schema.v2","profile_version":"profile-v1",'
    '"prompt_version":"ревью-v1",'
    '"rendered_input_digest":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
    '"repository_id":"11111111-1111-1111-1111-111111111111"}'
)


def _rendered(code: str, policy: str, rules: str) -> str:
    messages = (
        ChatMessage(role="system", content=rules),
        ChatMessage(role="user", content=policy),
        ChatMessage(role="user", content=code),
    )
    return rendered_messages_sha256(messages)


def _identity(
    rendered: str,
    *,
    repository_id: UUID = _REPO_A,
    operation: ReviewResultOperation = ReviewResultOperation.REVIEW,
    model: ReviewResultModel = _MODEL,
    prompt_version: str = "prompt-v1",
    output_schema_version: str = "review-output.schema.v2",
    profile_version: str = "profile-v1",
) -> ReviewResultIdentity:
    return ReviewResultIdentity(
        operation=operation,
        repository_id=repository_id,
        model=model,
        prompt_version=prompt_version,
        output_schema_version=output_schema_version,
        profile_version=profile_version,
        rendered_input_digest=rendered,
    )


def test_key_misses_when_code_policy_or_rules_text_changes() -> None:
    code = "def add(left, right):\n    return left + right\n"
    policy = "Do not allow secrets in the diff."
    rules = "Name functions with verbs."
    rendered = _rendered(code, policy, rules)
    base_key = review_result_cache_key(_identity(rendered))

    assert review_result_cache_key(_identity(rendered)) == base_key

    code_key = review_result_cache_key(_identity(_rendered(code + "\n", policy, rules)))
    policy_key = review_result_cache_key(_identity(_rendered(code, policy + " extra", rules)))
    rules_key = review_result_cache_key(_identity(_rendered(code, policy, rules + " extra")))
    assert code_key != base_key
    assert policy_key != base_key
    assert rules_key != base_key
    assert len({base_key, code_key, policy_key, rules_key}) == 4

    verification_key = review_result_cache_key(
        _identity(rendered, operation=ReviewResultOperation.VERIFICATION)
    )
    assert verification_key != base_key


def test_key_misses_when_model_prompt_schema_or_profile_changes() -> None:
    rendered = _rendered("def add():\n    return 1\n", "policy text", "rule text")
    base_key = review_result_cache_key(_identity(rendered))

    model_key = review_result_cache_key(
        _identity(rendered, model=ReviewResultModel(provider="openai", model_id="gpt-other"))
    )
    provider_key = review_result_cache_key(
        _identity(rendered, model=ReviewResultModel(provider="other", model_id="gpt-test"))
    )
    prompt_key = review_result_cache_key(_identity(rendered, prompt_version="prompt-v2"))
    schema_key = review_result_cache_key(
        _identity(rendered, output_schema_version="verifier-output.schema.v1")
    )
    profile_key = review_result_cache_key(_identity(rendered, profile_version="profile-v2"))

    assert model_key != base_key
    assert provider_key != base_key
    assert prompt_key != base_key
    assert schema_key != base_key
    assert profile_key != base_key
    assert len({base_key, model_key, provider_key, prompt_key, schema_key, profile_key}) == 6


def test_same_input_in_two_repositories_does_not_share_a_key() -> None:
    rendered = _rendered("def add():\n    return 1\n", "policy text", "rule text")
    left = review_result_cache_key(_identity(rendered, repository_id=_REPO_A))
    right = review_result_cache_key(_identity(rendered, repository_id=_REPO_B))

    assert left != right
    assert left.startswith("review-result:")
    assert right.startswith("review-result:")
    assert left.removeprefix("review-result:") != right.removeprefix("review-result:")

    exact = review_result_cache_key(
        _identity(
            _RENDERED_LITERAL,
            prompt_version="ревью-v1",
        )
    )
    expected = "review-result:" + sha256(_EXACT_CANONICAL_JSON.encode("utf-8")).hexdigest()
    assert exact == expected


# 256 KiB is the Redis blob ceiling in SYSTEM_DESIGN §10. One KiB is 1024 bytes.
_MAX_STORED_BYTES = 256 * 1024
_CUSTOMER_CODE = "customer-code-should-not-be-logged-315"


@dataclass
class _FakeRedis:
    """In-memory Redis double that records TTL and can fail or stall a command."""

    values: dict[str, bytes] = field(default_factory=dict)
    lifetimes: dict[str, int | None] = field(default_factory=dict)
    broken: bool = False
    hang: bool = False

    async def get(self, name: str) -> bytes | None:
        await self._before_command()
        return self.values.get(name)

    async def set(self, name: str, value: str, ex: int | None = None) -> object:
        await self._before_command()
        self.values[name] = value.encode("utf-8")
        self.lifetimes[name] = ex
        return True

    async def _before_command(self) -> None:
        if self.hang:
            await asyncio.sleep(30)
        if self.broken:
            raise ConnectionError("redis is down")


def test_entry_round_trips_with_blob_cache_ttl() -> None:
    redis = _FakeRedis()
    cache = RedisReviewResultCache(redis)
    key = "review-result:" + "ab" * 32
    stored = {
        "summary": "имя",
        "findings": [{"message": "unclear name", "line": 4, "blocking": True, "note": None}],
        "score": 1.5,
    }

    async def scenario() -> None:
        assert await cache.get(key) is None
        await cache.set(key, stored)
        assert await cache.get(key) == stored

    asyncio.run(scenario())
    assert set(redis.values) == {key}
    recorded = redis.lifetimes[key]
    assert isinstance(recorded, int)
    assert recorded == int(BLOB_CACHE_TTL.total_seconds())
    assert timedelta(seconds=recorded) <= BLOB_CACHE_TTL


def test_miss_unavailable_redis_and_corrupt_entry_return_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert REVIEW_RESULT_CACHE_SOCKET_TIMEOUT_SECONDS == 0.2
    redis = _FakeRedis()
    cache = RedisReviewResultCache(redis)
    key = "review-result:unavailable"
    logger = "app.modules.reviews.infrastructure.redis_review_result_cache"

    async def scenario() -> None:
        assert await cache.get("review-result:missing") is None

        redis.broken = True
        await cache.set(key, {"body": _CUSTOMER_CODE})
        assert await cache.get(key) is None
        assert key not in redis.values

        redis.broken = False
        redis.hang = True
        assert await asyncio.wait_for(cache.get(key), timeout=1) is None
        await asyncio.wait_for(cache.set(key, {"body": _CUSTOMER_CODE}), timeout=1)
        assert key not in redis.values

        redis.hang = False
        redis.values[key] = f"{{{_CUSTOMER_CODE}".encode()
        assert await cache.get(key) is None
        redis.values[key] = json.dumps([_CUSTOMER_CODE]).encode()
        assert await cache.get(key) is None
        redis.values[key] = json.dumps(_CUSTOMER_CODE).encode()
        assert await cache.get(key) is None
        redis.values[key] = b"null"
        assert await cache.get(key) is None
        redis.values[key] = b"x" * (_MAX_STORED_BYTES + 1)
        assert await cache.get(key) is None

    with caplog.at_level(logging.WARNING, logger=logger):
        asyncio.run(scenario())

    messages = [record.getMessage() for record in caplog.records if record.name == logger]
    assert any("get" in message and "ConnectionError" in message for message in messages)
    assert any("set" in message and "ConnectionError" in message for message in messages)
    assert any("get" in message and "TimeoutError" in message for message in messages)
    assert any("set" in message and "TimeoutError" in message for message in messages)
    assert any("get" in message and "JSONDecodeError" in message for message in messages)
    assert _CUSTOMER_CODE not in caplog.text


def test_oversize_value_is_not_stored() -> None:
    redis = _FakeRedis()
    cache = RedisReviewResultCache(redis)
    key = "review-result:oversize"
    body = "я" * _MAX_STORED_BYTES
    value = {"body": body}
    smallest_utf8 = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    assert len(body.encode("utf-8")) > _MAX_STORED_BYTES
    assert len(smallest_utf8) > _MAX_STORED_BYTES

    async def scenario() -> None:
        await cache.set(key, value)
        assert await cache.get(key) is None

    asyncio.run(scenario())
    assert redis.values == {}
    assert redis.lifetimes == {}


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


# ---------- gateway hit and miss ----------

_CACHE_NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
_CACHE_RUN = UUID("00000000-0000-4000-8000-000000000315")
_CACHE_WORKSPACE = UUID("00000000-0000-4000-8000-000000000316")
_PROMPT_A = UUID("00000000-0000-4000-8000-000000000317")
_PROMPT_B = UUID("00000000-0000-4000-8000-000000000318")
_REPO_C = UUID("33333333-3333-4333-8333-333333333333")
_MAIN = "app/main.py"
_BASE_MESSAGES = (
    ChatMessage("system", "rules"),
    ChatMessage("user", "policy"),
    ChatMessage("user", "code"),
)


def _only_numbered(content: str) -> dict[str, object]:
    """Accept ``{"n": <int>}`` and reject everything else as an invalid answer."""
    try:
        loaded = json.loads(content)
    except json.JSONDecodeError as error:
        raise InvalidAnswer([f"not valid JSON: {error.msg}"]) from None
    if not isinstance(loaded, dict) or isinstance(loaded.get("n"), bool):
        raise InvalidAnswer(["n must be an int"])
    if not isinstance(loaded.get("n"), int):
        raise InvalidAnswer(["n must be an int"])
    return cast(dict[str, object], loaded)


def _chat(request: ChatRequest, content: str) -> ChatResponse:
    return ChatResponse(
        raw={"choices": [{"message": {"content": content}}]},
        content=content,
        finish_reason="stop",
        model=request.profile.model,
        prompt_tokens=11,
        completion_tokens=7,
        cached_tokens=0,
        cost=None,
        cost_currency=None,
    )


class _QueueTransport:
    def __init__(self) -> None:
        self.answers: list[str | BaseException] = []
        self.requests: list[ChatRequest] = []

    async def complete(
        self, request: ChatRequest, *, checkpoint: RunProviderCheckpoint
    ) -> ChatResponse:
        await checkpoint()
        self.requests.append(request)
        if not self.answers:
            raise AssertionError("unexpected provider call")
        item = self.answers.pop(0)
        if isinstance(item, BaseException):
            raise item
        return _chat(request, item)


class _StickyTransport:
    """Every call returns the answer last assigned by the test."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.requests: list[ChatRequest] = []
        self.answer = "{}"

    async def complete(
        self, request: ChatRequest, *, checkpoint: RunProviderCheckpoint
    ) -> ChatResponse:
        await checkpoint()
        self.events.append("http")
        self.requests.append(request)
        return _chat(request, self.answer)


class _MemoryCache:
    def __init__(self, events: list[str] | None = None) -> None:
        self.values: dict[str, dict[str, Any]] = {}
        self.gets: list[str] = []
        self.sets: list[str] = []
        self.events = events

    async def get(self, key: str) -> dict[str, Any] | None:
        self.gets.append(key)
        if self.events is not None:
            self.events.append("get")
        return self.values.get(key)

    async def set(self, key: str, value: dict[str, Any]) -> None:
        self.sets.append(key)
        if self.events is not None:
            self.events.append("set")
        self.values[key] = value


class _RaisingCache(_MemoryCache):
    async def get(self, key: str) -> dict[str, Any] | None:
        self.gets.append(key)
        raise RuntimeError("redis down")

    async def set(self, key: str, value: dict[str, Any]) -> None:
        self.sets.append(key)
        raise RuntimeError("redis down")


class _Ledger(InMemoryUsageLedger):
    def __init__(self, events: list[str] | None = None) -> None:
        super().__init__()
        self.log = events
        self.blocked = False

    async def run_cost_usd(self, run_id: UUID) -> Decimal:
        if self.blocked:
            return Decimal("1000")
        return await super().run_cost_usd(run_id)

    async def record(self, context: RunCallContext, usage: LlmUsage) -> None:
        if self.log is not None:
            self.log.append("ledger")
        await super().record(context, usage)


class _Trace(InMemoryLlmCallTrace):
    def __init__(self, events: list[str] | None = None) -> None:
        super().__init__()
        self.log = events

    async def record_call(self, run_id: UUID, record: LlmCallRecord) -> None:
        if self.log is not None:
            self.log.append("llm.call")
        await super().record_call(run_id, record)

    async def record_result_cache_hit(
        self, run_id: UUID, operation: str, *, started_at: datetime
    ) -> None:
        if self.log is not None:
            self.log.append("llm.result_cache")
        await super().record_result_cache_hit(run_id, operation, started_at=started_at)


class _FailingHitTrace(InMemoryLlmCallTrace):
    async def record_result_cache_hit(
        self, run_id: UUID, operation: str, *, started_at: datetime
    ) -> None:
        raise RuntimeError("trace down")


class _ManifestLog:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.writes = 0

    async def record_manifest(self, run_id: UUID, entry: ManifestEntry) -> None:
        self.writes += 1
        self.events.append("manifest")


@dataclass(frozen=True)
class _Spec:
    messages: tuple[ChatMessage, ...] = _BASE_MESSAGES
    repository_id: UUID | None = None
    prompt_version_id: UUID | None = _PROMPT_A
    schema_name: str = "ReviewOutput"
    provider: str = "router"
    model: str = "review-model"
    context_window: int = 100_000
    max_output_tokens: int = 1_000
    chars_per_token: float = 3.0
    expensive: bool = False
    snapshot: PolicySnapshot | None = None
    operation: str = "review"
    route: Literal["review", "verifier"] = "review"
    verifier_model: str | None = None


@dataclass
class _Rig:
    cache: _MemoryCache = field(default_factory=_MemoryCache)
    transport: _QueueTransport = field(default_factory=_QueueTransport)
    ledger: _Ledger = field(default_factory=_Ledger)
    trace: _Trace = field(default_factory=_Trace)
    clock: FakeClock = field(default_factory=lambda: FakeClock(_CACHE_NOW))

    def gateway(
        self,
        spec: _Spec,
        *,
        policy: GatewayPolicy | None = None,
        fallback: ModelProfile | None = None,
        trace: InMemoryLlmCallTrace | None = None,
    ) -> LlmGateway:
        verifier = None if spec.verifier_model is None else _model(spec.verifier_model, spec)
        return LlmGateway(
            LlmSettings(
                primary=_model(spec.model, spec),
                fallback=fallback,
                verifier=verifier,
                policy=policy or GatewayPolicy(),
            ),
            self.transport,
            self.ledger,
            trace if trace is not None else self.trace,
            checkpoint=context_checkpoint,
            clock=self.clock,
            result_cache=self.cache,
        )


def _model(name: str, spec: _Spec) -> ModelProfile:
    price = (
        ModelPrice(Decimal("9"), Decimal("9"))
        if spec.expensive
        else ModelPrice(Decimal("1"), Decimal("1"))
    )
    return ModelProfile(
        provider=spec.provider,
        base_url="https://router.test/v1",
        model=name,
        context_window=spec.context_window,
        max_output_tokens=spec.max_output_tokens,
        price=price,
        chars_per_token=spec.chars_per_token,
        api_keys=("sk-test",),
    )


def _context(spec: _Spec, *, run_id: UUID = _CACHE_RUN) -> RunCallContext:
    return RunCallContext(
        run_id=run_id,
        workspace_id=_CACHE_WORKSPACE,
        attempt=1,
        engine="fast",
        deadline=_CACHE_NOW + timedelta(hours=1),
        prompt_version_id=spec.prompt_version_id,
        repository_id=spec.repository_id,
    )


def _task(spec: _Spec) -> StructuredTask:
    return StructuredTask(
        operation=spec.operation,
        messages=spec.messages,
        schema=ResponseSchema(spec.schema_name, {"type": "object"}),
        validate=_only_numbered,
        policy_snapshot=spec.snapshot,
        route=spec.route,
    )


def _cache_snapshot(
    repository_id: UUID,
    *,
    review_checksum: str = "review-checksum-v1",
    verifier_checksum: str = "verifier-checksum-v1",
    schema_version: str = "review-output/v2",
    temperature: str = "0.0",
) -> PolicySnapshot:
    """A snapshot whose key fields are literals. ``generate`` does not load prompt files."""
    return PolicySnapshot(
        schema_version=POLICY_SNAPSHOT_SCHEMA_VERSION,
        snapshot_id=uuid4(),
        run_id=_CACHE_RUN,
        repository_id=repository_id,
        policy_base_sha="b" * 40,
        rules=make_policy_document_present(RULES_DOCUMENT_PATH, "# Rules\nBe careful.\n"),
        agents=make_policy_document_missing(AGENTS_DOCUMENT_PATH),
        ignore_patterns=(),
        ignore_digest="0" * 64,
        parser_profile="rules-markdown/v1",
        parser_version="4.0.0",
        matcher_version="review-ignore/v1",
        limits_version="limits/v1",
        builtin_exclusions_version="builtin-exclusions/v1",
        priority_matcher_version="priority/v1",
        service_defaults_rule_version_id=UUID("00000000-0000-4000-8000-000000000505"),
        defaults_checksum="default-backend.v1",
        prompt_bindings=replace(
            make_complete_prompt_bindings(),
            review_system_checksum=review_checksum,
            verifier_checksum=verifier_checksum,
        ),
        execution_profile=replace(
            make_complete_execution_profile(),
            response_schema_version=schema_version,
            temperature=temperature,
        ),
        policy_digest="d" * 64,
    )


def _with_message(index: int, content: str) -> tuple[ChatMessage, ...]:
    messages = list(_BASE_MESSAGES)
    messages[index] = ChatMessage(messages[index].role, content)
    return tuple(messages)


async def _pair(left: _Spec, right: _Spec) -> tuple[GatewayResult, GatewayResult, _Rig]:
    rig = _Rig()
    rig.transport.answers.extend(['{"n": 1}', '{"n": 2}'])
    first = await rig.gateway(left).generate(_task(left), _context(left))
    second = await rig.gateway(right).generate(_task(right), _context(right))
    return first, second, rig


def _assert_hit(left: _Spec, right: _Spec) -> None:
    first, second, rig = asyncio.run(_pair(left, right))
    assert first.output == {"n": 1}
    assert second.output == {"n": 1}
    assert second.calls == 0
    assert second.usage == ()
    assert len(rig.transport.requests) == 1
    assert len(rig.ledger.events) == 1


def _assert_miss(left: _Spec, right: _Spec) -> None:
    first, second, rig = asyncio.run(_pair(left, right))
    assert first.output == {"n": 1}
    assert second.output == {"n": 2}
    assert len(rig.transport.requests) == 2
    assert len(rig.ledger.events) == 2
    assert [record.kind.value for _, record in rig.trace.records] == ["primary", "primary"]


def test_each_key_part_is_a_miss_and_a_normal_call() -> None:
    legacy = _Spec(repository_id=_REPO_A)
    _assert_hit(legacy, legacy)
    _assert_miss(legacy, replace(legacy, messages=_with_message(2, "other code")))
    _assert_miss(legacy, replace(legacy, messages=_with_message(1, "other policy")))
    _assert_miss(legacy, replace(legacy, messages=_with_message(0, "other rules")))
    _assert_miss(legacy, replace(legacy, model="other-model"))
    _assert_miss(legacy, replace(legacy, prompt_version_id=_PROMPT_B))
    _assert_miss(legacy, replace(legacy, schema_name="ReviewOutputV2"))
    _assert_miss(legacy, replace(legacy, chars_per_token=4.0))
    _assert_miss(legacy, replace(legacy, context_window=50_000))
    _assert_miss(legacy, replace(legacy, max_output_tokens=2_000))
    _assert_miss(legacy, replace(legacy, provider="other-provider"))
    _assert_miss(legacy, replace(legacy, repository_id=_REPO_B))
    # Price is not one of the profile fields that change the call.
    _assert_hit(legacy, replace(legacy, expensive=True))

    pinned = _Spec(repository_id=_REPO_C, snapshot=_cache_snapshot(_REPO_A))
    _assert_hit(pinned, replace(pinned, snapshot=_cache_snapshot(_REPO_A)))
    # A snapshot pins the stage checksum, not the run's prompt version id.
    _assert_hit(
        pinned, replace(pinned, prompt_version_id=_PROMPT_B, snapshot=_cache_snapshot(_REPO_A))
    )
    # The review schema version is the pinned profile field, not StructuredTask.schema.
    _assert_hit(
        pinned, replace(pinned, schema_name="IgnoredName", snapshot=_cache_snapshot(_REPO_A))
    )
    _assert_miss(pinned, replace(pinned, messages=_with_message(2, "other code")))
    _assert_miss(pinned, replace(pinned, model="other-model"))
    _assert_miss(
        pinned, replace(pinned, snapshot=_cache_snapshot(_REPO_A, review_checksum="other-review"))
    )
    _assert_miss(
        pinned,
        replace(pinned, snapshot=_cache_snapshot(_REPO_A, schema_version="review-output/v9")),
    )
    _assert_miss(pinned, replace(pinned, snapshot=_cache_snapshot(_REPO_A, temperature="0.2")))
    _assert_miss(pinned, replace(pinned, snapshot=_cache_snapshot(_REPO_B)))

    verifier = _Spec(
        repository_id=_REPO_A,
        snapshot=_cache_snapshot(_REPO_A),
        operation="verification",
        route="verifier",
        verifier_model="verifier-model",
    )
    _assert_hit(verifier, replace(verifier, snapshot=_cache_snapshot(_REPO_A)))
    _assert_hit(
        verifier, replace(verifier, schema_name="OtherSchema", snapshot=_cache_snapshot(_REPO_A))
    )
    # response_schema_version is inside the pinned profile digest, so the verifier misses
    # even though its output schema id stays the verifier document.
    _assert_miss(
        verifier,
        replace(verifier, snapshot=_cache_snapshot(_REPO_A, schema_version="review-output/v9")),
    )
    _assert_hit(verifier, replace(verifier, model="review-model-changed"))
    _assert_miss(verifier, replace(verifier, verifier_model="verifier-other"))
    _assert_miss(
        verifier,
        replace(verifier, snapshot=_cache_snapshot(_REPO_A, verifier_checksum="other-verifier")),
    )
    _assert_miss(verifier, replace(verifier, messages=_with_message(1, "other policy")))


def test_two_repositories_do_not_share_a_cached_answer() -> None:
    asyncio.run(_two_repositories())


async def _two_repositories() -> None:
    legacy = _Rig()
    legacy.transport.answers.extend(['{"n": 1}', '{"n": 2}', '{"n": 3}'])
    repo_a = _Spec(repository_id=_REPO_A)
    repo_b = _Spec(repository_id=_REPO_B)
    first = await legacy.gateway(repo_a).generate(_task(repo_a), _context(repo_a))
    second = await legacy.gateway(repo_b).generate(_task(repo_b), _context(repo_b))
    again = await legacy.gateway(repo_a).generate(_task(repo_a), _context(repo_a))
    assert first.output == {"n": 1}
    assert second.output == {"n": 2}
    assert again.output == {"n": 1}
    assert again.calls == 0
    assert len(legacy.transport.requests) == 2
    assert len(legacy.ledger.events) == 2

    # The snapshot repository id wins over the call context. A later legacy call
    # with only the context id must not read the snapshot entry.
    pinned = _Rig()
    pinned.transport.answers.extend(['{"n": 1}', '{"n": 2}'])
    stored = _Spec(repository_id=_REPO_C, snapshot=_cache_snapshot(_REPO_A))
    same_snapshot = _Spec(repository_id=_REPO_B, snapshot=_cache_snapshot(_REPO_A))
    context_only = _Spec(repository_id=_REPO_C)
    await pinned.gateway(stored).generate(_task(stored), _context(stored))
    hit = await pinned.gateway(same_snapshot).generate(
        _task(same_snapshot), _context(same_snapshot)
    )
    missed = await pinned.gateway(context_only).generate(
        _task(context_only), _context(context_only)
    )
    assert hit.output == {"n": 1}
    assert hit.calls == 0
    assert missed.output == {"n": 2}
    assert len(pinned.transport.requests) == 2
    assert len(pinned.ledger.events) == 2


def test_corrupt_entry_and_unavailable_cache_call_the_model(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    asyncio.run(_corrupt_and_unavailable())
    assert "llm.result_cache trace write failed" in caplog.text
    assert "redis down" not in caplog.text


async def _corrupt_and_unavailable() -> None:
    spec = _Spec(repository_id=_REPO_A)
    planted = _Rig()
    planted.transport.answers.extend(['{"n": 1}', '{"n": 2}', '{"n": 3}'])
    await planted.gateway(spec).generate(_task(spec), _context(spec))
    if planted.cache.values:
        planted.cache.values[next(iter(planted.cache.values))] = {"n": "corrupt"}
    rejected = await planted.gateway(spec).generate(_task(spec), _context(spec))
    assert rejected.output == {"n": 2}
    replaced = await planted.gateway(spec).generate(_task(spec), _context(spec))
    assert replaced.output == {"n": 2}
    assert replaced.calls == 0
    assert len(planted.transport.requests) == 2
    assert len(planted.ledger.events) == 2

    down = _Rig(cache=_RaisingCache())
    down.transport.answers.append('{"n": 1}')
    answered = await down.gateway(spec).generate(_task(spec), _context(spec))
    assert answered.output == {"n": 1}
    assert len(down.cache.gets) == 1
    assert len(down.cache.sets) == 1
    assert len(down.transport.requests) == 1
    assert len(down.ledger.events) == 1

    bare = _Spec(repository_id=None)
    skipped = _Rig()
    skipped.transport.answers.extend(['{"n": 1}', '{"n": 2}'])
    await skipped.gateway(bare).generate(_task(bare), _context(bare))
    await skipped.gateway(bare).generate(_task(bare), _context(bare))
    assert skipped.cache.gets == []
    assert skipped.cache.sets == []
    assert len(skipped.transport.requests) == 2

    traced = _Rig()
    traced.transport.answers.extend(['{"n": 1}', '{"n": 1}'])
    await traced.gateway(spec).generate(_task(spec), _context(spec))
    failing = _FailingHitTrace()
    hit = await traced.gateway(spec, trace=failing).generate(_task(spec), _context(spec))
    assert hit.output == {"n": 1}
    assert hit.calls == 0
    assert len(traced.transport.requests) == 1
    assert len(traced.ledger.events) == 1


def test_repair_is_stored_for_the_original_messages_and_fallback_is_not() -> None:
    asyncio.run(_repair_and_fallback())


async def _repair_and_fallback() -> None:
    spec = _Spec(repository_id=_REPO_A)
    repaired = _Rig()
    repaired.transport.answers.extend(["not-json", '{"n": 1}', '{"n": 2}'])
    gateway = repaired.gateway(spec)
    first = await gateway.generate(_task(spec), _context(spec))
    second = await gateway.generate(_task(spec), _context(spec))
    assert first.output == {"n": 1}
    assert second.output == {"n": 1}
    assert second.calls == 0
    assert len(repaired.transport.requests) == 2
    assert len(repaired.ledger.events) == 2
    assert [record.kind.value for _, record in repaired.trace.records] == ["primary", "repair"]

    fallback_model = _model("fallback-model", spec)

    class _Split(_QueueTransport):
        async def complete(
            self, request: ChatRequest, *, checkpoint: RunProviderCheckpoint
        ) -> ChatResponse:
            await checkpoint()
            self.requests.append(request)
            if request.profile.model == "review-model":
                raise TransportPaymentRequired("no credit")
            return _chat(request, '{"n": 1}')

    split = _Split()
    rig = _Rig(transport=split)
    primary = rig.gateway(spec, fallback=fallback_model)
    await primary.generate(_task(spec), _context(spec))
    await primary.generate(_task(spec), _context(spec))
    assert len(split.requests) == 4
    assert [request.profile.model for request in split.requests] == [
        "review-model",
        "fallback-model",
        "review-model",
        "fallback-model",
    ]
    assert len(rig.cache.values) == 0


class _NoMeta:
    async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta | None:
        return None


def _ready_snapshot(repository_id: UUID) -> PolicySnapshot:
    """A snapshot the review and verifier adapters accept, including the pinned verifier."""
    return PolicySnapshot(
        schema_version=POLICY_SNAPSHOT_SCHEMA_VERSION,
        snapshot_id=UUID("00000000-0000-4000-8000-000000000503"),
        run_id=_CACHE_RUN,
        repository_id=repository_id,
        policy_base_sha="b" * 40,
        rules=make_policy_document_present(
            RULES_DOCUMENT_PATH, "# Rules\nAlways sanitize input.\n"
        ),
        agents=make_policy_document_missing(AGENTS_DOCUMENT_PATH),
        ignore_patterns=(),
        ignore_digest="0" * 64,
        parser_profile="rules-markdown/v1",
        parser_version="4.0.0",
        matcher_version="review-ignore/v1",
        limits_version="limits/v1",
        builtin_exclusions_version="builtin-exclusions/v1",
        priority_matcher_version="priority/v1",
        service_defaults_rule_version_id=UUID("00000000-0000-4000-8000-000000000505"),
        defaults_checksum="default-backend.v1",
        prompt_bindings=replace(make_complete_prompt_bindings(), review_system_version="v4"),
        execution_profile=make_complete_execution_profile(),
        policy_digest="d" * 64,
    )


def _review_context(snapshot: PolicySnapshot) -> ReviewContext:
    return ReviewContext(
        system="Review carefully.",
        rules=(),
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=PullRequestMeta(
            "PR", None, "u", "b", "main", (), 1, 1, 0, False, False, head_sha="head_sha_123"
        ),
        changed_files=(ChangedFile(_MAIN, "modified", (DiffLine(1, "added", "x = 1"),)),),
        omitted_files=(),
        policy_snapshot=snapshot,
    )


def _review_answer() -> str:
    body = {
        "findings": [
            {
                "path": _MAIN,
                "line": 1,
                "start_line": None,
                "severity": "critical",
                "category": "security",
                "title": "An issue",
                "body": "Issue on the line.",
                "suggestion": None,
                "confidence": 0.95,
                "rule_name": None,
                "evidence_refs": [
                    {"block_id": file_block_id(_MAIN), "start_line": 1, "end_line": 1}
                ],
            }
        ],
        "summary": {
            "problem": "One issue was found.",
            "done_well": "The change is small.",
            "effort": "none",
        },
    }
    return json.dumps(body)


def _candidate(candidate_id: str) -> CandidateFindingItem:
    return CandidateFindingItem(
        candidate_id=candidate_id,
        finding=ReviewFindingV2(
            path=_MAIN,
            line=1,
            start_line=None,
            severity="high",
            category="security",
            title="Unchecked value",
            body="The new assignment is unchecked.",
            suggestion=None,
            confidence=0.9,
            rule_name=".review/rules.md",
            evidence_refs=[EvidenceRef(block_id=file_block_id(_MAIN), start_line=1, end_line=1)],
        ),
    )


def _verifier_answer(candidate_id: str) -> str:
    return json.dumps(
        {
            "decisions": [
                {
                    "candidate_id": candidate_id,
                    "status": "supported",
                    "reason": "The assignment is visible on line 1.",
                    "evidence_refs": [
                        {"block_id": file_block_id(_MAIN), "start_line": 1, "end_line": 1}
                    ],
                    "requirement_refs": [],
                }
            ]
        }
    )


def _conventions_answer() -> str:
    return json.dumps(
        {
            "files": [{"path": "app/service.py", "relevance": "changed service"}],
            "key_patterns": ["a", "b", "c"],
            "recommendations": [
                f"Check {index} (from: standard/correctness)" for index in range(5)
            ],
        }
    )


def test_rerun_of_review_and_verifier_writes_no_llm_call_and_no_usage() -> None:
    asyncio.run(_rerun_review_and_verifier())


async def _rerun_review_and_verifier() -> None:
    events: list[str] = []
    transport = _StickyTransport(events)
    cache = _MemoryCache(events)
    ledger = _Ledger(events)
    trace = _Trace(events)
    manifests = _ManifestLog(events)
    clock = FakeClock(_CACHE_NOW)
    gateway = LlmGateway(
        LlmSettings(primary=_model("review-model", _Spec()), fallback=None),
        transport,
        ledger,
        trace,
        checkpoint=context_checkpoint,
        clock=clock,
        manifest_store=manifests,
        result_cache=cache,
    )
    run = RunCallContext(
        run_id=_CACHE_RUN,
        workspace_id=_CACHE_WORKSPACE,
        attempt=1,
        engine="fast",
        deadline=_CACHE_NOW + timedelta(hours=1),
        prompt_version_id=_PROMPT_A,
        repository_id=_REPO_A,
    )
    snapshot = _ready_snapshot(_REPO_A)
    context = _review_context(snapshot)
    review = GatewayReviewModel(gateway, run, _NoMeta())
    transport.answer = _review_answer()
    first_review = await review.draft_review(context=context)
    second_review = await review.draft_review(context=context)
    assert isinstance(first_review, ReviewReply)
    assert isinstance(second_review, ReviewReply)
    assert dict(second_review.output)["findings"] == dict(first_review.output)["findings"]
    assert second_review.manifest.call_id != first_review.manifest.call_id
    assert review.last_result is not None
    assert review.last_result.calls == 0
    assert review.last_result.usage == ()
    assert review.last_result.accepted_manifest == second_review.manifest
    assert review.last_result.accepted_manifest_id is None
    assert len(transport.requests) == 1
    assert len(ledger.events) == 1
    assert manifests.writes == 1
    assert events == [
        "get",
        "manifest",
        "http",
        "ledger",
        "llm.call",
        "set",
        "get",
        "llm.result_cache",
    ]

    builder = VerifierPromptBuilder()
    prompt_a, manifest_a = builder.build_verifier_prompt(
        candidates=(_candidate("c-1"),), context=context, call_id=uuid4()
    )
    prompt_b, manifest_b = builder.build_verifier_prompt(
        candidates=(_candidate("c-1"),), context=context, call_id=uuid4()
    )
    ids_b = re.findall(r'candidate_id="([^"]+)"', prompt_b.user)
    assert ids_b
    assert re.findall(r'candidate_id="([^"]+)"', prompt_a.user) == ids_b
    transport.answer = _verifier_answer(ids_b[0])
    verifier = GatewayVerifierModel(gateway, run)
    first_pass = await verifier.verify(
        system_prompt=prompt_a.system,
        user_prompt=prompt_a.user,
        manifest=manifest_a,
        context=context,
    )
    second_pass = await verifier.verify(
        system_prompt=prompt_b.system,
        user_prompt=prompt_b.user,
        manifest=manifest_b,
        context=context,
    )
    assert first_pass.output == second_pass.output
    assert isinstance(second_pass.output, dict)
    decisions = cast(list[dict[str, Any]], second_pass.output["decisions"])
    assert [item["candidate_id"] for item in decisions] == ids_b
    assert second_pass.accepted_call_id == manifest_b.call_id
    assert second_pass.manifest_id is None
    assert len(transport.requests) == 2
    assert len(ledger.events) == 2
    assert manifests.writes == 2
    assert len(trace.records) == 2
    assert [action[1] for action in trace.actions] == ["llm.result_cache", "llm.result_cache"]
    assert trace.actions[0][2:] == ({"operation": "review", "cache": "hit"}, {})
    assert trace.actions[1][2:] == ({"operation": "verification", "cache": "hit"}, {})

    ledger.blocked = True
    third_review = await review.draft_review(context=context)
    ledger.blocked = False
    assert isinstance(third_review, ReviewReply)
    assert dict(third_review.output)["findings"] == dict(first_review.output)["findings"]
    assert len(transport.requests) == 2
    assert len(ledger.events) == 2

    gets_before = len(cache.gets)
    sets_before = len(cache.sets)
    calls_before = len(transport.requests)
    transport.answer = _conventions_answer()
    conventions_run = replace(run, run_id=UUID("00000000-0000-4000-8000-000000000319"))
    conventions = GatewayConventionsModel(gateway, conventions_run)
    request = ConventionsRequest("P", (), None, ("app/service.py",), (), {}, ("app/service.py",))
    await conventions.draft_conventions(request=request)
    await conventions.draft_conventions(request=request)
    assert len(cache.gets) == gets_before
    assert len(cache.sets) == sets_before
    assert len(transport.requests) == calls_before + 2
    assert len(ledger.events) == 4

    tight_spec = _Spec(repository_id=_REPO_A)
    tight = _Rig()
    tight.transport.answers.append('{"n": 1}')
    tight_gateway = tight.gateway(tight_spec, policy=GatewayPolicy(max_calls_per_attempt=1))
    await tight_gateway.generate(_task(tight_spec), _context(tight_spec))
    tight.ledger.blocked = True
    cached = await tight_gateway.generate(_task(tight_spec), _context(tight_spec))
    assert cached.output == {"n": 1}
    assert cached.calls == 0
    assert cached.usage == ()
    assert len(tight.transport.requests) == 1
    assert len(tight.ledger.events) == 1


_WORKER_REDIS_URL = "redis://cache.example.test:6379/4"
_WORKER_BASE_ENV = {
    "DATABASE_URL": "postgresql+psycopg://unused/unused",
    "RABBITMQ_URL": "amqp://unused/unused",
    "LLM_MODEL": "test-model",
    "LLM_BASE_URL": "https://llm.test/v1",
    "LLM_API_KEYS": "test-key",
    "LLM_CONTEXT_WINDOW": "100000",
    "LLM_MAX_OUTPUT_TOKENS": "8000",
    "LLM_PRICE_INPUT_PER_MTOK": "1",
    "LLM_PRICE_OUTPUT_PER_MTOK": "2",
}
_RESULT_CACHE_SOCKET_OPTIONS = {
    "socket_connect_timeout": 0.2,
    "socket_timeout": 0.2,
}


class _StopWorkerStartup(Exception):
    """End process startup once the composed gateway is visible."""


class _GatewayResultCacheCapture:
    """Record ``LlmGateway``'s public ``result_cache`` and build the real gateway."""

    def __init__(self) -> None:
        self.caches: list[object] = []

    def __call__(self, *args: Any, **kwargs: Any) -> LlmGateway:
        self.caches.append(kwargs.get("result_cache"))
        return LlmGateway(*args, **kwargs)


class _WorkerRedisClient:
    def __init__(self) -> None:
        self.closed = False
        self.read_keys: list[str] = []

    async def aclose(self) -> None:
        self.closed = True

    async def get(self, name: str) -> None:
        self.read_keys.append(name)

    async def set(self, name: str, value: str, ex: int | None = None) -> None:
        raise AssertionError((name, value, ex))


class _WorkerRedisDouble:
    """Stand-in for ``Redis.from_url``: one client per call, options recorded."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.clients: list[_WorkerRedisClient] = []

    def from_url(self, url: str, **options: object) -> _WorkerRedisClient:
        client = _WorkerRedisClient()
        self.calls.append((url, dict(options)))
        self.clients.append(client)
        return client


def _start_worker_gateway(
    monkeypatch: pytest.MonkeyPatch, redis_url: str | None
) -> tuple[object, _WorkerRedisDouble]:
    env = dict(_WORKER_BASE_ENV)
    if redis_url is not None:
        env["REDIS_URL"] = redis_url
    settings = WorkerSettings.from_environment(env)
    redis = _WorkerRedisDouble()
    engine = _WorkerRedisClient()
    constructed = _GatewayResultCacheCapture()
    seen: list[object] = []

    def create_engine(*args: object, **kwargs: object) -> AsyncEngine:
        return cast(AsyncEngine, SimpleNamespace(dispose=engine.aclose))

    @asynccontextmanager
    async def channels(*args: object, **kwargs: object) -> AsyncIterator[SimpleNamespace]:
        yield SimpleNamespace(connection=object(), publisher=object())

    @asynccontextmanager
    async def index(*args: object, **kwargs: object) -> AsyncIterator[None]:
        yield None

    def stop_at_composition(**kwargs: object) -> NoReturn:
        seen.append(kwargs["gateway"])
        raise _StopWorkerStartup

    monkeypatch.setattr("app.bootstrap.llm_gateway.LlmGateway", constructed)
    monkeypatch.setattr("app.worker.create_async_engine", create_engine)
    monkeypatch.setattr("app.worker.amqp_channels", channels)
    monkeypatch.setattr("app.worker.index_consumer", index)
    monkeypatch.setattr("app.worker.compose_worker_process", stop_at_composition)
    monkeypatch.setattr("app.worker.Redis", redis)
    with pytest.raises(_StopWorkerStartup):
        asyncio.run(run_worker(settings))

    assert engine.closed
    assert len(seen) == 1
    assert isinstance(seen[0], LlmGateway)
    assert len(constructed.caches) == 1
    return constructed.caches[0], redis


def _clients_for(
    redis: _WorkerRedisDouble, options: Mapping[str, object]
) -> list[_WorkerRedisClient]:
    return [
        client
        for (_, call_options), client in zip(redis.calls, redis.clients, strict=True)
        if call_options == options
    ]


def test_worker_gateway_uses_result_cache_only_when_redis_url_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache, redis = _start_worker_gateway(monkeypatch, _WORKER_REDIS_URL)
    assert isinstance(cache, RedisReviewResultCache)
    cache_clients = _clients_for(redis, _RESULT_CACHE_SOCKET_OPTIONS)
    shared_clients = _clients_for(redis, {})
    assert len(redis.calls) == 2
    assert all(url == _WORKER_REDIS_URL for url, _options in redis.calls)
    assert len(cache_clients) == 1
    assert len(shared_clients) == 1
    assert asyncio.run(cache.get("review-result:probe")) is None
    assert cache_clients[0].read_keys == ["review-result:probe"]
    assert shared_clients[0].read_keys == []
    assert cache_clients[0].closed
    assert shared_clients[0].closed

    for absent in (None, ""):
        idle_cache, idle_redis = _start_worker_gateway(monkeypatch, absent)
        assert idle_redis.calls == []
        assert idle_cache is None
