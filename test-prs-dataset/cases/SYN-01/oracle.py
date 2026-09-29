from __future__ import annotations

import runpy
import shutil
import subprocess
import tempfile
from pathlib import Path

CASE = Path(__file__).resolve().parent


def observe(source: Path) -> list[list[str]]:
    collect = runpy.run_path(str(source))["collect_required_checks"]
    return [
        collect([]),
        collect(["test", "lint", "test", "typecheck"]),
        collect(name for name in ["build", "lint", "build"]),
    ]


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        shutil.copytree(CASE / "base", root, dirs_exist_ok=True)
        source = root / "ci/checks.py"
        before = observe(source)
        assert before == [[], ["lint", "test", "typecheck"], ["build", "lint"]]
        subprocess.run(["git", "apply", str(CASE / "diff.patch")], cwd=root, check=True)
        after = observe(source)
        assert after == before
        print("same sorted, deduplicated check names before and after patch")


if __name__ == "__main__":
    main()
