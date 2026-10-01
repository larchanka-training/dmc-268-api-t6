from __future__ import annotations

import runpy
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import cast

CASE = Path(__file__).resolve().parent


class FormattedRunId(int):
    def __format__(self, format_spec: str) -> str:
        assert format_spec == ""
        return "ID{state}"


class FormattedState(str):
    def __format__(self, format_spec: str) -> str:
        assert format_spec == ""
        return "STATE{run_id}"


RUN_IDS = (-1, 0, 7, 1_000_000, FormattedRunId(13))
STATES = ("", "queued", "failed", "{run_id}", "{state}", "[✓] ready", FormattedState("custom"))


def observe(source: Path) -> dict[tuple[int, str], str]:
    namespace = runpy.run_path(str(source))
    render_run_label = cast(Callable[[int, str], str], namespace["render_run_label"])
    return {
        (run_id, state): render_run_label(run_id, state) for run_id in RUN_IDS for state in STATES
    }


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        shutil.copytree(CASE / "base", root, dirs_exist_ok=True)
        source = root / "review/run_label.py"
        before = observe(source)
        assert len(before) == 35
        assert before[(7, "queued")] == "Run #7: queued"
        assert before[(-1, "{run_id}")] == "Run #-1: {run_id}"
        assert before[(0, "")] == "Run #0: "
        assert before[(FormattedRunId(13), FormattedState("custom"))] == (
            "Run #ID{state}: STATE{run_id}"
        )
        subprocess.run(["git", "apply", str(CASE / "diff.patch")], cwd=root, check=True)
        after = observe(source)
        assert after == before
        print("same run labels for 35 IDs and states, including custom formatting")


if __name__ == "__main__":
    main()
