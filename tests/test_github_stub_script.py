"""The GitHub stub of the local recipe answers the PR of the smoke delivery (#70)."""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

from app.modules.integrations.webhooks.api.pull_request_dtos import parse_pull_request_label_event
from app.modules.integrations.webhooks.infrastructure.github_pull_request_dtos import (
    parse_current_pull_request,
)

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(name: str) -> Iterator[ModuleType]:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


@pytest.fixture(scope="module")
def smoke() -> Iterator[ModuleType]:
    yield from _load("webhook_smoke")


@pytest.fixture(scope="module")
def stub() -> Iterator[ModuleType]:
    yield from _load("github_stub")


def test_stub_pull_request_is_the_current_pr_of_the_smoke_delivery(
    smoke: ModuleType, stub: ModuleType
) -> None:
    body, _ = smoke.build_delivery("secret", "delivery-1")
    payload = json.loads(body)
    labeled = parse_pull_request_label_event(payload)

    current = parse_current_pull_request(labeled.pull_request, stub.PULL_REQUEST)

    assert labeled.label_name == "ai-review"
    assert payload["repository"]["full_name"] == stub.REPOSITORY
    assert current.head_sha == payload["pull_request"]["head"]["sha"]
    assert current.current_label_names == frozenset({"ai-review"})
