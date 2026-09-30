"""Offline replay contract for recorded issue #30 model responses."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from review.scripts.eval_replay import ReplayError, format_console, replay

REPO_ROOT = Path(__file__).resolve().parents[1]
DATASET_ROOT = REPO_ROOT / "test-prs-dataset"
SCRIPT = REPO_ROOT / "review" / "scripts" / "eval_replay.py"


def finding() -> dict[str, Any]:
    return {
        "path": "app/authorization.py",
        "line": 3,
        "start_line": None,
        "severity": "critical",
        "category": "security",
        "title": "Authorization check is bypassed",
        "body": "The permission check does not protect this path.",
        "suggestion": None,
        "confidence": 0.9,
        "rule_name": None,
    }


def review_output(*, findings: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "findings": [] if findings is None else findings,
        "summary": {
            "problem": "Authorization is incomplete.",
            "done_well": "The handler is readable.",
            "effort": "small",
        },
    }


def fixture_root(tmp_path: Path, case_ids: tuple[str, ...] = ("SEC-01",)) -> Path:
    root = tmp_path / "dataset"
    for case_id in case_ids:
        shutil.copytree(DATASET_ROOT / "cases" / case_id, root / "cases" / case_id)
    (root / "responses").mkdir(parents=True)
    return root


def record(root: Path, responses: dict[str, object]) -> None:
    for case_id, body in responses.items():
        (root / "responses" / f"{case_id}.json").write_text(json.dumps(body), encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "model_id": "test-model",
        "prompt_path": "review/prompts/review.system.v1.md",
        "prompt_sha": "a" * 64,
        "prompt_version": "v1",
        "run_metadata": {"recorded_at": "2026-09-29T00:00:00Z"},
        "responses": {case_id: f"responses/{case_id}.json" for case_id in responses},
    }
    (root / "responses" / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_replay_scores_valid_responses_and_preserves_provenance(tmp_path: Path) -> None:
    root = fixture_root(tmp_path, ("SEC-01", "CLEAN-01"))
    record(root, {"SEC-01": review_output(findings=[finding()]), "CLEAN-01": review_output()})

    report = replay(root)

    assert report["schema_version"] == 1
    assert report["provenance"] == {
        "model_id": "test-model",
        "prompt_path": "review/prompts/review.system.v1.md",
        "prompt_sha": "a" * 64,
        "prompt_version": "v1",
        "run_metadata": {"recorded_at": "2026-09-29T00:00:00Z"},
    }
    assert report["case_count"] == 2
    assert report["validity"] == 1.0
    assert report["micro"] == {"tp": 1, "fp": 0, "fn": 0, "precision": 1.0, "recall": 1.0}
    assert report["critical"] == {"matched": 1, "total": 1, "recall": 1.0}
    assert report["per_category"]["security"]["tp"] == 1
    assert report["verdict"] == {"agreed": 2, "total": 2, "agreement": 1.0}
    assert all(item["validator_exit_code"] == 0 for item in report["responses"])


def test_replay_warns_when_recorded_prompt_differs_from_current_file(tmp_path: Path) -> None:
    root = fixture_root(tmp_path)
    record(root, {"SEC-01": review_output()})
    prompt_root = tmp_path / "repo"
    prompt = prompt_root / "review" / "prompts" / "review.system.v1.md"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("recorded prompt\n", encoding="utf-8")
    manifest_path = root / "responses" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["prompt_sha"] = hashlib.sha256(prompt.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert replay(root, prompt_root=prompt_root)["warnings"] == []

    prompt.write_text("changed prompt\n", encoding="utf-8")
    report = replay(root, prompt_root=prompt_root)

    assert len(report["warnings"]) == 1
    assert "Prompt SHA mismatch" in report["warnings"][0]
    assert "Warning: Prompt SHA mismatch" in format_console(report)


def test_replay_warns_when_recorded_prompt_is_missing(tmp_path: Path) -> None:
    root = fixture_root(tmp_path)
    record(root, {"SEC-01": review_output()})

    report = replay(root, prompt_root=tmp_path / "empty-repo")

    assert report["warnings"] == ["Prompt file is missing: review/prompts/review.system.v1.md"]


@pytest.mark.parametrize(
    ("raw", "expected_code"),
    [
        ({"findings": [{"line": "bad"}], "summary": {}}, 1),
        ({"nonsense": True}, 2),
        (
            {
                "files": [{"path": "app/main.py", "relevance": "Entry point."}],
                "key_patterns": ["First.", "Second.", "Third."],
                "recommendations": [
                    f"Recommendation {n} (from: standard/correctness)" for n in range(5)
                ],
            },
            0,
        ),
    ],
)
def test_invalid_outputs_stay_in_validity_denominator_and_add_only_fn(
    tmp_path: Path, raw: object, expected_code: int
) -> None:
    root = fixture_root(tmp_path)
    record(root, {"SEC-01": raw})

    report = replay(root)

    assert report["responses"][0]["validator_exit_code"] == expected_code
    assert report["responses"][0]["valid"] is False
    assert report["validity"] == 0.0
    assert report["micro"] == {"tp": 0, "fp": 0, "fn": 1, "precision": None, "recall": 0.0}
    assert report["verdict"]["agreement"] == 0.0


def test_cli_writes_byte_identical_json_and_console_metrics(tmp_path: Path) -> None:
    root = fixture_root(tmp_path)
    record(root, {"SEC-01": review_output(findings=[finding()])})
    outputs = []
    for index in range(2):
        target = tmp_path / f"report-{index}.json"
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--root", str(root), "--report-json", str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "Validity: 100.0% (1/1)" in result.stdout
        assert "Critical Recall: 100.0% (1/1)" in result.stdout
        assert "security: TP=1 FP=0 FN=0" in result.stdout
        assert "Model: test-model" in result.stdout
        assert "Prompt SHA: " + "a" * 64 in result.stdout
        outputs.append(target.read_bytes())
    assert outputs[0] == outputs[1]


def test_missing_manifest_or_response_is_clear_error(tmp_path: Path) -> None:
    root = fixture_root(tmp_path)
    with pytest.raises(ReplayError, match="manifest is missing"):
        replay(root)

    record(root, {"SEC-01": review_output()})
    (root / "responses" / "SEC-01.json").unlink()
    with pytest.raises(ReplayError, match="response is missing.*SEC-01"):
        replay(root)


def test_manifest_must_cover_case_ids_exactly(tmp_path: Path) -> None:
    root = fixture_root(tmp_path, ("SEC-01", "CLEAN-01"))
    record(root, {"SEC-01": review_output()})

    with pytest.raises(ReplayError, match="missing CLEAN-01"):
        replay(root)


def test_replay_rejects_broken_case_before_scoring(tmp_path: Path) -> None:
    root = fixture_root(tmp_path)
    record(root, {"SEC-01": review_output()})
    (root / "cases" / "SEC-01" / "diff.patch").write_text("not a patch\n", encoding="utf-8")

    with pytest.raises(ReplayError, match="invalid case SEC-01"):
        replay(root)


def test_manifest_response_path_cannot_leave_responses(tmp_path: Path) -> None:
    root = fixture_root(tmp_path)
    record(root, {"SEC-01": review_output()})
    manifest_path = root / "responses" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["responses"]["SEC-01"] = "../outside.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ReplayError, match="must be relative"):
        replay(root)


def test_cli_missing_manifest_exits_with_guidance(tmp_path: Path) -> None:
    root = fixture_root(tmp_path)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "manifest is missing" in result.stderr
    assert "no baseline responses" in result.stderr
