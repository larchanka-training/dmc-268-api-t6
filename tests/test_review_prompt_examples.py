"""Few-shot examples of review.system.v2 are valid answers (Р-6, docs/PIPELINE_SPEC.md §9)."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from app.modules.reviews.infrastructure.llm.answers import parse_review_answer

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPTS = REPO_ROOT / "review" / "prompts"
V1 = (PROMPTS / "review.system.v1.md").read_text(encoding="utf-8")
V2 = (PROMPTS / "review.system.v2.md").read_text(encoding="utf-8")
EXAMPLES = V2.split("\n## 11. Examples\n", 1)[1]
ANSWERS = re.findall(r"```json\n(.*?)\n```", EXAMPLES, re.DOTALL)
INPUTS = re.findall(r"```xml\n(.*?)\n```", EXAMPLES, re.DOTALL)
_DIFF_MARKER = re.compile(r"^(\+|-|@@)", re.MULTILINE)


# review.system.v1.md as seeded; a prompt version is immutable (Р-6).
V1_SHA256 = "1964c855aa9cc4659e5b7ec72e90eb419b0ce1415359b3808224e08f9145e685"


def test_v2_adds_two_or_three_example_pairs_and_keeps_v1_intact() -> None:
    assert 2 <= len(ANSWERS) == len(INPUTS) <= 3
    assert hashlib.sha256((PROMPTS / "review.system.v1.md").read_bytes()).hexdigest() == V1_SHA256
    assert V2.startswith("---\nkey: review.system\nversion: 2\n")
    assert "## 11. Examples" not in V1


def test_every_example_answer_passes_the_gateway_parse_path() -> None:
    outputs = [parse_review_answer(answer) for answer in ANSWERS]

    assert any(not output.findings for output in outputs)
    assert any(finding.start_line is not None for output in outputs for finding in output.findings)
    assert any(finding.start_line is None for output in outputs for finding in output.findings)


def test_example_inputs_are_rendered_like_the_real_envelope() -> None:
    for xml in INPUTS:
        assert xml.startswith("<changed_files>\n<file ")
        assert xml.endswith("</file>\n</changed_files>")
        for content in re.findall(r'<line n="\d+" type="\w+">(.*)</line>', xml):
            assert "<" not in content and ">" not in content  # escaped as in PromptBuilder


def test_example_suggestions_are_drop_in_replacements_of_the_anchored_lines() -> None:
    for xml, answer in zip(INPUTS, ANSWERS, strict=True):
        numbered = {
            int(number)
            for number, kind in re.findall(r'<line n="(\d+)" type="(added|context)">', xml)
        }
        for finding in json.loads(answer)["findings"]:
            suggestion = finding["suggestion"]
            assert finding["line"] in numbered
            assert finding["start_line"] is None or finding["start_line"] in numbered
            if suggestion is None:
                continue
            assert "```" not in suggestion
            assert _DIFF_MARKER.search(suggestion) is None
            first_line = finding["start_line"] or finding["line"]
            assert len(suggestion.splitlines()) == finding["line"] - first_line + 1


def test_examples_are_not_taken_from_the_proof_run_fixtures_or_the_eval_dataset() -> None:
    sources = [REPO_ROOT / "review" / "examples" / "sample.diff"]
    sources += [path for path in (REPO_ROOT / "test-prs-dataset").rglob("*") if path.is_file()]
    corpus = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in sources)
    assert len(sources) > 1
    for xml in INPUTS:
        path = re.search(r'<file path="([^"]+)"', xml)
        assert path is not None
        assert path.group(1) not in corpus
