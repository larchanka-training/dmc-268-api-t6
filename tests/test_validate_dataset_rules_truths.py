"""Case validator: the rules.md sub-corpus - rule truths, exception anchors, arm lists, profile."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from tests.validate_dataset_fakes import (
    OTHER_FILE_PATCH,
    ROOT,
    RULES_PATCH,
    _case,
    _metadata,
    _rules_case,
    _rules_metadata,
    _run,
)


@pytest.mark.parametrize(
    ("keep_rule_truth", "keep_exception", "diagnostic"),
    [
        (True, False, None),
        (False, True, None),
        (
            False,
            False,
            "RUL-01: rules case: needs an origin rule truth or a must_not_flag anchor\n",
        ),
    ],
    ids=["rule-truth-only", "exception-only", "neither"],
)
def test_rules_case_needs_a_rule_truth_or_an_exception(
    tmp_path: Path, keep_rule_truth: bool, keep_exception: bool, diagnostic: str | None
) -> None:
    metadata = _rules_metadata()
    if not keep_rule_truth:
        del metadata["expected_findings"][1]["origin"]
        del metadata["expected_findings"][1]["rule_ref"]
    if not keep_exception:
        del metadata["must_not_flag"]
    dataset = _rules_case(tmp_path, metadata)

    result = _run(dataset, "--case", "RUL-01")

    assert result.stdout == (diagnostic or "OK RUL-01\n")
    assert result.returncode == (0 if diagnostic is None else 1)


@pytest.mark.parametrize(
    ("fields", "diagnostic"),
    [
        ({"line": 1}, "must_not_flag[0]: app/example.py:1 is not a new-side added line"),
        ({"start_line": 1, "line": 3}, "must_not_flag[0]: app/example.py:1 is not a new-side"),
        ({"start_line": 3, "line": 2}, "must_not_flag[0].start_line: must be < line"),
        ({"path": "app/other.py"}, "must_not_flag[0]: app/other.py:2 is not a new-side added"),
    ],
    ids=["line", "start-line", "inverted-range", "other-path"],
)
def test_exception_anchor_follows_the_truth_anchor_rules(
    tmp_path: Path, fields: dict[str, Any], diagnostic: str
) -> None:
    metadata = _rules_metadata()
    metadata["must_not_flag"][0].update(fields)
    dataset = _rules_case(tmp_path, metadata)

    result = _run(dataset, "--case", "RUL-01")

    assert result.returncode == 1
    assert f"RUL-01: {diagnostic}" in result.stdout


@pytest.mark.parametrize("category", ["security", "readability"], ids=["same", "other-category"])
def test_duplicate_exception_anchor_is_rejected(tmp_path: Path, category: str) -> None:
    metadata = _rules_metadata()
    metadata["must_not_flag"].append({**metadata["must_not_flag"][0], "category": category})
    dataset = _rules_case(tmp_path, metadata)

    result = _run(dataset, "--case", "RUL-01")

    assert result.returncode == 1
    assert result.stdout == (
        "RUL-01: must_not_flag[1]: duplicate exception anchor ('app/example.py', 2)\n"
    )


@pytest.mark.parametrize(
    ("truth_arms", "verdict", "extra"),
    [
        (None, "valid", ""),
        (
            ["malicious"],
            "malicious",
            "RUL-01: expected_findings[0].arms: list both valid and malicious or neither\n"
            "RUL-01: must_not_flag[0]: needs a none-arm truth at app/example.py:2\n",
        ),
    ],
    ids=["truth-in-all-arms", "truth-in-one-shared-arm"],
)
def test_exception_anchor_must_not_contradict_a_truth_in_a_shared_arm(
    tmp_path: Path, truth_arms: list[str] | None, verdict: str, extra: str
) -> None:
    metadata = _rules_metadata()
    if truth_arms is None:
        del metadata["expected_findings"][0]["arms"]
    else:
        metadata["expected_findings"][0]["arms"] = truth_arms
    dataset = _rules_case(tmp_path, metadata)

    result = _run(dataset, "--case", "RUL-01")

    assert result.returncode == 1
    assert result.stdout == (
        f"RUL-01: must_not_flag[0]: within 2 lines of expected_findings[0] in arm {verdict}\n"
        + extra
    )


RULE_ARMS = "expected_findings[1].arms: a rule-made truth lists valid and malicious, not none"
BOTH_ARMS = "list both valid and malicious or neither"


def _set(index: int, **fields: object) -> Any:
    def apply(record: dict[str, Any]) -> None:
        target = record["expected_findings"] if index >= 0 else record["must_not_flag"]
        item = target[index if index >= 0 else 0]
        for key, value in fields.items():
            if value is None:
                del item[key]
            else:
                item[key] = value

    return apply


def _generic_rule_truth(arms: list[str]) -> Any:
    def apply(record: dict[str, Any]) -> None:
        truth = record["expected_findings"][1]
        del truth["origin"], truth["rule_ref"]
        truth["arms"] = arms

    return apply


@pytest.mark.parametrize(
    ("change", "diagnostics"),
    [
        (_set(1, arms=["none", "valid", "malicious"]), [RULE_ARMS]),
        (_set(1, arms=None), [RULE_ARMS]),
        (_set(1, arms=["malicious"]), [RULE_ARMS, f"expected_findings[1].arms: {BOTH_ARMS}"]),
        (_set(1, arms=["valid"]), [f"expected_findings[1].arms: {BOTH_ARMS}"]),
        (_generic_rule_truth(["valid"]), [f"expected_findings[1].arms: {BOTH_ARMS}"]),
        (_generic_rule_truth(["none"]), []),
        (
            _set(-1, arms=["none", "valid", "malicious"]),
            [
                "must_not_flag[0]: within 2 lines of expected_findings[0] in arm none",
                "must_not_flag[0].arms: must not list none",
            ],
        ),
        (_set(0, line=3), ["must_not_flag[0]: needs a none-arm truth at app/example.py:2"]),
        (
            _set(0, path="app/other.py"),
            ["must_not_flag[0]: needs a none-arm truth at app/example.py:2"],
        ),
    ],
    ids=[
        "rule-truth-in-none",
        "rule-truth-in-all-arms",
        "rule-truth-malicious-only",
        "rule-truth-valid-only",
        "generic-truth-valid-only",
        "generic-truth-none-only",
        "exception-in-none",
        "twin-on-another-line",
        "twin-in-another-file",
    ],
)
def test_policy_arm_lists_follow_what_the_rules_add_or_exempt(
    tmp_path: Path, change: Any, diagnostics: list[str]
) -> None:
    metadata = _rules_metadata()
    change(metadata)
    dataset = _rules_case(tmp_path, metadata)
    (dataset / "cases/RUL-01/diff.patch").write_text(
        RULES_PATCH + OTHER_FILE_PATCH, encoding="utf-8"
    )

    result = _run(dataset, "--case", "RUL-01")

    assert result.stdout == ("".join(f"RUL-01: {line}\n" for line in diagnostics) or "OK RUL-01\n")
    assert result.returncode == (1 if diagnostics else 0)


@pytest.mark.parametrize(
    ("truth_line", "truth_path", "rejected"),
    [
        (2, "app/example.py", True),
        (3, "app/example.py", True),
        (4, "app/example.py", True),
        (5, "app/example.py", False),
        (2, "app/other.py", False),
    ],
    ids=["same-line-other-category", "one-line", "two-lines", "three-lines", "other-file"],
)
def test_exception_anchor_stays_out_of_the_window_of_a_shared_arm_truth(
    tmp_path: Path, truth_line: int, truth_path: str, rejected: bool
) -> None:
    # The rule-made truth (correctness, valid and malicious) moves next to the security anchor.
    metadata = _rules_metadata()
    metadata["expected_findings"][1].update(line=truth_line, path=truth_path)
    dataset = _rules_case(tmp_path, metadata)
    if truth_path == "app/other.py":
        patch = dataset / "cases/RUL-01/diff.patch"
        patch.write_text(RULES_PATCH + OTHER_FILE_PATCH, encoding="utf-8")

    result = _run(dataset, "--case", "RUL-01")

    if rejected:
        assert result.returncode == 1
        assert result.stdout == (
            "RUL-01: must_not_flag[0]: within 2 lines of expected_findings[1] in arm valid\n"
        )
    else:
        assert result.returncode == 0, result.stdout
        assert result.stdout == "OK RUL-01\n"


@pytest.mark.parametrize(
    "field",
    ["must_not_flag", "arms", "origin"],
)
def test_policy_arm_fields_need_the_case_policy(tmp_path: Path, field: str) -> None:
    metadata = _metadata()
    if field == "must_not_flag":
        # RULES_PATCH adds lines 2-6, so the anchor sits outside the window of the line-2 truth.
        metadata["must_not_flag"] = [
            {
                "path": "app/example.py",
                "start_line": None,
                "line": 6,
                "category": "correctness",
                "arms": ["valid"],
                "reason": "fixture",
            }
        ]
    elif field == "arms":
        metadata["expected_findings"][0]["arms"] = ["none"]
    else:
        metadata["expected_findings"][0]["origin"] = "generic"
    dataset = _case(tmp_path, metadata, patch=RULES_PATCH)

    result = _run(dataset, "--case", "SEC-01")

    assert result.returncode == 1
    assert result.stdout == (
        "SEC-01: policy: must_not_flag, arms and origin need the case policy arms\n"
    )


def test_profile_requires_final_mode(tmp_path: Path) -> None:
    dataset = _rules_case(tmp_path)

    result = _run(dataset, "--profile", "rules")

    assert result.returncode == 2
    assert "--profile requires --final" in result.stderr


def test_rules_profile_reports_its_own_minimums(tmp_path: Path) -> None:
    dataset = _rules_case(tmp_path)
    report = tmp_path / "rules-distribution.json"

    result = _run(dataset, "--final", "--profile", "rules", "--report-json", str(report))
    data = json.loads(report.read_text(encoding="utf-8"))

    assert result.returncode == 1
    assert data == {
        "schema_version": 1,
        "profile": "rules",
        "case_count": 1,
        "valid_case_count": 1,
        "class_counts": {"rules": 1},
        "language_counts": {"python": 1, "typescript": 0, "tsx": 0},
        "rule_truth_case_count": 1,
        "exception_case_count": 1,
        "passed": False,
        "errors": [
            "rules corpus: expected ≥8 cases, got 1",
            "rules corpus: needs ≥3 cases with a rule-made truth, got 1",
            "rules corpus: needs ≥3 cases with must_not_flag, got 1",
            "rules corpus: needs Python and TypeScript/React cases",
        ],
    }


def test_rules_profile_rejects_a_main_corpus_class(tmp_path: Path) -> None:
    dataset = _rules_case(tmp_path)
    main_case = _case(tmp_path / "main", _metadata()) / "cases" / "SEC-01"
    shutil.copytree(main_case, dataset / "cases" / "SEC-01")

    result = _run(dataset, "--final", "--profile", "rules")

    assert result.returncode == 1
    assert "rules corpus: every case must be class rules, got security\n" in result.stdout


def test_main_final_keeps_rules_cases_out_of_the_main_corpus(tmp_path: Path) -> None:
    dataset = _rules_case(tmp_path)

    result = _run(dataset, "--final")

    assert result.returncode == 1
    assert "final corpus: RUL-01 uses policy arms; validate it with --profile rules\n" in (
        result.stdout
    )


def test_committed_rules_corpus_passes_its_profile(tmp_path: Path) -> None:
    report = tmp_path / "rules-distribution.json"

    result = _run(
        ROOT / "test-prs-dataset" / "rules",
        "--final",
        "--profile",
        "rules",
        "--report-json",
        str(report),
    )
    data = json.loads(report.read_text(encoding="utf-8"))

    assert result.returncode == 0, result.stdout + result.stderr
    assert data == {
        "schema_version": 1,
        "profile": "rules",
        "case_count": 8,
        "valid_case_count": 8,
        "class_counts": {"rules": 8},
        "language_counts": {"python": 4, "typescript": 2, "tsx": 2},
        "rule_truth_case_count": 5,
        "exception_case_count": 4,
        "passed": True,
        "errors": [],
    }


def test_main_corpus_report_keeps_its_fields_without_a_profile(tmp_path: Path) -> None:
    report = tmp_path / "distribution.json"

    result = _run(ROOT / "test-prs-dataset", "--final", "--report-json", str(report))
    data = json.loads(report.read_text(encoding="utf-8"))

    assert result.returncode == 0, result.stdout + result.stderr
    assert sorted(data) == [
        "case_count",
        "class_counts",
        "critical_truth_count",
        "distinct_real_source_count",
        "errors",
        "language_counts",
        "passed",
        "real_case_count",
        "real_class_counts",
        "schema_version",
        "valid_case_count",
    ]
    assert sorted(data["class_counts"]) == ["clean", "logic", "resource", "security", "syntax"]
