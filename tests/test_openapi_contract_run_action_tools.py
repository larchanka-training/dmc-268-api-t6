"""The ``RunAction.tool`` catalog against the code and the spec (#165, #210)."""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping
from typing import Any

import pytest

from tests.test_openapi_contract import APP_PATH, _components

# api#165: every step name the code writes to run_actions (docs/PIPELINE_SPEC.md §2). The column
# is free text, not an enum, so nothing else catches a tool missing from the contract.
RUN_ACTION_TOOLS = frozenset(
    {
        "policy.load",
        "vcs.fetch_diff",
        "context.build",
        "context.rag",
        "context.manifest",
        "llm.repo_conventions",
        "llm.call",
        "llm.result_cache",
        "llm.review_output",
        "llm.verify",
        "review.postprocess",
        "github.publish_review",
        "gitlab.publish_review",
        "bitbucket.publish_review",
    }
)


# A backticked `<lower>.<lower_snake>` token; state names and docs/ paths do not match.
BACKTICKED_TOOL = re.compile(r"`([a-z]+\.[a-z_]+)`")


def _string_constant(node: ast.expr | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _written_tools(source: str) -> set[str]:
    """Collect the string-constant tool names from the shapes that write run_actions rows.

    The scanned shapes: a module-level ``NAME_TOOL = "..."`` assignment, the third positional
    argument of a ``traced_step(...)`` call, the second positional argument of a ``record(...)``
    call (the ``RunTrace.record(run_id, tool, ...)`` sink) and a ``tool=`` keyword of any call.
    Only string constants count; a name or an attribute in these positions is skipped. A
    ``tool=`` keyword and a literal second argument of ``record(...)`` are counted on any call,
    whatever the receiver, so a ``tool="x.y"`` or a ``ledger.record(context, "x.y")`` that does not
    write run_actions surfaces as unexpected and must be renamed or the scan taught to skip it. A
    blanket ``"x.y"`` match would also pick up prompt names such as ``review.system``, which are
    not tools.
    """
    tree = ast.parse(source)
    found: list[str | None] = []
    targets: list[ast.expr]
    value: ast.expr | None
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            targets, value = statement.targets, statement.value
        elif isinstance(statement, ast.AnnAssign):
            targets, value = [statement.target], statement.value
        else:
            continue
        if any(isinstance(target, ast.Name) and target.id.endswith("_TOOL") for target in targets):
            found.append(_string_constant(value))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        callee = func.id if isinstance(func, ast.Name) else None
        callee = func.attr if isinstance(func, ast.Attribute) else callee
        if callee == "traced_step" and len(node.args) >= 3:
            found.append(_string_constant(node.args[2]))
        if callee == "record" and len(node.args) >= 2:
            found.append(_string_constant(node.args[1]))
        found += [
            _string_constant(keyword.value) for keyword in node.keywords if keyword.arg == "tool"
        ]
    return {tool for tool in found if tool is not None}


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('traced_step(trace, run_id, "a.b", {})', {"a.b"}),
        ('trace.record(run_id, "x.y", {}, None, now, 0)', {"x.y"}),
        ("ledger.record(context, usage)", set()),
        ("self._trace.record(pointer.run_id, route.tool, {})", set()),
        ('Route(tool="c.d")', {"c.d"}),
        ('LLM_CALL_TOOL = "e.f"', {"e.f"}),
        ('X_TOOL: Final = "g.h"', {"g.h"}),
        ('ledger.record(context, "x.y")', {"x.y"}),
        ('def record(self, run_id, tool="x.y"): ...', set()),
        ('prompts.load("review.system")', set()),
    ],
    ids=[
        "traced_step-third-argument",
        "record-second-argument",
        "record-without-tool-argument",
        "record-attribute-argument",
        "tool-keyword",
        "module-constant",
        "module-annotated-constant",
        "record-any-receiver",
        "record-definition",
        "plain-string-literal",
    ],
)
def test_written_tools_scan_picks_up_only_the_run_action_shapes(
    source: str, expected: set[str]
) -> None:
    assert _written_tools(source) == expected


def _catalog_drift(side: str, found: set[str]) -> str:
    missing, unexpected = sorted(RUN_ACTION_TOOLS - found), sorted(found - RUN_ACTION_TOOLS)
    return f"{side} vs catalog: missing {missing}, unexpected {unexpected}"


def test_run_action_tool_catalog_matches_the_tools_the_code_writes() -> None:
    written: set[str] = set()
    for path in sorted(APP_PATH.rglob("*.py")):
        written |= _written_tools(path.read_text(encoding="utf-8"))

    assert written == RUN_ACTION_TOOLS, _catalog_drift("code", written)


@pytest.mark.parametrize(
    "location", [("RunAction", "properties", "tool"), ("RunStatus",)], ids=".".join
)
def test_run_action_tool_descriptions_list_the_catalog(location: tuple[str, ...]) -> None:
    schema: Mapping[str, Any] = _components()
    for key in location:
        schema = schema[key]

    listed = set(BACKTICKED_TOOL.findall(schema["description"]))

    assert listed == RUN_ACTION_TOOLS, _catalog_drift(f"{'.'.join(location)} description", listed)
