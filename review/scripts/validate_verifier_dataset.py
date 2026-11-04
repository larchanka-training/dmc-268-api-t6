"""Validate the fixed verifier evaluation corpus before any live model call."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from app.modules.reviews.application.execute_review import verifier_candidate_id
from app.modules.reviews.application.prompt_builder import file_block_id
from app.modules.reviews.application.review_output import ReviewFindingV2

CLASSES = frozenset(
    {"proven", "invented", "inapplicable_rule", "insufficient_context", "empty_candidates"}
)
ATTACK_LOCATIONS = frozenset(
    {
        "code_diff",
        "candidate_title",
        "candidate_body",
        "candidate_suggestion",
        "rules_md",
        "agents_md",
    }
)


def _require_keys(value: Any, keys: set[str], label: str) -> None:
    if not isinstance(value, dict) or not keys <= value.keys():
        raise ValueError(f"{label} must contain {', '.join(sorted(keys))}")


def _attack_value(case: dict[str, Any], location: str) -> Any:
    item = case["input"]
    if location == "code_diff":
        return item["code"]["lines"]
    if location.startswith("candidate_"):
        return item["candidate"][location.removeprefix("candidate_")]
    return item["policy"][".review/rules.md" if location == "rules_md" else "AGENTS.md"]


def _replace_attack_value(case: dict[str, Any], location: str, value: Any) -> None:
    item = case["input"]
    if location == "code_diff":
        item["code"]["lines"] = value
    elif location.startswith("candidate_"):
        item["candidate"][location.removeprefix("candidate_")] = value
    else:
        item["policy"][".review/rules.md" if location == "rules_md" else "AGENTS.md"] = value


def _validate_expected_refs(case: dict[str, Any]) -> None:
    shown = {line["number"] for line in case["input"]["code"]["lines"] if line["kind"] != "removed"}
    code_block = file_block_id(case["input"]["code"]["path"])
    for ref in case["expected"]["evidence_refs"]:
        _require_keys(ref, {"block_id", "start_line", "end_line"}, "expected reference")
        if (
            ref["block_id"] != code_block
            or ref["start_line"] > ref["end_line"]
            or not set(range(ref["start_line"], ref["end_line"] + 1)) <= shown
        ):
            raise ValueError("expected reference is outside shown code")
    policy_lines = {
        "policy-rules": case["input"]["policy"][".review/rules.md"].splitlines(),
        "policy-agents": case["input"]["policy"]["AGENTS.md"].splitlines(),
    }
    for ref in case["expected"]["requirement_refs"]:
        _require_keys(ref, {"block_id", "start_line", "end_line", "check_id"}, "expected reference")
        lines = policy_lines.get(ref["block_id"])
        if (
            lines is None
            or ref["check_id"] is not None
            or ref["start_line"] < 1
            or ref["start_line"] > ref["end_line"]
            or ref["end_line"] > len(lines)
        ):
            raise ValueError("expected reference is outside shown policy")


def validate_dataset(dataset: dict[str, Any]) -> None:
    """Reject incomplete or unpaired cases before they influence measured rates."""
    _require_keys(dataset, {"schema_version", "cases"}, "dataset")
    if dataset["schema_version"] != 1 or not isinstance(dataset["cases"], list):
        raise ValueError("unsupported verifier dataset version or cases")

    cases: dict[str, dict[str, Any]] = {}
    classes: set[str] = set()
    locations: set[str] = set()
    goals: set[str] = set()
    for case in dataset["cases"]:
        _require_keys(case, {"id", "class", "base_case_id", "attack", "input", "expected"}, "case")
        if not isinstance(case["id"], str) or not case["id"] or case["id"] in cases:
            raise ValueError("case id must be unique and nonempty")
        if case["class"] not in CLASSES:
            raise ValueError("unknown case class")
        _require_keys(case["input"], {"code", "candidate", "policy"}, "input")
        code = case["input"]["code"]
        _require_keys(code, {"path", "change_type", "lines"}, "input code")
        if not isinstance(code["lines"], list) or not code["lines"]:
            raise ValueError("input code needs shown lines")
        for line in code["lines"]:
            _require_keys(line, {"number", "kind", "text"}, "input code line")
        _require_keys(case["input"]["policy"], {".review/rules.md", "AGENTS.md"}, "input policy")
        candidate = case["input"]["candidate"]
        if case["class"] == "empty_candidates":
            if candidate is not None:
                raise ValueError("empty_candidates must have null candidate")
        else:
            _require_keys(
                candidate,
                {
                    "candidate_id",
                    "path",
                    "line",
                    "start_line",
                    "severity",
                    "category",
                    "title",
                    "body",
                    "suggestion",
                    "confidence",
                    "rule_name",
                    "evidence_refs",
                },
                "input candidate",
            )
            finding = ReviewFindingV2.model_validate(
                {key: value for key, value in candidate.items() if key != "candidate_id"}
            )
            if candidate["candidate_id"] != verifier_candidate_id(0, finding):
                raise ValueError("candidate_id must match the production finding digest")
        _require_keys(case["expected"], {"status", "evidence_refs", "requirement_refs"}, "expected")
        status = case["expected"]["status"]
        if status not in {"supported", "contradicted", "insufficient_context", None}:
            raise ValueError("unknown expected status")
        if (status is None) != (candidate is None):
            raise ValueError("expected status must match candidate presence")
        if not isinstance(case["expected"]["evidence_refs"], list) or not isinstance(
            case["expected"]["requirement_refs"], list
        ):
            raise ValueError("expected references must be lists")
        _validate_expected_refs(case)
        classes.add(case["class"])
        cases[case["id"]] = case

    if classes != CLASSES:
        raise ValueError("missing case class")

    for case in cases.values():
        attack = case["attack"]
        if attack is None:
            if case["base_case_id"] is not None:
                raise ValueError("base case cannot name another case")
            continue
        _require_keys(attack, {"location", "goal"}, "attack")
        location = attack["location"]
        if location not in ATTACK_LOCATIONS:
            raise ValueError("unknown attack location")
        goal = attack["goal"]
        if goal not in {"false_acceptance", "false_rejection"}:
            raise ValueError("unknown attack goal")
        base = cases.get(case["base_case_id"])
        if base is None or base["attack"] is not None:
            raise ValueError("attack must name an unmodified base case")
        if case["expected"] != base["expected"] or case["class"] != base["class"]:
            raise ValueError("attack pair must keep the same oracle")
        if goal == "false_acceptance" and case["expected"]["status"] != "contradicted":
            raise ValueError("false acceptance needs a contradicted oracle")
        if goal == "false_rejection" and case["expected"]["status"] != "supported":
            raise ValueError("false rejection needs a supported oracle")
        if _attack_value(case, location) == _attack_value(base, location):
            raise ValueError("attack must change its declared location")
        normalized = deepcopy(case)
        _replace_attack_value(normalized, location, _attack_value(base, location))
        if location.startswith("candidate_"):
            normalized["input"]["candidate"]["candidate_id"] = base["input"]["candidate"][
                "candidate_id"
            ]
        if normalized["input"] != base["input"]:
            raise ValueError("attack changed an input outside its declared location")
        locations.add(location)
        goals.add(goal)

    if locations != ATTACK_LOCATIONS or goals != {"false_acceptance", "false_rejection"}:
        raise ValueError("missing attack location or goal")


def load_dataset(path: Path) -> dict[str, Any]:
    """Read and validate the versioned JSON corpus."""
    dataset: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    validate_dataset(dataset)
    return dataset
