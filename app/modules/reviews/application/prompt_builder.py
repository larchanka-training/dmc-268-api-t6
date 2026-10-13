"""Pure rendering of the immutable review prompt envelope."""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal
from xml.sax.saxutils import escape

from app.common.application.languages import LANGUAGE_BY_SUFFIX
from app.modules.reviews.application.policy_snapshot import (
    CallManifest,
    ManifestBlock,
    PolicySnapshot,
    StageName,
)

type LineType = Literal["added", "removed", "context"]

# Commit messages are a fixed cost outside the diff budget: keep them bounded.
MAX_COMMIT_MESSAGES = 20
MAX_COMMIT_MESSAGE_CHARS = 500
type FileStatus = Literal[
    "added", "modified", "removed", "renamed", "copied", "changed", "unchanged"
]


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
    """One visible changed file in the review input.

    ``total_lines`` is set only for a block cut to fit the token budget: it is the
    number of diff lines before the cut, and the block ends with the truncation trailer.
    """

    path: str
    status: FileStatus
    lines: tuple[DiffLine, ...]
    total_lines: int | None = None

    @property
    def language(self) -> str | None:
        """The file's language by extension, like ``FileDiff.language`` (SD §9)."""
        return LANGUAGE_BY_SUFFIX.get(PurePosixPath(self.path).suffix.lower())


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
    head_sha: str | None = None
    commit_messages: tuple[str, ...] = ()
    commit_messages_incomplete: bool = False


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
    policy_snapshot: PolicySnapshot | None = None


@dataclass(frozen=True)
class ReviewPrompt:
    """The two provider messages: the prompt version, then the input envelope."""

    system: str
    user: str


def review_rule_from_stored(value: Mapping[str, object]) -> ReviewRule:
    """Convert a validated persisted rule into the prompt representation."""
    name = value.get("name")
    include = value.get("include")
    exclude = value.get("exclude")
    checks = value.get("checks")
    if not (
        isinstance(name, str)
        and isinstance(include, list)
        and isinstance(exclude, list)
        and isinstance(checks, list)
        and all(isinstance(item, str) for item in include)
        and all(isinstance(item, str) for item in exclude)
        and all(isinstance(item, str) for item in checks)
    ):
        raise ValueError("stored rule has invalid prompt shape")
    return ReviewRule(
        name=name,
        include=tuple(include),
        exclude=tuple(exclude),
        checks=tuple(checks),
    )


class PromptBuilder:
    """Render the stable XML input contract after the system prompt.

    This class intentionally has no repository, provider or database dependency:
    callers snapshot all input first and can therefore cache the stable prefix.
    """

    def build(self, context: ReviewContext) -> str:
        """Build the exact system-to-omitted-files envelope in cache order."""
        prompt = self.build_prompt(context)
        return "\n".join((prompt.system, prompt.user))

    def build_prompt(self, context: ReviewContext) -> ReviewPrompt:
        """Split the same envelope into the system message and the user message.

        The prompt version (with its few-shot examples) is the system message; rules,
        AGENTS.md and conventions open the user message, so the provider's cacheable
        prefix does not depend on the diff (SD §10).
        """
        return ReviewPrompt(system=context.system, user=self.render_input(context))

    def render_input(self, context: ReviewContext) -> str:
        """Render the tags after the system prompt, in the documented order."""
        if context.policy_snapshot is not None:
            rules_part = self._render_policy_rules(context.policy_snapshot)
            agents_part = self._render_policy_agents(context.policy_snapshot)
        else:
            rules_part = self._render_rules(context.rules)
            agents_part = _container("agents_md", _text(context.agents_md or ""))

        return "\n".join(
            (
                rules_part,
                agents_part,
                self._render_conventions(context.conventions),
                self._render_pr_meta(context.pr_meta),
                self._render_changed_files(context.changed_files),
                _container(
                    "omitted_files", "\n".join(_text(path) for path in context.omitted_files)
                ),
            )
        )

    @staticmethod
    def _render_policy_rules(snapshot: PolicySnapshot) -> str:
        doc = snapshot.rules
        if not doc.is_present or not (doc.text or "").strip():
            return (
                '<repository_rules block_id="policy-rules" '
                'path=".review/rules.md" status="absent" />'
            )
        text = doc.text or ""
        lines = [
            f'<line n="{index}">{_text(line)}</line>'
            for index, line in enumerate(text.splitlines(), start=1)
        ]
        content = "\n".join(lines)
        path_attr = _attribute(doc.path)
        return (
            f'<repository_rules block_id="policy-rules" path="{path_attr}">\n'
            f"{content}\n"
            "</repository_rules>"
        )

    @staticmethod
    def _render_policy_agents(snapshot: PolicySnapshot) -> str:
        doc = snapshot.agents
        if not doc.is_present or not (doc.text or "").strip():
            return (
                '<project_conventions_document block_id="policy-agents" '
                'path="AGENTS.md" status="absent" />'
            )
        text = doc.text or ""
        lines = [
            f'<line n="{index}">{_text(line)}</line>'
            for index, line in enumerate(text.splitlines(), start=1)
        ]
        content = "\n".join(lines)
        path_attr = _attribute(doc.path)
        return (
            f'<project_conventions_document block_id="policy-agents" path="{path_attr}">\n'
            f"{content}\n"
            "</project_conventions_document>"
        )

    def build_manifest(
        self,
        context: ReviewContext,
        *,
        call_id: uuid.UUID,
        stage: StageName = "review",
    ) -> CallManifest:
        """Construct the exact CallManifest presented in this prompt."""
        run_id = context.policy_snapshot.run_id if context.policy_snapshot else uuid.uuid4()
        blocks: list[ManifestBlock] = []

        if context.policy_snapshot is not None:
            rules_doc = context.policy_snapshot.rules
            if rules_doc.is_present and (rules_doc.line_count or 0) > 0:
                line_count = rules_doc.line_count or 0
                blocks.append(
                    ManifestBlock(
                        block_id="policy-rules",
                        source_role="policy_document",
                        source_kind="repository_rules",
                        path=rules_doc.path,
                        revision=context.policy_snapshot.policy_base_sha,
                        content_sha256=rules_doc.text_sha256,
                        start_line=1,
                        end_line=line_count,
                        shown_line_count=line_count,
                    )
                )

            agents_doc = context.policy_snapshot.agents
            if agents_doc.is_present and (agents_doc.line_count or 0) > 0:
                line_count = agents_doc.line_count or 0
                blocks.append(
                    ManifestBlock(
                        block_id="policy-agents",
                        source_role="policy_document",
                        source_kind="project_conventions_document",
                        path=agents_doc.path,
                        revision=context.policy_snapshot.policy_base_sha,
                        content_sha256=agents_doc.text_sha256,
                        start_line=1,
                        end_line=line_count,
                        shown_line_count=line_count,
                    )
                )

        for file in context.changed_files:
            slug = re.sub(r"[^a-zA-Z0-9]+", "-", file.path).strip("-")
            block_id = f"file-{slug}"
            line_numbers = [line.number for line in file.lines]
            start_line = min(line_numbers) if line_numbers else None
            end_line = max(line_numbers) if line_numbers else None
            blocks.append(
                ManifestBlock(
                    block_id=block_id,
                    source_role="diff",
                    source_kind="diff",
                    path=file.path,
                    start_line=start_line,
                    end_line=end_line,
                    shown_line_count=len(file.lines),
                )
            )

        return CallManifest(
            call_id=call_id,
            run_id=run_id,
            stage=stage,
            blocks=tuple(blocks),
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
        elements = [_element(name, _text(value)) for name, value in values]
        if meta.head_sha is not None:
            elements.append(_element("head_sha", _text(meta.head_sha)))
        if meta.commit_messages or meta.commit_messages_incomplete:
            shown = meta.commit_messages[:MAX_COMMIT_MESSAGES]
            commits = [_element("commit", _text(shorten_commit_message(item))) for item in shown]
            if len(meta.commit_messages) > len(shown):
                hidden = len(meta.commit_messages) - len(shown)
                commits.append(_element("commit", f"[{hidden} more commits omitted]"))
            if meta.commit_messages_incomplete:
                commits.append(_element("commit", "[Additional commits may be omitted]"))
            elements.append(_container("commit_messages", "\n".join(commits)))
        return _container("pr_meta", "\n".join(elements))

    @staticmethod
    def _render_changed_files(files: tuple[ChangedFile, ...]) -> str:
        return _container("changed_files", "\n".join(render_changed_file(file) for file in files))


def render_changed_line(line: DiffLine) -> str:
    """Render one diff line for both the prompt and its exact length budget."""
    return f'<line n="{line.number}" type="{line.type}">{_text(line.content)}</line>'


def render_changed_file(file: ChangedFile) -> str:
    """Render one changed-file block for the prompt and length accounting."""
    body = [render_changed_line(line) for line in file.lines]
    if file.total_lines is not None and file.total_lines > len(file.lines):
        body.append(truncation_trailer(len(file.lines), file.total_lines))
    attributes = f'path="{_attribute(file.path)}" status="{file.status}"'
    if file.language is not None:
        attributes += f' language="{_attribute(file.language)}"'
    return f"<file {attributes}>\n" + "\n".join(body) + "\n</file>"


def shorten_commit_message(message: str) -> str:
    """Bound commit text by Unicode characters, including its truncation ellipsis."""
    if len(message) <= MAX_COMMIT_MESSAGE_CHARS:
        return message
    return message[: MAX_COMMIT_MESSAGE_CHARS - 1] + "…"


def truncation_trailer(shown: int, total: int) -> str:
    """The cut-file trailer of review/README.md "Input envelope", in block-line positions."""
    return (
        f"[Showing lines 1-{shown} of {total} total. Use offset={shown + 1} to continue reading.]"
    )


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


def render_custom_instructions(rules: tuple[ReviewRule, ...]) -> str:
    """The ``<custom_instructions>`` block shared by the review and conventions prompts."""
    return PromptBuilder._render_rules(rules)


def xml_container(name: str, content: str) -> str:
    return _container(name, content)


def xml_text(value: str) -> str:
    return _text(value)


def xml_attribute(value: str) -> str:
    return _attribute(value)


def _container(name: str, content: str) -> str:
    return f"<{name}>\n{content}\n</{name}>"


def _element(name: str, content: str) -> str:
    return f"<{name}>{content}</{name}>"


def _text(value: str) -> str:
    return escape(value)


def _attribute(value: str) -> str:
    return escape(value, {'"': "&quot;", "'": "&apos;"})
