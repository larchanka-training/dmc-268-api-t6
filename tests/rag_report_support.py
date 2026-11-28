"""Helpers the pinning tests of the RAG reports share (``tests/test_rag_report*.py``).

Markdown table reading and the set of answers the gate scores. A shared module, not a test
file: a ``test_*.py`` never imports from another one (``.agents/rules/backend.md`` §5).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from review.scripts.validate_findings import validate_review_output_v2


def cells(row: str) -> list[str]:
    """The cells of a Markdown table row, split on the ``|`` that is not escaped."""
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", row)[1:-1]]


def table(report: Path, header: str) -> list[list[str]]:
    """The body rows (as cells) of the table of ``report`` headed by a row starting ``header``."""
    lines = report.read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith(header))
    body = []
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        body.append(cells(line))
    return body


def carries(cell: str, value: str) -> bool:
    """The cell is the value, or the value followed by a parenthesised remark."""
    return cell == value or cell.startswith(value + " (")


def valid_answers(directory: Path) -> list[str]:
    """The ``<case>.json`` names the gate scores: answers the v2 validator accepts."""
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    return sorted(
        f"{case_id}.json"
        for case_id in manifest["responses"]
        if not validate_review_output_v2(
            json.loads((directory / f"{case_id}.json").read_text(encoding="utf-8"))
        )
    )
