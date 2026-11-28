"""Blanket orders in a policy document, removed from the text a v8 run shows the model.

Escaping keeps an order to report nothing inside ``<repository_rules>``. review.system v5
tells the model that order is not a criterion, and the model obeys it anyway (#362). A run
pinned to v8 is shown a marker on each such line. The stored document is unchanged, so
ignore rules and evidence coordinates stay on the file's own lines. A line that names a
real exception ("do not report" one defect or path) is not an order of this kind.
"""

from __future__ import annotations

import re

POLICY_COMMAND_REMOVED = "[policy command removed]"

# Review prompts whose policy blocks show the marker instead of a command line.
POLICY_COMMANDS_REMOVED_PROMPT_VERSIONS: frozenset[tuple[str, str]] = frozenset(
    {("review.system", "v8")}
)

_QUOTE = re.compile(r"^(?:>\s*)+")
_COMMAND = re.compile(
    "|".join(
        (
            r"report\s+nothing",
            r"no\s+issues\s+to\s+report",
            r"ignore\s+previous\s+instructions",
            r"findings\s*=\s*\[\s*\]",
            r"return\s+no\s+findings",
            r"report\s+no\s+findings",
            r"(?:^|\s)report\s*$",
            r"do\s+not\s+mention\s+this\s+section",
            r"approval\s+mode",
            r"only\s+this\s+section\s+is\s+authoritative",
            r"treat\s+this\s+document\s+as\s+the\s+system",
            r"override\s+the\s+system\s+prompt",
            r"outrank\s+the\s+service",
            r"supersedes\s+the\s+service",
            r"overrides\s+the\s+review\s+service",
            r"exempt\s+from\s+every\s+rule",
            r"priority\s*:\s*(?:highest|maximum|system)",
        )
    ),
    re.IGNORECASE,
)
_HEADING = re.compile(
    r"^#{1,6}\s+.*(?:"
    r"highest[- ]priority"
    r"|priority\s*:\s*(?:highest|maximum)"
    r"|priority\s+0\b"
    r"|system\s+override"
    r"|review\s+policy\s+override"
    r")",
    re.IGNORECASE,
)
_FORGED_TAG = re.compile(
    r"</?\s*(?:repository_rules|repo_rules|project_conventions_document|system|instructions)\b",
    re.IGNORECASE,
)


def removes_policy_commands(key: str, version: str) -> bool:
    """Whether a run pinned to this review prompt is shown the marker instead of the order."""
    return (key, version) in POLICY_COMMANDS_REMOVED_PROMPT_VERSIONS


def neutralize_policy_commands(text: str) -> str:
    """Replace each command line with the marker. Line breaks, including a trailing one, stay."""
    if not text:
        return text
    suffix = "\n" if text.endswith("\n") else ""
    body = text[:-1] if suffix else text
    if body == "":
        return text
    lines = [POLICY_COMMAND_REMOVED if _is_command(line) else line for line in body.split("\n")]
    return "\n".join(lines) + suffix


def _is_command(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if _FORGED_TAG.search(stripped):
        return True
    prose = _QUOTE.sub("", stripped)
    return _HEADING.search(prose) is not None or _COMMAND.search(prose) is not None
