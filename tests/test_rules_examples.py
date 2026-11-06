"""The rule templates in examples/rules/ (#459), read as files through the service's parsers."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from app.modules.reviews.application.policy_documents import (
    MAX_DOCUMENT_BYTES,
    normalize_document,
)
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.policy_snapshot import RULES_DOCUMENT_PATH, PolicyDocument
from app.modules.reviews.application.prompt_budget import EstimatedTokenCounter
from app.modules.reviews.application.prompt_builder import PromptBuilder
from app.modules.reviews.application.review_ignore import parse_review_ignore
from tests.policy_snapshot_fakes import make_policy_snapshot

_CATALOG = Path(__file__).resolve().parents[1] / "examples" / "rules"
_OPENING = "```review-ignore\n"
_MAX_TEMPLATE_TOKENS = 6_000
# Literal masks, in the policy's code point order: a relabelled or deleted block parses to an
# empty policy without an error, so "no PolicyError" alone would not notice it.
_MASKS = {
    "go": ("**/*_gen.go", "**/zz_generated.*.go", "vendor/**"),
    "python-fastapi": ("**/*_pb2.py", "**/*_pb2_grpc.py", "clients/generated/**"),
    "security-baseline": ("**/*.map", "coverage/**"),
    "typescript-react": ("coverage/**", "src/types/api/**", "storybook-static/**"),
}


def _template(name: str) -> bytes:
    return (_CATALOG / name / ".review" / "rules.md").read_bytes()


def _document(raw: bytes) -> PolicyDocument:
    return normalize_document(RULES_DOCUMENT_PATH, raw, blob_oid="0" * 40)


def _masks(raw: bytes) -> tuple[str, ...]:
    return parse_review_ignore(_document(raw).text).patterns


def _tokens(raw: bytes) -> int:
    """The service's estimate of the block the model sees, not of the raw file."""
    snapshot = replace(make_policy_snapshot(), rules=_document(raw))
    return EstimatedTokenCounter().count(PromptBuilder.render_policy_rules(snapshot))


def _largest_merge() -> tuple[int, int]:
    """Bytes and tokens of the largest stack plus the baseline, an upper bound of their merge."""
    baseline = _template("security-baseline")
    stacks = [_template(name) for name in _MASKS if name != "security-baseline"]
    return (
        max(len(raw) for raw in stacks) + len(baseline),
        max(_tokens(raw) for raw in stacks) + _tokens(baseline),
    )


def _replace_last(raw: bytes, old: str, new: str) -> bytes:
    """Edit the block of a real template: it closes the file, so the last match is in the block."""
    before, separator, after = raw.decode("utf-8").rpartition(old)
    assert separator, f"{old!r} is not in the template"
    return f"{before}{new}{after}".encode()


def test_the_catalog_holds_exactly_the_known_templates() -> None:
    found = sorted(path.parent.parent.name for path in _CATALOG.glob("*/.review/rules.md"))

    assert found == sorted(_MASKS)


@pytest.mark.parametrize("name", _MASKS)
def test_a_template_opens_with_a_one_line_note(name: str) -> None:
    first, second = _template(name).decode("utf-8").split("\n")[:2]

    assert first.startswith("Template for ")
    assert "; adapt to your project: " in first
    assert second == ""


@pytest.mark.parametrize("name", _MASKS)
def test_a_template_passes_the_policy_parsers_with_its_masks(name: str) -> None:
    assert _masks(_template(name)) == _MASKS[name]


@pytest.mark.parametrize("name", _MASKS)
@pytest.mark.parametrize(
    ("old", "new", "code"),
    [
        ("```\n", "", PolicyErrorCode.IGNORE_BLOCK_UNCLOSED),
        (_OPENING, "```review-ignore extra\n", PolicyErrorCode.IGNORE_INFO_INVALID),
        (_OPENING, f"{_OPENING}!keep/**\n", PolicyErrorCode.IGNORE_PATTERN_INVALID),
        (_OPENING, f"{_OPENING}```\n\n{_OPENING}", PolicyErrorCode.IGNORE_BLOCK_DUPLICATE),
    ],
    ids=["unclosed", "parameter", "bad-mask", "second-block"],
)
def test_a_template_with_a_broken_block_fails_with_a_policy_error(
    name: str, old: str, new: str, code: PolicyErrorCode
) -> None:
    broken = _replace_last(_template(name), old, new)

    with pytest.raises(PolicyError) as error:
        _masks(broken)

    assert error.value.code == code


@pytest.mark.parametrize("name", _MASKS)
@pytest.mark.parametrize(
    ("old", "new"),
    [(_OPENING, "```Review-ignore\n"), (_OPENING, "```review-ignore-example\n")],
    ids=["capitalized", "other-label"],
)
def test_a_template_with_a_relabelled_block_loses_its_masks(name: str, old: str, new: str) -> None:
    assert _masks(_replace_last(_template(name), old, new)) == ()


@pytest.mark.parametrize("name", _MASKS)
def test_a_template_fits_the_document_and_token_limits(name: str) -> None:
    raw = _template(name)

    assert len(raw) <= MAX_DOCUMENT_BYTES
    assert _tokens(raw) <= _MAX_TEMPLATE_TOKENS


def test_the_largest_stack_with_the_security_baseline_leaves_half_of_the_budget() -> None:
    assert _largest_merge()[1] <= _MAX_TEMPLATE_TOKENS


def test_the_guide_states_the_measured_sizes() -> None:
    guide = (_CATALOG / "RULES_GUIDE.md").read_text(encoding="utf-8")

    for name in _MASKS:
        raw = _template(name)
        assert f"| `{name}` | {len(raw)} | {_tokens(raw)} |" in guide
    size, tokens = _largest_merge()
    assert f"| Largest stack + `security-baseline` | {size} | {tokens} |" in guide
