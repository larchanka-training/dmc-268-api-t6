"""The change summary agent's contract (#316 Part 2): prompt ``review.summary`` v1, the
``ChangeSummaryOutput`` schema and the gateway's parse path of its answer."""

from __future__ import annotations

import json
import re
from dataclasses import fields
from pathlib import Path

import pytest

from app.modules.reviews.application.change_summary import (
    ChangeSummaryRequest,
    build_change_summary_prompt,
    change_summary_system_prompt,
)
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.policy_snapshot import PINNED_PROMPT_CHECKSUMS
from app.modules.reviews.application.prompt_budget import EstimatedTokenCounter
from app.modules.reviews.application.prompt_builder import ChangedFile, DiffLine
from app.modules.reviews.infrastructure.llm.answers import (
    InvalidAnswer,
    validate_change_summary_answer,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT = (REPO_ROOT / "review" / "prompts" / "review.summary.v1.md").read_text(encoding="utf-8")
EXAMPLE_ANSWERS = re.findall(r"```json\n(.*?)\n```", PROMPT, re.DOTALL)
EXAMPLE_INPUTS = re.findall(r"```xml\n(.*?)\n```", PROMPT, re.DOTALL)


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


# ---------- the port: what the agent sees ----------

COUNTER = EstimatedTokenCounter()


def _file(path: str, lines: int, *, width: int = 60) -> ChangedFile:
    return ChangedFile(
        path=path,
        status="modified",
        lines=tuple(DiffLine(n, "added", "x" * width) for n in range(1, lines + 1)),
    )


def test_change_summary_request_has_the_title_and_the_changed_files_and_no_findings() -> None:
    assert [field.name for field in fields(ChangeSummaryRequest)] == ["title", "changed_files"]


def test_change_summary_system_prompt_is_the_pinned_file_without_its_frontmatter() -> None:
    system = change_summary_system_prompt()

    assert system == PROMPT.split("---\n", 2)[2].strip()
    assert system.startswith("You describe what a pull request changes.")


def test_change_summary_prompt_that_does_not_match_its_pin_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(PINNED_PROMPT_CHECKSUMS, "review.summary:v1", "0" * 64)

    with pytest.raises(PolicyError) as caught:
        change_summary_system_prompt()

    assert caught.value.code is PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR


def test_change_summary_input_within_its_budget_is_the_title_and_every_file() -> None:
    request = ChangeSummaryRequest(
        title="Clean up <old> runs",
        changed_files=(
            ChangedFile(
                path="app/jobs/retention.py",
                status="added",
                lines=(DiffLine(1, "added", "async def purge() -> int:"),),
            ),
        ),
    )

    prompt = build_change_summary_prompt(request, max_input_tokens=16_000, counter=COUNTER)

    assert prompt.system == change_summary_system_prompt()
    assert prompt.user == (
        "<pr_title>Clean up &lt;old&gt; runs</pr_title>\n"
        "<changed_files>\n"
        '<file path="app/jobs/retention.py" status="added" language="Python">\n'
        '<line n="1" type="added">async def purge() -&gt; int:</line>\n'
        "</file>\n"
        "</changed_files>"
    )


def test_change_summary_example_input_is_rendered_like_the_real_envelope() -> None:
    request = ChangeSummaryRequest(
        title="Clean up old runs",
        changed_files=(
            ChangedFile(
                path="app/jobs/retention.py",
                status="added",
                lines=(
                    DiffLine(
                        1,
                        "added",
                        "async def purge_stale_runs(session: AsyncSession, older_than: datetime)"
                        " -> int:",
                    ),
                    DiffLine(
                        2,
                        "added",
                        "    result = await session.execute(delete(RunDiff)"
                        ".where(RunDiff.created_at < older_than))",
                    ),
                    DiffLine(3, "added", "    return result.rowcount"),
                ),
            ),
            ChangedFile(
                path="app/worker.py",
                status="modified",
                lines=(
                    DiffLine(
                        40, "context", "    scheduler.add(sweep_no_ci, every=timedelta(minutes=2))"
                    ),
                    DiffLine(
                        41, "added", "    scheduler.add(purge_stale_runs, every=timedelta(days=1))"
                    ),
                ),
            ),
        ),
    )

    prompt = build_change_summary_prompt(request, max_input_tokens=16_000, counter=COUNTER)

    assert [prompt.user] == EXAMPLE_INPUTS


def test_change_summary_input_over_its_budget_cuts_the_diff_and_counts_the_rest() -> None:
    request = ChangeSummaryRequest(
        title="Three files",
        changed_files=(_file("app/a.py", 100), _file("app/b.py", 100), _file("app/c.py", 100)),
    )
    # The system prompt and about one and a half 100-line files of 60 characters a line.
    budget = COUNTER.count(change_summary_system_prompt()) + 4_000

    prompt = build_change_summary_prompt(request, max_input_tokens=budget, counter=COUNTER)

    assert COUNTER.count(prompt.system) + COUNTER.count(prompt.user) <= budget
    assert prompt.user.count('<line n="') < 200
    assert '<file path="app/a.py"' in prompt.user
    assert '<line n="100" type="added">' in prompt.user.split('<file path="app/b.py"')[0]
    cut = re.search(r"\[Showing lines 1-(\d+) of 100 total\.", prompt.user)
    assert cut is not None and 1 <= int(cut.group(1)) < 100
    assert "app/c.py" not in prompt.user
    assert prompt.user.endswith('</changed_files>\n<files_not_shown count="1" />')
    assert build_change_summary_prompt(request, max_input_tokens=budget, counter=COUNTER) == prompt


def test_change_summary_input_without_room_for_a_line_shows_no_file_and_does_not_fail() -> None:
    request = ChangeSummaryRequest(
        title="Three files",
        changed_files=(_file("app/a.py", 3), _file("app/b.py", 3), _file("app/c.py", 3)),
    )

    prompt = build_change_summary_prompt(request, max_input_tokens=10, counter=COUNTER)

    assert prompt.user == (
        "<pr_title>Three files</pr_title>\n"
        "<changed_files>\n"
        "\n"
        "</changed_files>\n"
        '<files_not_shown count="3" />'
    )
