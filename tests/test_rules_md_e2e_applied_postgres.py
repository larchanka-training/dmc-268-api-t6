"""``.review/rules.md`` from a signed webhook: a run that applies the base policy (#163).

The run loads the policy of the base revision and applies it: what reaches the model, what the
policy ignores, and what a change in the head does not alter. The chain, its fakes and seeds
live in ``tests/rules_md_e2e.py``. A case runs on both seeds unless it pins one.

The module gets one migrated database; each case starts from emptied tables with the same seed and
from a deleted review topology, so the cases stay isolated.

Opt-in: ``TEST_DATABASE_URL`` (disposable PostgreSQL) and ``TEST_RABBITMQ_URL`` (disposable
RabbitMQ: the review topology is deleted around every case); in CI ``RABBITMQ_URL``.
"""

from __future__ import annotations

import hashlib
import json
import re

import pytest

from tests.rules_md_e2e import (
    _AGENTS_HALF,
    _AT_LIMIT,
    _RULES_HALF,
    CLIENT_MARKER,
    HEAD,
    PLAIN_RULES,
    RULES,
    SERVICE_MARKER,
    SERVICE_PATH,
    VALID_RULES,
    Outcome,
    Seed,
    Stack,
    _block,
    _github,
    _review,
    _rules_load,
    _user_message,
)

pytest_plugins = ("tests.rules_md_e2e",)

EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

# The rules.md block of a snapshot-path prompt: numbered lines, or one absent/empty tag.
_POLICY_RULES = re.compile(
    r'<repository_rules block_id="policy-rules"[^>]*?(?: />|>\n.*?\n</repository_rules>)',
    re.DOTALL,
)
# RFS §8.1 `source_code`: what a prompt shows of the changed files, read at the head.
_SOURCE_CODE = re.compile(r"<(changed_files|context_excerpts)>.*?</\1>", re.DOTALL)
_REVIEW_TASK = {Seed.LEGACY: "ReviewOutput", Seed.V4: "ReviewOutputV2"}


# The ignored file: with a mask neither its text nor its name may reach the model (§5.3).
CLIENT_PATH = "generated/client.py"

ALL_IGNORED_RULES = _block("app/**", "generated/**").encode()


# A head rule without a mask: only the policy revision tells the head text from the base one.
HEAD_ONLY_RULE = "Head-only rule."
PLAIN_RULES_WITH_A_HEAD_RULE = PLAIN_RULES + f"{HEAD_ONLY_RULE}\n".encode()
IGNORE_EVERYTHING_RULES = _block("**/*").encode()
# A review-ignore block covering both changed files, in AGENTS.md: ordinary text there (§4.3).
AGENTS_WITH_IGNORE_BLOCK = ("# Agents\n\n" + _block("app/**", "generated/**")).encode()

# The rules.md block of the snapshot-path input, as review.system v4 describes it
# (review/prompts/review.system.v4.md:22-26): the base text, one `<line n="…">` per line from
# 1, XML-escaped; a missing document is a self-closing tag with `status="absent"`, an empty
# one with `status="empty"`. Written out, so the renderer's escaping and splitting are not reused.
_ABSENT_RULES_BLOCK = (
    '<repository_rules block_id="policy-rules" path=".review/rules.md" status="absent" />'
)
_EMPTY_RULES_BLOCK = (
    '<repository_rules block_id="policy-rules" path=".review/rules.md" status="empty" />'
)
_VALID_RULES_BLOCK = """\
<repository_rules block_id="policy-rules" path=".review/rules.md">
<line n="1"># Rules</line>
<line n="2"></line>
<line n="3">## Style</line>
<line n="4"></line>
<line n="5">Prefer small functions.</line>
<line n="6">Keep `a &lt; b &amp;&amp; b &gt; c` checks explicit.</line>
<line n="7"></line>
<line n="8"># Ignore</line>
<line n="9"></line>
<line n="10">```review-ignore</line>
<line n="11">generated/**</line>
<line n="12">```</line>
</repository_rules>"""
_PLAIN_RULES_BLOCK = """\
<repository_rules block_id="policy-rules" path=".review/rules.md">
<line n="1"># Rules</line>
<line n="2"></line>
<line n="3">Prefer small functions.</line>
</repository_rules>"""


def _assert_published(outcome: Outcome, seed: Seed) -> None:
    assert (outcome.state, outcome.error_code) == ("succeeded", None)
    assert _REVIEW_TASK[seed] in outcome.tasks
    assert len(outcome.reviews) == 1
    assert outcome.reviews[0]["commit_id"] == HEAD
    # PIPELINE_SPEC §7: `succeeded` → `completed` / `neutral`.
    assert outcome.check_run["conclusion"] == "neutral"


def _texts(outcome: Outcome) -> list[str]:
    """Every message of every LLM call, parsed: the raw bodies escape quotes and newlines."""
    return [
        "\n".join(item["content"] for item in json.loads(body)["messages"])
        for body in outcome.bodies
    ]


def _user_messages(outcome: Outcome, task: str) -> list[str]:
    """The input envelopes of the calls with this response schema; at least one was made."""
    messages = [
        _user_message(json.loads(body))
        for name, body in zip(outcome.tasks, outcome.bodies, strict=True)
        if name == task
    ]
    assert messages, (task, outcome.tasks)
    return messages


def _assert_rules_shown(outcome: Outcome, seed: Seed, rules: bytes | None, block: str) -> None:
    """The rules.md each review path puts in the model's input, for these base bytes.

    The snapshot path shows the base document whole as exactly ``block``, once in the
    reviewer's and once in the verifier's input (RFS §5.3, §8.1). The legacy envelope carries
    neither the block nor the base text; a changed rules.md reaches it as the head's code only.
    """
    if seed is Seed.LEGACY:
        assert all("<repository_rules" not in item for item in _texts(outcome))
        lines = [line for line in (rules or b"").decode().splitlines() if line.strip()]
        for item in _texts(outcome):
            outside = _outside_source_code(item)
            assert [line for line in lines if line in outside] == []
        return
    for task in (_REVIEW_TASK[seed], "VerificationOutput"):
        for message in _user_messages(outcome, task):
            assert _POLICY_RULES.findall(message) == [block], task


def _outside_source_code(message: str) -> str:
    """The envelope without the head's code: the diff and the excerpts of changed files."""
    return _SOURCE_CODE.sub("", message)


def _changed_files(message: str) -> str:
    """The one ``<changed_files>`` block of a reviewer or verifier input: the diff."""
    [diff] = re.findall(r"<changed_files>.*?</changed_files>", message, re.DOTALL)
    return str(diff)


def _assert_in_the_diff(outcome: Outcome, seed: Seed, text: str) -> None:
    """On the snapshot path the reviewer and the verifier get ``text`` as changed code."""
    for task in (_REVIEW_TASK[seed], "VerificationOutput"):
        for message in _user_messages(outcome, task):
            assert text in _changed_files(message), task


def _user_texts(outcome: Outcome) -> list[str]:
    """The input envelope of every call; the system prompts are the service's own text."""
    return [_user_message(json.loads(body)) for body in outcome.bodies]


@pytest.mark.integration
def test_missing_rules_md_lets_the_whole_diff_reach_the_model(stack: Stack, seed: Seed) -> None:
    outcome = _review(stack, _github(None), seed)

    _assert_published(outcome, seed)
    assert outcome.documents[RULES][:2] == ("missing", None)
    assert _rules_load(outcome)["presence"] == "missing"
    # Without a policy both changed files reach the model: the control of the valid case.
    for needle in (SERVICE_PATH, SERVICE_MARKER, CLIENT_PATH, CLIENT_MARKER):
        assert any(needle in body for body in outcome.bodies), needle
    _assert_rules_shown(outcome, seed, None, _ABSENT_RULES_BLOCK)


@pytest.mark.integration
def test_review_ignore_keeps_the_ignored_file_out_of_every_llm_call(
    stack: Stack, seed: Seed
) -> None:
    outcome = _review(stack, _github(VALID_RULES), seed)

    _assert_published(outcome, seed)
    assert outcome.documents[RULES][0] == "present"
    assert _rules_load(outcome)["presence"] == "present"
    # Every call: on the snapshot path the verifier's too (`_assert_rules_shown` needs one).
    assert all(CLIENT_MARKER not in body for body in outcome.bodies)
    assert all(CLIENT_PATH not in body for body in outcome.bodies)
    assert any(SERVICE_MARKER in body for body in outcome.bodies)
    # The base document whole, its review-ignore block included (§3.2), as policy only.
    _assert_rules_shown(outcome, seed, VALID_RULES, _VALID_RULES_BLOCK)
    if seed is Seed.V4:
        for task in (_REVIEW_TASK[seed], "VerificationOutput"):
            for message in _user_messages(outcome, task):
                diff = _changed_files(message)
                assert "Prefer small functions." not in diff
                assert "generated/**" not in diff
        # §5.3: the ignored file is counted, never named; the counter is in the review input.
        for message in _user_messages(outcome, _REVIEW_TASK[seed]):
            assert '<excluded_files count="1" />' in message


@pytest.mark.integration
def test_review_ignore_in_agents_md_is_ordinary_text(stack: Stack, seed: Seed) -> None:
    # §4.1, §4.3: only `.review/rules.md` is read for masks; in AGENTS.md the block is text.
    outcome = _review(stack, _github(None, agents=AGENTS_WITH_IGNORE_BLOCK), seed)

    _assert_published(outcome, seed)
    assert outcome.documents["AGENTS.md"][0] == "present"
    assert outcome.documents[RULES][:2] == ("missing", None)
    for path in (SERVICE_PATH, CLIENT_PATH):
        assert any(path in body for body in outcome.bodies), path
    _assert_rules_shown(outcome, seed, None, _ABSENT_RULES_BLOCK)
    if seed is Seed.V4:
        # Nothing is excluded, so the verifier is shown both changed files as well.
        for message in _user_messages(outcome, "VerificationOutput"):
            assert SERVICE_MARKER in message and CLIENT_MARKER in message


@pytest.mark.integration
def test_empty_rules_md_is_present_and_has_another_policy_than_a_missing_one(
    stack: Stack, seed: Seed
) -> None:
    missing = _review(stack, _github(None), seed)
    empty = _review(stack, _github(b""), seed)

    # AC2: the empty and the missing file never share the snapshot identity (§3.2).
    assert missing.policy_digest is not None and empty.policy_digest is not None
    assert empty.policy_digest != missing.policy_digest
    assert empty.documents[RULES] == ("present", EMPTY_SHA256, 0)
    assert _rules_load(empty)["presence"] == "present"
    _assert_published(empty, seed)
    assert missing.documents[RULES][:2] == ("missing", None)
    # The model tells them apart as well: an empty document, not an absent one.
    _assert_rules_shown(empty, seed, b"", _EMPTY_RULES_BLOCK)
    _assert_rules_shown(missing, seed, None, _ABSENT_RULES_BLOCK)


@pytest.mark.integration
@pytest.mark.parametrize("seed", [Seed.LEGACY], ids=str)
def test_rules_md_at_the_size_limit_is_present(stack: Stack, seed: Seed) -> None:
    # 65 536 bytes is the limit itself (§3.3): the document is loaded whole. The legacy
    # envelope does not show it, so the policy token budget is not checked there.
    outcome = _review(stack, _github(_AT_LIMIT), seed)

    _assert_published(outcome, seed)
    presence, _, byte_count = outcome.documents[RULES]
    assert (presence, byte_count) == ("present", 65_536)


_RULES_HALF_BLOCK = (
    '<repository_rules block_id="policy-rules" path=".review/rules.md">\n'
    f'<line n="1">{_RULES_HALF.decode()}</line>\n'
    "</repository_rules>"
)
_AGENTS_HALF_BLOCK = (
    '<project_conventions_document block_id="policy-agents" path="AGENTS.md">\n'
    f'<line n="1">{_AGENTS_HALF.decode()}</line>\n'
    "</project_conventions_document>"
)


@pytest.mark.integration
@pytest.mark.parametrize("seed", [Seed.V4], ids=str)
def test_policy_documents_together_at_the_budget_reach_the_model_whole(
    stack: Stack, seed: Seed
) -> None:
    # §3.3: 12 000 tokens of the two blocks is the budget itself, so the run goes on and
    # the model gets both documents whole.
    outcome = _review(stack, _github(_RULES_HALF, agents=_AGENTS_HALF), seed)

    _assert_published(outcome, seed)
    assert outcome.documents[RULES] == (
        "present",
        hashlib.sha256(_RULES_HALF).hexdigest(),
        len(_RULES_HALF),
    )
    assert outcome.documents["AGENTS.md"] == (
        "present",
        hashlib.sha256(_AGENTS_HALF).hexdigest(),
        len(_AGENTS_HALF),
    )
    _assert_rules_shown(outcome, seed, _RULES_HALF, _RULES_HALF_BLOCK)
    for task in (_REVIEW_TASK[seed], "VerificationOutput"):
        for message in _user_messages(outcome, task):
            assert message.count(_AGENTS_HALF_BLOCK) == 1, task


@pytest.mark.integration
def test_all_changes_ignored_skips_the_run_without_llm_or_review(stack: Stack, seed: Seed) -> None:
    outcome = _review(stack, _github(ALL_IGNORED_RULES), seed)

    assert (outcome.state, outcome.error_code) == ("skipped", "all_changes_ignored")
    assert outcome.documents[RULES][0] == "present"
    assert outcome.tasks == []
    assert outcome.reviews == []
    # PIPELINE_SPEC §7: `skipped` → `completed` / `skipped`.
    assert outcome.check_run["conclusion"] == "skipped"


@pytest.mark.integration
def test_rules_md_added_by_the_head_is_not_the_policy(stack: Stack, seed: Seed) -> None:
    outcome = _review(stack, _github(None, head_rules=IGNORE_EVERYTHING_RULES), seed)

    _assert_published(outcome, seed)
    assert outcome.documents[RULES][:2] == ("missing", None)
    # §5.3: the added text is no policy. The base has none, and its `**/*` is not applied.
    _assert_rules_shown(outcome, seed, None, _ABSENT_RULES_BLOCK)
    for path in (SERVICE_PATH, CLIENT_PATH):
        assert any(path in body for body in outcome.bodies), path
    # It is ordinary changed code (§5.3): no call shows it but as the head's code, and the
    # snapshot path does show it in the diff.
    assert all("**/*" not in _outside_source_code(item) for item in _user_texts(outcome))
    if seed is Seed.V4:
        _assert_in_the_diff(outcome, seed, "**/*")


@pytest.mark.integration
def test_rules_md_changed_by_the_head_keeps_the_base_policy(stack: Stack, seed: Seed) -> None:
    outcome = _review(stack, _github(PLAIN_RULES, head_rules=IGNORE_EVERYTHING_RULES), seed)

    _assert_published(outcome, seed)
    assert outcome.documents[RULES][:2] == ("present", hashlib.sha256(PLAIN_RULES).hexdigest())
    # §3.1, §5.3: the base text is the policy; the head's `**/*` is neither shown as policy
    # nor applied, and reaches the model only as the head's code of a changed file.
    _assert_rules_shown(outcome, seed, PLAIN_RULES, _PLAIN_RULES_BLOCK)
    for path in (SERVICE_PATH, CLIENT_PATH):
        assert any(path in body for body in outcome.bodies), path
    assert all("**/*" not in _outside_source_code(item) for item in _user_texts(outcome))
    if seed is Seed.V4:
        _assert_in_the_diff(outcome, seed, "**/*")


@pytest.mark.integration
def test_rules_md_changed_by_the_head_without_a_mask_keeps_the_base_text(
    stack: Stack, seed: Seed
) -> None:
    # The head adds a rule and no mask: the run is the same either way, so only the shown
    # policy and its stored hash tell the base revision from the head one (§3.1).
    outcome = _review(stack, _github(PLAIN_RULES, head_rules=PLAIN_RULES_WITH_A_HEAD_RULE), seed)

    _assert_published(outcome, seed)
    _assert_rules_shown(outcome, seed, PLAIN_RULES, _PLAIN_RULES_BLOCK)
    assert all(
        HEAD_ONLY_RULE not in block
        for item in _user_texts(outcome)
        for block in _POLICY_RULES.findall(item)
    )
    assert outcome.documents[RULES][:2] == ("present", hashlib.sha256(PLAIN_RULES).hexdigest())
    # §5.3: the head's rule is ordinary changed code.
    assert all(HEAD_ONLY_RULE not in _outside_source_code(item) for item in _user_texts(outcome))
    if seed is Seed.V4:
        _assert_in_the_diff(outcome, seed, HEAD_ONLY_RULE)
