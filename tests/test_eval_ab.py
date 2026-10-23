"""A/B aggregation of repeated live-eval reports per arm (#163, AC8)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from review.scripts.eval_ab import AbError, aggregate, format_markdown, main

PROVENANCE = {
    "model_id": "model-a",
    "prompt_version": "v3",
    "static_digest": "sha256-v1:" + "b" * 64,
    "corpus_digest": "sha256-v1:" + "c" * 64,
}


def _report(
    arm: str,
    *,
    tp: int,
    fp: int,
    fn: int,
    rule: tuple[int, int] = (0, 0),
    exceptions: tuple[int, int] = (0, 0),
    usage: dict[str, Any] | None = None,
    publishable: bool = True,
    provenance: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "provenance": {
            **PROVENANCE,
            **(provenance or {}),
            "run_metadata": {"policy_arm": arm, "baseline_publishable": publishable},
        },
        "case_count": 8,
        "valid_response_count": 6,
        "micro": {"tp": tp, "fp": fp, "fn": fn},
        "critical": {"matched": 1, "total": 2},
        "rule_constraints": {"found": rule[0], "total": rule[1]},
        "exceptions": {"respected": exceptions[0], "applicable": exceptions[1]},
        "usage": usage,
    }


def _write(tmp_path: Path, name: str, report: dict[str, Any]) -> Path:
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def _usage(tokens_in: int, cost: str) -> dict[str, Any]:
    return {"cases": 8, "calls": 8, "tokens_in": tokens_in, "tokens_out": 80, "cost_usd": cost}


def _experiment(tmp_path: Path) -> list[tuple[str, Path]]:
    return [
        ("none", _write(tmp_path, "none-1", _report("none", tp=2, fp=2, fn=6))),
        (
            "valid",
            _write(
                tmp_path,
                "valid-1",
                _report(
                    "valid",
                    tp=4,
                    fp=1,
                    fn=4,
                    rule=(2, 5),
                    exceptions=(3, 4),
                    usage=_usage(1000, "0.01"),
                ),
            ),
        ),
        (
            "valid",
            _write(
                tmp_path,
                "valid-2",
                _report(
                    "valid",
                    tp=6,
                    fp=2,
                    fn=2,
                    rule=(4, 5),
                    exceptions=(4, 4),
                    usage=_usage(3000, "0.03"),
                ),
            ),
        ),
    ]


def test_aggregates_runs_per_arm_with_numerators_spread_and_deltas(tmp_path: Path) -> None:
    result = aggregate(_experiment(tmp_path))

    assert result["baseline"] == "none"
    assert result["provenance"] == PROVENANCE
    none, valid = result["arms"]
    assert (none["label"], valid["label"]) == ("none", "valid")
    assert [row["metrics"]["recall"] for row in valid["runs"]] == [
        {"numerator": 4, "denominator": 8, "value": 0.5},
        {"numerator": 6, "denominator": 8, "value": 0.75},
    ]
    assert valid["summary"]["recall"] == {"mean": 0.625, "min": 0.5, "max": 0.75}
    assert valid["summary"]["rule_constraints_found"] == {
        "mean": pytest.approx(0.6),
        "min": 0.4,
        "max": 0.8,
    }
    assert valid["summary"]["exceptions_respected"] == {
        "mean": pytest.approx(0.875),
        "min": 0.75,
        "max": 1.0,
    }
    assert valid["summary"]["tokens_in"] == {"mean": 2000.0, "min": 1000.0, "max": 3000.0}
    assert valid["summary"]["cost_usd"] == {"mean": pytest.approx(0.02), "min": 0.01, "max": 0.03}
    assert none["summary"]["rule_constraints_found"] == {"mean": None, "min": None, "max": None}
    assert none["summary"]["tokens_in"] == {"mean": None, "min": None, "max": None}
    assert none["runs"][0]["metrics"]["rule_constraints_found"] == {
        "numerator": 0,
        "denominator": 0,
        "value": None,
    }
    assert valid["delta"]["recall"] == pytest.approx(0.375)
    assert valid["delta"]["precision"] == pytest.approx((0.8 + 0.75) / 2 - 0.5)
    assert valid["delta"]["rule_constraints_found"] is None
    assert none["delta"]["recall"] == 0.0


def test_explicit_baseline_moves_the_deltas(tmp_path: Path) -> None:
    result = aggregate(_experiment(tmp_path), baseline="valid")

    none, valid = result["arms"]
    assert result["baseline"] == "valid"
    assert valid["delta"]["recall"] == 0.0
    assert none["delta"]["recall"] == pytest.approx(-0.375)


@pytest.mark.parametrize("field", ["model_id", "prompt_version", "static_digest", "corpus_digest"])
def test_refuses_reports_of_different_experiments(tmp_path: Path, field: str) -> None:
    runs = _experiment(tmp_path)
    other = _write(
        tmp_path, "other", _report("none", tp=1, fp=1, fn=1, provenance={field: "different"})
    )

    with pytest.raises(AbError) as raised:
        aggregate([*runs, ("none", other)])

    assert str(raised.value) == f"{other}: {field} differs from {runs[0][1]}"


def test_policy_arm_label_must_match_the_recorded_arm(tmp_path: Path) -> None:
    path = _write(tmp_path, "r", _report("none", tp=1, fp=0, fn=0))

    with pytest.raises(AbError) as raised:
        aggregate([("valid", path)])

    assert str(raised.value) == f"{path}: labelled valid but captured with policy arm none"


def test_free_form_label_takes_any_recorded_arm(tmp_path: Path) -> None:
    path = _write(tmp_path, "r", _report("valid", tp=1, fp=0, fn=0))

    result = aggregate([("rag", path)])

    assert [arm["label"] for arm in result["arms"]] == ["rag"]


def test_legacy_capture_without_recorded_arm_is_legacy(tmp_path: Path) -> None:
    report = _report("legacy", tp=1, fp=0, fn=0)
    del report["provenance"]["run_metadata"]["policy_arm"]
    path = _write(tmp_path, "r", report)

    assert aggregate([("legacy", path)])["arms"][0]["label"] == "legacy"
    with pytest.raises(AbError, match="labelled none but captured with policy arm legacy"):
        aggregate([("none", path)])


def test_refuses_a_nonpublishable_capture(tmp_path: Path) -> None:
    path = _write(tmp_path, "r", _report("none", tp=1, fp=0, fn=0, publishable=False))

    with pytest.raises(AbError) as raised:
        aggregate([("none", path)])

    assert str(raised.value) == f"{path}: capture is not publishable; rerun the arm"


def test_refuses_a_report_given_twice_and_an_unknown_baseline(tmp_path: Path) -> None:
    path = _write(tmp_path, "r", _report("none", tp=1, fp=0, fn=0))

    with pytest.raises(AbError, match="the same report is given twice"):
        aggregate([("none", path), ("none", path)])
    with pytest.raises(AbError, match="baseline arm valid has no run"):
        aggregate([("none", path)], baseline="valid")
    with pytest.raises(AbError, match="at least one --run"):
        aggregate([])


def test_refuses_a_report_without_policy_arm_metrics(tmp_path: Path) -> None:
    report = _report("none", tp=1, fp=0, fn=0)
    del report["rule_constraints"]
    path = _write(tmp_path, "r", report)

    with pytest.raises(AbError, match="not an eval report with policy-arm metrics"):
        aggregate([("none", path)])


def test_markdown_table_shows_spread_and_every_run_fraction(tmp_path: Path) -> None:
    text = format_markdown(aggregate(_experiment(tmp_path)))

    lines = text.splitlines()
    assert lines[0] == (
        "Model `model-a`, prompt v3, static `sha256-v1:"
        + "b" * 64
        + "`, corpus `sha256-v1:"
        + "c" * 64
        + "`."
    )
    assert lines[2] == (
        "| Arm | Runs | Precision | Recall | Critical Recall | Validity | Rule constraints found "
        "| Exceptions respected | Tokens in | Tokens out | Cost USD |"
    )
    assert lines[5] == (
        "| valid | 2 | 77.5% (75.0%–80.0%) [4/5, 6/8] | 62.5% (50.0%–75.0%) [4/8, 6/8] "
        "| 50.0% (50.0%–50.0%) [1/2, 1/2] | 75.0% (75.0%–75.0%) [6/8, 6/8] "
        "| 60.0% (40.0%–80.0%) [2/5, 4/5] | 87.5% (75.0%–100.0%) [3/4, 4/4] "
        "| 2000 (1000–3000) | 80 (80–80) | 0.020000 (0.010000–0.030000) |"
    )
    assert lines[4].startswith("| none | 1 | 50.0% (50.0%–50.0%) [2/4] | 25.0% (25.0%–25.0%) [2/8]")
    assert lines[4].endswith(
        "| n/a (n/a–n/a) [0/0] | n/a (n/a–n/a) [0/0] | n/a (n/a–n/a) "
        "| n/a (n/a–n/a) | n/a (n/a–n/a) |"
    )
    assert lines[-1] == (
        "| valid | +27.5 pp | +37.5 pp | +0.0 pp | +0.0 pp | n/a | n/a | n/a | n/a | n/a |"
    )


def test_cli_prints_json_or_markdown_and_reports_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = _experiment(tmp_path)
    arguments = [item for label, path in runs for item in ("--run", f"{label}={path}")]

    assert main(arguments) == 0
    assert json.loads(capsys.readouterr().out)["baseline"] == "none"
    assert main([*arguments, "--markdown", "--baseline", "valid"]) == 0
    assert capsys.readouterr().out.startswith("Model `model-a`")
    assert main([*arguments, "--baseline", "rag"]) == 1
    assert capsys.readouterr().err == "eval ab: baseline arm rag has no run\n"
    with pytest.raises(SystemExit) as raised:
        main(["--run", "no-separator"])
    assert raised.value.code == 2
