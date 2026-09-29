"""Pure rendering of the ``review.conventions`` input envelope (review/README.md)."""

from __future__ import annotations

from app.modules.reviews.application.conventions import ConventionsRequest
from app.modules.reviews.application.prompt_builder import (
    PromptBuilder,
    ReviewPrompt,
    _attribute,
    _container,
    _text,
)


def build_conventions_prompt(request: ConventionsRequest) -> ReviewPrompt:
    """Tags in the prompt's order: rules, AGENTS.md, tree, files, changed paths."""
    files = []
    for file in request.repo_files:
        if file.content is None:
            continue
        lines = "\n".join(
            f'<line n="{number}">{_text(line)}</line>'
            for number, line in enumerate(file.content.splitlines(), start=1)
        )
        files.append(f'<file path="{_attribute(file.path)}">\n{lines}\n</file>')
    user = "\n".join(
        (
            PromptBuilder._render_rules(request.rules),
            _container("agents_md", _text(request.agents_md or "")),
            _container("repo_tree", "\n".join(_text(path) for path in request.repo_tree)),
            _container("repo_files", "\n".join(files)),
            _container("changed_files", "\n".join(_text(path) for path in request.changed_files)),
        )
    )
    return ReviewPrompt(system=request.system, user=user)
