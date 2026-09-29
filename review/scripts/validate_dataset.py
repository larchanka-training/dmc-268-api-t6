#!/usr/bin/env python3
"""Validate issue #30 case inputs without model access.

Usage: uv run python review/scripts/validate_dataset.py --case SEC-01
       uv run python review/scripts/validate_dataset.py --final
       uv run python review/scripts/validate_dataset.py --final --report-json /tmp/report.json

The optional --root is for isolated fixtures. Normal runs use test-prs-dataset/.
"""

from __future__ import annotations

import argparse
import codecs
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, TypedDict

from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ROOT = REPO_ROOT / "test-prs-dataset"
SCHEMA_PATH = DEFAULT_ROOT / "schema" / "case.schema.json"
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
CLASS_PREFIX = {
    "security": "SEC",
    "resource": "RES",
    "logic": "LOG",
    "syntax": "SYN",
    "clean": "CLEAN",
}
DEFAULT_CATEGORY = {
    "security": "security",
    "resource": "performance",
    "logic": "correctness",
    "syntax": "readability",
}


class DistributionCounts(TypedDict):
    valid_case_count: int
    class_counts: dict[str, int]
    real_case_count: int
    real_class_counts: dict[str, int]
    distinct_real_source_count: int
    language_counts: dict[str, int]
    critical_truth_count: int


def _case_path(case_dir: Path, raw: str) -> Path | None:
    """Resolve a case-local path without allowing absolute paths or escapes."""
    relative = Path(raw)
    if relative.is_absolute() or ".." in relative.parts or raw == ".":
        return None
    result = (case_dir / relative).resolve()
    return result if result.is_relative_to(case_dir.resolve()) else None


def _patch_added_lines(patch: Path) -> dict[str, set[int]]:
    """Read new-side added line numbers from unified diff hunks."""
    added: dict[str, set[int]] = {}
    current_path: str | None = None
    new_line: int | None = None
    for text in patch.read_text(encoding="utf-8").splitlines():
        if text.startswith("diff --git "):
            current_path = None
            new_line = None
        elif text.startswith("+++ ") and new_line is None:
            raw_path = text[4:].split("\t", 1)[0]
            if raw_path.startswith('"'):
                quoted = re.match(r'^"(?:\\.|[^"\\])*"', raw_path)
                escaped = quoted.group(0)[1:-1] if quoted else ""
                path = codecs.escape_decode(escaped.encode("utf-8"))[0].decode("utf-8")
            else:
                path = raw_path
            current_path = path.removeprefix("b/") if path != "/dev/null" else None
        elif match := HUNK.match(text):
            new_line = int(match.group(1))
        elif new_line is not None and current_path is not None:
            if text.startswith("+"):
                added.setdefault(current_path, set()).add(new_line)
                new_line += 1
            elif text.startswith(" "):
                new_line += 1
            elif text.startswith("-") or text.startswith("\\"):
                continue
    return added


def _derived_verdict(findings: list[dict[str, Any]]) -> str:
    severities = {item["severity"] for item in findings}
    if severities & {"critical", "high"}:
        return "blocking"
    if severities & {"medium", "low"}:
        return "attention"
    return "clean"


def _schema_errors(data: object, validator: Draft202012Validator) -> list[str]:
    errors = []
    for error in validator.iter_errors(data):
        location = ".".join(str(part) for part in error.absolute_path) or "$"
        errors.append(f"case.json.{location}: {error.message}")
    return sorted(errors)


def validate_case(
    case_dir: Path, validator: Draft202012Validator
) -> tuple[list[str], dict[str, Any] | None]:
    """Return diagnostics and a validated case record for one directory."""
    errors: list[str] = []
    cases_root = case_dir.parent.resolve()
    if case_dir.is_symlink() or not case_dir.resolve().is_relative_to(cases_root):
        return ["case directory: symlink or path outside cases root is forbidden"], None
    metadata_path = case_dir / "case.json"
    if metadata_path.is_symlink() or not metadata_path.resolve().is_relative_to(case_dir.resolve()):
        return ["case.json: symlink or path outside case directory is forbidden"], None
    try:
        data: object = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return [f"case.json: {exc}"], None
    errors.extend(_schema_errors(data, validator))
    if errors or not isinstance(data, dict):
        return errors, None

    record: dict[str, Any] = data
    if record["id"] != case_dir.name:
        errors.append(f"case.json.id: {record['id']} does not match directory {case_dir.name}")
    if not record["id"].startswith(CLASS_PREFIX[record["class"]] + "-"):
        errors.append("case.json.id: prefix does not match class")

    seen_anchors: set[tuple[str, str, int]] = set()
    for index, finding in enumerate(record["expected_findings"]):
        start = finding["start_line"]
        line = finding["line"]
        if isinstance(line, float) or isinstance(start, float):
            errors.append(f"expected_findings[{index}]: line numbers must be integers")
        elif start is not None and start >= line:
            errors.append(f"expected_findings[{index}].start_line: must be < line")
        if (
            record["class"] != "clean"
            and finding["category"] != DEFAULT_CATEGORY[record["class"]]
            and not record.get("category_reason")
        ):
            errors.append(f"expected_findings[{index}].category: category_reason is required")
        key = (finding["path"], finding["category"], line)
        if key in seen_anchors:
            errors.append(f"expected_findings[{index}]: duplicate truth anchor {key}")
        seen_anchors.add(key)
    if errors:
        return errors, None

    expected = _derived_verdict(record["expected_findings"])
    if record["expected_verdict"] != expected:
        errors.append(f"expected_verdict: {record['expected_verdict']} must be {expected}")

    base = _case_path(case_dir, record["base_path"])
    patch = _case_path(case_dir, record["patch_path"])
    if base is None or not base.is_dir() or not any(path.is_file() for path in base.rglob("*")):
        errors.append("base_path: base pre-image directory is missing or empty")
    elif any(path.is_symlink() for path in base.rglob("*")):
        errors.append("base_path: symlinks are not allowed in pre-images")
    if patch is None or not patch.is_file():
        errors.append("patch_path: patch file is missing or escapes case directory")
    apply_directory = record.get("apply_directory")
    if apply_directory is not None and (
        base is None
        or (apply_root := _case_path(base, apply_directory)) is None
        or not apply_root.is_dir()
    ):
        errors.append("apply_directory: must be an existing directory inside base_path")
    if errors:
        return errors, None
    assert base is not None and patch is not None

    with tempfile.TemporaryDirectory(prefix="dataset-case-") as tmp:
        workdir = Path(tmp) / "worktree"
        shutil.copytree(base, workdir)
        command = ["git", "apply", "--check"]
        if apply_directory is not None:
            command.append(f"--directory={apply_directory}")
        applied = subprocess.run(
            [*command, str(patch)],
            cwd=workdir,
            capture_output=True,
            text=True,
            check=False,
        )
    if applied.returncode != 0:
        detail = applied.stderr.strip() or applied.stdout.strip() or "git apply --check failed"
        return [f"patch_path: {detail}"], None

    try:
        added = _patch_added_lines(patch)
    except (OSError, UnicodeError, ValueError, SyntaxError) as exc:
        return [f"patch_path: cannot read unified patch: {exc}"], None
    if record["expected_findings"] and not added:
        errors.append("patch_path: no new-side added lines found")
    for index, finding in enumerate(record["expected_findings"]):
        path = finding["path"]
        anchors = [finding["line"]]
        if finding["start_line"] is not None:
            anchors.append(finding["start_line"])
        for anchor in anchors:
            if anchor not in added.get(path, set()):
                errors.append(
                    f"expected_findings[{index}]: {path}:{anchor} is not a new-side added line"
                )
    return errors, record if not errors else None


def _distribution_counts(records: list[dict[str, Any]]) -> DistributionCounts:
    """Count only validated records for the final corpus report."""
    classes = Counter(record["class"] for record in records)
    real_records = [record for record in records if record["source"]["kind"] == "real"]
    real_classes = Counter(record["class"] for record in real_records)
    languages = Counter(record["language"] for record in records)
    critical = len(
        {
            (record["id"], finding["path"], finding["category"], finding["line"])
            for record in records
            for finding in record["expected_findings"]
            if finding["severity"] == "critical"
        }
    )
    return {
        "valid_case_count": len(records),
        "class_counts": {name: classes[name] for name in CLASS_PREFIX},
        "real_case_count": len(real_records),
        "real_class_counts": {name: real_classes[name] for name in CLASS_PREFIX},
        "distinct_real_source_count": len({record["source"]["url"] for record in real_records}),
        "language_counts": {name: languages[name] for name in ("python", "typescript", "tsx")},
        "critical_truth_count": critical,
    }


def _distribution_errors(counts: DistributionCounts) -> list[str]:
    """Check the final corpus separately from incremental single-case work."""
    errors: list[str] = []
    valid = counts["valid_case_count"]
    if not 20 <= valid <= 30:
        errors.append(f"final corpus: expected 20–30 cases, got {valid}")
    for name in CLASS_PREFIX:
        class_count = counts["class_counts"][name]
        if class_count < 4:
            errors.append(f"final corpus: class {name} has {class_count} cases, needs ≥4")
        if counts["real_class_counts"][name] < 1:
            errors.append(f"final corpus: class {name} needs ≥1 licensed real case")
    if counts["distinct_real_source_count"] < 5:
        errors.append("final corpus: needs ≥5 distinct licensed real cases")
    languages = counts["language_counts"]
    if languages["python"] == 0 or (languages["typescript"] == 0 and languages["tsx"] == 0):
        errors.append("final corpus: needs Python and TypeScript/React cases")
    critical = counts["critical_truth_count"]
    if critical < 5:
        errors.append(f"final corpus: needs ≥5 critical findings, got {critical}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="dataset root")
    parser.add_argument("--case", help="validate one case ID")
    parser.add_argument("--final", action="store_true", help="also check corpus distribution")
    parser.add_argument(
        "--report-json", type=Path, metavar="PATH", help="write final counts as JSON"
    )
    args = parser.parse_args()
    if args.case and args.final:
        parser.error("--case and --final cannot be combined")
    if args.report_json and not args.final:
        parser.error("--report-json requires --final")
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    cases_root = args.root / "cases"
    if cases_root.is_symlink():
        print("dataset: cases directory cannot be a symlink")
        return 1
    if args.case:
        if not re.fullmatch(r"[A-Z]+-[0-9]{2}", args.case):
            parser.error("--case must be a case ID such as SEC-01")
        case_dirs = [cases_root / args.case]
    else:
        case_dirs = sorted(path for path in cases_root.glob("*") if path.is_dir())
    if not case_dirs:
        print("dataset: no cases found")
        return 1

    records: list[dict[str, Any]] = []
    failed = False
    diagnostics: list[str] = []
    for case_dir in case_dirs:
        errors, record = validate_case(case_dir, validator)
        if errors:
            failed = True
            for error in errors:
                diagnostic = f"{case_dir.name}: {error}"
                print(diagnostic)
                diagnostics.append(diagnostic)
        else:
            assert record is not None
            records.append(record)
            print(f"OK {case_dir.name}")
    if args.final:
        print(f"cases: {len(case_dirs)}; valid: {len(records)}")
        counts = _distribution_counts(records)
        for error in _distribution_errors(counts):
            print(error)
            diagnostics.append(error)
            failed = True
        if args.report_json:
            report = {
                "schema_version": 1,
                "case_count": len(case_dirs),
                **counts,
                "passed": not failed,
                "errors": diagnostics,
            }
            try:
                args.report_json.parent.mkdir(parents=True, exist_ok=True)
                args.report_json.write_text(
                    json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
            except OSError as exc:
                print(f"report JSON: {exc}", file=sys.stderr)
                return 1
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
