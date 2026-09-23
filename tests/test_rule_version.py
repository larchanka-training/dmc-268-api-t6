"""Contracts for persistence-ready repository rule versions."""

from __future__ import annotations

from hashlib import sha256
from uuid import uuid4

import pytest

from app.modules.repositories.infrastructure.models import RuleVersion

RULES = [
    {
        "name": "Error Handling Standards",
        "include": ["app/**/*.py"],
        "exclude": ["tests/**"],
        "checks": ["Handle I/O failures."],
    }
]


def test_rule_version_from_rules_uses_canonical_json_checksum() -> None:
    version = RuleVersion.from_rules(repository_id=uuid4(), version=1, rules=RULES)

    assert version.rules == RULES
    assert version.is_active is True
    assert (
        version.checksum
        == sha256(
            b'[{"checks":["Handle I/O failures."],"exclude":["tests/**"],'
            b'"include":["app/**/*.py"],"name":"Error Handling Standards"}]'
        ).hexdigest()
    )


def test_rule_version_from_rules_rejects_non_positive_version() -> None:
    with pytest.raises(ValueError, match="positive"):
        RuleVersion.from_rules(repository_id=uuid4(), version=0, rules=RULES)
