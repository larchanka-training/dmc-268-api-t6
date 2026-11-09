"""Compare the SYNC copies shared by the ui and api repositories (#270).

``markers``: a file under ``.agents/`` or ``.github/`` of either root is a SYNC file when it holds
``SYNC: mirrored in dmc-268-{ui,api}-t6/<path>`` with ``<path>`` equal to its own path; every
such file of the two roots must exist in both with the same bytes. A line that names another path
(the quoted marker in ``.agents/README.md``, a marker inside a JSON body) does not count.

``json-dirs``: JSON has no comments, so the sync map of ``.agents/README.md`` names the marker-less
JSON paths in its sentence "JSON files under ... carry no marker". The paths of both roots' sync
maps are compared with ``diff -r``: a directory as a whole (``.jsonl`` and ``.diff`` included), a
file pattern per matching file of either root.

ui is the source (``.agents/README.md`` § Sync map). Exit 0: no drift; 1: drift, listed on
stdout; 2: usage error or a sync map without that sentence.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

SCAN_DIRS = (".agents", ".github")
MARKER = re.compile(rb"SYNC: mirrored in dmc-268-\{ui,api\}-t6/(\S+?)(?=-->|\s|\Z)")
SYNC_MAP = ".agents/README.md"
JSON_SENTENCE = re.compile(r"JSON\s+files\s+under\s+(.*?)\s+carry\s+no\s+marker", re.DOTALL)
SPAN = re.compile(r"`([^`]+)`")
BRACES = re.compile(r"\{([^{}]*)\}")
HINT = (
    "ui main is the source: copy the ui file into api (.agents/README.md § Sync map). "
    "Between a ui merge and its api copy this check is red on purpose."
)


def _sync_files(root: Path) -> set[str]:
    """Repo-relative POSIX paths of the files under SCAN_DIRS that mark their own path."""
    found: set[str] = set()
    for top in SCAN_DIRS:
        for dirpath, _dirnames, filenames in os.walk(root / top):
            for name in filenames:
                path = Path(dirpath, name)
                relative = path.relative_to(root).as_posix()
                own = os.fsencode(relative)
                if any(match.group(1) == own for match in MARKER.finditer(path.read_bytes())):
                    found.add(relative)
    return found


def check_markers(ui: Path, api: Path) -> tuple[int, list[str]]:
    """The number of SYNC files over both roots and one drift line per file that differs."""
    paths = sorted(_sync_files(ui) | _sync_files(api))
    drift: list[str] = []
    for relative in paths:
        ui_file, api_file = ui / relative, api / relative
        if not ui_file.is_file():
            drift.append(f"missing in ui: {relative}")
        elif not api_file.is_file():
            drift.append(f"missing in api: {relative}")
        elif ui_file.read_bytes() != api_file.read_bytes():
            drift.append(f"differs: {relative}")
    return len(paths), drift


class SyncMapError(Exception):
    """A sync map without the sentence that names the JSON paths."""


def _expand(pattern: str) -> list[str]:
    """``a/{b,c}/`` → ``a/b/``, ``a/c/``."""
    group = BRACES.search(pattern)
    if group is None:
        return [pattern]
    head, tail = pattern[: group.start()], pattern[group.end() :]
    return [path for choice in group.group(1).split(",") for path in _expand(head + choice + tail)]


def json_paths(root: Path) -> list[str]:
    """The JSON paths of the root's sync map, repo-relative, in the order it names them."""
    sync_map = root / SYNC_MAP
    sentence = JSON_SENTENCE.search(sync_map.read_text(encoding="utf-8"))
    spans = SPAN.findall(sentence.group(1)) if sentence else []
    paths = [
        f".agents/{path}" for span in spans if span.startswith("skills/") for path in _expand(span)
    ]
    if not paths:
        raise SyncMapError(
            f"{sync_map}: no 'JSON files under `skills/...` carry no marker' sentence"
        )
    return paths


def check_json_dirs(ui: Path, api: Path) -> tuple[list[str], list[str]]:
    """One line per compared sync map path and the ``diff -r`` lines of every drift."""
    patterns = list(dict.fromkeys(json_paths(ui) + json_paths(api)))
    compared: list[str] = []
    drift: list[str] = []
    for pattern in patterns:
        if pattern.endswith("/"):
            targets = [pattern]
            compared.append(pattern)
        else:
            targets = sorted(
                {
                    path.relative_to(root).as_posix()
                    for root in (ui, api)
                    for path in root.glob(pattern)
                }
            )
            compared.append(f"{pattern} ({len(targets)} file{'' if len(targets) == 1 else 's'})")
            if not targets:
                drift.append(f"no file matches {pattern} in ui or api")
        for target in targets:
            result = subprocess.run(
                ["diff", "-r", "-q", str(ui / target), str(api / target)],
                capture_output=True,
                encoding="utf-8",
                check=False,
            )
            if result.returncode != 0:
                drift.extend((result.stdout + result.stderr).splitlines())
    return compared, drift


def _report(title: str, drift: list[str]) -> int:
    print(f"{title} ({len(drift)}):")
    for line in drift:
        print(line)
    print(HINT)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("check", choices=["markers", "json-dirs"])
    parser.add_argument("--ui", type=Path, required=True, help="root of a dmc-268-ui-t6 checkout")
    parser.add_argument("--api", type=Path, required=True, help="root of a dmc-268-api-t6 checkout")
    args = parser.parse_args(argv)

    if args.check == "markers":
        count, drift = check_markers(args.ui, args.api)
        if drift:
            return _report("SYNC files that differ between ui and api", drift)
        print(f"{count} SYNC files match between ui and api.")
        return 0

    try:
        compared, drift = check_json_dirs(args.ui, args.api)
    except SyncMapError as error:
        print(error, file=sys.stderr)
        return 2
    if drift:
        return _report("Sync map JSON paths that differ between ui and api", drift)
    print("Sync map JSON paths match between ui and api:")
    for line in compared:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
