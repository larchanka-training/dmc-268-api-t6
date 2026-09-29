#!/usr/bin/env python3
"""Replay committed raw review responses offline against the gold cases.

Usage: uv run python review/scripts/eval_replay.py [--root test-prs-dataset]
       [--report-json /tmp/eval-report.json]

Manifest format (at <root>/responses/manifest.json)::

    {
      "schema_version": 1,
      "model_id": "recorded-model-id",
      "prompt_path": "review/prompts/review.system.v1.md",
      "prompt_sha": "<64-character SHA-256 hex digest>",
      "prompt_version": "v1",
      "run_metadata": {"recorded_at": "<ISO 8601 timestamp>"},
      "responses": {"SEC-01": "responses/SEC-01.json"}
    }

The responses map must cover every case exactly once. Invalid recorded output
contributes to scoring as invalid rather than being corrected or dropped.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from review.scripts.eval_score import ScoringCase, score_cases  # noqa: E402
from review.scripts.validate_dataset import (  # noqa: E402
    DEFAULT_ROOT,
    SCHEMA_PATH,
    _patch_added_lines,
    validate_case,
)

VALIDATOR_SCRIPT = Path(__file__).with_name("validate_findings.py")
SUCCESS = re.compile(r"OK ReviewOutput \d+ items")
SHA256 = re.compile(r"[0-9a-f]{64}")


class ReplayError(Exception):
    """Input corpus or response manifest cannot be replayed reliably."""


def _manifest(root: Path) -> dict[str, Any]:
    path = root / "responses" / "manifest.json"
    if not path.is_file():
        raise ReplayError(f"response manifest is missing: {path}; no baseline responses recorded")
    if path.is_symlink():
        raise ReplayError(f"response manifest cannot be a symlink: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReplayError(f"response manifest cannot be read: {exc}") from exc
    if not isinstance(data, dict) or set(data) != {
        "schema_version",
        "model_id",
        "prompt_path",
        "prompt_sha",
        "prompt_version",
        "run_metadata",
        "responses",
    }:
        raise ReplayError("response manifest has missing or unexpected fields")
    if data["schema_version"] != 1:
        raise ReplayError("response manifest schema_version must be 1")
    for key in ("model_id", "prompt_path", "prompt_version"):
        if not isinstance(data[key], str) or not data[key].strip():
            raise ReplayError(f"response manifest {key} must be a nonempty string")
    if not isinstance(data["prompt_sha"], str) or not SHA256.fullmatch(data["prompt_sha"]):
        raise ReplayError("response manifest prompt_sha must be a SHA-256 hex digest")
    if not isinstance(data["run_metadata"], dict) or not data["run_metadata"]:
        raise ReplayError("response manifest run_metadata must be a nonempty object")
    if not isinstance(data["responses"], dict):
        raise ReplayError("response manifest responses must be an object")
    return data


def _case_records(root: Path) -> list[tuple[Path, dict[str, Any]]]:
    cases_root = root / "cases"
    if cases_root.is_symlink() or not cases_root.is_dir():
        raise ReplayError(f"cases directory is missing or is a symlink: {cases_root}")
    case_dirs = sorted(path for path in cases_root.iterdir() if path.is_dir())
    if not case_dirs:
        raise ReplayError(f"no cases found in {cases_root}")
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    records = []
    for case_dir in case_dirs:
        errors, record = validate_case(case_dir, validator)
        if errors or record is None:
            raise ReplayError(f"invalid case {case_dir.name}: {'; '.join(errors)}")
        records.append((case_dir, record))
    return records


def _response_path(root: Path, case_id: str, raw: object) -> Path:
    if not isinstance(raw, str) or not raw:
        raise ReplayError(f"response path for {case_id} must be a nonempty string")
    relative = Path(raw)
    responses_root = (root / "responses").resolve()
    if relative.is_absolute() or ".." in relative.parts or raw == ".":
        raise ReplayError(f"response path for {case_id} must be relative to dataset root")
    path = root / relative
    if path.is_symlink() or not path.resolve().is_relative_to(responses_root):
        raise ReplayError(f"response path for {case_id} must stay inside responses/")
    if not path.is_file():
        raise ReplayError(f"response is missing for {case_id}: {path}")
    return path


def _validated_raw(path: Path) -> tuple[object, bool, int, str]:
    result = subprocess.run(
        [sys.executable, str(VALIDATOR_SCRIPT), str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    output = result.stdout.strip() or result.stderr.strip()
    valid = result.returncode == 0 and SUCCESS.fullmatch(result.stdout.strip()) is not None
    if not valid:
        return None, False, result.returncode, output
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, False, 2, f"response changed or cannot be read: {exc}"
    return raw, True, result.returncode, output


def replay(root: Path = DEFAULT_ROOT) -> dict[str, Any]:
    """Validate case inputs and raw responses, then return a deterministic report."""
    records = _case_records(root)
    manifest = _manifest(root)
    entries = manifest["responses"]
    case_ids = {record["id"] for _, record in records}
    missing = sorted(case_ids - entries.keys())
    extra = sorted(entries.keys() - case_ids)
    if missing or extra:
        pieces = []
        if missing:
            pieces.append("missing " + ", ".join(missing))
        if extra:
            pieces.append("unexpected " + ", ".join(extra))
        raise ReplayError("response manifest case IDs: " + "; ".join(pieces))

    cases = []
    statuses = []
    for case_dir, record in records:
        case_id = record["id"]
        response_path = _response_path(root, case_id, entries[case_id])
        raw, valid, exit_code, validator_output = _validated_raw(response_path)
        cases.append(
            ScoringCase(
                case_id=case_id,
                truths=tuple(record["expected_findings"]),
                expected_verdict=record["expected_verdict"],
                added_lines=_patch_added_lines(case_dir / record["patch_path"]),
                raw_response=raw,
                response_valid=valid,
            )
        )
        statuses.append(
            {
                "case_id": case_id,
                "path": entries[case_id],
                "valid": valid,
                "validator_exit_code": exit_code,
                "validator_output": validator_output,
            }
        )
    return {
        "schema_version": 1,
        "provenance": {
            key: manifest[key]
            for key in ("model_id", "prompt_path", "prompt_sha", "prompt_version", "run_metadata")
        },
        **score_cases(cases),
        "responses": statuses,
    }


def _percent(value: float | None) -> str:
    return "undefined" if value is None else f"{value * 100:.1f}%"


def format_console(report: dict[str, Any]) -> str:
    """Human-readable summary using the same counts as the JSON report."""
    provenance = report["provenance"]
    micro = report["micro"]
    critical = report["critical"]
    verdict = report["verdict"]
    lines = [
        f"Cases: {report['case_count']}",
        f"Model: {provenance['model_id']}",
        f"Prompt: {provenance['prompt_path']} (version {provenance['prompt_version']})",
        f"Prompt SHA: {provenance['prompt_sha']}",
        f"Validity: {_percent(report['validity'])} "
        f"({report['valid_response_count']}/{report['case_count']})",
        f"Micro: TP={micro['tp']} FP={micro['fp']} FN={micro['fn']} "
        f"Precision={_percent(micro['precision'])} Recall={_percent(micro['recall'])}",
        f"Critical Recall: {_percent(critical['recall'])} "
        f"({critical['matched']}/{critical['total']})",
        f"Verdict agreement: {_percent(verdict['agreement'])} "
        f"({verdict['agreed']}/{verdict['total']})",
        "Per category:",
    ]
    for name, counts in report["per_category"].items():
        lines.append(
            f"  {name}: TP={counts['tp']} FP={counts['fp']} FN={counts['fn']} "
            f"Precision={_percent(counts['precision'])} Recall={_percent(counts['recall'])}"
        )
    lines.append(f"Severity mismatches: {len(report['severity_mismatches'])}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="dataset root")
    parser.add_argument("--report-json", type=Path, metavar="PATH", help="write JSON report")
    args = parser.parse_args(argv)
    try:
        report = replay(args.root)
        if args.report_json:
            args.report_json.parent.mkdir(parents=True, exist_ok=True)
            args.report_json.write_text(
                json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
    except (ReplayError, OSError) as exc:
        print(f"replay: {exc}", file=sys.stderr)
        return 1
    print(format_console(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
