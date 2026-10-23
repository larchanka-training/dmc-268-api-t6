"""Deterministic one-to-one scoring of raw ReviewOutput against curated truth.

The caller validates each raw response with validate_findings.py and supplies
new-side added-line sets from the case patch. This module does not call a model,
read files, or post-process findings.

Policy arms (#163): with ``arm`` set, only truths listed for that arm (or without
``arms``) count for P/R, and the expected verdict is derived from them. Two figures cover
every arm with the same denominator, so arms compare directly:

- ``rule_constraints``: the ``origin: rule`` truths of the ``valid`` arm. Where the arm
  lists one, it is found when it is a TP; where it does not (arm ``none``), it is found
  when a prediction that no truth of the arm matched would match it, the informational
  "found without rules". ``expected`` counts the ones the arm lists. An invalid response
  finds nothing.
- ``exceptions``: every ``must_not_flag`` anchor, over valid responses only (an invalid
  response flags nothing, so it would respect every exception vacuously). A prediction on
  the anchor's path within the line window flags it, whatever the category. Where the arm
  lists the anchor (``exempt``), only a prediction that no truth matched flags it, an
  exception violation that stays a false positive; where it does not (arm ``none``), the
  anchor is a generic truth of that arm and any prediction flags it, "flagged without
  rules". ``respected`` is ``total - flagged``.

``arm=None`` (legacy captures) scores every truth and no exception, exactly as before.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from functools import cache
from typing import Any

CATEGORIES = ("security", "correctness", "performance", "readability")
SEVERITIES = ("critical", "high", "medium", "low", "info")


@dataclass(frozen=True)
class ScoringCase:
    case_id: str
    truths: tuple[dict[str, Any], ...]
    expected_verdict: str
    added_lines: Mapping[str, set[int]]
    raw_response: object
    response_valid: bool
    # Per-call usage recorded with the capture (#163); ``None`` when it was not recorded.
    usage: tuple[Mapping[str, Any], ...] | None = None
    arm: str | None = None
    must_not_flag: tuple[dict[str, Any], ...] = ()


def _in_arm(item: Mapping[str, Any], arm: str | None) -> bool:
    return arm is None or "arms" not in item or arm in item["arms"]


def _rule_made(truth: Mapping[str, Any]) -> bool:
    """A truth of ``rule_constraints``: rule-made and expected in the ``valid`` arm."""
    return truth.get("origin") == "rule" and _in_arm(truth, "valid")


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _predictions(case: ScoringCase) -> list[dict[str, Any]] | None:
    """Return minimally safe findings, or None for an invalid raw response."""
    if not case.response_valid or not isinstance(case.raw_response, dict):
        return None
    items = case.raw_response.get("findings")
    if not isinstance(items, list):
        return None
    predictions: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            return None
        path = item.get("path")
        line = item.get("line")
        start_line = item.get("start_line")
        if (
            not isinstance(path, str)
            or not path
            or type(line) is not int
            or line < 1
            or (
                start_line is not None
                and (type(start_line) is not int or start_line < 1 or start_line >= line)
            )
            or item.get("severity") not in SEVERITIES
            or item.get("category") not in CATEGORIES
        ):
            return None
        predictions.append(item)
    return predictions


def _verdict(findings: Sequence[dict[str, Any]]) -> str:
    severities = {item["severity"] for item in findings}
    if severities & {"critical", "high"}:
        return "blocking"
    if severities & {"medium", "low"}:
        return "attention"
    return "clean"


def _usage_totals(cases: Sequence[ScoringCase]) -> dict[str, Any] | None:
    """Sum recorded call usage; ``None`` when no case recorded any (older captures)."""
    recorded = [case.usage for case in cases if case.usage is not None]
    if not recorded:
        return None
    calls = [call for usage in recorded for call in usage]
    cost = sum((Decimal(call["cost_usd"]) for call in calls), Decimal(0))
    return {
        "cases": len(recorded),
        "calls": len(calls),
        "tokens_in": sum(call["tokens_in"] for call in calls),
        "tokens_out": sum(call["tokens_out"] for call in calls),
        "cache_read_tokens": sum(call["cache_read_tokens"] for call in calls),
        "cost_usd": format(cost.normalize(), "f"),
    }


def _matches(
    truths: tuple[dict[str, Any], ...],
    predictions: list[dict[str, Any]],
    added_lines: Mapping[str, set[int]],
    *,
    match_category: bool = True,
) -> tuple[tuple[int, int], ...]:
    """Maximize pair count, minimize distance, then choose lexicographic pairs.

    ``match_category=False`` pairs exception anchors on path and line window only.
    """

    @cache
    def solve(
        truth_index: int, used_predictions: int
    ) -> tuple[int, int, tuple[tuple[int, int], ...]]:
        if truth_index == len(truths):
            return 0, 0, ()
        best = solve(truth_index + 1, used_predictions)
        truth = truths[truth_index]
        for prediction_index, prediction in enumerate(predictions):
            if used_predictions & (1 << prediction_index):
                continue
            if (
                prediction["path"] != truth["path"]
                or (match_category and prediction["category"] != truth["category"])
                or prediction["line"] not in added_lines.get(prediction["path"], set())
            ):
                continue
            distance = abs(prediction["line"] - truth["line"])
            if distance > 2:
                continue
            count, later_distance, pairs = solve(
                truth_index + 1, used_predictions | (1 << prediction_index)
            )
            candidate = (
                count + 1,
                later_distance + distance,
                ((truth_index, prediction_index), *pairs),
            )
            if (-candidate[0], candidate[1], candidate[2]) < (-best[0], best[1], best[2]):
                best = candidate
        return best

    return solve(0, 0)[2]


def score_cases(cases: Sequence[ScoringCase]) -> dict[str, Any]:
    """Score a corpus; invalid responses contribute only false negatives."""
    category_counts = {name: {"tp": 0, "fp": 0, "fn": 0} for name in CATEGORIES}
    case_reports: list[dict[str, Any]] = []
    severity_mismatches: list[dict[str, Any]] = []
    total_tp = total_fp = total_fn = 0
    critical_total = critical_matched = 0
    valid_count = verdict_agreed = 0
    rule_total = rule_found = rule_expected = 0
    exceptions_total = exceptions_exempt = exceptions_flagged = 0

    for case in cases:
        # Positions in case.truths, so reported indices stay those of case.json.
        positions = [index for index, item in enumerate(case.truths) if _in_arm(item, case.arm)]
        truths = tuple(case.truths[index] for index in positions)
        expected_verdict = case.expected_verdict if case.arm is None else _verdict(truths)
        predictions = _predictions(case)
        valid = predictions is not None
        anchors = case.must_not_flag if case.arm is not None and valid else ()
        exempt = tuple(item for item in anchors if _in_arm(item, case.arm))
        elsewhere = tuple(item for item in anchors if not _in_arm(item, case.arm))
        if valid:
            assert predictions is not None
            valid_count += 1
            pairs = _matches(truths, predictions, case.added_lines)
            predicted_verdict: str | None = _verdict(predictions)
        else:
            predictions = []
            pairs = ()
            predicted_verdict = None
        matched_truths = {truth_index for truth_index, _ in pairs}
        matched_predictions = {prediction_index for _, prediction_index in pairs}
        case_tp = len(pairs)
        case_fp = len(predictions) - len(matched_predictions)
        case_fn = len(truths) - len(matched_truths)
        total_tp += case_tp
        total_fp += case_fp
        total_fn += case_fn
        unmatched = [
            item for index, item in enumerate(predictions) if index not in matched_predictions
        ]
        case_flagged = len(
            _matches(exempt, unmatched, case.added_lines, match_category=False)
        ) + len(_matches(elsewhere, predictions, case.added_lines, match_category=False))
        case_rule_expected = sum(_rule_made(truth) for truth in truths)
        unexpected_rules = tuple(
            truth for truth in case.truths if _rule_made(truth) and not _in_arm(truth, case.arm)
        )
        case_rule_found = sum(_rule_made(truths[index]) for index in matched_truths) + len(
            _matches(unexpected_rules, unmatched, case.added_lines)
        )
        rule_total += case_rule_expected + len(unexpected_rules)
        rule_expected += case_rule_expected
        rule_found += case_rule_found
        exceptions_total += len(anchors)
        exceptions_exempt += len(exempt)
        exceptions_flagged += case_flagged

        for truth_index, truth in enumerate(truths):
            if truth["severity"] == "critical":
                critical_total += 1
                if truth_index in matched_truths:
                    critical_matched += 1
            category_counts[truth["category"]]["tp" if truth_index in matched_truths else "fn"] += 1
        for prediction_index, prediction in enumerate(predictions):
            if prediction_index not in matched_predictions:
                category_counts[prediction["category"]]["fp"] += 1

        match_details = []
        for truth_index, prediction_index in pairs:
            truth = truths[truth_index]
            prediction = predictions[prediction_index]
            match_details.append(
                {
                    "truth_index": positions[truth_index],
                    "prediction_index": prediction_index,
                    "distance": abs(prediction["line"] - truth["line"]),
                }
            )
            if truth["severity"] != prediction["severity"]:
                severity_mismatches.append(
                    {
                        "case_id": case.case_id,
                        "truth_index": positions[truth_index],
                        "prediction_index": prediction_index,
                        "truth_severity": truth["severity"],
                        "predicted_severity": prediction["severity"],
                    }
                )

        agrees = valid and predicted_verdict == expected_verdict
        verdict_agreed += agrees
        case_reports.append(
            {
                "case_id": case.case_id,
                "valid": valid,
                "tp": case_tp,
                "fp": case_fp,
                "fn": case_fn,
                "expected_verdict": expected_verdict,
                "predicted_verdict": predicted_verdict,
                "verdict_agrees": agrees,
                "matches": match_details,
                "rule_truths": case_rule_expected + len(unexpected_rules),
                "rule_found": case_rule_found,
                "exceptions": len(anchors),
                "exceptions_flagged": case_flagged,
            }
        )

    per_category = {
        name: {
            **counts,
            "precision": _ratio(counts["tp"], counts["tp"] + counts["fp"]),
            "recall": _ratio(counts["tp"], counts["tp"] + counts["fn"]),
        }
        for name, counts in category_counts.items()
    }
    return {
        "case_count": len(cases),
        "valid_response_count": valid_count,
        "validity": _ratio(valid_count, len(cases)),
        "micro": {
            "tp": total_tp,
            "fp": total_fp,
            "fn": total_fn,
            "precision": _ratio(total_tp, total_tp + total_fp),
            "recall": _ratio(total_tp, total_tp + total_fn),
        },
        "critical": {
            "matched": critical_matched,
            "total": critical_total,
            "recall": _ratio(critical_matched, critical_total),
        },
        "per_category": per_category,
        "verdict": {
            "agreed": verdict_agreed,
            "total": len(cases),
            "agreement": _ratio(verdict_agreed, len(cases)),
        },
        "severity_mismatches": severity_mismatches,
        "rule_constraints": {
            "found": rule_found,
            "total": rule_total,
            "expected": rule_expected,
            "share": _ratio(rule_found, rule_total),
        },
        "exceptions": {
            "total": exceptions_total,
            "exempt": exceptions_exempt,
            "flagged": exceptions_flagged,
            "respected": exceptions_total - exceptions_flagged,
            "share": _ratio(exceptions_total - exceptions_flagged, exceptions_total),
        },
        "usage": _usage_totals(cases),
        "cases": case_reports,
    }
