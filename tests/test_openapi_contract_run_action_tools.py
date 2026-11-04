"""The ``RunAction.tool`` catalog against the code and the spec (#165, #210)."""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from typing import Any

import pytest

from app.modules.reviews.application.publish_run_review import PublicationRoute
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
        "review.finalize",
        "review.publish_start",
        "github.publish_review",
        "gitlab.publish_review",
        "bitbucket.publish_review",
    }
)


# A backticked `<lower>.<lower_snake>` token; state names and docs/ paths do not match.
BACKTICKED_TOOL = re.compile(r"`([a-z]+\.[a-z_]+)`")


# The calls that take the tool by position, and where; any call can also take it as ``tool=``.
TOOL_POSITIONS = {"traced_step": 2, "record": 1, "add_action": 1}
# A star argument on one of these can carry the tool past the scan. PublicationRoute and RunAction
# take fields by keyword only, however the class is spelled: ``cls(...)``, ``type(route)(...)``.
TOOL_WRITERS = frozenset(TOOL_POSITIONS) | {"insert_run_action", "RunAction", "PublicationRoute"}
# The callee of a site that names a writer without calling it.
_REFERENCE = "<reference>"
# The callee of a site that gives a mapping key or a variable named ``tool`` its value.
_TOOL_FIELD = "tool"
# The calls that take the RunAction model to build a read; ``RunAction.column`` is one too. Any
# other use of it is a site: a write through ``insert``, ``update`` or ``query`` can take the tool
# from anywhere later.
_MODEL_READS = frozenset({"select", "select_from", "join", "outerjoin"})
# The calls that run code given as text, which no scan can read.
_CODE_RUNNERS = frozenset({"eval", "exec"})
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
        # PublicationRoute takes ``tool=`` by keyword only, so the scan reads each name it gets.
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
        # A field named `record` on a locked row or an import item, not RunTrace.record: only read.
        (
            "app/modules/code_index/application/build_index.py",
            "_Job._index",
            _REFERENCE,
            "item.record",
        ),
        (
            f"{_REVIEWS}/application/project_github_pull_request.py",
            "ProjectGitHubPullRequest._project_label_under_lock",
            _REFERENCE,
            "locked.record",
        ),
        (
            f"{_REVIEWS}/application/project_github_pull_request.py",
            "ProjectGitHubPullRequest._project",
            _REFERENCE,
            "locked.record",
        ),
        (
            f"{_REVIEWS}/application/project_gitlab_merge_request.py",
            "ProjectGitLabMergeRequest.execute",
            _REFERENCE,
            "locked.record",
        ),
        # The read model of get_run_actions, not the run_actions table: it writes no row.
        (
            f"{_REVIEWS}/infrastructure/run_repository.py",
            "<module>",
            _REFERENCE,
            "RunAction as RunActionProjection",
        ),
        # The column itself, pinned whole: a default added to it would change this entry.
        (
            f"{_REVIEWS}/infrastructure/models.py",
            "RunAction",
            _TOOL_FIELD,
            "mapped_column(String(100), nullable=False)",
        ),
        # A stored name on its way out in a NOTIFY payload or an SSE frame: no row is written.
        (
            f"{_REVIEWS}/infrastructure/run_action_records.py",
            "insert_run_action",
            _TOOL_FIELD,
            "tool",
        ),
        ("app/bootstrap/run_step_listener.py", "_event", _TOOL_FIELD, "str(data['tool'])"),
        ("app/main.py", "_run_step_frame", _TOOL_FIELD, "step.tool"),
        ("app/bootstrap/run_step_listener.py", "_event", _REFERENCE, "'tool'"),
        # The table's own name and a constraint and an index named after it, in the model that
        # defines them.
        (f"{_REVIEWS}/infrastructure/models.py", "RunAction", _REFERENCE, "'run_actions'"),
        (
            f"{_REVIEWS}/infrastructure/models.py",
            "RunAction",
            _REFERENCE,
            "'ck_run_actions_response_location'",
        ),
        (
            f"{_REVIEWS}/infrastructure/models.py",
            "RunAction",
            _REFERENCE,
            "'ix_run_actions_postprocess_run_index'",
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
    module: str
    names: set[str] = field(default_factory=set)
    constants: set[str] = field(default_factory=set)
    imported: set[str] = field(default_factory=set)
    sites: set[_ToolSite] = field(default_factory=set)


def _module_name(path: str) -> str:
    return path.removesuffix(".py").removesuffix("/__init__").replace("/", ".")


def _string_constant(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _last_name(node: ast.AST) -> str | None:
    """The name an expression ends in: ``a`` for both ``a`` and ``x.a``."""
    if isinstance(node, ast.Name):
        return node.id
    return node.attr if isinstance(node, ast.Attribute) else None


def _callee(call: ast.Call) -> str:
    return _last_name(call.func) or ast.unparse(call.func)


def _writer_reference(node: ast.Name | ast.Attribute | ast.alias) -> str | None:
    """A writer named without calling it, since the scan cannot follow what happens to it next.

    A bare ``record`` is a common local variable, so it does not count: binding the method to any
    name has to read ``x.record`` first, and that does.
    """
    if isinstance(node, ast.alias):
        renamed = node.name in TOOL_WRITERS and node.asname not in (None, node.name)
        return f"{node.name} as {node.asname}" if renamed else None
    if not isinstance(node.ctx, ast.Load) or _last_name(node) not in TOOL_WRITERS:
        return None
    return None if isinstance(node, ast.Name) and node.id == "record" else ast.unparse(node)


def _names_a_writer_as_text(text: str) -> bool:
    """Text that can reach a writer, the column or the table without naming them in code."""
    return (
        text in TOOL_WRITERS
        or text == "tool"
        or text.endswith("_TOOL")
        or "run_actions" in text.lower()
    )


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


_NameBinding = (
    ast.Name
    | ast.Attribute
    | ast.arg
    | ast.keyword
    | ast.alias
    | ast.ExceptHandler
    | ast.MatchAs
    | ast.MatchStar
    | ast.TypeVar
    | ast.ParamSpec
    | ast.TypeVarTuple
)


def _bound_name(node: _NameBinding) -> str | None:
    if isinstance(node, ast.Name | ast.Attribute):
        return _last_name(node) if isinstance(node.ctx, ast.Store) else None
    if isinstance(node, ast.arg | ast.keyword):
        return node.arg
    return (node.asname or node.name) if isinstance(node, ast.alias) else node.name


def _parameter_defaults(parameters: ast.arguments) -> list[tuple[ast.arg, ast.expr]]:
    positional = parameters.posonlyargs + parameters.args
    defaults = zip(reversed(positional), reversed(parameters.defaults), strict=False)
    keyword_only = zip(parameters.kwonlyargs, parameters.kw_defaults, strict=True)
    found = [(parameter, default) for parameter, default in keyword_only if default is not None]
    return [*defaults, *found]


def _scan_tool_writes(source: str, path: str = "<source>") -> _ToolScan:
    """Read the tool names of one module from the shapes that can reach run_actions.

    The scanned shapes: a module-level ``NAME_TOOL = ...`` assignment, a slot of
    ``TOOL_POSITIONS``, a ``tool=`` keyword, a ``"tool"`` mapping key, a value or a default given
    to anything named ``tool``. A string constant there is a name. Anything else becomes a site,
    which ``_unread_tools`` reports unless it can vouch for it. These count on any call and any
    receiver, so a ``tool="x.y"`` or a ``ledger.record(context, "x.y")`` that does not write
    run_actions surfaces as unexpected and must be renamed or the scan taught to skip it. A blanket
    ``"x.y"`` match would also pick up prompt names such as ``review.system``, which are not tools.

    These become sites too, wherever the code goes on from them:

    - a writer named without calling it, and a star import, which can bring one in;
    - a ``*_TOOL`` or ``tool`` name bound any other way than the above, except a plain ``tool``
      parameter, whose caller is what gets read;
    - an import of a ``*_TOOL`` name, which ``_unread_tools`` vouches for only if the module it
      names defines it;
    - text spelling a writer, ``tool``, a ``*_TOOL`` name or the table, outside a bare string
      statement such as a docstring, and a call of ``eval`` or ``exec``: that is how a name reaches
      them through ``getattr``, ``setattr`` or raw SQL.

    An annotation is scanned for calls, but a name in it is no reference. What stays out of sight
    is a name computed at runtime: ``"to" + "ol"`` or ``RunAction.tool.key``.
    """
    tree = ast.parse(source)
    scan = _ToolScan(_module_name(path))
    known_bindings: set[ast.AST] = set()
    plain_uses: set[ast.AST] = set()
    targets: list[ast.expr]
    value: ast.expr | None

    def read(argument: ast.expr | ast.keyword, callee: str, line: int, scope: str) -> None:
        name = _string_constant(argument)
        if name is None:
            scan.sites.add(_ToolSite(path, line, scope, callee, ast.unparse(argument)))
        else:
            scan.names.add(name)

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
            known_bindings.add(target)
            read(value, target.id, statement.lineno, "<module>")
            if _string_constant(value) is not None:
                scan.constants.add(target.id)
    pending: list[tuple[ast.AST, str]] = [(tree, "<module>")]
    while pending:
        node, scope = pending.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            scope = node.name if scope == "<module>" else f"{scope}.{node.name}"
        for annotation in getattr(node, "annotation", None), getattr(node, "returns", None):
            if annotation is not None:
                plain_uses.update(ast.walk(annotation))
        pending += [(child, scope) for child in ast.iter_child_nodes(node)]
        if isinstance(node, ast.Assign | ast.AnnAssign | ast.NamedExpr):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            direct = [target for target in targets if _last_name(target) == "tool"]
            known_bindings.update(direct)
            if direct and node.value is not None:
                read(node.value, _TOOL_FIELD, node.lineno, scope)
        if isinstance(node, ast.arguments):
            for parameter, default in _parameter_defaults(node):
                if parameter.arg == "tool":
                    read(default, _TOOL_FIELD, parameter.lineno, scope)
        if isinstance(node, ast.Dict):
            for key, item in zip(node.keys, node.values, strict=True):
                if key is not None and _string_constant(key) == "tool":
                    plain_uses.add(key)
                    read(item, _TOOL_FIELD, key.lineno, scope)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            plain_uses.add(node.value)
        if isinstance(node, ast.Constant) and node not in plain_uses:
            literal = node.value
            text = literal.decode("utf-8", "replace") if isinstance(literal, bytes) else literal
            if isinstance(text, str) and _names_a_writer_as_text(text):
                scan.sites.add(_ToolSite(path, node.lineno, scope, _REFERENCE, repr(literal)))
        if isinstance(node, ast.ImportFrom):
            source = "." * node.level + (node.module or "")
            for alias in node.names:
                if alias.name == "*":
                    scan.sites.add(_ToolSite(path, node.lineno, scope, _REFERENCE, f"{source}.*"))
                elif alias.name.endswith("_TOOL") and alias.asname is None:
                    known_bindings.add(alias)
                    scan.imported.add(alias.name)
                    origin = f"{source}.{alias.name}"
                    scan.sites.add(_ToolSite(path, node.lineno, scope, alias.name, origin))
        if isinstance(node, _NameBinding) and node not in known_bindings:
            bound = _bound_name(node)
            if bound is not None and (
                bound.endswith("_TOOL")
                or (bound == "tool" and not isinstance(node, ast.arg | ast.keyword))
            ):
                scan.sites.add(_ToolSite(path, node.lineno, scope, bound, "rebound"))
        if isinstance(node, ast.Name | ast.Attribute | ast.alias) and node not in plain_uses:
            reference = _writer_reference(node)
            if reference is not None:
                scan.sites.add(_ToolSite(path, node.lineno, scope, _REFERENCE, reference))
        if (
            isinstance(node, ast.Attribute)
            and _last_name(node.value) == "RunAction"
            and not node.attr.startswith("_")
        ):
            plain_uses.add(node.value)
        if not isinstance(node, ast.Call):
            continue
        callee = _callee(node)
        plain_uses.add(node.func)
        if callee in _CODE_RUNNERS:
            scan.sites.add(_ToolSite(path, node.lineno, scope, callee, "<code>"))
        if callee in _MODEL_READS:
            plain_uses.update(arg for arg in node.args if _last_name(arg) == "RunAction")
        for argument in _tool_arguments(node, callee):
            read(argument, callee, node.lineno, scope)
    return scan


def _written_tools(source: str) -> set[str]:
    return _scan_tool_writes(source).names


def _unread_tools(scans: list[_ToolScan]) -> list[_ToolSite]:
    """The sites of ``scans`` that are not in the allowlist and do not resolve to a constant.

    A bare ``*_TOOL`` name resolves in its own module only, to a constant defined or imported
    there. An import is a site of its own, and resolves if the module it names defines the constant.
    """
    defined = {f"{scan.module}.{constant}" for scan in scans for constant in scan.constants}
    return sorted(
        site
        for scan in scans
        for site in scan.sites
        if site.key not in TOOL_PASS_THROUGH
        and site.argument not in defined | scan.constants | scan.imported
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
        ('prompts.load("review.system")', set()),
        ('uow.trace.add_action(run_id, "i.j", {}, None, now, 0)', {"i.j"}),
        ('PublicationRoute(gitlab=False, tool="k.l")', {"k.l"}),
        ('cls(gitlab=False, tool="k.m")', {"k.m"}),
        ('type(route)(gitlab=False, tool="k.n")', {"k.n"}),
        ('route.__class__(gitlab=False, tool="k.o")', {"k.o"}),
        ('session.execute(statement.values({"tool": "probe.sql"}))', {"probe.sql"}),
        ('session.execute(statement, [{"tool": "probe.sql"}])', {"probe.sql"}),
        ('RunAction(**{"tool": "probe.x"})', {"probe.x"}),
        ('row.tool = "probe.attr"', {"probe.attr"}),
        ('tool = "m.n"', {"m.n"}),
        ('tool: str = "o.p"', {"o.p"}),
        ('(tool := "q.r")', {"q.r"}),
        ('def record(self, run_id, tool="x.y"): ...', {"x.y"}),
        ('def record(self, run_id, *, tool="s.t"): ...', {"s.t"}),
        ('def record(self, tool="u.v", /): ...', {"u.v"}),
        ('f = lambda tool="w.x": tool', {"w.x"}),
        ('def f(row: RunAction(tool="a.c") = None): ...', {"a.c"}),
        ('def f() -> RunAction(tool="a.d"): ...', {"a.d"}),
        ('x: trace.record(run_id, "a.e", {}) = 1', {"a.e"}),
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
        "plain-string-literal",
        "add_action-second-argument",
        "publication-route-keyword",
        "publication-route-as-cls",
        "publication-route-as-type-of",
        "publication-route-as-dunder-class",
        "mapping-in-values",
        "mapping-in-execute-rows",
        "mapping-unpacked",
        "attribute-of-a-row",
        "variable",
        "annotated-variable",
        "walrus",
        "parameter-default",
        "keyword-only-default",
        "positional-only-default",
        "lambda-default",
        "call-in-a-parameter-annotation",
        "call-in-a-return-annotation",
        "call-in-a-variable-annotation",
    ],
)
def test_written_tools_scan_picks_up_only_the_run_action_shapes(
    source: str, expected: set[str]
) -> None:
    assert _written_tools(source) == expected


def _unread(*sources: str) -> list[str]:
    """Scan the sources as the modules ``m0``, ``m1``, ... and list what stays unread."""
    scans = [_scan_tool_writes(source, f"m{index}.py") for index, source in enumerate(sources)]
    return [f"{site.callee}({site.argument})" for site in _unread_tools(scans)]


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
        ("replace(route, tool=name)", "replace(name)"),
        ("partial(helper, tool=name)", "partial(name)"),
        ("statement.values(tool=name)", "values(name)"),
        ("factory()(tool=name)", "factory()(name)"),
        ("insert_run_action(session, **params)", "insert_run_action(**params)"),
        ("RunAction(**fields)", "RunAction(**fields)"),
        ("traced_step(trace, *rest)", "traced_step(*rest)"),
        ("trace.record(run_id, *rest)", "record(*rest)"),
        ("X_TOOL = make()", "X_TOOL(make())"),
        ('X_TOOL = "a.b"\nrecord(run_id, other.X_TOOL)', "record(other.X_TOOL)"),
        ('statement.values({"tool": name})', "tool(name)"),
        ("row.tool = name", "tool(name)"),
        ("tool = make()", "tool(make())"),
        ("tool: str = make()", "tool(make())"),
        ('column: Mapped[str] = mapped_column(default="a.b")\ntool = column', "tool(column)"),
        ('tool: Mapped[str] = mapped_column(default="a.b")', "tool(mapped_column(default='a.b'))"),
        ("def f(tool=make()): ...", "tool(make())"),
        ("for tool in names: ...", "tool(rebound)"),
        ("with make() as tool: ...", "tool(rebound)"),
        ("tool, other = pair", "tool(rebound)"),
        ('tool += ".x"', "tool(rebound)"),
        ("from m import name as tool", "tool(rebound)"),
    ],
    ids=[
        "f-string",
        "concatenation",
        "constant-without-the-suffix",
        "add_action-name",
        "conditional",
        "publication-route-keyword",
        "keyword-on-a-helper",
        "keyword-on-a-partial",
        "keyword-on-values",
        "keyword-on-a-computed-callee",
        "double-star-on-the-sink",
        "double-star-on-the-model",
        "star-before-the-position",
        "star-at-the-position",
        "computed-constant",
        "attribute-of-a-constant",
        "mapping-variable",
        "attribute-of-a-row-variable",
        "variable-computed",
        "annotated-variable-computed",
        "variable-from-a-variable",
        "column-default",
        "parameter-default-computed",
        "loop",
        "with",
        "tuple",
        "augmented",
        "import-alias",
    ],
)
def test_written_tools_scan_reports_a_tool_it_cannot_read(source: str, expected: str) -> None:
    assert _unread(source) == [expected]


@pytest.mark.parametrize(
    "sources",
    [
        ('X_TOOL = "a.b"\ntrace.record(run_id, X_TOOL, {})',),
        ('X_TOOL: Final = "a.b"\ninsert_run_action(session, tool=X_TOOL)',),
        ('X_TOOL = "a.b"', "from m0 import X_TOOL\ntrace.record(run_id, X_TOOL, {})"),
        ('X_TOOL = "a.b"', "def f():\n    from m0 import X_TOOL\n    record(run_id, X_TOOL)"),
        ('X_TOOL = "a.b"', "import m0\nrecord(run_id, m0.X_TOOL)"),
    ],
    ids=["same-module", "annotated", "imported", "imported-in-a-function", "attribute-of-a-module"],
)
def test_written_tools_scan_reads_a_module_tool_constant(sources: tuple[str, ...]) -> None:
    assert _unread(*sources) == []


@pytest.mark.parametrize(
    "source",
    [
        "parse(record)",
        "select(RunAction)",
        "statement.select_from(RunAction).outerjoin(RunAction)",
        "query.where(RunAction.run_id == run_id)",
        "query.where(models.RunAction.run_id == run_id)",
        "select(models.RunAction)",
        "statement.join(RunAction)",
        "tool: str",
        "def f(tool): ...",
        "def f(tool: str, /, *, other: int = 1): ...",
        'x: Literal["tool", "record"] = make()',
        'def f(a: "RunAction", b: traced_step) -> "run_actions": ...',
        "route.tool",
        "name == tool",
        '"""Insert one run_actions row, record its tool."""',
        'def f():\n    """record"""',
        'label = "records of a toolbox"',
        "compile(pattern)",
        "from m import traced_step",
        "from m import traced_step as traced_step",
        "def f(route: PublicationRoute) -> PublicationRoute: ...",
        "route: PublicationRoute = make()",
        "self.record = value",
        'trace.record(run_id, "a.b", {})',
    ],
    ids=[
        "variable",
        "model-in-a-query",
        "model-joined",
        "model-column",
        "model-column-of-a-module",
        "model-of-a-module-in-a-query",
        "model-in-a-join",
        "tool-field-without-a-value",
        "tool-parameter",
        "tool-parameter-with-other-defaults",
        "text-in-a-variable-annotation",
        "names-in-annotations",
        "tool-read",
        "tool-compared",
        "docstring-of-a-module",
        "docstring-of-a-function",
        "text-that-only-contains-a-writer-name",
        "compile-is-not-a-code-runner",
        "plain-import",
        "import-as-itself",
        "annotations",
        "annotated-variable",
        "stored-field",
        "direct-call",
    ],
)
def test_written_tools_scan_leaves_common_uses_of_a_writer_name_alone(source: str) -> None:
    assert _unread(source) == []


@pytest.mark.parametrize(
    ("source", "reference"),
    [
        (
            'from m import traced_step as ts\nts(trace, run_id, "probe.alias", {})',
            "traced_step as ts",
        ),
        (
            'from m import traced_step as record\ntrace.record(run_id, "a.b")',
            "traced_step as record",
        ),
        ('write = trace.record\nwrite(run_id, "probe.alias", {})', "trace.record"),
        ('write: Writer = trace.add_action\nwrite(run_id, "a.b", {})', "trace.add_action"),
        ('self._write = trace.record\nself._write(run_id, "a.b", {})', "trace.record"),
        ('(write := trace.record)\nwrite(run_id, "a.b", {})', "trace.record"),
        ('first = trace.record\nsecond = first\nsecond(run_id, "a.b", {})', "trace.record"),
        ('write, other = trace.record, 1\nwrite(run_id, "a.b", {})', "trace.record"),
        ('write = trace.record if flag else None\nwrite(run_id, "a.b", {})', "trace.record"),
        ('write = trace.record or noop\nwrite(run_id, "a.b", {})', "trace.record"),
        ('def f(write=trace.record):\n    write(run_id, "a.b", {})', "trace.record"),
        ('def f(*, write=traced_step):\n    write(trace, run_id, "a.b", {})', "traced_step"),
        ('g = lambda write=trace.record: write(run_id, "a.b", {})', "trace.record"),
        ('partial(trace.record, run_id, "a.b")', "trace.record"),
        ("partial(traced_step, trace, run_id)", "traced_step"),
        ("schedule(writer=trace.add_action)", "trace.add_action"),
        ("writers = {'write': trace.record}", "trace.record"),
        ("def f(trace):\n    return trace.record", "trace.record"),
        ('trace.record.__call__(run_id, "a.b", {})', "trace.record"),
        ('make = PublicationRoute\nmake(False, "a.b", "x", False, None)', "PublicationRoute"),
        ('class R(PublicationRoute): ...\nR(False, "a.b", "x", False, None)', "PublicationRoute"),
        ("make = RunAction\nmake(**fields)", "RunAction"),
        ("class Row(RunAction): ...\nRow(**fields)", "RunAction"),
        ("partial(RunAction, **fields)", "RunAction"),
        ("RunAction.__call__(**fields)", "RunAction"),
        ("RunAction.__init__(row, **fields)", "RunAction"),
        ("record = locked.record\nrecord.external_id", "locked.record"),
        ('session.execute(insert(RunAction).values({"tool_name": "a.b"}))', "RunAction"),
        ("session.execute(insert(RunAction).values(fields))", "RunAction"),
        ("session.execute(insert(RunAction).values(**fields))", "RunAction"),
        ("session.execute(insert(RunAction), rows)", "RunAction"),
        ("session.execute(update(RunAction).values(fields))", "RunAction"),
        ("insert(models.RunAction)", "models.RunAction"),
        ("session.bulk_insert_mappings(RunAction, rows)", "RunAction"),
        ("session.query(RunAction).update(fields)", "RunAction"),
        ("delete(RunAction)", "RunAction"),
        ("session.get(RunAction, action_id)", "RunAction"),
        ("aliased(RunAction)", "RunAction"),
        ("isinstance(value, RunAction)", "RunAction"),
    ],
    ids=[
        "import-alias",
        "import-alias-named-like-a-writer",
        "method-alias",
        "annotated-alias",
        "attribute-alias",
        "walrus-alias",
        "alias-of-an-alias",
        "tuple-alias",
        "conditional-alias",
        "or-alias",
        "default-of-a-parameter",
        "default-of-a-keyword-parameter",
        "default-of-a-lambda",
        "partial-of-a-method",
        "partial-of-a-function",
        "method-as-a-keyword",
        "in-a-container",
        "returned",
        "dunder-call",
        "class-alias",
        "subclass",
        "model-alias",
        "model-subclass",
        "model-in-a-partial",
        "model-dunder-call",
        "model-dunder-init",
        "field-named-like-a-method",
        "model-in-an-insert-with-a-mapping",
        "model-in-an-insert-with-a-variable",
        "model-in-an-insert-unpacked",
        "model-in-an-insert-with-rows",
        "model-in-an-update",
        "model-of-a-module-in-an-insert",
        "model-in-bulk-mappings",
        "model-in-a-session-query",
        "model-in-a-delete",
        "model-in-a-session-get",
        "model-aliased",
        "model-in-isinstance",
    ],
)
def test_written_tools_scan_reports_a_writer_used_other_than_by_a_call(
    source: str, reference: str
) -> None:
    assert _unread(source) == [f"<reference>({reference})"]


_RAW_INSERT = "INSERT INTO run_actions (tool) VALUES ('probe.sql')"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('getattr(trace, "record")(run_id, "a.b", {})', "<reference>('record')"),
        ('methodcaller("record", run_id, "a.b", {})(trace)', "<reference>('record')"),
        ('globals()["traced_step"](trace, run_id, "a.b", {})', "<reference>('traced_step')"),
        ('getattr(models, "RunAction")(**fields)', "<reference>('RunAction')"),
        ('name = "record"\ngetattr(trace, name)', "<reference>('record')"),
        ('setattr(row, "tool", name)', "<reference>('tool')"),
        ('row.__dict__["tool"] = name', "<reference>('tool')"),
        ('column("tool")', "<reference>('tool')"),
        ('setattr(module, "LLM_CALL_TOOL", "a.b")', "<reference>('LLM_CALL_TOOL')"),
        ('globals()["LLM_CALL_TOOL"] = "a.b"', "<reference>('LLM_CALL_TOOL')"),
        (f'session.execute(text("{_RAW_INSERT}"))', f"<reference>({_RAW_INSERT!r})"),
        (
            'text(f"UPDATE run_actions SET x = {name}")',
            "<reference>('UPDATE run_actions SET x = ')",
        ),
        (
            'conn.exec_driver_sql(b"DELETE FROM run_actions")',
            "<reference>(b'DELETE FROM run_actions')",
        ),
        (
            'exec_driver_sql("insert into RUN_ACTIONS values (1)")',
            "<reference>('insert into RUN_ACTIONS values (1)')",
        ),
        ('table("run_actions", column_of_tool)', "<reference>('run_actions')"),
        ('Base.metadata.tables["run_actions"].insert()', "<reference>('run_actions')"),
        ('eval("trace.record(run_id, name, {})")', "eval(<code>)"),
        ("exec(source)", "exec(<code>)"),
    ],
    ids=[
        "getattr-of-a-method",
        "methodcaller",
        "globals-of-a-function",
        "getattr-of-the-model",
        "name-in-a-variable",
        "setattr-of-the-column",
        "dict-of-a-row",
        "column-by-name",
        "setattr-of-a-constant",
        "globals-of-a-constant",
        "raw-insert",
        "raw-update-in-an-f-string",
        "raw-sql-in-bytes",
        "raw-sql-in-capitals",
        "table-by-name",
        "metadata-by-name",
        "eval",
        "exec",
    ],
)
def test_written_tools_scan_reports_a_name_given_as_text(source: str, expected: str) -> None:
    assert _unread(source) == [expected]


def test_written_tools_scan_reports_an_alias_made_in_another_module() -> None:
    importer = 'from a import ts\nts(trace, run_id, "probe.cross", {})'

    assert _unread("ts = traced_step", importer) == ["<reference>(traced_step)"]


@pytest.mark.parametrize(
    "rebinding",
    [
        'def f():\n    LLM_CALL_TOOL = "probe.shadow"\n    record(run_id, LLM_CALL_TOOL, {})',
        "def f(LLM_CALL_TOOL):\n    record(run_id, LLM_CALL_TOOL)",
        "for LLM_CALL_TOOL in names:\n    record(run_id, LLM_CALL_TOOL)",
        "with make() as LLM_CALL_TOOL:\n    record(run_id, LLM_CALL_TOOL)",
        "if (LLM_CALL_TOOL := make()):\n    record(run_id, LLM_CALL_TOOL)",
        "match name:\n    case LLM_CALL_TOOL:\n        record(run_id, LLM_CALL_TOOL)",
        "from m import NAME as LLM_CALL_TOOL\nrecord(run_id, LLM_CALL_TOOL)",
        "if flag:\n    LLM_CALL_TOOL = make()\nrecord(run_id, LLM_CALL_TOOL)",
        'class Stage:\n    LLM_CALL_TOOL = "probe.shadow"\n    record(run_id, LLM_CALL_TOOL)',
        "def f():\n    global LLM_CALL_TOOL\n    LLM_CALL_TOOL = make()",
        "try:\n    pass\nexcept E as LLM_CALL_TOOL:\n    record(run_id, LLM_CALL_TOOL)",
        "match name:\n    case [*LLM_CALL_TOOL]:\n        record(run_id, LLM_CALL_TOOL)",
        "def f[LLM_CALL_TOOL](x):\n    record(run_id, LLM_CALL_TOOL)",
        "def f[**LLM_CALL_TOOL](x):\n    record(run_id, LLM_CALL_TOOL)",
        "def f[*LLM_CALL_TOOL](x):\n    record(run_id, LLM_CALL_TOOL)",
        'import m\nm.LLM_CALL_TOOL = "probe.x"',
        'vars(m).update(LLM_CALL_TOOL="probe.kw")',
    ],
    ids=[
        "local",
        "parameter",
        "loop",
        "with",
        "walrus",
        "match",
        "import-alias",
        "conditional",
        "class",
        "global",
        "except",
        "match-star",
        "type-parameter",
        "param-spec",
        "type-var-tuple",
        "attribute-of-a-module",
        "keyword-of-a-call",
    ],
)
def test_written_tools_scan_reports_a_rebound_tool_constant(rebinding: str) -> None:
    assert "LLM_CALL_TOOL(rebound)" in _unread('LLM_CALL_TOOL = "llm.call"', rebinding)


_USE = "record(run_id, LLM_CALL_TOOL, {})"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            f"from external_tools import LLM_CALL_TOOL\n{_USE}",
            ["LLM_CALL_TOOL(external_tools.LLM_CALL_TOOL)"],
        ),
        (f"from m9 import LLM_CALL_TOOL\n{_USE}", ["LLM_CALL_TOOL(m9.LLM_CALL_TOOL)"]),
        (f"from .m0 import LLM_CALL_TOOL\n{_USE}", ["LLM_CALL_TOOL(.m0.LLM_CALL_TOOL)"]),
        (f"from . import LLM_CALL_TOOL\n{_USE}", ["LLM_CALL_TOOL(..LLM_CALL_TOOL)"]),
        (
            f"def f():\n    from external_tools import LLM_CALL_TOOL\n    {_USE}",
            ["LLM_CALL_TOOL(external_tools.LLM_CALL_TOOL)"],
        ),
        (
            f"from m0 import LLM_CALL_TOOL\nfrom external_tools import LLM_CALL_TOOL\n{_USE}",
            ["LLM_CALL_TOOL(external_tools.LLM_CALL_TOOL)"],
        ),
        (
            f"from external_tools import *\n{_USE}",
            ["<reference>(external_tools.*)", "record(LLM_CALL_TOOL)"],
        ),
        (f"from m0 import *\n{_USE}", ["<reference>(m0.*)", "record(LLM_CALL_TOOL)"]),
        (f"import LLM_CALL_TOOL\n{_USE}", ["LLM_CALL_TOOL(rebound)", "record(LLM_CALL_TOOL)"]),
        ("from m0 import tool\nrecord(run_id, tool, {})", ["tool(rebound)", "record(tool)"]),
        (_USE, ["record(LLM_CALL_TOOL)"]),
    ],
    ids=[
        "other-package",
        "other-module",
        "relative",
        "relative-package",
        "inside-a-function",
        "second-import-over-a-good-one",
        "star",
        "star-from-a-known-module",
        "plain-import",
        "tool-variable-imported",
        "never-bound",
    ],
)
def test_written_tools_scan_reports_a_tool_constant_from_elsewhere(
    source: str, expected: list[str]
) -> None:
    assert _unread('LLM_CALL_TOOL = "llm.call"', source) == expected


def test_written_tools_scan_reports_an_import_of_a_reexported_tool_constant() -> None:
    sources = 'X_TOOL = "a.b"', "from m0 import X_TOOL", "from m1 import X_TOOL"

    assert _unread(*sources) == ["X_TOOL(m1.X_TOOL)"]


def test_written_tools_scan_reports_an_import_of_a_computed_tool_constant() -> None:
    sources = "X_TOOL = make()", "from m0 import X_TOOL"

    assert _unread(*sources) == ["X_TOOL(make())", "X_TOOL(m0.X_TOOL)"]


def test_written_tools_scan_resolves_a_tool_constant_in_a_package_module() -> None:
    package = _scan_tool_writes('X_TOOL = "a.b"', "app/x/__init__.py")
    importer = _scan_tool_writes("from app.x import X_TOOL", "app/y.py")

    assert _unread_tools([package, importer]) == []


def test_written_tools_scan_reports_a_local_tool_name_and_its_use() -> None:
    source = 'def f():\n    X_TOOL = "a.b"\n    record(run_id, X_TOOL)'

    assert _unread(source) == ["X_TOOL(rebound)", "record(X_TOOL)"]


def test_publication_route_takes_the_tool_by_keyword_only() -> None:
    assert all(item.kw_only for item in fields(PublicationRoute))


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
