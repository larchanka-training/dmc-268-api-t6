"""The fake `gh` of the pr-review tests: imported by each test file, never copied."""
# SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/pr-review/tests/gh_fakes.py

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REFUSING_GH = 'import sys\nsys.stderr.write("unexpected gh call\\n")\nsys.exit(97)\n'


def fake_gh(bin_dir: Path, body: str) -> Path:
    """Write an executable `gh` (a Python script) into bin_dir."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    gh.chmod(0o755)
    return bin_dir


def prepend_path(monkeypatch: pytest.MonkeyPatch, bin_dir: Path) -> None:
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")


def refuse_live_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Any request that escapes the fixture seam or a test's own fake is an error, never a call."""
    prepend_path(monkeypatch, fake_gh(tmp_path / "guard-bin", REFUSING_GH))
