"""Commit context capture provenance and immutable offline replay (#82)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import review.scripts.commit_context as commit_context_module
from review.scripts.commit_context import (
    CommitContextError,
    CommitContextSidecar,
    parse_commit_context,
)
from review.scripts.eval_live import RecorderError
from review.scripts.eval_provenance import rule_json_paths, static_input_paths
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

LIMITS = "app/modules/reviews/application/commit_prefix_limits.py"
SIDECAR = "review/eval/pr-commit-messages.json"
SHAPING_INPUTS = (
    LIMITS,
    SIDECAR,
    "review/scripts/commit_context.py",
    "app/common/infrastructure/github_link_header.py",
    "test-prs-dataset/cases/SYN-04/case.json",
)


def capture_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    payload = json.loads((REPO_ROOT / SIDECAR).read_text(encoding="utf-8"))
    repo = commit_context_repo(tmp_path, monkeypatch, payload)
    # Copy independently of the pin list, so missing pins are observable.
    for relative in SHAPING_INPUTS:
        source = repo / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / relative, source)
    return repo


@pytest.mark.parametrize("changed", SHAPING_INPUTS)
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
    if changed.endswith(".py"):
        target.write_bytes(target.read_bytes() + b"\n# Changed retrieval contract.\n")
    else:
        payload = json.loads(target.read_text(encoding="utf-8"))
        if changed == SIDECAR:
            payload["records"][0]["pages"][0]["entries"][0]["message"] += " changed prefix"
        else:
            payload["source"]["revision"] = "f" * 40
        target.write_text(json.dumps(payload), encoding="utf-8")

    report = replay(root, prompt_root=repo)

    assert report["warnings"] == ["Static input digest mismatch; refresh the complete baseline"]
    assert changed in manifest["static_inputs"]
    assert LIMITS in manifest["static_inputs"]
    assert SIDECAR in manifest["static_inputs"]
    assert "review/scripts/eval_live.py" in manifest["static_inputs"]
    assert {path.name: path.read_bytes() for path in (root / "responses").iterdir()} == before
    assert (REPO_ROOT / "test-prs-dataset/responses/manifest.json").read_bytes() == old_manifest


def test_inputs_that_do_not_shape_the_eval_are_not_static_inputs() -> None:
    # Does not shape the eval: not imported, or imported without shaping what it sends or
    # parses; edits to these must not change the static digest (#177).
    paths = static_input_paths("review/prompts/review.system.v2.md", rule_json_paths(REPO_ROOT))

    assert "app/modules/reviews/infrastructure/github_vcs.py" not in paths
    assert "app/modules/reviews/infrastructure/github_run_source.py" not in paths
    assert "app/modules/reviews/infrastructure/vcs_failures.py" not in paths
    # Imported by eval only through llm_gateway; shapes nothing it sends or parses.
    assert "app/modules/reviews/application/vcs_diff.py" not in paths


def sidecar_with_encodings(*, accept_encoding: str, content_encoding: str | None) -> str:
    payload: dict[str, Any] = json.loads((REPO_ROOT / SIDECAR).read_text(encoding="utf-8"))
    payload["retrieval"]["accept_encoding"] = accept_encoding
    payload["records"][0]["pages"][0]["content_encoding"] = content_encoding
    return json.dumps(payload)


def test_real_sidecar_is_rejected_once_the_production_encoding_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every real page has content_encoding null, so only the retrieval check can reject it.
    monkeypatch.setattr(commit_context_module, "COMMIT_ACCEPT_ENCODING", "gzip")

    with pytest.raises(ValidationError, match="unexpected accept encoding"):
        CommitContextSidecar.model_validate_json((REPO_ROOT / SIDECAR).read_bytes())
    with pytest.raises(CommitContextError, match="malformed commit context sidecar"):
        parse_commit_context(REPO_ROOT / SIDECAR)

    repo = commit_context_repo(
        tmp_path, monkeypatch, json.loads((REPO_ROOT / SIDECAR).read_text(encoding="utf-8"))
    )
    root = fixture_root(tmp_path, ("CLEAN-03",))
    transport = FakeTransport([answer("primary-model", VALID)])
    with pytest.raises(RecorderError, match="commit context"):
        record_with_repo(root, repo, transport)
    assert transport.requests == []


@pytest.mark.parametrize(
    ("constant", "content_encoding", "rejection"),
    [
        ("identity", "identity", None),
        ("gzip", "gzip", None),
        ("gzip", "identity", "unexpected content encoding"),
    ],
)
def test_page_content_encoding_is_compared_with_the_constant_value(
    monkeypatch: pytest.MonkeyPatch,
    constant: str,
    content_encoding: str,
    rejection: str | None,
) -> None:
    # The retrieval value follows the constant, so only the page check decides each case.
    monkeypatch.setattr(commit_context_module, "COMMIT_ACCEPT_ENCODING", constant)
    document = sidecar_with_encodings(accept_encoding=constant, content_encoding=content_encoding)

    if rejection is None:
        sidecar = CommitContextSidecar.model_validate_json(document)
        assert sidecar.records[0].pages[0].content_encoding == content_encoding
    else:
        with pytest.raises(ValidationError, match=rejection):
            CommitContextSidecar.model_validate_json(document)


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
