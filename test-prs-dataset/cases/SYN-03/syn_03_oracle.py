from __future__ import annotations

import runpy
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import cast

CASE = Path(__file__).resolve().parent
STATUSES = ("failed", "cancelled", "queued", "running", "passed", "")
RETRIES_USED = (-1, 0, 1, 2, 3)
RETRY_LIMITS = (0, 1, 3)


def observe(source: Path) -> dict[tuple[str, int, int], bool]:
    namespace = runpy.run_path(str(source))
    can_retry_review = cast(Callable[[str, int, int], bool], namespace["can_retry_review"])
    return {
        (status, used, limit): can_retry_review(status, used, limit)
        for status in STATUSES
        for used in RETRIES_USED
        for limit in RETRY_LIMITS
    }


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        shutil.copytree(CASE / "base", root, dirs_exist_ok=True)
        source = root / "review/retry_policy.py"
        before = observe(source)
        assert len(before) == 90
        assert before[("failed", 0, 1)] is True
        assert before[("cancelled", 2, 3)] is True
        assert before[("queued", 0, 1)] is False
        assert before[("failed", 1, 1)] is False
        subprocess.run(["git", "apply", str(CASE / "diff.patch")], cwd=root, check=True)
        after = observe(source)
        assert after == before
        print("same retry decisions for 90 state and boundary combinations")


if __name__ == "__main__":
    main()
