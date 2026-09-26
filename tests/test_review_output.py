"""Public contract tests for strict model review output parsing."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.modules.reviews.application.review_output import (
    InvalidReviewOutput,
    ReviewOutput,
    parse_review_output,
)


def valid_output() -> dict[str, object]:
    return {
        "findings": [
            {
                "path": "app/example.py",
                "line": 12,
                "start_line": 10,
                "severity": "high",
                "category": "correctness",
                "title": "Missing transaction boundary",
                "body": "The provider call can leave the transaction open.",
                "suggestion": None,
                "confidence": 0.9,
                "rule_name": None,
            }
        ],
        "summary": {
            "problem": "A transaction can span a provider call.",
            "done_well": "The repository adapter is small.",
            "effort": "small",
        },
    }


def test_review_output_accepts_the_exact_contract() -> None:
    output = ReviewOutput.model_validate(valid_output())

    assert output.findings[0].line == 12
    assert output.summary.effort == "small"


def test_review_output_accepts_a_body_over_the_prompt_word_target() -> None:
    """The schema enforces the character ceiling, not prompt-level brevity."""
    data = valid_output()
    data["findings"][0]["body"] = " ".join(["word"] * 121)  # type: ignore[index]

    output = ReviewOutput.model_validate(data)

    assert len(output.findings[0].body.split()) == 121


def test_review_output_rejects_a_body_over_the_character_ceiling() -> None:
    data = valid_output()
    data["findings"][0]["body"] = "x" * 1201  # type: ignore[index]

    with pytest.raises(ValidationError):
        ReviewOutput.model_validate(data)


@pytest.mark.parametrize("start_line", [12, 13], ids=["equal", "after"])
def test_parser_rejects_non_strict_multiline_anchor_start(start_line: int) -> None:
    data = valid_output()
    data["findings"][0]["start_line"] = start_line  # type: ignore[index]

    with pytest.raises(InvalidReviewOutput):
        parse_review_output(data)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(unexpected=True),
        lambda value: value["findings"][0].update(extra="no"),
        lambda value: value["findings"][0].update(line=0),
        lambda value: value["findings"][0].update(severity="urgent"),
        lambda value: value["findings"].extend([value["findings"][0]] * 10),
        lambda value: value["findings"][0].update(title="x" * 81),
        lambda value: value["findings"][0].update(title="Ends with a period."),
        lambda value: value["findings"][0].update(title="Two\nlines"),
        lambda value: value["findings"][0].update(title="Two\u2028lines"),
        lambda value: value["summary"].update(effort="tiny"),
        lambda value: value["summary"].update(problem="First issue. Second issue."),
        lambda value: value["summary"].update(done_well="One good thing. Another. A third."),
    ],
)
def test_review_output_rejects_invalid_contract(mutate: object) -> None:
    data = valid_output()
    assert callable(mutate)
    mutate(data)

    with pytest.raises(ValidationError):
        ReviewOutput.model_validate(data)
