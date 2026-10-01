from __future__ import annotations

import runpy
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import cast

CASE = Path(__file__).resolve().parent
MATCHED = (-1, 0, 1, 2, 3, 99, 1_000_000, 2**53 - 1, 2**53 + 1)
DENOMINATORS = (-2, -1, 0, 1, 2, 3, 6, 7, 100, 1_000_001, 2**53 + 3)


def observe(source: Path) -> dict[tuple[str, int, int], float]:
    namespace = runpy.run_path(str(source))
    functions = {
        name: cast(Callable[[int, int], float], namespace[name])
        for name in ("findings_recall_percent", "findings_precision_percent")
    }
    return {
        (name, matched, denominator): calculate(matched, denominator)
        for name, calculate in functions.items()
        for matched in MATCHED
        for denominator in DENOMINATORS
    }


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        shutil.copytree(CASE / "base", root, dirs_exist_ok=True)
        source = root / "review/metrics.py"
        before = observe(source)
        assert len(before) == 198
        assert before[("findings_recall_percent", 0, 0)] == 0.0
        assert before[("findings_precision_percent", 1, -1)] == 0.0
        assert before[("findings_recall_percent", 1, 3)] == 33.33333333333333
        assert before[("findings_precision_percent", 2, 3)] == 66.66666666666666
        assert before[("findings_recall_percent", 3, 2)] == 150.0
        subprocess.run(["git", "apply", str(CASE / "diff.patch")], cwd=root, check=True)
        after = observe(source)
        assert after == before
        print("same precision and recall for 198 boundary and rounding combinations")


if __name__ == "__main__":
    main()
