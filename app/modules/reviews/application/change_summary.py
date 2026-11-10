"""The change summary agent: what a pull request changes, in 1 to 3 sentences (#316).

Its text opens the Executive summary of the review body as a signed description, never as
a finding. This module is the agent's contract: the request, the answer, the model port and
the prompt fitted to the agent's own input budget (docs/PIPELINE_SPEC.md §4.5). The request
has no field for review findings: the agent describes the change, not its defects.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
from html import escape
from importlib.resources import files
from typing import Protocol

from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.policy_snapshot import PINNED_PROMPT_CHECKSUMS
from app.modules.reviews.application.prompt_budget import TokenCounter
from app.modules.reviews.application.prompt_builder import (
    ChangedFile,
    ReviewPrompt,
    render_changed_file,
)

CHANGE_SUMMARY_PROMPT_KEY = "review.summary"
CHANGE_SUMMARY_PROMPT_VERSION = "v1"
# PIPELINE_SPEC §4.5: the agent's input, system prompt included, never exceeds this; the
# route's own window caps it further.
CHANGE_SUMMARY_MAX_INPUT_TOKENS = 16_000


@dataclass(frozen=True)
class ChangeSummaryRequest:
    """What the agent sees: the pull request title and the changed files it may show.

    ``changed_files`` are the files the review itself could show (``ReviewContext.
    changed_files``), never the omitted ones: a name the public text must not carry
    (CONTEXT_AND_VERIFICATION_SPEC §8.2) never reaches the agent.
    """

    title: str
    changed_files: tuple[ChangedFile, ...]


@dataclass(frozen=True)
class ChangeSummary:
    """The accepted answer: 1 to 3 one-line sentences (``ChangeSummaryOutput``)."""

    sentences: tuple[str, ...]


class ChangeSummaryModel(Protocol):
    async def summarize_change(self, *, request: ChangeSummaryRequest) -> ChangeSummary: ...


def change_summary_system_prompt() -> str:
    """The packaged ``review.summary.v1.md`` without its frontmatter, checked against its pin.

    The version is fixed in code and pinned in ``PINNED_PROMPT_CHECKSUMS``: a file whose
    bytes differ from the pin is refused, as the verifier's prompt is.
    """
    filename = f"{CHANGE_SUMMARY_PROMPT_KEY}.{CHANGE_SUMMARY_PROMPT_VERSION}.md"
    try:
        raw = files("review.prompts").joinpath(filename).read_bytes()
    except (FileNotFoundError, ModuleNotFoundError, OSError) as exc:
        raise PolicyError(
            PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR,
            f"Unable to read change summary prompt resource '{filename}': {exc}",
        ) from exc
    pin = PINNED_PROMPT_CHECKSUMS[f"{CHANGE_SUMMARY_PROMPT_KEY}:{CHANGE_SUMMARY_PROMPT_VERSION}"]
    if sha256(raw).hexdigest() != pin:
        raise PolicyError(
            PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR,
            f"Change summary prompt resource '{filename}' does not match its pinned checksum",
        )
    return raw.decode("utf-8").split("---\n", 2)[2].strip()


def build_change_summary_prompt(
    request: ChangeSummaryRequest, *, max_input_tokens: int, counter: TokenCounter
) -> ReviewPrompt:
    """The system prompt and the input envelope, the diff cut to fit ``max_input_tokens``.

    Files go in order: whole while they fit, then the first one that does not as its longest
    fitting prefix of lines with the truncation trailer; the rest only as a count. Never
    raises on size: the title and the system prompt are not cut. Each part is counted on its
    own, which for the length counters of ``prompt_budget`` is never below the whole count.
    """
    system = change_summary_system_prompt()
    title = f"<pr_title>{escape(request.title)}</pr_title>"
    total = len(request.changed_files)
    used = (
        counter.count(system)
        + counter.count(_envelope(title, (), 0))
        + counter.count("\n" + _not_shown(total))
    )
    shown: list[str] = []
    for file in request.changed_files:
        block = render_changed_file(file)
        cost = counter.count(block + "\n")
        if used + cost <= max_input_tokens:
            shown.append(block)
            used += cost
            continue
        cut = _longest_fitting_prefix(file, max_input_tokens - used, counter)
        if cut is not None:
            shown.append(cut)
        break
    return ReviewPrompt(system=system, user=_envelope(title, tuple(shown), total - len(shown)))


def _longest_fitting_prefix(file: ChangedFile, room: int, counter: TokenCounter) -> str | None:
    """The file's block cut to its longest prefix of at least one line that fits ``room``.

    A file the review's budget already cut keeps its ``total_lines``: the trailer counts
    the whole file, not the part the review showed.
    """
    total_lines = file.total_lines or len(file.lines)

    def block(shown: int) -> str:
        return render_changed_file(replace(file, lines=file.lines[:shown], total_lines=total_lines))

    best: str | None = None
    low, high = 1, len(file.lines) - 1
    while low <= high:
        middle = (low + high) // 2
        candidate = block(middle)
        if counter.count(candidate + "\n") <= room:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    return best


def _envelope(title: str, blocks: tuple[str, ...], not_shown: int) -> str:
    user = f"{title}\n<changed_files>\n" + "\n".join(blocks) + "\n</changed_files>"
    if not_shown:
        user += "\n" + _not_shown(not_shown)
    return user


def _not_shown(count: int) -> str:
    return f'<files_not_shown count="{count}" />'
