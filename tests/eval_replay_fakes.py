"""Shared fixed inputs for offline replay tests."""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from review.scripts.eval_provenance import (
    corpus_input_paths,
    digest_files,
    rule_json_paths,
    static_input_paths,
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


def edit_manifest(responses: Path, edit: Callable[[dict[str, Any]], None]) -> None:
    path = responses / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    edit(manifest)
    path.write_text(json.dumps(manifest), encoding="utf-8")
