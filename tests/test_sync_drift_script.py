"""The SYNC drift check (#270): ``scripts/check_sync_drift.py`` run on two repository roots.

``markers`` compares, byte for byte, every file under ``.agents/`` and ``.github/`` of either
root whose ``SYNC: mirrored in dmc-268-{ui,api}-t6/<path>`` line names its own path.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts/check_sync_drift.py"
MIRRORED = "SYNC: mirrored in dmc-268-{ui,api}-t6/"
SKILL = ".agents/skills/qa/SKILL.md"
SCRIPT_PY = ".agents/skills/issue-graph/scripts/graph.py"
TEMPLATE = ".github/pull_request_template.md"


def _md(path: str, body: str = "body\n") -> bytes:
    return f"# Title\n\n<!-- {MIRRORED}{path} -->\n\n{body}".encode()


def _py(path: str, body: str = "x = 1\n") -> bytes:
    return f'"""Doc."""\n\n# {MIRRORED}{path}  # noqa: E501\n\n{body}'.encode()


def _tree(root: Path, files: dict[str, bytes]) -> Path:
    for path, content in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(content)
    return root


def _run(tmp_path: Path, ui: dict[str, bytes], api: dict[str, bytes]) -> subprocess.CompletedProcess[str]:
    ui_root = _tree(tmp_path / "ui", ui)
    api_root = _tree(tmp_path / "api", api)
    return subprocess.run(
        [sys.executable, str(SCRIPT), "markers", "--ui", str(ui_root), "--api", str(api_root)],
        capture_output=True,
        encoding="utf-8",
        check=False,
    )


def test_marked_copies_with_the_same_bytes_pass(tmp_path: Path) -> None:
    files = {SKILL: _md(SKILL), SCRIPT_PY: _py(SCRIPT_PY), TEMPLATE: _md(TEMPLATE)}

    result = _run(tmp_path, files, dict(files))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "3 SYNC files match" in result.stdout


@pytest.mark.parametrize(
    ("ui", "api", "line"),
    [
        ({SKILL: _md(SKILL)}, {SKILL: _md(SKILL, "edited in api\n")}, f"differs: {SKILL}"),
        ({SCRIPT_PY: _py(SCRIPT_PY)}, {}, f"missing in api: {SCRIPT_PY}"),
        ({}, {TEMPLATE: _md(TEMPLATE)}, f"missing in ui: {TEMPLATE}"),
        # The marker only in ui still makes the file a SYNC file: the api copy lost it.
        ({SKILL: _md(SKILL)}, {SKILL: b"# Title\n\nbody\n"}, f"differs: {SKILL}"),
    ],
    ids=["differs", "only-in-ui", "only-in-api", "marker-dropped-in-api"],
)
def test_drift_in_a_marked_file_fails_with_its_path(
    tmp_path: Path, ui: dict[str, bytes], api: dict[str, bytes], line: str
) -> None:
    result = _run(tmp_path, ui, api)

    assert result.returncode == 1, result.stdout + result.stderr
    assert line in result.stdout.splitlines()


def test_lines_that_do_not_mark_their_own_file_are_not_sync_files(tmp_path: Path) -> None:
    readme = ".agents/README.md"
    in_repo = ".agents/skills/code-review/references/standards.md"
    fixture = ".agents/skills/pr-review/examples/case/pr.json"
    builder = ".agents/skills/accept-issue/tests/test_examples.py"
    other = ".agents/skills/qa/notes.md"
    ui = {
        # The quoted marker form in the sync map (the issue's README.md:61).
        readme: f"`<!-- {MIRRORED}.agents/<path> -->` line:\nui\n".encode(),
        # An in-repo marker: no {ui,api} pair.
        in_repo: b"<!-- SYNC: mirrors .agents/rules/backend.md -->\nui\n",
        # A PR body that carries the template's marker, JSON-escaped.
        fixture: f'{{"body": "<!-- {MIRRORED}{TEMPLATE} --\\u003e ui"}}\n'.encode(),
        # A test that builds markers with an f-string.
        builder: b'line = f"<!-- SYNC: mirrored in dmc-268-{{ui,api}}-t6/' + builder.encode() + b' -->"\n',
        # A marker that names another file.
        other: _md(SKILL),
    }
    api = {path: content + b"api\n" for path, content in ui.items()}

    result = _run(tmp_path, ui, api)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 SYNC files match" in result.stdout
