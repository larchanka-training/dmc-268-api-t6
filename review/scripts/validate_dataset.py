#!/usr/bin/env python3
"""Validate issue #30 case inputs without model access.

Usage: uv run python review/scripts/validate_dataset.py --case SEC-01
       uv run python review/scripts/validate_dataset.py --final
       uv run python review/scripts/validate_dataset.py --final --report-json /tmp/report.json
       uv run python review/scripts/validate_dataset.py --root test-prs-dataset/rules \
           --final --profile rules

The optional --root is for isolated fixtures. Normal runs use test-prs-dataset/.
--profile rules checks the rules.md sub-corpus (#163) against its own distribution;
per-case checks are the same in both profiles.
"""

from __future__ import annotations

import argparse
import codecs
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, TypedDict

from jsonschema import Draft202012Validator

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.modules.reviews.application.policy_documents import (  # noqa: E402
    normalize_document,
)
from app.modules.reviews.application.policy_errors import PolicyError  # noqa: E402
from app.modules.reviews.application.policy_snapshot import (  # noqa: E402
    AGENTS_DOCUMENT_PATH,
    RULES_DOCUMENT_PATH,
)
from app.modules.reviews.application.review_ignore import parse_review_ignore  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ROOT = REPO_ROOT / "test-prs-dataset"
SCHEMA_PATH = DEFAULT_ROOT / "schema" / "case.schema.json"
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
# The main corpus classes: the final distribution of the main profile counts exactly these.
CLASS_PREFIX = {
    "security": "SEC",
    "resource": "RES",
    "logic": "LOG",
    "syntax": "SYN",
    "clean": "CLEAN",
}
RULES_CLASS = "rules"
ID_PREFIX = {**CLASS_PREFIX, RULES_CLASS: "RUL"}
POLICY_ARMS = ("none", "valid", "malicious")
# The line window of eval_score.py: a prediction within this many lines matches an anchor.
MATCH_WINDOW = 2
_HEADING = re.compile(r"^#{1,6}\s")
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


class RulesDistributionCounts(TypedDict):
    valid_case_count: int
    class_counts: dict[str, int]
    language_counts: dict[str, int]
    rule_truth_case_count: int
    exception_case_count: int


def _case_path(case_dir: Path, raw: str) -> Path | None:
    """Resolve a case-local path without allowing absolute paths or escapes."""
    relative = Path(raw)
    if relative.is_absolute() or ".." in relative.parts or raw == ".":
        return None
    result = (case_dir / relative).resolve()
    return result if result.is_relative_to(case_dir.resolve()) else None


def _git_env() -> dict[str, str]:
    """The environment without any ``GIT_*`` key, for git runs on the copied base.

    An exported ``GIT_DIR`` (``git rebase --exec``) would make git read the caller's repository
    and its config instead of the copy.
    """
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


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


def uses_policy_arms(record: dict[str, Any]) -> bool:
    """Whether a case carries any field of the rules.md sub-corpus (#163)."""
    return (
        record["class"] == RULES_CLASS
        or "policy" in record
        or "must_not_flag" in record
        or any("arms" in item or "origin" in item for item in record["expected_findings"])
    )


def _normalized(text: str) -> str:
    """Collapse whitespace, so a quote may wrap differently than the rules document."""
    return " ".join(text.split())


def _sections(text: str) -> list[str]:
    """Markdown sections of a rules document: each heading line with the text up to the next."""
    sections: list[list[str]] = [[]]
    for line in text.split("\n"):
        if _HEADING.match(line) and any(part.strip() for part in sections[-1]):
            sections.append([])
        sections[-1].append(line)
    return [body for lines in sections if (body := "\n".join(lines).strip())]


def _malicious_errors(arms: dict[str, str], valid: str, malicious: str) -> list[str]:
    """The malicious arm is the benign rules plus an injection, so each valid section stays."""
    if malicious == valid:
        return ["policy.arms.malicious: must differ from the valid rules document"]
    errors: list[str] = []
    for section in _sections(valid):
        if section not in malicious:
            heading = section.split("\n", 1)[0]
            errors.append(
                f"policy.arms.malicious: {arms['malicious']} lacks the section {heading!r} "
                f"of {arms['valid']}"
            )
    return errors


def _policy_document_errors(base: Path, record: dict[str, Any]) -> list[str]:
    """Check every arm file with the production parsers, so a broken fixture fails here.

    ``rule_ref`` and each exception ``reason`` quote the rules document verbatim (whitespace
    may wrap differently); the quote must occur in both arm files.
    """
    policy = record.get("policy")
    if policy is None:
        return (
            ["policy: must_not_flag, arms and origin need the case policy arms"]
            if uses_policy_arms(record)
            else []
        )
    errors: list[str] = []
    arms = policy["arms"]
    if arms["valid"] != RULES_DOCUMENT_PATH:
        errors.append(f"policy.arms.valid: must be {RULES_DOCUMENT_PATH}")
    if arms["malicious"] == arms["valid"]:
        errors.append("policy.arms.malicious: must name another file than policy.arms.valid")
    texts: dict[str, str] = {}
    documents = [(f"policy.arms.{arm}", arms[arm], RULES_DOCUMENT_PATH) for arm in arms]
    if (base / AGENTS_DOCUMENT_PATH).is_file():
        documents.append((AGENTS_DOCUMENT_PATH, AGENTS_DOCUMENT_PATH, AGENTS_DOCUMENT_PATH))
    for label, raw, snapshot_path in documents:
        path = _case_path(base, raw)
        if path is None or not path.is_file():
            errors.append(f"{label}: {raw} is missing or escapes base_path")
            continue
        try:
            document = normalize_document(snapshot_path, path.read_bytes(), blob_oid="0" * 40)
            if snapshot_path == RULES_DOCUMENT_PATH:
                parse_review_ignore(document.text)
        except (OSError, PolicyError) as exc:
            errors.append(f"{label}: {raw}: {exc}")
            continue
        texts[label] = document.text or ""
    valid_text = texts.get("policy.arms.valid")
    malicious_text = texts.get("policy.arms.malicious")
    if arms["malicious"] != arms["valid"] and valid_text is not None and malicious_text is not None:
        errors.extend(_malicious_errors(arms, valid_text, malicious_text))
    quotes = [
        (f"expected_findings[{index}].rule_ref", finding["rule_ref"])
        for index, finding in enumerate(record["expected_findings"])
        if "rule_ref" in finding
    ]
    quotes.extend(
        (f"must_not_flag[{index}].reason", anchor["reason"])
        for index, anchor in enumerate(record.get("must_not_flag", []))
    )
    for label, quote in quotes:
        for arm in ("valid", "malicious"):
            text = texts.get(f"policy.arms.{arm}")
            if text is not None and _normalized(quote) not in _normalized(text):
                errors.append(f"{label}: not found in {arms[arm]}")
    return errors


def _arms(item: dict[str, Any]) -> set[str]:
    return set(item.get("arms", POLICY_ARMS))


def _exception_errors(record: dict[str, Any], added: dict[str, set[int]]) -> list[str]:
    """``must_not_flag`` anchors follow the truth anchor rules and stay clear of every truth.

    Scoring matches an exception anchor on path and line window only, whatever the category,
    so an anchor within the window of a truth that shares an arm would be ambiguous.
    """
    errors: list[str] = []
    seen: set[tuple[str, int]] = set()
    for index, anchor in enumerate(record.get("must_not_flag", [])):
        label = f"must_not_flag[{index}]"
        start = anchor["start_line"]
        line = anchor["line"]
        if isinstance(line, float) or isinstance(start, float):
            errors.append(f"{label}: line numbers must be integers")
            continue
        if start is not None and start >= line:
            errors.append(f"{label}.start_line: must be < line")
        key = (anchor["path"], line)
        if key in seen:
            errors.append(f"{label}: duplicate exception anchor {key}")
        seen.add(key)
        for point in (line, start):
            if point is not None and point not in added.get(anchor["path"], set()):
                errors.append(f"{label}: {anchor['path']}:{point} is not a new-side added line")
        for truth_index, truth in enumerate(record["expected_findings"]):
            shared = [arm for arm in POLICY_ARMS if arm in _arms(truth) & _arms(anchor)]
            if (
                shared
                and truth["path"] == anchor["path"]
                and abs(truth["line"] - line) <= MATCH_WINDOW
            ):
                errors.append(
                    f"{label}: within {MATCH_WINDOW} lines of expected_findings[{truth_index}] "
                    f"in arm {shared[0]}"
                )
    return errors


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
    if not record["id"].startswith(ID_PREFIX[record["class"]] + "-"):
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
            record["class"] in DEFAULT_CATEGORY
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
            env=_git_env(),
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
    if (
        record["class"] == RULES_CLASS
        and not record.get("must_not_flag")
        and not any(item.get("origin") == "rule" for item in record["expected_findings"])
    ):
        errors.append("rules case: needs an origin rule truth or a must_not_flag anchor")
    errors.extend(_exception_errors(record, added))
    errors.extend(_policy_document_errors(base, record))
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


def _rules_profile_errors(records: list[dict[str, Any]]) -> list[str]:
    """The main corpus and its paid baseline stay free of rules.md cases (#163)."""
    return [
        f"final corpus: {record['id']} uses policy arms; validate it with --profile rules"
        for record in records
        if uses_policy_arms(record)
    ]


def _rules_distribution_counts(records: list[dict[str, Any]]) -> RulesDistributionCounts:
    classes = Counter(record["class"] for record in records)
    languages = Counter(record["language"] for record in records)
    return {
        "valid_case_count": len(records),
        "class_counts": dict(sorted(classes.items())),
        "language_counts": {name: languages[name] for name in ("python", "typescript", "tsx")},
        "rule_truth_case_count": sum(
            any(item.get("origin") == "rule" for item in record["expected_findings"])
            for record in records
        ),
        "exception_case_count": sum("must_not_flag" in record for record in records),
    }


def _rules_distribution_errors(counts: RulesDistributionCounts) -> list[str]:
    """Minimums of the rules.md sub-corpus; synthetic sources are allowed."""
    errors: list[str] = []
    valid = counts["valid_case_count"]
    if valid < 8:
        errors.append(f"rules corpus: expected ≥8 cases, got {valid}")
    other = sorted(name for name in counts["class_counts"] if name != RULES_CLASS)
    if other:
        errors.append(f"rules corpus: every case must be class rules, got {', '.join(other)}")
    rule_cases = counts["rule_truth_case_count"]
    if rule_cases < 3:
        errors.append(f"rules corpus: needs ≥3 cases with a rule-made truth, got {rule_cases}")
    exception_cases = counts["exception_case_count"]
    if exception_cases < 3:
        errors.append(f"rules corpus: needs ≥3 cases with must_not_flag, got {exception_cases}")
    languages = counts["language_counts"]
    if languages["python"] == 0 or (languages["typescript"] == 0 and languages["tsx"] == 0):
        errors.append("rules corpus: needs Python and TypeScript/React cases")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="dataset root")
    parser.add_argument("--case", help="validate one case ID")
    parser.add_argument("--final", action="store_true", help="also check corpus distribution")
    parser.add_argument(
        "--report-json", type=Path, metavar="PATH", help="write final counts as JSON"
    )
    parser.add_argument(
        "--profile", choices=(RULES_CLASS,), help="final distribution of a sub-corpus"
    )
    args = parser.parse_args()
    if args.case and args.final:
        parser.error("--case and --final cannot be combined")
    if args.report_json and not args.final:
        parser.error("--report-json requires --final")
    if args.profile and not args.final:
        parser.error("--profile requires --final")
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
        counts: DistributionCounts | RulesDistributionCounts
        if args.profile == RULES_CLASS:
            counts = _rules_distribution_counts(records)
            distribution_errors = _rules_distribution_errors(counts)
            profile: dict[str, str] = {"profile": RULES_CLASS}
        else:
            counts = _distribution_counts(records)
            distribution_errors = [*_distribution_errors(counts), *_rules_profile_errors(records)]
            profile = {}
        for error in distribution_errors:
            print(error)
            diagnostics.append(error)
            failed = True
        if args.report_json:
            report = {
                "schema_version": 1,
                **profile,
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
