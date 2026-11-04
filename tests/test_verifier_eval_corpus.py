"""Contract checks for the fixed verifier eval corpus and its provisional labels."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from app.modules.reviews.application.execute_review import verifier_candidate_id
from app.modules.reviews.application.review_output import ReviewFindingV2
from review.scripts.validate_verifier_dataset import load_dataset, validate_dataset

CORPUS = Path(__file__).resolve().parents[1] / "test-prs-dataset/verifier/v1/cases.json"


def test_fixed_corpus_covers_decisions_and_paired_injections() -> None:
    dataset = load_dataset(CORPUS)

    assert dataset["schema_version"] == 1
    assert len(dataset["cases"]) == 11
    assert {case["class"] for case in dataset["cases"]} == {
        "proven",
        "invented",
        "inapplicable_rule",
        "insufficient_context",
        "empty_candidates",
    }
    assert {case["attack"]["location"] for case in dataset["cases"] if case["attack"]} == {
        "code_diff",
        "candidate_title",
        "candidate_body",
        "candidate_suggestion",
        "rules_md",
        "agents_md",
    }
    assert {case["attack"]["goal"] for case in dataset["cases"] if case["attack"]} == {
        "false_acceptance",
        "false_rejection",
    }


@pytest.mark.parametrize("missing", ["code", "candidate", "policy"])
def test_rejects_missing_input_field(missing: str) -> None:
    dataset = deepcopy(load_dataset(CORPUS))
    del dataset["cases"][0]["input"][missing]

    with pytest.raises(ValueError, match="input"):
        validate_dataset(dataset)


def test_rejects_missing_oracle() -> None:
    dataset = deepcopy(load_dataset(CORPUS))
    del dataset["cases"][0]["expected"]

    with pytest.raises(ValueError, match="expected"):
        validate_dataset(dataset)


def test_rejects_oracle_reference_outside_shown_context() -> None:
    dataset = deepcopy(load_dataset(CORPUS))
    dataset["cases"][2]["expected"]["evidence_refs"][0]["start_line"] = 999

    with pytest.raises(ValueError, match="expected reference"):
        validate_dataset(dataset)


def test_rejects_missing_attack_location() -> None:
    dataset = deepcopy(load_dataset(CORPUS))
    dataset["cases"][5]["attack"]["location"] = "unknown"

    with pytest.raises(ValueError, match="attack location"):
        validate_dataset(dataset)


def test_rejects_attack_that_changes_oracle() -> None:
    dataset = deepcopy(load_dataset(CORPUS))
    dataset["cases"][5]["expected"]["requirement_refs"][0]["start_line"] = 3
    dataset["cases"][5]["expected"]["requirement_refs"][0]["end_line"] = 3

    with pytest.raises(ValueError, match="same oracle"):
        validate_dataset(dataset)


def test_rejects_attack_that_changes_another_input_field() -> None:
    dataset = deepcopy(load_dataset(CORPUS))
    dataset["cases"][5]["input"]["policy"]["AGENTS.md"] += "Unexpected extra policy.\n"

    with pytest.raises(ValueError, match="outside its declared location"):
        validate_dataset(dataset)


def test_policy_oracle_coordinates_name_the_actual_rule() -> None:
    cases = {case["id"]: case for case in load_dataset(CORPUS)["cases"]}

    for case_id in ("VFY-01", "VFY-02"):
        case = cases[case_id]
        ref = case["expected"]["requirement_refs"][0]
        assert ref["start_line"] == ref["end_line"] == 2
        assert (
            "SQL queries must use bound parameters"
            in case["input"]["policy"][".review/rules.md"].splitlines()[ref["start_line"] - 1]
        )

    go_case = cases["VFY-03"]
    go_ref = go_case["expected"]["requirement_refs"][0]
    assert go_ref["start_line"] == go_ref["end_line"] == 3
    assert (
        "Go files must use CamelCase"
        in go_case["input"]["policy"][".review/rules.md"].splitlines()[go_ref["start_line"] - 1]
    )


def test_suggestion_attack_keeps_a_valid_fix() -> None:
    cases = {case["id"]: case for case in load_dataset(CORPUS)["cases"]}
    suggestion = cases["VFY-09"]["input"]["candidate"]["suggestion"]

    assert "bound parameter" in suggestion
    assert "contradicted" in suggestion


def test_proven_oracle_claims_only_visible_sql_interpolation() -> None:
    cases = load_dataset(CORPUS)["cases"]

    for case in cases:
        if case["class"] not in {"proven", "invented"}:
            continue
        claim = " ".join(
            (case["input"]["candidate"]["title"], case["input"]["candidate"]["body"])
        ).lower()
        assert "user input" not in claim
        assert "untrusted" not in claim
        assert "sql injection" not in claim


def test_rejects_nonproduction_candidate_id() -> None:
    dataset = deepcopy(load_dataset(CORPUS))
    dataset["cases"][2]["input"]["candidate"]["candidate_id"] = "cand-rule"

    with pytest.raises(ValueError, match="candidate_id"):
        validate_dataset(dataset)


def test_insufficient_context_case_uses_shared_path_and_policy_shape() -> None:
    cases = {case["id"]: case for case in load_dataset(CORPUS)["cases"]}
    case = cases["VFY-04"]
    candidate = case["input"]["candidate"]

    assert case["input"]["code"]["path"] == "app/auth.py"
    assert candidate["path"] == "app/auth.py"
    assert candidate["rule_name"] == ".review/rules.md"
    assert (
        "API keys must not be transmitted to untrusted servers"
        in case["input"]["policy"][".review/rules.md"]
    )
    assert case["expected"]["status"] == "insufficient_context"
    assert case["expected"]["evidence_refs"] == []
    assert case["expected"]["requirement_refs"] == []


@pytest.mark.parametrize(
    "path",
    [
        (),
        ("source",),
        ("cases", 0),
        ("cases", 0, "input"),
        ("cases", 0, "input", "code"),
        ("cases", 0, "input", "code", "lines", 0),
        ("cases", 0, "input", "candidate"),
        ("cases", 0, "input", "policy"),
        ("cases", 0, "expected"),
        ("cases", 0, "expected", "evidence_refs", 0),
        ("cases", 0, "expected", "requirement_refs", 0),
        ("cases", 5, "attack"),
    ],
)
def test_rejects_unknown_corpus_keys(path: tuple[str | int, ...]) -> None:
    dataset = deepcopy(load_dataset(CORPUS))
    node: Any = dataset
    for part in path:
        node = node[part]
    node["unexpected"] = "value"

    with pytest.raises(ValueError, match="must contain exactly"):
        validate_dataset(dataset)


@pytest.mark.parametrize("kind", ["source", "line"])
def test_rejects_unknown_kind(kind: str) -> None:
    dataset = deepcopy(load_dataset(CORPUS))
    if kind == "source":
        dataset["source"]["kind"] = "unknown"
    else:
        dataset["cases"][0]["input"]["code"]["lines"][0]["kind"] = "unknown"

    with pytest.raises(ValueError, match=f"{kind} kind"):
        validate_dataset(dataset)


@pytest.mark.parametrize(
    ("case_index", "status", "references", "error"),
    [
        (0, "contradicted", None, "class and status"),
        (0, "supported", "evidence_refs", "requires evidence"),
        (0, "supported", "requirement_refs", "requires requirement"),
        (3, "insufficient_context", "requirement_refs", "must have no references"),
        (4, None, "evidence_refs", "must have no references"),
    ],
)
def test_rejects_oracle_status_reference_mismatch(
    case_index: int, status: str | None, references: str | None, error: str
) -> None:
    dataset = deepcopy(load_dataset(CORPUS))
    expected = dataset["cases"][case_index]["expected"]
    expected["status"] = status
    if references is not None:
        expected[references] = (
            [] if status == "supported" else deepcopy(dataset["cases"][0]["expected"][references])
        )

    with pytest.raises(ValueError, match=error):
        validate_dataset(dataset)


def test_inapplicable_rule_accepts_insufficient_context_without_references() -> None:
    dataset = deepcopy(load_dataset(CORPUS))
    expected = dataset["cases"][2]["expected"]
    expected["status"] = "insufficient_context"
    expected["evidence_refs"] = []
    expected["requirement_refs"] = []

    validate_dataset(dataset)


@pytest.mark.parametrize("references", ["evidence_refs", "requirement_refs"])
def test_inapplicable_rule_insufficient_context_rejects_references(references: str) -> None:
    dataset = deepcopy(load_dataset(CORPUS))
    expected = dataset["cases"][2]["expected"]
    expected["status"] = "insufficient_context"
    expected["evidence_refs" if references == "requirement_refs" else "requirement_refs"] = []

    with pytest.raises(ValueError, match="must have no references"):
        validate_dataset(dataset)


@pytest.mark.parametrize("anchor", ["line", "evidence_refs"])
def test_rejects_candidate_anchor_outside_shown_code(anchor: str) -> None:
    dataset = deepcopy(load_dataset(CORPUS))
    candidate = dataset["cases"][0]["input"]["candidate"]
    if anchor == "line":
        candidate["line"] = 999
    else:
        candidate["evidence_refs"][0]["start_line"] = 999
        candidate["evidence_refs"][0]["end_line"] = 999
    finding = ReviewFindingV2.model_validate(
        {key: value for key, value in candidate.items() if key != "candidate_id"}
    )
    candidate["candidate_id"] = verifier_candidate_id(0, finding)

    with pytest.raises(ValueError, match="candidate anchor is outside shown code"):
        validate_dataset(dataset)
