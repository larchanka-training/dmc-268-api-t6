"""Write or check the frozen graph-index snapshot (``index.json``) of every RAG case.

``ROOT`` holds case directories, each with a ``base/`` tree. ``--write`` regenerates
``index.json`` next to ``base/``; ``--check`` regenerates it in memory and fails when a
committed ``index.json`` is missing or differs byte for byte (the base tree changed, an
extractor changed, or the file was edited by hand).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from review.scripts.rag_snapshot import (  # noqa: E402
    INDEX_FILE,
    SnapshotError,
    build_case_snapshot,
    render_snapshot,
)


def case_dirs(root: Path) -> list[Path]:
    """The directories of ``root`` that hold a ``base/`` tree, in name order."""
    return sorted(path for path in root.iterdir() if (path / "base").is_dir())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="regenerate every index.json")
    mode.add_argument("--check", action="store_true", help="fail if an index.json is stale")
    parser.add_argument("root", type=Path, help="directory of case directories")
    args = parser.parse_args(argv)
    if not args.root.is_dir():
        print(f"build_rag_snapshot: {args.root} is not a directory", file=sys.stderr)
        return 1
    cases = case_dirs(args.root)
    if not cases:
        print(f"build_rag_snapshot: no case with a base/ tree under {args.root}", file=sys.stderr)
        return 1
    failed = False
    for case in cases:
        try:
            expected = render_snapshot(build_case_snapshot(case))
        except SnapshotError as error:
            print(f"build_rag_snapshot: {case.name}: {error}", file=sys.stderr)
            failed = True
            continue
        target = case / INDEX_FILE
        if args.write:
            target.write_bytes(expected)
        elif not target.is_file():
            print(f"build_rag_snapshot: {case.name}: {INDEX_FILE} is missing", file=sys.stderr)
            failed = True
        elif target.read_bytes() != expected:
            print(
                f"build_rag_snapshot: {case.name}: {INDEX_FILE} is stale"
                f" (run --write and commit the result)",
                file=sys.stderr,
            )
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
