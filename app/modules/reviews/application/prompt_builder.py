"""Pure rendering of the immutable review prompt envelope."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from xml.sax.saxutils import escape

type LineType = Literal["added", "removed", "context"]
type FileStatus = Literal["added", "modified", "removed", "renamed"]


@dataclass(frozen=True)
class ReviewRule:
    """One active custom rule, already validated during repository onboarding."""

    name: str
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    checks: tuple[str, ...]


@dataclass(frozen=True)
class DiffLine:
    """A numbered line in a unified diff hunk."""

    number: int
    type: LineType
    content: str


@dataclass(frozen=True)
class ChangedFile:
    """One visible changed file in the review input."""

    path: str
    status: FileStatus
    lines: tuple[DiffLine, ...]


@dataclass(frozen=True)
class RepoConventions:
    """The cacheable conventions that guide a review."""

    key_patterns: tuple[str, ...]
    recommendations: tuple[str, ...]


@dataclass(frozen=True)
class PullRequestMeta:
    """Provider-neutral pull request metadata included in the prompt tail."""

    title: str
    description: str | None
    author: str
    source_branch: str
    target_branch: str
    labels: tuple[str, ...]
    files_changed: int
    lines_added: int
    lines_removed: int
    is_draft: bool
    is_fork: bool


@dataclass(frozen=True)
class ReviewContext:
    """All immutable values needed to assemble one model request."""

    system: str
    rules: tuple[ReviewRule, ...]
    agents_md: str | None
    conventions: RepoConventions
    pr_meta: PullRequestMeta
    changed_files: tuple[ChangedFile, ...]
    omitted_files: tuple[str, ...]


class PromptBuilder:
    """Render the stable XML input contract after the system prompt.

    This class intentionally has no repository, provider or database dependency:
    callers snapshot all input first and can therefore cache the stable prefix.
    """

    def build(self, context: ReviewContext) -> str:
        """Build the exact system-to-omitted-files envelope in cache order."""
        return "\n".join(
            (
                context.system,
                self._render_rules(context.rules),
                _container("agents_md", _text(context.agents_md or "")),
                self._render_conventions(context.conventions),
                self._render_pr_meta(context.pr_meta),
                self._render_changed_files(context.changed_files),
                _container(
                    "omitted_files", "\n".join(_text(path) for path in context.omitted_files)
                ),
            )
        )

    @staticmethod
    def _render_rules(rules: tuple[ReviewRule, ...]) -> str:
        blocks = []
        for rule in rules:
            attributes = " ".join(
                (
                    f'name="{_attribute(rule.name)}"',
                    f'include="{_attribute(" ".join(rule.include))}"',
                    f'exclude="{_attribute(" ".join(rule.exclude))}"',
                )
            )
            checks = "\n".join(
                f"{index}. {_text(check)}" for index, check in enumerate(rule.checks, start=1)
            )
            blocks.append(f"<rule {attributes}>\n{checks}\n</rule>")
        return _container("custom_instructions", "\n".join(blocks))

    @staticmethod
    def _render_conventions(conventions: RepoConventions) -> str:
        patterns = "\n".join(_element("pattern", _text(item)) for item in conventions.key_patterns)
        recommendations = "\n".join(
            _element("recommendation", _text(item)) for item in conventions.recommendations
        )
        return _container(
            "repo_conventions",
            "\n".join(
                (
                    _container("key_patterns", patterns),
                    _container("recommendations", recommendations),
                )
            ),
        )

    @staticmethod
    def _render_pr_meta(meta: PullRequestMeta) -> str:
        values = (
            ("title", meta.title),
            ("description", meta.description or ""),
            ("author", meta.author),
            ("source_branch", meta.source_branch),
            ("target_branch", meta.target_branch),
            ("labels", " ".join(meta.labels)),
            ("files_changed", str(meta.files_changed)),
            ("lines_added", str(meta.lines_added)),
            ("lines_removed", str(meta.lines_removed)),
            ("draft", str(meta.is_draft).lower()),
            ("fork", str(meta.is_fork).lower()),
        )
        return _container(
            "pr_meta", "\n".join(_element(name, _text(value)) for name, value in values)
        )

    @staticmethod
    def _render_changed_files(files: tuple[ChangedFile, ...]) -> str:
        rendered = []
        for file in files:
            lines = "\n".join(
                f'<line n="{line.number}" type="{line.type}">{_text(line.content)}</line>'
                for line in file.lines
            )
            rendered.append(
                f'<file path="{_attribute(file.path)}" status="{file.status}">\n{lines}\n</file>'
            )
        return _container("changed_files", "\n".join(rendered))


_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def parse_unified_diff(diff: str) -> tuple[ChangedFile, ...]:
    """Parse visible unified-diff lines without changing their anchor semantics."""
    parsed: list[ChangedFile] = []
    path: str | None = None
    status: FileStatus = "modified"
    lines: list[DiffLine] = []
    old_number = 0
    new_number = 0
    in_hunk = False

    def finish() -> None:
        nonlocal path
        if path is not None:
            parsed.append(ChangedFile(path=path, status=status, lines=tuple(lines)))
        path = None

    for raw_line in diff.splitlines():
        if raw_line.startswith("diff --git "):
            finish()
            path = _path_from_diff_header(raw_line)
            status = "modified"
            lines = []
            in_hunk = False
            continue
        if path is None:
            continue
        if raw_line.startswith("new file mode "):
            status = "added"
            continue
        if raw_line.startswith("deleted file mode "):
            status = "removed"
            continue
        if raw_line.startswith("rename to "):
            path = raw_line.removeprefix("rename to ")
            status = "renamed"
            continue
        header = _HUNK_HEADER.match(raw_line)
        if header is not None:
            old_number = int(header.group(1))
            new_number = int(header.group(2))
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if raw_line.startswith("\\ No newline at end of file"):
            continue
        if raw_line.startswith("+"):
            lines.append(DiffLine(number=new_number, type="added", content=raw_line[1:]))
            new_number += 1
            continue
        if raw_line.startswith("-"):
            lines.append(DiffLine(number=old_number, type="removed", content=raw_line[1:]))
            old_number += 1
            continue
        if raw_line.startswith(" "):
            lines.append(DiffLine(number=new_number, type="context", content=raw_line[1:]))
            old_number += 1
            new_number += 1
    finish()
    return tuple(parsed)


def _path_from_diff_header(header: str) -> str:
    """Extract the new side path; quote handling stays the provider's responsibility."""
    _, _, remainder = header.partition(" b/")
    if not remainder:
        raise ValueError(f"invalid unified diff header: {header}")
    return remainder


def _container(name: str, content: str) -> str:
    return f"<{name}>\n{content}\n</{name}>"


def _element(name: str, content: str) -> str:
    return f"<{name}>{content}</{name}>"


def _text(value: str) -> str:
    return escape(value)


def _attribute(value: str) -> str:
    return escape(value, {'"': "&quot;", "'": "&apos;"})
