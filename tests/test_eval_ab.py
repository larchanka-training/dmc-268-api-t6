"""A/B aggregation of repeated live-eval reports per arm (#163, AC8)."""

from __future__ import annotations

import asyncio
import copy
import json
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from review.scripts import eval_live
from review.scripts.eval_ab import AbError, aggregate, format_markdown, main
from review.scripts.eval_live import BACKEND_RULES, FRONTEND_RULES, REPO_ROOT, record_live
from review.scripts.eval_rag_rehearsal import DEFAULT_MODEL, RehearsalTransport, rehearsal_settings
from review.scripts.eval_replay import replay
from review.scripts.validate_dataset import DEFAULT_ROOT
from tests.eval_rag_fakes import rag_root, record_rag, valid_answers, window_without_rag

SYSTEM_V3 = REPO_ROOT / "review/prompts/review.system.v3.md"
SYSTEM_V5 = REPO_ROOT / "review/prompts/review.system.v5.md"
PROVENANCE = {
    "model_id": "model-a",
    "prompt_version": "v3",
    "static_digest": "sha256-v1:" + "b" * 64,
    "corpus_digest": "sha256-v1:" + "c" * 64,
}


RUN_METADATA: dict[str, Any] = {
    "engine": "fast",
    "effective_settings": {"temperature": 0, "max_output_tokens": 4096},
    "fallback_model_id": None,
}


def _report(
    arm: str,
    *,
    tp: int,
    fp: int,
    fn: int,
    rule: tuple[int, int] = (0, 0),
    exceptions: tuple[int, int] = (0, 0),  # (respected, total)
    usage: dict[str, Any] | None = None,
    publishable: bool = True,
    provenance: dict[str, str] | None = None,
    run_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "provenance": {
            **PROVENANCE,
            **(provenance or {}),
            "run_metadata": {
                **RUN_METADATA,
                **(run_metadata or {}),
                "policy_arm": arm,
                "baseline_publishable": publishable,
            },
        },
        "case_count": 8,
        "valid_response_count": 6,
        "micro": {"tp": tp, "fp": fp, "fn": fn},
        "critical": {"matched": 1, "total": 2},
        "rule_constraints": {"found": rule[0], "total": rule[1]},
        "exceptions": {
            "total": exceptions[1],
            "flagged": exceptions[1] - exceptions[0],
            "respected": exceptions[0],
        },
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
        (
            "none",
            _write(
                tmp_path,
                "none-1",
                _report("none", tp=2, fp=2, fn=6, rule=(1, 5), exceptions=(0, 4)),
            ),
        ),
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
    assert result["provenance"] == {**PROVENANCE, "run_metadata": RUN_METADATA}
    assert result["warnings"] == []
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
    assert valid["summary"]["exceptions_flagged"] == {"mean": 0.125, "min": 0.0, "max": 0.25}
    # Arm none: the rule truths found without rules and the exception anchors it flagged.
    assert none["summary"]["rule_constraints_found"] == {"mean": 0.2, "min": 0.2, "max": 0.2}
    assert none["runs"][0]["metrics"]["exceptions_flagged"] == {
        "numerator": 4,
        "denominator": 4,
        "value": 1.0,
    }
    assert none["summary"]["tokens_in"] == {"mean": None, "min": None, "max": None}
    assert valid["delta"]["recall"] == pytest.approx(0.375)
    assert valid["delta"]["precision"] == pytest.approx((0.8 + 0.75) / 2 - 0.5)
    assert valid["delta"]["rule_constraints_found"] == pytest.approx(0.4)
    assert valid["delta"]["exceptions_respected"] == pytest.approx(0.875)
    assert valid["delta"]["exceptions_flagged"] == pytest.approx(-0.875)
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
    copy = _write(tmp_path, "copy", _report("none", tp=1, fp=0, fn=0))

    with pytest.raises(AbError) as raised:
        aggregate([("none", path), ("none", path)])
    assert str(raised.value) == f"{path}: same report content as {path}"
    # A copy under another name is the same run as well.
    with pytest.raises(AbError) as raised:
        aggregate([("none", path), ("none", copy)])
    assert str(raised.value) == f"{copy}: same report content as {path}"
    with pytest.raises(AbError, match="baseline arm valid has no run"):
        aggregate([("none", path)], baseline="valid")
    with pytest.raises(AbError, match="at least one --run"):
        aggregate([])


def test_refuses_a_report_without_policy_arm_metrics(tmp_path: Path) -> None:
    report = _report("none", tp=1, fp=0, fn=0)
    del report["rule_constraints"]
    path = _write(tmp_path, "r", report)

    with pytest.raises(AbError) as raised:
        aggregate([("none", path)])
    assert str(raised.value) == (
        f"{path}: not an eval report with provenance and policy-arm metrics ('rule_constraints')"
    )


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
        "| Exceptions respected | Exceptions flagged | Tokens in | Tokens out | Cost USD |"
    )
    assert lines[5] == (
        "| valid | 2 | 77.5% (75.0%–80.0%) [4/5, 6/8] | 62.5% (50.0%–75.0%) [4/8, 6/8] "
        "| 50.0% (50.0%–50.0%) [1/2, 1/2] | 75.0% (75.0%–75.0%) [6/8, 6/8] "
        "| 60.0% (40.0%–80.0%) [2/5, 4/5] | 87.5% (75.0%–100.0%) [3/4, 4/4] "
        "| 12.5% (0.0%–25.0%) [1/4, 0/4] "
        "| 2000 (1000–3000) | 80 (80–80) | 0.020000 (0.010000–0.030000) |"
    )
    assert lines[4].startswith("| none | 1 | 50.0% (50.0%–50.0%) [2/4] | 25.0% (25.0%–25.0%) [2/8]")
    assert lines[4].endswith(
        "| 20.0% (20.0%–20.0%) [1/5] | 0.0% (0.0%–0.0%) [0/4] | 100.0% (100.0%–100.0%) [4/4] "
        "| n/a (n/a–n/a) | n/a (n/a–n/a) | n/a (n/a–n/a) |"
    )
    assert lines[-1] == (
        "| valid | +27.5 pp | +37.5 pp | +0.0 pp | +0.0 pp | +40.0 pp | +87.5 pp | -87.5 pp "
        "| n/a | n/a | n/a |"
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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("engine", "deep"),
        ("effective_settings", {"temperature": 0, "max_output_tokens": 2048}),
        ("fallback_model_id", "model-b"),
    ],
)
def test_refuses_reports_recorded_with_other_run_settings(
    tmp_path: Path, field: str, value: object
) -> None:
    runs = _experiment(tmp_path)
    other = _write(
        tmp_path, "other", _report("none", tp=1, fp=1, fn=1, run_metadata={field: value})
    )

    with pytest.raises(AbError) as raised:
        aggregate([*runs, ("none", other)])

    assert str(raised.value) == f"{other}: run_metadata.{field} differs from {runs[0][1]}"


def _drop_provenance(report: dict[str, Any], key: str) -> None:
    if key.startswith("run_metadata."):
        del report["provenance"]["run_metadata"][key.removeprefix("run_metadata.")]
    else:
        del report["provenance"][key]


@pytest.mark.parametrize(
    "key",
    [
        "model_id",
        "prompt_version",
        "static_digest",
        "corpus_digest",
        "run_metadata.engine",
        "run_metadata.effective_settings",
        "run_metadata.fallback_model_id",
    ],
)
def test_refuses_a_report_without_a_comparability_key(tmp_path: Path, key: str) -> None:
    report = _report("none", tp=1, fp=0, fn=0)
    _drop_provenance(report, key)
    path = _write(tmp_path, "r", report)

    with pytest.raises(AbError) as raised:
        aggregate([("none", path)])

    assert str(raised.value) == (
        f"{path}: not an eval report with provenance and policy-arm metrics ('{key}')"
    )


@pytest.mark.parametrize(
    ("provenance", "diagnostic"),
    [
        ([], "list indices must be integers or slices, not str"),
        ({**PROVENANCE, "run_metadata": []}, "provenance and run_metadata must be objects"),
        ({**PROVENANCE, "run_metadata": "fast"}, "provenance and run_metadata must be objects"),
    ],
    ids=["provenance-list", "metadata-list", "metadata-string"],
)
def test_refuses_a_report_with_malformed_provenance(
    tmp_path: Path, provenance: object, diagnostic: str
) -> None:
    report = _report("none", tp=1, fp=0, fn=0)
    report["provenance"] = provenance
    path = _write(tmp_path, "r", report)

    with pytest.raises(AbError) as raised:
        aggregate([("none", path)])

    assert str(raised.value) == (
        f"{path}: not an eval report with provenance and policy-arm metrics ({diagnostic})"
    )


def test_warns_when_only_some_runs_of_an_arm_carry_usage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = [
        *_experiment(tmp_path),
        ("valid", _write(tmp_path, "valid-3", _report("valid", tp=5, fp=1, fn=3))),
    ]

    result = aggregate(runs)

    warning = "arm valid: only 2 of 3 runs carry usage; its tokens and cost cover those runs only"
    assert result["warnings"] == [warning]
    assert format_markdown(result).splitlines()[-2:] == ["", f"Warning: {warning}"]
    arguments = [item for label, path in runs for item in ("--run", f"{label}={path}")]
    assert main(arguments) == 0
    assert capsys.readouterr().err == f"eval ab: warning: {warning}\n"


def _set_path(report: dict[str, Any], keys: str, value: object) -> None:
    *parents, last = keys.split(".")
    target: Any = report
    for key in parents:
        target = target[key]
    target[last] = value


@pytest.mark.parametrize(
    ("keys", "value", "diagnostic"),
    [
        ("critical.matched", "1", "critical_recall needs non-negative integer counts"),
        ("micro.tp", True, "precision needs non-negative integer counts"),
        ("micro.fn", 1.5, "recall needs non-negative integer counts"),
        ("critical.total", -1, "critical_recall needs non-negative integer counts"),
        ("case_count", None, "validity needs non-negative integer counts"),
        (
            "rule_constraints.found",
            False,
            "rule_constraints_found needs non-negative integer counts",
        ),
        ("exceptions.flagged", "0", "exceptions_flagged needs non-negative integer counts"),
        ("usage", [], "usage must be an object or null"),
        (
            "usage",
            {**_usage(10, "0.1"), "tokens_in": True},
            "usage.tokens_in must be a non-negative integer",
        ),
        (
            "usage",
            {**_usage(10, "0.1"), "tokens_out": "80"},
            "usage.tokens_out must be a non-negative integer",
        ),
        ("usage", _usage(10, "ten cents"), "usage.cost_usd must be a non-negative decimal"),
        ("usage", _usage(10, "NaN"), "usage.cost_usd must be a non-negative decimal"),
        ("usage", _usage(10, "-0.1"), "usage.cost_usd must be a non-negative decimal"),
        (
            "usage",
            {**_usage(10, "0.1"), "cost_usd": 0.1},
            "usage.cost_usd must be a non-negative decimal",
        ),
    ],
    ids=[
        "string-count",
        "bool-count",
        "float-count",
        "negative-denominator",
        "null-denominator",
        "bool-rule-count",
        "string-exception-count",
        "usage-list",
        "bool-tokens",
        "string-tokens",
        "word-cost",
        "nan-cost",
        "negative-cost",
        "float-cost",
    ],
)
def test_refuses_a_report_with_malformed_counts(
    tmp_path: Path, keys: str, value: object, diagnostic: str
) -> None:
    report = _report("none", tp=1, fp=0, fn=0, usage=_usage(10, "0.1"))
    _set_path(report, keys, value)
    path = _write(tmp_path, "r", report)

    with pytest.raises(AbError) as raised:
        aggregate([("none", path)])

    assert str(raised.value) == (
        f"{path}: not an eval report with provenance and policy-arm metrics ({diagnostic})"
    )


def test_accepts_integer_cost_and_zero_counts(tmp_path: Path) -> None:
    report = _report("none", tp=0, fp=0, fn=0, usage={**_usage(0, "0"), "cost_usd": 0})
    path = _write(tmp_path, "r", report)

    result = aggregate([("none", path)])

    assert result["arms"][0]["summary"]["cost_usd"] == {"mean": 0.0, "min": 0.0, "max": 0.0}


def test_markdown_notes_that_arm_none_shows_no_rules(tmp_path: Path) -> None:
    lines = format_markdown(aggregate(_experiment(tmp_path))).splitlines()
    note = (
        "Arm `none` (policy arm none) shows no rules: its Rule constraints found and "
        "Exceptions flagged are found and flagged without rules, and Exceptions respected "
        "does not apply."
    )

    assert lines[6:8] == ["", note]
    assert [arm["policy_arms"] for arm in aggregate(_experiment(tmp_path))["arms"]] == [
        ["none"],
        ["valid"],
    ]
    relabelled = [("baseline", path) for _, path in _experiment(tmp_path)[:1]]
    assert format_markdown(aggregate(relabelled)).count("(policy arm none) shows no rules") == 1
    valid_only = [item for item in _experiment(tmp_path) if item[0] == "valid"]
    assert "shows no rules" not in format_markdown(aggregate(valid_only))


def test_a_count_that_breaks_the_arithmetic_is_refused_as_well(tmp_path: Path) -> None:
    report = _report("none", tp=1, fp=0, fn=0)
    report["micro"]["tp"] = "2"
    path = _write(tmp_path, "r", report)

    with pytest.raises(AbError, match="not an eval report with provenance and policy-arm metrics"):
        aggregate([("none", path)])


# --- the RAG arms (#146): rag_off / rag_on over recorded captures of the RAG fixture cases ---


@pytest.fixture(scope="module")
def rag_captures(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict[str, Any]]:
    """Replay reports of both arms (and of a graph arm whose RAG slot the gateway fitted away),
    recorded with a fake transport over the fixture cases."""
    tmp_path = tmp_path_factory.mktemp("rag-ab")
    root = rag_root(tmp_path, {"RAG-01": ("spike", "reverse_edge")})
    reports: dict[str, dict[str, Any]] = {}
    for name, arm, model_settings in (
        ("none", "none", None),
        ("graph", "graph", None),
        ("fitted", "graph", window_without_rag(root, "RAG-01")),
    ):
        responses = tmp_path / name
        record_rag(
            root, valid_answers(1), arm, model_settings=model_settings, responses_dir=responses
        )
        reports[name] = replay(root, responses_dir=responses)
    return reports


def _rag_run(
    tmp_path: Path, name: str, capture: dict[str, Any], run: int, edit: Any = None
) -> Path:
    """The report of one run: runs of an arm must differ in bytes, so each carries its number."""
    report = copy.deepcopy(capture)
    report["run"] = run
    if edit is not None:
        edit(report["provenance"]["run_metadata"])
    return _write(tmp_path, name, report)


def _rag_pair(
    tmp_path: Path, rag_captures: dict[str, dict[str, Any]], *, on: str = "graph"
) -> list[tuple[str, Path]]:
    return [
        ("rag_off", _rag_run(tmp_path, "off-1", rag_captures["none"], 1)),
        ("rag_off", _rag_run(tmp_path, "off-2", rag_captures["none"], 2)),
        ("rag_on", _rag_run(tmp_path, "on-1", rag_captures[on], 1)),
    ]


def test_a_correctly_labelled_rag_pair_is_aggregated(
    tmp_path: Path, rag_captures: dict[str, dict[str, Any]]
) -> None:
    result = aggregate(_rag_pair(tmp_path, rag_captures), baseline="rag_off")

    assert [(arm["label"], len(arm["runs"])) for arm in result["arms"]] == [
        ("rag_off", 2),
        ("rag_on", 1),
    ]
    assert result["baseline"] == "rag_off"
    assert result["warnings"] == []


@pytest.mark.parametrize(
    ("labels", "message"),
    [
        (("rag_on", "rag_on"), r"off-1.*captured with RAG arm none.*labelled rag_off, not rag_on"),
        (("none", "rag_on"), r"off-1.*labelled rag_off, not none"),
        (("rag_off", "rag_off"), r"on-1.*captured with RAG arm graph.*labelled rag_on"),
        (("rag_off", "valid"), r"on-1.*labelled valid but captured with policy arm none"),
    ],
    ids=["swapped", "policy-label", "off-labelled-twice", "on-has-policy-label"],
)
def test_a_mislabelled_rag_capture_is_refused(
    tmp_path: Path,
    rag_captures: dict[str, dict[str, Any]],
    labels: tuple[str, str],
    message: str,
) -> None:
    off = _rag_run(tmp_path, "off-1", rag_captures["none"], 1)
    on = _rag_run(tmp_path, "on-1", rag_captures["graph"], 1)

    with pytest.raises(AbError, match=message):
        aggregate([(labels[0], off), (labels[1], on)])


def test_a_rag_label_on_a_capture_without_a_rag_arm_is_refused(tmp_path: Path) -> None:
    plain = _write(tmp_path, "plain", _report("none", tp=1, fp=0, fn=1))

    with pytest.raises(AbError, match="labelled rag_on but captured without a RAG arm"):
        aggregate([("rag_on", plain)])


def test_an_unknown_recorded_rag_arm_is_refused(
    tmp_path: Path, rag_captures: dict[str, dict[str, Any]]
) -> None:
    path = _rag_run(
        tmp_path, "odd", rag_captures["graph"], 1, lambda meta: meta.update(rag_arm="vector")
    )

    with pytest.raises(AbError, match="unknown RAG arm vector"):
        aggregate([("rag_on", path)])


def test_a_graph_case_whose_rag_slot_was_fitted_away_is_refused(
    tmp_path: Path, rag_captures: dict[str, dict[str, Any]]
) -> None:
    runs = _rag_pair(tmp_path, rag_captures, on="fitted")

    with pytest.raises(AbError, match=r"on-1.*fitted away: RAG-01"):
        aggregate(runs)


def test_the_rag_off_arm_must_have_sent_no_retrieved_block(
    tmp_path: Path, rag_captures: dict[str, dict[str, Any]]
) -> None:
    graph_item = rag_captures["graph"]["provenance"]["run_metadata"]["rag_evidence"]["RAG-01"]

    def leaked(metadata: dict[str, Any]) -> None:
        item = metadata["rag_evidence"]["RAG-01"]
        # A forged record, consistent in itself: only the arm contradicts its blocks.
        item["retrieved"] = graph_item["retrieved"]
        item["offline_retrieved_ids"] = graph_item["offline_retrieved_ids"]
        item["prompt_digest"] = item["non_rag_prompt_digest"]

    off = _rag_run(tmp_path, "off-1", rag_captures["none"], 1, leaked)
    on = _rag_run(tmp_path, "on-1", rag_captures["graph"], 1)

    with pytest.raises(AbError, match=r"off-1.*rag_off sent retrieved blocks for RAG-01"):
        aggregate([("rag_off", off), ("rag_on", on)])


def test_the_arms_must_have_been_sent_the_same_non_rag_prompt(
    tmp_path: Path, rag_captures: dict[str, dict[str, Any]]
) -> None:
    def other_prompt(metadata: dict[str, Any]) -> None:
        metadata["rag_evidence"]["RAG-01"]["non_rag_prompt_digest"] = "sha256-v1:" + "0" * 64

    off = _rag_run(tmp_path, "off-1", rag_captures["none"], 1)
    on = _rag_run(tmp_path, "on-1", rag_captures["graph"], 1, other_prompt)

    with pytest.raises(AbError, match=r"on-1.*RAG-01.*another non-RAG prompt than in .*off-1"):
        aggregate([("rag_off", off), ("rag_on", on)])
    # Runs of one arm are held to the same: one digest per case over every run.
    on_again = _rag_run(tmp_path, "on-2", rag_captures["graph"], 2, other_prompt)
    on_third = _rag_run(tmp_path, "on-3", rag_captures["graph"], 3)
    with pytest.raises(AbError, match=r"on-3.*RAG-01.*another non-RAG prompt"):
        aggregate([("rag_on", on_again), ("rag_on", on_third)])


def test_a_rag_report_without_evidence_for_a_case_is_refused(
    tmp_path: Path, rag_captures: dict[str, dict[str, Any]]
) -> None:
    def no_request(metadata: dict[str, Any]) -> None:
        metadata["rag_evidence"]["RAG-01"] = None

    path = _rag_run(tmp_path, "on-1", rag_captures["graph"], 1, no_request)

    with pytest.raises(AbError, match="case RAG-01 has no recorded evidence"):
        aggregate([("rag_on", path)])


def test_rag_reports_must_cover_the_same_cases(
    tmp_path: Path, rag_captures: dict[str, dict[str, Any]]
) -> None:
    def other_case(metadata: dict[str, Any]) -> None:
        metadata["rag_evidence"]["RAG-09"] = metadata["rag_evidence"].pop("RAG-01")

    off = _rag_run(tmp_path, "off-1", rag_captures["none"], 1)
    on = _rag_run(tmp_path, "on-1", rag_captures["graph"], 1, other_case)

    with pytest.raises(AbError, match=r"on-1.*covers other cases than .*off-1"):
        aggregate([("rag_off", off), ("rag_on", on)])


def test_the_cli_refuses_a_mislabelled_rag_capture(
    tmp_path: Path,
    rag_captures: dict[str, dict[str, Any]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    off = _rag_run(tmp_path, "off-1", rag_captures["none"], 1)

    assert main(["--run", f"rag_on={off}"]) == 1
    assert "captured with RAG arm none" in capsys.readouterr().err


def test_an_unhashable_recorded_rag_arm_is_refused_not_a_crash(
    tmp_path: Path, rag_captures: dict[str, dict[str, Any]]
) -> None:
    path = _rag_run(
        tmp_path, "odd", rag_captures["graph"], 1, lambda meta: meta.update(rag_arm=["graph"])
    )

    with pytest.raises(AbError, match="unknown RAG arm"):
        aggregate([("rag_on", path)])


def test_a_rag_off_label_on_a_capture_without_a_rag_arm_is_refused(tmp_path: Path) -> None:
    plain = _write(tmp_path, "plain", _report("none", tp=1, fp=0, fn=1))

    with pytest.raises(AbError, match="labelled rag_off but captured without a RAG arm"):
        aggregate([("rag_off", plain)])


def test_rag_evidence_that_is_not_an_object_is_refused(
    tmp_path: Path, rag_captures: dict[str, dict[str, Any]]
) -> None:
    path = _rag_run(
        tmp_path, "on-1", rag_captures["graph"], 1, lambda meta: meta.update(rag_evidence=[])
    )

    with pytest.raises(AbError, match="rag_evidence must be an object"):
        aggregate([("rag_on", path)])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("non_rag_prompt_digest", 5),
        ("prompt_digest", None),
        ("retrieved", "none"),
        ("offline_retrieved_ids", "none"),
        ("trimmed", "no"),
        ("trimmed", 0),
    ],
)
def test_malformed_rag_evidence_fields_are_refused(
    tmp_path: Path, rag_captures: dict[str, dict[str, Any]], field: str, value: object
) -> None:
    def edit(metadata: dict[str, Any]) -> None:
        metadata["rag_evidence"]["RAG-01"][field] = value

    path = _rag_run(tmp_path, "on-1", rag_captures["graph"], 1, edit)

    with pytest.raises(AbError, match="case RAG-01 has malformed evidence"):
        aggregate([("rag_on", path)])


def test_a_trimmed_flag_that_contradicts_the_records_is_refused(
    tmp_path: Path, rag_captures: dict[str, dict[str, Any]]
) -> None:
    """``trimmed`` is derived (sent ids differ from the offline ids): a record that hides a
    fitted-away slot behind ``trimmed: false`` is not evidence."""

    def hidden(metadata: dict[str, Any]) -> None:
        metadata["rag_evidence"]["RAG-01"]["retrieved"] = []

    path = _rag_run(tmp_path, "on-1", rag_captures["graph"], 1, hidden)

    with pytest.raises(AbError, match=r"on-1.*trimmed flag.*RAG-01"):
        aggregate([("rag_on", path)])


def test_a_rag_off_prompt_that_is_not_the_non_rag_prompt_is_refused(
    tmp_path: Path, rag_captures: dict[str, dict[str, Any]]
) -> None:
    def other(metadata: dict[str, Any]) -> None:
        metadata["rag_evidence"]["RAG-01"]["prompt_digest"] = "sha256-v1:" + "0" * 64

    path = _rag_run(tmp_path, "off-1", rag_captures["none"], 1, other)

    with pytest.raises(AbError, match=r"off-1.*prompt of rag_off case RAG-01"):
        aggregate([("rag_off", path)])


def test_a_nonpublishable_rag_capture_is_reported_as_such(
    tmp_path: Path, rag_captures: dict[str, dict[str, Any]]
) -> None:
    """A capture whose case never reached the provider has null evidence; the reason to
    rerun the arm is that it is not publishable, not a shape error."""

    def failed(metadata: dict[str, Any]) -> None:
        metadata["baseline_publishable"] = False
        metadata["rag_evidence"]["RAG-01"] = None

    path = _rag_run(tmp_path, "on-1", rag_captures["graph"], 1, failed)

    with pytest.raises(AbError, match="not publishable"):
        aggregate([("rag_on", path)])


def _capture(
    root: Path, directory: Path, system: Path, *, repo: Path = REPO_ROOT
) -> dict[str, Any]:
    """The report of one real capture, offline: record with the rehearsal's transport, replay."""
    directory.mkdir(parents=True)
    responses = directory / "responses"
    asyncio.run(
        record_live(
            root,
            rehearsal_settings(DEFAULT_MODEL),
            system_prompt=system,
            backend_rules=repo / BACKEND_RULES.relative_to(REPO_ROOT),
            frontend_rules=repo / FRONTEND_RULES.relative_to(REPO_ROOT),
            transport=RehearsalTransport(),
            policy_arm="none",
            responses_dir=responses,
        )
    )
    return replay(root, responses_dir=responses)


@pytest.fixture(scope="module")
def prompt_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One gold case: what the prompt A/B chain needs, at a fraction of the corpus's time."""
    root = tmp_path_factory.mktemp("prompt-ab") / "dataset"
    shutil.copytree(DEFAULT_ROOT / "cases" / "CLEAN-01", root / "cases" / "CLEAN-01")
    return root


@pytest.fixture(scope="module")
def prompt_reports(
    tmp_path_factory: pytest.TempPathFactory, prompt_root: Path
) -> dict[str, dict[str, Any]]:
    directory = tmp_path_factory.mktemp("prompt-ab-captures")
    return {
        "v3": _capture(prompt_root, directory / "v3", SYSTEM_V3),
        "v5": _capture(prompt_root, directory / "v5", SYSTEM_V5),
    }


def test_captures_of_two_prompts_record_one_digest_of_the_other_static_inputs(
    prompt_reports: dict[str, dict[str, Any]],
) -> None:
    """That digest moves with the other inputs: the one-byte test below shows it."""
    older = prompt_reports["v3"]["provenance"]
    newer = prompt_reports["v5"]["provenance"]
    digest = older["run_metadata"]["static_digest_without_prompt"]

    assert re.fullmatch(r"sha256-v1:[0-9a-f]{64}", digest)
    assert newer["run_metadata"]["static_digest_without_prompt"] == digest
    assert older["static_digest"] != newer["static_digest"]
    assert digest not in (older["static_digest"], newer["static_digest"])


def test_captures_that_differ_only_in_the_prompt_give_one_table_with_tokens_and_deltas(
    tmp_path: Path, prompt_reports: dict[str, dict[str, Any]]
) -> None:
    older, newer = prompt_reports["v3"], prompt_reports["v5"]
    assert older["provenance"]["static_digest"] != newer["provenance"]["static_digest"]

    result = aggregate(
        [
            ("v3", _write(tmp_path, "v3", older)),
            ("v5", _write(tmp_path, "v5", newer)),
        ]
    )

    assert result["baseline"] == "v3"
    assert [(arm["prompt_version"], arm["prompt_path"]) for arm in result["arms"]] == [
        ("v3", "review/prompts/review.system.v3.md"),
        ("v5", "review/prompts/review.system.v5.md"),
    ]
    assert [arm["prompt_sha"] for arm in result["arms"]] == [
        older["provenance"]["prompt_sha"],
        newer["provenance"]["prompt_sha"],
    ]
    growth = newer["usage"]["tokens_in"] - older["usage"]["tokens_in"]
    assert growth > 0
    assert result["arms"][0]["delta"]["tokens_in"] == 0.0
    assert result["arms"][1]["delta"]["tokens_in"] == growth

    # No single prompt or static digest describes the experiment; each arm names its own.
    assert "prompt_version" not in result["provenance"]
    assert "static_digest" not in result["provenance"]
    digest = older["provenance"]["run_metadata"]["static_digest_without_prompt"]
    assert result["provenance"]["static_digest_without_prompt"] == digest
    table = format_markdown(result)
    assert f"static inputs without the prompt `{digest}`" in table
    assert older["provenance"]["static_digest"] not in table
    assert (
        "- Arm `v5`: prompt v5, `review/prompts/review.system.v5.md`, "
        f"SHA-256 `{newer['provenance']['prompt_sha']}`."
    ) in table
    assert "Tokens in" in table
    assert f"+{growth} " in table


REST_OF_THE_INPUTS = "sha256-v1:" + "d" * 64


def _prompt_report(
    version: str,
    *,
    tokens: int = 1000,
    arm: str = "none",
    provenance: dict[str, str] | None = None,
    run_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A report of a prompt A/B: the prompt's fields follow ``version``, the rest is shared."""
    return _report(
        arm,
        tp=2,
        fp=2,
        fn=6,
        usage=_usage(tokens, "0.01"),
        provenance={
            "prompt_path": f"review/prompts/review.system.{version}.md",
            "prompt_sha": version * 32,
            "prompt_version": version,
            "static_digest": f"sha256-v1:{version}",
            **(provenance or {}),
        },
        run_metadata={"static_digest_without_prompt": REST_OF_THE_INPUTS, **(run_metadata or {})},
    )


def test_the_command_prints_a_prompt_ab_with_the_spread_of_tokens_and_their_delta(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    arguments = ["--markdown", "--baseline", "v3"]
    for number, (label, tokens) in enumerate(
        [("v3", 1000), ("v3", 3000), ("v5", 2500), ("v5", 3500)]
    ):
        path = _write(tmp_path, f"run-{number}", _prompt_report(label, tokens=tokens))
        arguments += ["--run", f"{label}={path}"]

    assert main(arguments) == 0

    table = capsys.readouterr().out
    assert "2000 (1000–3000)" in table
    assert "3000 (2500–3500)" in table
    assert "| v3 | +0.0 pp |" in table
    assert "| +1000 | +0 | +0.000000 |" in table
    assert "- Arm `v3`: prompt v3, `review/prompts/review.system.v3.md`, SHA-256 " in table
    assert f"static inputs without the prompt `{REST_OF_THE_INPUTS}`" in table


def test_two_files_of_one_version_compare_by_their_bytes(tmp_path: Path) -> None:
    result = aggregate(
        [
            ("draft-a", _write(tmp_path, "a", _prompt_report("v6"))),
            (
                "draft-b",
                _write(
                    tmp_path,
                    "b",
                    _prompt_report(
                        "v6", provenance={"prompt_sha": "e" * 64, "static_digest": "other"}
                    ),
                ),
            ),
        ]
    )

    assert [arm["prompt_version"] for arm in result["arms"]] == ["v6", "v6"]
    assert [arm["prompt_sha"] for arm in result["arms"]] == ["v6" * 32, "e" * 64]


def test_one_prompt_keeps_its_output_when_the_reports_carry_the_new_digest(tmp_path: Path) -> None:
    """Policy arms of one prompt: they may differ in the arm, and one prompt describes them."""
    result = aggregate(
        [
            ("none", _write(tmp_path, "none", _prompt_report("v5", arm="none"))),
            ("valid", _write(tmp_path, "valid", _prompt_report("v5", arm="valid"))),
        ]
    )

    assert result["provenance"]["prompt_version"] == "v5"
    assert result["provenance"]["static_digest"] == "sha256-v1:v5"
    assert "static_digest_without_prompt" not in result["provenance"]
    assert "prompt v5, static `sha256-v1:v5`" in format_markdown(result)


@pytest.mark.parametrize(
    ("field", "provenance"),
    [
        ("prompt_version", {"prompt_version": "v5", "static_digest": "sha256-v1:v5"}),
        # One version, other bytes: only the static digest tells them apart.
        ("static_digest", {"static_digest": "sha256-v1:edited", "prompt_sha": "e" * 64}),
    ],
)
def test_an_arm_that_mixes_prompts_is_refused(
    tmp_path: Path, field: str, provenance: dict[str, str]
) -> None:
    first = _write(tmp_path, "first", _prompt_report("v3"))
    mixed = _write(tmp_path, "mixed", _prompt_report("v3", provenance=provenance))
    other = _write(tmp_path, "other", _prompt_report("v5"))

    with pytest.raises(AbError) as raised:
        aggregate([("v3", first), ("v3", mixed), ("v5", other)])

    assert str(raised.value) == f"{mixed}: {field} differs from {first}"


@pytest.mark.parametrize("positions", [(0,), (1,), (0, 1)])
@pytest.mark.parametrize(
    "broken",
    [
        lambda report: report["provenance"]["run_metadata"].pop("static_digest_without_prompt"),
        lambda report: report["provenance"]["run_metadata"].update(
            static_digest_without_prompt=None
        ),
        lambda report: report["provenance"]["run_metadata"].update(
            static_digest_without_prompt="sha256-v1:short"
        ),
        lambda report: report["provenance"]["run_metadata"].pop("policy_arm"),
        lambda report: report["provenance"]["run_metadata"].update(policy_arm="unknown"),
        lambda report: report["provenance"].pop("prompt_path"),
        lambda report: report["provenance"].pop("prompt_sha"),
    ],
)
def test_a_prompt_ab_without_the_proof_is_refused(
    tmp_path: Path, broken: Callable[[dict[str, Any]], object], positions: tuple[int, ...]
) -> None:
    reports = [_prompt_report("v3"), _prompt_report("v5")]
    for position in positions:
        broken(reports[position])
    runs = [
        ("v3", _write(tmp_path, "v3", reports[0])),
        ("v5", _write(tmp_path, "v5", reports[1])),
    ]

    with pytest.raises(AbError) as raised:
        aggregate(runs)

    assert str(raised.value) == (
        f"{runs[positions[0]][1]}: the arms differ in prompt_version or static_digest, and this "
        "report lacks a valid prompt_path, prompt_sha, run_metadata.policy_arm or "
        "run_metadata.static_digest_without_prompt to prove that only the prompt differs; "
        "capture both arms again"
    )


def test_a_prompt_ab_with_another_digest_of_the_rest_is_refused(tmp_path: Path) -> None:
    first = _write(tmp_path, "v3", _prompt_report("v3"))
    other = _write(
        tmp_path,
        "v5",
        _prompt_report(
            "v5", run_metadata={"static_digest_without_prompt": "sha256-v1:" + "f" * 64}
        ),
    )

    with pytest.raises(AbError) as raised:
        aggregate([("v3", first), ("v5", other)])

    assert str(raised.value) == (
        f"{other}: static inputs other than the prompt differ from {first}; "
        "capture both arms from one ref"
    )


def test_a_prompt_ab_across_policy_arms_is_refused(tmp_path: Path) -> None:
    first = _write(tmp_path, "v3", _prompt_report("v3", arm="legacy"))
    other = _write(tmp_path, "v5", _prompt_report("v5", arm="none"))

    with pytest.raises(AbError) as raised:
        aggregate([("v3", first), ("v5", other)])

    assert str(raised.value) == f"{other}: run_metadata.policy_arm differs from {first}"


@pytest.mark.parametrize(
    ("field", "provenance", "run_metadata"),
    [
        ("model_id", {"model_id": "model-b"}, {}),
        ("corpus_digest", {"corpus_digest": "other"}, {}),
        ("run_metadata.engine", {}, {"engine": "deep"}),
        ("run_metadata.effective_settings", {}, {"effective_settings": {"temperature": 1}}),
        ("run_metadata.fallback_model_id", {}, {"fallback_model_id": "model-c"}),
    ],
)
def test_a_prompt_ab_keeps_the_other_inputs_of_the_experiment_equal(
    tmp_path: Path, field: str, provenance: dict[str, str], run_metadata: dict[str, Any]
) -> None:
    first = _write(tmp_path, "v3", _prompt_report("v3"))
    other = _write(
        tmp_path, "v5", _prompt_report("v5", provenance=provenance, run_metadata=run_metadata)
    )

    with pytest.raises(AbError) as raised:
        aggregate([("v3", first), ("v5", other)])

    assert str(raised.value) == f"{other}: {field} differs from {first}"


@pytest.mark.parametrize("one_byte_changed", [False, True])
def test_a_capture_with_another_static_input_changed_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prompt_root: Path,
    prompt_reports: dict[str, dict[str, Any]],
    one_byte_changed: bool,
) -> None:
    """The same second arm from a copy of the repository: the copy passes, one byte does not."""
    repo = tmp_path / "repo"
    for relative in [
        *prompt_reports["v5"]["provenance"]["static_inputs"],
        "review/eval/pr-commit-messages.json",
    ]:
        destination = repo / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / relative, destination)
    if one_byte_changed:
        changed = repo / "app/bootstrap/llm_gateway.py"
        changed.write_bytes(changed.read_bytes() + b"\n")
    monkeypatch.setattr(eval_live, "REPO_ROOT", repo)
    second = _capture(
        prompt_root,
        tmp_path / "second",
        repo / SYSTEM_V5.relative_to(REPO_ROOT),
        repo=repo,
    )
    runs = [
        ("v3", _write(tmp_path, "v3", prompt_reports["v3"])),
        ("v5", _write(tmp_path, "v5", second)),
    ]

    if not one_byte_changed:
        assert [arm["label"] for arm in aggregate(runs)["arms"]] == ["v3", "v5"]
        return
    with pytest.raises(AbError) as raised:
        aggregate(runs)
    assert str(raised.value) == (
        f"{runs[1][1]}: static inputs other than the prompt differ from {runs[0][1]}; "
        "capture both arms from one ref"
    )
