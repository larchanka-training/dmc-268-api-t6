"""The six RAG captures of #146 (``test-prs-dataset/rag/responses``) are all there and labelled.

Three runs of arm A (``rag_arm`` ``none``) and three of arm B (``graph``), all recorded by the
paid ``eval-live`` workflow on one model, one prompt and one corpus. The CI step "RAG sub-corpus"
replays every one of them offline; this file is the fast local signal that a capture was not
dropped, relabelled or mixed in from another run. A replay takes about 40 seconds of CPU (it
revalidates the 24 cases), so it is left to that CI step and not repeated in the unit job. The
user messages of the hallucination oracle (``oracle/context``) and the v5 system prompt are
bound here to the ``prompt_digest`` recorded for each answer.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from review.scripts.eval_ab import SAME_PROVENANCE, SAME_RUN_METADATA
from review.scripts.eval_live import _prompt_digest
from review.scripts.validate_findings import validate_review_output_v2

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "test-prs-dataset" / "rag"
CAPTURES = CORPUS / "responses"
PROMPTS = CORPUS / "oracle" / "context"
SYSTEM_PROMPT = ROOT / "review" / "prompts" / "review.system.v5.md"  # the captures record v5
EXPECTED = {
    f"{arm}-run{run}": rag_arm
    for run in (1, 2, 3)
    for arm, rag_arm in (("A", "none"), ("B", "graph"))
}
SHARED = (*SAME_PROVENANCE, "prompt_sha")


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
        "other static inputs",
        lambda c: _rewrite(c / "A-run1", lambda m: m.update(static_digest="sha256-v1:" + "0" * 64)),
        "disagree on static_digest",
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


def _prompt_files(captures: Path) -> dict[str, str]:
    """File name -> recorded digest of the prompt of every (run, case) with findings to review.

    The gate reviews the findings of the valid answers only; an answer the validator refuses
    is left out, as it was for the tech lead.
    """
    wanted: dict[str, str] = {}
    for name in sorted(EXPECTED):
        manifest = _manifest(captures / name)
        for case_id, raw in sorted(manifest["responses"].items()):
            answer = json.loads((captures / name / Path(raw).name).read_text(encoding="utf-8"))
            if answer["findings"] and not validate_review_output_v2(answer):
                digest = manifest["run_metadata"]["rag_evidence"][case_id]["prompt_digest"]
                wanted[f"{case_id}-{digest.removeprefix('sha256-v1:')[:12]}.txt"] = digest
    return wanted


def test_the_oracle_holds_the_exact_prompt_of_every_valid_answer_with_findings() -> None:
    """The user messages the tech lead read are committed byte for byte, pinned to the recording."""
    wanted = _prompt_files(CAPTURES)
    system = SYSTEM_PROMPT.read_bytes().decode("utf-8")

    assert {path.name for path in PROMPTS.iterdir()} == set(wanted)
    for name, digest in wanted.items():
        user = (PROMPTS / name).read_bytes().decode("utf-8")
        assert _prompt_digest(system, user) == digest, name
