#!/usr/bin/env python3
"""Replay committed raw review responses offline against the gold cases.

Usage: uv run python review/scripts/eval_replay.py [--root test-prs-dataset]
       [--report-json /tmp/eval-report.json] [--responses-dir DIR]

Manifest format (at <root>/responses/manifest.json, or DIR/manifest.json)::

    {
      "schema_version": 1,
      "model_id": "recorded-model-id",
      "prompt_path": "review/prompts/review.system.v2.md",
      "prompt_sha": "<64-character SHA-256 hex digest>",
      "prompt_version": "v2",
      "static_inputs": ["<sorted relative prompt/rule/rendering paths>"],
      "static_digest": "sha256-v1:<64-character SHA-256 hex digest>",
      "corpus_digest": "sha256-v1:<64-character SHA-256 hex digest>",
      "run_metadata": {
        "recorded_at": "<ISO 8601 timestamp>",
        "baseline_publishable": true,
        "nonpublishable_case_ids": [],
        "cases": {
          "SEC-01": {
            "first_call": "answer",
            "gateway_status": "accepted",
            "paid_metadata_error": false
          }
        },
        "policy_arm": "legacy",
        "policy_arm_digest": null,
        "usage": {
          "SEC-01": [{"operation": "review", "tokens_in": 100, "tokens_out": 30,
                      "cache_read_tokens": 0, "cost_usd": "0.00038"}]
        }
      },
      "responses": {"SEC-01": "responses/SEC-01.json"}
    }

The responses map must cover every case exactly once at responses/<case-id>.json; the
``responses/`` prefix names the responses directory, so a capture moved to another
directory (``--responses-dir``, one per arm and run) replays unchanged. ``policy_arm``,
``policy_arm_digest`` and ``usage`` are optional (absent in captures before #163): a missing
arm is ``legacy`` and missing usage is reported as not recorded.
Invalid recorded output contributes to scoring as invalid rather than being
corrected or dropped. The answers of a snapshot-path ``prompt_version`` (v4/v5) are
validated as ReviewOutput v2 (``validate_findings.py --schema v2``). Schema-v1
manifests require all three digest fields and publishability metadata consistent
with per-case statuses, so replay cannot silently skip drift or terminal provider
failures. Recorded run_metadata, including any effective settings snapshot, is
returned as provenance;
offline replay does not read the current model environment or detect env drift.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.modules.reviews.application.review_path import (  # noqa: E402
    SNAPSHOT_REVIEW_PROMPT_VERSIONS,
)
from review.scripts.eval_provenance import (  # noqa: E402
    DigestInputError,
    DigestInputMissing,
    corpus_input_paths,
    digest_files,
    rule_json_paths,
    static_input_paths,
)
from review.scripts.eval_score import ScoringCase, score_cases  # noqa: E402
from review.scripts.validate_dataset import (  # noqa: E402
    DEFAULT_ROOT,
    POLICY_ARMS,
    SCHEMA_PATH,
    _patch_added_lines,
    uses_policy_arms,
    validate_case,
)

VALIDATOR_SCRIPT = Path(__file__).with_name("validate_findings.py")
SUCCESS = re.compile(r"OK ReviewOutput \d+ items")
SUCCESS_V2 = re.compile(r"OK ReviewOutputV2 \d+ items")
SHA256 = re.compile(r"[0-9a-f]{64}")
REPO_ROOT = Path(__file__).resolve().parents[2]
# ``legacy`` renders no policy snapshot (the prompt before #163); the others are case arms.
RECORDED_POLICY_ARMS = ("legacy", *POLICY_ARMS)
_DIGEST = re.compile(r"sha256-v1:[0-9a-f]{64}")
_USD = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?")
_USAGE_CALL_FIELDS = {"operation", "tokens_in", "tokens_out", "cache_read_tokens", "cost_usd"}


class ReplayError(Exception):
    """Input corpus or response manifest cannot be replayed reliably."""


def nonpublishable_case_ids(statuses: Mapping[str, Mapping[str, object]]) -> list[str]:
    """Return paid-metadata or terminal failures that invalidate a baseline.

    A paid-metadata failure is always nonpublishable, even with an answer.
    Without that marker, ``llm_invalid_output`` is nonpublishable only with
    ``no_call`` or ``no_content``. Every other terminal gateway status is
    nonpublishable, even if its first call returned an answer. An ``accepted``
    case without the marker remains publishable when a later repair or fallback
    succeeds after an empty first response.
    """
    return sorted(
        case_id
        for case_id, status in statuses.items()
        if status["paid_metadata_error"] is True
        or (
            status["gateway_status"] != "accepted"
            and (
                status["gateway_status"] != "llm_invalid_output"
                or status["first_call"] in {"no_call", "no_content"}
            )
        )
    )


def _usage_call_valid(call: object) -> bool:
    return (
        isinstance(call, dict)
        and set(call) == _USAGE_CALL_FIELDS
        and isinstance(call["operation"], str)
        and bool(call["operation"])
        and all(
            type(call[name]) is int and call[name] >= 0
            for name in ("tokens_in", "tokens_out", "cache_read_tokens")
        )
        and isinstance(call["cost_usd"], str)
        and _USD.fullmatch(call["cost_usd"]) is not None
    )


def _policy_metadata(metadata: dict[str, Any], case_ids: set[str]) -> None:
    """Check the optional #163 fields: the policy arm, its file digest and per-call usage."""
    arm = metadata.get("policy_arm", "legacy")
    if arm not in RECORDED_POLICY_ARMS:
        raise ReplayError("response manifest run_metadata policy_arm is unknown")
    digest = metadata.get("policy_arm_digest")
    if (digest is not None) != (arm in ("valid", "malicious")) or (
        digest is not None and (not isinstance(digest, str) or not _DIGEST.fullmatch(digest))
    ):
        raise ReplayError(
            "response manifest run_metadata policy_arm_digest must be a sha256-v1 digest "
            "exactly for the valid and malicious arms"
        )
    if "usage" not in metadata:
        return
    usage = metadata["usage"]
    if (
        not isinstance(usage, dict)
        or set(usage) != case_ids
        or not all(
            isinstance(calls, list) and all(_usage_call_valid(call) for call in calls)
            for calls in usage.values()
        )
    ):
        raise ReplayError("response manifest run_metadata usage is invalid")


def _manifest(responses_dir: Path) -> dict[str, Any]:
    if responses_dir.is_symlink():
        raise ReplayError("responses directory cannot be a symlink")
    path = responses_dir / "manifest.json"
    if not path.is_file():
        raise ReplayError(f"response manifest is missing: {path}; no baseline responses recorded")
    if path.is_symlink():
        raise ReplayError(f"response manifest cannot be a symlink: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReplayError(f"response manifest cannot be read: {exc}") from exc
    required_fields = {
        "schema_version",
        "model_id",
        "prompt_path",
        "prompt_sha",
        "prompt_version",
        "static_inputs",
        "static_digest",
        "corpus_digest",
        "run_metadata",
        "responses",
    }
    if not isinstance(data, dict) or set(data) != required_fields:
        raise ReplayError("response manifest has missing or unexpected fields")
    inputs = data["static_inputs"]
    if (
        not isinstance(inputs, list)
        or not inputs
        or not all(isinstance(item, str) and item for item in inputs)
        or len(inputs) != len(set(inputs))
        or inputs != sorted(inputs)
    ):
        raise ReplayError("response manifest static_inputs must be sorted unique paths")
    for name in ("static_digest", "corpus_digest"):
        value = data[name]
        if not isinstance(value, str) or re.fullmatch(r"sha256-v1:[0-9a-f]{64}", value) is None:
            raise ReplayError(f"response manifest {name} must be a sha256-v1 digest")
    if data["schema_version"] != 1:
        raise ReplayError("response manifest schema_version must be 1")
    for key in ("model_id", "prompt_path", "prompt_version"):
        if not isinstance(data[key], str) or not data[key].strip():
            raise ReplayError(f"response manifest {key} must be a nonempty string")
    if not isinstance(data["prompt_sha"], str) or not SHA256.fullmatch(data["prompt_sha"]):
        raise ReplayError("response manifest prompt_sha must be a SHA-256 hex digest")
    metadata = data["run_metadata"]
    if not isinstance(metadata, dict) or not metadata:
        raise ReplayError("response manifest run_metadata must be a nonempty object")
    if not isinstance(data["responses"], dict):
        raise ReplayError("response manifest responses must be an object")
    if (
        type(metadata.get("baseline_publishable")) is not bool
        or not isinstance(metadata.get("nonpublishable_case_ids"), list)
        or not isinstance(metadata.get("cases"), dict)
    ):
        raise ReplayError("response manifest run_metadata lacks publishability metadata")
    statuses = metadata["cases"]
    if set(statuses) != set(data["responses"]) or any(
        not isinstance(status, dict)
        or status.get("first_call") not in ("no_call", "no_content", "empty_answer", "answer")
        or not isinstance(status.get("gateway_status"), str)
        or not status["gateway_status"]
        or type(status.get("paid_metadata_error")) is not bool
        or (status["paid_metadata_error"] and status["gateway_status"] != "llm_invalid_output")
        for status in statuses.values()
    ):
        raise ReplayError("response manifest run_metadata cases have invalid statuses")
    expected_ids = nonpublishable_case_ids(statuses)
    expected_publishable = not expected_ids
    if (
        metadata["nonpublishable_case_ids"] != expected_ids
        or metadata["baseline_publishable"] != expected_publishable
    ):
        raise ReplayError("response manifest publishability conflicts with case statuses")
    _policy_metadata(metadata, set(data["responses"]))
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


def _response_path(responses_dir: Path, case_id: str, raw: object) -> Path:
    if not isinstance(raw, str) or not raw:
        raise ReplayError(f"response path for {case_id} must be a nonempty string")
    relative = Path(raw)
    responses_root = responses_dir.resolve()
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or relative.as_posix() != raw
        or not relative.parts
        or relative.parts[0] != "responses"
    ):
        raise ReplayError(f"response path for {case_id} must be relative to dataset root")
    expected = f"responses/{case_id}.json"
    if raw != expected:
        raise ReplayError(f"response path for {case_id} must be {expected}")
    # The leading ``responses`` names the responses directory itself.
    path = responses_dir.joinpath(*relative.parts[1:])
    current = responses_dir
    for part in relative.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise ReplayError(f"response path for {case_id} must not contain a symlink")
    if not path.resolve().is_relative_to(responses_root):
        raise ReplayError(f"response path for {case_id} must stay inside responses/")
    if not path.is_file():
        raise ReplayError(f"response is missing for {case_id}: {path}")
    return path


def _response_paths(responses_dir: Path, entries: dict[str, object]) -> dict[str, Path]:
    """Require one unique mapped file per case and no unreferenced artifacts."""
    mapped: dict[str, Path] = {}
    seen: set[Path] = set()
    for case_id, raw in entries.items():
        path = _response_path(responses_dir, case_id, raw)
        if path in seen:
            raise ReplayError(f"duplicate response path: {path}")
        seen.add(path)
        mapped[case_id] = path
    expected = seen | {responses_dir / "manifest.json"}
    allowed_dirs: set[Path] = set()
    for path in expected:
        parent = path.parent
        while parent != responses_dir and parent.is_relative_to(responses_dir):
            allowed_dirs.add(parent)
            parent = parent.parent
    for path in responses_dir.rglob("*"):
        if path.is_symlink():
            raise ReplayError(f"unreferenced response file or symlink: {path}")
        if path.is_file() and path not in expected:
            raise ReplayError(f"unreferenced response file: {path}")
        if path.is_dir() and path not in allowed_dirs:
            raise ReplayError(f"unreferenced response directory: {path}")
    return mapped


def _validated_raw(path: Path, *, v2: bool) -> tuple[object, bool, int, str]:
    schema = ["--schema", "v2"] if v2 else []
    result = subprocess.run(
        [sys.executable, str(VALIDATOR_SCRIPT), *schema, str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    output = result.stdout.strip() or result.stderr.strip()
    success = SUCCESS_V2 if v2 else SUCCESS
    valid = result.returncode == 0 and success.fullmatch(result.stdout.strip()) is not None
    if not valid:
        return None, False, result.returncode, output
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, False, 2, f"response changed or cannot be read: {exc}"
    return raw, True, result.returncode, output


def _prompt_warnings(manifest: dict[str, Any], prompt_root: Path) -> list[str]:
    relative = Path(manifest["prompt_path"])
    if relative.is_absolute() or ".." in relative.parts:
        raise ReplayError("response manifest prompt_path must be relative to repository root")
    path = prompt_root / relative
    if not path.resolve().is_relative_to(prompt_root.resolve()):
        raise ReplayError("response manifest prompt_path must stay inside repository root")
    if not path.is_file():
        return [f"Prompt file is missing: {manifest['prompt_path']}"]
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != manifest["prompt_sha"]:
        return [
            f"Prompt SHA mismatch for {manifest['prompt_path']}: "
            f"manifest {manifest['prompt_sha']}, current {digest}"
        ]
    return []


def _is_rule_set(path: Path) -> bool:
    """Tell whether a recorded static input is a rule file the recorder could have selected."""
    # CONTRACT: same top-level shape eval_live._rules requires of --backend-rules and
    # --frontend-rules; the path already passed digest_files (in the root, no links, a file).
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        return False
    return isinstance(value, dict) and isinstance(value.get("rules"), list)


def _digest_warnings(
    manifest: dict[str, Any],
    prompt_root: Path,
    root: Path,
    records: list[tuple[Path, dict[str, Any]]],
) -> list[str]:
    warnings: list[str] = []
    recorded_inputs = manifest["static_inputs"]
    try:
        recorded_digest = digest_files(prompt_root, recorded_inputs)
    except DigestInputMissing as exc:
        warnings.append(f"Static input is missing: {exc}")
    except DigestInputError as exc:
        raise ReplayError(str(exc)) from exc
    else:
        try:
            rules = rule_json_paths(prompt_root)
            convention_paths = [
                path
                for path in recorded_inputs
                if path.startswith("review/prompts/") and path != manifest["prompt_path"]
            ]
            conventions_prompt = convention_paths[0] if len(convention_paths) == 1 else None
            # Only a recorded --backend-rules/--frontend-rules file comes back; any other
            # recorded path the current pins no longer produce is a path-set change. A rule
            # file the pins already produce is deduplicated by static_input_paths.
            selected_custom_rules = [
                path for path in recorded_inputs if _is_rule_set(prompt_root / path)
            ]
            current_inputs = static_input_paths(
                manifest["prompt_path"],
                [*rules, *selected_custom_rules],
                conventions_prompt=conventions_prompt,
                repo_root=prompt_root,
            )
            digest_files(prompt_root, current_inputs)
        except DigestInputMissing as exc:
            warnings.append(f"Static input is missing: {exc}")
        except DigestInputError as exc:
            raise ReplayError(str(exc)) from exc
        else:
            if current_inputs != recorded_inputs:
                warnings.append("Static input path set changed; refresh the complete baseline")
            if recorded_digest != manifest["static_digest"]:
                warnings.append("Static input digest mismatch; refresh the complete baseline")
    try:
        current_corpus = digest_files(root, corpus_input_paths(root, records))
    except DigestInputMissing as exc:
        warnings.append(f"Corpus input is missing: {exc}")
    except DigestInputError as exc:
        raise ReplayError(str(exc)) from exc
    else:
        if current_corpus != manifest["corpus_digest"]:
            warnings.append("Corpus input digest mismatch; refresh the complete baseline")
    return warnings


def replay(
    root: Path = DEFAULT_ROOT,
    *,
    prompt_root: Path = REPO_ROOT,
    responses_dir: Path | None = None,
) -> dict[str, Any]:
    """Validate case inputs and raw responses, then return a deterministic report.

    ``responses_dir`` defaults to ``<root>/responses``.
    """
    responses = root / "responses" if responses_dir is None else responses_dir
    records = _case_records(root)
    manifest = _manifest(responses)
    warnings = _prompt_warnings(manifest, prompt_root)
    warnings.extend(_digest_warnings(manifest, prompt_root, root, records))
    if not manifest["run_metadata"]["baseline_publishable"]:
        warnings.append(
            "Recorded capture is not publishable: provider, infrastructure or policy failure"
        )
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

    response_paths = _response_paths(responses, entries)
    usage = manifest["run_metadata"].get("usage")
    # A legacy capture saw no policy snapshot: score it on every truth, as before #163.
    recorded_arm = manifest["run_metadata"].get("policy_arm", "legacy")
    scoring_arm = None if recorded_arm == "legacy" else recorded_arm
    # A snapshot-path prompt (v4/v5) answers in ReviewOutput v2, as the gateway required.
    v2 = manifest["prompt_version"] in SNAPSHOT_REVIEW_PROMPT_VERSIONS
    cases = []
    statuses = []
    for case_dir, record in records:
        case_id = record["id"]
        response_path = response_paths[case_id]
        raw, valid, exit_code, validator_output = _validated_raw(response_path, v2=v2)
        cases.append(
            ScoringCase(
                case_id=case_id,
                truths=tuple(record["expected_findings"]),
                expected_verdict=record["expected_verdict"],
                added_lines=_patch_added_lines(case_dir / record["patch_path"]),
                raw_response=raw,
                response_valid=valid,
                usage=None if usage is None else tuple(usage[case_id]),
                arm=scoring_arm,
                must_not_flag=tuple(record.get("must_not_flag", ())),
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
        "warnings": warnings,
        # The main --final refuses policy-arm cases and the rules profile requires them.
        "corpus": ("rules" if any(uses_policy_arms(record) for _, record in records) else "main"),
        "provenance": {
            key: manifest[key]
            for key in (
                "model_id",
                "prompt_path",
                "prompt_sha",
                "prompt_version",
                "run_metadata",
                "static_inputs",
                "static_digest",
                "corpus_digest",
            )
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
    lines = [f"Cases: {report['case_count']}"]
    metadata = provenance.get("run_metadata", {})
    if "policy_arm" in metadata:
        # Captures since #163 record their arm; older summaries keep their exact lines.
        lines.extend([f"Corpus: {report['corpus']}", f"Policy arm: {metadata['policy_arm']}"])
    lines += [
        f"Model: {provenance['model_id']}",
        f"Prompt: {provenance['prompt_path']} (version {provenance['prompt_version']})",
        f"Prompt SHA: {provenance['prompt_sha']}",
        f"Static digest: {provenance['static_digest']}",
        f"Corpus digest: {provenance['corpus_digest']}",
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
    # Shown only where they apply, so main-corpus summaries keep their exact lines.
    # In arm none the rules are not shown: what is found or flagged there is "without rules".
    rule = report.get("rule_constraints")
    if rule is not None and rule["total"]:
        label = "Rule constraints found" + ("" if rule["expected"] else " without rules")
        lines.append(f"{label}: {_percent(rule['share'])} ({rule['found']}/{rule['total']})")
    exceptions = report.get("exceptions")
    if exceptions is not None and exceptions["total"]:
        total = exceptions["total"]
        if exceptions["exempt"]:
            lines.append(
                f"Exceptions respected: {_percent(exceptions['share'])} "
                f"({exceptions['respected']}/{total})"
            )
        else:
            lines.append(
                f"Exceptions flagged without rules: {_percent(exceptions['flagged'] / total)} "
                f"({exceptions['flagged']}/{total})"
            )
    usage = report.get("usage")
    if usage is not None:
        lines.append(
            f"Usage: calls={usage['calls']} tokens_in={usage['tokens_in']} "
            f"tokens_out={usage['tokens_out']} cost_usd={usage['cost_usd']}"
        )
    for warning in report["warnings"]:
        lines.append(f"Warning: {warning}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="dataset root")
    parser.add_argument("--report-json", type=Path, metavar="PATH", help="write JSON report")
    parser.add_argument(
        "--responses-dir", type=Path, metavar="DIR", help="capture directory (<root>/responses)"
    )
    args = parser.parse_args(argv)
    try:
        report = replay(args.root, responses_dir=args.responses_dir)
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
    if not report["provenance"]["run_metadata"]["baseline_publishable"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
