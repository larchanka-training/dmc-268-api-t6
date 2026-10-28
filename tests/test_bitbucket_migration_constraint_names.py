"""Keep Bitbucket CHECK names in the ORM and Alembic upgrades aligned."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import cast

from sqlalchemy import CheckConstraint, Table

from app.modules.integrations.bitbucket.infrastructure.models import BitbucketConnection
from app.modules.integrations.webhooks.infrastructure.models import WebhookEvent

VERSIONS = Path(__file__).parents[1] / "alembic" / "versions"


def _upgrade_check_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    upgrade = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "upgrade"
    )
    names: set[str] = set()
    for node in ast.walk(upgrade):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        name_node: ast.expr | None = None
        if node.func.attr == "CheckConstraint":
            name_node = next(
                (keyword.value for keyword in node.keywords if keyword.arg == "name"), None
            )
        elif node.func.attr == "create_check_constraint" and node.args:
            name_node = node.args[0]
        if isinstance(name_node, ast.Constant) and isinstance(name_node.value, str):
            names.add(name_node.value)
    return names


def test_bitbucket_check_names_match_orm_models_and_migrations() -> None:
    orm_names = {
        constraint.name
        for table in BitbucketConnection.metadata.tables.values()
        if table.name.startswith("bitbucket_")
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }
    migration_names = set().union(
        *(_upgrade_check_names(path) for path in VERSIONS.glob("20261026_004[4-9]*bitbucket*.py")),
        _upgrade_check_names(VERSIONS / "20261027_0050_bitbucket_status_publications.py"),
    )
    assert orm_names == {name for name in migration_names if name.startswith("ck_bitbucket_")}


def test_bitbucket_webhook_receipt_check_names_match_migration() -> None:
    orm_names = {
        constraint.name
        for constraint in cast(Table, WebhookEvent.__table__).constraints
        if isinstance(constraint, CheckConstraint)
    }
    migration_names = _upgrade_check_names(VERSIONS / "20261026_0047_bitbucket_webhook_receipts.py")
    assert {"ck_webhook_events_provider", "ck_webhook_events_provider_origin"} <= (
        orm_names & migration_names
    )
