"""The SYNC drift check (#270): ``scripts/check_sync_drift.py`` run on two repository roots.

``markers`` compares, byte for byte, every file under ``.agents/`` and ``.github/`` of either
root whose ``SYNC: mirrored in dmc-268-{ui,api}-t6/<path>`` line names its own path.
``json-dirs`` runs ``diff -r`` on the marker-less JSON paths that the sync map of either root's
``.agents/README.md`` names.
"""

from __future__ import annotations

import shutil
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


def _check(check: str, ui: Path, api: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), check, "--ui", str(ui), "--api", str(api)],
        capture_output=True,
        encoding="utf-8",
        check=False,
    )


def _run(
    tmp_path: Path, ui: dict[str, bytes], api: dict[str, bytes]
) -> subprocess.CompletedProcess[str]:
    return _check("markers", _tree(tmp_path / "ui", ui), _tree(tmp_path / "api", api))


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
        builder: b'line = f"<!-- SYNC: mirrored in dmc-268-{{ui,api}}-t6/'
        + builder.encode()
        + b' -->"\n',
        # A marker that names another file.
        other: _md(SKILL),
    }
    api = {path: content + b"api\n" for path, content in ui.items()}

    result = _run(tmp_path, ui, api)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 SYNC files match" in result.stdout


# The JSON paths of the sync map in .agents/README.md on 09.11.2026, brace groups expanded.
SYNC_MAP_JSON_PATHS = [
    ".agents/skills/issue-graph/tests/fixtures/",
    ".agents/skills/implement-issue/tests/fixtures/",
    ".agents/skills/implement-issue/examples/",
    ".agents/skills/accept-issue/tests/golden/",
    ".agents/skills/accept-issue/tests/fixtures/",
    ".agents/skills/accept-issue/examples/",
    ".agents/skills/pr-review/examples/",
    ".agents/skills/create-issue/examples/*/batch.json",
]
README = ".agents/README.md"
# A sync map in the shape of the real one: the golden directory of the first sentence holds marked
# files and is not one of the JSON paths.
SYNC_MAP = (
    b"Golden files under `skills/g/tests/golden/` carry the SYNC line. JSON files under\n"
    b"`skills/a/{x,y}/`, `skills/b/` (also its `.jsonl` and `.diff`) and\n"
    b"`skills/c/*/batch.json` carry no marker (JSON has no comments).\n"
)
JSON_TREE = {
    README: SYNC_MAP,
    ".agents/skills/a/x/one.json": b"{}\n",
    ".agents/skills/a/y/two.json": b"[]\n",
    ".agents/skills/b/case/calls.jsonl": b"{}\n{}\n",
    ".agents/skills/c/01-single/batch.json": b"{}\n",
}


def _json_dirs(
    tmp_path: Path, ui: dict[str, bytes | None], api: dict[str, bytes | None]
) -> subprocess.CompletedProcess[str]:
    """``json-dirs`` on JSON_TREE in both roots, changed per root (``None`` deletes a path)."""
    roots = []
    for name, change in (("ui", ui), ("api", api)):
        root = _tree(tmp_path / name, JSON_TREE)
        for path, content in change.items():
            if content is None:
                shutil.rmtree(root / path)
            else:
                _tree(root, {path: content})
        roots.append(root)
    return _check("json-dirs", *roots)


def test_the_sync_map_of_this_repo_names_the_json_paths() -> None:
    result = _check("json-dirs", REPO_ROOT, REPO_ROOT)

    assert result.returncode == 0, result.stdout + result.stderr
    compared = [line.split(" (", 1)[0] for line in result.stdout.splitlines()[1:]]
    assert compared == SYNC_MAP_JSON_PATHS


def test_json_paths_with_the_same_bytes_pass(tmp_path: Path) -> None:
    # The golden directory outside the JSON sentence differs and is not compared.
    golden = ".agents/skills/g/tests/golden/case.md"

    result = _json_dirs(tmp_path, {golden: b"ui\n"}, {golden: b"api\n"})

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines()[1:] == [
        ".agents/skills/a/x/",
        ".agents/skills/a/y/",
        ".agents/skills/b/",
        ".agents/skills/c/*/batch.json (1 file)",
    ]


@pytest.mark.parametrize(
    ("ui", "api", "name"),
    [
        ({}, {".agents/skills/b/case/calls.jsonl": b"{}\n"}, "calls.jsonl"),
        ({}, {".agents/skills/b/case.diff": b"+x\n"}, "case.diff"),
        ({}, {".agents/skills/a/y/two.json": b"[1]\n"}, "two.json"),
        ({".agents/skills/c/02-two/batch.json": b"{}\n"}, {}, "02-two/batch.json"),
        ({}, {".agents/skills/c/01-single/batch.json": b"{ }\n"}, "01-single/batch.json"),
        # A directory the sync map names is gone from both roots: the map or the tree is stale.
        ({".agents/skills/a/x": None}, {".agents/skills/a/x": None}, "skills/a/x"),
        ({".agents/skills/c": None}, {".agents/skills/c": None}, "skills/c/*/batch.json"),
        # A path named only in the ui sync map is compared too.
        (
            {README: SYNC_MAP.replace(b"`skills/b/`", b"`skills/b/`, `skills/d/`")},
            {".agents/skills/d/three.json": b"{}\n"},
            "skills/d",
        ),
    ],
    ids=[
        "jsonl-differs",
        "diff-only-in-api",
        "braced-dir",
        "batch-only-in-ui",
        "batch-differs",
        "named-dir-in-neither",
        "no-batch-in-either",
        "ui-sync-map-only",
    ],
)
def test_drift_in_a_json_path_fails_with_the_file(
    tmp_path: Path, ui: dict[str, bytes | None], api: dict[str, bytes | None], name: str
) -> None:
    result = _json_dirs(tmp_path, ui, api)

    assert result.returncode == 1, result.stdout + result.stderr
    assert any(name in line for line in result.stdout.splitlines()[1:-1]), result.stdout


@pytest.mark.parametrize("side", ["ui", "api"])
def test_a_sync_map_without_the_json_sentence_is_an_error(tmp_path: Path, side: str) -> None:
    rewritten: dict[str, bytes | None] = {README: b"JSON fixtures are compared elsewhere.\n"}

    result = _json_dirs(
        tmp_path, rewritten if side == "ui" else {}, rewritten if side == "api" else {}
    )

    assert result.returncode == 2, result.stdout + result.stderr
    assert f"{side}/{README}" in result.stderr
