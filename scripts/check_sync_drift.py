"""Compare the SYNC copies shared by the ui and api repositories (#270).

``markers``: a file under ``.agents/`` or ``.github/`` of either root is a SYNC file when it holds
``SYNC: mirrored in dmc-268-{ui,api}-t6/<path>`` with ``<path>`` equal to its own path; every
such file of the two roots must exist in both with the same bytes. A line that names another path
(the quoted marker in ``.agents/README.md``, a marker inside a JSON body) does not count.

ui is the source (``.agents/README.md`` § Sync map). Exit 0: no drift; 1: drift, listed on
stdout; 2: usage error.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

SCAN_DIRS = (".agents", ".github")
MARKER = re.compile(rb"SYNC: mirrored in dmc-268-\{ui,api\}-t6/(\S+?)(?=-->|\s|\Z)")
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


def _report(title: str, drift: list[str]) -> int:
    print(f"{title} ({len(drift)}):")
    for line in drift:
        print(line)
    print(HINT)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("check", choices=["markers"])
    parser.add_argument("--ui", type=Path, required=True, help="root of a dmc-268-ui-t6 checkout")
    parser.add_argument("--api", type=Path, required=True, help="root of a dmc-268-api-t6 checkout")
    args = parser.parse_args(argv)

    count, drift = check_markers(args.ui, args.api)
    if drift:
        return _report("SYNC files that differ between ui and api", drift)
    print(f"{count} SYNC files match between ui and api.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
