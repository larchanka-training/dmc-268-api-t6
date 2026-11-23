"""Integration tests for eval_replay post-patch reconstruction and suggestion metrics (#446)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from review.scripts.eval_provenance import (
    corpus_input_paths,
    digest_files,
    rule_json_paths,
    static_input_paths,
)
from review.scripts.eval_replay import (
    format_console,
    reconstruct_head_file_contents,
    replay,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_eval_replay_reconstructs_post_patch_files_and_scores_suggestions(tmp_path: Path) -> None:
    """replay() applies diff.patch (shifted lines + added files) before running AST validation."""
    case_dir = tmp_path / "dataset" / "cases" / "SEC-01"
    base_src = case_dir / "base" / "src"
    base_src.mkdir(parents=True)
    (base_src / "calc.ts").write_text(
        "export function calc(x: number): number {\n  return x;\n}\n",
        encoding="utf-8",
    )

    work_old = tmp_path / "pair" / "old"
    work_new = tmp_path / "pair" / "new"
    (work_old / "src").mkdir(parents=True)
    (work_new / "src").mkdir(parents=True)
    (work_old / "src" / "calc.ts").write_bytes((base_src / "calc.ts").read_bytes())
    (work_new / "src" / "calc.ts").write_text(
        "// shifted line 1\n// shifted line 2\n"
        "export function calc(x: number): number {\n  return x;\n}\n",
        encoding="utf-8",
    )
    (work_new / "src" / "added.ts").write_text(
        "export function helper(): number {\n  return 1;\n}\n",
        encoding="utf-8",
    )
    git_diff = subprocess.run(
        [
            "git",
            "-c",
            "core.autocrlf=false",
            "diff",
            "--no-index",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            "old",
            "new",
        ],
        cwd=tmp_path / "pair",
        capture_output=True,
        check=False,
    )
    assert git_diff.returncode == 1
    patch_text = git_diff.stdout.decode("utf-8").replace("a/old/", "a/").replace("b/new/", "b/")
    (case_dir / "diff.patch").write_bytes(patch_text.encode("utf-8"))

    record_data = {
        "id": "SEC-01",
        "class": "security",
        "language": "typescript",
        "source": {"kind": "synthetic", "attribution": "eval replay suggestion test"},
        "base_path": "base",
        "patch_path": "diff.patch",
        "expected_verdict": "blocking",
        "expected_findings": [
            {
                "path": "src/calc.ts",
                "start_line": None,
                "line": 1,
                "severity": "critical",
                "category": "security",
            }
        ],
    }
    (case_dir / "case.json").write_text(json.dumps(record_data), encoding="utf-8")

    root = tmp_path / "dataset"
    (root / "responses").mkdir()
    response_body = {
        "findings": [
            {
                # Post-patch line 4 is `  return x;` (valid replacement)
                "path": "src/calc.ts",
                "line": 4,
                "start_line": None,
                "severity": "critical",
                "category": "security",
                "title": "Validate input before returning",
                "body": "Checks shifted line numbers against patched calc.ts.",
                "suggestion": "  return Math.max(0, x);",
                "confidence": 0.9,
                "rule_name": None,
            },
            {
                # New added file line 2 is `  return 1;` (valid replacement)
                "path": "src/added.ts",
                "line": 2,
                "start_line": None,
                "severity": "low",
                "category": "readability",
                "title": "Return explicit value",
                "body": "Checks newly added file without a pre-image in base/.",
                "suggestion": "  return 42;",
                "confidence": 0.8,
                "rule_name": None,
            },
            {
                # Broken syntax suggestion inside calc.ts body
                "path": "src/calc.ts",
                "line": 4,
                "start_line": None,
                "severity": "low",
                "category": "readability",
                "title": "Broken syntax suggestion",
                "body": "Inserts unclosed statement.",
                "suggestion": "  return (;",
                "confidence": 0.7,
                "rule_name": None,
            },
            {
                # Unchecked unsupported file extension must stay outside denominator
                "path": "README.md",
                "line": 1,
                "start_line": None,
                "severity": "info",
                "category": "readability",
                "title": "Unchecked extension",
                "body": "Non-source path should be unchecked.",
                "suggestion": "# Updated title",
                "confidence": 0.5,
                "rule_name": None,
            },
        ],
        "summary": {"problem": "One defect.", "done_well": "Clear helpers.", "effort": "small"},
    }
    (root / "responses" / "SEC-01.json").write_text(json.dumps(response_body), encoding="utf-8")

    static_inputs = static_input_paths(
        "review/prompts/review.system.v1.md", rule_json_paths(_REPO_ROOT)
    )
    records = [(case_dir, record_data)]
    manifest = {
        "schema_version": 1,
        "model_id": "test-model",
        "prompt_path": "review/prompts/review.system.v1.md",
        "prompt_sha": digest_files(_REPO_ROOT, ["review/prompts/review.system.v1.md"]).removeprefix(
            "sha256-v1:"
        ),
        "prompt_version": "v1",
        "static_inputs": static_inputs,
        "static_digest": digest_files(_REPO_ROOT, static_inputs),
        "corpus_digest": digest_files(root, corpus_input_paths(root, records)),
        "run_metadata": {
            "recorded_at": "2026-09-29T00:00:00Z",
            "baseline_publishable": True,
            "nonpublishable_case_ids": [],
            "cases": {
                "SEC-01": {
                    "first_call": "answer",
                    "gateway_status": "accepted",
                    "paid_metadata_error": False,
                }
            },
        },
        "responses": {"SEC-01": "responses/SEC-01.json"},
    }
    (root / "responses" / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    report = replay(root)
    assert report["suggestions"] == {
        "total": 3,
        "valid": 2,
        "unchecked": 1,
        "share": pytest.approx(2 / 3),
    }

    # Verify format_console prints suggestions valid in strict and normalized
    console = format_console(report)
    console_lines = console.splitlines()
    assert any(line == "Suggestions valid: 66.7% (2/3, unchecked=1)" for line in console_lines)
    assert any(line == "  Suggestions valid: 66.7% (2/3, unchecked=1)" for line in console_lines)


def test_reconstruct_head_file_contents_sec04_apply_directory() -> None:
    """Explicitly verify apply_directory (.upstream) reconstruction on committed SEC-04 case."""
    sec04_dir = _REPO_ROOT / "test-prs-dataset" / "cases" / "SEC-04"
    sec04_record = json.loads((sec04_dir / "case.json").read_text(encoding="utf-8"))
    sec04_files = reconstruct_head_file_contents(sec04_dir, sec04_record)
    assert "src/flask/sessions.py" in sec04_files
    assert b"SECRET_KEY_FALLBACKS" in sec04_files["src/flask/sessions.py"]


def test_reconstruct_head_file_contents_git_apply_failure_records_warning(tmp_path: Path) -> None:
    """When git apply fails, an informative warning is recorded and empty dict is returned."""
    case_dir = tmp_path / "case"
    base = case_dir / "base"
    base.mkdir(parents=True)
    (base / "file.txt").write_text("line 1\nline 2\n", encoding="utf-8")
    bad_patch = (
        "--- a/file.txt\n+++ b/file.txt\n"
        "@@ -1,2 +1,2 @@\n-wrong line\n line 2\n+replacement\n line 2\n"
    )
    (case_dir / "diff.patch").write_text(bad_patch, encoding="utf-8")
    record = {
        "id": "FAIL-01",
        "base_path": "base",
        "patch_path": "diff.patch",
    }
    warnings: list[str] = []
    files = reconstruct_head_file_contents(case_dir, record, warnings=warnings)
    assert files == {}
    assert len(warnings) == 1
    assert warnings[0].startswith("case FAIL-01: ")
    assert "file.txt" in warnings[0]


def test_reconstruct_head_missing_base_or_patch_warning(tmp_path: Path) -> None:
    """When base_path or patch_path does not exist on disk, a warning is recorded."""
    case_dir = tmp_path / "case"
    case_dir.mkdir(parents=True)
    record = {
        "id": "MISSING-01",
        "base_path": "missing_base",
        "patch_path": "missing.patch",
    }
    warnings: list[str] = []
    files = reconstruct_head_file_contents(case_dir, record, warnings=warnings)
    assert files == {}
    assert len(warnings) == 1
    assert "case MISSING-01: base directory or patch file missing" in warnings[0]


def test_reconstruct_head_non_string_base_or_patch_path_warning(tmp_path: Path) -> None:
    """When base_path or patch_path is not a string, a warning is recorded."""
    case_dir = tmp_path / "case"
    case_dir.mkdir(parents=True)
    record = {
        "id": "NONSTR-01",
        "base_path": 123,
        "patch_path": None,
    }
    warnings: list[str] = []
    files = reconstruct_head_file_contents(case_dir, record, warnings=warnings)
    assert files == {}
    assert warnings == ["case NONSTR-01: base_path or patch_path missing"]


def test_reconstruct_head_missing_apply_directory_warning(tmp_path: Path) -> None:
    """When apply_directory is specified but absent after patch, a warning is recorded."""
    case_dir = tmp_path / "case"
    base = case_dir / "base"
    base.mkdir(parents=True)
    (base / "file.txt").write_text("hello\n", encoding="utf-8")
    (case_dir / "diff.patch").write_text(
        "--- a/file.txt\n+++ b/file.txt\n@@ -1 +1 @@\n-hello\n+world\n", encoding="utf-8"
    )
    record = {
        "id": "NO-DIR-01",
        "base_path": "base",
        "patch_path": "diff.patch",
        "apply_directory": "nonexistent_subdir",
    }
    warnings: list[str] = []
    files = reconstruct_head_file_contents(case_dir, record, warnings=warnings)
    assert files == {}
    assert len(warnings) == 1
    assert warnings[0].startswith("case NO-DIR-01: ")
    assert "nonexistent_subdir/file.txt" in warnings[0]
