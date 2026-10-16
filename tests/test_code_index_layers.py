"""Layer boundary of the graph indexer (AC9, `.agents/rules/backend.md` §3; #142).

The `code_index` application layer depends only on its ports: it imports neither the
`integrations` module, nor an infrastructure module, nor a provider client.
"""

from __future__ import annotations

import ast
from pathlib import Path

_APPLICATION = Path(__file__).resolve().parents[1] / "app/modules/code_index/application"
_FORBIDDEN = ("app.modules.integrations", "httpx", "sqlalchemy")


def _imports(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules


def test_the_application_layer_imports_no_provider_adapter_or_infrastructure() -> None:
    sources = sorted(_APPLICATION.glob("*.py"))
    assert {path.name for path in sources} >= {
        "build_index.py",
        "exclusions.py",
        "index_resolution.py",
        "index_store.py",
        "ports.py",
        "symbol_rows.py",
        "vcs.py",
    }
    offending = {
        f"{path.name}: {module}"
        for path in sources
        for module in _imports(path)
        if module.startswith(_FORBIDDEN)
        or ".infrastructure" in f".{module}"
        or "github" in module.casefold()
    }
    assert offending == set()
