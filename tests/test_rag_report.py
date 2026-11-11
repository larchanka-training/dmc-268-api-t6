"""The report of #146 quotes the gate script's numbers, not retyped ones.

``docs/test-plan/RAG_V1_CONTRIBUTION_REPORT.md`` is the report and
``test-prs-dataset/rag/reports/146-v5/gate.json`` the stdout of ``eval_rag_gate.py`` over the six
committed captures and the committed hallucination oracle
(``reports/146-v5/oracle/hallucination.json``). Each number is compared with the cell of its
table row and column, so a value moved to another row, column or arm fails. Recomputing the gate
takes six replays (minutes) and stays with CI's capture replay.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from review.scripts.eval_live import _prompt_digest
from review.scripts.eval_provenance import digest_files
from review.scripts.validate_findings import validate_review_output_v2

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "test-prs-dataset" / "rag" / "reports" / "146-v5"  # the frozen #146 record
GATE = ARCHIVE / "gate.json"
REPORT = ROOT / "docs" / "test-plan" / "RAG_V1_CONTRIBUTION_REPORT.md"
CAPTURES = ARCHIVE / "responses"
PROMPTS = ARCHIVE / "oracle" / "context"
SYSTEM_PROMPT = ROOT / "review" / "prompts" / "review.system.v5.md"  # the #146 record is v5
PROMPT_LINK_DIR = "../../test-prs-dataset/rag/reports/146-v5/oracle/context"
ORACLE = ARCHIVE / "oracle" / "hallucination.json"
RUN_IDS = {  # test-prs-dataset/rag/README.md "Captures"
    "A-run1": "41720614606",
    "A-run2": "41724358586",
    "A-run3": "41725329228",
    "B-run1": "41720730389",
    "B-run2": "41724449009",
    "B-run3": "41725470313",
}
STATIC_DRIFT_WARNINGS = [  # two static inputs changed on main after the recording (report §1)
    f"{run}: Static input digest mismatch; refresh the complete baseline" for run in RUN_IDS
]
RUN_METRICS = ("recall", "precision", "hallucination", "critical_recall", "validity")
ARM_ROWS: dict[str, Callable[[dict[str, Any]], str]] = {  # report row label -> arm value text
    "| Recall, среднее": lambda arm: str(arm["recall_mean"]["exact"]),
    "| Precision, среднее": lambda arm: str(arm["precision_mean"]["exact"]),
    "| Precision, pooled": lambda arm: _ratio(arm["precision_pooled"]),
    "| Hallucination Rate, среднее": lambda arm: str(arm["hallucination_mean"]["exact"]),
    "| Hallucination Rate, pooled": lambda arm: _ratio(arm["hallucination_pooled"]),
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


def _cells(row: str) -> list[str]:
    """The cells of a Markdown table row, split on the ``|`` that is not escaped."""
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", row)[1:-1]]


def _table(header: str) -> list[list[str]]:
    """The body rows (as cells) of the report table whose header row starts with ``header``."""
    lines = REPORT.read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith(header))
    body = []
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        body.append(_cells(line))
    return body


def _carries(cell: str, value: str) -> bool:
    """The cell is the value, or the value followed by a parenthesised remark."""
    return cell == value or cell.startswith(value + " (")


def _gated_runs() -> list[dict[str, Any]]:
    return [run for arm in _gate()["arms"].values() for run in arm["runs"]]


def _ratio(counts: dict[str, int]) -> str:
    return f"{counts['numerator']}/{counts['denominator']}"


def test_the_verdict_line_is_the_gate_verdict() -> None:
    gate = _gate()

    assert f"**{gate['verdict']}**" in REPORT.read_text(encoding="utf-8").split("\n\n")[1]


@pytest.mark.parametrize("arm_name", ["rag_off", "rag_on"])
def test_each_run_row_carries_its_own_ratios_and_digest(arm_name: str) -> None:
    metrics = {row[0]: row[1:] for row in _table("| Прогон | Recall (10) |")}
    digests = {row[0]: row[-1] for row in _table("| Прогон | Плечо (`rag_arm`) |")}

    for run in _gate()["arms"][arm_name]["runs"]:
        cells = metrics[run["run_id"]]

        assert len(cells) == len(RUN_METRICS), (run["run_id"], cells)
        for cell, name in zip(cells, RUN_METRICS, strict=True):
            assert _carries(cell, _ratio(run[name])), (run["run_id"], name, cell)
        assert digests[run["run_id"]] == f"`{run['responses_digest']}`", run["run_id"]


def test_each_arm_row_carries_the_arm_values_in_arm_order() -> None:
    arms = _gate()["arms"]

    for prefix, value in ARM_ROWS.items():
        rows = _rows(prefix)
        assert len(rows) == 1, (prefix, rows)
        _, arm_a, arm_b = _cells(rows[0])

        assert _carries(arm_a, value(arms["rag_off"])), (prefix, arm_a)
        assert _carries(arm_b, value(arms["rag_on"])), (prefix, arm_b)


def test_each_condition_row_carries_its_value_arms_and_status() -> None:
    for key, prefix in CONDITION_ROWS.items():
        condition = _gate()["conditions"][key]
        rows = _rows(prefix)
        assert len(rows) == 1, (prefix, rows)
        _, _, value, arm_a, arm_b, status = _cells(rows[0])

        if condition["exact"] is None:
            assert "n/a" in value, (key, value)
        else:
            assert _carries(value, condition["exact"]), (key, value)
        assert (arm_a, arm_b) == (condition.get("a", "-"), condition.get("b", "-")), (key, rows[0])
        assert status == condition["status"], (key, status)


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
    rows = {(row[0], row[1]): row for row in _table("| Кейс | Прогон | Находка")}
    for event in events:
        finding, anchor = event["finding"], event["anchor"]
        _, _, reported, forced, *_ = rows[(event["case_id"], event["run_id"])]

        assert reported == f"#{event['finding_index']}, `{finding['path']}:{finding['line']}`"
        assert forced == f"`{anchor['path']}:{anchor['line']}`"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (("verdict",), "FAIL"),
        (("conditions", "delta_recall", "exact"), "20"),
        (("conditions", "robust_wins", "exact"), "3"),
        (("conditions", "precision", "exact"), "-2575/459"),
        (("conditions", "tokens", "exact"), "4316/4239"),
        (("conditions", "injections", "exact"), "3"),
        (("conditions", "hallucination", "exact"), "-2675/459"),
        (("conditions", "hallucination", "status"), "pass"),
        (("oracle", "present"), True),
        (("oracle", "runs_covered"), 6),
        (("arms", "rag_off", "hallucination_mean", "exact"), "13/27"),
        (("arms", "rag_on", "hallucination_mean", "exact"), "259/612"),
        (("warnings",), STATIC_DRIFT_WARNINGS),
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
    provenance = {row[0]: row for row in _table("| Прогон | Плечо (`rag_arm`) |")}
    for run_id, workflow_run in RUN_IDS.items():
        manifest = json.loads((CAPTURES / run_id / "manifest.json").read_text(encoding="utf-8"))
        recorded = manifest["run_metadata"]["recorded_at"][:19].replace("T", " ")

        _, _, listed_run, listed_at, _ = provenance[run_id]
        assert (listed_run, listed_at) == (workflow_run, recorded), run_id
        _assert_row("| Промпт", [manifest["prompt_sha"]])
        _assert_row("| Корпус", [manifest["corpus_digest"]])
        _assert_row("| Статика", [manifest["static_digest"]])
        _assert_row("| Индекс RAG", [manifest["run_metadata"]["rag_index_digest"]])


def _confirmed_hallucinations() -> set[tuple[str, str, int]]:
    """``(run, case, finding index)`` of every entry of the committed oracle."""
    oracle = json.loads(ORACLE.read_text(encoding="utf-8"))
    return {
        (run_id, item["case_id"], item["finding_index"])
        for run_id, entry in oracle["runs"].items()
        for item in entry["hallucinations"]
    }


def test_each_oracle_entry_is_a_finding_of_a_valid_answer() -> None:
    oracle = json.loads(ORACLE.read_text(encoding="utf-8"))

    for run_id, entry in oracle["runs"].items():
        valid = _valid_answers(CAPTURES / run_id)
        confirmed = [(item["case_id"], item["finding_index"]) for item in entry["hallucinations"]]
        assert len(set(confirmed)) == len(confirmed), (run_id, confirmed)
        for item in entry["hallucinations"]:
            assert f"{item['case_id']}.json" in valid, (run_id, item)
            answer = json.loads(
                (CAPTURES / run_id / f"{item['case_id']}.json").read_text(encoding="utf-8")
            )
            assert 0 <= item["finding_index"] < len(answer["findings"]), (run_id, item)


def test_the_gate_numerators_are_the_oracle_entry_counts_per_run() -> None:
    oracle = json.loads(ORACLE.read_text(encoding="utf-8"))

    assert {run["run_id"]: run["hallucination"]["numerator"] for run in _gated_runs()} == {
        run_id: len(entry["hallucinations"]) for run_id, entry in oracle["runs"].items()
    }


def _reviewed_findings() -> set[tuple[str, str, int]]:
    """``(run, case, finding index)`` of every finding of every valid answer: the gate's 76."""
    return {
        (run["run_id"], name.removesuffix(".json"), index)
        for run in _gated_runs()
        for name in _valid_answers(CAPTURES / run["run_id"])
        for index in range(
            len(
                json.loads((CAPTURES / run["run_id"] / name).read_text(encoding="utf-8"))[
                    "findings"
                ]
            )
        )
    }


def test_the_report_lists_each_reviewed_finding_once_with_its_title_and_prompt() -> None:
    confirmed = _confirmed_hallucinations()

    rows = _table("| Прогон | Кейс #индекс |")
    listed = [(row[0], *row[1].split(" #")) for row in rows]
    assert len(listed) == len(set(listed)) == 76
    assert {(run_id, case_id, int(index)) for run_id, case_id, index in listed} == (
        _reviewed_findings()
    )
    for run_id, key, anchor, _, title, _, hallucination, *_, link in rows:
        case_id, index = key.split(" #")
        manifest = json.loads((CAPTURES / run_id / "manifest.json").read_text(encoding="utf-8"))
        digest = manifest["run_metadata"]["rag_evidence"][case_id]["prompt_digest"]
        file_name = f"{case_id}-{digest.removeprefix('sha256-v1:')[:12]}.txt"
        answer = json.loads((CAPTURES / run_id / f"{case_id}.json").read_text(encoding="utf-8"))
        finding = answer["findings"][int(index)]

        assert anchor == f"`{finding['path']}:{finding['line']}`", (run_id, key)
        assert title == finding["title"].replace("|", "\\|"), (run_id, key)
        assert (hallucination == "д") == ((run_id, case_id, int(index)) in confirmed), (run_id, key)
        assert link == f"[{file_name}]({PROMPT_LINK_DIR}/{file_name})"
        assert (PROMPTS / file_name).is_file(), (run_id, key)


def _prompt_files() -> dict[str, str]:
    """File name -> recorded digest of the prompt of every (run, case) with findings to review.

    The gate reviews the findings of the valid answers only; an answer the validator refuses
    is left out, as it was for the tech lead.
    """
    wanted: dict[str, str] = {}
    for run_id in sorted(RUN_IDS):
        manifest = json.loads((CAPTURES / run_id / "manifest.json").read_text(encoding="utf-8"))
        for case_id in sorted(_valid_answers(CAPTURES / run_id)):
            name = case_id.removesuffix(".json")
            answer = json.loads((CAPTURES / run_id / case_id).read_text(encoding="utf-8"))
            if answer["findings"]:
                digest = manifest["run_metadata"]["rag_evidence"][name]["prompt_digest"]
                wanted[f"{name}-{digest.removeprefix('sha256-v1:')[:12]}.txt"] = digest
    return wanted


def test_the_oracle_holds_the_exact_prompt_of_every_valid_answer_with_findings() -> None:
    """The user messages the tech lead read are committed byte for byte, pinned to the recording."""
    wanted = _prompt_files()
    system = SYSTEM_PROMPT.read_bytes().decode("utf-8")

    assert {path.name for path in PROMPTS.iterdir()} == set(wanted)
    for name, digest in wanted.items():
        user = (PROMPTS / name).read_bytes().decode("utf-8")
        assert _prompt_digest(system, user) == digest, name
