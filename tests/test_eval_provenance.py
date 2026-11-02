"""Digest inputs of the RAG sub-corpus (#146): what the replay of a RAG capture pins.

The main and rules baselines keep the digests they were recorded with: the RAG modules join the
static inputs only on request, and ``index.json`` joins the corpus inputs of a RAG case only.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from review.scripts import eval_provenance
from review.scripts.eval_provenance import (
    DigestInputMissing,
    corpus_input_paths,
    digest_files,
    rule_json_paths,
    static_input_paths,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests/fixtures/rag_eval/spike"
PROMPT = "review/prompts/review.system.v5.md"
RAG_ONLY = sorted(set(eval_provenance._RAG_CODE) - set(eval_provenance._REVIEW_CODE))
# Literal on purpose: the modules a RAG block, its frozen index and its replay are made of.
ANCHORS = {
    "app/modules/code_index/application/build_index.py",
    "app/modules/reviews/application/context_sources.py",
    "app/modules/reviews/application/graph_retriever.py",
    "app/modules/reviews/application/prepare_policy_snapshot.py",
    "app/modules/reviews/application/rag_context.py",
    "app/modules/reviews/infrastructure/ast_worker.py",
    "app/modules/reviews/infrastructure/subprocess_source_parser.py",
    "app/modules/reviews/infrastructure/tree_sitter_facts.py",
    "review/scripts/eval_rag_context.py",
    "review/scripts/eval_rag_source.py",
    "review/scripts/rag_snapshot.py",
}


def _static(*, rag_code: bool) -> list[str]:
    return static_input_paths(PROMPT, rule_json_paths(REPO_ROOT), rag_code=rag_code)


def _copy_inputs(tmp_path: Path, paths: list[str]) -> Path:
    root = tmp_path / "repo"
    for raw in paths:
        target = root / raw
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / raw, target)
    return root


def test_the_default_static_inputs_are_those_of_the_recorded_baselines() -> None:
    default = static_input_paths(PROMPT, rule_json_paths(REPO_ROOT))

    assert default == _static(rag_code=False)
    assert not set(RAG_ONLY) & set(default)


def test_rag_static_inputs_add_exactly_the_rag_modules() -> None:
    assert RAG_ONLY
    assert set(_static(rag_code=True)) - set(_static(rag_code=False)) == set(RAG_ONLY)
    assert set(_static(rag_code=False)) <= set(_static(rag_code=True))


def test_the_rag_static_inputs_hold_every_anchor_module() -> None:
    assert set(_static(rag_code=True)) >= ANCHORS


def test_every_rag_module_is_a_file_the_digest_can_read() -> None:
    assert digest_files(REPO_ROOT, _static(rag_code=True)).startswith(eval_provenance.PREFIX)


@pytest.mark.parametrize("module", RAG_ONLY)
def test_editing_a_rag_module_moves_the_rag_digest_only(tmp_path: Path, module: str) -> None:
    rag_paths, default_paths = _static(rag_code=True), _static(rag_code=False)
    root = _copy_inputs(tmp_path, rag_paths)
    rag_before = digest_files(root, rag_paths)
    default_before = digest_files(root, default_paths)

    with (root / module).open("a", encoding="utf-8") as handle:
        handle.write("\n# edited\n")

    assert digest_files(root, rag_paths) != rag_before
    assert digest_files(root, default_paths) == default_before


def _rag_dataset(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    root = tmp_path / "dataset"
    case_dir = root / "cases" / "RAG-01"
    shutil.copytree(FIXTURE, case_dir)
    (case_dir / "case.json").write_text("{}", encoding="utf-8")
    return root, case_dir, {"class": "rag", "patch_path": "diff.patch", "base_path": "base"}


def test_a_rag_case_digests_its_frozen_index(tmp_path: Path) -> None:
    root, case_dir, record = _rag_dataset(tmp_path)
    paths = corpus_input_paths(root, [(case_dir, record)])
    before = digest_files(root, paths)

    assert "cases/RAG-01/index.json" in paths
    with (case_dir / "index.json").open("a", encoding="utf-8") as handle:
        handle.write(" ")

    assert digest_files(root, corpus_input_paths(root, [(case_dir, record)])) != before


def test_a_rag_case_without_its_index_is_an_error_not_a_smaller_digest(tmp_path: Path) -> None:
    root, case_dir, record = _rag_dataset(tmp_path)
    (case_dir / "index.json").unlink()

    with pytest.raises(DigestInputMissing, match="cases/RAG-01/index.json"):
        corpus_input_paths(root, [(case_dir, record)])


def test_the_corpus_inputs_of_another_class_never_list_an_index(tmp_path: Path) -> None:
    root, case_dir, record = _rag_dataset(tmp_path)
    expected = [
        path
        for path in corpus_input_paths(root, [(case_dir, record)])
        if path != "cases/RAG-01/index.json"
    ]

    for other in ("security", "rules"):
        assert corpus_input_paths(root, [(case_dir, {**record, "class": other})]) == expected
    # An old record (no class at all) is read as before.
    bare = {"patch_path": "diff.patch", "base_path": "base"}
    assert corpus_input_paths(root, [(case_dir, bare)]) == expected
