"""Pure rendering of the immutable review prompt envelope."""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Literal
from xml.sax.saxutils import escape

from app.common.application.languages import LANGUAGE_BY_SUFFIX
from app.modules.reviews.application.commit_prefix_limits import (
    MAX_COMMIT_MESSAGE_CHARS,
    MAX_COMMIT_MESSAGES,
)
from app.modules.reviews.application.context_provenance import SourceRole
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.policy_snapshot import (
    CallManifest,
    ManifestBlock,
    PolicyDocument,
    PolicySnapshot,
    RecoveryKind,
    StageName,
)
from app.modules.reviews.application.unified_diff import split_lf

if TYPE_CHECKING:
    from app.modules.reviews.application.prompt_budget import TokenCounter
    from app.modules.reviews.application.review_context import ContextBlock, RetrievedBlock

type LineType = Literal["added", "removed", "context"]

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


DEFAULT_BACKEND_V1_CHECKSUM = "282d61c2da897152046db742fbd9bfda061ba1c71ce32759d47e042c06b0ff52"
DEFAULT_BACKEND_V1_RAW_SHA256 = "94aa971ae0d0f97af86dba5139ce70bb3488dc2aa21f6fc03db98f2ac98bec28"
DEFAULT_FRONTEND_V1_CHECKSUM = "32673f59b6f0f3f96155e4012dcc42718b0814c473cc255c70e69f63660dcb4a"
DEFAULT_FRONTEND_V1_RAW_SHA256 = "8327c437b1edada1415fbff214484920959fa4fc1d011c9c7c4baf80062c386c"

# Canonical stable check IDs per pinned default rule artifact (RFS §6.2 :555-604).
BACKEND_V1_RULE_CHECK_IDS: dict[str, tuple[str, ...]] = {
    "Naming Consistency": ("N1", "N2", "N3", "N4"),
    "Error Handling Standards": ("E1", "E2", "E3", "E4"),
    "Complex Function Improvement Suggestions": ("C1", "C2", "C3", "C4"),
    "Meaningful Comments for Complex Logic": ("D1", "D2", "D3"),
    "Test Coverage for Complex Functions": ("T1", "T2", "T3", "T4"),
    "Clean Architecture Boundaries": ("A1", "A2", "A3", "A4", "A5"),
}

FRONTEND_V1_RULE_CHECK_IDS: dict[str, tuple[str, ...]] = {
    "Naming Consistency": ("N1", "N2", "N5", "N4"),
    "Error Handling Standards": ("E1", "E2", "E3", "E5"),
    "Complex Function Improvement Suggestions": ("C1", "C2", "C3", "C5"),
    "Meaningful Comments for Complex Logic": ("D1", "D2", "D3"),
    "Test Coverage for Complex Functions": ("T5", "T3", "T4"),
    "FSD Layer Boundaries": ("FSD1", "FSD2", "FSD3", "FSD4", "FSD5"),
}

PINNED_DEFAULTS_CHECK_IDS: dict[str, dict[str, tuple[str, ...]]] = {
    DEFAULT_BACKEND_V1_CHECKSUM: BACKEND_V1_RULE_CHECK_IDS,
    DEFAULT_BACKEND_V1_RAW_SHA256: BACKEND_V1_RULE_CHECK_IDS,
    "default-backend.v1": BACKEND_V1_RULE_CHECK_IDS,
    DEFAULT_FRONTEND_V1_CHECKSUM: FRONTEND_V1_RULE_CHECK_IDS,
    DEFAULT_FRONTEND_V1_RAW_SHA256: FRONTEND_V1_RULE_CHECK_IDS,
    "default-frontend.v1": FRONTEND_V1_RULE_CHECK_IDS,
}


def get_rule_check_ids(
    rule: ReviewRule,
    *,
    defaults_checksum: str | None = None,
) -> tuple[str, ...]:
    """Return stable check IDs for a rule from the pinned defaults version (RFS §6.2, §8.2)."""
    if not defaults_checksum or defaults_checksum not in PINNED_DEFAULTS_CHECK_IDS:
        raise PolicyError(
            PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR,
            f"Unknown service defaults checksum: {defaults_checksum!r}",
        )
    rule_map = PINNED_DEFAULTS_CHECK_IDS[defaults_checksum]
    if rule.name not in rule_map:
        raise PolicyError(
            PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR,
            f"Rule {rule.name!r} is not part of pinned service defaults {defaults_checksum!r}",
        )
    canonical_ids = rule_map[rule.name]
    if len(rule.checks) != len(canonical_ids):
        raise PolicyError(
            PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR,
            f"Rule {rule.name!r} has {len(rule.checks)} checks, "
            f"expected {len(canonical_ids)} for pinned defaults {defaults_checksum!r}",
        )
    return canonical_ids


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
    diff_old_sha: str | None = None
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
    severity_threshold: Literal["all", "warning", "critical"] = "all"
    # Snapshot path only (R2): the L2-L4 blocks of the attempt's context payload, shown as
    # numbered excerpts after the changed files; L1 stays ``changed_files``.
    context_blocks: tuple[ContextBlock, ...] = ()
    # The RAG slot of the payload, kept only for a prompt with a retrieved section; #144
    # renders it, nothing shows it before.
    retrieved: tuple[RetrievedBlock, ...] = ()
    # Snapshot path only (P6-4): changed files the ignore policy withheld, shown as one
    # counter and never by name (RULES_FORMAT_SPEC §5.3).
    excluded_files: int = 0


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
        blocks: list[str] = []
        include_block_id = context.policy_snapshot is not None
        if context.policy_snapshot is not None:
            blocks.append(self.render_policy_rules(context.policy_snapshot))
            blocks.append(self.render_policy_agents(context.policy_snapshot))
            if context.rules:
                blocks.append(
                    self.render_service_defaults(
                        context.rules,
                        defaults_checksum=context.policy_snapshot.defaults_checksum,
                    )
                )
        else:
            blocks.append(self._render_rules(context.rules))
            blocks.append(_container("agents_md", _text(context.agents_md or "")))

        blocks.extend(
            (
                self.render_conventions(context.conventions),
                self._render_pr_meta(context.pr_meta),
                self.render_changed_files(context.changed_files, include_block_id=include_block_id),
            )
        )
        if context.context_blocks:
            blocks.append(render_context_excerpts(context.context_blocks))
        blocks.append(
            _container("omitted_files", "\n".join(_text(path) for path in context.omitted_files))
        )
        if context.excluded_files:
            blocks.append(f'<excluded_files count="{context.excluded_files}" />')
        return "\n".join(blocks)

    @staticmethod
    def render_policy_rules(snapshot: PolicySnapshot) -> str:
        """Render the numbered repository-rules block, including absence and empty state."""
        return _render_policy_document(
            snapshot.rules, tag="repository_rules", block_id="policy-rules"
        )

    @staticmethod
    def render_policy_agents(snapshot: PolicySnapshot) -> str:
        """Render the numbered AGENTS.md block, including absence and empty state."""
        return _render_policy_document(
            snapshot.agents, tag="project_conventions_document", block_id="policy-agents"
        )

    @staticmethod
    def render_service_defaults(
        rules: tuple[ReviewRule, ...],
        *,
        defaults_checksum: str | None = None,
    ) -> str:
        """Render pinned service-default checks with their stable IDs and line coordinates."""
        blocks = []
        line_offset = 1
        seen_check_ids: set[str] = set()
        for rule in rules:
            attributes = " ".join(
                (
                    f'name="{_attribute(rule.name)}"',
                    f'include="{_attribute(" ".join(rule.include))}"',
                    f'exclude="{_attribute(" ".join(rule.exclude))}"',
                )
            )
            check_ids = get_rule_check_ids(rule, defaults_checksum=defaults_checksum)
            checks = []
            for index, check in enumerate(rule.checks):
                c_id = check_ids[index]
                if c_id in seen_check_ids:
                    raise PolicyError(
                        PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR,
                        f"Duplicate service defaults check ID {c_id!r}",
                    )
                seen_check_ids.add(c_id)
                checks.append(
                    f'<check id="{c_id}"><line n="{line_offset}">{_text(check)}</line></check>'
                )
                line_offset += 1
            checks_content = "\n".join(checks)
            blocks.append(f"<rule {attributes}>\n{checks_content}\n</rule>")
        content = "\n".join(blocks)
        return f'<service_defaults block_id="service-defaults">\n{content}\n</service_defaults>'

    def build_manifest(
        self,
        context: ReviewContext,
        *,
        call_id: uuid.UUID,
        stage: StageName = "review",
        recovery_kind: RecoveryKind = "none",
        counter: TokenCounter | None = None,
    ) -> CallManifest:
        """Construct the exact CallManifest presented in this prompt.

        With ``counter`` (the snapshot path's pinned counter) every block carries the
        tokens of its rendered text.
        """
        run_id = context.policy_snapshot.run_id if context.policy_snapshot is not None else None
        blocks: list[ManifestBlock] = []

        def tokens(rendered: str) -> int | None:
            return None if counter is None else counter.count(rendered)

        if context.policy_snapshot is not None:
            rules_doc = context.policy_snapshot.rules
            if rules_doc.is_present:
                line_count = rules_doc.line_count or 0
                if line_count > 0:
                    blocks.append(
                        ManifestBlock(
                            block_id="policy-rules",
                            source_role=SourceRole.POLICY_DOCUMENT,
                            source_kind="repository_rules",
                            path=rules_doc.path,
                            revision=context.policy_snapshot.policy_base_sha,
                            content_sha256=rules_doc.text_sha256,
                            start_line=1,
                            end_line=line_count,
                            shown_line_count=line_count,
                            shown_ranges=((1, line_count),),
                            tokens=tokens(self.render_policy_rules(context.policy_snapshot)),
                        )
                    )
                else:
                    blocks.append(
                        ManifestBlock(
                            block_id="policy-rules",
                            source_role=SourceRole.POLICY_DOCUMENT,
                            source_kind="repository_rules",
                            path=rules_doc.path,
                            revision=context.policy_snapshot.policy_base_sha,
                            content_sha256=rules_doc.text_sha256,
                            start_line=None,
                            end_line=None,
                            shown_line_count=0,
                            tokens=tokens(self.render_policy_rules(context.policy_snapshot)),
                        )
                    )

            agents_doc = context.policy_snapshot.agents
            if agents_doc.is_present:
                line_count = agents_doc.line_count or 0
                if line_count > 0:
                    blocks.append(
                        ManifestBlock(
                            block_id="policy-agents",
                            source_role=SourceRole.POLICY_DOCUMENT,
                            source_kind="project_conventions_document",
                            path=agents_doc.path,
                            revision=context.policy_snapshot.policy_base_sha,
                            content_sha256=agents_doc.text_sha256,
                            start_line=1,
                            end_line=line_count,
                            shown_line_count=line_count,
                            shown_ranges=((1, line_count),),
                            tokens=tokens(self.render_policy_agents(context.policy_snapshot)),
                        )
                    )
                else:
                    blocks.append(
                        ManifestBlock(
                            block_id="policy-agents",
                            source_role=SourceRole.POLICY_DOCUMENT,
                            source_kind="project_conventions_document",
                            path=agents_doc.path,
                            revision=context.policy_snapshot.policy_base_sha,
                            content_sha256=agents_doc.text_sha256,
                            start_line=None,
                            end_line=None,
                            shown_line_count=0,
                            tokens=tokens(self.render_policy_agents(context.policy_snapshot)),
                        )
                    )

            if context.rules:
                defaults_lines = sum(len(r.checks) for r in context.rules) or 1
                blocks.append(
                    ManifestBlock(
                        block_id="service-defaults",
                        source_role=SourceRole.SERVICE_DEFAULTS,
                        source_kind="service_defaults",
                        start_line=1,
                        end_line=defaults_lines,
                        shown_line_count=defaults_lines,
                        shown_ranges=((1, defaults_lines),),
                        tokens=None
                        if counter is None
                        else counter.count(
                            self.render_service_defaults(
                                context.rules,
                                defaults_checksum=context.policy_snapshot.defaults_checksum,
                            )
                        ),
                    )
                )

        include_block_id = context.policy_snapshot is not None
        repository_id = (
            None if context.policy_snapshot is None else context.policy_snapshot.repository_id
        )
        for file in context.changed_files:
            block_id = file_block_id(file.path)
            shown_ranges = _calculate_shown_ranges(file.lines)
            start_line = min((r[0] for r in shown_ranges), default=None)
            end_line = max((r[1] for r in shown_ranges), default=None)
            shown_count = sum(r[1] - r[0] + 1 for r in shown_ranges)
            blocks.append(
                ManifestBlock(
                    block_id=block_id,
                    source_role=SourceRole.DIFF,
                    source_kind="source_code",
                    path=file.path,
                    revision=context.pr_meta.head_sha if context.pr_meta is not None else None,
                    side="RIGHT",
                    start_line=start_line,
                    end_line=end_line,
                    shown_line_count=shown_count,
                    shown_ranges=shown_ranges,
                    tokens=tokens(render_changed_file(file, include_block_id=include_block_id)),
                    repository_id=repository_id,
                    truncated=file.total_lines is not None and file.total_lines > len(file.lines),
                )
            )

        for excerpt in context.context_blocks:
            source = excerpt.source
            ranges = tuple((item.start_line, item.end_line) for item in source.ranges)
            blocks.append(
                ManifestBlock(
                    block_id=excerpt.block_id,
                    source_role=source.role,
                    source_kind="source_code",
                    path=source.path,
                    revision=source.revision,
                    side=_manifest_side(source.side),
                    start_line=min((start for start, _ in ranges), default=None),
                    end_line=max((end for _, end in ranges), default=None),
                    shown_line_count=sum(end - start + 1 for start, end in ranges),
                    shown_ranges=ranges,
                    tokens=tokens(_render_excerpt(excerpt)),
                    repository_id=source.repository_id,
                    truncated=excerpt.truncated,
                )
            )

        return CallManifest(
            call_id=call_id,
            run_id=run_id,
            stage=stage,
            recovery_kind=recovery_kind,
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
    def render_conventions(conventions: RepoConventions) -> str:
        """Render the shared repository-conventions envelope."""
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
    def render_changed_files(
        files: tuple[ChangedFile, ...], *, include_block_id: bool = False
    ) -> str:
        """Render changed-file blocks for reviewer/verifier prompts and budget accounting."""
        return _container(
            "changed_files",
            "\n".join(
                render_changed_file(file, include_block_id=include_block_id) for file in files
            ),
        )


def _render_policy_document(document: PolicyDocument, *, tag: str, block_id: str) -> str:
    attributes = f'block_id="{block_id}" path="{_attribute(document.path)}"'
    if not document.is_present:
        return f'<{tag} {attributes} status="absent" />'
    if not (document.text or ""):
        return f'<{tag} {attributes} status="empty" />'
    lines = "\n".join(
        f'<line n="{index}">{_text(line)}</line>'
        for index, line in enumerate(_split_policy_lines(document.text or ""), start=1)
    )
    return f"<{tag} {attributes}>\n{lines}\n</{tag}>"


def _split_policy_lines(text: str) -> list[str]:
    """Split policy document text strictly by newline to match policy loader counts."""
    if not text:
        return []
    if text.endswith("\n"):
        text = text[:-1]
    return text.split("\n")


def file_block_id(path: str) -> str:
    """Deterministic, collision-free block_id for changed files in manifest and prompt."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", path).strip("-")[:64].rstrip("-")
    digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:8]
    return f"file-{slug}-{digest}"


def _calculate_shown_ranges(lines: tuple[DiffLine, ...]) -> tuple[tuple[int, int], ...]:
    """Calculate contiguous shown line ranges for the visible file diff (RIGHT-side only)."""
    right_numbers = sorted({line.number for line in lines if line.type != "removed"})
    if not right_numbers:
        return ()
    ranges: list[tuple[int, int]] = []
    start = right_numbers[0]
    prev = right_numbers[0]
    for num in right_numbers[1:]:
        if num == prev + 1:
            prev = num
        else:
            ranges.append((start, prev))
            start = num
            prev = num
    ranges.append((start, prev))
    return tuple(ranges)


def render_changed_line(line: DiffLine) -> str:
    """Render one diff line for both the prompt and its exact length budget."""
    return f'<line n="{line.number}" type="{line.type}">{_text(line.content)}</line>'


def render_changed_file(file: ChangedFile, *, include_block_id: bool = False) -> str:
    """Render one changed-file block for the prompt and length accounting."""
    body = [render_changed_line(line) for line in file.lines]
    if file.total_lines is not None and file.total_lines > len(file.lines):
        body.append(truncation_trailer(len(file.lines), file.total_lines))
    if include_block_id:
        block_id = file_block_id(file.path)
        attributes = (
            f'block_id="{_attribute(block_id)}" path="{_attribute(file.path)}" '
            f'status="{file.status}"'
        )
    else:
        attributes = f'path="{_attribute(file.path)}" status="{file.status}"'
    if file.language is not None:
        attributes += f' language="{_attribute(file.language)}"'
    return f"<file {attributes}>\n" + "\n".join(body) + "\n</file>"


def render_context_excerpts(blocks: tuple[ContextBlock, ...]) -> str:
    """The L2-L4 blocks of the context payload, one excerpt each with its source lines."""
    return _container("context_excerpts", "\n".join(_render_excerpt(block) for block in blocks))


def _render_excerpt(block: ContextBlock) -> str:
    source = block.source
    attributes = [f'block_id="{_attribute(block.block_id)}"', f'level="{block.level.value}"']
    if source.path is not None:
        attributes.append(f'path="{_attribute(source.path)}"')
    if source.revision is not None:
        attributes.append(f'revision="{_attribute(source.revision)}"')
    if source.side is not None:
        attributes.append(f'side="{source.side.value}"')
    attributes.append(f'representation="{block.representation.value}"')
    numbers = [
        number for item in source.ranges for number in range(item.start_line, item.end_line + 1)
    ]
    # CONTRACT: the stage cuts a block's text from exactly its source ranges, one text line
    # per source line (review_context.py ``_excerpt``, ``_symbol_block``). A block that breaks
    # it would show lines under numbers they do not have: the call is refused, terminally.
    text_lines = block.text.split("\n")
    try:
        lines = (
            [
                f'<line n="{number}">{_text(line)}</line>'
                for number, line in zip(numbers, text_lines, strict=True)
            ]
            if numbers
            else []
        )
    except ValueError as error:
        raise PolicyError(
            PolicyErrorCode.CONTEXT_VIOLATION,
            f"Excerpt '{block.block_id}' has {len(text_lines)} text lines "
            f"for {len(numbers)} source lines",
        ) from error
    return f"<excerpt {' '.join(attributes)}>\n" + "\n".join(lines) + "\n</excerpt>"


def _manifest_side(side: str | None) -> Literal["LEFT", "RIGHT"] | None:
    if side == "LEFT":
        return "LEFT"
    if side == "RIGHT":
        return "RIGHT"
    return None


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

    # Only LF ends a line: U+2028, FF and a lone CR can be literal content of a line.
    for raw_line in (line.removesuffix("\r") for line in split_lf(diff)):
        if raw_line.startswith("diff --git "):
            finish()
            path = _path_from_diff_header(raw_line)
            status = "modified"
            lines = []
            in_hunk = False
            continue
        if path is None:
            continue
        # The extended header ends at the first hunk; inside one such a line is content.
        if not in_hunk and raw_line.startswith("new file mode "):
            status = "added"
            continue
        if not in_hunk and raw_line.startswith("deleted file mode "):
            status = "removed"
            continue
        if not in_hunk and raw_line.startswith("rename to "):
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
    """Extract the new side path; quote handling stays the provider's responsibility.

    ``a/<P> b/<P>`` is split in the middle, so a path that holds `` b/`` stays whole. Two
    different names (a rename or a copy) keep the text after the first `` b/`` as a
    provisional path; a ``rename to`` line of the extended header replaces it.
    """
    rest = header.removeprefix("diff --git ")
    half = (len(rest) - len("a/ b/")) // 2
    # ``half > 0``: ``diff --git a/ b/`` names no path and stays an invalid header.
    if (
        half > 0
        and rest.startswith("a/")
        and rest[2 + half : 5 + half] == " b/"
        and rest[2 : 2 + half] == rest[5 + half :]
    ):
        return rest[5 + half :]
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
