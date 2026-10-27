"""Provider-independent Bitbucket target identity and request authority contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID


class BitbucketTargetInactive(ValueError):
    def __init__(self) -> None:
        super().__init__("bitbucket_target_inactive")


class BitbucketCredentialGenerationChanged(ValueError):
    def __init__(self) -> None:
        super().__init__("bitbucket_credential_generation_changed")


class BitbucketBindingGenerationChanged(BitbucketCredentialGenerationChanged):
    def __init__(self) -> None:
        ValueError.__init__(self, "bitbucket_binding_generation_changed")


class BitbucketCredentialUnavailable(ValueError):
    def __init__(self) -> None:
        super().__init__("bitbucket_credential_unavailable")


@dataclass(frozen=True)
class BoundBitbucketTarget:
    installation_id: UUID
    workspace_id: UUID
    repository_id: UUID
    repository_uuid: UUID
    credential_generation: int
    binding_generation: int = 1

    def __post_init__(self) -> None:
        if (
            type(self.installation_id) is not UUID
            or type(self.workspace_id) is not UUID
            or type(self.repository_id) is not UUID
            or type(self.repository_uuid) is not UUID
            or type(self.credential_generation) is not int
            or not 0 < self.credential_generation <= 9223372036854775807
            or type(self.binding_generation) is not int
            or not 0 < self.binding_generation <= 9223372036854775807
        ):
            raise ValueError("Invalid Bitbucket bound target")


@dataclass(frozen=True)
class BitbucketRequestCredential:
    target: BoundBitbucketTarget
    token: str = field(repr=False)


class BitbucketRequestAuthority(Protocol):
    async def checkpoint(self, target: BoundBitbucketTarget) -> None: ...

    async def credential_for(self, target: BoundBitbucketTarget) -> BitbucketRequestCredential: ...
