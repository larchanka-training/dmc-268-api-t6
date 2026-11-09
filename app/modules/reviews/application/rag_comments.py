"""Comments hidden from the code a RAG excerpt shows (#498, ADR 0001 §9).

A retrieved block is repository code the pull request did not change: a comment in it is text
the model reads but nobody reviewed. Each comment becomes a marker in the syntax it had, and
every line keeps its number, so the excerpt still shows one text line per source line.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.modules.reviews.application.ast_symbols import CommentSpan

_MARKERS = (
    ("#", "# [comment removed]"),
    ("//", "// [comment removed]"),
    ("/*", "/* [comment removed] */"),
)
_HTML_MARKER = "<!-- [comment removed] -->"


def hide_comments(lines: Sequence[str], comments: Sequence[CommentSpan]) -> list[str]:
    """``lines`` of a file (split on ``\\n``) with every comment of its facts replaced.

    The columns count UTF-8 bytes, as tree-sitter reports them. A carriage return that ends a
    line stays: some grammars count it into the comment, others do not.
    """
    shown = list(lines)
    # From the last comment back: a replacement changes the columns after it, never before.
    for comment in reversed(comments):
        first = comment.start_line - 1
        last = comment.end_line - 1
        head, head_end = _body(shown[first])
        opening = head[comment.start_column :].decode("utf-8")
        marker = next(
            (text for prefix, text in _MARKERS if opening.startswith(prefix)), _HTML_MARKER
        )
        before = head[: comment.start_column].decode("utf-8")
        tail, tail_end = _body(shown[last])
        after = tail[comment.end_column :].decode("utf-8")
        if first == last:
            shown[first] = before + marker + after + head_end
            continue
        shown[first] = before + marker + head_end
        for index in range(first + 1, last):
            shown[index] = _body(shown[index])[1]
        shown[last] = after + tail_end
    return shown


def _body(line: str) -> tuple[bytes, str]:
    """The UTF-8 bytes of ``line`` without a closing carriage return, and that return."""
    if line.endswith("\r"):
        return line[:-1].encode("utf-8"), "\r"
    return line.encode("utf-8"), ""
