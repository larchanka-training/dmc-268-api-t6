"""The change summary agent's contract (#316 Part 2): prompt ``review.summary`` v1, the
``ChangeSummaryOutput`` schema and the gateway's parse path of its answer."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.modules.reviews.infrastructure.llm.answers import (
    InvalidAnswer,
    validate_change_summary_answer,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT = (REPO_ROOT / "review" / "prompts" / "review.summary.v1.md").read_text(encoding="utf-8")
EXAMPLE_ANSWERS = re.findall(r"```json\n(.*?)\n```", PROMPT, re.DOTALL)


def test_change_summary_prompt_is_seeded_under_its_own_key_with_one_example_answer() -> None:
    assert PROMPT.startswith("---\nkey: review.summary\nversion: 1\n")
    assert len(EXAMPLE_ANSWERS) == 1


def test_change_summary_example_answer_passes_the_validator() -> None:
    (example,) = EXAMPLE_ANSWERS

    assert validate_change_summary_answer(example) == json.loads(example)


@pytest.mark.parametrize(
    ("answer", "errors"),
    [
        ('{"sentences": []}', ["sentences: [] should be non-empty"]),
        (
            '{"sentences": ["One.", "Two.", "Three.", "Four."]}',
            ["sentences: ['One.', 'Two.', 'Three.', 'Four.'] is too long"],
        ),
        (
            '{"sentences": ["Adds a job."], "risk": "low"}',
            ["(root): Additional properties are not allowed ('risk' was unexpected)"],
        ),
        (
            '{"summary": "Adds a job."}',
            [
                "(root): 'sentences' is a required property",
                "(root): Additional properties are not allowed ('summary' was unexpected)",
            ],
        ),
        (
            '{"sentences": [""]}',
            ["sentences/0: '' should be non-empty", "sentences/0: does not match"],
        ),
        ('{"sentences": ["Adds a job.\\nAnd more."]}', ["sentences/0: does not match"]),
        ('{"sentences": ["   "]}', ["sentences/0: does not match"]),
        (
            json.dumps({"sentences": ["x" * 201]}),
            ["sentences/0: is too long"],
        ),
        ('Here: {"sentences": ["Adds a job."]}', ["not valid JSON: Expecting value at line 1"]),
        ('["Adds a job."]', ["the answer must be one JSON object"]),
    ],
    ids=[
        "no-sentence",
        "four-sentences",
        "extra-field",
        "other-field",
        "empty-sentence",
        "line-break",
        "blank-sentence",
        "too-long",
        "prose-around",
        "not-an-object",
    ],
)
def test_change_summary_answer_outside_the_schema_is_invalid(
    answer: str, errors: list[str]
) -> None:
    with pytest.raises(InvalidAnswer) as caught:
        validate_change_summary_answer(answer)

    assert len(caught.value.errors) == len(errors)
    for message, expected in zip(caught.value.errors, errors, strict=True):
        location, _, verdict = expected.partition(": ")
        assert message.startswith(location)
        assert verdict in message
