"""Unit contracts for versioned prompt assets used by the deploy seed command."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from app.bootstrap.seed_prompts import load_prompt_assets

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_load_prompt_assets_preserves_full_file_and_checksum() -> None:
    prompts_dir = REPO_ROOT / "review" / "prompts"

    assets = load_prompt_assets(prompts_dir)

    assert [(asset.key, asset.version) for asset in assets] == [
        ("review.conventions", 1),
        ("review.system", 1),
    ]
    for asset in assets:
        expected_content = (prompts_dir / f"{asset.key}.v{asset.version}.md").read_text(
            encoding="utf-8"
        )
        assert asset.content == expected_content
        assert asset.checksum == sha256(expected_content.encode("utf-8")).hexdigest()


def test_load_prompt_assets_rejects_filename_version_mismatch(tmp_path: Path) -> None:
    (tmp_path / "review.system.v2.md").write_text(
        "---\nkey: review.system\nversion: 1\n---\nPrompt\n", encoding="utf-8"
    )

    try:
        load_prompt_assets(tmp_path)
    except ValueError as error:
        assert "does not match frontmatter version" in str(error)
    else:
        raise AssertionError("expected a filename/frontmatter mismatch to be rejected")
