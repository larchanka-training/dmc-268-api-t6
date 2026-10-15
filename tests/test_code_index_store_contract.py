"""Every graph index store operation is scoped to one repository (#141 AC4)."""

from __future__ import annotations

import inspect
from typing import get_type_hints
from uuid import UUID

import pytest

from app.modules.code_index.application.index_store import CodeIndexStore

_PORT_METHODS = [
    "get_state",
    "enable",
    "purge",
    "claim_lease",
    "discard_abandoned",
    "replace_file",
    "publish",
    "revoke_expired_lease",
    "collect_garbage",
    "get_file",
    "file_symbols",
    "outgoing_edges",
    "incoming_edges",
    "importers_of",
]


def test_store_port_has_exactly_the_planned_methods() -> None:
    # No leader enumeration or TTL seam: those belong to #143 and take no repository_id.
    assert {
        name
        for name, member in vars(CodeIndexStore).items()
        if inspect.isfunction(member) and not name.startswith("_")
    } == set(_PORT_METHODS)


@pytest.mark.parametrize("name", _PORT_METHODS)
def test_every_store_port_method_takes_repository_id_first(name: str) -> None:
    method = getattr(CodeIndexStore, name)
    parameters = list(inspect.signature(method).parameters.values())

    assert [parameter.name for parameter in parameters[:2]] == ["self", "repository_id"]
    assert parameters[1].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert get_type_hints(method)["repository_id"] is UUID
