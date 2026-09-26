"""Regression coverage for a linear Alembic migration graph."""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_alembic_migration_graph_has_one_current_head() -> None:
    """Alembic must not expose parallel heads after a migration is added."""
    config = Config(str(REPO_ROOT / "alembic.ini"))

    script_directory = ScriptDirectory.from_config(config)

    heads = script_directory.get_heads()

    assert len(heads) == 1, f"expected exactly one Alembic head, got {heads}"
