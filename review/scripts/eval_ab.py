#!/usr/bin/env python3
"""Aggregate repeated live-eval reports into an A/B table per arm (#163, AC8).

Usage: uv run python review/scripts/eval_ab.py --run none=r1.json --run none=r2.json \\
           --run valid=r3.json --run malicious=r4.json [--baseline none] [--markdown]

Each ``--run LABEL=PATH`` names one report JSON written by ``eval_live.py --report-json``.
Labels are free-form arm names, so another experiment can add its own arm; a label that is
a recorded policy arm must match the arm the report was captured with. A capture of the
RAG corpus (#146) is labelled ``rag_off`` (recorded RAG arm ``none``) or ``rag_on``
(``graph``), and those labels belong to no other capture. The RAG captures must also have
been given the same input but the retrieved blocks: per case, one digest of the prompt with
an empty RAG slot in every run of both arms, and no ``rag_on`` case whose retrieved blocks
the gateway fitted away.
All reports must share the model, prompt version, static-input digest and corpus digest,
and the recorded engine, effective settings and fallback model; every capture must be
publishable, and no report content may be given twice. Per arm the output gives each
metric's mean, min and max over the runs, with every run's numerator and denominator, and
the delta of each mean against the baseline arm (default: the first label). An arm whose
runs carry usage only in part gets a warning: its token and cost spread covers those runs
only. Without --markdown the output is JSON.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from review.scripts.eval_replay import RAG_ARM_LABELS, RECORDED_POLICY_ARMS  # noqa: E402

SAME_PROVENANCE = ("model_id", "prompt_version", "static_digest", "corpus_digest")
SAME_RUN_METADATA = ("engine", "effective_settings", "fallback_model_id")
type Ratio = tuple[int, int]
RATIOS: tuple[tuple[str, str, Callable[[dict[str, Any]], Ratio]], ...] = (
    ("precision", "Precision", lambda r: (r["micro"]["tp"], r["micro"]["tp"] + r["micro"]["fp"])),
    ("recall", "Recall", lambda r: (r["micro"]["tp"], r["micro"]["tp"] + r["micro"]["fn"])),
    (
        "critical_recall",
        "Critical Recall",
        lambda r: (r["critical"]["matched"], r["critical"]["total"]),
    ),
    ("validity", "Validity", lambda r: (r["valid_response_count"], r["case_count"])),
    (
        "rule_constraints_found",
        "Rule constraints found",
        lambda r: (r["rule_constraints"]["found"], r["rule_constraints"]["total"]),
    ),
    # Same truths and anchors in every arm (eval_score.py); the exception denominator counts
    # valid responses only, so it is equal across arms only when every response is valid. In
    # arm none the rule figure is "found without rules" and flagged exceptions are "flagged
    # without rules".
    (
        "exceptions_respected",
        "Exceptions respected",
        lambda r: (r["exceptions"]["respected"], r["exceptions"]["total"]),
    ),
    (
        "exceptions_flagged",
        "Exceptions flagged",
        lambda r: (r["exceptions"]["flagged"], r["exceptions"]["total"]),
    ),
)
QUANTITIES = (("tokens_in", "Tokens in"), ("tokens_out", "Tokens out"), ("cost_usd", "Cost USD"))


class AbError(Exception):
    """The reports cannot be compared as one experiment."""


def _load(label: str, path: Path) -> tuple[dict[str, Any], str]:
    """Return the report and the digest of its bytes; refuse anything but a complete report."""
    try:
        raw = path.read_bytes()
        report: dict[str, Any] = json.loads(raw.decode("utf-8"))
        provenance = report["provenance"]
        metadata = provenance["run_metadata"]
        if not isinstance(provenance, dict) or not isinstance(metadata, dict):
            raise TypeError("provenance and run_metadata must be objects")
        missing = [name for name in SAME_PROVENANCE if name not in provenance]
        missing += [f"run_metadata.{name}" for name in SAME_RUN_METADATA if name not in metadata]
        if missing:
            raise KeyError(", ".join(missing))
        for name, _, ratio in RATIOS:
            numerator, denominator = ratio(report)
            if not _is_count(numerator) or not _is_count(denominator):
                raise ValueError(f"{name} needs non-negative integer counts")
        usage_error = _usage_error(report.get("usage"))
        if usage_error is not None:
            raise ValueError(usage_error)
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise AbError(
            f"{path}: not an eval report with provenance and policy-arm metrics ({exc})"
        ) from None
    recorded = metadata.get("policy_arm", "legacy")
    publishable = metadata.get("baseline_publishable")
    if label in RECORDED_POLICY_ARMS and recorded != label:
        raise AbError(f"{path}: labelled {label} but captured with policy arm {recorded}")
    if publishable is not True:
        raise AbError(f"{path}: capture is not publishable; rerun the arm")
    # After the publishable check: a case that never reached the provider has no evidence.
    _check_rag_label(label, path, metadata)
    return report, hashlib.sha256(raw).hexdigest()


def _rag_error(metadata: dict[str, Any], recorded: str) -> str | None:
    """Why a RAG capture's evidence cannot be compared, or ``None``; the shape ``eval_replay``
    already validated, but a report is only a file. ``trimmed`` and the arm's prompt are
    derived fields, so they are checked against what they derive from."""
    evidence = metadata.get("rag_evidence")
    if not isinstance(evidence, dict):
        return "run_metadata.rag_evidence must be an object"
    for case_id, item in evidence.items():
        if item is None:
            return f"case {case_id} has no recorded evidence: no request was sent"
        if not (
            isinstance(item, dict)
            and isinstance(item.get("prompt_digest"), str)
            and isinstance(item.get("non_rag_prompt_digest"), str)
            and isinstance(item.get("retrieved"), list)
            and all(isinstance(block, dict) for block in item["retrieved"])
            and isinstance(item.get("offline_retrieved_ids"), list)
            and type(item.get("trimmed")) is bool
        ):
            return f"case {case_id} has malformed evidence"
        sent = [block.get("block_id") for block in item["retrieved"]]
        if item["trimmed"] != (sent != item["offline_retrieved_ids"]):
            return f"the trimmed flag of case {case_id} contradicts its retrieved blocks"
        if recorded == "none" and item["prompt_digest"] != item["non_rag_prompt_digest"]:
            return f"the prompt of rag_off case {case_id} is not its non-RAG prompt"
    return None


def _check_rag_label(label: str, path: Path, metadata: dict[str, Any]) -> None:
    """A RAG capture carries its arm's label and no other capture does."""
    recorded = metadata.get("rag_arm")
    if recorded is None:
        if label in RAG_ARM_LABELS.values():
            raise AbError(f"{path}: labelled {label} but captured without a RAG arm")
        return
    if not isinstance(recorded, str) or recorded not in RAG_ARM_LABELS:
        raise AbError(f"{path}: unknown RAG arm {recorded}")
    if label != RAG_ARM_LABELS[recorded]:
        raise AbError(
            f"{path}: captured with RAG arm {recorded}, so it is labelled "
            f"{RAG_ARM_LABELS[recorded]}, not {label}"
        )
    reason = _rag_error(metadata, recorded)
    if reason is None:
        # rag_off sends no retrieved block; rag_on must not have had its blocks fitted away.
        offending = sorted(
            case_id
            for case_id, item in metadata["rag_evidence"].items()
            if (item["retrieved"] if recorded == "none" else item["trimmed"])
        )
        if offending:
            reason = (
                "arm rag_off sent retrieved blocks for "
                if recorded == "none"
                else "the retrieved blocks of the rag_on case were fitted away: "
            ) + ", ".join(offending)
    if reason is not None:
        raise AbError(f"{path}: {reason}")


def _check_rag_inputs(reports: list[tuple[str, Path, dict[str, Any]]]) -> None:
    """Every RAG report covers the same cases, and each case was sent the same prompt but for
    its retrieved blocks: one ``non_rag_prompt_digest`` over all runs of both arms."""
    seen: dict[str, tuple[str, Path]] = {}
    cases: tuple[set[str], Path] | None = None
    for _, path, report in reports:
        metadata = report["provenance"]["run_metadata"]
        if "rag_arm" not in metadata:
            continue
        evidence = metadata["rag_evidence"]
        if cases is None:
            cases = (set(evidence), path)
        elif set(evidence) != cases[0]:
            raise AbError(f"{path}: covers other cases than {cases[1]}")
        for case_id, item in evidence.items():
            digest = item["non_rag_prompt_digest"]
            if case_id in seen and seen[case_id][0] != digest:
                raise AbError(
                    f"{path}: case {case_id} was sent another non-RAG prompt than in "
                    f"{seen[case_id][1]}"
                )
            seen.setdefault(case_id, (digest, path))


def _is_count(value: object) -> bool:
    # CONTRACT: bool is an int subclass; a JSON true/false is not a count.
    return type(value) is int and value >= 0


def _usage_error(usage: object) -> str | None:
    """Usage is absent (``None``) or token counts plus a finite, non-negative USD amount."""
    if usage is None:
        return None
    if not isinstance(usage, dict):
        return "usage must be an object or null"
    for name in ("tokens_in", "tokens_out"):
        if not _is_count(usage.get(name)):
            return f"usage.{name} must be a non-negative integer"
    cost = usage.get("cost_usd")
    try:
        amount = Decimal(cost) if isinstance(cost, str | int) and type(cost) is not bool else None
    except InvalidOperation:
        amount = None
    if amount is None or not amount.is_finite() or amount < 0:
        return "usage.cost_usd must be a non-negative decimal"
    return None


def _quantity(report: dict[str, Any], name: str) -> Decimal | None:
    """A usage total; ``_load`` has checked its type, ``None`` when usage was not recorded."""
    usage = report.get("usage")
    return None if usage is None else Decimal(str(usage[name]))


def _spread(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"mean": None, "min": None, "max": None}
    return {"mean": sum(values) / len(values), "min": min(values), "max": max(values)}


def aggregate(runs: list[tuple[str, Path]], baseline: str | None = None) -> dict[str, Any]:
    """Group reports by label; refuse reports that are not one experiment."""
    if not runs:
        raise AbError("at least one --run LABEL=PATH is required")
    reports: list[tuple[str, Path, dict[str, Any]]] = []
    seen: dict[str, Path] = {}
    for label, path in runs:
        report, digest = _load(label, path)
        # A copied report would count one run twice and shrink the spread.
        if digest in seen:
            raise AbError(f"{path}: same report content as {seen[digest]}")
        seen[digest] = path
        reports.append((label, path, report))
    _check_rag_inputs(reports)
    first = reports[0][2]["provenance"]
    for _, path, report in reports[1:]:
        for name in SAME_PROVENANCE:
            if report["provenance"][name] != first[name]:
                raise AbError(f"{path}: {name} differs from {reports[0][1]}")
        for name in SAME_RUN_METADATA:
            if report["provenance"]["run_metadata"][name] != first["run_metadata"][name]:
                raise AbError(f"{path}: run_metadata.{name} differs from {reports[0][1]}")
    labels = list(dict.fromkeys(label for label, _, _ in reports))
    chosen = baseline if baseline is not None else labels[0]
    if chosen not in labels:
        raise AbError(f"baseline arm {chosen} has no run")
    arms: list[dict[str, Any]] = []
    warnings: list[str] = []
    for label in labels:
        members = [(path, report) for name, path, report in reports if name == label]
        with_usage = sum(isinstance(report.get("usage"), dict) for _, report in members)
        if 0 < with_usage < len(members):
            warnings.append(
                f"arm {label}: only {with_usage} of {len(members)} runs carry usage; "
                "its tokens and cost cover those runs only"
            )
        run_rows: list[dict[str, Any]] = []
        for path, report in members:
            metrics: dict[str, Any] = {}
            for name, _, ratio in RATIOS:
                numerator, denominator = ratio(report)
                metrics[name] = {
                    "numerator": numerator,
                    "denominator": denominator,
                    "value": numerator / denominator if denominator else None,
                }
            for name, _ in QUANTITIES:
                value = _quantity(report, name)
                metrics[name] = {"value": None if value is None else float(value)}
            run_rows.append({"report": str(path), "metrics": metrics})
        summary = {
            name: _spread(
                [
                    row["metrics"][name]["value"]
                    for row in run_rows
                    if row["metrics"][name]["value"] is not None
                ]
            )
            for name in (*(item[0] for item in RATIOS), *(item[0] for item in QUANTITIES))
        }
        recorded = {
            report["provenance"]["run_metadata"].get("policy_arm", "legacy")
            for _, report in members
        }
        arms.append(
            {
                "label": label,
                "policy_arms": sorted(recorded),
                "runs": run_rows,
                "summary": summary,
            }
        )
    base = next(arm for arm in arms if arm["label"] == chosen)["summary"]
    for arm in arms:
        arm["delta"] = {
            name: (
                None
                if spread["mean"] is None or base[name]["mean"] is None
                else spread["mean"] - base[name]["mean"]
            )
            for name, spread in arm["summary"].items()
        }
    return {
        "schema_version": 1,
        "provenance": {
            **{name: first[name] for name in SAME_PROVENANCE},
            "run_metadata": {name: first["run_metadata"][name] for name in SAME_RUN_METADATA},
        },
        "baseline": chosen,
        "arms": arms,
        "warnings": warnings,
    }


def _percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _number(name: str, value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.6f}" if name == "cost_usd" else f"{value:.0f}"


def format_markdown(result: dict[str, Any]) -> str:
    """One row per arm: mean (min–max) and each run's numerator/denominator; then deltas."""
    provenance = result["provenance"]
    lines = [
        f"Model `{provenance['model_id']}`, prompt {provenance['prompt_version']}, "
        f"static `{provenance['static_digest']}`, corpus `{provenance['corpus_digest']}`.",
        "",
        "| Arm | Runs | "
        + " | ".join(title for _, title, _ in RATIOS)
        + " | "
        + " | ".join(title for _, title in QUANTITIES)
        + " |",
        "|" + " --- |" * (2 + len(RATIOS) + len(QUANTITIES)),
    ]
    for arm in result["arms"]:
        cells = [arm["label"], str(len(arm["runs"]))]
        for name, _, _ in RATIOS:
            spread = arm["summary"][name]
            fractions = ", ".join(
                f"{row['metrics'][name]['numerator']}/{row['metrics'][name]['denominator']}"
                for row in arm["runs"]
            )
            cells.append(
                f"{_percent(spread['mean'])} ({_percent(spread['min'])}–"
                f"{_percent(spread['max'])}) [{fractions}]"
            )
        for name, _ in QUANTITIES:
            spread = arm["summary"][name]
            cells.append(
                f"{_number(name, spread['mean'])} ({_number(name, spread['min'])}–"
                f"{_number(name, spread['max'])})"
            )
        lines.append("| " + " | ".join(cells) + " |")
    for arm in result["arms"]:
        if arm["policy_arms"] == ["none"]:
            lines.extend(
                [
                    "",
                    f"Arm `{arm['label']}` (policy arm none) shows no rules: its Rule constraints "
                    "found and Exceptions flagged are found and flagged without rules, and "
                    "Exceptions respected does not apply.",
                ]
            )
    lines.extend(
        [
            "",
            f"Delta of the mean against `{result['baseline']}` (percentage points; "
            "tokens and USD as numbers):",
            "",
            "| Arm | "
            + " | ".join(title for _, title, _ in RATIOS)
            + " | "
            + " | ".join(title for _, title in QUANTITIES)
            + " |",
            "|" + " --- |" * (1 + len(RATIOS) + len(QUANTITIES)),
        ]
    )
    for arm in result["arms"]:
        cells = [arm["label"]]
        for name, _, _ in RATIOS:
            delta = arm["delta"][name]
            cells.append("n/a" if delta is None else f"{delta * 100:+.1f} pp")
        for name, _ in QUANTITIES:
            delta = arm["delta"][name]
            cells.append(
                "n/a"
                if delta is None
                else (f"{delta:+.6f}" if name == "cost_usd" else f"{delta:+.0f}")
            )
        lines.append("| " + " | ".join(cells) + " |")
    if result["warnings"]:
        lines.extend(["", *(f"Warning: {warning}" for warning in result["warnings"])])
    return "\n".join(lines)


def _run_argument(raw: str) -> tuple[str, Path]:
    label, separator, path = raw.partition("=")
    if not separator or not label or not path:
        raise argparse.ArgumentTypeError("expected LABEL=PATH")
    return label, Path(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run", type=_run_argument, action="append", default=[], metavar="LABEL=PATH"
    )
    parser.add_argument("--baseline", help="arm label the deltas are computed against")
    parser.add_argument("--markdown", action="store_true", help="print Markdown tables")
    args = parser.parse_args(argv)
    try:
        result = aggregate(args.run, args.baseline)
    except AbError as exc:
        print(f"eval ab: {exc}", file=sys.stderr)
        return 1
    for warning in result["warnings"]:
        print(f"eval ab: warning: {warning}", file=sys.stderr)
    if args.markdown:
        print(format_markdown(result))
    else:
        print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
