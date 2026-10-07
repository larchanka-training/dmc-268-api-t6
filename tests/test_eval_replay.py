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

import review.scripts.eval_replay as eval_replay_module
from review.scripts import eval_provenance
from review.scripts.eval_provenance import (
    DigestInputError,
    corpus_input_paths,
    digest_files,
    rule_json_paths,
    static_input_paths,
)
from review.scripts.eval_replay import ReplayError, format_console, nonpublishable_case_ids, replay

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
    static_paths = sorted(
        [
            "review/prompts/review.system.v1.md",
            "review/rules/default-backend.v1.json",
            "review/rules/schema.json",
            "review/schemas/review-output.schema.json",
            "app/common/application/languages.py",
            "app/bootstrap/llm_gateway.py",
            "app/modules/reviews/application/llm.py",
            "app/modules/reviews/application/prompt_builder.py",
            "app/modules/reviews/application/prompt_budget.py",
            "app/modules/reviews/application/review_output.py",
            "app/modules/reviews/application/run_failures.py",
            "app/modules/reviews/infrastructure/llm/answers.py",
            "app/modules/reviews/infrastructure/llm/gateway.py",
            "app/modules/reviews/infrastructure/llm/models.py",
            "app/modules/reviews/infrastructure/llm/settings.py",
            "app/modules/reviews/infrastructure/llm/transport.py",
            "review/scripts/eval_live.py",
        ]
    )
    for relative in static_paths:
        path = repo / relative
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
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
        "review/rules/schema.json",
    ],
)
def test_replay_warns_on_request_shape_or_rule_schema_edit(tmp_path: Path, changed: str) -> None:
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
