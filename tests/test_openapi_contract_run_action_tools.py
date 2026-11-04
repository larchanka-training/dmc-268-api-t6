"""The ``RunAction.tool`` catalog against the code and the spec (#165, #210)."""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest

from tests.openapi_contract import APP_PATH, _components

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


# The calls that take the tool by position, and where; any call can also take it as ``tool=``.
TOOL_POSITIONS = {"traced_step": 2, "record": 1, "add_action": 1, "PublicationRoute": 1}
# A star argument on one of these can carry the tool past the scan.
TOOL_WRITERS = frozenset(TOOL_POSITIONS) | {"insert_run_action", "RunAction"}
_REVIEWS = "app/modules/reviews"
# The exact places allowed to pass a tool that is not a name: (path, scope, callee, argument), no
# line numbers. Each entry weakens the scan, so it says why it is safe. A new function that passes
# its own tool parameter on also goes into TOOL_POSITIONS, or nothing reads its callers.
TOOL_PASS_THROUGH = frozenset(
    {
        # The outer function is itself in TOOL_WRITERS, so the scan reads the name at its callers.
        (f"{_REVIEWS}/application/run_trace.py", "traced_step", "record", "tool"),
        (
            f"{_REVIEWS}/application/run_trace.py",
            "TransactionalRunTrace.record",
            "add_action",
            "tool",
        ),
        (
            f"{_REVIEWS}/infrastructure/run_lifecycle_store.py",
            "SqlAlchemyRunTraceStore.add_action",
            "insert_run_action",
            "tool",
        ),
        (
            f"{_REVIEWS}/infrastructure/run_action_records.py",
            "insert_run_action",
            "RunAction",
            "tool",
        ),
        # PublicationRoute is in TOOL_POSITIONS, so the scan reads the names where it is built.
        (
            f"{_REVIEWS}/application/publish_run_review.py",
            "PublishRunReview._submit",
            "record",
            "route.tool",
        ),
        # These copy a name that is already stored or already scanned; no row is written.
        (
            f"{_REVIEWS}/infrastructure/run_action_records.py",
            "insert_run_action",
            "record_step_span",
            "tool",
        ),
        ("app/bootstrap/run_step_listener.py", "_event", "RunStep", "tool"),
        ("app/main.py", "to_run_action_dto", "RunActionDto", "item.tool"),
        (
            f"{_REVIEWS}/application/get_run_actions.py",
            "GetRunActions._to_trace",
            "RunActionTrace",
            "action.tool",
        ),
        (
            f"{_REVIEWS}/infrastructure/run_repository.py",
            "SqlAlchemyRunRepository._to_run_action",
            "RunActionProjection",
            "action.tool",
        ),
        # UsageLedger.record(context, usage) shares the method name; it is not a trace.
        (
            f"{_REVIEWS}/infrastructure/llm/gateway.py",
            "LlmGateway._record_paid_answer.write_once",
            "record",
            "usage",
        ),
    }
)


@dataclass(frozen=True, order=True)
class _ToolSite:
    """A place that passes a tool the scan cannot read as a name."""

    path: str
    line: int
    scope: str
    callee: str
    argument: str

    @property
    def key(self) -> tuple[str, str, str, str]:
        return self.path, self.scope, self.callee, self.argument

    def __str__(self) -> str:
        return f"{self.path}:{self.line} {self.scope} -> {self.callee}({self.argument})"


@dataclass
class _ToolScan:
    names: set[str] = field(default_factory=set)
    constants: set[str] = field(default_factory=set)
    sites: set[_ToolSite] = field(default_factory=set)


def _string_constant(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _last_name(node: ast.AST) -> str | None:
    """The name an expression ends in: ``a`` for both ``a`` and ``x.a``."""
    if isinstance(node, ast.Name):
        return node.id
    return node.attr if isinstance(node, ast.Attribute) else None


def _callee(call: ast.Call) -> str:
    return _last_name(call.func) or ast.unparse(call.func)


def _writer_aliases(tree: ast.Module) -> dict[str, str]:
    """The other names a module gives a writer, by import or assignment, in any of its scopes."""
    aliases: dict[str, str] = {}
    bindings: list[tuple[ast.expr, ast.expr]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.alias):
            if node.asname and node.name in TOOL_WRITERS:
                aliases[node.asname] = node.name
        elif isinstance(node, ast.Assign):
            bindings += [(target, node.value) for target in node.targets]
        elif isinstance(node, ast.AnnAssign | ast.NamedExpr) and node.value is not None:
            bindings.append((node.target, node.value))
    known = -1
    while known != len(aliases):
        known = len(aliases)
        for target, value in bindings:
            alias, name = _last_name(target), _last_name(value)
            if alias is not None and name is not None:
                writer = aliases.get(name, name)
                if writer in TOOL_WRITERS:
                    aliases[alias] = writer
    return aliases


def _passes_a_writer(argument: ast.expr, aliases: Mapping[str, str]) -> bool:
    """A writer handed to another call, as in ``partial(trace.record, ...)``.

    The scan cannot read what that call gives the writer. A variable named ``record`` and the
    ``RunAction`` model are too common as arguments to count.
    """
    name = _last_name(argument)
    if name is None or (name == "record" and isinstance(argument, ast.Name)):
        return False
    return aliases.get(name, name) in TOOL_WRITERS - {"RunAction"}


def _tool_arguments(call: ast.Call, callee: str) -> list[ast.expr | ast.keyword]:
    found: list[ast.expr | ast.keyword] = [
        keyword.value for keyword in call.keywords if keyword.arg == "tool"
    ]
    position = TOOL_POSITIONS.get(callee)
    if position is not None and len(call.args) > position:
        found.append(call.args[position])
    if callee in TOOL_WRITERS:
        found += [argument for argument in call.args if isinstance(argument, ast.Starred)]
        found += [keyword for keyword in call.keywords if keyword.arg is None]
    return found


def _scan_tool_writes(source: str, path: str = "<source>") -> _ToolScan:
    """Read the tool names of one module from the shapes that can reach run_actions.

    The scanned shapes: a module-level ``NAME_TOOL = ...`` assignment, a slot of
    ``TOOL_POSITIONS`` and a ``tool=`` keyword. A string constant there is a name. Anything else
    becomes a site, which ``_unread_tools`` reports unless it can vouch for it. A ``tool=`` keyword
    and a slot of ``record(...)`` count on any call, whatever the receiver, so a ``tool="x.y"`` or
    a ``ledger.record(context, "x.y")`` that does not write run_actions surfaces as unexpected and
    must be renamed or the scan taught to skip it. A blanket ``"x.y"`` match would also pick up
    prompt names such as ``review.system``, which are not tools. A writer called under another name
    is read as that writer, and a writer handed to another call becomes a site.
    """
    tree = ast.parse(source)
    scan = _ToolScan()
    aliases = _writer_aliases(tree)
    targets: list[ast.expr]
    value: ast.expr | None
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            targets, value = statement.targets, statement.value
        elif isinstance(statement, ast.AnnAssign):
            targets, value = [statement.target], statement.value
        else:
            continue
        for target in targets:
            if value is None or not isinstance(target, ast.Name):
                continue
            if not target.id.endswith("_TOOL"):
                continue
            name = _string_constant(value)
            if name is None:
                site = _ToolSite(path, statement.lineno, "<module>", target.id, ast.unparse(value))
                scan.sites.add(site)
            else:
                scan.names.add(name)
                scan.constants.add(target.id)
    pending: list[tuple[ast.AST, str]] = [(tree, "<module>")]
    while pending:
        node, scope = pending.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            scope = node.name if scope == "<module>" else f"{scope}.{node.name}"
        pending += [(child, scope) for child in ast.iter_child_nodes(node)]
        if not isinstance(node, ast.Call):
            continue
        callee = _callee(node)
        for argument in _tool_arguments(node, aliases.get(callee, callee)):
            name = _string_constant(argument)
            if name is None:
                site = _ToolSite(path, node.lineno, scope, callee, ast.unparse(argument))
                scan.sites.add(site)
            else:
                scan.names.add(name)
        for passed in [*node.args, *(keyword.value for keyword in node.keywords)]:
            if _passes_a_writer(passed, aliases):
                site = _ToolSite(path, node.lineno, scope, callee, ast.unparse(passed))
                scan.sites.add(site)
    return scan


def _written_tools(source: str) -> set[str]:
    return _scan_tool_writes(source).names


def _unread_tools(scans: list[_ToolScan]) -> list[_ToolSite]:
    """The sites of ``scans`` that are neither a known ``*_TOOL`` constant nor in the allowlist."""
    constants = {constant for scan in scans for constant in scan.constants}
    return sorted(
        site
        for scan in scans
        for site in scan.sites
        if site.key not in TOOL_PASS_THROUGH and site.argument not in constants
    )


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
        ('uow.trace.add_action(run_id, "i.j", {}, None, now, 0)', {"i.j"}),
        ('PublicationRoute(False, "k.l", "x", False, None)', {"k.l"}),
        ('from m import traced_step as ts\nts(trace, run_id, "probe.alias", {})', {"probe.alias"}),
        ('write = trace.record\nwrite(run_id, "probe.alias", {})', {"probe.alias"}),
        ('write: Writer = trace.add_action\nwrite(run_id, "m.n", {})', {"m.n"}),
        ('self._write = trace.record\nself._write(run_id, "o.p", {})', {"o.p"}),
        ('(write := trace.record)\nwrite(run_id, "q.r", {})', {"q.r"}),
        ('second = first\nfirst = trace.record\nsecond(run_id, "s.t", {})', {"s.t"}),
        ('make = PublicationRoute\nmake(False, "u.v", "x", False, None)', {"u.v"}),
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
        "add_action-second-argument",
        "publication-route-second-argument",
        "import-alias",
        "method-alias",
        "annotated-alias",
        "attribute-alias",
        "walrus-alias",
        "alias-of-an-alias",
        "class-alias",
    ],
)
def test_written_tools_scan_picks_up_only_the_run_action_shapes(
    source: str, expected: set[str]
) -> None:
    assert _written_tools(source) == expected


def _unread(*sources: str) -> list[str]:
    return [
        f"{site.callee}({site.argument})"
        for site in _unread_tools([_scan_tool_writes(source) for source in sources])
    ]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('traced_step(trace, run_id, f"a.{x}", {})', "traced_step(f'a.{x}')"),
        ('trace.record(run_id, provider + ".x", {}, None, now, 0)', "record(provider + '.x')"),
        (
            'STEP_NAME = "a.b"\ninsert_run_action(session, tool=STEP_NAME)',
            "insert_run_action(STEP_NAME)",
        ),
        ("uow.trace.add_action(run_id, name, {}, None, now, 0)", "add_action(name)"),
        ('trace.record(run_id, "a" if flag else "b", {})', "record('a' if flag else 'b')"),
        ('PublicationRoute(tool=f"{provider}.x")', "PublicationRoute(f'{provider}.x')"),
        ('PublicationRoute(False, name, "x", False, None)', "PublicationRoute(name)"),
        ("replace(route, tool=name)", "replace(name)"),
        ("partial(helper, tool=name)", "partial(name)"),
        ("insert(RunAction).values(tool=name)", "values(name)"),
        ("factory()(tool=name)", "factory()(name)"),
        ("insert_run_action(session, **params)", "insert_run_action(**params)"),
        ("RunAction(**fields)", "RunAction(**fields)"),
        ("traced_step(trace, *rest)", "traced_step(*rest)"),
        ("trace.record(run_id, *rest)", "record(*rest)"),
        ("X_TOOL = make()", "X_TOOL(make())"),
        ('def f():\n    X_TOOL = "a.b"\n    record(run_id, X_TOOL)', "record(X_TOOL)"),
        ('X_TOOL = "a.b"\nrecord(run_id, other.X_TOOL)', "record(other.X_TOOL)"),
        ('from m import traced_step as ts\nts(trace, run_id, f"a.{x}", {})', "ts(f'a.{x}')"),
        ("write = trace.record\nwrite(run_id, name, {})", "write(name)"),
        ("write = trace.record\nwrite(run_id, *rest)", "write(*rest)"),
        ("make = RunAction\nmake(**fields)", "make(**fields)"),
        ('partial(trace.record, run_id, "a.b")', "partial(trace.record)"),
        ("partial(traced_step, trace, run_id)", "partial(traced_step)"),
        ("schedule(writer=trace.add_action)", "schedule(trace.add_action)"),
        ("write = trace.record\nschedule(write)", "schedule(write)"),
    ],
    ids=[
        "f-string",
        "concatenation",
        "constant-without-the-suffix",
        "add_action-name",
        "conditional",
        "publication-route-keyword",
        "publication-route-position",
        "keyword-on-a-helper",
        "keyword-on-a-partial",
        "keyword-on-an-insert",
        "keyword-on-a-computed-callee",
        "double-star-on-the-sink",
        "double-star-on-the-model",
        "star-before-the-position",
        "star-at-the-position",
        "computed-constant",
        "local-constant",
        "attribute-of-a-constant",
        "import-alias",
        "method-alias",
        "star-on-a-method-alias",
        "double-star-on-a-model-alias",
        "partial-of-a-method",
        "partial-of-a-function",
        "method-as-a-keyword",
        "alias-as-an-argument",
    ],
)
def test_written_tools_scan_reports_a_tool_it_cannot_read(source: str, expected: str) -> None:
    assert _unread(source) == [expected]


@pytest.mark.parametrize(
    "sources",
    [
        ('X_TOOL = "a.b"\ntrace.record(run_id, X_TOOL, {})',),
        ('X_TOOL: Final = "a.b"\ninsert_run_action(session, tool=X_TOOL)',),
        ('X_TOOL = "a.b"', "trace.record(run_id, X_TOOL, {})"),
    ],
    ids=["same-module", "annotated", "imported"],
)
def test_written_tools_scan_reads_a_module_tool_constant(sources: tuple[str, ...]) -> None:
    assert _unread(*sources) == []


@pytest.mark.parametrize(
    "source",
    [
        "parse(record)",
        "select(RunAction)",
        "record = locked.record\nrecord.external_id",
        "index(((item.record, item.edges) for item in imports))",
        "from m import RunAction as RunActionProjection\nRunActionProjection(id=1)",
    ],
    ids=["variable", "model", "field", "field-in-a-tuple", "model-alias"],
)
def test_written_tools_scan_leaves_common_uses_of_a_writer_name_alone(source: str) -> None:
    assert _unread(source) == []


def test_written_tools_scan_names_the_file_and_the_line() -> None:
    source = 'class Stage:\n    def run(self):\n        record(run_id, f"a.{x}")\n'

    (site,) = _unread_tools([_scan_tool_writes(source, "app/stage.py")])

    assert str(site) == "app/stage.py:3 Stage.run -> record(f'a.{x}')"


def _catalog_drift(side: str, found: set[str]) -> str:
    missing, unexpected = sorted(RUN_ACTION_TOOLS - found), sorted(found - RUN_ACTION_TOOLS)
    return f"{side} vs catalog: missing {missing}, unexpected {unexpected}"


def _scan_app() -> list[_ToolScan]:
    return [
        _scan_tool_writes(
            path.read_text(encoding="utf-8"), path.relative_to(APP_PATH.parent).as_posix()
        )
        for path in sorted(APP_PATH.rglob("*.py"))
    ]


def test_run_action_tool_catalog_matches_the_tools_the_code_writes() -> None:
    scans = _scan_app()

    unread = "\n".join(str(site) for site in _unread_tools(scans))
    assert not unread, (
        "a tool name the scan cannot read: pass a literal or a module-level *_TOOL constant, call "
        f"the writer directly, or add the place to TOOL_PASS_THROUGH with why it is safe\n{unread}"
    )
    written = {name for scan in scans for name in scan.names}
    assert written == RUN_ACTION_TOOLS, _catalog_drift("code", written)


def test_run_action_tool_pass_through_entries_match_a_call_in_the_code() -> None:
    passed = {site.key for scan in _scan_app() for site in scan.sites}

    stale = sorted(TOOL_PASS_THROUGH - passed)
    assert not stale, f"remove the TOOL_PASS_THROUGH entries that match no call: {stale}"


_RUN_TRACE = f"{_REVIEWS}/application/run_trace.py"
_TRACED_STEP = "async def traced_step(trace, run_id, tool):\n    await trace.record(run_id, tool)\n"


def test_run_action_tool_pass_through_vouches_for_its_exact_place() -> None:
    assert _unread_tools([_scan_tool_writes(_TRACED_STEP, _RUN_TRACE)]) == []


@pytest.mark.parametrize(
    ("source", "path"),
    [
        (_TRACED_STEP, f"{_REVIEWS}/application/other.py"),
        (_TRACED_STEP.replace("traced_step", "other_step"), _RUN_TRACE),
        (_TRACED_STEP.replace("trace.record", "trace.add_action"), _RUN_TRACE),
        (_TRACED_STEP.replace("record(run_id, tool)", "record(run_id, name)"), _RUN_TRACE),
    ],
    ids=["other-file", "other-function", "other-callee", "other-argument"],
)
def test_run_action_tool_pass_through_vouches_for_nothing_else(source: str, path: str) -> None:
    assert len(_unread_tools([_scan_tool_writes(source, path)])) == 1


@pytest.mark.parametrize(
    "location", [("RunAction", "properties", "tool"), ("RunStatus",)], ids=".".join
)
def test_run_action_tool_descriptions_list_the_catalog(location: tuple[str, ...]) -> None:
    schema: Mapping[str, Any] = _components()
    for key in location:
        schema = schema[key]

    listed = set(BACKTICKED_TOOL.findall(schema["description"]))

    assert listed == RUN_ACTION_TOOLS, _catalog_drift(f"{'.'.join(location)} description", listed)
