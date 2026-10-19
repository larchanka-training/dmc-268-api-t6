"""Every graph index store operation is scoped to one repository (#141 AC4)."""

from __future__ import annotations

import inspect
from typing import get_type_hints
from uuid import UUID

import pytest

from app.modules.code_index.application.index_store import CodeIndexStore, CodeIndexUnitOfWork
from app.modules.code_index.infrastructure.index_store import (
    SqlAlchemyCodeIndexStore,
    SqlAlchemyCodeIndexUnitOfWork,
)

_PORT_METHODS = [
    "get_state",
    "enable",
    "purge",
    "purge_unused",
    "claim_lease",
    "discard_abandoned",
    "replace_file",
    "publish",
    "mark_failed",
    "release",
    "revoke_expired_lease",
    "collect_garbage",
    "get_file",
    "file_symbols",
    "outgoing_edges",
    "incoming_edges",
    "importers_of",
    "iter_files",
    "iter_imports",
    "visible_counts",
]


def _public_methods(cls: type) -> set[str]:
    return {
        name
        for name, member in vars(cls).items()
        if inspect.isfunction(member) and not name.startswith("_")
    }


def test_store_port_has_exactly_the_planned_methods() -> None:
    # The TTL seam `purge_unused` takes repository_id like every other method; the leader
    # enumeration takes none and lives in the separate `CodeIndexMaintenance` port (#143).
    assert _public_methods(CodeIndexStore) == set(_PORT_METHODS)


def test_adapter_has_exactly_the_port_methods() -> None:
    assert _public_methods(SqlAlchemyCodeIndexStore) == set(_PORT_METHODS)


@pytest.mark.parametrize("name", _PORT_METHODS)
def test_every_store_port_method_takes_repository_id_first(name: str) -> None:
    method = getattr(CodeIndexStore, name)
    parameters = list(inspect.signature(method).parameters.values())

    assert [parameter.name for parameter in parameters[:2]] == ["self", "repository_id"]
    assert parameters[1].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert get_type_hints(method)["repository_id"] is UUID


# Every public adapter method, not the port list: an extra one without repository_id fails.
@pytest.mark.parametrize("name", sorted(_public_methods(SqlAlchemyCodeIndexStore)))
def test_every_adapter_method_takes_repository_id_first(name: str) -> None:
    method = getattr(SqlAlchemyCodeIndexStore, name)
    parameters = list(inspect.signature(method).parameters.values())

    assert [parameter.name for parameter in parameters[:2]] == ["self", "repository_id"]
    assert parameters[1].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert get_type_hints(method)["repository_id"] is UUID


def test_adapter_and_unit_of_work_are_built_without_scope() -> None:
    # Isolation is by repository_id on every call, never by an AuthScope or allow_unscoped.
    store = inspect.signature(SqlAlchemyCodeIndexStore.__init__).parameters
    unit_of_work = inspect.signature(SqlAlchemyCodeIndexUnitOfWork.__init__).parameters

    assert list(store) == ["self", "session"]
    assert list(unit_of_work) == ["self", "session_factory"]


def _unit_of_work_satisfies_the_port(uow: SqlAlchemyCodeIndexUnitOfWork) -> CodeIndexUnitOfWork:
    # mypy checks that the adapter and its unit of work structurally match the port.
    return uow
