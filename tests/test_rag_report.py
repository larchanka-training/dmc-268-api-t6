"""The report of #146 quotes the gate script's numbers, not retyped ones.

``docs/test-plan/RAG_V1_CONTRIBUTION_REPORT.md`` is the report and
``test-prs-dataset/rag/reports/gate.json`` the stdout of ``eval_rag_gate.py`` over the six
committed captures (no oracle). Each number is looked up in the table row that carries it, so a
value moved to another row or arm fails. Recomputing the gate takes six replays (minutes) and
stays with CI's capture replay.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from review.scripts.eval_provenance import digest_files
from review.scripts.validate_findings import validate_review_output_v2

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "test-prs-dataset" / "rag" / "reports" / "gate.json"
REPORT = ROOT / "docs" / "test-plan" / "RAG_V1_CONTRIBUTION_REPORT.md"
CAPTURES = ROOT / "test-prs-dataset" / "rag" / "responses"
RUN_IDS = {  # test-prs-dataset/rag/README.md "Captures"
    "A-run1": "41720614606",
    "A-run2": "41724358586",
    "A-run3": "41725329228",
    "B-run1": "41720730389",
    "B-run2": "41724449009",
    "B-run3": "41725470313",
}
RUN_METRICS = ("recall", "precision", "critical_recall", "validity")
ARM_ROWS: dict[str, Callable[[dict[str, Any]], str]] = {  # report row label -> arm value text
    "| Recall, среднее": lambda arm: str(arm["recall_mean"]["exact"]),
    "| Precision, среднее": lambda arm: str(arm["precision_mean"]["exact"]),
    "| Precision, pooled": lambda arm: _ratio(arm["precision_pooled"]),
    "| Входные токены, медиана": lambda arm: str(arm["tokens_in"]["median"]),
    "| Входные токены, сумма": lambda arm: str(arm["tokens_in"]["total"]),
    "| Выходные токены": lambda arm: str(arm["tokens_out_total"]),
    "| Стоимость провайдера": lambda arm: str(arm["cost_usd_total"]),
    "| Латентность": lambda arm: f"{arm['latency_ms']['p50']} / {arm['latency_ms']['p95']}",
}
CONDITION_ROWS = {
    "delta_recall": "| ΔRecall",
    "robust_wins": "| Устойчивые выигрыши",
    "precision": "| Падение Precision",
    "critical_recall": "| Critical Recall B >= A",
    "hallucination": "| Рост Hallucination Rate",
    "tokens": "| Медиана входных токенов",
    "injections": "| Исполненные инъекции",
}


def _gate() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(GATE.read_text(encoding="utf-8"))
    return loaded


def _rows(prefix: str) -> list[str]:
    return [
        line for line in REPORT.read_text(encoding="utf-8").splitlines() if line.startswith(prefix)
    ]


def _assert_row(prefix: str, cells: list[str]) -> None:
    rows = _rows(prefix)
    assert any(all(cell in row for cell in cells) for row in rows), (prefix, cells, rows)


def _ratio(counts: dict[str, int]) -> str:
    return f"{counts['numerator']}/{counts['denominator']}"


def test_the_verdict_line_is_the_gate_verdict() -> None:
    gate = _gate()

    assert f"**{gate['verdict']}**" in REPORT.read_text(encoding="utf-8").split("\n\n")[1]


@pytest.mark.parametrize("arm_name", ["rag_off", "rag_on"])
def test_each_run_row_carries_its_own_ratios_and_digest(arm_name: str) -> None:
    for run in _gate()["arms"][arm_name]["runs"]:
        prefix = f"| {run['run_id']} |"

        _assert_row(prefix, [_ratio(run[name]) + " (" for name in RUN_METRICS])
        _assert_row(prefix, [run["responses_digest"]])


def test_each_arm_row_carries_the_arm_values_in_arm_order() -> None:
    arms = _gate()["arms"]

    for prefix, value in ARM_ROWS.items():
        rows = _rows(prefix)
        assert any(
            row.index(value(arms["rag_off"])) < row.rindex(value(arms["rag_on"])) for row in rows
        ), (prefix, rows)


def test_each_condition_row_carries_its_value_arms_and_status() -> None:
    for key, prefix in CONDITION_ROWS.items():
        condition = _gate()["conditions"][key]
        cells = [str(condition["exact"] if condition["exact"] is not None else "n/a")]
        cells += [str(condition[name]) for name in ("a", "b") if condition.get(name)]
        cells.append(f"| {condition['status']} |")

        _assert_row(prefix, cells)


def test_each_reverse_edge_row_carries_its_counts_and_win() -> None:
    for case in _gate()["reverse_edge"]["cases"]:
        win = "да" if case["robust_win"] else "нет"

        _assert_row(
            f"| {case['case_id']} |",
            [f"| {case['missed_in_a_runs']} из 3 | {case['found_in_b_runs']} из 3 | {win} |"],
        )


def test_each_injection_row_carries_its_finding_and_anchor() -> None:
    events = _gate()["injection"]["executed"]

    assert len(events) == 3
    for event in events:
        finding, anchor = event["finding"], event["anchor"]
        _assert_row(
            f"| {event['case_id']} | {event['run_id']} |",
            [
                f"#{event['finding_index']}, `{finding['path']}:{finding['line']}`",
                f"`{anchor['path']}:{anchor['line']}`",
            ],
        )


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (("verdict",), "FAIL"),
        (("conditions", "delta_recall", "exact"), "20"),
        (("conditions", "robust_wins", "exact"), "3"),
        (("conditions", "precision", "exact"), "-2575/459"),
        (("conditions", "tokens", "exact"), "4316/4239"),
        (("conditions", "injections", "exact"), "3"),
        (("conditions", "hallucination", "status"), "undefined"),
        (("oracle", "present"), False),
        (("warnings",), []),
        (("arms", "rag_off", "recall_mean", "exact"), "1/30"),
        (("arms", "rag_on", "recall_mean", "exact"), "7/30"),
    ],
)
def test_the_committed_gate_output_holds_the_recorded_result(
    path: tuple[str, ...], expected: object
) -> None:
    value: Any = _gate()
    for key in path:
        value = value[key]

    assert value == expected


def _valid_answers(directory: Path) -> list[str]:
    """The ``<case>.json`` names the gate scores: answers the v2 validator accepts."""
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    return sorted(
        f"{case_id}.json"
        for case_id in manifest["responses"]
        if not validate_review_output_v2(
            json.loads((directory / f"{case_id}.json").read_text(encoding="utf-8"))
        )
    )


def test_each_gate_digest_is_the_digest_of_the_valid_answers_of_its_capture() -> None:
    for arm in _gate()["arms"].values():
        for run in arm["runs"]:
            names = _valid_answers(CAPTURES / run["run_id"])

            assert len(names) == run["validity"]["numerator"]
            assert digest_files(CAPTURES / run["run_id"], names) == run["responses_digest"]


def test_the_report_provenance_is_that_of_the_capture_manifests() -> None:
    for run_id, workflow_run in RUN_IDS.items():
        manifest = json.loads((CAPTURES / run_id / "manifest.json").read_text(encoding="utf-8"))
        recorded = manifest["run_metadata"]["recorded_at"][:19].replace("T", " ")

        _assert_row(f"| {run_id} |", [workflow_run, recorded])
        _assert_row("| Промпт", [manifest["prompt_sha"]])
        _assert_row("| Корпус", [manifest["corpus_digest"]])
        _assert_row("| Статика", [manifest["static_digest"]])
        _assert_row("| Индекс RAG", [manifest["run_metadata"]["rag_index_digest"]])
