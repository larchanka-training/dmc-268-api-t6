"""The reviewer baseline fingerprint includes every verifier input used by #217."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from review.scripts.eval_provenance import digest_files, rule_json_paths, static_input_paths

ROOT = Path(__file__).resolve().parents[1]
VERIFIER_PINS = {
    "review/prompts/review.verifier.v1.md",
    "review/prompts/review.verifier.v2.md",
    "review/schemas/review-output.schema.v2.json",
    "review/schemas/verifier-output.schema.v1.json",
    "app/modules/reviews/application/reflection_pass.py",
}


def test_static_inputs_pin_the_exact_verifier_contract_files() -> None:
    paths = static_input_paths(
        "review/prompts/review.system.v5.md", rule_json_paths(ROOT), repo_root=ROOT
    )

    verifier_paths = {
        path
        for path in paths
        if path.startswith("review/prompts/review.verifier.")
        or path.startswith("review/schemas/verifier-output.")
        or path == "review/schemas/review-output.schema.v2.json"
        or path.endswith("/reflection_pass.py")
    }
    assert verifier_paths == VERIFIER_PINS
    assert paths == sorted(set(paths))


@pytest.mark.parametrize("changed_path", sorted(VERIFIER_PINS))
def test_each_verifier_input_byte_change_moves_the_static_digest(
    tmp_path: Path, changed_path: str
) -> None:
    paths = static_input_paths(
        "review/prompts/review.system.v5.md", rule_json_paths(ROOT), repo_root=ROOT
    )
    root = tmp_path / "repo"
    for relative in paths:
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, destination)
    before = digest_files(root, paths)

    with (root / changed_path).open("ab") as handle:
        handle.write(b"\n")

    assert digest_files(root, paths) != before
