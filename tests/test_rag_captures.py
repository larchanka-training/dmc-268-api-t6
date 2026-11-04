"""The six RAG captures of #146 (``test-prs-dataset/rag/responses``) are all there and labelled.

Three runs of arm A (``rag_arm`` ``none``) and three of arm B (``graph``), all recorded by the
paid ``eval-live`` workflow on one model, one prompt and one corpus. The CI step "RAG sub-corpus"
replays every one of them offline; this file is the fast local signal that a capture was not
dropped, relabelled or mixed in from another run. A replay takes about 40 seconds of CPU (it
revalidates the 24 cases), so it is left to that CI step and not repeated in the unit job.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from review.scripts.eval_ab import SAME_RUN_METADATA

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "test-prs-dataset" / "rag"
CAPTURES = CORPUS / "responses"
EXPECTED = {
    f"{arm}-run{run}": rag_arm
    for run in (1, 2, 3)
    for arm, rag_arm in (("A", "none"), ("B", "graph"))
}
SHARED = ("model_id", "prompt_sha", "prompt_version", "corpus_digest")


def _case_ids() -> set[str]:
    return {path.name for path in (CORPUS / "cases").iterdir() if path.is_dir()}


def _manifest(directory: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    return loaded


def _file_problems(directory: Path, manifest: dict[str, Any]) -> list[str]:
    """The directory holds the manifest and its answers only, each a bare findings+summary."""
    mapped = {Path(raw).name for raw in manifest["responses"].values()}
    on_disk = {path.name for path in directory.iterdir()}
    if on_disk != mapped | {"manifest.json"}:
        return [f"{directory.name}: files on disk differ from the manifest and its answers"]
    problems = []
    for name in sorted(mapped):
        answer = json.loads((directory / name).read_text(encoding="utf-8"))
        if not isinstance(answer, dict) or set(answer) != {"findings", "summary"}:
            problems.append(f"{directory.name}: {name} is not exactly findings and summary")
    return problems


def _problems(captures: Path, case_ids: set[str]) -> list[str]:
    """Every way the capture set differs from the six labelled, publishable, comparable runs."""
    found = (
        {path.name for path in captures.iterdir() if path.is_dir()} if captures.is_dir() else set()
    )
    problems = (
        [f"capture set is {sorted(found)}, not {sorted(EXPECTED)}"]
        if found != set(EXPECTED)
        else []
    )
    manifests = {name: _manifest(captures / name) for name in sorted(found & set(EXPECTED))}
    for name, manifest in manifests.items():
        metadata = manifest["run_metadata"]
        if metadata["rag_arm"] != EXPECTED[name]:
            problems.append(f"{name}: rag_arm {metadata['rag_arm']!r}, not {EXPECTED[name]!r}")
        if set(manifest["responses"]) != case_ids:
            problems.append(f"{name}: responses do not map exactly the corpus cases")
        if metadata["baseline_publishable"] is not True:
            problems.append(f"{name}: not baseline_publishable")
        problems.extend(_file_problems(captures / name, manifest))
    for key in SHARED:
        values = {json.dumps(manifest[key]) for manifest in manifests.values()}
        if len(values) > 1:
            problems.append(f"captures disagree on {key}")
    for name in SAME_RUN_METADATA:
        values = {json.dumps(manifest["run_metadata"][name]) for manifest in manifests.values()}
        if len(values) > 1:
            problems.append(f"captures disagree on run_metadata.{name}")
    prompt_versions = {manifest["prompt_version"] for manifest in manifests.values()}
    if prompt_versions - {"v5"}:
        problems.append(f"prompt_version {sorted(prompt_versions)}, not v5")
    return problems


def test_six_labelled_publishable_captures_share_one_model_prompt_and_corpus() -> None:
    assert _problems(CAPTURES, _case_ids()) == []
    assert len(_case_ids()) == 24


def _rewrite(directory: Path, mutate: Callable[[dict[str, Any]], object]) -> None:
    manifest = _manifest(directory)
    mutate(manifest)
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _set_all(captures: Path, **fields: object) -> None:
    for name in EXPECTED:
        _rewrite(captures / name, lambda manifest: manifest.update(fields))


def _add_key(captures: Path) -> None:
    path = captures / "A-run1" / "RAG-01.json"
    path.write_text(json.dumps({**json.loads(path.read_text("utf-8")), "raw": {}}), "utf-8")


MUTATIONS: list[tuple[str, Callable[[Path], object], str]] = [
    ("dropped capture", lambda c: shutil.rmtree(c / "B-run3"), "capture set is"),
    (
        "swapped arm",
        lambda c: _rewrite(c / "A-run2", lambda m: m["run_metadata"].update(rag_arm="graph")),
        "A-run2: rag_arm 'graph', not 'none'",
    ),
    (
        "case dropped from manifest",
        lambda c: _rewrite(c / "B-run1", lambda m: m["responses"].pop("RAG-24")),
        "B-run1: responses do not map exactly",
    ),
    (
        "unpublishable",
        lambda c: _rewrite(
            c / "A-run1", lambda m: m["run_metadata"].update(baseline_publishable=False)
        ),
        "A-run1: not baseline_publishable",
    ),
    (
        "other prompt",
        lambda c: _rewrite(c / "A-run3", lambda m: m.update(prompt_sha="0" * 64)),
        "disagree on prompt_sha",
    ),
    (
        "other model",
        lambda c: _rewrite(c / "B-run2", lambda m: m.update(model_id="other-model")),
        "disagree on model_id",
    ),
    (
        "other corpus",
        lambda c: _rewrite(c / "B-run2", lambda m: m.update(corpus_digest="sha256-v1:" + "0" * 64)),
        "disagree on corpus_digest",
    ),
    (
        "other engine",
        lambda c: _rewrite(c / "B-run1", lambda m: m["run_metadata"].update(engine="deep")),
        "disagree on run_metadata.engine",
    ),
    (
        "other effective settings",
        lambda c: _rewrite(
            c / "A-run2", lambda m: m["run_metadata"].update(effective_settings={"policy": {}})
        ),
        "disagree on run_metadata.effective_settings",
    ),
    (
        "other fallback model",
        lambda c: _rewrite(
            c / "B-run3", lambda m: m["run_metadata"].update(fallback_model_id="other-fallback")
        ),
        "disagree on run_metadata.fallback_model_id",
    ),
    ("prompt not v5 everywhere", lambda c: _set_all(c, prompt_version="v4"), "not v5"),
    ("extra file", lambda c: (c / "A-run1" / "notes.txt").write_text("x"), "A-run1: files on disk"),
    ("missing answer", lambda c: (c / "B-run2" / "RAG-07.json").unlink(), "B-run2: files on disk"),
    ("answer with an extra key", _add_key, "A-run1: RAG-01.json is not exactly"),
]


@pytest.mark.parametrize(
    ("mutate", "fragment"), [row[1:] for row in MUTATIONS], ids=[row[0] for row in MUTATIONS]
)
def test_a_missing_or_mislabelled_capture_is_reported(
    tmp_path: Path, mutate: Callable[[Path], object], fragment: str
) -> None:
    copy = tmp_path / "responses"
    shutil.copytree(CAPTURES, copy)
    mutate(copy)

    assert any(fragment in problem for problem in _problems(copy, _case_ids()))
