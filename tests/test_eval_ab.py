"""A/B aggregation of repeated live-eval reports per arm (#163, AC8)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from review.scripts.eval_ab import AbError, aggregate, format_markdown, main
from review.scripts.eval_replay import replay
from tests.test_eval_live_rag import rag_root, record_rag, valid_answers, window_without_rag

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
