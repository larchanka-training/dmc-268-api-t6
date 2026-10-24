"""Extract AST facts with tree-sitter (CONTEXT_AND_VERIFICATION_SPEC §3.3).

Runs inside the parser worker. Tree-sitter rows are 0-based and an end point at column 0
excludes that row; facts carry 1-based inclusive source lines. Nothing here resolves an
import or reads another file.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from importlib.metadata import version

import tree_sitter_go
import tree_sitter_javascript
import tree_sitter_python
import tree_sitter_typescript
from tree_sitter import Language, Node, Parser

from app.modules.reviews.application.ast_symbols import (
    MAX_REFERENCES_PER_SYMBOL,
    AstFacts,
    AstImport,
    AstImportName,
    AstLanguage,
    AstSymbol,
    ImportForm,
    ImportKind,
    SymbolKind,
)
from app.modules.reviews.application.context_provenance import LineRange

QUERY_VERSION = "facts/v2"
_PACKAGES = {
    AstLanguage.PYTHON: "tree-sitter-python",
    AstLanguage.TYPESCRIPT: "tree-sitter-typescript",
    AstLanguage.TSX: "tree-sitter-typescript",
    AstLanguage.JAVASCRIPT: "tree-sitter-javascript",
    AstLanguage.GO: "tree-sitter-go",
}
_JS_FUNCTION_VALUES = frozenset(
    {"arrow_function", "function_expression", "function", "generator_function"}
)
_JS_TYPE_KINDS = {
    "interface_declaration": "interface",
    "type_alias_declaration": "type",
    "enum_declaration": "enum",
}
_JS_FUNCTIONS = frozenset(
    {"function_declaration", "generator_function_declaration", "function_signature"}
)
_JS_CLASSES = frozenset({"class_declaration", "abstract_class_declaration", "class"})
_JS_METHODS = frozenset({"method_definition", "method_signature", "abstract_method_signature"})
_REFERENCE_TYPES = frozenset({"identifier", "type_identifier", "package_identifier"})
_PYTHON_IMPORTS = frozenset(
    {"import_statement", "import_from_statement", "future_import_statement"}
)
_PYTHON_LABEL_OWNERS = _PYTHON_IMPORTS | {
    "attribute",
    "keyword_argument",
    "keyword_pattern",
    "dotted_name",
    "function_definition",
}


class ParseTimeout(Exception):
    """The CPU budget of one file ran out before the tree was complete."""


def profile_version(language: AstLanguage) -> str:
    """Runtime, grammar and query versions that decide the facts of a blob."""
    package = _PACKAGES[language]
    return f"tree-sitter={version('tree-sitter')};{package}={version(package)};{QUERY_VERSION}"


_GRAMMARS: dict[AstLanguage, Language] = {}


def _grammar(language: AstLanguage) -> Language:
    # A grammar must outlive every tree parsed with it, so it is kept for the process.
    if language not in _GRAMMARS:
        if language is AstLanguage.PYTHON:
            capsule = tree_sitter_python.language()
        elif language is AstLanguage.TYPESCRIPT:
            capsule = tree_sitter_typescript.language_typescript()
        elif language is AstLanguage.TSX:
            capsule = tree_sitter_typescript.language_tsx()
        elif language is AstLanguage.JAVASCRIPT:
            capsule = tree_sitter_javascript.language()
        else:
            capsule = tree_sitter_go.language()
        _GRAMMARS[language] = Language(capsule)
    return _GRAMMARS[language]


def extract_facts(
    language: AstLanguage, source: bytes, *, cpu_seconds: float | None = None
) -> AstFacts:
    """Parse valid UTF-8 ``source`` and return its facts.

    tree-sitter is pinned below 0.26: release 0.26.0 of the Python binding corrupts
    memory on files with a few hundred declarations and crashes when a parse is
    cancelled. A running parse is therefore bounded from outside the process; the
    budget here stops the Python walk that follows it.
    """
    started = time.process_time()
    parser = Parser(_grammar(language))
    tree = parser.parse(source)
    deadline = None if cpu_seconds is None else started + cpu_seconds
    # The tree owns its nodes: it stays referenced until the facts are built.
    return _Extractor(language, tree.root_node, deadline).facts()


def _text(node: Node) -> str:
    return (node.text or b"").decode("utf-8", errors="replace")


def _lines(node: Node) -> LineRange:
    start, end = node.start_point, node.end_point
    last = end.row if end.column == 0 and end.row > start.row else end.row + 1
    return LineRange(start.row + 1, last)


def _walk(node: Node) -> Iterator[Node]:
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _unquote(node: Node) -> str:
    return _text(node).strip("\"'`")


def _python_labels(node: Node) -> Iterator[Node]:
    """Identifiers under ``node`` that are not name reads.

    Each names an attribute, a keyword, a method or a module rather than reading a
    binding: ``save`` in ``repository.save()`` or ``f(save=1)``.
    """
    kind = node.type
    if kind == "attribute":
        yield from node.children_by_field_name("attribute")
    elif kind == "keyword_argument":
        yield from node.children_by_field_name("name")
    elif kind == "keyword_pattern":
        yield from node.named_children[:1]
    elif kind == "dotted_name":
        # Outside an import only a pattern has one (`case Color.RED`): the rest are attributes.
        yield from node.named_children[1:]
    elif kind in _PYTHON_IMPORTS:
        # An import reads no binding: its names are module paths and the names it binds.
        yield from (item for item in _walk(node) if item.type == "identifier")
    elif kind == "function_definition":
        # A method name is an attribute of its class; a function name stays a binding.
        block = node.parent
        if block is not None and block.type == "decorated_definition":
            block = block.parent
        owner = block.parent if block is not None else None
        if owner is not None and owner.type == "class_definition":
            yield from node.children_by_field_name("name")


class _Extractor:
    def __init__(self, language: AstLanguage, root: Node, deadline: float | None) -> None:
        self._language = language
        self._root = root
        self._deadline = deadline
        self._symbols: list[AstSymbol] = []
        self._imports: list[AstImport] = []
        self._exports: list[tuple[str, int]] = []
        self._pending_exports: list[tuple[str, str]] = []
        self._package: str | None = None
        self._constrained = False
        self._errors = (
            [_lines(node) for node in _walk(root) if node.is_error or node.is_missing]
            if root.has_error
            else []
        )

    def facts(self) -> AstFacts:
        if self._language is AstLanguage.PYTHON:
            for node in self._root.named_children:
                self._python(node, None)
        elif self._language is AstLanguage.GO:
            self._go()
        else:
            self._javascript()
        top_level = {
            item.name: item.index
            for item in self._symbols
            if item.enclosing is None and item.name is not None
        }
        exports = dict(self._exports)
        for exported, local in self._pending_exports:
            if local in top_level:
                exports.setdefault(exported, top_level[local])
        return AstFacts(
            language=self._language,
            profile_version=profile_version(self._language),
            symbols=tuple(self._symbols),
            imports=tuple(self._imports),
            exports=tuple(sorted(exports.items())),
            package_name=self._package,
            build_constrained=self._constrained,
            has_errors=bool(self._errors),
        )

    def _check_budget(self) -> None:
        """Stop the walk once the CPU budget of the file is spent."""
        if self._deadline is not None and time.process_time() > self._deadline:
            raise ParseTimeout

    def _add(
        self,
        node: Node,
        *,
        kind: SymbolKind,
        name: str | None,
        declaration_kind: str | None = None,
        body: Node | None = None,
        anchor: Node | None = None,
        docstrings: tuple[LineRange, ...] | None = None,
        enclosing: int | None = None,
        exported: bool = False,
    ) -> int:
        """Record a symbol; ``anchor`` is the outermost node that belongs to it."""
        self._check_budget()
        outer = anchor or node
        declaration = _lines(outer)
        if body is not None and body.prev_sibling is not None:
            header_end = max(body.prev_sibling.end_point.row + 1, declaration.start_line)
            signature = LineRange(declaration.start_line, min(header_end, declaration.end_line))
        else:
            signature = declaration
        references, members, bindings = self._references(outer, body)
        index = len(self._symbols)
        self._symbols.append(
            AstSymbol(
                index=index,
                kind=kind,
                name=name,
                declaration_kind=declaration_kind,
                declaration_range=declaration,
                signature_ranges=(signature,),
                docstring_ranges=self._leading_comments(outer)
                if docstrings is None
                else docstrings,
                enclosing=enclosing,
                exported=exported,
                complete=not any(
                    error.start_line <= declaration.end_line
                    and declaration.start_line <= error.end_line
                    for error in self._errors
                ),
                references=references,
                member_references=members,
                local_bindings=bindings,
            )
        )
        return index

    def _leading_comments(self, node: Node) -> tuple[LineRange, ...]:
        """Comments that touch the declaration from above, as one range."""
        first = None
        expected_row = node.start_point.row
        sibling = node.prev_sibling
        while sibling is not None and sibling.type == "comment":
            if _lines(sibling).end_line != expected_row:
                break
            first = sibling
            expected_row = sibling.start_point.row
            sibling = sibling.prev_sibling
        if first is None:
            return ()
        return (LineRange(first.start_point.row + 1, node.start_point.row),)

    def _references(
        self, node: Node, body: Node | None
    ) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...], tuple[str, ...]]:
        names: set[str] = set()
        members: set[tuple[str, str]] = set()
        bindings: set[str] = set()
        # A label is met after the node that names it, so it is marked before it is read.
        labels: set[int] = set()
        python = self._language is AstLanguage.PYTHON
        for current in _walk(node):
            kind = current.type
            if python and kind in _PYTHON_LABEL_OWNERS:
                labels.update(item.id for item in _python_labels(current))
            if kind in _REFERENCE_TYPES:
                if current.id not in labels:
                    names.add(_text(current))
            elif kind in {"attribute", "member_expression", "selector_expression"}:
                owner = current.child_by_field_name("object") or current.child_by_field_name(
                    "operand"
                )
                member = (
                    current.child_by_field_name("attribute")
                    or current.child_by_field_name("property")
                    or current.child_by_field_name("field")
                )
                if owner is not None and member is not None and owner.type == "identifier":
                    members.add((_text(owner), _text(member)))
            elif kind in {"nested_type_identifier", "qualified_type"}:
                owner = current.child_by_field_name("module") or current.child_by_field_name(
                    "package"
                )
                member = current.child_by_field_name("name")
                if owner is not None and member is not None and owner.child_count == 0:
                    members.add((_text(owner), _text(member)))
            elif kind in {
                "parameters",
                "lambda_parameters",
                "formal_parameters",
                "parameter_list",
            }:
                bindings.update(self._bound_names(current))
            elif body is not None and current.start_byte >= body.start_byte:
                bindings.update(self._local_names(current))
        return (
            tuple(sorted(names)[:MAX_REFERENCES_PER_SYMBOL]),
            tuple(sorted(members)[:MAX_REFERENCES_PER_SYMBOL]),
            tuple(sorted(bindings)[:MAX_REFERENCES_PER_SYMBOL]),
        )

    def _bound_names(self, parameters: Node) -> set[str]:
        """Names a parameter list binds; type annotations and defaults are not bindings."""
        names: set[str] = set()
        for parameter in parameters.named_children:
            if parameter.type == "identifier":
                names.add(_text(parameter))
                continue
            for field in ("name", "pattern"):
                for child in parameter.children_by_field_name(field):
                    if child.type == "identifier":
                        names.add(_text(child))
            if parameter.type in {
                "typed_parameter",
                "list_splat_pattern",
                "dictionary_splat_pattern",
            }:
                first = parameter.named_children[0] if parameter.named_children else None
                if first is not None and first.type == "identifier":
                    names.add(_text(first))
        return names

    def _local_names(self, node: Node) -> set[str]:
        left: list[Node] = []
        if node.type in {"assignment", "augmented_assignment", "for_statement", "for_in_clause"}:
            left = node.children_by_field_name("left")
        elif node.type in {"named_expression", "variable_declarator", "var_spec", "const_spec"}:
            left = node.children_by_field_name("name")
        elif node.type in {"short_var_declaration", "range_clause"}:
            left = node.children_by_field_name("left")
        elif node.type == "as_pattern":
            left = node.children_by_field_name("alias")
        names: set[str] = set()
        for target in left:
            if target.type == "identifier":
                names.add(_text(target))
            elif target.type in {
                "expression_list",
                "pattern_list",
                "tuple_pattern",
                "as_pattern_target",
            }:
                names.update(
                    _text(item) for item in target.named_children if item.type == "identifier"
                )
        return names

    # --- Python -----------------------------------------------------------------------

    def _python(self, node: Node, enclosing: int | None) -> None:
        definition = node
        if node.type == "decorated_definition":
            inner = node.child_by_field_name("definition")
            if inner is None:
                return
            definition = inner
        name_node = definition.child_by_field_name("name")
        body = definition.child_by_field_name("body")
        if definition.type == "function_definition" and name_node is not None:
            self._add(
                definition,
                kind=SymbolKind.FUNCTION if enclosing is None else SymbolKind.METHOD,
                name=_text(name_node),
                body=body,
                anchor=node,
                docstrings=self._python_docstring(body),
                enclosing=enclosing,
                exported=enclosing is None,
            )
        elif definition.type == "class_definition" and name_node is not None:
            index = self._add(
                definition,
                kind=SymbolKind.CLASS,
                name=_text(name_node),
                body=body,
                anchor=node,
                docstrings=self._python_docstring(body),
                enclosing=enclosing,
                exported=enclosing is None,
            )
            if enclosing is None and body is not None:
                for member in body.named_children:
                    self._python(member, index)
        elif enclosing is None and node.type == "expression_statement":
            assignment = node.named_children[0] if node.named_children else None
            if assignment is not None and assignment.type == "assignment":
                target = assignment.child_by_field_name("left")
                if target is not None and target.type == "identifier":
                    self._add(
                        node,
                        kind=SymbolKind.VARIABLE,
                        name=_text(target),
                        docstrings=(),
                        exported=True,
                    )
        elif enclosing is None and node.type in {"import_statement", "import_from_statement"}:
            self._python_import(node)

    def _python_docstring(self, body: Node | None) -> tuple[LineRange, ...]:
        first = body.named_children[0] if body is not None and body.named_children else None
        if first is None or first.type != "expression_statement" or not first.named_children:
            return ()
        return (_lines(first),) if first.named_children[0].type == "string" else ()

    def _python_import(self, node: Node) -> None:
        line = node.start_point.row + 1
        if node.type == "import_statement":
            for item in node.children_by_field_name("name"):
                dotted = item.child_by_field_name("name") if item.type == "aliased_import" else item
                alias = item.child_by_field_name("alias") if item.type == "aliased_import" else None
                if dotted is None:
                    continue
                module = _text(dotted)
                binding = _text(alias) if alias is not None else module.split(".")[0]
                self._imports.append(
                    AstImport(
                        module,
                        (AstImportName(ImportKind.MODULE, None, binding),),
                        ImportForm.STATIC,
                        line,
                    )
                )
            return
        module_node = node.child_by_field_name("module_name")
        if module_node is None:
            return
        names: list[AstImportName] = []
        if any(child.type == "wildcard_import" for child in node.children):
            names.append(AstImportName(ImportKind.WILDCARD, None, None))
        for item in node.children_by_field_name("name"):
            dotted = item.child_by_field_name("name") if item.type == "aliased_import" else item
            alias = item.child_by_field_name("alias") if item.type == "aliased_import" else None
            if dotted is not None:
                name = _text(dotted)
                names.append(
                    AstImportName(
                        ImportKind.NAMED, name, _text(alias) if alias is not None else name
                    )
                )
        self._imports.append(AstImport(_text(module_node), tuple(names), ImportForm.STATIC, line))

    # --- JavaScript and TypeScript ---------------------------------------------------

    def _javascript(self) -> None:
        for node in self._root.named_children:
            self._check_budget()
            if node.type == "import_statement":
                self._javascript_import(node)
            elif node.type == "export_statement":
                self._javascript_export(node)
            else:
                self._javascript_declaration(node, node, exported=False, default=False)
        for node in _walk(self._root):
            if node.type != "call_expression":
                continue
            function = node.child_by_field_name("function")
            arguments = node.child_by_field_name("arguments")
            argument = (
                arguments.named_children[0]
                if arguments is not None and arguments.named_children
                else None
            )
            if function is None or argument is None or argument.type != "string":
                continue
            if function.type == "import":
                form = ImportForm.DYNAMIC
            elif function.type == "identifier" and _text(function) == "require":
                form = ImportForm.COMMONJS
            else:
                continue
            parent = node.parent
            binding = parent.child_by_field_name("name") if parent is not None else None
            alias = _text(binding) if binding is not None and binding.type == "identifier" else None
            self._imports.append(
                AstImport(
                    _unquote(argument),
                    (AstImportName(ImportKind.NAMESPACE, None, alias),),
                    form,
                    node.start_point.row + 1,
                )
            )

    def _javascript_import(self, node: Node) -> None:
        source = node.child_by_field_name("source")
        line = node.start_point.row + 1
        if source is None:
            # ``import x = require("...")`` is CommonJS interop.
            literal = next((item for item in _walk(node) if item.type == "string"), None)
            if literal is not None:
                self._imports.append(AstImport(_unquote(literal), (), ImportForm.COMMONJS, line))
            return
        names: list[AstImportName] = []
        clause = next(
            (child for child in node.named_children if child.type == "import_clause"), None
        )
        for child in clause.named_children if clause is not None else []:
            if child.type == "identifier":
                names.append(AstImportName(ImportKind.DEFAULT, "default", _text(child)))
            elif child.type == "namespace_import":
                alias = next(
                    (item for item in child.named_children if item.type == "identifier"), None
                )
                names.append(
                    AstImportName(
                        ImportKind.NAMESPACE, None, None if alias is None else _text(alias)
                    )
                )
            elif child.type == "named_imports":
                for specifier in child.named_children:
                    name = specifier.child_by_field_name("name")
                    alias = specifier.child_by_field_name("alias")
                    if name is not None:
                        names.append(
                            AstImportName(
                                ImportKind.NAMED,
                                _text(name),
                                _text(alias if alias is not None else name),
                            )
                        )
        if not names:
            names.append(AstImportName(ImportKind.SIDE_EFFECT, None, None))
        self._imports.append(AstImport(_unquote(source), tuple(names), ImportForm.STATIC, line))

    def _javascript_export(self, node: Node) -> None:
        source = node.child_by_field_name("source")
        if source is not None:
            self._imports.append(
                AstImport(_unquote(source), (), ImportForm.REEXPORT, node.start_point.row + 1)
            )
            return
        default = any(child.type == "default" for child in node.children)
        declaration = node.child_by_field_name("declaration")
        if declaration is not None:
            self._javascript_declaration(declaration, node, exported=True, default=default)
            return
        value = node.child_by_field_name("value")
        if default and value is not None:
            if value.type == "identifier":
                self._pending_exports.append(("default", _text(value)))
            elif value.type in _JS_FUNCTION_VALUES or value.type in _JS_CLASSES:
                self._javascript_declaration(value, node, exported=True, default=True)
            return
        for clause in node.named_children:
            if clause.type != "export_clause":
                continue
            for specifier in clause.named_children:
                name = specifier.child_by_field_name("name")
                alias = specifier.child_by_field_name("alias")
                if name is not None:
                    self._pending_exports.append(
                        (_text(alias if alias is not None else name), _text(name))
                    )

    def _javascript_declaration(
        self, node: Node, anchor: Node, *, exported: bool, default: bool
    ) -> None:
        if node.type == "ambient_declaration" and node.named_children:
            node = node.named_children[0]
        name_node = node.child_by_field_name("name")
        name = None if name_node is None else _text(name_node)

        def export(index: int) -> None:
            if not exported:
                return
            if default:
                self._exports.append(("default", index))
            elif name is not None:
                self._exports.append((name, index))

        if node.type in _JS_FUNCTIONS or (default and node.type in _JS_FUNCTION_VALUES):
            export(
                self._add(
                    node,
                    kind=SymbolKind.FUNCTION,
                    name=name,
                    body=node.child_by_field_name("body"),
                    anchor=anchor,
                    exported=exported,
                )
            )
        elif node.type in _JS_CLASSES:
            body = node.child_by_field_name("body")
            index = self._add(
                node, kind=SymbolKind.CLASS, name=name, body=body, anchor=anchor, exported=exported
            )
            export(index)
            for member in body.named_children if body is not None else []:
                member_name = member.child_by_field_name("name")
                if member.type in _JS_METHODS and member_name is not None:
                    self._add(
                        member,
                        kind=SymbolKind.METHOD,
                        name=_text(member_name),
                        body=member.child_by_field_name("body"),
                        enclosing=index,
                    )
        elif node.type in _JS_TYPE_KINDS:
            export(
                self._add(
                    node,
                    kind=SymbolKind.TYPE,
                    name=name,
                    declaration_kind=_JS_TYPE_KINDS[node.type],
                    body=node.child_by_field_name("body"),
                    anchor=anchor,
                    exported=exported,
                )
            )
        elif node.type in {"lexical_declaration", "variable_declaration"}:
            keyword = node.child_by_field_name("kind")
            declarators = [
                item for item in node.named_children if item.type == "variable_declarator"
            ]
            for declarator in declarators:
                target = declarator.child_by_field_name("name")
                value = declarator.child_by_field_name("value")
                if target is None or target.type != "identifier":
                    continue
                name = _text(target)
                function = value is not None and value.type in _JS_FUNCTION_VALUES
                export(
                    self._add(
                        declarator if len(declarators) > 1 else node,
                        kind=SymbolKind.FUNCTION if function else SymbolKind.VARIABLE,
                        name=name,
                        declaration_kind="binding"
                        if function
                        else (_text(keyword) if keyword is not None else "var"),
                        body=value.child_by_field_name("body") if function and value else None,
                        anchor=anchor if len(declarators) == 1 else declarator,
                        exported=exported,
                    )
                )

    # --- Go ---------------------------------------------------------------------------

    def _go(self) -> None:
        seen_package = False
        for node in self._root.named_children:
            self._check_budget()
            kind = node.type
            if kind == "comment" and not seen_package:
                text = _text(node)
                if text.startswith(("//go:build", "// +build")):
                    self._constrained = True
            elif kind == "package_clause":
                seen_package = True
                identifier = next(
                    (item for item in node.named_children if item.type == "package_identifier"),
                    None,
                )
                self._package = None if identifier is None else _text(identifier)
            elif kind == "import_declaration":
                for spec in _walk(node):
                    if spec.type == "import_spec":
                        self._go_import(spec)
            elif kind == "function_declaration":
                self._go_symbol(node, node, SymbolKind.FUNCTION, None)
            elif kind == "method_declaration":
                receiver = node.child_by_field_name("receiver")
                receiver_type = next(
                    (
                        _text(item)
                        for item in (_walk(receiver) if receiver is not None else [])
                        if item.type == "type_identifier"
                    ),
                    None,
                )
                self._go_symbol(node, node, SymbolKind.METHOD, receiver_type)
            elif kind in {"type_declaration", "const_declaration", "var_declaration"}:
                specs = [
                    item
                    for item in _walk(node)
                    if item.type in {"type_spec", "type_alias", "const_spec", "var_spec"}
                ]
                for spec in specs:
                    anchor = node if len(specs) == 1 else spec
                    if kind == "type_declaration":
                        shape = spec.child_by_field_name("type")
                        declared = {"struct_type": "struct", "interface_type": "interface"}.get(
                            shape.type if shape is not None else "",
                            "alias" if spec.type == "type_alias" else "defined",
                        )
                        self._go_symbol(spec, anchor, SymbolKind.TYPE, declared)
                    else:
                        for name in spec.children_by_field_name("name"):
                            if name.type != "identifier":
                                continue
                            self._go_symbol(
                                spec,
                                anchor,
                                SymbolKind.VARIABLE,
                                "const" if kind == "const_declaration" else "var",
                                name=name,
                            )

    def _go_symbol(
        self,
        node: Node,
        anchor: Node,
        kind: SymbolKind,
        declaration_kind: str | None,
        *,
        name: Node | None = None,
    ) -> None:
        name_node = name or node.child_by_field_name("name")
        if name_node is None:
            return
        text = _text(name_node)
        self._add(
            node,
            kind=kind,
            name=text,
            declaration_kind=declaration_kind,
            body=node.child_by_field_name("body"),
            anchor=anchor,
            exported=text[:1].isupper(),
        )

    def _go_import(self, spec: Node) -> None:
        path = spec.child_by_field_name("path")
        if path is None:
            return
        module = _unquote(path)
        if module == "C":
            self._constrained = True
        name = spec.child_by_field_name("name")
        if name is None:
            imported = AstImportName(ImportKind.MODULE, None, None)
        elif name.type == "blank_identifier":
            imported = AstImportName(ImportKind.SIDE_EFFECT, None, None)
        elif name.type == "dot":
            imported = AstImportName(ImportKind.WILDCARD, None, None)
        else:
            imported = AstImportName(ImportKind.MODULE, None, _text(name))
        self._imports.append(
            AstImport(module, (imported,), ImportForm.STATIC, spec.start_point.row + 1)
        )
