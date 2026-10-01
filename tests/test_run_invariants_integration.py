"""Run persistence invariants against a disposable migrated PostgreSQL schema."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Connection, Table, create_engine, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.common.infrastructure.db.enums import CodeChangeState, Engine, RunState
from app.modules.repositories.infrastructure.models import (
    ProviderInstallation,
    Repository,
    RuleVersion,
)
from app.modules.reviews.infrastructure.models import CodeChange, PromptVersion, Run
from app.modules.workspaces.infrastructure.models import Workspace

pytestmark = pytest.mark.integration

WORKSPACE_TABLE = cast(Table, Workspace.__table__)
INSTALLATION_TABLE = cast(Table, ProviderInstallation.__table__)
REPOSITORY_TABLE = cast(Table, Repository.__table__)
RULE_VERSION_TABLE = cast(Table, RuleVersion.__table__)
PROMPT_VERSION_TABLE = cast(Table, PromptVersion.__table__)
CODE_CHANGE_TABLE = cast(Table, CodeChange.__table__)
RUN_TABLE = cast(Table, Run.__table__)


@dataclass(frozen=True)
class ParentIds:
    repository: UUID
    code_change: UUID
    rule_version: UUID
    prompt_version: UUID


@contextmanager
def migrated_schema(database_url: str) -> Iterator[Connection]:
    schema = f"test_run_invariants_{uuid4().hex}"
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            created = False
            try:
                connection.execute(CreateSchema(schema))
                created = True
                connection.execute(text(f'SET search_path TO "{schema}"'))
                connection.commit()
                config = Config("alembic.ini")
                config.attributes["connection"] = connection
                command.upgrade(config, "head")
                connection.commit()
                yield connection
            finally:
                connection.rollback()
                if created:
                    connection.execute(text("SET search_path TO public"))
                    connection.execute(DropSchema(schema, cascade=True, if_exists=True))
                    connection.commit()
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def migrated_connection() -> Iterator[Connection]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    with migrated_schema(database_url) as connection:
        yield connection


@pytest.fixture
def seeded_connection(migrated_connection: Connection) -> Iterator[tuple[Connection, ParentIds]]:
    transaction = migrated_connection.begin()
    try:
        yield migrated_connection, seed_chain(migrated_connection)
    finally:
        transaction.rollback()


def test_partial_migration_failure_drops_schema(
    migrated_connection: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url = os.environ["TEST_DATABASE_URL"]
    interrupted_schema: str | None = None

    def interrupt_upgrade(config: Config, revision: str) -> None:
        nonlocal interrupted_schema
        assert revision == "head"
        connection = cast(Connection, config.attributes["connection"])
        interrupted_schema = cast(str, connection.scalar(text("SELECT current_schema()")))
        connection.execute(text("CREATE TABLE partially_migrated (id integer)"))
        connection.commit()
        raise RuntimeError("migration interrupted")

    monkeypatch.setattr(command, "upgrade", interrupt_upgrade)
    with pytest.raises(RuntimeError, match="migration interrupted"), migrated_schema(database_url):
        pass
    assert interrupted_schema is not None
    try:
        still_exists = migrated_connection.scalar(
            text("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = :schema)"),
            {"schema": interrupted_schema},
        )
        assert still_exists is False
    finally:
        migrated_connection.rollback()


def add_code_change(connection: Connection, repository: UUID, number: int) -> UUID:
    code_change = uuid4()
    connection.execute(
        insert(CODE_CHANGE_TABLE).values(
            id=code_change,
            repository_id=repository,
            external_id=number,
            external_number=number,
            title=f"Change {number}",
            source_branch=f"feature/{number}",
            target_branch="main",
            base_sha="b" * 40,
            head_sha="h" * 40,
            state=CodeChangeState.OPEN,
            web_url=f"https://example.test/repo/pull/{number}",
        )
    )
    return code_change


def seed_chain(connection: Connection) -> ParentIds:
    workspace = uuid4()
    installation = uuid4()
    repository = uuid4()
    rule_version = uuid4()
    prompt_version = uuid4()
    connection.execute(
        insert(WORKSPACE_TABLE).values(
            id=workspace, name="run invariants", daily_budget_usd=Decimal("1")
        )
    )
    connection.execute(
        insert(INSTALLATION_TABLE).values(
            id=installation,
            workspace_id=workspace,
            provider="github",
            external_id=1,
            metadata={},
        )
    )
    connection.execute(
        insert(REPOSITORY_TABLE).values(
            id=repository,
            provider_installation_id=installation,
            external_id=1,
            full_name="owner/repo",
            default_branch="main",
            web_url="https://example.test/owner/repo",
        )
    )
    connection.execute(
        insert(PROMPT_VERSION_TABLE).values(
            id=prompt_version,
            key="review.system",
            version=1,
            content="system prompt",
            checksum="p" * 64,
        )
    )
    connection.execute(
        insert(RULE_VERSION_TABLE).values(
            id=rule_version,
            repository_id=repository,
            version=1,
            rules=[],
            checksum="a" * 64,
        )
    )
    code_change = add_code_change(connection, repository, 1)
    return ParentIds(repository, code_change, rule_version, prompt_version)


def add_run(
    connection: Connection,
    parents: ParentIds,
    *,
    key: str,
    state: RunState | None = None,
    code_change: UUID | None = None,
    rule_version: UUID | None = None,
    prompt_version: UUID | None = None,
) -> UUID:
    run_id = uuid4()
    values: dict[str, object] = {
        "id": run_id,
        "code_change_id": code_change or parents.code_change,
        "base_sha": "b" * 40,
        "head_sha": "h" * 40,
        "trigger": "webhook",
        "idempotency_key": key,
        "engine": Engine.FAST,
        "rule_version_id": rule_version or parents.rule_version,
        "prompt_version_id": prompt_version or parents.prompt_version,
        "available_at": datetime.now(UTC),
    }
    if state is not None:
        values["state"] = state
    connection.execute(insert(RUN_TABLE).values(**values))
    return run_id


def assert_rejected(
    connection: Connection,
    action: Callable[[], object],
    sqlstate: str,
    constraint: str | None = None,
) -> None:
    with pytest.raises(IntegrityError) as error, connection.begin_nested():
        action()
    assert getattr(error.value.orig, "sqlstate", None) == sqlstate
    if constraint is not None:
        name = getattr(getattr(error.value.orig, "diag", None), "constraint_name", None)
        assert name == constraint


def test_run_defaults_to_queued_on_insert(
    seeded_connection: tuple[Connection, ParentIds],
) -> None:
    connection, parents = seeded_connection
    run_id = add_run(connection, parents, key="1" * 64)
    state = connection.scalar(select(RUN_TABLE.c.state).where(RUN_TABLE.c.id == run_id))
    assert state == RunState.QUEUED


def test_one_active_run_per_code_change_but_terminal_runs_can_coexist(
    seeded_connection: tuple[Connection, ParentIds],
) -> None:
    connection, parents = seeded_connection
    first = add_run(connection, parents, key="1" * 64)
    assert_rejected(
        connection,
        lambda: add_run(connection, parents, key="2" * 64, state=RunState.RUNNING),
        "23505",
        "uq_runs_one_active_per_code_change",
    )
    connection.execute(
        update(RUN_TABLE).where(RUN_TABLE.c.id == first).values(state=RunState.SUCCEEDED)
    )
    second = add_run(connection, parents, key="2" * 64, state=RunState.RUNNING)
    add_run(connection, parents, key="3" * 64, state=RunState.FAILED)
    assert_rejected(
        connection,
        lambda: add_run(connection, parents, key="4" * 64, state=RunState.PUBLISHING),
        "23505",
        "uq_runs_one_active_per_code_change",
    )
    other_change = add_code_change(connection, parents.repository, 2)
    add_run(connection, parents, key="5" * 64, code_change=other_change)
    connection.execute(
        update(RUN_TABLE).where(RUN_TABLE.c.id == second).values(state=RunState.FAILED)
    )
    add_run(connection, parents, key="6" * 64)


def test_idempotency_key_is_unique_across_changes_and_terminal_states(
    seeded_connection: tuple[Connection, ParentIds],
) -> None:
    connection, parents = seeded_connection
    other_change = add_code_change(connection, parents.repository, 2)
    add_run(connection, parents, key="7" * 64, state=RunState.FAILED)
    assert_rejected(
        connection,
        lambda: add_run(
            connection,
            parents,
            key="7" * 64,
            state=RunState.CANCELLED,
            code_change=other_change,
        ),
        "23505",
    )
    add_run(connection, parents, key="8" * 64, state=RunState.CANCELLED, code_change=other_change)


def test_foreign_key_chain_rejects_orphans(
    seeded_connection: tuple[Connection, ParentIds],
) -> None:
    connection, parents = seeded_connection
    assert_rejected(
        connection,
        lambda: connection.execute(
            insert(INSTALLATION_TABLE).values(
                id=uuid4(), workspace_id=uuid4(), provider="github", external_id=2, metadata={}
            )
        ),
        "23503",
    )
    assert_rejected(
        connection,
        lambda: connection.execute(
            insert(REPOSITORY_TABLE).values(
                id=uuid4(),
                provider_installation_id=uuid4(),
                external_id=2,
                full_name="owner/orphan",
                default_branch="main",
                web_url="https://example.test/owner/orphan",
            )
        ),
        "23503",
    )
    assert_rejected(
        connection,
        lambda: add_code_change(connection, uuid4(), 2),
        "23503",
    )
    assert_rejected(
        connection,
        lambda: add_run(connection, parents, key="9" * 64, code_change=uuid4()),
        "23503",
    )
    assert_rejected(
        connection,
        lambda: add_run(connection, parents, key="a" * 64, rule_version=uuid4()),
        "23503",
    )
    assert_rejected(
        connection,
        lambda: add_run(connection, parents, key="b" * 64, prompt_version=uuid4()),
        "23503",
    )
