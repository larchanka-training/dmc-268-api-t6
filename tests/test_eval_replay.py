"""Offline replay contract for recorded issue #30 model responses."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import review.scripts.eval_live as eval_live_module
import review.scripts.eval_rag_context as eval_rag_context_module
import review.scripts.eval_replay as eval_replay_module
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.infrastructure.llm.answers import (
    validate_review_answer,
    validate_review_answer_v2,
)
from app.modules.reviews.infrastructure.llm.settings import LlmSettings
from review.scripts import eval_provenance
from review.scripts.eval_provenance import (
    DigestInputError,
    corpus_input_paths,
    digest_files,
    rule_json_paths,
    static_input_paths,
)
from review.scripts.eval_replay import ReplayError, format_console, nonpublishable_case_ids, replay
from review.scripts.rag_snapshot import build_case_snapshot, render_snapshot
from tests.test_eval_live import settings as rag_settings
from tests.test_eval_live_rag import (
    CASES,
    rag_root,
    record_rag,
    valid_answers,
    window_without_rag,
)

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
    static_inputs = static_input_paths(
        "review/prompts/review.system.v1.md", rule_json_paths(REPO_ROOT)
    )
    records = [
        (case_dir, json.loads((case_dir / "case.json").read_text()))
        for case_dir in sorted((root / "cases").iterdir())
    ]
    manifest = {
        "schema_version": 1,
        "model_id": "test-model",
        "prompt_path": "review/prompts/review.system.v1.md",
        "prompt_sha": "a" * 64,
        "prompt_version": "v1",
        "static_inputs": static_inputs,
        "static_digest": digest_files(REPO_ROOT, static_inputs),
        "corpus_digest": digest_files(root, corpus_input_paths(root, records)),
        "run_metadata": {
            "recorded_at": "2026-09-29T00:00:00Z",
            "baseline_publishable": True,
            "nonpublishable_case_ids": [],
            "cases": {
                case_id: {
                    "first_call": "answer",
                    "gateway_status": "accepted",
                    "paid_metadata_error": False,
                }
                for case_id in responses
            },
        },
        "responses": {case_id: f"responses/{case_id}.json" for case_id in responses},
    }
    (root / "responses" / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


@pytest.mark.parametrize(
    "missing_fields",
    [
        ("static_inputs", "static_digest", "corpus_digest"),
        ("static_inputs",),
        ("static_digest",),
        ("corpus_digest",),
    ],
)
def test_replay_rejects_manifest_without_provenance_digests(
    tmp_path: Path, missing_fields: tuple[str, ...]
) -> None:
    root = fixture_root(tmp_path)
    record(root, {"SEC-01": review_output()})
    manifest_path = root / "responses/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for field in missing_fields:
        manifest.pop(field, None)
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(ReplayError, match="missing or unexpected fields"):
        replay(root)


@pytest.mark.parametrize(
    "missing_field", ["baseline_publishable", "nonpublishable_case_ids", "cases"]
)
def test_replay_rejects_manifest_without_publishability_metadata(
    tmp_path: Path, missing_field: str
) -> None:
    root = fixture_root(tmp_path)
    record(root, {"SEC-01": review_output()})
    manifest_path = root / "responses/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["run_metadata"].pop(missing_field)
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(ReplayError, match="run_metadata"):
        replay(root)


@pytest.mark.parametrize(
    ("publishable", "case_ids"),
    [(True, []), (False, []), (True, ["SEC-01"])],
)
def test_replay_rejects_publishability_inconsistent_with_terminal_failure(
    tmp_path: Path, publishable: bool, case_ids: list[str]
) -> None:
    root = fixture_root(tmp_path)
    record(root, {"SEC-01": review_output()})
    manifest_path = root / "responses/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    metadata = manifest["run_metadata"]
    metadata["cases"]["SEC-01"] = {
        "first_call": "no_content",
        "gateway_status": "llm_payment_required",
        "paid_metadata_error": False,
    }
    metadata["baseline_publishable"] = publishable
    metadata["nonpublishable_case_ids"] = case_ids
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(ReplayError, match="publishability"):
        replay(root)


def test_replay_rejects_case_status_without_paid_metadata_marker(tmp_path: Path) -> None:
    root = fixture_root(tmp_path)
    record(root, {"SEC-01": review_output()})
    manifest_path = root / "responses/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["run_metadata"]["cases"]["SEC-01"].pop("paid_metadata_error")
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(ReplayError, match="invalid statuses"):
        replay(root)


@pytest.mark.parametrize("change", ["missing", "extra"])
def test_replay_rejects_case_status_ids_different_from_responses(
    tmp_path: Path, change: str
) -> None:
    root = fixture_root(tmp_path)
    record(root, {"SEC-01": review_output()})
    manifest_path = root / "responses/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    statuses = manifest["run_metadata"]["cases"]
    if change == "missing":
        statuses.pop("SEC-01")
    else:
        statuses["CLEAN-01"] = statuses["SEC-01"].copy()
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(ReplayError, match="run_metadata cases have invalid statuses"):
        replay(root)


@pytest.mark.parametrize(
    "status",
    [
        [],
        {"first_call": "unknown", "gateway_status": "accepted", "paid_metadata_error": False},
        {"first_call": "answer", "gateway_status": "", "paid_metadata_error": False},
        {"first_call": "answer", "gateway_status": 200, "paid_metadata_error": False},
        {"first_call": "answer", "gateway_status": "accepted", "paid_metadata_error": "false"},
        {"first_call": "answer", "gateway_status": "accepted", "paid_metadata_error": True},
    ],
)
def test_replay_rejects_malformed_case_status(tmp_path: Path, status: object) -> None:
    root = fixture_root(tmp_path)
    record(root, {"SEC-01": review_output()})
    manifest_path = root / "responses/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["run_metadata"]["cases"]["SEC-01"] = status
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(ReplayError, match="run_metadata cases have invalid statuses"):
        replay(root)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("static_digest", "unrecorded"),
        ("static_digest", "sha256-v1:" + "A" * 64),
        ("corpus_digest", None),
        ("corpus_digest", "sha256-v1:short"),
    ],
)
def test_replay_rejects_invalid_digest_fields(tmp_path: Path, field: str, value: object) -> None:
    root = fixture_root(tmp_path)
    record(root, {"SEC-01": review_output()})
    manifest_path = root / "responses/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest[field] = value
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(ReplayError, match=f"response manifest {field} must be a sha256-v1 digest"):
        replay(root)


@pytest.mark.parametrize(
    ("gateway_status", "first_call", "paid_metadata_error", "expected"),
    [
        ("accepted", "answer", False, []),
        ("accepted", "no_content", False, []),
        ("accepted", "empty_answer", False, []),
        ("llm_invalid_output", "answer", False, []),
        ("llm_invalid_output", "empty_answer", False, []),
        ("llm_invalid_output", "no_call", False, ["CASE"]),
        ("llm_invalid_output", "no_content", False, ["CASE"]),
        ("llm_invalid_output", "answer", True, ["CASE"]),
        ("accepted", "answer", True, ["CASE"]),
        ("llm_payment_required", "answer", False, ["CASE"]),
        ("llm_unavailable", "answer", False, ["CASE"]),
        ("budget_exceeded", "answer", False, ["CASE"]),
    ],
)
def test_nonpublishable_case_status_table(
    gateway_status: str,
    first_call: str,
    paid_metadata_error: bool,
    expected: list[str],
) -> None:
    statuses = {
        "CASE": {
            "gateway_status": gateway_status,
            "first_call": first_call,
            "paid_metadata_error": paid_metadata_error,
        }
    }

    assert nonpublishable_case_ids(statuses) == expected


def test_nonpublishable_case_ids_are_sorted() -> None:
    statuses = {
        case_id: {
            "gateway_status": "llm_payment_required",
            "first_call": "answer",
            "paid_metadata_error": False,
        }
        for case_id in ("Z", "A", "M")
    }

    assert nonpublishable_case_ids(statuses) == ["A", "M", "Z"]


def test_replay_scores_valid_responses_and_preserves_provenance(tmp_path: Path) -> None:
    root = fixture_root(tmp_path, ("SEC-01", "CLEAN-01"))
    record(root, {"SEC-01": review_output(findings=[finding()]), "CLEAN-01": review_output()})

    report = replay(root)

    assert report["schema_version"] == 1
    manifest = json.loads((root / "responses/manifest.json").read_text())
    assert report["provenance"] == {
        "model_id": "test-model",
        "prompt_path": "review/prompts/review.system.v1.md",
        "prompt_sha": "a" * 64,
        "prompt_version": "v1",
        "static_inputs": manifest["static_inputs"],
        "static_digest": manifest["static_digest"],
        "corpus_digest": manifest["corpus_digest"],
        "run_metadata": manifest["run_metadata"],
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
    prompt_root = tmp_path / "repo"
    prompt, _ = _record_with_digests(root, prompt_root)

    assert replay(root, prompt_root=prompt_root)["warnings"] == []

    prompt.write_text("changed prompt\n", encoding="utf-8")
    report = replay(root, prompt_root=prompt_root)

    assert len(report["warnings"]) == 2
    assert "Prompt SHA mismatch" in report["warnings"][0]
    assert report["warnings"][1] == ("Static input digest mismatch; refresh the complete baseline")
    assert "Warning: Prompt SHA mismatch" in format_console(report)


def test_replay_warns_when_recorded_prompt_is_missing(tmp_path: Path) -> None:
    root = fixture_root(tmp_path)
    record(root, {"SEC-01": review_output()})

    report = replay(root, prompt_root=tmp_path / "empty-repo")

    assert report["warnings"][0] == "Prompt file is missing: review/prompts/review.system.v1.md"


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


def test_cli_prints_both_drift_warnings_and_keeps_publishable_exit_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = fixture_root(tmp_path)
    repo = tmp_path / "repo"
    _, rule = _record_with_digests(root, repo)
    (repo / "review/rules/new-rule.json").write_text('{"rules": []}\n')
    rule.write_text('{"rules": [{"name": "changed"}]}\n')

    def replay_fixture(root: Path, *, responses_dir: Path | None = None) -> dict[str, Any]:
        return replay(root, prompt_root=repo, responses_dir=responses_dir)

    monkeypatch.setattr(eval_replay_module, "replay", replay_fixture)

    assert eval_replay_module.main(["--root", str(root)]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert [line for line in captured.out.splitlines() if line.startswith("Warning:")] == [
        "Warning: Static input path set changed; refresh the complete baseline",
        "Warning: Static input digest mismatch; refresh the complete baseline",
    ]


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


def _record_with_digests(root: Path, repo: Path) -> tuple[Path, Path]:
    record(root, {"SEC-01": review_output()})
    prompt = repo / "review/prompts/review.system.v1.md"
    rule = repo / "review/rules/default-backend.v1.json"
    prompt.parent.mkdir(parents=True)
    rule.parent.mkdir(parents=True)
    prompt.write_text("recorded prompt\n")
    rule.write_text('{"rules": []}\n')
    static_paths = static_input_paths(
        "review/prompts/review.system.v1.md",
        ["review/rules/default-backend.v1.json"],
    )
    for relative in static_paths:
        path = repo / relative
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            if relative.startswith("test-prs-dataset/cases/"):
                shutil.copy2(REPO_ROOT / relative, path)
            else:
                path.write_text(f"fixture {relative}\n")
    manifest_path = root / "responses/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["prompt_sha"] = hashlib.sha256(prompt.read_bytes()).hexdigest()
    manifest["static_inputs"] = static_paths
    manifest["static_digest"] = digest_files(repo, static_paths)
    case_dir = root / "cases/SEC-01"
    records = [(case_dir, json.loads((case_dir / "case.json").read_text()))]
    manifest["corpus_digest"] = digest_files(root, corpus_input_paths(root, records))
    manifest_path.write_text(json.dumps(manifest))
    return prompt, rule


def test_run_failure_deadline_change_triggers_static_drift(tmp_path: Path) -> None:
    root = fixture_root(tmp_path)
    repo = tmp_path / "repo"
    _record_with_digests(root, repo)
    run_failures = "app/modules/reviews/application/run_failures.py"
    assert run_failures in static_input_paths(
        "review/prompts/review.system.v1.md", rule_json_paths(repo)
    )
    assert replay(root, prompt_root=repo)["warnings"] == []

    target = repo / run_failures
    target.write_text(target.read_text() + "FAST_ATTEMPT_DEADLINE = 9 * 60\n")

    assert replay(root, prompt_root=repo)["warnings"] == [
        "Static input digest mismatch; refresh the complete baseline"
    ]


def test_replay_warns_on_rule_only_edit_without_rewriting_raw_answer(tmp_path: Path) -> None:
    root = fixture_root(tmp_path)
    repo = tmp_path / "repo"
    _, rule = _record_with_digests(root, repo)
    answer_path = root / "responses/SEC-01.json"
    original = answer_path.read_bytes()

    assert replay(root, prompt_root=repo)["warnings"] == []
    rule.write_text('{"rules": [{"name": "changed"}]}\n')
    report = replay(root, prompt_root=repo)

    assert report["warnings"] == ["Static input digest mismatch; refresh the complete baseline"]
    assert answer_path.read_bytes() == original
    assert report["validity"] == 1.0


def test_replay_warns_on_valid_case_patch_edit_without_rewriting_answer(tmp_path: Path) -> None:
    root = fixture_root(tmp_path)
    repo = tmp_path / "repo"
    _record_with_digests(root, repo)
    answer_path = root / "responses/SEC-01.json"
    original = answer_path.read_bytes()
    patch = root / "cases/SEC-01/diff.patch"

    assert replay(root, prompt_root=repo)["warnings"] == []
    patch.write_text(
        patch.read_text().replace("FAKE_TEST_CREDENTIAL_DO_NOT_USE", "CHANGED_FAKE_CREDENTIAL")
    )
    report = replay(root, prompt_root=repo)

    assert report["warnings"] == ["Corpus input digest mismatch; refresh the complete baseline"]
    assert answer_path.read_bytes() == original
    assert report["validity"] == 1.0


def test_digest_is_order_stable_and_rejects_unsafe_or_missing_paths(tmp_path: Path) -> None:
    root = tmp_path / "inputs"
    root.mkdir()
    (root / "a.txt").write_bytes(b"A")
    (root / "b.txt").write_bytes(b"B")
    expected = digest_files(root, ["a.txt", "b.txt"])

    assert expected == "sha256-v1:96ed7c9a60cf473e82e7c0bcf767d043697dd0783e0e6e04bbb972cdf25f3bb6"
    assert digest_files(root, ["b.txt", "a.txt"]) == expected
    assert digest_files(root, ["a.txt", "b.txt", "a.txt"]) == expected
    with pytest.raises(DigestInputError, match="unsafe"):
        digest_files(root, ["../outside.txt"])
    (root / "link.txt").symlink_to(root / "a.txt")
    with pytest.raises(DigestInputError, match="symlink"):
        digest_files(root, ["link.txt"])
    with pytest.raises(DigestInputError, match="missing"):
        digest_files(root, ["missing.txt"])


@pytest.mark.parametrize(
    "changed",
    [
        "app/modules/reviews/application/prompt_builder.py",
        "app/modules/reviews/application/prompt_budget.py",
        "app/modules/reviews/infrastructure/llm/gateway.py",
        "app/modules/reviews/infrastructure/llm/memory.py",
        "review/scripts/eval_live.py",
        "app/modules/reviews/infrastructure/llm/settings.py",
        "app/modules/reviews/infrastructure/llm/transport.py",
    ],
)
def test_request_shaping_source_edit_changes_static_digest(tmp_path: Path, changed: str) -> None:
    repo = tmp_path / "repo"
    inputs = static_input_paths("review/prompts/review.system.v2.md", [])
    for relative in inputs:
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("original\n", encoding="utf-8")
    target = repo / changed
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("original\n", encoding="utf-8")
    original = digest_files(repo, inputs)

    target.write_text("updated request shape\n", encoding="utf-8")

    assert changed in inputs
    assert digest_files(repo, inputs) != original


def test_static_paths_include_optional_conventions_renderer() -> None:
    paths = static_input_paths(
        "review/prompts/review.system.v2.md",
        ["review/rules/b.json", "review/rules/a.json"],
        conventions_prompt="review/prompts/review.conventions.v2.md",
    )

    assert paths == sorted(paths)
    assert "app/modules/reviews/application/prompt_builder.py" in paths
    assert "app/modules/reviews/application/prompt_budget.py" in paths
    assert "app/modules/reviews/application/conventions_prompt.py" in paths
    assert "review/prompts/review.conventions.v2.md" in paths
    assert "review/rules/a.json" in paths and "review/rules/b.json" in paths


def test_replay_rejects_unsafe_static_manifest_path(tmp_path: Path) -> None:
    root = fixture_root(tmp_path)
    repo = tmp_path / "repo"
    _record_with_digests(root, repo)
    manifest_path = root / "responses/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["static_inputs"] = ["../outside.txt"]
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(ReplayError, match="unsafe digest path"):
        replay(root, prompt_root=repo)


def test_replay_rejects_unsafe_static_path_named_missing(tmp_path: Path) -> None:
    root = fixture_root(tmp_path)
    repo = tmp_path / "repo"
    _record_with_digests(root, repo)
    manifest_path = root / "responses/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["static_inputs"] = ["../missing.txt"]
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(ReplayError, match="unsafe digest path"):
        replay(root, prompt_root=repo)


def test_replay_rejects_static_digest_directory_input(tmp_path: Path) -> None:
    root = fixture_root(tmp_path)
    repo = tmp_path / "repo"
    _record_with_digests(root, repo)
    manifest_path = root / "responses/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["static_inputs"] = ["review/rules"]
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(ReplayError, match="digest path is not a file"):
        replay(root, prompt_root=repo)


@pytest.mark.parametrize("rule_directory", ["missing", "symlink"])
def test_replay_classifies_current_rule_directory_failure(
    tmp_path: Path, rule_directory: str
) -> None:
    root = fixture_root(tmp_path)
    repo = tmp_path / "repo"
    _record_with_digests(root, repo)
    manifest_path = root / "responses/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["static_inputs"] = [
        path for path in manifest["static_inputs"] if not path.startswith("review/rules/")
    ]
    manifest["static_digest"] = digest_files(repo, manifest["static_inputs"])
    manifest_path.write_text(json.dumps(manifest))
    rules_dir = repo / "review/rules"
    shutil.rmtree(rules_dir)
    if rule_directory == "symlink":
        outside = tmp_path / "outside-rules"
        outside.mkdir()
        rules_dir.symlink_to(outside, target_is_directory=True)
        with pytest.raises(ReplayError, match="symlink rule directory"):
            replay(root, prompt_root=repo)
    else:
        assert replay(root, prompt_root=repo)["warnings"] == [
            f"Static input is missing: missing rule directory: {rules_dir}"
        ]


@pytest.mark.parametrize("base_path", ["missing", "diff.patch", "../missing"])
def test_corpus_digest_rejects_missing_or_unsafe_preimage_inputs(
    tmp_path: Path, base_path: str
) -> None:
    root = fixture_root(tmp_path, ("SEC-01",))
    case_dir = root / "cases/SEC-01"
    record_data = json.loads((case_dir / "case.json").read_text())
    record_data["base_path"] = base_path

    if base_path == "missing":
        with pytest.raises(eval_provenance.DigestInputMissing, match="missing pre-image directory"):
            corpus_input_paths(root, [(case_dir, record_data)])
    elif base_path == "diff.patch":
        with pytest.raises(DigestInputError, match="pre-image path is not a directory"):
            corpus_input_paths(root, [(case_dir, record_data)])
    else:
        with pytest.raises(DigestInputError, match="unsafe pre-image directory"):
            corpus_input_paths(root, [(case_dir, record_data)])


def test_missing_digest_input_has_distinct_error_type(tmp_path: Path) -> None:
    with pytest.raises(DigestInputError, match="missing") as error:
        digest_files(tmp_path, ["missing.txt"])
    assert isinstance(error.value, eval_provenance.DigestInputMissing)

    with pytest.raises(DigestInputError, match="missing") as error:
        rule_json_paths(tmp_path)
    assert isinstance(error.value, eval_provenance.DigestInputMissing)


def test_rule_json_paths_rejects_regular_file_instead_of_directory(tmp_path: Path) -> None:
    rules = tmp_path / "review/rules"
    rules.parent.mkdir(parents=True)
    rules.write_text("not a directory\n")

    with pytest.raises(DigestInputError, match="rule path is not a directory"):
        rule_json_paths(tmp_path)


def test_replay_warns_when_static_input_is_missing(tmp_path: Path) -> None:
    root = fixture_root(tmp_path)
    repo = tmp_path / "repo"
    _, rule = _record_with_digests(root, repo)
    original = (root / "responses/SEC-01.json").read_bytes()
    rule.unlink()

    report = replay(root, prompt_root=repo)

    assert len(report["warnings"]) == 1
    assert report["warnings"][0].startswith("Static input is missing:")
    assert (root / "responses/SEC-01.json").read_bytes() == original


def test_replay_warns_when_corpus_input_disappears_after_enumeration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = fixture_root(tmp_path)
    repo = tmp_path / "repo"
    _record_with_digests(root, repo)
    disappearing = root / "cases/SEC-01/base/README.md"
    original_paths = corpus_input_paths

    def enumerate_then_remove(
        dataset_root: Path, records: list[tuple[Path, dict[str, Any]]]
    ) -> list[str]:
        paths = original_paths(dataset_root, records)
        disappearing.unlink()
        return paths

    monkeypatch.setattr(eval_replay_module, "corpus_input_paths", enumerate_then_remove)
    report = replay(root, prompt_root=repo)

    assert report["warnings"] == [
        "Corpus input is missing: missing digest file: cases/SEC-01/base/README.md"
    ]
    assert report["case_count"] == 1


def test_replay_rejects_corpus_path_replaced_by_symlink_after_enumeration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = fixture_root(tmp_path)
    repo = tmp_path / "repo"
    _record_with_digests(root, repo)
    raw = (root / "responses/SEC-01.json").read_bytes()
    replaced = root / "cases/SEC-01/base/README.md"
    outside = tmp_path / "outside.txt"
    outside.write_text("unsafe replacement\n")
    original_paths = corpus_input_paths

    def enumerate_then_replace(
        dataset_root: Path, records: list[tuple[Path, dict[str, Any]]]
    ) -> list[str]:
        paths = original_paths(dataset_root, records)
        replaced.unlink()
        replaced.symlink_to(outside)
        return paths

    monkeypatch.setattr(eval_replay_module, "corpus_input_paths", enumerate_then_replace)

    with pytest.raises(ReplayError, match="symlink digest path"):
        replay(root, prompt_root=repo)
    assert (root / "responses/SEC-01.json").read_bytes() == raw


@pytest.mark.parametrize("changed", ["case.json", "base/README.md"])
def test_corpus_digest_covers_metadata_and_preimages(tmp_path: Path, changed: str) -> None:
    root = fixture_root(tmp_path)
    repo = tmp_path / "repo"
    _record_with_digests(root, repo)
    case_dir = root / "cases/SEC-01"
    record_data = json.loads((case_dir / "case.json").read_text())
    paths = corpus_input_paths(root, [(case_dir, record_data)])
    assert "cases/SEC-01/case.json" in paths
    assert "cases/SEC-01/diff.patch" in paths
    assert "cases/SEC-01/base/README.md" in paths
    original = (root / "responses/SEC-01.json").read_bytes()
    target = case_dir / changed
    if changed == "case.json":
        record_data["source"]["attribution"] += " updated"
        target.write_text(json.dumps(record_data))
    else:
        target.write_text(target.read_text() + "updated pre-image note\n")

    report = replay(root, prompt_root=repo)

    assert report["warnings"] == ["Corpus input digest mismatch; refresh the complete baseline"]
    assert (root / "responses/SEC-01.json").read_bytes() == original


def test_corpus_digest_rejects_symlinked_preimage_directory(tmp_path: Path) -> None:
    root = tmp_path / "dataset"
    case_dir = root / "cases/SEC-01"
    case_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "README.md").write_text("outside")
    (case_dir / "base").symlink_to(outside, target_is_directory=True)
    (case_dir / "case.json").write_text("{}")
    (case_dir / "diff.patch").write_text("patch")

    with pytest.raises(DigestInputError, match="symlink pre-image directory"):
        corpus_input_paths(root, [(case_dir, {"patch_path": "diff.patch", "base_path": "base"})])


@pytest.mark.parametrize(
    "changed",
    [
        "app/common/application/languages.py",
        "review/schemas/review-output.schema.json",
        "review/schemas/review-output.schema.v2.json",
    ],
)
def test_replay_warns_on_request_shape_or_output_schema_edit(tmp_path: Path, changed: str) -> None:
    root = fixture_root(tmp_path)
    repo = tmp_path / "repo"
    _record_with_digests(root, repo)
    assert replay(root, prompt_root=repo)["warnings"] == []
    target = repo / changed
    target.write_bytes(target.read_bytes() + b"changed\n")

    assert replay(root, prompt_root=repo)["warnings"] == [
        "Static input digest mismatch; refresh the complete baseline"
    ]


@pytest.mark.parametrize(
    "changed",
    [
        "app/modules/reviews/application/prompt_builder.py",
        "app/modules/reviews/application/prompt_budget.py",
        "app/modules/reviews/infrastructure/llm/gateway.py",
        "review/scripts/eval_live.py",
        "app/modules/reviews/infrastructure/llm/settings.py",
        "app/modules/reviews/infrastructure/llm/transport.py",
        "app/modules/reviews/application/review_path.py",
        "app/modules/reviews/application/rag_index.py",
        "app/modules/reviews/infrastructure/llm/answers.py",
    ],
)
def test_replay_warns_on_request_source_only_edit(tmp_path: Path, changed: str) -> None:
    root = fixture_root(tmp_path)
    repo = tmp_path / "repo"
    _record_with_digests(root, repo)
    response = root / "responses/SEC-01.json"
    original_response = response.read_bytes()
    assert replay(root, prompt_root=repo)["warnings"] == []

    target = repo / changed
    target.write_bytes(target.read_bytes() + b"changed\n")

    assert replay(root, prompt_root=repo)["warnings"] == [
        "Static input digest mismatch; refresh the complete baseline"
    ]
    assert response.read_bytes() == original_response


def test_replay_warns_on_new_rule_json_file(tmp_path: Path) -> None:
    root = fixture_root(tmp_path)
    repo = tmp_path / "repo"
    _record_with_digests(root, repo)
    assert replay(root, prompt_root=repo)["warnings"] == []
    (repo / "review/rules/new-rule.json").write_text('{"rules": []}\n')

    assert replay(root, prompt_root=repo)["warnings"] == [
        "Static input path set changed; refresh the complete baseline"
    ]


def test_replay_warns_on_path_and_recorded_content_drift(tmp_path: Path) -> None:
    root = fixture_root(tmp_path)
    repo = tmp_path / "repo"
    _, rule = _record_with_digests(root, repo)
    manifest_path = root / "responses/manifest.json"
    answer_path = root / "responses/SEC-01.json"
    original_manifest = manifest_path.read_bytes()
    original_answer = answer_path.read_bytes()
    assert replay(root, prompt_root=repo)["warnings"] == []

    (repo / "review/rules/new-rule.json").write_text('{"rules": []}\n')
    rule.write_text('{"rules": [{"name": "changed"}]}\n')

    assert replay(root, prompt_root=repo)["warnings"] == [
        "Static input path set changed; refresh the complete baseline",
        "Static input digest mismatch; refresh the complete baseline",
    ]
    assert manifest_path.read_bytes() == original_manifest
    assert answer_path.read_bytes() == original_answer


def test_replay_warns_when_manifest_omits_current_static_input(tmp_path: Path) -> None:
    root = fixture_root(tmp_path)
    repo = tmp_path / "repo"
    _record_with_digests(root, repo)
    manifest_path = root / "responses/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["static_inputs"].remove("app/common/application/languages.py")
    manifest["static_digest"] = digest_files(repo, manifest["static_inputs"])
    manifest_path.write_text(json.dumps(manifest))

    assert replay(root, prompt_root=repo)["warnings"] == [
        "Static input path set changed; refresh the complete baseline"
    ]


SIDECAR = "review/eval/pr-commit-messages.json"


def _pin_recorded_static_input(root: Path, repo: Path, relative: str, content: bytes) -> None:
    """Write a repository file and record it in the manifest's static inputs and digest."""
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    manifest_path = root / "responses/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["static_inputs"] = sorted({*manifest["static_inputs"], relative})
    manifest["static_digest"] = digest_files(repo, manifest["static_inputs"])
    manifest_path.write_text(json.dumps(manifest))


def _custom_rule_set() -> bytes:
    rules = json.loads((REPO_ROOT / "review/rules/default-backend.v1.json").read_text("utf-8"))
    rules["rules"][0]["name"] = "Custom Source Review"
    return json.dumps(rules).encode("utf-8")


@pytest.mark.parametrize("dropped", [SIDECAR, "test-prs-dataset/cases/RES-03/case.json"])
def test_replay_warns_when_recorded_json_pin_is_no_longer_produced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dropped: str
) -> None:
    root = fixture_root(tmp_path)
    repo = tmp_path / "repo"
    _record_with_digests(root, repo)
    _pin_recorded_static_input(root, repo, SIDECAR, (REPO_ROOT / SIDECAR).read_bytes())
    _pin_recorded_static_input(root, repo, "config/custom-backend.json", _custom_rule_set())
    manifest = json.loads((root / "responses/manifest.json").read_text())
    assert dropped in manifest["static_inputs"]
    assert "config/custom-backend.json" in manifest["static_inputs"]
    assert replay(root, prompt_root=repo)["warnings"] == []

    review_code = eval_provenance._REVIEW_CODE
    identity_paths = eval_provenance.canonical_identity_paths
    monkeypatch.setattr(
        eval_provenance, "_REVIEW_CODE", tuple(path for path in review_code if path != dropped)
    )
    monkeypatch.setattr(
        eval_provenance,
        "canonical_identity_paths",
        lambda repo_root: [path for path in identity_paths(repo_root) if path != dropped],
    )

    assert (repo / dropped).is_file()
    assert replay(root, prompt_root=repo)["warnings"] == [
        "Static input path set changed; refresh the complete baseline"
    ]


# eval_live --backend-rules/--frontend-rules accept any repository file holding a rules array.
@pytest.mark.parametrize("custom", ["config/custom-backend.json", "config/custom-backend.rules"])
def test_replay_keeps_recorded_custom_rule_without_path_set_warning(
    tmp_path: Path, custom: str
) -> None:
    root = fixture_root(tmp_path)
    repo = tmp_path / "repo"
    _record_with_digests(root, repo)
    _pin_recorded_static_input(root, repo, custom, _custom_rule_set())

    assert custom not in rule_json_paths(repo)
    assert custom in json.loads((root / "responses/manifest.json").read_text())["static_inputs"]
    assert replay(root, prompt_root=repo)["warnings"] == []


def test_recorded_rule_set_detection_accepts_every_file_the_recorder_loads(
    tmp_path: Path,
) -> None:
    # One direction only: _is_rule_set checks the top-level shape, not the rule items.
    for relative in (
        "review/rules/default-backend.v1.json",
        "review/rules/default-frontend.v1.json",
    ):
        assert eval_live_module._rules(REPO_ROOT / relative)
        assert eval_replay_module._is_rule_set(REPO_ROOT / relative)
    for relative in (
        SIDECAR,
        "test-prs-dataset/cases/RES-03/case.json",
        "review/rules/schema.json",
    ):
        with pytest.raises(eval_live_module.RecorderError, match="no rules array"):
            eval_live_module._rules(REPO_ROOT / relative)
        assert not eval_replay_module._is_rule_set(REPO_ROOT / relative)
    nested = tmp_path / "nested.json"
    nested.write_text("[" * 100_000 + "]" * 100_000, encoding="utf-8")
    assert not eval_replay_module._is_rule_set(nested)


def test_replay_rejects_unreferenced_response_file(tmp_path: Path) -> None:
    root = fixture_root(tmp_path)
    record(root, {"SEC-01": review_output()})
    (root / "responses/stray.json").write_text("{}")

    with pytest.raises(ReplayError, match="unreferenced response file"):
        replay(root)


def test_replay_rejects_duplicate_mapped_response_paths(tmp_path: Path) -> None:
    root = fixture_root(tmp_path, ("SEC-01", "CLEAN-01"))
    record(root, {"SEC-01": review_output(), "CLEAN-01": review_output()})
    manifest_path = root / "responses/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["responses"]["CLEAN-01"] = "responses/SEC-01.json"
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(ReplayError, match="must be responses/CLEAN-01.json"):
        replay(root)


def test_replay_rejects_symlinked_responses_directory_before_manifest_read(tmp_path: Path) -> None:
    root = fixture_root(tmp_path)
    record(root, {"SEC-01": review_output()})
    original = root / "responses"
    outside = tmp_path / "outside-responses"
    original.rename(outside)
    original.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ReplayError, match="responses directory cannot be a symlink"):
        replay(root)


@pytest.mark.parametrize(
    "wrong_path",
    ["responses/manifest.json", "responses/CLEAN-01.json"],
)
def test_replay_requires_canonical_response_filename(tmp_path: Path, wrong_path: str) -> None:
    root = fixture_root(tmp_path)
    record(root, {"SEC-01": review_output()})
    manifest_path = root / "responses/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["responses"]["SEC-01"] = wrong_path
    manifest_path.write_text(json.dumps(manifest))
    if wrong_path.endswith("CLEAN-01.json"):
        (root / wrong_path).write_text(json.dumps(review_output()))
    (root / "responses/SEC-01.json").unlink()

    with pytest.raises(ReplayError, match="must be responses/SEC-01.json"):
        replay(root)


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


# --- RAG captures (#146): replay re-derives the recorded evidence from the frozen snapshot ---


def record_rag_arm(
    tmp_path: Path, arm: str, *, cases: dict[str, tuple[str, str]] | None = None, **kwargs: Any
) -> tuple[Path, Path, dict[str, Any]]:
    """Record ``arm`` over fixture RAG cases with a fake transport (root, responses, manifest)."""
    root = rag_root(tmp_path, cases)
    responses = tmp_path / f"arm-{arm}"
    manifest = record_rag(
        root, valid_answers(len(cases or CASES)), arm, responses_dir=responses, **kwargs
    )
    return root, responses, manifest


def edit_manifest(responses: Path, edit: Callable[[dict[str, Any]], None]) -> None:
    path = responses / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    edit(manifest)
    path.write_text(json.dumps(manifest), encoding="utf-8")


@pytest.mark.parametrize("arm", ["none", "graph"])
def test_replay_of_a_recorded_rag_capture_reproduces_it_without_a_warning(
    tmp_path: Path, arm: str
) -> None:
    root, responses, manifest = record_rag_arm(tmp_path, arm)

    report = replay(root, responses_dir=responses)

    # The static digest of a RAG capture covers the RAG modules: no path-set warning.
    assert report["warnings"] == []
    assert report["corpus"] == "rag"
    assert report["provenance"]["run_metadata"]["rag_arm"] == arm
    assert report["provenance"]["run_metadata"] == manifest["run_metadata"]
    assert "RAG arm: " + arm in format_console(report).splitlines()


def test_replay_of_a_capture_whose_rag_slot_the_gateway_fitted_away_reproduces_it(
    tmp_path: Path,
) -> None:
    """The recorded prompt budget lets the replay redo the fit, so a fitted case replays too."""
    cases = {"RAG-01": ("spike", "reverse_edge")}
    root = rag_root(tmp_path, cases)
    window = window_without_rag(root, "RAG-01")
    responses = tmp_path / "fitted"
    manifest = record_rag(
        root, valid_answers(1), "graph", model_settings=window, responses_dir=responses
    )
    assert manifest["run_metadata"]["rag_evidence"]["RAG-01"]["trimmed"] is True

    report = replay(root, responses_dir=responses)

    assert report["warnings"] == []
    # A different budget is a different fit: the recorded evidence no longer reproduces.
    edit_manifest(
        responses,
        lambda value: value["run_metadata"]["effective_settings"]["primary"].update(
            input_budget_tokens=10**6
        ),
    )
    with pytest.raises(ReplayError, match=r"RAG-01 \(.*prompt_digest"):
        replay(root, responses_dir=responses)


def test_replay_needs_no_network_and_no_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, responses, _ = record_rag_arm(tmp_path, "graph")
    for name in list(os.environ):
        if name.startswith(("LLM_", "OPENAI_", "ANTHROPIC_")) or name.endswith("_API_KEY"):
            monkeypatch.delenv(name)

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("replay opened a connection")

    # asyncio's self-pipe needs socket construction; only reaching out is refused.
    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)

    assert replay(root, responses_dir=responses)["warnings"] == []


@pytest.mark.parametrize(
    ("field", "edit"),
    [
        ("prompt_digest", lambda item: item.update(prompt_digest="sha256-v1:" + "0" * 64)),
        (
            "non_rag_prompt_digest",
            lambda item: item.update(non_rag_prompt_digest="sha256-v1:" + "0" * 64),
        ),
        ("retrieved", lambda item: item["retrieved"][0].update(tokens=1)),
        ("retrieved", lambda item: item.update(retrieved=[])),
        ("offline_retrieved_ids", lambda item: item.update(offline_retrieved_ids=[])),
        ("trimmed", lambda item: item.update(trimmed=True)),
    ],
    ids=["prompt", "non-rag", "tokens", "dropped", "offline-ids", "trimmed"],
)
def test_a_tampered_evidence_record_fails_the_replay_naming_the_case(
    tmp_path: Path, field: str, edit: Callable[[dict[str, Any]], None]
) -> None:
    root, responses, _ = record_rag_arm(tmp_path, "graph")
    edit_manifest(responses, lambda value: edit(value["run_metadata"]["rag_evidence"]["RAG-02"]))

    with pytest.raises(ReplayError) as raised:
        replay(root, responses_dir=responses)

    message = str(raised.value)
    assert "RAG-02" in message
    assert "RAG-01" not in message
    assert field in message


def test_a_tampered_index_fails_the_replay_naming_the_case(tmp_path: Path) -> None:
    """The case validation every replay runs requires ``index.json`` to be what the builder
    renders from the case tree, so an edited index is refused before any evidence is compared."""
    root, responses, _ = record_rag_arm(tmp_path, "graph")
    index = root / "cases" / "RAG-01" / "index.json"
    document = json.loads(index.read_text(encoding="utf-8"))
    document["edges"] = [edge for edge in document["edges"] if edge["kind"] != "uses_imported"]
    index.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(ReplayError) as raised:
        replay(root, responses_dir=responses)

    message = str(raised.value)
    assert "RAG-01" in message
    assert "index.json" in message
    assert "RAG-02" not in message


def test_a_case_that_cannot_be_rebuilt_fails_the_replay_naming_the_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, responses, _ = record_rag_arm(tmp_path, "graph")
    original = eval_rag_context_module.build_arm_input

    async def build(case_dir: Path, arm: Any, *, review_model: str) -> Any:
        # The case validation builds with its own model name; the replay with the recorded one.
        if review_model == "primary-model":
            raise ValueError("the recorded model cannot be built for")
        return await original(case_dir, arm, review_model=review_model)

    monkeypatch.setattr(eval_rag_context_module, "build_arm_input", build)

    with pytest.raises(ReplayError, match="RAG case RAG-01: the offline build failed"):
        replay(root, responses_dir=responses)


def test_a_regenerated_snapshot_that_changes_no_answer_is_a_digest_warning(
    tmp_path: Path,
) -> None:
    """A case whose tree and index were regenerated together passes the validation; what the
    capture recorded then differs from the files by digest only."""
    root, responses, _ = record_rag_arm(tmp_path, "graph")
    case_dir = root / "cases" / "RAG-02"
    # A file no block shows: the prompt is the same, the snapshot is not.
    (case_dir / "base" / "zz_unused.py").write_text("UNUSED = 1\n", encoding="utf-8")
    (case_dir / "index.json").write_bytes(render_snapshot(build_case_snapshot(case_dir)))

    warnings = replay(root, responses_dir=responses)["warnings"]

    assert "Corpus input digest mismatch; refresh the complete baseline" in warnings
    assert "RAG index digest mismatch; refresh the complete baseline" in warnings


def test_a_tampered_pseudo_revision_or_missing_budget_fails_the_replay(tmp_path: Path) -> None:
    root, responses, _ = record_rag_arm(tmp_path, "none")
    original = (responses / "manifest.json").read_text(encoding="utf-8")

    edit_manifest(
        responses,
        lambda value: value["run_metadata"]["snapshot_pseudo_revisions"]["RAG-01"].update(
            base="0" * 40
        ),
    )
    with pytest.raises(ReplayError, match="snapshot_pseudo_revisions.*RAG-01"):
        replay(root, responses_dir=responses)

    (responses / "manifest.json").write_text(original, encoding="utf-8")
    edit_manifest(responses, lambda value: value["run_metadata"].pop("effective_settings"))
    with pytest.raises(ReplayError, match="input_budget_tokens"):
        replay(root, responses_dir=responses)


def test_a_case_without_evidence_must_be_a_nonpublishable_one(tmp_path: Path) -> None:
    root, responses, _ = record_rag_arm(tmp_path, "graph")

    edit_manifest(
        responses,
        lambda value: value["run_metadata"]["rag_evidence"].update({"RAG-01": None}),
    )

    with pytest.raises(ReplayError, match="evidence is missing for publishable cases: RAG-01"):
        replay(root, responses_dir=responses)


def test_a_capture_that_never_reached_the_provider_replays_as_nonpublishable(
    tmp_path: Path,
) -> None:
    cases = {"RAG-01": ("spike", "reverse_edge")}
    root = rag_root(tmp_path, cases)
    configured = rag_settings()
    nothing_fits = LlmSettings(
        replace(configured.primary, context_window=1_001, max_output_tokens=1_000), None
    )
    responses = tmp_path / "none-sent"
    record_rag(
        root, valid_answers(0), "graph", model_settings=nothing_fits, responses_dir=responses
    )

    report = replay(root, responses_dir=responses)

    assert report["warnings"] == [
        "Recorded capture is not publishable: provider, infrastructure or policy failure"
    ]


@pytest.mark.parametrize(
    "edit",
    [
        lambda metadata: metadata.pop("rag_evidence"),
        lambda metadata: metadata.update(rag_arm="vector"),
        lambda metadata: metadata.update(policy_arm="legacy"),
        lambda metadata: metadata.update(rag_index_digest="nope"),
        lambda metadata: metadata.update(latency_ms={}),
        lambda metadata: metadata["latency_ms"].update({"RAG-01": True}),
        lambda metadata: metadata["rag_evidence"].pop("RAG-01"),
        lambda metadata: metadata["rag_evidence"]["RAG-01"].pop("trimmed"),
        lambda metadata: metadata["rag_evidence"]["RAG-01"]["retrieved"][0].pop("tokens"),
        lambda metadata: metadata["snapshot_pseudo_revisions"].update({"RAG-01": {}}),
        lambda metadata: metadata["snapshot_pseudo_revisions"]["RAG-01"].update(extra="x"),
        lambda metadata: metadata["snapshot_pseudo_revisions"]["RAG-01"].update(base=1),
        lambda metadata: metadata["snapshot_pseudo_revisions"]["RAG-01"].update(head=None),
        lambda metadata: metadata["latency_ms"].update({"RAG-01": -1}),
        lambda metadata: metadata["latency_ms"].update({"RAG-01": 1.5}),
        lambda metadata: metadata["rag_evidence"]["RAG-01"].update(prompt_digest="x"),
        lambda metadata: metadata["rag_evidence"]["RAG-01"].update(prompt_digest=5),
        lambda metadata: metadata["rag_evidence"]["RAG-01"].update(non_rag_prompt_digest="x"),
        lambda metadata: metadata["rag_evidence"]["RAG-01"].update(trimmed=0),
        lambda metadata: metadata["rag_evidence"]["RAG-01"].update(fallback_called="no"),
        lambda metadata: metadata["rag_evidence"]["RAG-01"].update(repair_called=None),
        lambda metadata: metadata["rag_evidence"]["RAG-01"].update(offline_retrieved_ids="x"),
        lambda metadata: metadata["rag_evidence"]["RAG-01"].update(offline_retrieved_ids=[1]),
        lambda metadata: metadata["rag_evidence"]["RAG-01"].update(retrieved="x"),
        lambda metadata: metadata["rag_evidence"]["RAG-01"].update(retrieved=["x"]),
        lambda metadata: metadata["rag_evidence"].update({"RAG-01": []}),
        lambda metadata: metadata["rag_evidence"]["RAG-01"].update(extra=1),
    ],
    ids=[
        "partial-fields",
        "unknown-arm",
        "policy-arm",
        "index-digest",
        "latency-keys",
        "latency-bool",
        "evidence-keys",
        "evidence-fields",
        "block-fields",
        "revisions",
        "revisions-extra-key",
        "revisions-base-int",
        "revisions-head-null",
        "latency-negative",
        "latency-float",
        "prompt-digest-format",
        "prompt-digest-type",
        "non-rag-digest-format",
        "trimmed-int",
        "fallback-str",
        "repair-null",
        "offline-ids-str",
        "offline-ids-int",
        "retrieved-str",
        "retrieved-item-str",
        "evidence-list",
        "evidence-extra-field",
    ],
)
def test_malformed_rag_metadata_is_refused(tmp_path: Path, edit: Callable[[Any], None]) -> None:
    root, responses, _ = record_rag_arm(tmp_path, "graph")
    edit_manifest(responses, lambda value: edit(value["run_metadata"]))

    with pytest.raises(ReplayError, match="response manifest run_metadata"):
        replay(root, responses_dir=responses)


def test_a_rag_corpus_and_a_rag_capture_replay_only_together(tmp_path: Path) -> None:
    root, responses, _ = record_rag_arm(tmp_path, "graph")

    # A RAG corpus with a capture that has no RAG arm.
    def strip_rag(value: dict[str, Any]) -> None:
        for name in ("rag_arm", "rag_index_digest", "snapshot_pseudo_revisions", "latency_ms"):
            del value["run_metadata"][name]
        del value["run_metadata"]["rag_evidence"]

    edit_manifest(responses, strip_rag)
    with pytest.raises(ReplayError, match="need a capture recorded with a RAG arm"):
        replay(root, responses_dir=responses)

    # A RAG capture over a corpus with another case in it.
    root, responses, _ = record_rag_arm(tmp_path / "again", "graph")
    shutil.copytree(DATASET_ROOT / "cases" / "SEC-01", root / "cases" / "SEC-01")
    with pytest.raises(ReplayError, match="needs a corpus of RAG cases only, not: SEC-01"):
        replay(root, responses_dir=responses)


def test_importing_the_replay_loads_no_rag_stack() -> None:
    code = (
        "import sys, review.scripts.eval_replay;"
        "bad = [m for m in sys.modules if m.startswith(('tree_sitter', 'review.scripts.eval_rag', "
        "'review.scripts.rag_snapshot'))];"
        "assert not bad, bad"
    )
    subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, check=True)


def test_the_labels_of_the_two_rag_arms_are_the_arms_the_recorder_knows() -> None:
    assert set(eval_replay_module.RAG_ARM_LABELS) == set(eval_live_module.RAG_ARMS)


def test_replaying_the_main_baseline_asks_for_no_rag_code_and_adds_no_rag_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A capture without ``rag_arm`` replays as it did before #146: the main static path set,
    the main corpus label, and none of the RAG keys in its report."""
    asked: list[Any] = []
    original = static_input_paths

    def spy(*args: Any, **kwargs: Any) -> list[str]:
        asked.append(kwargs.get("rag_code", False))
        return original(*args, **kwargs)

    monkeypatch.setattr(eval_replay_module, "static_input_paths", spy)

    report = replay(DATASET_ROOT)

    assert asked == [False]
    assert report["corpus"] == "main"
    assert set(report["provenance"]["run_metadata"]) & set(eval_replay_module._RAG_KEYS) == set()
    # The static digest may drift (the warning says so), nothing else is a warning.
    assert set(report["warnings"]) <= {
        "Static input digest mismatch; refresh the complete baseline"
    }


@pytest.mark.parametrize("budget", [0, -5, 1.5, "100", True, None])
def test_a_recorded_prompt_budget_that_is_not_a_positive_integer_is_refused(
    tmp_path: Path, budget: object
) -> None:
    root, responses, _ = record_rag_arm(tmp_path, "graph")
    edit_manifest(
        responses,
        lambda value: value["run_metadata"]["effective_settings"]["primary"].update(
            input_budget_tokens=budget
        ),
    )

    with pytest.raises(ReplayError, match="input_budget_tokens"):
        replay(root, responses_dir=responses)


@pytest.mark.parametrize(
    "error",
    [
        ValueError("a value"),
        OSError("a file"),
        PolicyError(PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR, "a pin"),
    ],
    ids=["value", "os", "policy"],
)
def test_a_rebuild_that_raises_is_a_replay_error_naming_the_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    root, responses, _ = record_rag_arm(tmp_path, "graph")
    original = eval_rag_context_module.build_arm_input

    async def build(case_dir: Path, arm: Any, *, review_model: str) -> Any:
        if review_model == "primary-model":
            raise error
        return await original(case_dir, arm, review_model=review_model)

    monkeypatch.setattr(eval_rag_context_module, "build_arm_input", build)

    with pytest.raises(ReplayError, match="RAG case RAG-01: the offline build failed"):
        replay(root, responses_dir=responses)
