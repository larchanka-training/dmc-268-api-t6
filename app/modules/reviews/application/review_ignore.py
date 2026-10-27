"""The ``review-ignore`` block of ``.review/rules.md`` and its path matcher.

RULES_FORMAT_SPEC §4-5. The block is the only machine-read part of the rules document:
one top-level fenced block whose lines are path masks. The mask language is its own
(``*`` inside a segment, ``**`` for whole segments): it is neither ``.gitignore`` nor a
shell glob. Ignore decisions are kept apart from the diff map and keyed by ``file_id``.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256

import markdown_it
from markdown_it import MarkdownIt

from app.modules.reviews.application.diff_map import ChangeType, DiffMap, is_supported_path
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.policy_snapshot import RULES_DOCUMENT_PATH, PolicySnapshot

PARSER_PROFILE = "rules-markdown/v1"
PARSER_VERSION: str = markdown_it.__version__
MATCHER_VERSION = "review-ignore/v1"
LIMITS_VERSION = "policy-limits/v1"
BUILTIN_EXCLUSIONS_VERSION = "builtin-exclusions/v1"
MAX_PATTERN_LINES = 256
MAX_PATTERN_BYTES = 256
MAX_PATTERN_SEGMENTS = 32
MAX_TRANSITIONS_PER_PATH = 8_000_000
MAX_TRANSITIONS_PER_RUN = 80_000_000

_LOGGER = logging.getLogger(__name__)
_INFO = "review-ignore"
_FORBIDDEN_LITERALS = frozenset("\\!?[]{}")
_DRIVE_PREFIX = re.compile(r"[A-Za-z]:")
_CLOSING_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})[ \t]*")


@dataclass(frozen=True)
class IgnorePatternLine:
    """One non-empty line of the block, kept for diagnostics."""

    pattern: str
    line: int


@dataclass(frozen=True)
class IgnorePolicy:
    """The user ignore policy of one run: unique valid masks in code point order."""

    patterns: tuple[str, ...]
    source_lines: tuple[IgnorePatternLine, ...]
    digest: str


@dataclass(frozen=True)
class IgnoreDecision:
    """A changed file excluded from every model input, and the path and mask that did it."""

    file_id: str
    matched_pattern: str
    matched_path: str


@dataclass(frozen=True)
class IgnoreResult:
    """Decisions for one diff map; ``denied_paths`` taints every name of an excluded file."""

    decisions: tuple[IgnoreDecision, ...]
    denied_paths: frozenset[str]

    def decision_for(self, file_id: str) -> IgnoreDecision | None:
        return next((item for item in self.decisions if item.file_id == file_id), None)


def parse_review_ignore(
    rules_text: str | None, *, policy_base_sha: str | None = None
) -> IgnorePolicy:
    """Read the masks of the rules document; an absent document has an empty policy."""

    def error(
        code: PolicyErrorCode,
        message: str,
        *,
        line: int | None = None,
        column: int | None = None,
        pattern_index: int | None = None,
        limit: int | None = None,
        actual: int | None = None,
        excerpt: str | None = None,
    ) -> PolicyError:
        return PolicyError(
            code,
            message,
            source_path=RULES_DOCUMENT_PATH,
            policy_base_sha=policy_base_sha,
            line=line,
            column=column,
            pattern_index=pattern_index,
            limit=limit,
            actual=actual,
            excerpt=excerpt,
        )

    if not rules_text:
        return _policy(())
    source = rules_text.split("\n")
    block = None
    for token in MarkdownIt("commonmark").parse(rules_text):
        if token.type != "fence" or token.map is None:
            continue
        info = token.info.strip(" \t")
        opening_line = token.map[0] + 1
        if info != _INFO:
            if info.startswith((_INFO + " ", _INFO + "\t")):
                raise error(
                    PolicyErrorCode.IGNORE_INFO_INVALID,
                    "review-ignore does not take additional parameters",
                    line=opening_line,
                )
            continue
        if token.level != 0:
            raise error(
                PolicyErrorCode.IGNORE_BLOCK_NESTED,
                "The review-ignore block must be at the top level of the document",
                line=opening_line,
            )
        if block is not None:
            raise error(
                PolicyErrorCode.IGNORE_BLOCK_DUPLICATE,
                "Only one review-ignore block is allowed",
                line=opening_line,
            )
        closing = source[token.map[1] - 1] if token.map[1] - 1 > token.map[0] else ""
        closed = _CLOSING_FENCE.fullmatch(closing)
        if closed is None or not closed.group(1).startswith(token.markup):
            raise error(
                PolicyErrorCode.IGNORE_BLOCK_UNCLOSED,
                "The review-ignore block has no closing fence",
                line=opening_line,
            )
        block = token

    if block is None or block.map is None:
        return _policy(())
    lines: list[IgnorePatternLine] = []
    for offset, raw in enumerate(block.content.split("\n")):
        pattern = raw.strip(" \t")
        if pattern:
            lines.append(IgnorePatternLine(pattern, block.map[0] + 2 + offset))
    if len(lines) > MAX_PATTERN_LINES:
        raise error(
            PolicyErrorCode.IGNORE_LIMIT_EXCEEDED,
            f"The review-ignore block has more than {MAX_PATTERN_LINES} masks",
            line=block.map[0] + 1,
            limit=MAX_PATTERN_LINES,
            actual=len(lines),
        )
    for index, item in enumerate(lines):
        size = len(item.pattern.encode("utf-8"))
        segments = item.pattern.count("/") + 1
        if size > MAX_PATTERN_BYTES or segments > MAX_PATTERN_SEGMENTS:
            too_long = size > MAX_PATTERN_BYTES
            raise error(
                PolicyErrorCode.IGNORE_LIMIT_EXCEEDED,
                "A review-ignore mask is too long",
                line=item.line,
                pattern_index=index,
                excerpt=item.pattern,
                limit=MAX_PATTERN_BYTES if too_long else MAX_PATTERN_SEGMENTS,
                actual=size if too_long else segments,
            )
        invalid = _invalid_pattern(item.pattern)
        if invalid is not None:
            column, reason = invalid
            raise error(
                PolicyErrorCode.IGNORE_PATTERN_INVALID,
                f"Invalid review-ignore mask: {reason}",
                line=item.line,
                column=column,
                pattern_index=index,
                excerpt=item.pattern,
            )
    return _policy(tuple(lines))


def _policy(lines: tuple[IgnorePatternLine, ...]) -> IgnorePolicy:
    patterns = tuple(sorted({item.pattern for item in lines}))
    digest = ignore_digest(
        patterns,
        matcher_version=MATCHER_VERSION,
        builtin_exclusions_version=BUILTIN_EXCLUSIONS_VERSION,
        limits_version=LIMITS_VERSION,
    )
    return IgnorePolicy(patterns, lines, digest)


def snapshot_ignore_policy(snapshot: PolicySnapshot) -> IgnorePolicy:
    """The ignore policy of a stored snapshot, with the source lines of its masks if it can.

    The snapshot stores the masks, not the lines they came from (RULES_FORMAT_SPEC §7.1):
    they are recovered by parsing its staged rules text again. The lines are diagnostics
    and never fail the run: when the text no longer parses, or gives back other masks or,
    hashed with the versions the snapshot pinned, another ``ignore_digest``, the stored
    masks apply without lines and a warning is logged. Reading the snapshot already checked
    its stored values, so such a mismatch is drift of the parser, its limits or grammar.
    """
    return _snapshot_ignore_policy(
        snapshot.rules.text,
        snapshot.policy_base_sha,
        snapshot.ignore_patterns,
        snapshot.ignore_digest,
        snapshot.matcher_version,
        snapshot.builtin_exclusions_version,
        snapshot.limits_version,
    )


# The guard reads the snapshot before every model call, and a long rules text takes
# hundreds of milliseconds to parse: one parse per snapshot content and process.
@lru_cache(maxsize=64)
def _snapshot_ignore_policy(
    rules_text: str | None,
    policy_base_sha: str,
    patterns: tuple[str, ...],
    digest: str,
    matcher_version: str,
    builtin_exclusions_version: str,
    limits_version: str,
) -> IgnorePolicy:
    try:
        policy = parse_review_ignore(rules_text, policy_base_sha=policy_base_sha)
    except PolicyError as exc:
        reason = exc.code.value
    else:
        if policy.patterns != patterns:
            reason = "the re-parsed masks differ from the stored ones"
        elif (
            ignore_digest(
                policy.patterns,
                matcher_version=matcher_version,
                builtin_exclusions_version=builtin_exclusions_version,
                limits_version=limits_version,
            )
            != digest
        ):
            reason = "the re-parsed digest differs from the stored one"
        else:
            return IgnorePolicy(patterns, policy.source_lines, digest)
    # Only the reason and the base: never the rules text or a mask.
    _LOGGER.warning(
        "Ignore source lines are unavailable, the stored masks apply without them: "
        "%s policy_base_sha=%s",
        reason,
        policy_base_sha,
    )
    return IgnorePolicy(patterns, (), digest)


def ignore_digest(
    patterns: tuple[str, ...],
    *,
    matcher_version: str,
    builtin_exclusions_version: str,
    limits_version: str,
) -> str:
    """``ignore_digest`` of sorted unique masks (RULES_FORMAT_SPEC §7.1).

    The versions are inputs, so a stored snapshot is re-hashed with the ones it recorded;
    the three literals here are not stored, they are part of the formula itself.
    """
    payload = {
        "version": 1,
        "matcher_version": matcher_version,
        "builtin_exclusions_version": builtin_exclusions_version,
        "sorted_unique_patterns": list(patterns),
        "rename_copy_propagation": "both-paths/v1",
        "policy_document_exemption": "base-policy-role-only/v1",
        "limits_version": limits_version,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return sha256(canonical.encode("utf-8")).hexdigest()


def _invalid_pattern(pattern: str) -> tuple[int, str] | None:
    """Return the 1-based column and reason of the first grammar violation, if any."""
    column = 1
    for index, segment in enumerate(pattern.split("/")):
        if segment == "":
            return column, "empty path segment"
        if segment in {".", ".."}:
            return column, "'.' and '..' segments are not allowed"
        if index == 0 and _DRIVE_PREFIX.match(segment):
            return column, "a drive prefix is not allowed"
        if segment != "**":
            for offset, char in enumerate(segment):
                position = column + offset
                if char == "*" and segment[offset + 1 : offset + 2] == "*":
                    return position, "'**' is allowed only as a whole segment"
                if char in _FORBIDDEN_LITERALS:
                    return position, "the character is reserved in a mask"
                category = unicodedata.category(char)
                if category == "Cc" or (category.startswith("Z") and char != " "):
                    return position, "control and non-space whitespace characters are not allowed"
        column += len(segment) + 1
    return None


class IgnoreMatcher:
    """Match repository paths against the policy within the transition budget of a run."""

    def __init__(self, policy: IgnorePolicy, *, policy_base_sha: str | None = None) -> None:
        self._patterns = tuple((pattern, pattern.split("/")) for pattern in policy.patterns)
        self._policy_base_sha = policy_base_sha
        self._results: dict[str, str | None] = {}
        self._run_transitions = 0
        self._path_transitions = 0

    def match(self, path: str) -> str | None:
        """Return the first mask that matches the whole ``path``, or ``None``."""
        if path in self._results:
            return self._results[path]
        if not is_supported_path(path):
            raise PolicyError(
                PolicyErrorCode.PATH_INVALID,
                "A repository path violates the path contract",
                policy_base_sha=self._policy_base_sha,
            )
        self._path_transitions = 0
        segments = path.split("/")
        matched = next(
            (pattern for pattern, mask in self._patterns if self._match_segments(mask, segments)),
            None,
        )
        self._results[path] = matched
        return matched

    def _spend(self) -> None:
        self._path_transitions += 1
        self._run_transitions += 1
        if (
            self._path_transitions > MAX_TRANSITIONS_PER_PATH
            or self._run_transitions > MAX_TRANSITIONS_PER_RUN
        ):
            # The counter that crossed its limit, at the stop: a confirmed excess, a lower
            # bound of the real cost and never shown as exact (RFS §9).
            limit, actual = (
                (MAX_TRANSITIONS_PER_PATH, self._path_transitions)
                if self._path_transitions > MAX_TRANSITIONS_PER_PATH
                else (MAX_TRANSITIONS_PER_RUN, self._run_transitions)
            )
            raise PolicyError(
                PolicyErrorCode.MATCH_BUDGET_EXCEEDED,
                "The review-ignore policy is too expensive to evaluate",
                source_path=RULES_DOCUMENT_PATH,
                policy_base_sha=self._policy_base_sha,
                limit=limit,
                actual=actual,
                unit="переходов",
                actual_is_lower_bound=True,
            )

    def _match_segments(self, mask: list[str], path: list[str]) -> bool:
        # reachable[j]: the mask segments consumed so far can match the first j path segments.
        reachable = [False] * (len(path) + 1)
        reachable[0] = True
        for segment in mask:
            following = [False] * (len(path) + 1)
            if segment == "**":
                seen = False
                for index in range(len(path) + 1):
                    self._spend()
                    seen = seen or reachable[index]
                    following[index] = seen
            else:
                for index in range(len(path)):
                    if reachable[index]:
                        following[index + 1] = self._match_segment(segment, path[index])
            reachable = following
            if not any(reachable):
                return False
        return reachable[len(path)]

    def _match_segment(self, mask: str, name: str) -> bool:
        """Wildcard match of one segment; ``*`` is zero or more characters."""
        mask_index = name_index = 0
        star = restart = -1
        while name_index < len(name):
            self._spend()
            if mask_index < len(mask) and mask[mask_index] == "*":
                star, restart = mask_index, name_index
                mask_index += 1
            elif mask_index < len(mask) and mask[mask_index] == name[name_index]:
                mask_index += 1
                name_index += 1
            elif star >= 0:
                restart += 1
                mask_index, name_index = star + 1, restart
            else:
                return False
        return all(char == "*" for char in mask[mask_index:])


def apply_ignore(
    matcher: IgnoreMatcher,
    diff_map: DiffMap,
    *,
    builtin_excluded: Callable[[str], bool] = lambda path: False,
) -> IgnoreResult:
    """Decide which changed files are excluded; a rename or copy is checked by both paths.

    A built-in exclusion of the service withholds every name of the file as a mask does,
    so a file renamed or copied out of it stays withheld under its new name.
    """
    decisions: list[IgnoreDecision] = []
    denied: set[str] = set()
    for file in diff_map.files:
        names = [path for path in (file.old_path, file.new_path) if path is not None]
        # The source of a copy still exists and is its own file: it is withheld only by
        # an exclusion of its own name, never because its copy landed in an excluded place.
        withheld = (
            [file.new_path]
            if file.change_type is ChangeType.COPIED and file.new_path is not None
            else names
        )
        if any(builtin_excluded(path) for path in names):
            denied.update(withheld)
        if file.omission_reason is not None:
            # Its paths cannot be matched reliably: no name of it is a source until its
            # identity is resolved, also when an allowed file imports it.
            denied.update(names)
            continue
        for path in dict.fromkeys(names):
            pattern = matcher.match(path)
            if pattern is not None:
                decisions.append(IgnoreDecision(file.file_id, pattern, path))
                denied.update(withheld)
                break
    return IgnoreResult(tuple(decisions), frozenset(denied))
