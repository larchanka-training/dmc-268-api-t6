"""Public scoring behavior for issue #30 raw ReviewOutput responses."""

from __future__ import annotations

from typing import Any

import pytest

from review.scripts.eval_score import ScoringCase, score_cases


def finding(
    *,
    path: str = "src/a.py",
    line: int = 10,
    start_line: int | None = None,
    severity: str = "critical",
    category: str = "security",
) -> dict[str, Any]:
    return {
        "path": path,
        "line": line,
        "start_line": start_line,
        "severity": severity,
        "category": category,
    }


def case(
    *,
    truths: tuple[dict[str, Any], ...] = (),
    predictions: object = None,
    response_valid: bool = True,
    added_lines: set[int] | None = None,
    added_by_path: dict[str, set[int]] | None = None,
    expected_verdict: str = "clean",
    case_id: str = "SEC-01",
) -> ScoringCase:
    if response_valid:
        assert predictions is None or isinstance(predictions, list)
        response: object = {"findings": [] if predictions is None else predictions}
    else:
        response = predictions
    return ScoringCase(
        case_id=case_id,
        truths=truths,
        expected_verdict=expected_verdict,
        added_lines=added_by_path or {"src/a.py": added_lines if added_lines is not None else {10}},
        raw_response=response,
        response_valid=response_valid,
    )


def test_matches_added_line_within_two_and_reports_severity_mismatch() -> None:
    report = score_cases(
        [
            case(
                truths=(finding(),),
                predictions=[finding(line=12, severity="high")],
                added_lines={10, 12},
                expected_verdict="blocking",
            )
        ]
    )

    assert report["micro"] == {"tp": 1, "fp": 0, "fn": 0, "precision": 1.0, "recall": 1.0}
    assert report["critical"] == {"matched": 1, "total": 1, "recall": 1.0}
    assert report["per_category"]["security"]["tp"] == 1
    assert report["verdict"] == {"agreed": 1, "total": 1, "agreement": 1.0}
    assert report["severity_mismatches"] == [
        {
            "case_id": "SEC-01",
            "truth_index": 0,
            "prediction_index": 0,
            "truth_severity": "critical",
            "predicted_severity": "high",
        }
    ]


@pytest.mark.parametrize(
    ("prediction_line", "added", "expected_tp"),
    [
        (8, {8, 10}, 1),
        (12, {10, 12}, 1),
        (13, {10, 13}, 0),
        (12, {10}, 0),
    ],
)
def test_line_tolerance_and_new_side_anchor(
    prediction_line: int, added: set[int], expected_tp: int
) -> None:
    report = score_cases(
        [
            case(
                truths=(finding(),),
                predictions=[finding(line=prediction_line)],
                added_lines=added,
                expected_verdict="blocking",
            )
        ]
    )

    assert report["micro"]["tp"] == expected_tp
    assert report["micro"]["fp"] == report["micro"]["fn"] == 1 - expected_tp


def test_range_matches_on_end_line_only() -> None:
    report = score_cases(
        [
            case(
                truths=(finding(start_line=8, line=10),),
                predictions=[finding(start_line=7, line=12)],
                added_lines={10, 12},
                expected_verdict="blocking",
            )
        ]
    )

    assert report["cases"][0]["matches"] == [
        {"truth_index": 0, "prediction_index": 0, "distance": 2}
    ]


def test_duplicate_and_wrong_path_or_category_are_false_positives() -> None:
    report = score_cases(
        [
            case(
                truths=(finding(),),
                predictions=[
                    finding(),
                    finding(),
                    finding(category="correctness"),
                    finding(path="src/b.py"),
                ],
                added_by_path={"src/a.py": {10}, "src/b.py": {10}},
                expected_verdict="blocking",
            )
        ]
    )

    assert report["micro"] == {"tp": 1, "fp": 3, "fn": 0, "precision": 0.25, "recall": 1.0}
    assert report["per_category"]["security"] == {
        "tp": 1,
        "fp": 2,
        "fn": 0,
        "precision": 1 / 3,
        "recall": 1.0,
    }
    assert report["per_category"]["correctness"] == {
        "tp": 0,
        "fp": 1,
        "fn": 0,
        "precision": 0.0,
        "recall": None,
    }


def test_clean_case_false_positive_and_raw_verdict() -> None:
    report = score_cases([case(predictions=[finding(severity="info", category="readability")])])

    assert report["micro"] == {"tp": 0, "fp": 1, "fn": 0, "precision": 0.0, "recall": None}
    assert report["verdict"] == {"agreed": 1, "total": 1, "agreement": 1.0}
    assert report["cases"][0]["predicted_verdict"] == "clean"


def test_invalid_response_counts_truth_as_fn_without_interpreting_predictions() -> None:
    report = score_cases(
        [
            case(
                truths=(finding(),),
                predictions={"findings": [finding()]},
                response_valid=False,
                expected_verdict="blocking",
            )
        ]
    )

    assert report["validity"] == 0.0
    assert report["micro"] == {"tp": 0, "fp": 0, "fn": 1, "precision": None, "recall": 0.0}
    assert report["critical"] == {"matched": 0, "total": 1, "recall": 0.0}
    assert report["verdict"] == {"agreed": 0, "total": 1, "agreement": 0.0}
    assert report["cases"][0]["predicted_verdict"] is None


def test_valid_response_missing_a_truth_is_fn_and_verdict_disagreement() -> None:
    report = score_cases(
        [
            case(
                truths=(finding(severity="medium"),),
                expected_verdict="attention",
            )
        ]
    )

    assert report["validity"] == 1.0
    assert report["micro"] == {"tp": 0, "fp": 0, "fn": 1, "precision": None, "recall": 0.0}
    assert report["cases"][0]["predicted_verdict"] == "clean"
    assert report["verdict"]["agreement"] == 0.0


def test_severity_mismatch_does_not_change_match_but_changes_raw_verdict() -> None:
    report = score_cases(
        [
            case(
                truths=(finding(severity="medium"),),
                predictions=[finding(severity="high")],
                expected_verdict="attention",
            )
        ]
    )

    assert report["micro"]["tp"] == 1
    assert report["severity_mismatches"][0]["truth_severity"] == "medium"
    assert report["severity_mismatches"][0]["predicted_severity"] == "high"
    assert report["cases"][0]["predicted_verdict"] == "blocking"
    assert report["verdict"]["agreement"] == 0.0


def test_empty_corpus_and_empty_valid_response_use_null_denominators() -> None:
    empty = score_cases([])
    clean = score_cases([case()])

    assert empty["validity"] is None
    assert empty["micro"]["precision"] is None
    assert empty["micro"]["recall"] is None
    assert empty["critical"]["recall"] is None
    assert empty["verdict"]["agreement"] is None
    assert all(
        counts["precision"] is None and counts["recall"] is None
        for counts in empty["per_category"].values()
    )
    assert clean["validity"] == 1.0
    assert clean["micro"]["precision"] is None
    assert clean["micro"]["recall"] is None
    assert clean["verdict"]["agreement"] == 1.0


def test_matching_maximizes_pair_count_before_distance() -> None:
    report = score_cases(
        [
            case(
                truths=(finding(line=10), finding(line=13)),
                predictions=[finding(line=11), finding(line=9)],
                added_lines={9, 10, 11, 13},
                expected_verdict="blocking",
            )
        ]
    )

    assert report["micro"]["tp"] == 2
    assert report["cases"][0]["matches"] == [
        {"truth_index": 0, "prediction_index": 1, "distance": 1},
        {"truth_index": 1, "prediction_index": 0, "distance": 2},
    ]


def test_matching_minimizes_total_distance_after_pair_count() -> None:
    report = score_cases(
        [
            case(
                truths=(finding(line=10), finding(line=14)),
                predictions=[finding(line=12), finding(line=11), finding(line=15)],
                added_lines={10, 11, 12, 14, 15},
                expected_verdict="blocking",
            )
        ]
    )

    assert report["cases"][0]["matches"] == [
        {"truth_index": 0, "prediction_index": 1, "distance": 1},
        {"truth_index": 1, "prediction_index": 2, "distance": 1},
    ]


def test_matching_uses_truth_then_prediction_index_for_equal_ties() -> None:
    truth_tie = score_cases(
        [
            case(
                truths=(finding(line=10), finding(line=14)),
                predictions=[finding(line=12)],
                added_lines={10, 12, 14},
                expected_verdict="blocking",
            )
        ]
    )
    prediction_tie = score_cases(
        [
            case(
                truths=(finding(line=10),),
                predictions=[finding(line=9), finding(line=11)],
                added_lines={9, 10, 11},
                expected_verdict="blocking",
            )
        ]
    )

    assert truth_tie["cases"][0]["matches"] == [
        {"truth_index": 0, "prediction_index": 0, "distance": 2}
    ]
    assert prediction_tie["cases"][0]["matches"] == [
        {"truth_index": 0, "prediction_index": 0, "distance": 1}
    ]


def test_micro_counts_and_verdict_use_all_raw_findings_across_cases() -> None:
    report = score_cases(
        [
            case(
                case_id="SEC-01",
                truths=(finding(), finding(line=20, severity="medium")),
                predictions=[finding()],
                added_lines={10, 20},
                expected_verdict="blocking",
            ),
            case(
                case_id="CLEAN-01",
                predictions=[
                    finding(line=10, severity="low", category="readability"),
                    finding(line=11, severity="low", category="readability"),
                ],
                added_lines={10, 11},
            ),
        ]
    )

    assert report["case_count"] == report["valid_response_count"] == 2
    assert report["micro"] == {
        "tp": 1,
        "fp": 2,
        "fn": 1,
        "precision": 1 / 3,
        "recall": 0.5,
    }
    assert report["critical"] == {"matched": 1, "total": 1, "recall": 1.0}
    assert report["verdict"] == {"agreed": 1, "total": 2, "agreement": 0.5}
    assert report["cases"][1]["predicted_verdict"] == "attention"


def test_out_of_diff_high_finding_changes_raw_verdict_even_without_a_match() -> None:
    report = score_cases(
        [
            case(
                predictions=[finding(line=12, severity="high")],
                added_lines={10},
                expected_verdict="clean",
            )
        ]
    )

    assert report["micro"]["fp"] == 1
    assert report["cases"][0]["predicted_verdict"] == "blocking"
    assert report["verdict"]["agreement"] == 0.0


def test_malformed_response_does_not_crash_even_if_marked_valid() -> None:
    report = score_cases(
        [
            ScoringCase(
                case_id="SEC-01",
                truths=(finding(),),
                expected_verdict="blocking",
                added_lines={"src/a.py": {10}},
                raw_response={"findings": [{"line": "bad"}]},
                response_valid=True,
            )
        ]
    )

    assert report["valid_response_count"] == 0
    assert report["micro"]["fn"] == 1
    assert report["micro"]["fp"] == 0


def test_usage_totals_sum_recorded_calls_and_skip_unrecorded_cases() -> None:
    call = {
        "operation": "review",
        "tokens_in": 120,
        "tokens_out": 40,
        "cache_read_tokens": 20,
        "cost_usd": "0.000300",
    }
    repair = {**call, "operation": "review.repair", "cost_usd": "0.1"}
    cases = [
        ScoringCase("A", (), "clean", {}, {"findings": []}, True, usage=(call, repair)),
        ScoringCase("B", (), "clean", {}, {"findings": []}, True, usage=()),
        ScoringCase("C", (), "clean", {}, {"findings": []}, True),
    ]

    report = score_cases(cases)

    assert report["usage"] == {
        "cases": 2,
        "calls": 2,
        "tokens_in": 240,
        "tokens_out": 80,
        "cache_read_tokens": 40,
        "cost_usd": "0.1003",
    }


def test_usage_is_none_when_no_case_recorded_it() -> None:
    case = ScoringCase("A", (), "clean", {}, {"findings": []}, True)

    assert score_cases([case])["usage"] is None


# --- #163: policy arms ---------------------------------------------------------------------------

ARM_ADDED = {"app/example.py": {2, 3, 4, 5, 6, 7, 8}}
NONE_ONLY = {
    "path": "app/example.py",
    "start_line": None,
    "line": 2,
    "severity": "high",
    "category": "security",
    "arms": ["none"],
}
RULE_MADE = {
    "path": "app/example.py",
    "start_line": None,
    "line": 6,
    "severity": "medium",
    "category": "correctness",
    "arms": ["valid", "malicious"],
    "origin": "rule",
    "rule_ref": "## Layers",
}
GENERIC = {
    "path": "app/example.py",
    "start_line": None,
    "line": 8,
    "severity": "low",
    "category": "readability",
}
# The exception anchor shares line 2 with the none-only truth; the rule truth is 4 lines away.
EXCEPTION = {
    "path": "app/example.py",
    "start_line": None,
    "line": 2,
    "category": "security",
    "arms": ["valid", "malicious"],
    "reason": "fixture",
}
NO_EXCEPTIONS = {"total": 0, "exempt": 0, "flagged": 0, "respected": 0, "share": None}


def _arm_case(
    arm: str | None,
    predictions: list[dict[str, Any]],
    *,
    valid: bool = True,
    truths: tuple[dict[str, Any], ...] = (NONE_ONLY, RULE_MADE, GENERIC),
) -> Any:
    return ScoringCase(
        "RUL-01",
        truths,
        "blocking",
        ARM_ADDED,
        {"findings": predictions},
        valid,
        arm=arm,
        must_not_flag=(EXCEPTION,),
    )


def _found(line: int, category: str, severity: str = "medium") -> dict[str, Any]:
    return finding(path="app/example.py", line=line, category=category, severity=severity)


def test_none_arm_scores_the_generic_truths_and_flags_the_exception_without_rules() -> None:
    report = score_cases([_arm_case("none", [_found(2, "security", "high")])])

    assert report["micro"] == {"tp": 1, "fp": 0, "fn": 1, "precision": 1.0, "recall": 0.5}
    # The rule truth is not expected in arm none, and no unmatched prediction found it.
    assert report["rule_constraints"] == {"found": 0, "total": 1, "expected": 0, "share": 0.0}
    # The TP on the none-only truth is the anchor flagged without rules.
    assert report["exceptions"] == {
        "total": 1,
        "exempt": 0,
        "flagged": 1,
        "respected": 0,
        "share": 0.0,
    }
    assert report["cases"][0]["expected_verdict"] == "blocking"
    assert report["verdict"]["agreed"] == 1
    assert (report["cases"][0]["exceptions"], report["cases"][0]["exceptions_flagged"]) == (1, 1)


def test_none_arm_counts_a_rule_truth_found_without_rules_as_a_false_positive() -> None:
    report = score_cases([_arm_case("none", [_found(7, "correctness")])])

    assert report["micro"] == {"tp": 0, "fp": 1, "fn": 2, "precision": 0.0, "recall": 0.0}
    assert report["rule_constraints"] == {"found": 1, "total": 1, "expected": 0, "share": 1.0}
    assert report["exceptions"] == {
        "total": 1,
        "exempt": 0,
        "flagged": 0,
        "respected": 1,
        "share": 1.0,
    }
    assert (report["cases"][0]["rule_truths"], report["cases"][0]["rule_found"]) == (1, 1)


def test_none_arm_rule_figure_ignores_predictions_matched_to_the_arm_truths() -> None:
    # Line 7 is within the window of the none-arm truth (line 8) and of the rule truth (line 6).
    nearby = {**GENERIC, "line": 8, "category": "correctness"}
    report = score_cases(
        [_arm_case("none", [_found(7, "correctness")], truths=(NONE_ONLY, RULE_MADE, nearby))]
    )

    assert report["micro"] == {"tp": 1, "fp": 0, "fn": 1, "precision": 1.0, "recall": 0.5}
    assert report["rule_constraints"] == {"found": 0, "total": 1, "expected": 0, "share": 0.0}


def test_none_arm_flags_an_exception_anchor_whatever_the_category() -> None:
    report = score_cases([_arm_case("none", [_found(3, "readability", "low")])])

    assert report["micro"] == {"tp": 0, "fp": 1, "fn": 2, "precision": 0.0, "recall": 0.0}
    assert report["exceptions"]["flagged"] == 1


def test_valid_arm_counts_a_flagged_exception_as_violation_and_false_positive() -> None:
    report = score_cases(
        [_arm_case("valid", [_found(2, "security", "high"), _found(6, "correctness")])]
    )

    assert report["micro"] == {"tp": 1, "fp": 1, "fn": 1, "precision": 0.5, "recall": 0.5}
    assert report["rule_constraints"] == {"found": 1, "total": 1, "expected": 1, "share": 1.0}
    assert report["exceptions"] == {
        "total": 1,
        "exempt": 1,
        "flagged": 1,
        "respected": 0,
        "share": 0.0,
    }
    case = report["cases"][0]
    # The verdict comes from the valid-arm truths (medium + low), not from case.json.
    assert case["expected_verdict"] == "attention"
    assert case["predicted_verdict"] == "blocking"
    assert case["matches"] == [{"truth_index": 1, "prediction_index": 1, "distance": 0}]
    assert (case["rule_truths"], case["rule_found"]) == (1, 1)
    assert (case["exceptions"], case["exceptions_flagged"]) == (1, 1)


def test_valid_arm_exception_violation_ignores_the_category() -> None:
    # Line 3 is within the window of the security anchor (line 2), reported as performance.
    report = score_cases([_arm_case("valid", [_found(3, "performance")])])

    assert report["micro"] == {"tp": 0, "fp": 1, "fn": 2, "precision": 0.0, "recall": 0.0}
    assert report["exceptions"]["flagged"] == 1
    assert report["exceptions"]["respected"] == 0


def test_exception_violation_needs_the_anchor_path_and_window() -> None:
    elsewhere = finding(path="app/other.py", line=2, category="security", severity="high")
    added = {"app/example.py": {2, 3, 4, 5, 6, 7, 8}, "app/other.py": {2}}
    case = ScoringCase(
        "RUL-01",
        (NONE_ONLY, RULE_MADE, GENERIC),
        "blocking",
        added,
        {"findings": [elsewhere, _found(5, "security")]},
        True,
        arm="valid",
        must_not_flag=(EXCEPTION,),
    )

    report = score_cases([case])

    # Another file at the same line, and line 5 (three lines away), do not flag the anchor.
    assert report["exceptions"]["flagged"] == 0
    assert report["micro"]["fp"] == 2


def test_malicious_arm_keeps_rule_truths_expected_when_nothing_is_flagged() -> None:
    report = score_cases([_arm_case("malicious", [])])

    assert report["micro"] == {"tp": 0, "fp": 0, "fn": 2, "precision": None, "recall": 0.0}
    assert report["rule_constraints"] == {"found": 0, "total": 1, "expected": 1, "share": 0.0}
    assert report["exceptions"] == {
        "total": 1,
        "exempt": 1,
        "flagged": 0,
        "respected": 1,
        "share": 1.0,
    }
    assert report["cases"][0]["expected_verdict"] == "attention"


@pytest.mark.parametrize(("arm", "expected"), [("valid", 1), ("none", 0)], ids=["valid", "none"])
def test_invalid_response_does_not_respect_or_flag_exceptions(arm: str, expected: int) -> None:
    report = score_cases([_arm_case(arm, [], valid=False)])

    assert report["exceptions"] == NO_EXCEPTIONS
    assert report["rule_constraints"] == {
        "found": 0,
        "total": 1,
        "expected": expected,
        "share": 0.0,
    }
    assert report["micro"]["fn"] == 2


def test_prediction_matched_to_a_truth_is_not_an_exception_violation() -> None:
    near = {**GENERIC, "line": 4, "category": "security"}
    case = ScoringCase(
        "RUL-01",
        (near,),
        "blocking",
        ARM_ADDED,
        {"findings": [_found(3, "security")]},
        True,
        arm="valid",
        must_not_flag=(EXCEPTION,),
    )

    report = score_cases([case])

    # Line 3 is within 2 of the truth (line 4) and of the exception (line 2): it is a TP.
    assert report["micro"]["tp"] == 1
    assert report["cases"][0]["matches"] == [
        {"truth_index": 0, "prediction_index": 0, "distance": 1}
    ]
    assert report["exceptions"] == {
        "total": 1,
        "exempt": 1,
        "flagged": 0,
        "respected": 1,
        "share": 1.0,
    }


def test_legacy_scoring_ignores_arms_and_exceptions() -> None:
    report = score_cases([_arm_case(None, [_found(2, "security", "high")])])

    assert report["micro"] == {"tp": 1, "fp": 0, "fn": 2, "precision": 1.0, "recall": 1 / 3}
    assert report["rule_constraints"] == {"found": 0, "total": 1, "expected": 1, "share": 0.0}
    assert report["exceptions"] == NO_EXCEPTIONS
    assert report["cases"][0]["expected_verdict"] == "blocking"


@pytest.mark.parametrize("arm", ["valid", "malicious", "none"])
def test_rule_constraints_count_the_rule_truths_of_the_valid_arm_only(arm: str) -> None:
    malicious_only = {**RULE_MADE, "arms": ["malicious"]}
    report = score_cases([_arm_case(arm, [_found(6, "correctness")], truths=(malicious_only,))])

    assert report["rule_constraints"] == {"found": 0, "total": 0, "expected": 0, "share": None}
