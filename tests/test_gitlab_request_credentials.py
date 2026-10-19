"""Public contracts for pinned GitLab request credentials."""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError, replace
from typing import Any
from uuid import UUID

import pytest

from app.modules.integrations.gitlab.application.manage_connection import (
    CredentialEnvelope,
    GitLabManagementError,
)
from app.modules.integrations.gitlab.application.request_credentials import (
    BoundGitLabTarget,
    GitLabCredentialGenerationChanged,
    GitLabCredentialUnavailable,
    GitLabRequestCredential,
    GitLabRequestCredentials,
    GitLabRequestUnavailable,
    GitLabTargetIdentity,
    GitLabTargetInactive,
)

_ORIGIN_ID = UUID("11111111-1111-1111-1111-111111111111")
_WORKSPACE = UUID("22222222-2222-2222-2222-222222222222")
_REPOSITORY = UUID("33333333-3333-3333-3333-333333333333")


def test_target_is_frozen_detached_identity_without_credentials() -> None:
    identity = GitLabTargetIdentity(_ORIGIN_ID, 17)
    target = BoundGitLabTarget(_ORIGIN_ID, _WORKSPACE, _REPOSITORY, "https://gitlab.com", 17, 1)
    assert identity.project_id == 17
    assert target.repository_id == _REPOSITORY
    assert set(vars(target)) == {
        "origin_provider_installation_id",
        "workspace_id",
        "repository_id",
        "instance_origin",
        "project_id",
        "credential_generation",
    }
    with pytest.raises(FrozenInstanceError):
        attribute = "project_id"
        setattr(target, attribute, 18)


_TARGET = BoundGitLabTarget(_ORIGIN_ID, _WORKSPACE, _REPOSITORY, "https://gitlab.com", 17, 1)


class Reader:
    def __init__(self) -> None:
        self.current = _TARGET
        self.read_open = False
        self.envelope_reads = 0
        self.checkpoints = 0

    async def resolve_target(self, identity: GitLabTargetIdentity) -> BoundGitLabTarget:
        if identity != GitLabTargetIdentity(_ORIGIN_ID, 17):
            raise GitLabTargetInactive()
        return self.current

    async def checkpoint(self, target: BoundGitLabTarget) -> None:
        self.checkpoints += 1
        if self.current != target:
            raise GitLabCredentialGenerationChanged()

    async def credential_envelope_for(self, target: BoundGitLabTarget) -> CredentialEnvelope:
        await self.checkpoint(target)
        self.envelope_reads += 1
        self.read_open = True
        envelope = CredentialEnvelope("secret-key-label", b"secret-envelope")
        self.read_open = False
        return envelope


class Codec:
    def __init__(self, reader: Reader) -> None:
        self.reader = reader
        self.rotate = False
        self.failure: Exception | None = None

    def decrypt(
        self,
        credential: CredentialEnvelope,
        *,
        installation_id: UUID,
        workspace_id: UUID,
        instance_origin: str,
    ) -> str:
        assert not self.reader.read_open
        assert installation_id == _ORIGIN_ID
        assert workspace_id == _WORKSPACE
        assert instance_origin == "https://gitlab.com"
        assert credential == CredentialEnvelope("secret-key-label", b"secret-envelope")
        if self.failure:
            raise self.failure
        if self.rotate:
            self.reader.current = replace(_TARGET, credential_generation=2)
        return "synthetic-PAT-canary"

    def encrypt(
        self,
        token: str,
        *,
        installation_id: UUID,
        workspace_id: UUID,
        instance_origin: str,
    ) -> CredentialEnvelope:
        raise AssertionError("request resolution cannot encrypt")

    def webhook_digest(self, secret: str) -> str:
        raise AssertionError("request resolution cannot digest")


def test_rotation_during_decrypt_never_returns_a_stale_credential() -> None:
    reader = Reader()
    codec = Codec(reader)
    codec.rotate = True
    with pytest.raises(GitLabCredentialGenerationChanged):
        asyncio.run(GitLabRequestCredentials(reader, codec).credential_for(_TARGET))


def test_credential_resolution_rechecks_after_decrypt_and_every_later_checkpoint() -> None:
    async def exercise() -> None:
        reader = Reader()
        authority = GitLabRequestCredentials(reader, Codec(reader))
        assert await authority.resolve_target(GitLabTargetIdentity(_ORIGIN_ID, 17)) == _TARGET
        assert reader.envelope_reads == 0
        value = await authority.credential_for(_TARGET)
        assert value.target == _TARGET
        assert value.token == "synthetic-PAT-canary"
        assert reader.checkpoints == 2
        assert "synthetic-PAT-canary" not in repr(value)
        reader.current = replace(_TARGET, credential_generation=2)
        with pytest.raises(GitLabCredentialGenerationChanged):
            await authority.checkpoint(value.target)
        with pytest.raises(GitLabCredentialGenerationChanged):
            await authority.credential_for(value.target)
        assert reader.envelope_reads == 1

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "failure, expected",
    [
        (GitLabManagementError("credential_operation_failed"), GitLabCredentialUnavailable),
        (GitLabManagementError("unexpected-code"), GitLabRequestUnavailable),
        (RuntimeError("synthetic-PAT-canary secret-envelope DSN-canary"), GitLabRequestUnavailable),
    ],
)
def test_only_known_codec_refusal_is_credential_failure(
    failure: Exception,
    expected: type[Exception],
    caplog: pytest.LogCaptureFixture,
) -> None:
    reader = Reader()
    codec = Codec(reader)
    codec.failure = failure
    with pytest.raises(expected) as caught:
        asyncio.run(GitLabRequestCredentials(reader, codec).credential_for(_TARGET))
    output = str(caught.value) + repr(caught.value) + caplog.text
    for secret in ("synthetic-PAT-canary", "secret-envelope", "DSN-canary", "unexpected-code"):
        assert secret not in output


@pytest.mark.parametrize("value", [True, False, 0, -1, 9223372036854775808, "17", 17.0, None])
def test_project_identity_requires_exact_positive_bigint(value: Any) -> None:
    with pytest.raises(ValueError, match="Invalid GitLab target identity"):
        GitLabTargetIdentity(_ORIGIN_ID, value)


@pytest.mark.parametrize("value", [str(_ORIGIN_ID), None, 17])
def test_origin_identity_requires_uuid(value: Any) -> None:
    with pytest.raises(ValueError):
        GitLabTargetIdentity(value, 17)


@pytest.mark.parametrize(
    "field, value",
    [
        ("workspace_id", str(_WORKSPACE)),
        ("repository_id", None),
        ("credential_generation", True),
        ("credential_generation", 0),
        ("credential_generation", 9223372036854775808),
        ("instance_origin", "http://gitlab.com"),
        ("instance_origin", "https://gitlab.com/"),
        ("instance_origin", "https://user:secret@gitlab.com"),
        ("instance_origin", "https://GitLab.com"),
    ],
)
def test_bound_target_rejects_invalid_metadata(field: str, value: Any) -> None:
    with pytest.raises(ValueError):
        replace(_TARGET, **{field: value})


@pytest.mark.parametrize("token", ["", "has space", "bad\nheader", "å", "x" * 4097])
def test_request_credential_rejects_invalid_tokens_safely(token: str) -> None:
    with pytest.raises(GitLabCredentialUnavailable, match="^gitlab_credential_unavailable$"):
        GitLabRequestCredential(_TARGET, token)
