"""Format replay reports and validate answer contracts."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import review.scripts.eval_replay as eval_replay_module
from app.modules.reviews.infrastructure.llm.answers import (
    validate_review_answer,
    validate_review_answer_v2,
)
from review.scripts.eval_replay import ReplayError, format_console, replay
from tests.eval_replay_fakes import (
    REPO_ROOT,
    finding,
    fixture_root,
    record,
    review_output,
)


def test_format_console_complete_fixed_report() -> None:
    report = {
        "case_count": 2,
        "valid_response_count": 1,
        "validity": 0.5,
        "provenance": {
            "model_id": "model-test",
            "prompt_path": "review/prompts/review.system.v2.md",
            "prompt_version": "v2",
            "prompt_sha": "a" * 64,
            "static_digest": "sha256-v1:" + "b" * 64,
            "corpus_digest": "sha256-v1:" + "c" * 64,
        },
        "micro": {"tp": 2, "fp": 1, "fn": 1, "precision": 2 / 3, "recall": 2 / 3},
        "critical": {"matched": 1, "total": 2, "recall": 0.5},
        "per_category": {
            "security": {"tp": 1, "fp": 0, "fn": 1, "precision": 1.0, "recall": 0.5},
            "correctness": {"tp": 1, "fp": 1, "fn": 0, "precision": 0.5, "recall": 1.0},
            "performance": {"tp": 0, "fp": 0, "fn": 0, "precision": None, "recall": None},
            "readability": {"tp": 0, "fp": 0, "fn": 0, "precision": None, "recall": None},
        },
        "verdict": {"agreed": 1, "total": 2, "agreement": 0.5},
        "severity_mismatches": [{"case_id": "SEC-01"}],
        "warnings": ["Static input digest mismatch", "Corpus input digest mismatch"],
    }
    expected = "\n".join(
        [
            "Cases: 2",
            "Model: model-test",
            "Prompt: review/prompts/review.system.v2.md (version v2)",
            "Prompt SHA: " + "a" * 64,
            "Static digest: sha256-v1:" + "b" * 64,
            "Corpus digest: sha256-v1:" + "c" * 64,
            "Validity: 50.0% (1/2)",
            "Micro: TP=2 FP=1 FN=1 Precision=66.7% Recall=66.7%",
            "Critical Recall: 50.0% (1/2)",
            "Verdict agreement: 50.0% (1/2)",
            "Per category:",
            "  security: TP=1 FP=0 FN=1 Precision=100.0% Recall=50.0%",
            "  correctness: TP=1 FP=1 FN=0 Precision=50.0% Recall=100.0%",
            "  performance: TP=0 FP=0 FN=0 Precision=undefined Recall=undefined",
            "  readability: TP=0 FP=0 FN=0 Precision=undefined Recall=undefined",
            "Severity mismatches: 1",
            "Warning: Static input digest mismatch",
            "Warning: Corpus input digest mismatch",
        ]
    )

    assert format_console(report) == expected


def test_format_console_undefined_denominators() -> None:
    report: dict[str, Any] = {
        "case_count": 0,
        "valid_response_count": 0,
        "validity": None,
        "provenance": {
            "model_id": "empty-model",
            "prompt_path": "review/prompts/review.system.v2.md",
            "prompt_version": "v2",
            "prompt_sha": "d" * 64,
            "static_digest": "sha256-v1:" + "e" * 64,
            "corpus_digest": "sha256-v1:" + "f" * 64,
        },
        "micro": {"tp": 0, "fp": 0, "fn": 0, "precision": None, "recall": None},
        "critical": {"matched": 0, "total": 0, "recall": None},
        "per_category": {
            "security": {"tp": 0, "fp": 0, "fn": 0, "precision": None, "recall": None}
        },
        "verdict": {"agreed": 0, "total": 0, "agreement": None},
        "severity_mismatches": [],
        "warnings": [],
    }
    expected = "\n".join(
        [
            "Cases: 0",
            "Model: empty-model",
            "Prompt: review/prompts/review.system.v2.md (version v2)",
            "Prompt SHA: " + "d" * 64,
            "Static digest: sha256-v1:" + "e" * 64,
            "Corpus digest: sha256-v1:" + "f" * 64,
            "Validity: undefined (0/0)",
            "Micro: TP=0 FP=0 FN=0 Precision=undefined Recall=undefined",
            "Critical Recall: undefined (0/0)",
            "Verdict agreement: undefined (0/0)",
            "Per category:",
            "  security: TP=0 FP=0 FN=0 Precision=undefined Recall=undefined",
            "Severity mismatches: 0",
        ]
    )

    assert format_console(report) == expected


# --- #163: policy arm metadata, per-call usage and moved captures ------------------------------


def test_format_console_prints_the_corpus_arm_and_policy_figures_of_a_rules_capture() -> None:
    report: dict[str, Any] = {
        "case_count": 8,
        "corpus": "rules",
        "valid_response_count": 8,
        "validity": 1.0,
        "provenance": {
            "model_id": "model-test",
            "prompt_path": "review/prompts/review.system.v3.md",
            "prompt_version": "v3",
            "prompt_sha": "a" * 64,
            "static_digest": "sha256-v1:" + "b" * 64,
            "corpus_digest": "sha256-v1:" + "c" * 64,
            "run_metadata": {"policy_arm": "none"},
        },
        "micro": {"tp": 9, "fp": 0, "fn": 0, "precision": 1.0, "recall": 1.0},
        "critical": {"matched": 1, "total": 1, "recall": 1.0},
        "per_category": {},
        "verdict": {"agreed": 8, "total": 8, "agreement": 1.0},
        "severity_mismatches": [],
        "rule_constraints": {"found": 1, "total": 5, "expected": 0, "share": 0.2},
        "exceptions": {"total": 4, "exempt": 0, "flagged": 3, "respected": 1, "share": 0.25},
        "usage": None,
        "warnings": [],
    }

    assert format_console(report) == "\n".join(
        [
            "Cases: 8",
            "Corpus: rules",
            "Policy arm: none",
            "Model: model-test",
            "Prompt: review/prompts/review.system.v3.md (version v3)",
            "Prompt SHA: " + "a" * 64,
            "Static digest: sha256-v1:" + "b" * 64,
            "Corpus digest: sha256-v1:" + "c" * 64,
            "Validity: 100.0% (8/8)",
            "Micro: TP=9 FP=0 FN=0 Precision=100.0% Recall=100.0%",
            "Critical Recall: 100.0% (1/1)",
            "Verdict agreement: 100.0% (8/8)",
            "Per category:",
            "Severity mismatches: 0",
            "Rule constraints found without rules: 20.0% (1/5)",
            "Exceptions flagged without rules: 75.0% (3/4)",
        ]
    )


USAGE_CALL = {
    "operation": "review",
    "tokens_in": 100,
    "tokens_out": 30,
    "cache_read_tokens": 0,
    "cost_usd": "0.00038",
}


def _edit_metadata(root: Path, **fields: object) -> None:
    manifest_path = root / "responses/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["run_metadata"].update(fields)
    manifest_path.write_text(json.dumps(manifest))


def test_capture_without_policy_fields_replays_as_legacy_without_usage(tmp_path: Path) -> None:
    root = fixture_root(tmp_path)
    record(root, {"SEC-01": review_output()})

    report = replay(root)

    assert "policy_arm" not in report["provenance"]["run_metadata"]
    assert report["usage"] is None
    assert "Usage:" not in format_console(report)


def test_recorded_usage_is_totalled_in_the_report(tmp_path: Path) -> None:
    root = fixture_root(tmp_path, ("SEC-01", "CLEAN-01"))
    record(root, {"SEC-01": review_output(), "CLEAN-01": review_output()})
    _edit_metadata(
        root,
        policy_arm="none",
        policy_arm_digest=None,
        usage={"SEC-01": [USAGE_CALL, USAGE_CALL], "CLEAN-01": []},
    )

    report = replay(root)

    assert report["usage"] == {
        "cases": 2,
        "calls": 2,
        "tokens_in": 200,
        "tokens_out": 60,
        "cache_read_tokens": 0,
        "cost_usd": "0.00076",
    }


@pytest.mark.parametrize(
    ("fields", "error"),
    [
        ({"policy_arm": "rag"}, "policy_arm is unknown"),
        ({"policy_arm": "valid", "policy_arm_digest": None}, "policy_arm_digest"),
        ({"policy_arm": "none", "policy_arm_digest": "sha256-v1:" + "a" * 64}, "policy_arm_digest"),
        ({"policy_arm": "malicious", "policy_arm_digest": "sha256-v1:short"}, "policy_arm_digest"),
        ({"usage": {}}, "usage is invalid"),
        ({"usage": {"SEC-01": [{**USAGE_CALL, "tokens_in": -1}]}}, "usage is invalid"),
        ({"usage": {"SEC-01": [{**USAGE_CALL, "tokens_out": True}]}}, "usage is invalid"),
        ({"usage": {"SEC-01": [{**USAGE_CALL, "cost_usd": 0.1}]}}, "usage is invalid"),
        ({"usage": {"SEC-01": [{**USAGE_CALL, "cost_usd": "-0.1"}]}}, "usage is invalid"),
        ({"usage": {"SEC-01": [{**USAGE_CALL, "model": "x"}]}}, "usage is invalid"),
        ({"usage": {"SEC-01": [{**USAGE_CALL, "operation": ""}]}}, "usage is invalid"),
        ({"usage": {"SEC-01": {"calls": []}}}, "usage is invalid"),
    ],
    ids=[
        "unknown-arm",
        "valid-without-digest",
        "none-with-digest",
        "malformed-digest",
        "usage-misses-a-case",
        "negative-tokens",
        "boolean-tokens",
        "float-cost",
        "negative-cost",
        "provider-model-field",
        "empty-operation",
        "usage-not-a-list",
    ],
)
def test_replay_rejects_invalid_policy_metadata(
    tmp_path: Path, fields: dict[str, object], error: str
) -> None:
    root = fixture_root(tmp_path)
    record(root, {"SEC-01": review_output()})
    _edit_metadata(root, **fields)

    with pytest.raises(ReplayError, match=error):
        replay(root)


def test_replay_reads_a_capture_from_another_responses_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = fixture_root(tmp_path)
    record(root, {"SEC-01": review_output(findings=[finding()])})
    moved = tmp_path / "runs" / "valid-1"
    moved.parent.mkdir()
    (root / "responses").rename(moved)

    report = replay(root, responses_dir=moved)

    assert report["micro"] == {"tp": 1, "fp": 0, "fn": 0, "precision": 1.0, "recall": 1.0}
    assert eval_replay_module.main(["--root", str(root), "--responses-dir", str(moved)]) == 0
    assert capsys.readouterr().out == format_console(report) + "\n"
    with pytest.raises(ReplayError, match="response manifest is missing"):
        replay(root)
    (moved / "stray.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ReplayError, match="unreferenced response file"):
        replay(root, responses_dir=moved)


def finding_v2(**changes: Any) -> dict[str, Any]:
    """A ReviewOutput v2 finding of review prompt v4/v5: evidence refs, no attribution prefix."""
    return {
        **finding(),
        "rule_name": "service-defaults:E3",
        "evidence_refs": [{"block_id": "F1:app/authorization.py", "start_line": 1, "end_line": 3}],
        **changes,
    }


@pytest.mark.parametrize(
    ("prompt_version", "item", "valid", "validator_output"),
    [
        ("v4", finding_v2(), True, "OK ReviewOutputV2 1 items"),
        ("v5", finding_v2(), True, "OK ReviewOutputV2 1 items"),
        (
            "v4",
            finding(),
            False,
            "findings[0]: 'evidence_refs' is a required property",
        ),
        (
            "v4",
            finding_v2(title="Authorization check is bypassed."),
            False,
            "findings[0]: Value error, title must not end with a period",
        ),
        (
            "v3",
            finding_v2(),
            False,
            'findings[0].body: rule_name is set, body must start with "According to custom '
            "instructions in 'service-defaults:E3' (\"\n"
            "findings[0]: Additional properties are not allowed ('evidence_refs' was unexpected)",
        ),
    ],
    ids=["v4-answer", "v5-answer", "v4-without-evidence", "v4-model-rule", "v3-keeps-v1"],
)
def test_replay_validates_an_answer_by_the_contract_of_the_recorded_prompt_version(
    tmp_path: Path,
    prompt_version: str,
    item: dict[str, Any],
    valid: bool,
    validator_output: str,
) -> None:
    root = fixture_root(tmp_path)
    record(root, {"SEC-01": review_output(findings=[item])})
    manifest_path = root / "responses/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["prompt_version"] = prompt_version
    manifest_path.write_text(json.dumps(manifest))

    report = replay(root)

    assert report["responses"] == [
        {
            "case_id": "SEC-01",
            "path": "responses/SEC-01.json",
            "valid": valid,
            "validator_exit_code": 0 if valid else 1,
            "validator_output": validator_output,
        }
    ]
    assert report["valid_response_count"] == int(valid)
    assert report["micro"]["tp"] == int(valid)


@pytest.mark.parametrize(
    ("prompt_version", "findings", "validator_output", "gateway", "accepted_findings"),
    [
        (
            "v4",
            [finding_v2(start_line=3)],
            "findings[0]: Value error, start_line must be strictly earlier than line",
            validate_review_answer_v2,
            [finding_v2()],
        ),
        (
            "v4",
            [finding_v2(severity="medium"), finding_v2()],
            "$: Value error, findings must be severity then confidence ordered",
            validate_review_answer_v2,
            [finding_v2(), finding_v2(severity="medium")],
        ),
        (
            "v3",
            [{**finding(), "start_line": 3}],
            "findings[0].start_line: must be < line",
            validate_review_answer,
            [finding()],
        ),
        (
            "v3",
            [{**finding(), "severity": "medium"}, finding()],
            "findings: must be ordered by severity, then by confidence descending",
            validate_review_answer,
            [finding(), {**finding(), "severity": "medium"}],
        ),
    ],
    ids=["v4-start-line-is-line", "v4-unordered", "v3-start-line-is-line", "v3-unordered"],
)
def test_replay_rejects_the_raw_answer_the_gateway_normalizes(
    tmp_path: Path,
    prompt_version: str,
    findings: list[dict[str, Any]],
    validator_output: str,
    gateway: Callable[[str], dict[str, object]],
    accepted_findings: list[dict[str, Any]],
) -> None:
    """Replay scores the raw first answer before the gateway's repair, for v2 as for v1."""
    answer = review_output(findings=findings)
    root = fixture_root(tmp_path)
    record(root, {"SEC-01": answer})
    manifest_path = root / "responses/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["prompt_version"] = prompt_version
    manifest_path.write_text(json.dumps(manifest))

    report = replay(root)

    assert report["responses"] == [
        {
            "case_id": "SEC-01",
            "path": "responses/SEC-01.json",
            "valid": False,
            "validator_exit_code": 1,
            "validator_output": validator_output,
        }
    ]
    assert report["valid_response_count"] == 0
    assert gateway(json.dumps(answer)) == review_output(findings=accepted_findings)


VALIDATE_FINDINGS = REPO_ROOT / "review" / "scripts" / "validate_findings.py"
USAGE = "usage: validate_findings.py [--schema v1|v2] <file.json>\n"


@pytest.mark.parametrize(
    ("arguments", "exit_code", "stdout", "stderr"),
    [
        (["--schema", "v2", "{v2}"], 0, "OK ReviewOutputV2 1 items\n", ""),
        (["--schema", "v1", "{findings}"], 0, "OK ReviewOutput 5 items\n", ""),
        (["{conventions}"], 0, "OK RepoConventionsDraft 3 items\n", ""),
        (["--schema", "v3", "{v2}"], 2, "", USAGE),
        (["--schema", "{v2}"], 2, "", USAGE),
        (["{v2}", "--schema", "v2"], 2, "", USAGE),
        (
            ["--schema", "v2", "{conventions}"],
            2,
            "",
            "error: --schema v2 checks a ReviewOutput ('findings'); this file has 'files'\n",
        ),
    ],
    ids=[
        "v2",
        "explicit-v1",
        "conventions",
        "unknown-schema",
        "missing-value",
        "option-after-file",
        "v2-on-conventions",
    ],
)
def test_validate_findings_takes_one_file_and_an_optional_answer_schema(
    tmp_path: Path, arguments: list[str], exit_code: int, stdout: str, stderr: str
) -> None:
    answer_v2 = tmp_path / "answer-v2.json"
    answer_v2.write_text(json.dumps(review_output(findings=[finding_v2()])), encoding="utf-8")
    paths = {
        "v2": str(answer_v2),
        "findings": str(REPO_ROOT / "review/examples/findings.sample.json"),
        "conventions": str(REPO_ROOT / "review/examples/conventions.sample.json"),
    }

    result = subprocess.run(
        [sys.executable, str(VALIDATE_FINDINGS), *(item.format(**paths) for item in arguments)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert (result.returncode, result.stdout, result.stderr) == (exit_code, stdout, stderr)
