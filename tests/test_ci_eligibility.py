"""Current-head CI eligibility before the Task 5 Run insertion boundary."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import httpx
import pytest

from app.modules.reviews.application.determine_ci_eligibility import (
    CheckSuite,
    CiEligibility,
    CiSnapshot,
    CiWaitMode,
    DetermineCiEligibility,
    EligibilityCandidate,
    EligibilityReason,
)
from app.modules.reviews.application.project_github_pull_request import PullRequestState
from app.modules.reviews.infrastructure.github_ci import HttpGitHubCurrentHeadCiProvider

_PR_ID = UUID("11111111-1111-1111-1111-111111111111")
_AT = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
_HEAD = "a" * 40


def _candidate(**overrides: object) -> EligibilityCandidate:
    values: dict[str, object] = {
        "code_change_id": _PR_ID,
        "installation_external_id": 17,
        "repository_full_name": "octo/repo",
        "head_sha": _HEAD,
        "state": PullRequestState.OPEN,
        "repository_enabled": True,
        "ai_review_labeled": True,
        "ai_review_labeled_at": _AT,
        "head_first_seen_at": _AT,
        "wait_for_ci": CiWaitMode.AUTO,
    }
    values.update(overrides)
    return EligibilityCandidate(**values)  # type: ignore[arg-type]


@dataclass
class CandidateStore:
    candidate: EligibilityCandidate | None
    reads: int = 0

    async def get(self, code_change_id: UUID) -> EligibilityCandidate | None:
        assert code_change_id == _PR_ID
        self.reads += 1
        return self.candidate


@dataclass
class CiProvider:
    snapshot: CiSnapshot
    calls: list[tuple[int, str, str]] = field(default_factory=list)
    store: CandidateStore | None = None
    change_on_fetch: EligibilityCandidate | None = None

    async def get_current_head_ci(
        self, installation_external_id: int, repository_full_name: str, head_sha: str
    ) -> CiSnapshot:
        self.calls.append((installation_external_id, repository_full_name, head_sha))
        if self.store is not None and self.change_on_fetch is not None:
            self.store.candidate = self.change_on_fetch
        return self.snapshot


def _ci(*suites: CheckSuite, status_state: str = "pending", status_count: int = 0) -> CiSnapshot:
    return CiSnapshot(_HEAD, suites, status_state, status_count)


def _decide(
    candidate: EligibilityCandidate,
    snapshot: CiSnapshot,
    *,
    now: datetime = _AT,
    expected_head: str = _HEAD,
) -> tuple[CiEligibility, CiProvider, CandidateStore]:
    store = CandidateStore(candidate)
    provider = CiProvider(snapshot)
    use_case = DetermineCiEligibility(candidates=store, ci=provider, own_app_id=42, now=lambda: now)
    return asyncio.run(use_case.execute(_PR_ID, expected_head)), provider, store


def test_never_needs_label_but_does_not_fetch_ci() -> None:
    result, provider, _ = _decide(
        _candidate(wait_for_ci=CiWaitMode.NEVER), _ci(status_state="failure", status_count=1)
    )
    assert result == CiEligibility(True, EligibilityReason.ELIGIBLE, _HEAD)
    assert provider.calls == []

    for candidate in (
        _candidate(wait_for_ci=CiWaitMode.NEVER, ai_review_labeled=False),
        _candidate(wait_for_ci=CiWaitMode.NEVER, repository_enabled=False),
        _candidate(wait_for_ci=CiWaitMode.NEVER, state=PullRequestState.CLOSED),
    ):
        result, provider, _ = _decide(candidate, _ci())
        assert result.eligible is False
        assert provider.calls == []

    legacy_label, provider, _ = _decide(
        _candidate(wait_for_ci=CiWaitMode.NEVER, ai_review_labeled_at=None), _ci()
    )
    assert legacy_label.eligible is True
    assert provider.calls == []


@pytest.mark.parametrize(
    ("suites", "status_state", "status_count", "eligible"),
    [
        ((CheckSuite(7, "completed", "success"),), "pending", 0, True),
        ((CheckSuite(7, "completed", "neutral"),), "success", 1, True),
        ((CheckSuite(7, "completed", "skipped"),), "pending", 0, True),
        ((CheckSuite(7, "queued", None),), "success", 1, False),
        ((CheckSuite(7, "completed", "failure"),), "success", 1, False),
        ((CheckSuite(7, "completed", "success"),), "failure", 1, False),
        ((), "success", 1, True),
        ((), "pending", 0, False),
        ((CheckSuite(42, "completed", "success"),), "pending", 0, False),
        (
            (
                CheckSuite(42, "completed", "failure"),
                CheckSuite(7, "completed", "success"),
            ),
            "pending",
            0,
            True,
        ),
        (
            (CheckSuite(7, "completed", "success"), CheckSuite(8, "in_progress", None)),
            "success",
            1,
            False,
        ),
    ],
)
def test_always_requires_green_foreign_ci(
    suites: tuple[CheckSuite, ...], status_state: str, status_count: int, eligible: bool
) -> None:
    result, provider, _ = _decide(
        _candidate(wait_for_ci=CiWaitMode.ALWAYS),
        _ci(*suites, status_state=status_state, status_count=status_count),
    )
    assert result.eligible is eligible
    assert provider.calls == [(17, "octo/repo", _HEAD)]


def _idle(app_id: int = 8) -> CheckSuite:
    """A foreign suite whose App created no check run: GitHub leaves it ``queued`` for good."""
    return CheckSuite(app_id, "queued", None, latest_check_runs_count=0)


def test_queued_foreign_suite_without_runs_is_no_ci_evidence_in_always_and_auto() -> None:
    always, _, _ = _decide(_candidate(wait_for_ci=CiWaitMode.ALWAYS), _ci(_idle()))
    assert always == CiEligibility(
        False, EligibilityReason.WAITING_FOR_CI, _HEAD, detail="no CI yet"
    )

    before, _, _ = _decide(_candidate(), _ci(_idle()), now=_AT + timedelta(minutes=1, seconds=59))
    after, _, _ = _decide(_candidate(), _ci(_idle()), now=_AT + timedelta(minutes=2))
    assert before == CiEligibility(
        False,
        EligibilityReason.WAITING_FOR_CI,
        _HEAD,
        detail="no CI yet, auto start at 2026-09-28T12:02:00+00:00",
    )
    assert after == CiEligibility(True, EligibilityReason.ELIGIBLE, _HEAD)


def test_two_queued_foreign_suites_without_runs_do_not_block_either() -> None:
    result, _, _ = _decide(
        _candidate(wait_for_ci=CiWaitMode.ALWAYS), _ci(_idle(8), _idle(9), _idle(10))
    )
    assert result.reason == EligibilityReason.WAITING_FOR_CI


@pytest.mark.parametrize("mode", [CiWaitMode.ALWAYS, CiWaitMode.AUTO])
def test_queued_foreign_suite_without_runs_next_to_green_ci_is_eligible(mode: CiWaitMode) -> None:
    green = CheckSuite(7, "completed", "success", latest_check_runs_count=1)
    result, _, _ = _decide(_candidate(wait_for_ci=mode), _ci(_idle(), green), now=_AT)
    assert result == CiEligibility(True, EligibilityReason.ELIGIBLE, _HEAD)


@pytest.mark.parametrize("mode", [CiWaitMode.ALWAYS, CiWaitMode.AUTO])
def test_queued_foreign_suite_without_runs_next_to_combined_success_is_eligible(
    mode: CiWaitMode,
) -> None:
    result, _, _ = _decide(
        _candidate(wait_for_ci=mode),
        _ci(_idle(), status_state="success", status_count=1),
        now=_AT,
    )
    assert result == CiEligibility(True, EligibilityReason.ELIGIBLE, _HEAD)


def test_queued_foreign_suite_without_runs_does_not_hide_failing_combined_status() -> None:
    result, _, _ = _decide(
        _candidate(), _ci(_idle(), status_state="failure", status_count=1), now=_AT
    )
    assert result.reason == EligibilityReason.CI_BLOCKED


@pytest.mark.parametrize(
    ("suite", "detail"),
    [
        (
            CheckSuite(8, "in_progress", None, latest_check_runs_count=1),
            "check suite app=8 in_progress",
        ),
        (CheckSuite(8, "queued", None, latest_check_runs_count=1), "check suite app=8 queued"),
        (CheckSuite(8, "queued", None, latest_check_runs_count=3), "check suite app=8 queued"),
        (CheckSuite(8, "queued", None), "check suite app=8 queued"),
    ],
)
def test_foreign_suite_with_runs_or_unknown_count_still_blocks(
    suite: CheckSuite, detail: str
) -> None:
    for mode in (CiWaitMode.ALWAYS, CiWaitMode.AUTO):
        result, _, _ = _decide(
            _candidate(wait_for_ci=mode), _ci(suite, _idle(9)), now=_AT + timedelta(hours=1)
        )
        assert result == CiEligibility(False, EligibilityReason.CI_BLOCKED, _HEAD, detail=detail), (
            mode
        )


@pytest.mark.parametrize(
    ("suite", "detail"),
    [
        (
            CheckSuite(8, "in_progress", None, latest_check_runs_count=0),
            "check suite app=8 in_progress",
        ),
        (
            CheckSuite(8, "completed", "failure", latest_check_runs_count=0),
            "check suite app=8 completed/failure",
        ),
        (
            CheckSuite(8, "completed", "cancelled", latest_check_runs_count=0),
            "check suite app=8 completed/cancelled",
        ),
    ],
)
def test_only_a_queued_foreign_suite_without_runs_is_ignored(
    suite: CheckSuite, detail: str
) -> None:
    for mode in (CiWaitMode.ALWAYS, CiWaitMode.AUTO):
        result, _, _ = _decide(
            _candidate(wait_for_ci=mode), _ci(suite, _idle(9)), now=_AT + timedelta(hours=1)
        )
        assert result == CiEligibility(False, EligibilityReason.CI_BLOCKED, _HEAD, detail=detail), (
            mode
        )


def test_completed_green_foreign_suite_counts_as_ci_whatever_its_run_count() -> None:
    suite = CheckSuite(7, "completed", "success", latest_check_runs_count=0)
    result, _, _ = _decide(_candidate(wait_for_ci=CiWaitMode.ALWAYS), _ci(suite))
    assert result == CiEligibility(True, EligibilityReason.ELIGIBLE, _HEAD)


def test_check_suite_defaults_to_blocking_when_the_run_count_is_unknown() -> None:
    assert CheckSuite(8, "queued", None).latest_check_runs_count >= 1


def test_auto_no_ci_waits_from_later_label_or_head_at_inclusive_boundary() -> None:
    candidate = _candidate(head_first_seen_at=_AT + timedelta(minutes=1))
    empty = _ci()
    before, _, _ = _decide(candidate, empty, now=_AT + timedelta(minutes=2, seconds=59))
    at_boundary, _, _ = _decide(candidate, empty, now=_AT + timedelta(minutes=3))
    assert before == CiEligibility(
        False,
        EligibilityReason.WAITING_FOR_CI,
        _HEAD,
        detail="no CI yet, auto start at 2026-09-28T12:03:00+00:00",
    )
    assert at_boundary == CiEligibility(True, EligibilityReason.ELIGIBLE, _HEAD)

    label_later = _candidate(
        ai_review_labeled_at=_AT + timedelta(minutes=1), head_first_seen_at=_AT
    )
    before, _, _ = _decide(label_later, empty, now=_AT + timedelta(minutes=2, seconds=59))
    at_boundary, _, _ = _decide(label_later, empty, now=_AT + timedelta(minutes=3))
    assert before.eligible is False
    assert at_boundary.eligible is True


def test_auto_green_ci_is_immediate_and_missing_head_clock_waits() -> None:
    green = _ci(CheckSuite(7, "completed", "success"))
    result, _, _ = _decide(_candidate(), green, now=_AT)
    assert result.eligible is True

    no_head_clock, _, _ = _decide(
        _candidate(head_first_seen_at=None), _ci(), now=_AT + timedelta(hours=1)
    )
    assert no_head_clock.reason == EligibilityReason.WAITING_FOR_CI


@pytest.mark.parametrize(
    ("suites", "detail"),
    [
        ((CheckSuite(5111174, "queued", None),), "check suite app=5111174 queued"),
        (
            (
                CheckSuite(42, "completed", "failure"),
                _idle(9),
                CheckSuite(7, "completed", "success"),
                CheckSuite(8, "completed", "timed_out"),
                CheckSuite(10, "in_progress", None),
            ),
            "check suite app=8 completed/timed_out (+1 more)",
        ),
        (
            (
                CheckSuite(8, "in_progress", None),
                CheckSuite(9, "queued", None),
                CheckSuite(10, "completed", "action_required"),
            ),
            "check suite app=8 in_progress (+2 more)",
        ),
        ((CheckSuite(8, "Queued", None),), "check suite app=8 ?"),
        ((CheckSuite(8, "in progress\n", None),), "check suite app=8 ?"),
        ((CheckSuite(8, "completed", ""),), "check suite app=8 completed/?"),
        ((CheckSuite(8, "completed", "x" * 41),), "check suite app=8 completed/?"),
        (
            (CheckSuite(8, "completed", "x" * 40),),
            "check suite app=8 completed/xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        ),
    ],
)
def test_ci_blocked_names_the_first_blocking_foreign_suite(
    suites: tuple[CheckSuite, ...], detail: str
) -> None:
    for mode in (CiWaitMode.ALWAYS, CiWaitMode.AUTO):
        # A failing commit status as well: the suite is reported, not the status.
        result, _, _ = _decide(
            _candidate(wait_for_ci=mode), _ci(*suites, status_state="failure", status_count=1)
        )
        assert result == CiEligibility(False, EligibilityReason.CI_BLOCKED, _HEAD, detail=detail), (
            mode
        )


@pytest.mark.parametrize(
    ("state", "detail"),
    [
        ("failure", "commit status failure"),
        ("pending", "commit status pending"),
        ("error", "commit status error"),
        ("Failure", "commit status ?"),
        ("x" * 41, "commit status ?"),
    ],
)
def test_ci_blocked_by_the_commit_status_names_its_state(state: str, detail: str) -> None:
    green = CheckSuite(7, "completed", "success")
    for mode in (CiWaitMode.ALWAYS, CiWaitMode.AUTO):
        result, _, _ = _decide(
            _candidate(wait_for_ci=mode), _ci(green, status_state=state, status_count=2)
        )
        assert result == CiEligibility(False, EligibilityReason.CI_BLOCKED, _HEAD, detail=detail), (
            mode
        )


def test_waiting_for_ci_says_what_it_waits_for() -> None:
    always, _, _ = _decide(_candidate(wait_for_ci=CiWaitMode.ALWAYS), _ci())
    assert always == CiEligibility(
        False, EligibilityReason.WAITING_FOR_CI, _HEAD, detail="no CI yet"
    )

    for unknown in (_candidate(ai_review_labeled_at=None), _candidate(head_first_seen_at=None)):
        result, _, _ = _decide(unknown, _ci(), now=_AT + timedelta(hours=1))
        assert result == CiEligibility(
            False,
            EligibilityReason.WAITING_FOR_CI,
            _HEAD,
            detail="no CI yet, label or head time unknown",
        )

    # The label (12:01:30 UTC, stored with an offset) is later than the head (12:00 UTC).
    labeled_at = datetime(2026, 9, 28, 15, 1, 30, tzinfo=timezone(timedelta(hours=3)))
    result, _, _ = _decide(
        _candidate(ai_review_labeled_at=labeled_at), _ci(), now=_AT + timedelta(minutes=2)
    )
    assert result == CiEligibility(
        False,
        EligibilityReason.WAITING_FOR_CI,
        _HEAD,
        detail="no CI yet, auto start at 2026-09-28T12:03:30+00:00",
    )


def test_pending_ci_never_times_out_and_stale_or_disabled_state_blocks() -> None:
    pending = _ci(CheckSuite(7, "in_progress", None))
    result, _, _ = _decide(_candidate(), pending, now=_AT + timedelta(hours=1))
    assert result.eligible is False

    result, provider, _ = _decide(_candidate(), _ci(), expected_head="b" * 40)
    assert result.reason == EligibilityReason.STALE_HEAD
    assert provider.calls == []

    store = CandidateStore(_candidate())
    provider = CiProvider(_ci(CheckSuite(7, "completed", "success")))
    provider.store = store
    assert store.candidate is not None
    provider.change_on_fetch = replace(store.candidate, head_sha="b" * 40)
    use_case = DetermineCiEligibility(candidates=store, ci=provider, own_app_id=42)
    result = asyncio.run(use_case.execute(_PR_ID, _HEAD))
    assert result.reason == EligibilityReason.STALE_HEAD
    assert store.reads == 2

    store = CandidateStore(_candidate())
    provider = CiProvider(_ci(CheckSuite(7, "completed", "success")))
    provider.store = store
    assert store.candidate is not None
    provider.change_on_fetch = replace(store.candidate, repository_enabled=False)
    use_case = DetermineCiEligibility(candidates=store, ci=provider, own_app_id=42)
    result = asyncio.run(use_case.execute(_PR_ID, _HEAD))
    assert result.reason == EligibilityReason.DISABLED_REPOSITORY


def test_retained_label_rechecks_new_head_from_later_ci_or_sweep() -> None:
    new_head = "c" * 40
    candidate = _candidate(
        head_sha=new_head,
        head_first_seen_at=_AT + timedelta(minutes=1),
        ai_review_labeled_at=_AT,
    )
    store = CandidateStore(candidate)
    provider = CiProvider(
        CiSnapshot(new_head, (CheckSuite(7, "completed", "success"),), "pending", 0)
    )
    use_case = DetermineCiEligibility(candidates=store, ci=provider, own_app_id=42)

    old_head = asyncio.run(use_case.execute(_PR_ID, _HEAD))
    later_ci_or_sweep = asyncio.run(use_case.execute(_PR_ID, new_head))

    assert old_head.reason == EligibilityReason.STALE_HEAD
    assert later_ci_or_sweep.reason == EligibilityReason.ELIGIBLE
    assert provider.calls == [(17, "octo/repo", new_head)]


def test_candidate_reads_complete_before_and_after_github_fetch() -> None:
    events: list[str] = []

    class Store:
        async def get(self, code_change_id: UUID) -> EligibilityCandidate:
            events.append("db-read-complete")
            return _candidate(wait_for_ci=CiWaitMode.ALWAYS)

    class Provider:
        async def get_current_head_ci(
            self, installation_external_id: int, repository_full_name: str, head_sha: str
        ) -> CiSnapshot:
            events.append("github-fetch")
            return _ci(CheckSuite(7, "completed", "success"))

    result = asyncio.run(
        DetermineCiEligibility(candidates=Store(), ci=Provider(), own_app_id=42).execute(
            _PR_ID, _HEAD
        )
    )
    assert result.eligible is True
    assert events == ["db-read-complete", "github-fetch", "db-read-complete"]


def test_github_ci_adapter_paginates_suites_and_reads_combined_status_for_exact_sha() -> None:
    seen: list[httpx.Request] = []

    class Tokens:
        async def get_installation_access_token(self, installation_external_id: int) -> str:
            assert installation_external_id == 17
            return "installation-token"

    def suite(app_id: int) -> dict[str, object]:
        return {
            "id": app_id,
            "head_sha": _HEAD,
            "app": {"id": app_id},
            "status": "completed",
            "conclusion": "success",
            "latest_check_runs_count": 1,
        }

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/check-suites"):
            page = request.url.params["page"]
            return httpx.Response(
                200,
                json={
                    "total_count": 101,
                    "check_suites": [suite(42)] * 100 if page == "1" else [suite(7)],
                },
            )
        return httpx.Response(200, json={"sha": _HEAD, "state": "success", "total_count": 1})

    async def exercise() -> CiSnapshot:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.github.com"
        ) as client:
            provider = HttpGitHubCurrentHeadCiProvider(client=client, token_provider=Tokens())
            return await provider.get_current_head_ci(17, "octo/repo", _HEAD)

    result = asyncio.run(exercise())
    assert len(result.check_suites) == 101
    assert result.check_suites[-1] == CheckSuite(7, "completed", "success", 1)
    assert result.combined_state == "success"
    assert [request.url.path for request in seen] == [
        f"/repos/octo/repo/commits/{_HEAD}/check-suites",
        f"/repos/octo/repo/commits/{_HEAD}/check-suites",
        f"/repos/octo/repo/commits/{_HEAD}/status",
    ]
    assert all(request.headers["Authorization"] == "Bearer installation-token" for request in seen)


def _fetch_suites(suites: list[dict[str, object]]) -> CiSnapshot:
    class Tokens:
        async def get_installation_access_token(self, installation_external_id: int) -> str:
            return "installation-token"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/check-suites"):
            return httpx.Response(200, json={"total_count": len(suites), "check_suites": suites})
        return httpx.Response(200, json={"sha": _HEAD, "state": "pending", "total_count": 0})

    async def exercise() -> CiSnapshot:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.github.com"
        ) as client:
            provider = HttpGitHubCurrentHeadCiProvider(client=client, token_provider=Tokens())
            return await provider.get_current_head_ci(17, "octo/repo", _HEAD)

    return asyncio.run(exercise())


def _raw_suite(app_id: int, status: str, **extra: object) -> dict[str, object]:
    return {
        "id": app_id,
        "head_sha": _HEAD,
        "app": {"id": app_id},
        "status": status,
        "conclusion": None,
        **extra,
    }


def test_github_ci_adapter_reads_the_latest_check_runs_count_of_each_suite() -> None:
    result = _fetch_suites(
        [
            _raw_suite(8, "queued", latest_check_runs_count=0),
            _raw_suite(9, "queued", latest_check_runs_count=2),
        ]
    )
    assert result.check_suites == (
        CheckSuite(8, "queued", None, latest_check_runs_count=0),
        CheckSuite(9, "queued", None, latest_check_runs_count=2),
    )


def test_github_ci_adapter_treats_a_missing_run_count_as_blocking() -> None:
    (suite,) = _fetch_suites([_raw_suite(8, "queued")]).check_suites
    assert suite.latest_check_runs_count >= 1


@pytest.mark.parametrize("count", [-1, "0", None, 1.5])
def test_github_ci_adapter_rejects_a_malformed_run_count(count: object) -> None:
    with pytest.raises(ValueError, match=r"check_suites\.0\.latest_check_runs_count"):
        _fetch_suites([_raw_suite(8, "queued", latest_check_runs_count=count)])


@pytest.mark.parametrize(
    ("status_body", "reason"),
    [
        (
            {"sha": "b" * 40, "state": "success", "total_count": 1},
            "GitHub combined status SHA mismatch",
        ),
        (
            {"sha": _HEAD, "state": "unknown", "total_count": 0},
            "Input should be 'success', 'failure' or 'pending'",
        ),
    ],
)
def test_github_ci_adapter_rejects_uncertain_combined_status(
    status_body: dict[str, object], reason: str
) -> None:
    class Tokens:
        async def get_installation_access_token(self, installation_external_id: int) -> str:
            return "installation-token"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/check-suites"):
            return httpx.Response(200, json={"total_count": 0, "check_suites": []})
        return httpx.Response(200, json=status_body)

    async def exercise() -> CiSnapshot:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.github.com"
        ) as client:
            provider = HttpGitHubCurrentHeadCiProvider(client=client, token_provider=Tokens())
            return await provider.get_current_head_ci(17, "octo/repo", _HEAD)

    with pytest.raises(ValueError, match=re.escape(reason)):
        asyncio.run(exercise())
