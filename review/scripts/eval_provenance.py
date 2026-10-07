"""Versioned provenance for immutable review-evaluation inputs.

``sha256-v1:<hex>`` hashes sorted, unique POSIX paths and exact file bytes. The
stream begins with ``b"review-eval-inputs-v1\\0"``; each entry is its UTF-8 path
length (8-byte big-endian), path, content length (8-byte big-endian), content.
Any prompt/rule/rendering/corpus edit requires a fresh *complete* baseline; replay
warns on drift and never changes recorded answers.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

PREFIX = "sha256-v1:"
_MAGIC = b"review-eval-inputs-v1\0"
_REVIEW_CODE = (
    "app/bootstrap/llm_gateway.py",
    "app/common/application/languages.py",
    "app/modules/reviews/application/llm.py",
    "app/modules/reviews/application/prompt_budget.py",
    "app/modules/reviews/application/prompt_builder.py",
    "app/modules/reviews/application/review_output.py",
    "app/modules/reviews/application/run_failures.py",
    "app/modules/reviews/infrastructure/llm/answers.py",
    "app/modules/reviews/infrastructure/llm/gateway.py",
    "app/modules/reviews/infrastructure/llm/models.py",
    "app/modules/reviews/infrastructure/llm/settings.py",
    "app/modules/reviews/infrastructure/llm/transport.py",
    "review/schemas/review-output.schema.json",
    "review/scripts/eval_live.py",
)
_CONVENTIONS_RENDERER = "app/modules/reviews/application/conventions_prompt.py"


class DigestInputError(ValueError):
    """A digest input is unsafe or cannot be read."""


class DigestInputMissing(DigestInputError):
    """A recorded digest input path does not exist."""


def _safe_file(root: Path, raw: str) -> Path:
    relative = Path(raw)
    if (
        not raw
        or relative.is_absolute()
        or ".." in relative.parts
        or relative.as_posix() != raw
        or "\\" in raw
        or raw == "."
    ):
        raise DigestInputError(f"unsafe digest path: {raw}")
    if root.is_symlink():
        raise DigestInputError(f"symlink digest root: {root}")
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise DigestInputError(f"symlink digest path: {raw}")
    if not current.resolve().is_relative_to(root.resolve()):
        raise DigestInputError(f"unsafe digest path: {raw}")
    if not current.exists():
        raise DigestInputMissing(f"missing digest file: {raw}")
    if not current.is_file():
        raise DigestInputError(f"digest path is not a file: {raw}")
    return current


def digest_files(root: Path, paths: Iterable[str]) -> str:
    """Hash path-and-byte inputs independent of caller order, without following links."""
    digest = hashlib.sha256(_MAGIC)
    for raw in sorted(set(paths)):
        path = _safe_file(root, raw)
        name = raw.encode("utf-8")
        data = path.read_bytes()
        digest.update(len(name).to_bytes(8, "big"))
        digest.update(name)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return PREFIX + digest.hexdigest()


def rule_json_paths(root: Path) -> list[str]:
    """Include every literal review/rules/*.json file, including schema.json."""
    rules_dir = root / "review/rules"
    if rules_dir.is_symlink():
        raise DigestInputError(f"symlink rule directory: {rules_dir}")
    if not rules_dir.exists():
        raise DigestInputMissing(f"missing rule directory: {rules_dir}")
    if not rules_dir.is_dir():
        raise DigestInputError(f"rule path is not a directory: {rules_dir}")
    return sorted(path.relative_to(root).as_posix() for path in rules_dir.glob("*.json"))


def static_input_paths(
    system_prompt: str,
    rule_paths: Iterable[str],
    *,
    conventions_prompt: str | None = None,
) -> list[str]:
    """List every checked-in file that can shape this runner's provider messages."""
    paths = [system_prompt, *_REVIEW_CODE, *rule_paths]
    if conventions_prompt is not None:
        paths.extend((conventions_prompt, _CONVENTIONS_RENDERER))
    return sorted(set(paths))


def corpus_input_paths(root: Path, records: Sequence[tuple[Path, Mapping[str, Any]]]) -> list[str]:
    """List exact metadata, patch, and recursive pre-image files for active cases."""
    paths: list[str] = []
    for case_dir, record in records:
        try:
            case = case_dir.relative_to(root)
        except ValueError as exc:
            raise DigestInputError(f"unsafe case directory: {case_dir}") from exc
        if case_dir.is_symlink() or ".." in case.parts:
            raise DigestInputError(f"symlink or unsafe case directory: {case_dir}")
        for name in ("case.json", record["patch_path"]):
            raw = (case / name).as_posix()
            _safe_file(root, raw)
            paths.append(raw)
        base_relative = Path(record["base_path"])
        if base_relative.is_absolute() or ".." in base_relative.parts:
            raise DigestInputError(f"unsafe pre-image directory: {base_relative}")
        base = case_dir
        for part in base_relative.parts:
            base = base / part
            if base.is_symlink():
                raise DigestInputError(f"symlink pre-image directory: {base}")
        relative_base = base.relative_to(root).as_posix()
        if not base.exists():
            raise DigestInputMissing(f"missing pre-image directory: {relative_base}")
        if not base.is_dir():
            raise DigestInputError(f"pre-image path is not a directory: {relative_base}")
        for item in base.rglob("*"):
            if item.is_symlink():
                raise DigestInputError(f"symlink pre-image path: {item}")
            if item.is_file():
                raw = item.relative_to(root).as_posix()
                _safe_file(root, raw)
                paths.append(raw)
    return sorted(set(paths))
