"""The rule templates in examples/rules/ (#459), read as files through the service's parsers."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.modules.reviews.application.policy_documents import normalize_document
from app.modules.reviews.application.policy_snapshot import RULES_DOCUMENT_PATH
from app.modules.reviews.application.review_ignore import parse_review_ignore

_CATALOG = Path(__file__).resolve().parents[1] / "examples" / "rules"
# Literal masks, in the policy's code point order: a relabelled or deleted block parses to an
# empty policy without an error, so "no PolicyError" alone would not notice it.
_MASKS = {
    "go": ("**/*_gen.go", "**/zz_generated.*.go", "vendor/**"),
    "python-fastapi": ("**/*_pb2.py", "**/*_pb2_grpc.py", "clients/generated/**"),
    "typescript-react": ("coverage/**", "src/types/api/**", "storybook-static/**"),
}


def _template(name: str) -> bytes:
    return (_CATALOG / name / ".review" / "rules.md").read_bytes()


def _masks(raw: bytes) -> tuple[str, ...]:
    document = normalize_document(RULES_DOCUMENT_PATH, raw, blob_oid="0" * 40)
    return parse_review_ignore(document.text).patterns


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
