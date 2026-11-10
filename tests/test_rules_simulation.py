"""Rules playground simulations (#311 Part 1): input checks, use cases and the HTTP surface."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from app.modules.reviews.application.rules_simulation import (
    ActiveRulesSimulationExists,
    CreateRulesSimulation,
    GetRulesSimulation,
    RulesSimulation,
    RulesSimulationMode,
    RulesSimulationRejected,
    RulesSimulationRequest,
    RulesSimulationStatus,
    check_simulation_input,
)
from tests.rules_simulation_fakes import VISIBLE_REPOSITORY_ID, FakeRulesSimulations

REPOSITORY_ID = VISIBLE_REPOSITORY_ID
SECRET = "sk-sandbox-SECRET-4f1c"
CREATED_AT = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
FIRST_ID = UUID(int=1)
SECOND_ID = UUID(int=2)


def _request(
    *,
    mode: RulesSimulationMode = RulesSimulationMode.CODE,
    path: str = "src/app.py",
    content: str = "print('hello')\n",
    rules_md: str = "",
) -> RulesSimulationRequest:
    return RulesSimulationRequest(
        repository_id=REPOSITORY_ID, mode=mode, path=path, content=content, rules_md=rules_md
    )


def _rejection(request: RulesSimulationRequest) -> RulesSimulationRejected:
    with pytest.raises(RulesSimulationRejected) as caught:
        check_simulation_input(request)
    return caught.value


@pytest.mark.parametrize(
    "rules_md",
    [
        "",
        "# Rules\n\nPrefer explicit errors.\n",
        "```review-ignore\ngenerated/**\n```\n",
    ],
    ids=["defaults-only", "prose", "ignore-block"],
)
@pytest.mark.parametrize("mode", list(RulesSimulationMode))
def test_a_valid_input_passes(mode: RulesSimulationMode, rules_md: str) -> None:
    check_simulation_input(_request(mode=mode, rules_md=rules_md))


@pytest.mark.parametrize(
    "path",
    ["", "/src/app.py", "src//app.py", "src/../app.py", "src\\app.py", "src/a\x00.py", "\ud800"],
    ids=["empty", "absolute", "empty-segment", "dot-dot", "backslash", "nul", "lone-surrogate"],
)
def test_a_path_that_is_not_a_canonical_repository_path_is_rejected(path: str) -> None:
    rejection = _rejection(_request(path=path))

    assert rejection.code == "path_invalid"


@pytest.mark.parametrize("content", ["a\x00b", "x\ud800y"], ids=["nul", "lone-surrogate"])
def test_content_that_is_not_storable_utf8_text_is_rejected(content: str) -> None:
    rejection = _rejection(_request(content=content))

    assert rejection.code == "content_invalid"


def test_content_over_64_kib_of_utf8_is_rejected_with_the_limit() -> None:
    at_limit = "é" * 32_768  # 65 536 UTF-8 bytes

    check_simulation_input(_request(content=at_limit))
    rejection = _rejection(_request(content=at_limit + "a"))

    assert rejection.code == "content_too_large"
    assert (rejection.limit, rejection.actual) == (65_536, 65_537)


@pytest.mark.parametrize(
    ("rules_md", "code", "line", "column", "limit", "actual"),
    [
        ("a" * 65_537, "policy_document_too_large", None, None, 65_536, 65_537),
        ("ok\nb\x01", "policy_document_control_character", 2, 2, None, None),
        ("rules \ud800", "policy_document_decode_error", None, None, None, None),
        ("```review-ignore\nsrc/**\n", "policy_ignore_block_unclosed", 1, None, None, None),
        ("```review-ignore\nsrc/[ab]\n```\n", "policy_ignore_pattern_invalid", 2, 5, None, None),
        ("a" * 40_000, "policy_prompt_budget_exceeded", None, None, 12_000, None),
    ],
    ids=["too-large", "control-character", "lone-surrogate", "unclosed", "bad-mask", "tokens"],
)
def test_rules_outside_the_format_or_its_limits_are_rejected_with_a_diagnostic(
    rules_md: str,
    code: str,
    line: int | None,
    column: int | None,
    limit: int | None,
    actual: int | None,
) -> None:
    rejection = _rejection(_request(rules_md=rules_md))

    assert rejection.code == "rules_invalid"
    [diagnostic] = rejection.diagnostics
    assert (diagnostic.code, diagnostic.line, diagnostic.column) == (code, line, column)
    assert diagnostic.limit == limit
    if actual is not None:
        assert diagnostic.actual == actual
    if code == "policy_prompt_budget_exceeded":
        assert diagnostic.actual is not None and diagnostic.actual > 12_000


def test_a_rules_diagnostic_never_quotes_the_rules() -> None:
    rejection = _rejection(_request(rules_md=f"```review-ignore\n{SECRET}/[x]\n```\n"))

    [diagnostic] = rejection.diagnostics
    assert diagnostic.code == "policy_ignore_pattern_invalid"
    assert SECRET not in diagnostic.message
    assert SECRET not in str(rejection)


class Clock:
    def __init__(self) -> None:
        self.now = CREATED_AT

    def __call__(self) -> datetime:
        return self.now


class Ids:
    def __init__(self) -> None:
        self._ids = iter((FIRST_ID, SECOND_ID, UUID(int=3)))

    def __call__(self) -> UUID:
        return next(self._ids)


def _use_cases(
    store: FakeRulesSimulations, clock: Clock
) -> tuple[CreateRulesSimulation, GetRulesSimulation]:
    return (
        CreateRulesSimulation(lambda: store, now=clock, new_id=Ids()),
        GetRulesSimulation(lambda: store, now=clock),
    )


def test_a_created_simulation_is_queued_and_read_back() -> None:
    store, clock = FakeRulesSimulations(), Clock()
    create, get = _use_cases(store, clock)

    created = asyncio.run(create.execute(_request(mode=RulesSimulationMode.DIFF)))
    read = asyncio.run(get.execute(FIRST_ID))

    expected = RulesSimulation(
        id=FIRST_ID,
        repository_id=REPOSITORY_ID,
        mode=RulesSimulationMode.DIFF,
        path="src/app.py",
        status=RulesSimulationStatus.QUEUED,
        error_code=None,
        created_at=CREATED_AT,
        finished_at=None,
    )
    assert created == expected
    assert read == expected


def test_a_repository_the_caller_cannot_see_creates_nothing() -> None:
    store, clock = FakeRulesSimulations(visible=frozenset()), Clock()
    create, _ = _use_cases(store, clock)

    assert asyncio.run(create.execute(_request())) is None
    assert store.rows == {}


def test_a_refused_input_never_opens_a_transaction() -> None:
    store, clock = FakeRulesSimulations(), Clock()
    create, _ = _use_cases(store, clock)

    with pytest.raises(RulesSimulationRejected):
        asyncio.run(create.execute(_request(path="")))
    assert store.entered == 0


def test_a_second_simulation_while_one_is_active_names_the_active_one() -> None:
    store, clock = FakeRulesSimulations(), Clock()
    create, _ = _use_cases(store, clock)
    asyncio.run(create.execute(_request()))

    with pytest.raises(ActiveRulesSimulationExists) as caught:
        asyncio.run(create.execute(_request()))

    assert caught.value.simulation_id == FIRST_ID
    assert set(store.rows) == {FIRST_ID}


@pytest.mark.parametrize(
    ("elapsed", "status", "error_code"),
    [
        (timedelta(seconds=599), RulesSimulationStatus.QUEUED, None),
        (timedelta(minutes=10), RulesSimulationStatus.FAILED, "deadline_exceeded"),
    ],
    ids=["before-the-deadline", "at-the-deadline"],
)
def test_a_simulation_not_finished_within_10_minutes_reads_as_failed(
    elapsed: timedelta, status: RulesSimulationStatus, error_code: str | None
) -> None:
    store, clock = FakeRulesSimulations(), Clock()
    create, get = _use_cases(store, clock)
    asyncio.run(create.execute(_request()))
    clock.now = CREATED_AT + elapsed

    read = asyncio.run(get.execute(FIRST_ID))

    assert read is not None
    assert (read.status, read.error_code) == (status, error_code)
    assert read.finished_at == (None if error_code is None else clock.now)
    assert store.rows[FIRST_ID][1] == read


@pytest.mark.parametrize("read_first", [True, False], ids=["after-a-read", "without-a-read"])
def test_an_overdue_simulation_no_longer_blocks_a_new_one(read_first: bool) -> None:
    store, clock = FakeRulesSimulations(), Clock()
    create, get = _use_cases(store, clock)
    asyncio.run(create.execute(_request()))
    clock.now = CREATED_AT + timedelta(minutes=10)
    if read_first:
        asyncio.run(get.execute(FIRST_ID))

    created = asyncio.run(create.execute(_request()))

    assert created is not None
    assert (created.id, created.status) == (SECOND_ID, RulesSimulationStatus.QUEUED)
    assert store.rows[FIRST_ID][1].error_code == "deadline_exceeded"


def test_another_users_or_an_unknown_simulation_is_not_found() -> None:
    store, clock = FakeRulesSimulations(), Clock()
    create, _ = _use_cases(store, clock)
    asyncio.run(create.execute(_request()))
    _, other_get = _use_cases(store.for_user(43), clock)
    _, get = _use_cases(store, clock)

    assert asyncio.run(other_get.execute(FIRST_ID)) is None
    assert asyncio.run(get.execute(UUID(int=99))) is None
