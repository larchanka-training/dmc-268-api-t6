#!/usr/bin/env python3
"""Verdict of the second pass of scripts/skylos_quality.sh (#263): changed functions.

Usage: scripts/skylos_changed_functions.py REPORT RULES

REPORT is the JSON report (``--format json-ci``) of a Skylos ``--diff-base`` scan, RULES a
comma-separated list of function metric rules. Skylos compares each metric of a changed file with
the merge base and marks it ``introduced`` (over the limit now, not at the base), ``worsened``
(over the limit at the base and worse now) or ``unverified`` (the file has no version at the base:
a new file or a moved one). Only the first two fail: a new file is checked line by line by the
first pass, and a moved legacy function is not a new finding. Exit codes: 0 passed, 1 a changed
function over a limit, 2 unreadable report.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

FAILING_CHANGES = ("introduced", "worsened")


def _relative(path: object) -> str:
    try:
        return str(Path(str(path)).relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def _read_findings(report: str) -> list[dict[str, object]]:
    """The ``code_health`` findings of the report; raises on an unreadable report."""
    findings = json.loads(Path(report).read_text(encoding="utf-8"))["code_health"]
    if not isinstance(findings, list) or not all(isinstance(item, dict) for item in findings):
        raise TypeError("code_health is not a list of objects")
    return findings


def _failing(findings: list[dict[str, object]], rules: set[str]) -> list[str]:
    return [
        f"{finding['rule_id']}  {_relative(finding.get('file'))}:{finding.get('line')}  "
        f"{finding.get('message')} ({finding['code_health_change']})"
        for finding in findings
        if finding.get("rule_id") in rules and finding.get("code_health_change") in FAILING_CHANGES
    ]


def _write_summary(headline: str, failing: list[str]) -> None:
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as output:
            output.write("\n".join(["", f"### {headline}", *(f"- `{line}`" for line in failing)]))
            output.write("\n")


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: scripts/skylos_changed_functions.py REPORT RULES", file=sys.stderr)
        return 2
    try:
        findings = _read_findings(argv[1])
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Changed functions: cannot read {argv[1]}: {error!r}", file=sys.stderr)
        return 2
    failing = _failing(findings, set(argv[2].split(",")))
    verdict = "FAILED" if failing else "PASSED"
    headline = f"Changed functions over a limit: {verdict} ({len(failing)})"
    print("\n".join([headline, *failing]))
    _write_summary(headline, failing)
    return 1 if failing else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
