"""Contract checks for the fixed verifier eval corpus and its provisional labels."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

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
    dataset["cases"][5]["expected"]["status"] = "supported"

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
