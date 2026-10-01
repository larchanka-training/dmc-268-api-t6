from __future__ import annotations

import runpy
import shutil
import subprocess
import tempfile
from collections import OrderedDict
from collections.abc import Callable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import cast

CASE = Path(__file__).resolve().parent
INPUTS: tuple[Mapping[str, str], ...] = (
    {},
    {"X-GitHub-Delivery": "  abc-123  ", "X-GitHub-Event": " push "},
    {"X-GitHub-Delivery": " first ", "x-github-delivery": " final "},
    {"x-github-delivery": " final ", "X-GitHub-Delivery": " first "},
    {"X-Événement": "  prêt  ", "X-GitHub-Event": " pull_request "},
    {"X-GitHub-Delivery ": " spaced-key ", "X-GitHub-Event": ""},
    MappingProxyType({"X-GitHub-Delivery": " frozen ", "X-GitHub-Event": " issue_comment "}),
    OrderedDict((("Z-Trace", " last "), ("X-GitHub-Event", " labeled "), ("A-First", " 1 "))),
)


def observe(source: Path) -> list[tuple[list[tuple[str, str]], tuple[str | None, str | None]]]:
    namespace = runpy.run_path(str(source))
    canonicalize = cast(
        Callable[[Mapping[str, str]], dict[str, str]], namespace["canonicalize_headers"]
    )
    metadata = cast(
        Callable[[Mapping[str, str]], tuple[str | None, str | None]],
        namespace["delivery_metadata"],
    )
    return [(list(canonicalize(headers).items()), metadata(headers)) for headers in INPUTS]


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        shutil.copytree(CASE / "base", root, dirs_exist_ok=True)
        source = root / "webhook/headers.py"
        before = observe(source)
        assert len(before) == 8
        assert before[0] == ([], (None, None))
        assert before[1] == (
            [("x-github-delivery", "abc-123"), ("x-github-event", "push")],
            ("abc-123", "push"),
        )
        assert before[2] == ([("x-github-delivery", "final")], ("final", None))
        assert before[3] == ([("x-github-delivery", "first")], ("first", None))
        subprocess.run(["git", "apply", str(CASE / "diff.patch")], cwd=root, check=True)
        after = observe(source)
        assert after == before
        print("same canonical headers and delivery metadata for eight edge cases")


if __name__ == "__main__":
    main()
