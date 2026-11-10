"""Rules playground simulations (#311 Part 1): input checks, use cases and the HTTP surface."""

from __future__ import annotations

from uuid import UUID

import pytest

from app.modules.reviews.application.rules_simulation import (
    RulesSimulationMode,
    RulesSimulationRejected,
    RulesSimulationRequest,
    check_simulation_input,
)

REPOSITORY_ID = UUID("00000000-0000-4000-8000-0000000000a2")
SECRET = "sk-sandbox-SECRET-4f1c"


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
