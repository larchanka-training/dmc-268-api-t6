"""Check the relative links of the repository's Markdown files (#270, Part 2).

Reads the tracked ``*.md`` files under ``docs/`` and ``.agents/``, ``AGENTS.md`` and
``test-prs-dataset/README.md`` of the git work tree it runs in. A relative link outside code (an
inline link or image, a reference definition) must name a tracked file or a directory that holds
one; a path starting with ``/`` is read from the repository root. A ``#fragment`` on a ``.md``
target (or alone, for the same file) must be the id GitHub renders for one of its ATX headings, or
an ``<a id>`` / ``<a name>`` in it. Links with a scheme (``https:``, ``mailto:``) or ``//host``
are not checked, nor is a fragment on any other file (``#L10``).

Exit 0: every link resolves; 1: broken links, listed on stdout as ``path:line: target: reason``;
2: not run inside a git work tree.
"""

# SYNC: mirrored in dmc-268-{ui,api}-t6/.github/scripts/check_md_links.py

from __future__ import annotations

import html
import posixpath
import re
import subprocess
import sys
import unicodedata
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import unquote

SCOPE_DIRS = ("docs/", ".agents/")
SCOPE_FILES = ("AGENTS.md", "test-prs-dataset/README.md")

FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
CODE_SPAN = re.compile(r"(`+)(.+?)(?<!`)\1(?!`)")
DESTINATION = r"(<[^<>\n]*>|[^\s()]*(?:\([^\s()]*\)[^\s()]*)*)"
TITLE = r"""(?:\s+(?:"[^"]*"|'[^']*'|\([^()]*\)))?"""
INLINE_LINK = re.compile(r"!?\[(?:[^\[\]]|\[[^\[\]]*\])*\]\(\s*" + DESTINATION + TITLE + r"\s*\)")
REFERENCE = re.compile(r"^ {0,3}\[(?!\^)[^\]]+\]:\s*(<[^<>\n]*>|\S+)")
HEADING = re.compile(r"^ {0,3}#{1,6}(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
HTML_ANCHOR = re.compile(r"<a\s[^>]*?\b(?:id|name)\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")

# Inline Markdown of a heading, reduced to the text GitHub renders before it makes the id.
IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
# Only inline links: an undefined reference such as `[ui][api]` stays literal text on GitHub.
LINK_TEXT = re.compile(r"\[([^\]]*)\]\([^)]*\)")
AUTOLINK = re.compile(r"<([A-Za-z][A-Za-z0-9+.-]*:[^<>\s]*)>")
TAG = re.compile(r"</?[A-Za-z][^<>]*>")
ESCAPE = re.compile(r"\\([!-/:-@\[-`{-~])")
UNDERSCORE_EMPHASIS = re.compile(r"(?<![^\W_])(_+)(?=\S)(.+?)(?<=\S)\1(?![^\W_])")


def _in_scope(path: str) -> bool:
    return path.endswith(".md") and (path.startswith(SCOPE_DIRS) or path in SCOPE_FILES)


def _prose_lines(text: str) -> Iterator[tuple[int, str]]:
    """(line number, line) of every line outside a fenced code block."""
    fence = ""
    for number, line in enumerate(text.splitlines(), 1):
        match = FENCE.match(line)
        if fence:
            if (
                match
                and match.group(1)[0] == fence[0]
                and len(match.group(1)) >= len(fence)
                and not line[match.end() :].strip()
            ):
                fence = ""
            continue
        if match:
            fence = match.group(1)
            continue
        yield number, line


def _inline_text(markdown: str) -> str:
    text = IMAGE.sub("", markdown)
    text = LINK_TEXT.sub(r"\1", text)
    text = AUTOLINK.sub(r"\1", text)
    text = TAG.sub("", text)
    text = ESCAPE.sub(r"\1", text)
    previous = None
    while previous != text:
        previous, text = text, UNDERSCORE_EMPHASIS.sub(r"\2", text)
    return html.unescape(text)


def _heading_text(markdown: str) -> str:
    """The rendered text of a heading: code spans verbatim, the rest without Markdown syntax."""
    parts: list[str] = []
    position = 0
    for match in CODE_SPAN.finditer(markdown):
        parts.append(_inline_text(markdown[position : match.start()]))
        code = match.group(2)
        if len(code) > 2 and code[0] == code[-1] == " " and code.strip():
            code = code[1:-1]
        parts.append(code)
        position = match.end()
    parts.append(_inline_text(markdown[position:]))
    return "".join(parts).strip()


def _slug(text: str) -> str:
    """GitHub's heading id: lowercase, only word characters, ``-`` and spaces kept, space -> -."""
    kept = (
        char
        for char in text.lower()
        if char in "- " or char.isalnum() or unicodedata.category(char) in {"Pc", "Mn", "Mc", "Me"}
    )
    return "".join(kept).replace(" ", "-")


def _anchors(text: str) -> set[str]:
    anchors: set[str] = set()
    seen: dict[str, int] = {}
    for _, line in _prose_lines(text):
        heading = HEADING.match(line)
        if heading:
            base = _slug(_heading_text(heading.group(1) or ""))
            count = seen.get(base, 0)
            anchors.add(base if count == 0 else f"{base}-{count}")
            seen[base] = count + 1
        anchors.update(HTML_ANCHOR.findall(line))
    return anchors


def _targets(line: str) -> list[str]:
    prose = CODE_SPAN.sub(lambda match: " " * len(match.group(0)), line)
    found = [match.group(1) for match in INLINE_LINK.finditer(prose)]
    reference = REFERENCE.match(prose)
    if reference:
        found.append(reference.group(1))
    return [target[1:-1] if target.startswith("<") else target for target in found]


class Repository:
    def __init__(self, root: Path, files: set[str]) -> None:
        self.root = root
        self.files = files
        self.dirs = {"."}
        for path in files:
            parent = posixpath.dirname(path)
            while parent and parent not in self.dirs:
                self.dirs.add(parent)
                parent = posixpath.dirname(parent)
        self._anchors: dict[str, set[str]] = {}

    def text(self, path: str) -> str:
        return (self.root / path).read_text(encoding="utf-8", errors="replace")

    def anchors(self, path: str) -> set[str]:
        if path not in self._anchors:
            self._anchors[path] = _anchors(self.text(path))
        return self._anchors[path]

    def problem(self, page: str, target: str) -> str | None:
        """Why ``target``, linked from ``page``, does not resolve; None when it does."""
        location, _, fragment = target.partition("#")
        location = unquote(location.split("?", 1)[0])
        if location.startswith("/"):
            resolved = posixpath.normpath(location.lstrip("/") or ".")
        elif location:
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(page), location))
        else:
            resolved = page
        if resolved not in self.files and resolved not in self.dirs:
            return "no such file"
        if (
            fragment
            and resolved in self.files
            and resolved.endswith(".md")
            and unquote(fragment) not in self.anchors(resolved)
        ):
            return "no such anchor"
        return None


def main() -> int:
    top = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, encoding="utf-8", check=False
    )
    if top.returncode != 0:
        print(f"check_md_links: not a git work tree: {Path.cwd()}", file=sys.stderr)
        return 2
    root = Path(top.stdout.strip())
    listing = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True
    ).stdout.decode("utf-8")
    repository = Repository(root, {path for path in listing.split("\0") if path})

    pages = sorted(path for path in repository.files if _in_scope(path))
    broken: list[str] = []
    checked = 0
    for page in pages:
        for number, line in _prose_lines(repository.text(page)):
            for target in _targets(line):
                if not target or SCHEME.match(target) or target.startswith("//"):
                    continue
                checked += 1
                problem = repository.problem(page, target)
                if problem:
                    broken.append(f"{page}:{number}: {target}: {problem}")

    if broken:
        print("\n".join(broken))
        print(f"{len(broken)} broken relative link{'' if len(broken) == 1 else 's'}.")
        return 1
    print(f"All {checked} relative links in {len(pages)} Markdown files resolve.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
