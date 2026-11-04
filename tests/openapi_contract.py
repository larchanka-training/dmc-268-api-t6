"""Load ``contracts/openapi.yaml`` and build validators of its schemas; shared by its test files."""

from __future__ import annotations

from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema.protocols import Validator
from openapi_schema_validator import OAS31Validator
from openapi_spec_validator.readers import read_from_filename
from referencing import Registry
from referencing.jsonschema import DRAFT202012

SPEC_PATH = Path(__file__).parents[1] / "contracts" / "openapi.yaml"
APP_PATH = Path(__file__).parents[1] / "app"


@cache
def _load_spec() -> tuple[Mapping[str, Any], str]:
    return read_from_filename(str(SPEC_PATH))


def _spec() -> Mapping[str, Any]:
    return _load_spec()[0]


def _components() -> Mapping[str, Any]:
    schemas: Mapping[str, Any] = _spec()["components"]["schemas"]
    return schemas


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _response_schema_pointer(method: str, path: str, status: str) -> str:
    """Locate the declared JSON body schema, following a response-level ``$ref``."""
    pointer = f"/paths/{_escape(path)}/{method}/responses/{status}"
    reference = _spec()["paths"][path][method]["responses"][status].get("$ref")
    if reference is not None:
        pointer = str(reference).removeprefix("#")
    return f"{pointer}/content/{_escape('application/json')}/schema"


@cache
def _registry() -> Registry[Any]:
    spec, base_uri = _load_spec()
    registry: Registry[Any] = Registry().with_resource(
        base_uri, DRAFT202012.create_resource(dict(spec))
    )
    return registry


def _validator_at(pointer: str) -> Validator:
    """Validate against the schema at ``pointer``, resolving ``$ref`` inside the whole document."""
    validator: Validator = OAS31Validator(
        {"$ref": f"{_load_spec()[1]}#{pointer}"},
        registry=_registry(),
        format_checker=OAS31Validator.FORMAT_CHECKER,
    )
    return validator


def _validator_for(method: str, path: str, status: str) -> Validator:
    return _validator_at(_response_schema_pointer(method, path, status))
