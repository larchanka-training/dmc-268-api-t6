"""Fit the L1 review context into a token budget (SD §9 "Сборка и бюджет", §13).

In sprint 2 the context is the diff only (L1): a file either fits whole, is cut with
the truncation trailer of review/README.md "Input envelope", or is moved to
``<omitted_files>``. Levels L2-L4 and the full ``BudgetAllocator`` are backlog.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable
from dataclasses import replace
from pathlib import PurePosixPath
from typing import Literal, Protocol, runtime_checkable

from app.modules.reviews.application.diff_map import is_supported_path
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.policy_snapshot import (
    DEFAULT_MAX_POLICY_TOKENS,
    CallManifest,
    ExecutionProfile,
    PolicySnapshot,
    SourceRole,
)
from app.modules.reviews.application.prompt_builder import (
    ChangedFile,
    PromptBuilder,
    ReviewContext,
    ReviewRule,
    render_changed_file,
    render_changed_line,
    truncation_trailer,
    xml_text,
)
from app.modules.reviews.application.review_ignore import IgnoreMatcher, IgnorePolicy

_LOGGER = logging.getLogger(__name__)

_TEST_DIRECTORIES = frozenset({"tests", "test", "__tests__", "spec"})
_TEST_NAME_MARKERS = ("test_", "_test.", ".test.", ".spec.")
_CONFIG_SUFFIXES = frozenset(
    {".cfg", ".conf", ".env", ".ini", ".json", ".lock", ".toml", ".xml", ".yaml", ".yml"}
)
_MAX_BRACE_EXPANSION_WORK = 256


class TokenCounter(Protocol):
    """Counts one message's tokens."""

    def count(self, text: str) -> int: ...


@runtime_checkable
class LengthAwareTokenCounter(TokenCounter, Protocol):
    """``count_length(n)`` equals ``count(text)`` for every text of length ``n``."""

    def count_length(self, characters: int) -> int: ...


class EstimatedTokenCounter:
    """Three characters per token, rounded up: a conservative upper estimate
    matching the gateway (RFS §3.3).
    """

    def count(self, text: str) -> int:
        return (len(text) + 2) // 3

    def count_length(self, characters: int) -> int:
        return (characters + 2) // 3


class LegacyTokenCounter:
    """Four characters per token: (len + 3) // 4 (legacy counter/v1)."""

    def count(self, text: str) -> int:
        return (len(text) + 3) // 4

    def count_length(self, characters: int) -> int:
        return (characters + 3) // 4


SUPPORTED_TOKENIZER_VERSIONS = frozenset({"tokenizer/v1"})
SUPPORTED_COUNTER_VERSIONS = frozenset({"counter/v1", "counter/v2"})
SUPPORTED_RECOVERY_PROFILE_VERSIONS = frozenset({"recovery/v1"})
SUPPORTED_BUDGET_PROFILE_VERSIONS = frozenset({"budget/v1"})

_MODEL_WINDOWS: dict[str, tuple[int, int]] = {
    "mistral-small-4": (262_144, 8_000),
    "mistral-small-3.2-24b": (128_000, 8_000),
}
_DEFAULT_CONTEXT_WINDOW = 262_144
_DEFAULT_MAX_OUTPUT_TOKENS = 8_000


def token_counter_for(counter_version: str | None) -> TokenCounter:
    """Resolve token counter by pinned version in ExecutionProfile (RFS §7.2)."""
    if counter_version is None or counter_version == "counter/v2":
        return EstimatedTokenCounter()
    if counter_version == "counter/v1":
        return LegacyTokenCounter()
    raise PolicyError(
        PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR,
        f"Unknown counter_version '{counter_version}' in execution profile",
    )


def validate_execution_profile_versions(profile: ExecutionProfile) -> None:
    """Resolve and validate pinned execution profile versions (RFS §7.1-7.2)."""
    if (
        profile.tokenizer_version is not None
        and profile.tokenizer_version not in SUPPORTED_TOKENIZER_VERSIONS
    ):
        raise PolicyError(
            PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR,
            f"Unknown tokenizer_version '{profile.tokenizer_version}' in execution profile",
        )
    if (
        profile.counter_version is not None
        and profile.counter_version not in SUPPORTED_COUNTER_VERSIONS
    ):
        raise PolicyError(
            PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR,
            f"Unknown counter_version '{profile.counter_version}' in execution profile",
        )
    if (
        profile.recovery_profile_version is not None
        and profile.recovery_profile_version not in SUPPORTED_RECOVERY_PROFILE_VERSIONS
    ):
        raise PolicyError(
            PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR,
            f"Unknown recovery_profile_version '{profile.recovery_profile_version}' "
            "in execution profile",
        )
    if (
        profile.budget_profile_version is not None
        and profile.budget_profile_version not in SUPPORTED_BUDGET_PROFILE_VERSIONS
    ):
        raise PolicyError(
            PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR,
            f"Unknown budget_profile_version '{profile.budget_profile_version}' "
            "in execution profile",
        )


def send_budget_for(
    profile: ExecutionProfile,
    *,
    stage: Literal["review", "verification"] = "review",
    model_max_prompt_tokens: int | None = None,
) -> int:
    """Compute the prompt token budget for a stage from the model window and execution profile.

    Matches ``LlmGateway.max_prompt_tokens``:
    ``min(context_window, max_input_tokens) - max_output_tokens``. The route's configured
    window reaches this only as ``model_max_prompt_tokens`` from the caller (E-9): the
    environment is never read here.
    """
    validate_execution_profile_versions(profile)

    model_name = profile.verifier_model if stage == "verification" else profile.review_model
    context_window, max_output_tokens = _MODEL_WINDOWS.get(
        model_name, (_DEFAULT_CONTEXT_WINDOW, _DEFAULT_MAX_OUTPUT_TOKENS)
    )

    limit = min(context_window, profile.max_input_tokens)
    budget = max(0, limit - max_output_tokens)
    if model_max_prompt_tokens is not None:
        return min(budget, model_max_prompt_tokens)
    return budget


class PolicyBudgetExceededError(ValueError):
    """Rendered policy documents exceed the maximum token budget (RULES_FORMAT_SPEC §3.3)."""

    def __init__(
        self,
        message: str,
        *,
        limit: int | None = None,
        actual: int | None = None,
    ) -> None:
        super().__init__(message)
        self.limit = limit
        self.actual = actual


class PreSendGuardError(ValueError):
    """Rendered prompt exceeds the maximum input token budget or context window."""


def policy_tokens(snapshot: PolicySnapshot, counter: TokenCounter) -> int:
    """Measure the token count of rendered policy documents."""
    rendered_rules = PromptBuilder.render_policy_rules(snapshot)
    rendered_agents = PromptBuilder.render_policy_agents(snapshot)
    return counter.count(rendered_rules) + counter.count(rendered_agents)


def verify_policy_budget(
    snapshot: PolicySnapshot,
    counter: TokenCounter,
    *,
    max_policy_tokens: int = DEFAULT_MAX_POLICY_TOKENS,
) -> None:
    """Ensure rendered policy documents do not exceed max_policy_tokens.

    Fails closed with policy_prompt_budget_exceeded if limit is violated.
    """
    count = policy_tokens(snapshot, counter)
    if count > max_policy_tokens:
        raise PolicyBudgetExceededError(
            f"policy_prompt_budget_exceeded: rendered policy is {count} tokens, "
            f"limit is {max_policy_tokens}",
            limit=max_policy_tokens,
            actual=count,
        )


def verify_pre_send_guard(
    context: ReviewContext,
    counter: TokenCounter,
    *,
    max_input_tokens: int,
) -> None:
    """Pre-send guard verifying total input budget and policy budget before an LLM call."""
    if context.policy_snapshot is not None:
        verify_policy_budget(
            context.policy_snapshot,
            counter,
            max_policy_tokens=context.policy_snapshot.execution_profile.max_policy_tokens,
        )

    total = prompt_tokens(context, counter)
    if total > max_input_tokens:
        raise PreSendGuardError(
            f"llm_context_overflow: rendered prompt is {total} tokens, limit is {max_input_tokens}"
        )


_ALLOWED_SOURCE_ROLES = frozenset(
    {
        SourceRole.POLICY_DOCUMENT,
        SourceRole.SERVICE_DEFAULTS,
        SourceRole.CONVENTIONS,
        SourceRole.PR_METADATA,
        SourceRole.DIFF,
        SourceRole.SOURCE_EXCERPT,
        SourceRole.AST_EXCERPT,
        SourceRole.RETRIEVED_EXCERPT,
        SourceRole.CANDIDATE,
        SourceRole.PREVIOUS_OUTPUT,
    }
)

_ROLE_KIND_MAP = {
    SourceRole.POLICY_DOCUMENT: frozenset({"repository_rules", "project_conventions_document"}),
    SourceRole.SERVICE_DEFAULTS: frozenset({"service_defaults"}),
    SourceRole.CONVENTIONS: frozenset({"generated_conventions"}),
    SourceRole.PR_METADATA: frozenset({"pr_metadata"}),
    SourceRole.DIFF: frozenset({"source_code"}),
    SourceRole.SOURCE_EXCERPT: frozenset({"source_code"}),
    SourceRole.AST_EXCERPT: frozenset({"source_code"}),
    SourceRole.RETRIEVED_EXCERPT: frozenset({"source_code"}),
    SourceRole.CANDIDATE: frozenset({"candidate_findings"}),
    SourceRole.PREVIOUS_OUTPUT: frozenset({"previous_model_output"}),
}


def verify_provenance_pre_send_guard(
    manifest: CallManifest,
    *,
    context: ReviewContext | None = None,
    snapshot: PolicySnapshot | None = None,
) -> None:
    """Validate manifest blocks before any LLM call (CVS §5.2).

    Failure raises PolicyError(PolicyErrorCode.CONTEXT_VIOLATION), which results
    in RunFailure("policy_context_violation") without sending the request.
    """
    ignore_matcher = (
        IgnoreMatcher(
            IgnorePolicy(snapshot.ignore_patterns, (), snapshot.ignore_digest),
            policy_base_sha=snapshot.policy_base_sha,
        )
        if snapshot is not None and snapshot.ignore_patterns
        else None
    )
    for block in manifest.blocks:
        # 1. Role and kind validation
        if block.source_role not in _ALLOWED_SOURCE_ROLES:
            raise PolicyError(
                PolicyErrorCode.CONTEXT_VIOLATION,
                f"Block '{block.block_id}' has disallowed source_role: '{block.source_role}'",
            )
        allowed_kinds = _ROLE_KIND_MAP.get(block.source_role, frozenset())
        if block.source_kind not in allowed_kinds:
            raise PolicyError(
                PolicyErrorCode.CONTEXT_VIOLATION,
                f"Block '{block.block_id}' with role '{block.source_role}' "
                f"has disallowed source_kind: '{block.source_kind}'",
            )

        # 2. Path validation
        if block.path is not None:
            path_str = block.path
            if not path_str:
                raise PolicyError(
                    PolicyErrorCode.CONTEXT_VIOLATION,
                    f"Block '{block.block_id}' has empty path",
                )
            try:
                encoded = path_str.encode("utf-8")
            except UnicodeEncodeError as err:
                raise PolicyError(
                    PolicyErrorCode.CONTEXT_VIOLATION,
                    f"Block '{block.block_id}' path has invalid encoding",
                ) from err
            if len(encoded) > 1024:
                raise PolicyError(
                    PolicyErrorCode.CONTEXT_VIOLATION,
                    f"Block '{block.block_id}' path exceeds 1024 bytes limit",
                )
            segments = path_str.split("/")
            if len(segments) > 128:
                raise PolicyError(
                    PolicyErrorCode.CONTEXT_VIOLATION,
                    f"Block '{block.block_id}' path exceeds 128 segments limit",
                )
            if ".." in segments or "." in segments or any(s == "" for s in segments):
                raise PolicyError(
                    PolicyErrorCode.CONTEXT_VIOLATION,
                    f"Block '{block.block_id}' path violates repository path contract: "
                    "directory traversal or empty segment",
                )
            if not is_supported_path(path_str):
                raise PolicyError(
                    PolicyErrorCode.CONTEXT_VIOLATION,
                    f"Block '{block.block_id}' references unsupported path",
                )
            if ignore_matcher is not None and ignore_matcher.match(path_str) is not None:
                raise PolicyError(
                    PolicyErrorCode.CONTEXT_VIOLATION,
                    f"Block '{block.block_id}' references ignored path matching deny-set",
                )

        # 3. Revision validation
        if block.revision is not None:
            if (
                block.source_role
                in {
                    SourceRole.DIFF,
                    SourceRole.SOURCE_EXCERPT,
                    SourceRole.AST_EXCERPT,
                    SourceRole.RETRIEVED_EXCERPT,
                }
                or block.source_kind == "source_code"
            ):
                if block.side == "LEFT":
                    diff_old_sha = (
                        context.pr_meta.diff_old_sha
                        if context is not None and context.pr_meta is not None
                        else None
                    )
                    if not diff_old_sha or block.revision != diff_old_sha:
                        raise PolicyError(
                            PolicyErrorCode.CONTEXT_VIOLATION,
                            f"Block '{block.block_id}' LEFT revision does not match "
                            f"PR revisions: diff_old_sha '{diff_old_sha}'",
                        )
                else:
                    head_sha = (
                        context.pr_meta.head_sha
                        if context is not None and context.pr_meta is not None
                        else None
                    )
                    if head_sha is not None and block.revision != head_sha:
                        raise PolicyError(
                            PolicyErrorCode.CONTEXT_VIOLATION,
                            f"Block '{block.block_id}' revision does not match "
                            f"PR revisions: head_sha '{head_sha}'",
                        )
            elif (
                block.source_role == SourceRole.POLICY_DOCUMENT
                and snapshot is not None
                and block.revision != snapshot.policy_base_sha
            ):
                raise PolicyError(
                    PolicyErrorCode.CONTEXT_VIOLATION,
                    f"Block '{block.block_id}' revision does not match policy_base_sha",
                )

        # 4. Policy document snapshot checks
        if snapshot is not None:
            if block.source_kind == "repository_rules":
                if not snapshot.rules.is_present:
                    raise PolicyError(
                        PolicyErrorCode.CONTEXT_VIOLATION,
                        "Rules block present but snapshot has no rules document",
                    )
                if (
                    block.content_sha256
                    and snapshot.rules.text_sha256
                    and block.content_sha256 != snapshot.rules.text_sha256
                ):
                    raise PolicyError(
                        PolicyErrorCode.CONTEXT_VIOLATION,
                        "Rules block checksum mismatch with policy snapshot",
                    )
            elif block.source_kind == "project_conventions_document":
                if not snapshot.agents.is_present:
                    raise PolicyError(
                        PolicyErrorCode.CONTEXT_VIOLATION,
                        "Agents block present but snapshot has no agents document",
                    )
                if (
                    block.content_sha256
                    and snapshot.agents.text_sha256
                    and block.content_sha256 != snapshot.agents.text_sha256
                ):
                    raise PolicyError(
                        PolicyErrorCode.CONTEXT_VIOLATION,
                        "Agents block checksum mismatch with policy snapshot",
                    )


def prompt_tokens(context: ReviewContext, counter: TokenCounter) -> int:
    """Tokens of both provider messages of the rendered prompt."""
    prompt = PromptBuilder().build_prompt(context)
    return counter.count(prompt.system) + counter.count(prompt.user)


def fit_review_context(
    context: ReviewContext, *, max_prompt_tokens: int, counter: TokenCounter
) -> ReviewContext:
    """Return the context whose rendered prompt fits ``max_prompt_tokens``.

    Excerpts of the snapshot path go first: L3, then L2, then L4, before any file is cut.
    Deterministic: files are taken by descending SD §9 priority (ties by path); each one
    is kept whole, else cut to the longest prefix of its lines that fits, else omitted.
    A length-aware counter uses additive rendered character lengths with its exact
    per-message token rounding. Other counters retain full-message counts, which are
    required when tokenization depends on content. When even the prompt without any
    diff lines does not fit, the all-omitted context is returned and the gateway's
    pre-send count reports ``llm_context_overflow``.
    """
    if prompt_tokens(context, counter) <= max_prompt_tokens:
        return context
    if context.context_blocks:
        context = _drop_context_blocks(
            context, max_prompt_tokens=max_prompt_tokens, counter=counter
        )
        if context.context_blocks or prompt_tokens(context, counter) <= max_prompt_tokens:
            return context

    _warn_ignored_rules(context.rules)
    files = context.changed_files
    order = sorted(
        range(len(files)),
        key=lambda index: (-_priority(files[index], context.rules), files[index].path),
    )
    kept: dict[int, ChangedFile] = {}

    def candidate(extra: dict[int, ChangedFile]) -> ReviewContext:
        chosen = {**kept, **extra}
        return replace(
            context,
            changed_files=tuple(chosen[index] for index in sorted(chosen)),
            omitted_files=context.omitted_files
            + tuple(files[index].path for index in range(len(files)) if index not in chosen),
        )

    if isinstance(counter, LengthAwareTokenCounter):
        lengths = _PromptLengths(context, files, counter)

        def fits(extra: dict[int, ChangedFile]) -> bool:
            return lengths.tokens(kept, extra, counter.count_length) <= max_prompt_tokens

    else:

        def fits(extra: dict[int, ChangedFile]) -> bool:
            return prompt_tokens(candidate(extra), counter) <= max_prompt_tokens

    for index in order:
        file = files[index]
        if fits({index: file}):
            kept[index] = file
            continue
        shown = _longest_fitting_prefix(index, file, fits)
        if shown is not None:
            kept[index] = shown
    return candidate({})


# CVS §4.1 selects L1 → L4 → RAG → L2 → L3; the fit drops excerpts in the reverse order,
# each level from its last block (the stage appends blocks by descending file priority).
_EXCERPT_DROP_ORDER = ("L3", "L2", "L4")


def _drop_context_blocks(
    context: ReviewContext, *, max_prompt_tokens: int, counter: TokenCounter
) -> ReviewContext:
    """The context with the fewest excerpts dropped whose prompt fits, else none of them.

    Fewer excerpts never make the prompt longer, so a binary search over the number dropped
    finds the cut; every candidate is counted whole, with ``counter``.
    """
    blocks = context.context_blocks
    order = sorted(
        range(len(blocks)),
        key=lambda index: (_EXCERPT_DROP_ORDER.index(blocks[index].level), -index),
    )

    def without(dropped: int) -> ReviewContext:
        gone = frozenset(order[:dropped])
        return replace(
            context,
            context_blocks=tuple(block for index, block in enumerate(blocks) if index not in gone),
        )

    low, high = 1, len(blocks)
    while low < high:
        middle = (low + high) // 2
        if prompt_tokens(without(middle), counter) <= max_prompt_tokens:
            high = middle
        else:
            low = middle + 1
    return without(low)


class _FileLengths:
    """Exact rendered lengths of a file and each possible line prefix."""

    def __init__(self, file: ChangedFile, *, include_block_id: bool = False) -> None:
        self.file = file
        self.include_block_id = include_block_id
        self.whole = len(render_changed_file(file, include_block_id=include_block_id))
        self.empty = len(
            render_changed_file(
                replace(file, lines=(), total_lines=None),
                include_block_id=include_block_id,
            )
        )
        prefix = [0]
        for line in file.lines:
            prefix.append(prefix[-1] + len(render_changed_line(line)))
        self.prefix = tuple(prefix)

    def length(self, candidate: ChangedFile) -> int:
        if candidate is self.file:
            return self.whole
        shown = len(candidate.lines)
        # A cut block has ``shown`` separators between its lines and trailer.
        return (
            self.empty
            + self.prefix[shown]
            + shown
            + len(truncation_trailer(shown, len(self.file.lines)))
        )


class _PromptLengths:
    """Account for variable tags without rebuilding the stable prompt prefix."""

    def __init__(
        self, context: ReviewContext, files: tuple[ChangedFile, ...], counter: TokenCounter
    ) -> None:
        include_block_id = context.policy_snapshot is not None
        empty = replace(context, changed_files=(), omitted_files=())
        self.base_user = len(PromptBuilder().render_input(empty))
        self.system_tokens = counter.count(context.system)
        self.files = tuple(_FileLengths(file, include_block_id=include_block_id) for file in files)
        self.omitted_paths = tuple(len(xml_text(file.path)) for file in files)
        self.original_omitted = tuple(len(xml_text(path)) for path in context.omitted_files)

    def tokens(
        self,
        kept: dict[int, ChangedFile],
        extra: dict[int, ChangedFile],
        count_length: Callable[[int], int],
    ) -> int:
        chosen = {**kept, **extra}
        shown = tuple(sorted(chosen))
        changed_chars = sum(self.files[index].length(chosen[index]) for index in shown)
        changed_chars += max(0, len(shown) - 1)
        omitted = tuple(index for index in range(len(self.files)) if index not in chosen)
        omitted_count = len(self.original_omitted) + len(omitted)
        omitted_chars = sum(self.original_omitted) + sum(
            self.omitted_paths[index] for index in omitted
        )
        omitted_chars += max(0, omitted_count - 1)
        return self.system_tokens + count_length(self.base_user + changed_chars + omitted_chars)


def _longest_fitting_prefix(index: int, file: ChangedFile, fits: _Fits) -> ChangedFile | None:
    """Binary search for the largest line prefix (at least one line) that still fits."""
    low, high = 1, len(file.lines) - 1
    best: ChangedFile | None = None
    while low <= high:
        middle = (low + high) // 2
        cut = replace(file, lines=file.lines[:middle], total_lines=len(file.lines))
        if fits({index: cut}):
            best = cut
            low = middle + 1
        else:
            high = middle - 1
    return best


class _Fits(Protocol):
    def __call__(self, extra: dict[int, ChangedFile], /) -> bool: ...


def _priority(file: ChangedFile, rules: tuple[ReviewRule, ...]) -> float:
    """SD §9 step 1: kind weight × log(changed lines + 1) × 1.5 under a custom rule."""
    changed = sum(1 for line in file.lines if line.type != "context")
    weight = _kind_weight(file.path) * math.log(changed + 1)
    if any(_matches_rule(file.path, rule) for rule in rules):
        weight *= 1.5
    return weight


def _kind_weight(path: str) -> float:
    lowered = PurePosixPath(path.lower())
    name = lowered.name
    if _TEST_DIRECTORIES & set(lowered.parts[:-1]) or any(
        marker in name for marker in _TEST_NAME_MARKERS
    ):
        return 0.6
    if lowered.suffix in _CONFIG_SUFFIXES or name == "dockerfile":
        return 0.4
    return 1.0


def _rule_is_ignored(rule: ReviewRule) -> bool:
    """Whether an include or exclude glob of ``rule`` exceeds the brace expansion limit.

    Such a rule raises no file's priority. An oversized include disables it for every
    path; an oversized exclude matters only for paths its include matches, and skipping
    that exclude must not promote the file.
    """
    return any(_expand_braces(pattern) is None for pattern in (*rule.include, *rule.exclude))


def _matches_rule(path: str, rule: ReviewRule) -> bool:
    if _rule_is_ignored(rule):
        return False
    candidate = PurePosixPath(path)

    def matches(patterns: tuple[str, ...]) -> bool:
        # Every pattern expands within the limit once the rule is not ignored.
        return any(
            candidate.full_match(glob)
            for pattern in patterns
            for glob in _expand_braces(pattern) or ()
        )

    return matches(rule.include) and not matches(rule.exclude)


def _warn_ignored_rules(rules: tuple[ReviewRule, ...]) -> None:
    """Log each rule that ``_rule_is_ignored`` drops from file priority, once per fit."""
    for rule in rules:
        if _rule_is_ignored(rule):
            _LOGGER.warning(
                "Custom rule %r is ignored for file priority: "
                "a brace glob exceeds %d expansion units",
                rule.name,
                _MAX_BRACE_EXPANSION_WORK,
            )


def _expand_braces(pattern: str) -> list[str] | None:
    """Expand brace globs for ``full_match``, within 256 units of work.

    One unit is a candidate examined or an alternative produced. Return ``None``
    when the cap is exceeded; the caller ignores the entire rule for that path.
    This bounds work before a pattern such as ``{a,b}`` repeated 18 times can
    materialize all 262,144 alternatives.
    """
    expanded = [pattern]
    work = 0
    while True:
        next_patterns: list[str] = []
        changed = False
        for candidate in expanded:
            work += 1
            if work > _MAX_BRACE_EXPANSION_WORK:
                return None
            match = _BRACES.search(candidate)
            if match is None:
                next_patterns.append(candidate)
                continue
            changed = True
            options = match.group(1)
            work += options.count(",") + 1
            if work > _MAX_BRACE_EXPANSION_WORK:
                return None
            head, tail = candidate[: match.start()], candidate[match.end() :]
            next_patterns.extend(head + option + tail for option in options.split(","))
        if not changed:
            return next_patterns
        expanded = next_patterns


_BRACES = re.compile(r"\{([^{}]*)\}")
