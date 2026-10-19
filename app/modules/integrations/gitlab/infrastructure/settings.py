"""Pure shared operator settings. Never read files, environment globals or PATs.

DATABASE_URL is a postgresql+psycopg DSN. GITLAB_CREDENTIAL_KEYS is a JSON object
mapping key IDs to canonical standard-base64 32-byte keys; GITLAB_ACTIVE_KEY_ID
selects writes. GITLAB_ALLOWED_ORIGINS and optional GITLAB_PRIVATE_ORIGINS are
JSON arrays of distinct canonical HTTPS origins. Private opt-ins must be allowed.
Optional SSL_CERT_FILE selects a PEM CA bundle at HTTP composition, with ordinary
certificate/hostname verification. Recovery operations use DatabaseSettings only.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from app.modules.integrations.gitlab.application.manage_connection import (
    GitLabManagementError,
    GitLabOriginPolicy,
)


class GitLabSettingsError(ValueError):
    def __init__(self, kind: Literal["partial", "invalid"] = "invalid") -> None:
        self.kind = kind
        super().__init__("configuration_invalid")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def strict_json(raw: bytes, *, limit: int) -> Any:
    """Decode bounded UTF-8 JSON, rejecting duplicate keys and nonfinite constants.

    Callers translate ValueError to their boundary's static safe error code.
    """
    if len(raw) > limit:
        raise ValueError
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError from None


def _constant(value: str) -> None:
    raise ValueError


def _required(env: Mapping[str, str], name: str, limit: int) -> str:
    value = env.get(name)
    if not isinstance(value, str) or not value or len(value) > limit:
        raise GitLabSettingsError()
    return value


@dataclass(frozen=True)
class DatabaseSettings:
    database_url: str = field(repr=False)

    @classmethod
    def from_environment(cls, env: Mapping[str, str]) -> DatabaseSettings:
        try:
            value = _required(env, "DATABASE_URL", 8192)
            if any(ord(c) < 33 or ord(c) == 127 for c in value):
                raise ValueError
            url = make_url(value)
            if url.drivername != "postgresql+psycopg" or not url.database:
                raise ValueError
            return cls(value)
        except (ValueError, TypeError, ArgumentError):
            raise GitLabSettingsError() from None


def _origins(env: Mapping[str, str], name: str, *, optional: bool = False) -> frozenset[str]:
    raw = env.get(name, "[]" if optional else "")
    if not isinstance(raw, str) or len(raw) > 16384:
        raise GitLabSettingsError()
    values = strict_json(raw.encode("utf-8"), limit=16384)
    if (
        not isinstance(values, list)
        or not (0 if optional else 1) <= len(values) <= 32
        or any(not isinstance(value, str) for value in values)
        or len(set(values)) != len(values)
    ):
        raise GitLabSettingsError()
    return frozenset(values)


@dataclass(frozen=True)
class GitLabRuntimeSettings:
    database: DatabaseSettings
    active_key_id: str = field(repr=False)
    keys: Mapping[str, bytes] = field(repr=False)
    origin_policy: GitLabOriginPolicy
    ssl_cert_file: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "keys", MappingProxyType(dict(self.keys)))

    @classmethod
    def optional_from_environment(cls, env: Mapping[str, str]) -> GitLabRuntimeSettings | None:
        core = ("GITLAB_CREDENTIAL_KEYS", "GITLAB_ACTIVE_KEY_ID", "GITLAB_ALLOWED_ORIGINS")
        if not any(env.get(name) for name in (*core, "GITLAB_PRIVATE_ORIGINS")):
            return None
        if not all(env.get(name) for name in core):
            raise GitLabSettingsError("partial")
        return cls.from_environment(env)

    @classmethod
    def from_environment(cls, env: Mapping[str, str]) -> GitLabRuntimeSettings:
        try:
            database = DatabaseSettings.from_environment(env)
            active = _required(env, "GITLAB_ACTIVE_KEY_ID", 64)
            raw = _required(env, "GITLAB_CREDENTIAL_KEYS", 16384)
            encoded = strict_json(raw.encode("utf-8"), limit=16384)
            if not isinstance(encoded, dict) or not 1 <= len(encoded) <= 32:
                raise ValueError
            keys: dict[str, bytes] = {}
            for label, value in encoded.items():
                if re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", label
                ) is None or not isinstance(value, str):
                    raise ValueError
                key = base64.b64decode(value, validate=True)
                if len(key) != 32 or base64.b64encode(key).decode("ascii") != value:
                    raise ValueError
                keys[label] = key
            if active not in keys:
                raise ValueError
            allowed = _origins(env, "GITLAB_ALLOWED_ORIGINS")
            private = _origins(env, "GITLAB_PRIVATE_ORIGINS", optional=True)
            if not private <= allowed:
                raise ValueError
            policy = GitLabOriginPolicy(allowed, private)
            for origin in allowed:
                policy.validate(origin)
            ca = env.get("SSL_CERT_FILE")
            if ca is not None and (
                not isinstance(ca, str)
                or not 1 <= len(ca) <= 4096
                or any(ord(c) < 32 or ord(c) == 127 for c in ca)
            ):
                raise ValueError
            return cls(database, active, keys, policy, ca)
        except (ValueError, TypeError, UnicodeError, binascii.Error, GitLabManagementError):
            raise GitLabSettingsError() from None


@dataclass(frozen=True)
class GitLabApiSettings:
    """Admission only: independent of credentials, CA files and worker resources."""

    intake_enabled: bool = False

    @classmethod
    def from_environment(cls, env: Mapping[str, str]) -> GitLabApiSettings:
        value = env.get("GITLAB_WEBHOOK_INTAKE_ENABLED", "").strip().lower()
        if value not in {"", "true", "false"}:
            raise GitLabSettingsError() from None
        return cls(value == "true")
