from __future__ import annotations

import runpy
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast

CASE = Path(__file__).resolve().parent
RUN_IDS = (-1, 0, 7)
TITLES = ("", "checks", "✓ ready", "{placeholder}")
SCORES = (None, -0.0, 0.0, 1.05, 33.333333, 100.0, float("inf"), float("nan"))


class Renderer(Protocol):
    def render(self, title: str, score: float | None) -> str: ...


def observe(source: Path) -> dict[tuple[str, int, str, int], str]:
    namespace = runpy.run_path(str(source))
    base_type = cast(type[object], namespace["SummaryLine"])
    run_type = cast(type[object], namespace["RunSummaryLine"])
    base_render = cast(Callable[[object, str, float | None], str], base_type.__dict__["render"])

    def timestamped_render(self: object, title: str, score: float | None) -> str:
        return f"timestamped<{base_render(self, title, score)}>"

    mixin_type = type("TimestampMixin", (base_type,), {"render": timestamped_render})
    composite_type = type("CompositeRunSummaryLine", (run_type, mixin_type), {})
    factories = {
        "direct": cast(Callable[[int], Renderer], run_type),
        "composite": cast(Callable[[int], Renderer], composite_type),
    }
    return {
        (variant, run_id, title, score_index): factory(run_id).render(title, score)
        for variant, factory in factories.items()
        for run_id in RUN_IDS
        for title in TITLES
        for score_index, score in enumerate(SCORES)
    }


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        shutil.copytree(CASE / "base", root, dirs_exist_ok=True)
        source = root / "review/summary_line.py"
        before = observe(source)
        assert len(before) == 192
        assert before[("direct", 7, "checks", 5)] == "[run 7] checks: 100.0%"
        assert before[("composite", 7, "checks", 5)] == ("[run 7] timestamped<checks: 100.0%>")
        assert before[("direct", -1, "", 0)] == "[run -1] "
        subprocess.run(["git", "apply", str(CASE / "diff.patch")], cwd=root, check=True)
        after = observe(source)
        assert after == before
        print("same rendered lines for 192 direct and multiple-inheritance inputs")


if __name__ == "__main__":
    main()
