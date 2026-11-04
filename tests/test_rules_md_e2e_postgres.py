"""``.review/rules.md`` from a signed webhook: the RULES_FORMAT_SPEC §9 failure catalog (#163).

A policy that cannot be loaded fails the run once in ``policy.load``, without an LLM call. The
chain, its fakes and seeds live in ``tests/rules_md_e2e.py``. Every case here runs on the v4
seed.

The module migrates one schema; each case starts from emptied tables with the same seed and
from a deleted review topology, so the cases stay isolated.

Opt-in: ``TEST_DATABASE_URL`` (disposable PostgreSQL) and ``TEST_RABBITMQ_URL`` (disposable
RabbitMQ: the review topology is deleted around every case); in CI ``RABBITMQ_URL``.
"""

from __future__ import annotations

import hashlib

import pytest

from app.modules.reviews.application import review_ignore
from tests.rules_md_e2e import (
    _AGENTS_HALF,
    _AT_LIMIT,
    _RULES_HALF,
    BASE,
    PLAIN_RULES,
    RULES,
    VALID_RULES,
    Fault,
    Outcome,
    Seed,
    Stack,
    _block,
    _github,
    _review,
    _rules_load,
)

pytest_plugins = ("tests.rules_md_e2e",)

# RFS §3.3: the two rendered policy blocks together, in input tokens.
_MAX_POLICY_TOKENS = 12_000


# §9 for a 404: `policy_revision_unavailable` ("закреплённая ревизия недоступна") or
# `policy_access_denied` ("недоступный repository"); a private repository answers 404 to a
# lost access as well (§3.1), so the code may be either, but never a missing document.
_UNREADABLE_404 = frozenset({"policy_revision_unavailable", "policy_access_denied"})
# §9: a 401/403 without rate-limit signs is `policy_access_denied`, not `cancelled`.
_ACCESS_DENIED = frozenset({"policy_access_denied"})


def _assert_policy_load_failed(outcome: Outcome) -> None:
    # PIPELINE_SPEC §trace: a failed `policy.load` stores `{error: {type, message, diagnostic}}`;
    # the diagnostic carries the RFS §9 fields of the policy failure.
    assert outcome.policy_load
    for response in outcome.policy_load:
        assert set(response) == {"error"}, response
        assert set(response["error"]) == {"type", "message", "diagnostic"}, response
        diagnostic = response["error"]["diagnostic"]
        assert diagnostic["code"] == outcome.error_code, response
        assert diagnostic["policy_base_sha"] == BASE, response


def _assert_failed_once(outcome: Outcome, error_codes: frozenset[str]) -> None:
    assert (outcome.state, outcome.attempt) == ("failed", 1)
    assert outcome.error_code in error_codes
    # Current behaviour: a failed load stores no snapshot; RFS §7.2 allows that
    # («полного валидного snapshot может не быть»).
    assert outcome.snapshots == 0
    _assert_policy_load_failed(outcome)
    assert outcome.tasks == []
    assert outcome.reviews == []
    # PIPELINE_SPEC §7: `failed` → `completed` / `neutral`, the bot never blocks a merge.
    assert outcome.check_run["conclusion"] == "neutral"


@pytest.mark.integration
@pytest.mark.parametrize("seed", [Seed.V4], ids=str)
def test_rules_md_at_the_size_limit_exceeds_the_policy_budget(stack: Stack, seed: Seed) -> None:
    # §3.3: the 65 536 bytes pass the byte limit, but rendered whole the block is over the
    # 12 000 tokens of the two policy blocks; §11: failed without LLM and without retry, the
    # document not truncated. §9: the budget fails in `policy.load` and names no document.
    outcome = _review(stack, _github(_AT_LIMIT), seed)

    assert (outcome.state, outcome.attempt) == ("failed", 1)
    assert outcome.error_code == "policy_prompt_budget_exceeded"
    assert outcome.documents[RULES] == ("present", hashlib.sha256(_AT_LIMIT).hexdigest(), 65_536)
    _assert_policy_load_failed(outcome)
    [load] = outcome.policy_load
    diagnostic = load["error"]["diagnostic"]
    # §3.3 counts both rendered blocks, the 65 536 `a` on one numbered line and the absent
    # AGENTS.md tag: 21 911 tokens by counter/v2, `(len + 2) // 3` per block (§7.2).
    assert (diagnostic["source_path"], diagnostic["limit"], diagnostic["actual"]) == (
        None,
        _MAX_POLICY_TOKENS,
        21_911,
    )
    # The stored snapshot is not asserted: RFS §7.2 allows a failed configuration with or
    # without one.
    assert outcome.tasks == []
    assert outcome.reviews == []
    # PIPELINE_SPEC §7: `failed` → `completed` / `neutral`.
    assert outcome.check_run["conclusion"] == "neutral"


@pytest.mark.integration
@pytest.mark.parametrize("seed", [Seed.V4], ids=str)
def test_policy_documents_together_over_the_budget_fail_the_run_without_llm(
    stack: Stack, seed: Seed
) -> None:
    # One byte more of AGENTS.md: 6 001 tokens, 12 001 together, each document still far
    # under 64 KiB and under the budget alone. §3.3, §11: failed in `policy.load` before
    # the conventions model, without retry, and neither document is truncated.
    agents = _AGENTS_HALF + b"g"
    outcome = _review(stack, _github(_RULES_HALF, agents=agents), seed)

    assert (outcome.state, outcome.attempt) == ("failed", 1)
    assert outcome.error_code == "policy_prompt_budget_exceeded"
    assert outcome.documents[RULES] == (
        "present",
        hashlib.sha256(_RULES_HALF).hexdigest(),
        len(_RULES_HALF),
    )
    assert outcome.documents["AGENTS.md"] == (
        "present",
        hashlib.sha256(agents).hexdigest(),
        len(agents),
    )
    _assert_policy_load_failed(outcome)
    [load] = outcome.policy_load
    diagnostic = load["error"]["diagnostic"]
    # §9: the budget of the two documents names neither of them.
    assert (diagnostic["source_path"], diagnostic["limit"], diagnostic["actual"]) == (
        None,
        _MAX_POLICY_TOKENS,
        12_001,
    )
    assert outcome.tasks == []
    assert outcome.reviews == []
    # PIPELINE_SPEC §7: `failed` → `completed` / `neutral`.
    assert outcome.check_run["conclusion"] == "neutral"


# The failures below end in `prepare_policy.execute` (`policy.load`); the only earlier
# path-specific call, `require_served_provider` (`process_run.py:214` -> `:368`), passes a
# GitHub run on either path. They run on the v4 seed, the path of every new GitHub run, only.


@pytest.mark.integration
@pytest.mark.parametrize(
    ("rules", "error_code"),
    [
        pytest.param(b"ok\n\xff\xfe", "policy_document_decode_error", id="decode"),
        # U+D800 as UTF-8 bytes: an unpaired surrogate (§3.2 item 5).
        pytest.param(b"ok\n\xed\xa0\x80", "policy_document_decode_error", id="surrogate"),
        # ESC (U+001B): only TAB, LF and CR are allowed below U+0020 (§3.2 item 5).
        pytest.param(b"\x1bfirst", "policy_document_control_character", id="control"),
        pytest.param(
            (_block("a/**") + _block("b/**")).encode(),
            "policy_ignore_block_duplicate",
            id="duplicate",
        ),
        pytest.param(
            b"- item\n\n  ```review-ignore\n  a/**\n  ```\n",
            "policy_ignore_block_nested",
            id="nested",
        ),
        pytest.param(b"```review-ignore\na/**\n", "policy_ignore_block_unclosed", id="unclosed"),
        pytest.param(
            _block("a/**", info="review-ignore extra").encode(),
            "policy_ignore_info_invalid",
            id="info",
        ),
        pytest.param(
            _block("ok/**", "a?.py").encode(), "policy_ignore_pattern_invalid", id="pattern"
        ),
        # 65 536 bytes is the limit itself (§3.3); one more byte is over it.
        pytest.param(b"a" * 65_537, "policy_document_too_large", id="too-large"),
        pytest.param(
            _block(*[f"d{index}/**" for index in range(257)]).encode(),
            "policy_ignore_limit_exceeded",
            id="limit",
        ),
    ],
)
def test_invalid_rules_md_fails_the_run_once_without_llm(
    stack: Stack, rules: bytes, error_code: str
) -> None:
    outcome = _review(stack, _github(rules), Seed.V4)

    _assert_failed_once(outcome, frozenset({error_code}))


@pytest.mark.integration
def test_symlinked_rules_md_fails_the_run_once_without_llm(stack: Stack) -> None:
    # §3.1: a symlink is never dereferenced; the document must be a regular Git blob.
    outcome = _review(stack, _github(b"../docs/rules.md", symlink=RULES), Seed.V4)

    _assert_failed_once(outcome, frozenset({"policy_document_type_unsupported"}))


@pytest.mark.integration
@pytest.mark.parametrize(
    ("agents", "symlink", "error_code"),
    [
        # 65 536 bytes is the limit of each document (§3.3); one more byte is over it.
        pytest.param(b"a" * 65_537, None, "policy_document_too_large", id="too-large"),
        # §3.1: the symlink is not dereferenced, its target never read as the document.
        pytest.param(
            b"docs/AGENTS.md", "AGENTS.md", "policy_document_type_unsupported", id="symlink"
        ),
        pytest.param(b"ok\n\xff\xfe", None, "policy_document_decode_error", id="decode"),
        pytest.param(b"\x1bfirst", None, "policy_document_control_character", id="control"),
    ],
)
def test_invalid_agents_md_fails_the_run_once_without_llm(
    stack: Stack, agents: bytes, symlink: str | None, error_code: str
) -> None:
    # §9: an AGENTS.md error stops the review like a rules.md one: the document is optional
    # only in that it may be absent. A valid rules.md beside it does not let the run go on.
    outcome = _review(stack, _github(VALID_RULES, agents=agents, symlink=symlink), Seed.V4)

    _assert_failed_once(outcome, frozenset({error_code}))
    [load] = outcome.policy_load
    # §9: the diagnostic names the document the error is about.
    assert load["error"]["diagnostic"]["source_path"] == "AGENTS.md"
    # §11, §7.2: rules.md, read first, stays staged; the failed AGENTS.md is never missing.
    assert outcome.documents[RULES] == (
        "present",
        hashlib.sha256(VALID_RULES).hexdigest(),
        len(VALID_RULES),
    )
    assert outcome.documents.get("AGENTS.md", (None,))[0] != "missing"


@pytest.mark.integration
def test_exhausted_match_budget_fails_the_run_once_without_llm(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    # §3.3: the matcher budget per path, lowered to one transition, so matching the mask of
    # the valid rules.md against a changed path exceeds it. With the real budget the same
    # PR is reviewed (`test_review_ignore_keeps_the_ignored_file_out_of_every_llm_call`).
    monkeypatch.setattr(review_ignore, "MAX_TRANSITIONS_PER_PATH", 1)
    outcome = _review(stack, _github(VALID_RULES), Seed.V4)

    # §9: failed, no retry, no LLM call, no review; the bot never blocks a merge.
    assert (outcome.state, outcome.attempt, outcome.error_code) == (
        "failed",
        1,
        "policy_match_budget_exceeded",
    )
    assert outcome.tasks == []
    assert outcome.reviews == []
    assert outcome.check_run["conclusion"] == "neutral"
    # The policy itself loads; the error is the matcher's, traced in the step where it ran
    # (§9: `vcs.fetch_diff` or `context.build`): here the first one, storing the diff.
    assert _rules_load(outcome)["presence"] == "present"
    failed = [
        (tool, response)
        for tool, response in outcome.steps
        if isinstance(response, dict) and "error" in response
    ]
    assert [tool for tool, _ in failed] == ["vcs.fetch_diff"], outcome.steps
    diagnostic = failed[0][1]["error"]["diagnostic"]
    assert (diagnostic["code"], diagnostic["source_path"], diagnostic["policy_base_sha"]) == (
        "policy_match_budget_exceeded",
        RULES,
        BASE,
    )
    # §9: `actual` is the counter that crossed the limit at the stop, a lower bound.
    assert (diagnostic["limit"], diagnostic["actual_is_lower_bound"]) == (1, True)
    assert diagnostic["actual"] > 1


@pytest.mark.integration
@pytest.mark.parametrize(
    ("base_rules", "fault", "error_codes"),
    [
        # The base root itself: without a `.review` entry, a 404 read as "absent" would pass.
        pytest.param(None, Fault.ROOT_404, _UNREADABLE_404, id="root-404"),
        pytest.param(None, Fault.ROOT_DENIED, _ACCESS_DENIED, id="root-denied"),
        pytest.param(PLAIN_RULES, Fault.TREE_404, _UNREADABLE_404, id="tree-404"),
        pytest.param(PLAIN_RULES, Fault.BLOB_404, _UNREADABLE_404, id="blob-404"),
        # No `X-RateLimit-Remaining: 0` and no `Retry-After` on the refusal.
        pytest.param(PLAIN_RULES, Fault.DENIED, _ACCESS_DENIED, id="denied"),
    ],
)
def test_unreadable_rules_md_fails_the_run_and_is_never_missing(
    stack: Stack, base_rules: bytes | None, fault: Fault, error_codes: frozenset[str]
) -> None:
    outcome = _review(stack, _github(base_rules, fault=fault), Seed.V4)

    # Failing proves the read was not taken for a missing document (§3.1, §11).
    _assert_failed_once(outcome, error_codes)


@pytest.mark.integration
def test_truncated_base_tree_fails_the_run_and_is_never_missing(stack: Stack) -> None:
    # No `.review` entry in a cut listing: only completeness tells absence from a gap.
    outcome = _review(stack, _github(None, fault=Fault.TRUNCATED), Seed.V4)

    _assert_failed_once(outcome, frozenset({"policy_revision_unavailable"}))
