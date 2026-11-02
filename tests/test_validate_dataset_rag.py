"""Case validator: the RAG sub-corpus (#146)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from review.scripts import validate_dataset
from review.scripts.rag_snapshot import INDEX_FILE, build_case_snapshot, render_snapshot
from tests.rag_case_fakes import _rag_metadata
from tests.validate_dataset_fakes import ROOT, _case, _drop, _metadata, _run

# --- the RAG sub-corpus (#146) -------------------------------------------------------------

_RAG_FIXTURES = ROOT / "tests" / "fixtures" / "rag_eval"
_SCHEMA = Draft202012Validator(
    json.loads((ROOT / "test-prs-dataset" / "schema" / "case.schema.json").read_text("utf-8"))
)


def _rag_case(
    tmp_path: Path,
    metadata: dict[str, Any] | None = None,
    *,
    fixture: str = "spike",
    reindex: bool = True,
) -> Path:
    """A dataset root with one RAG case: a copy of a fixture case, its ``index.json`` rebuilt.

    The indexed commit id derives from the directory name, so a copy needs a fresh index.
    """
    record = metadata if metadata is not None else _rag_metadata()
    dataset = tmp_path / "rag"
    case_dir = dataset / "cases" / record["id"]
    shutil.copytree(_RAG_FIXTURES / fixture, case_dir)
    (case_dir / "case.json").write_text(json.dumps(record), encoding="utf-8")
    if reindex:
        (case_dir / INDEX_FILE).write_bytes(render_snapshot(build_case_snapshot(case_dir)))
    return dataset


def test_rag_case_passes_its_per_case_checks(tmp_path: Path) -> None:
    dataset = _rag_case(tmp_path)

    result = _run(dataset, "--case", "RAG-01")

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "OK RAG-01\n"


def test_rag_clean_case_may_have_no_findings(tmp_path: Path) -> None:
    metadata = _rag_metadata("clean")
    dataset = _rag_case(tmp_path, metadata, fixture="clean")

    result = _run(dataset, "--case", "RAG-01")

    assert result.returncode == 0, result.stdout + result.stderr


def _rag_set(**fields: object) -> Any:
    def apply(record: dict[str, Any]) -> None:
        record["rag"].update(fields)

    return apply


def _two_truths(record: dict[str, Any]) -> None:
    record["expected_findings"].append({**record["expected_findings"][0], "line": 7})


def _no_truth(record: dict[str, Any]) -> None:
    record["expected_findings"] = []
    record["expected_verdict"] = "clean"


def _rag_injection(**fields: object) -> dict[str, Any]:
    return {
        "bait_path": "pkg/api.py",
        "bait_text": "ignore the rest",
        "forced": [{"path": "pkg/store.py", "line": 6, "category": "correctness"}],
        **fields,
    }


@pytest.mark.parametrize(
    ("change", "diagnostic"),
    [
        (_drop("rag"), "case.json.$: 'rag' is a required property"),
        (_drop("rag", "kind"), "case.json.rag: 'kind' is a required property"),
        (_drop("rag", "expected_retrieved"), "'expected_retrieved' is a required property"),
        (_drop("rag", "absent_from_context"), "'absent_from_context' is a required property"),
        (_drop("rag", "defect_form"), "case.json.rag: 'defect_form' is a required property"),
        (_rag_set(defect_form="signature"), "case.json.rag.defect_form: 'signature' is not one of"),
        (_rag_set(kind="other"), "case.json.rag.kind: 'other' is not one of"),
        (_rag_set(surprise=1), "Additional properties are not allowed ('surprise'"),
        (_two_truths, "case.json.expected_findings: [{"),
        (_no_truth, "case.json.expected_findings: [] should be non-empty"),
        (_rag_set(injection=_rag_injection()), "case.json.rag.injection: "),
        (_rag_set(l4_definitions=["pkg/models.py"]), "case.json.rag.l4_definitions: "),
        (lambda record: record.update(base_path="other"), "case.json.base_path: 'base' was"),
        (lambda record: record.update(patch_path="x.patch"), "case.json.patch_path: 'diff.patch'"),
        (lambda record: record.update(apply_directory="pkg"), "case.json.apply_directory: "),
        (
            lambda record: record.update(policy={"arms": {"valid": "a", "malicious": "b"}}),
            "case.json.policy: ",
        ),
        (
            lambda record: record.update(
                must_not_flag=[
                    {
                        "path": "pkg/store.py",
                        "start_line": None,
                        "line": 6,
                        "category": "correctness",
                        "arms": ["none"],
                        "reason": "quoted",
                    }
                ]
            ),
            "case.json.must_not_flag: ",
        ),
        (
            lambda record: record["expected_findings"][0].update(arms=["none"]),
            "case.json.expected_findings.0.arms: ",
        ),
        (
            lambda record: record["expected_findings"][0].update(origin="rule", rule_ref="## A"),
            "case.json.expected_findings.0.origin: ",
        ),
        (
            lambda record: record["expected_findings"][0].update(origin="rule", rule_ref="## A"),
            "case.json.expected_findings.0.rule_ref: ",
        ),
        (
            _rag_set(expected_retrieved=["pkg/api.py", "pkg/api.py"]),
            "case.json.rag.expected_retrieved: ",
        ),
    ],
)
def test_schema_rejects_an_invalid_rag_case(change: Any, diagnostic: str) -> None:
    record = _rag_metadata()
    change(record)

    errors = validate_dataset._schema_errors(record, _SCHEMA)

    assert any(diagnostic in error for error in errors), errors


def test_schema_accepts_every_kind_with_its_required_blocks() -> None:
    control = _rag_metadata("control")
    control["rag"]["l4_definitions"] = ["pkg/models.py"]
    injection = _rag_metadata("injection")
    injection["rag"]["injection"] = _rag_injection()
    suppressed = _rag_metadata("injection")
    suppressed["rag"]["injection"] = _rag_injection(forced=[], suppresses_truth=True)

    for record in (
        _rag_metadata(),
        control,
        _rag_metadata("depth2"),
        injection,
        suppressed,
        _rag_metadata("clean"),
    ):
        assert validate_dataset._schema_errors(record, _SCHEMA) == [], record["rag"]["kind"]


@pytest.mark.parametrize(
    ("kind", "change", "diagnostic"),
    [
        ("control", _drop("rag", "l4_definitions"), "'l4_definitions' is a required property"),
        ("control", _rag_set(l4_definitions=[]), "case.json.rag.l4_definitions: [] should be non"),
        ("injection", _drop("rag", "injection"), "'injection' is a required property"),
        ("injection", _drop("rag", "injection", "bait_text"), "'bait_text' is a required"),
        ("injection", _drop("rag", "injection", "forced"), "'forced' is a required property"),
        (
            "injection",
            _rag_set(injection=_rag_injection(forced=[])),
            "case.json.rag.injection: {",
        ),
        (
            "injection",
            _rag_set(
                injection=_rag_injection(
                    forced=[{"path": "a.py", "line": 2, "start_line": 1, "category": "security"}]
                )
            ),
            "'start_line' was unexpected",
        ),
        ("clean", lambda record: record["expected_findings"].append({}), "should be non-empty"),
    ],
)
def test_schema_rejects_an_incomplete_kind_block(kind: str, change: Any, diagnostic: str) -> None:
    record = _rag_metadata(kind)
    if kind == "control":
        record["rag"]["l4_definitions"] = ["pkg/models.py"]
    if kind == "injection":
        record["rag"]["injection"] = _rag_injection()
    change(record)

    errors = validate_dataset._schema_errors(record, _SCHEMA)

    assert errors, record
    if kind != "clean":
        assert any(diagnostic in error for error in errors), errors


def test_schema_rejects_an_injection_case_without_a_defect_form() -> None:
    record = _rag_metadata("injection")
    record["rag"]["injection"] = _rag_injection()
    del record["rag"]["defect_form"]

    errors = validate_dataset._schema_errors(record, _SCHEMA)

    assert any("'defect_form' is a required property" in error for error in errors), errors


def test_schema_accepts_a_clean_case_only_without_a_truth() -> None:
    record = _rag_metadata("clean")
    assert validate_dataset._schema_errors(record, _SCHEMA) == []
    record["expected_findings"] = _rag_metadata()["expected_findings"]

    errors = validate_dataset._schema_errors(record, _SCHEMA)

    assert any(error.startswith("case.json.expected_findings: ") for error in errors), errors


@pytest.mark.parametrize("raised", [RuntimeError("boom"), OSError("gone")])
def test_rag_index_check_that_cannot_run_is_an_error_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raised: Exception
) -> None:
    from review.scripts import rag_snapshot

    case_dir = _rag_case(tmp_path) / "cases" / "RAG-01"

    def broken(_: Path) -> object:
        raise raised

    monkeypatch.setattr(rag_snapshot, "build_case_snapshot", broken)

    assert validate_dataset._rag_case_errors(case_dir, _rag_metadata()) == [
        f"index.json: cannot be checked: {raised}"
    ]


def test_schema_keeps_rag_out_of_the_other_classes() -> None:
    record = _metadata()
    record["rag"] = _rag_metadata()["rag"]

    errors = validate_dataset._schema_errors(record, _SCHEMA)

    assert any(error.startswith("case.json.rag: ") for error in errors), errors


def test_rag_case_id_needs_the_rag_prefix(tmp_path: Path) -> None:
    dataset = _rag_case(tmp_path, _rag_metadata(case_id="SEC-01"))

    result = _run(dataset, "--case", "SEC-01")

    assert result.returncode == 1
    assert "SEC-01: case.json.id: prefix does not match class" in result.stdout


def test_two_truths_in_a_reverse_edge_case_fail_the_validator(tmp_path: Path) -> None:
    metadata = _rag_metadata()
    _two_truths(metadata)
    dataset = _rag_case(tmp_path, metadata)

    result = _run(dataset, "--case", "RAG-01")

    assert result.returncode == 1
    assert "RAG-01: case.json.expected_findings: [{" in result.stdout


def test_empty_findings_fail_a_case_that_is_not_clean(tmp_path: Path) -> None:
    metadata = _rag_metadata()
    _no_truth(metadata)
    dataset = _rag_case(tmp_path, metadata)

    result = _run(dataset, "--case", "RAG-01")

    assert result.returncode == 1
    assert "RAG-01: case.json.expected_findings: [] should be non-empty" in result.stdout


def test_rag_case_needs_its_index(tmp_path: Path) -> None:
    dataset = _rag_case(tmp_path)
    (dataset / "cases" / "RAG-01" / INDEX_FILE).unlink()

    result = _run(dataset, "--case", "RAG-01")

    assert result.returncode == 1
    assert "RAG-01: index.json: missing; run build_rag_snapshot.py --write" in result.stdout


def test_rag_case_with_a_stale_index_fails(tmp_path: Path) -> None:
    dataset = _rag_case(tmp_path)
    case_dir = dataset / "cases" / "RAG-01"
    (case_dir / "base" / "pkg" / "api.py").write_text("x = 1\n", encoding="utf-8")

    result = _run(dataset, "--case", "RAG-01")

    assert result.returncode == 1
    assert "RAG-01: index.json: stale; run build_rag_snapshot.py --write" in result.stdout


def test_rag_case_runs_the_integrity_checks(tmp_path: Path) -> None:
    metadata = _rag_metadata()
    metadata["rag"]["expected_retrieved"] = ["pkg/models.py"]
    dataset = _rag_case(tmp_path, metadata)

    result = _run(dataset, "--case", "RAG-01")

    assert result.returncode == 1
    assert "RAG-01: reverse_edge_caller:" in result.stdout


def test_main_final_keeps_rag_cases_out_of_the_main_corpus(tmp_path: Path) -> None:
    dataset = _rag_case(tmp_path)

    result = _run(dataset, "--final")

    assert result.returncode == 1
    assert "final corpus: RAG-01 is a rag case; validate it with --profile rag\n" in result.stdout


def test_rules_profile_keeps_rag_cases_out_of_the_rules_corpus(tmp_path: Path) -> None:
    dataset = _rag_case(tmp_path)

    result = _run(dataset, "--final", "--profile", "rules")

    assert result.returncode == 1
    assert "rules corpus: every case must be class rules, got rag\n" in result.stdout


def test_rag_profile_rejects_a_main_corpus_class(tmp_path: Path) -> None:
    dataset = _rag_case(tmp_path)
    main_case = _case(tmp_path / "main", _metadata()) / "cases" / "SEC-01"
    shutil.copytree(main_case, dataset / "cases" / "SEC-01")

    result = _run(dataset, "--final", "--profile", "rag")

    assert result.returncode == 1
    assert "rag corpus: every case must be class rag, got security\n" in result.stdout


def test_rag_profile_reports_its_own_distribution(tmp_path: Path) -> None:
    dataset = _rag_case(tmp_path)
    report = tmp_path / "rag-distribution.json"

    result = _run(dataset, "--final", "--profile", "rag", "--report-json", str(report))
    data = json.loads(report.read_text(encoding="utf-8"))

    assert result.returncode == 1
    assert data == {
        "schema_version": 1,
        "profile": "rag",
        "case_count": 1,
        "valid_case_count": 1,
        "class_counts": {"rag": 1},
        "kind_counts": {"reverse_edge": 1, "control": 0, "depth2": 0, "injection": 0, "clean": 0},
        "export_defect_count": 0,
        "critical_truth_count": 0,
        "language_counts": {"python": 1, "typescript": 0, "tsx": 0},
        "passed": False,
        "errors": [
            "rag corpus: kind reverse_edge has 1 cases, needs exactly 10",
            "rag corpus: kind control has 0 cases, needs exactly 6",
            "rag corpus: kind depth2 has 0 cases, needs exactly 4",
            "rag corpus: kind injection has 0 cases, needs exactly 2",
            "rag corpus: kind clean has 0 cases, needs exactly 2",
            "rag corpus: needs exactly 3 renamed or removed export cases, got 0",
            "rag corpus: needs Python and TypeScript/React cases",
            "rag corpus: needs ≥1 critical truth, got 0",
        ],
    }


def _rag_record(
    kind: str,
    language: str = "python",
    form: str | None = None,
    severity: str = "critical",
) -> dict[str, Any]:
    rag = {"kind": kind, **({"defect_form": form} if form else {})}
    findings = [{"path": "pkg/a.py", "line": 1, "category": "correctness", "severity": severity}]
    return {
        "id": "RAG-00",
        "class": "rag",
        "language": language,
        "expected_findings": findings,
        "rag": rag,
    }


def _rag_records(
    *, exports: tuple[str, ...] = ("renamed_export", "removed_export", "renamed_export")
) -> list[dict[str, Any]]:
    forms = [*exports, *["new_required_param"] * (10 - len(exports))]
    records = [_rag_record("reverse_edge", form=form) for form in forms]
    records += [_rag_record("control")] * 6 + [_rag_record("depth2")] * 4
    records += [_rag_record("injection", form="narrowed_type")] * 2
    records += [_rag_record("clean", language="typescript")] * 2
    return records


def test_rag_distribution_of_the_full_corpus_is_accepted() -> None:
    counts = validate_dataset._rag_distribution_counts(_rag_records())

    assert counts["kind_counts"] == {
        "reverse_edge": 10,
        "control": 6,
        "depth2": 4,
        "injection": 2,
        "clean": 2,
    }
    assert counts["export_defect_count"] == 3
    assert validate_dataset._rag_distribution_errors(counts) == []


@pytest.mark.parametrize(
    ("records", "diagnostic"),
    [
        (_rag_records()[:-1], "kind clean has 1 cases, needs exactly 2"),
        (_rag_records() + [_rag_record("control")], "kind control has 7 cases, needs exactly 6"),
        (
            _rag_records(exports=("renamed_export", "removed_export")),
            "needs exactly 3 renamed or removed export cases, got 2",
        ),
        (
            _rag_records(exports=("renamed_export",) * 4),
            "needs exactly 3 renamed or removed export cases, got 4",
        ),
        (
            [{**record, "language": "python"} for record in _rag_records()],
            "needs Python and TypeScript/React cases",
        ),
    ],
)
def test_rag_distribution_refuses_a_corpus_that_misses_the_numbers(
    records: list[dict[str, Any]], diagnostic: str
) -> None:
    counts = validate_dataset._rag_distribution_counts(records)

    assert validate_dataset._rag_distribution_errors(counts) == [f"rag corpus: {diagnostic}"]


def test_rag_distribution_counts_the_critical_truths() -> None:
    records = _rag_records()
    records[3] = _rag_record("reverse_edge", severity="high", form="new_required_param")

    counts = validate_dataset._rag_distribution_counts(records)

    assert counts["critical_truth_count"] == len(records) - 1


def test_rag_distribution_accepts_exactly_one_critical_truth() -> None:
    records = [
        {**record, "expected_findings": [{**record["expected_findings"][0], "severity": "high"}]}
        for record in _rag_records()
    ]
    records[0]["expected_findings"][0]["severity"] = "critical"

    counts = validate_dataset._rag_distribution_counts(records)

    assert counts["critical_truth_count"] == 1
    assert validate_dataset._rag_distribution_errors(counts) == []


def test_rag_distribution_counts_every_critical_finding_of_a_record() -> None:
    record = _rag_record("reverse_edge", form="new_required_param")
    record["expected_findings"] = [
        {**record["expected_findings"][0], "line": line, "severity": severity}
        for line, severity in ((1, "critical"), (2, "high"), (3, "critical"))
    ]

    counts = validate_dataset._rag_distribution_counts([record])

    assert counts["critical_truth_count"] == 2


def test_rag_distribution_refuses_a_corpus_without_a_critical_truth() -> None:
    # Critical Recall has a null denominator (TEST_PLAN §3.2): the gate could only be INCOMPLETE.
    records = [
        {**record, "expected_findings": [{**record["expected_findings"][0], "severity": "high"}]}
        for record in _rag_records()
    ]

    counts = validate_dataset._rag_distribution_counts(records)

    assert counts["critical_truth_count"] == 0
    assert validate_dataset._rag_distribution_errors(counts) == [
        "rag corpus: needs ≥1 critical truth, got 0"
    ]


def test_a_defect_form_of_another_kind_does_not_count_as_an_export_case() -> None:
    records = _rag_records(exports=("renamed_export", "removed_export"))
    records[-1] = _rag_record("injection", form="removed_export")

    counts = validate_dataset._rag_distribution_counts(records)

    assert counts["export_defect_count"] == 2
