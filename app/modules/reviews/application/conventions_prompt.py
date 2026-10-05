"""Pure rendering of the ``review.conventions`` input envelope (review/README.md)."""

from __future__ import annotations

from dataclasses import replace

from app.modules.reviews.application.conventions import ConventionsRequest
from app.modules.reviews.application.prompt_budget import TokenCounter
from app.modules.reviews.application.prompt_builder import (
    ReviewPrompt,
    render_custom_instructions,
    xml_attribute,
    xml_container,
    xml_text,
)


def build_conventions_prompt(request: ConventionsRequest) -> ReviewPrompt:
    """Tags in the prompt's order: rules, AGENTS.md, tree, files, changed paths.

    ``<changed_files>`` lists the paths the model must describe (``traced_files``); a cut
    tree or a pull request over that limit ends its list with an explicit count.
    """
    files = []
    visible_paths: set[str] = set()
    for file in request.repo_files:
        if file.content is None:
            continue
        visible_paths.add(file.path)
        lines = [
            f'<line n="{number}">{xml_text(line)}</line>'
            for number, line in enumerate(file.content.splitlines(), start=1)
        ]
        if file.omitted_lines:
            lines.append(f"[{file.omitted_lines} more lines omitted]")
        files.append(f'<file path="{xml_attribute(file.path)}">\n' + "\n".join(lines) + "\n</file>")
    omitted_contexts = max(
        0, len(request.repo_tree) + request.omitted_tree_paths - len(visible_paths)
    )
    if omitted_contexts:
        files.append(f"[{omitted_contexts} more repository file contexts omitted]")
    tree = [xml_text(path) for path in request.repo_tree]
    if request.omitted_tree_paths:
        tree.append(f"[{request.omitted_tree_paths} more paths omitted]")
    changed = [xml_text(path) for path in request.traced_files]
    not_traced = len(request.changed_files) - len(request.traced_files)
    if not_traced:
        changed.append(f"[{not_traced} more changed paths omitted]")
    user = "\n".join(
        (
            render_custom_instructions(request.rules),
            xml_container("agents_md", xml_text(request.agents_md or "")),
            xml_container("repo_tree", "\n".join(tree)),
            xml_container("repo_files", "\n".join(files)),
            xml_container("changed_files", "\n".join(changed)),
        )
    )
    return ReviewPrompt(system=request.system, user=user)


def fit_conventions_request(
    request: ConventionsRequest, *, max_prompt_tokens: int, counter: TokenCounter
) -> ConventionsRequest:
    """Cut ``repo_tree`` from its end until the prompt fits; nothing else is cut.

    The tree is the only unbounded input: ``repo_files`` is bounded when selected and
    every traced changed path must stay. Deterministic; when even an empty tree does
    not fit, the gateway's pre-send count reports ``llm_context_overflow``.
    """

    def tokens(candidate: ConventionsRequest) -> int:
        prompt = build_conventions_prompt(candidate)
        return counter.count(prompt.system) + counter.count(prompt.user)

    if tokens(request) <= max_prompt_tokens:
        return request
    tree = request.repo_tree

    def keep(count: int) -> ConventionsRequest:
        return replace(
            request,
            repo_tree=tree[:count],
            omitted_tree_paths=request.omitted_tree_paths + len(tree) - count,
        )

    low, high = 0, len(tree) - 1
    while low < high:
        middle = (low + high + 1) // 2
        if tokens(keep(middle)) <= max_prompt_tokens:
            low = middle
        else:
            high = middle - 1
    return keep(low)
