"""Commit context capture provenance and immutable offline replay (#82)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from review.scripts.eval_replay import replay
from tests.test_eval_live import (
    REPO_ROOT,
    VALID,
    FakeTransport,
    answer,
    commit_context_repo,
    fixture_root,
    record_with_repo,
)

SOURCE = "app/modules/reviews/infrastructure/github_run_source.py"
SIDECAR = "review/eval/pr-commit-messages.json"


def capture_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    payload = json.loads((REPO_ROOT / SIDECAR).read_text(encoding="utf-8"))
    repo = commit_context_repo(tmp_path, monkeypatch, payload)
    # Copy independently of the pin list, so a missing provenance pin is observable.
    source = repo / SOURCE
    source.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO_ROOT / SOURCE, source)
    return repo


@pytest.mark.parametrize("changed", [SOURCE, SIDECAR])
def test_each_commit_shaping_input_causes_replay_drift_without_rewriting_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed: str,
) -> None:
    old_manifest = (REPO_ROOT / "test-prs-dataset/responses/manifest.json").read_bytes()
    repo = capture_repo(tmp_path, monkeypatch)
    root = fixture_root(tmp_path, ("CLEAN-03", "SEC-01"))
    transport = FakeTransport([answer("primary-model", VALID)] * 2)
    manifest = record_with_repo(root, repo, transport)
    before = {path.name: path.read_bytes() for path in (root / "responses").iterdir()}
    assert replay(root, prompt_root=repo)["warnings"] == []
    assert "<commit>refactor(core): extract react.ts from index.ts</commit>" in (
        transport.requests[0].messages[1].content
    )

    target = repo / changed
    if changed == SOURCE:
        target.write_bytes(target.read_bytes() + b"\n# Changed retrieval contract.\n")
    else:
        payload = json.loads(target.read_text(encoding="utf-8"))
        payload["records"][0]["message"] += " changed snapshot"
        target.write_text(json.dumps(payload), encoding="utf-8")

    report = replay(root, prompt_root=repo)

    assert report["warnings"] == ["Static input digest mismatch; refresh the complete baseline"]
    assert SOURCE in manifest["static_inputs"]
    assert SIDECAR in manifest["static_inputs"]
    assert "review/scripts/eval_live.py" in manifest["static_inputs"]
    assert {path.name: path.read_bytes() for path in (root / "responses").iterdir()} == before
    assert (REPO_ROOT / "test-prs-dataset/responses/manifest.json").read_bytes() == old_manifest


def test_populated_context_capture_and_two_json_replay_reports_are_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = capture_repo(tmp_path, monkeypatch)
    roots: list[Path] = []
    transcripts: list[list[str]] = []
    for name in ("first", "second"):
        root = fixture_root(tmp_path / name, ("CLEAN-03", "LOG-03", "SEC-01"))
        transport = FakeTransport([answer("primary-model", VALID)] * 3)
        record_with_repo(root, repo, transport)
        roots.append(root)
        transcripts.append([request.messages[1].content for request in transport.requests])
    assert transcripts[0] == transcripts[1]
    assert "<commit>refactor(core): extract react.ts from index.ts</commit>" in transcripts[0][0]
    assert "<commit>fix(server): reassemble COTP fragments" in transcripts[0][1]
    assert "   in the Connection Confirm, an…</commit>" in transcripts[0][1]
    assert "<commit_messages>" not in transcripts[0][2]
    assert (roots[0] / "responses/manifest.json").read_bytes() == (
        roots[1] / "responses/manifest.json"
    ).read_bytes()

    reports: list[Path] = []
    for index, root in enumerate(roots, start=1):
        report = replay(root, prompt_root=repo)
        assert report["warnings"] == []
        assert report["case_count"] == 3
        assert report["validity"] == 1.0
        path = tmp_path / f"report-{index}.json"
        path.write_text(
            json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        reports.append(path)
    assert reports[0].read_bytes() == reports[1].read_bytes()
