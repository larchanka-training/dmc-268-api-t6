"""Immutable Bitbucket UUID identity and portal workspace ownership."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BIGINT,
    BOOLEAN,
    INTEGER,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    Index,
    LargeBinary,
    Sequence,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.common.infrastructure.db.base import Base
from app.common.infrastructure.db.columns import timestamp_column

bitbucket_installation_external_id_seq = Sequence(
    "bitbucket_installation_external_id_seq",
    metadata=Base.metadata,
    start=1,
    minvalue=1,
    maxvalue=9223372036854775807,
    cycle=False,
    data_type=BIGINT,
)
bitbucket_repository_external_id_seq = Sequence(
    "bitbucket_repository_external_id_seq",
    metadata=Base.metadata,
    start=1,
    minvalue=1,
    maxvalue=9223372036854775807,
    cycle=False,
    data_type=BIGINT,
)


class BitbucketInstallationIdentity(Base):
    __tablename__ = "bitbucket_installation_identities"
    __table_args__ = (
        ForeignKeyConstraint(
            ["provider_installation_id", "workspace_id", "provider"],
            [
                "provider_installations.id",
                "provider_installations.workspace_id",
                "provider_installations.provider",
            ],
        ),
        UniqueConstraint(
            "provider_installation_id", "workspace_id", name="uq_bitbucket_installation_owner"
        ),
        UniqueConstraint(
            "workspace_id", "bitbucket_workspace_uuid", name="uq_bitbucket_workspace_identity"
        ),
        CheckConstraint("provider = 'bitbucket'", name="ck_bitbucket_installation_provider"),
    )

    provider_installation_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False, server_default="bitbucket")
    bitbucket_workspace_uuid: Mapped[UUID] = mapped_column(Uuid, nullable=False)


class BitbucketRepositoryIdentity(Base):
    __tablename__ = "bitbucket_repository_identities"
    __table_args__ = (
        ForeignKeyConstraint(
            ["provider_installation_id", "workspace_id"],
            [
                "bitbucket_installation_identities.provider_installation_id",
                "bitbucket_installation_identities.workspace_id",
            ],
        ),
        ForeignKeyConstraint(
            ["repository_id", "provider_installation_id", "repository_external_id"],
            [
                "repositories.id",
                "repositories.provider_installation_id",
                "repositories.external_id",
            ],
        ),
        UniqueConstraint(
            "workspace_id", "repository_uuid", name="uq_bitbucket_workspace_repository_uuid"
        ),
        UniqueConstraint(
            "repository_id",
            "provider_installation_id",
            "workspace_id",
            "repository_uuid",
            name="uq_bitbucket_repository_origin",
        ),
    )

    repository_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    provider_installation_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    workspace_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    repository_external_id: Mapped[int] = mapped_column(BIGINT, nullable=False)
    repository_uuid: Mapped[UUID] = mapped_column(Uuid, nullable=False)


class BitbucketConnection(Base):
    """One shared API credential; intake_enabled records any signed hook since token rotation."""

    __tablename__ = "bitbucket_connections"
    __table_args__ = (
        ForeignKeyConstraint(
            ["provider_installation_id", "workspace_id"],
            [
                "bitbucket_installation_identities.provider_installation_id",
                "bitbucket_installation_identities.workspace_id",
            ],
        ),
        CheckConstraint("credential_generation > 0", name="ck_bitbucket_connections_generation"),
        CheckConstraint(
            "(token_ciphertext IS NOT NULL AND octet_length(token_ciphertext) > 0 AND "
            "token_key_id IS NOT NULL AND length(trim(token_key_id)) > 0) OR "
            "(token_ciphertext IS NULL AND token_key_id IS NULL AND revoked_at IS NOT NULL)",
            name="ck_bitbucket_connections_token_pair",
        ),
        CheckConstraint(
            "NOT intake_enabled OR (revoked_at IS NULL AND token_ciphertext IS NOT NULL)",
            name="ck_bitbucket_connections_enabled",
        ),
    )

    provider_installation_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    credential_generation: Mapped[int] = mapped_column(
        BIGINT, nullable=False, server_default=text("1")
    )
    token_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    token_key_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    intake_enabled: Mapped[bool] = mapped_column(
        BOOLEAN, nullable=False, server_default=text("false")
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class BitbucketRepositoryWebhookSecret(Base):
    """Encrypted per-repository hook secret; revocation is independent of siblings."""

    __tablename__ = "bitbucket_repository_webhook_secrets"
    __table_args__ = (
        ForeignKeyConstraint(["repository_id"], ["bitbucket_repository_identities.repository_id"]),
        CheckConstraint("generation > 0", name="ck_bitbucket_repository_webhook_generation"),
        CheckConstraint("binding_generation > 0", name="ck_bitbucket_binding_generation"),
        CheckConstraint(
            "(secret_ciphertext IS NOT NULL AND octet_length(secret_ciphertext) > 0 "
            "AND secret_key_id IS NOT NULL AND length(trim(secret_key_id)) > 0 "
            "AND revoked_at IS NULL) OR "
            "(secret_ciphertext IS NULL AND secret_key_id IS NULL AND revoked_at IS NULL) OR "
            "(secret_ciphertext IS NULL AND secret_key_id IS NULL AND revoked_at IS NOT NULL)",
            name="ck_bitbucket_repository_webhook_secret_pair",
        ),
        CheckConstraint(
            "confirmed_at IS NULL OR (secret_ciphertext IS NOT NULL AND revoked_at IS NULL)",
            name="ck_bitbucket_repository_webhook_confirmation",
        ),
    )

    repository_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    generation: Mapped[int] = mapped_column(BIGINT, nullable=False, server_default=text("1"))
    binding_generation: Mapped[int] = mapped_column(
        BIGINT, nullable=False, server_default=text("1")
    )
    secret_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    secret_key_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class BitbucketRunOrigin(Base):
    """Immutable repository authority captured when a Bitbucket Run is inserted."""

    __tablename__ = "bitbucket_run_origins"
    __table_args__ = (
        UniqueConstraint("run_order", name="uq_bitbucket_run_origins_order"),
        ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["provider_installation_id", "workspace_id"],
            [
                "bitbucket_installation_identities.provider_installation_id",
                "bitbucket_installation_identities.workspace_id",
            ],
        ),
        ForeignKeyConstraint(
            ["repository_id", "provider_installation_id", "workspace_id", "repository_uuid"],
            [
                "bitbucket_repository_identities.repository_id",
                "bitbucket_repository_identities.provider_installation_id",
                "bitbucket_repository_identities.workspace_id",
                "bitbucket_repository_identities.repository_uuid",
            ],
        ),
        CheckConstraint("binding_generation > 0", name="ck_bitbucket_run_binding_generation"),
        CheckConstraint("credential_generation > 0", name="ck_bitbucket_run_credential_generation"),
        CheckConstraint("pr_number > 0", name="ck_bitbucket_run_pr_number"),
        CheckConstraint("length(head_sha) BETWEEN 40 AND 64", name="ck_bitbucket_run_head_sha"),
    )

    run_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    run_order: Mapped[int] = mapped_column(BIGINT, Identity(always=True), nullable=False)
    workspace_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    provider_installation_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    repository_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    repository_uuid: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    binding_generation: Mapped[int] = mapped_column(BIGINT, nullable=False)
    credential_generation: Mapped[int] = mapped_column(BIGINT, nullable=False)
    pr_number: Mapped[int] = mapped_column(INTEGER, nullable=False)
    head_sha: Mapped[str] = mapped_column(String(64), nullable=False)


class BitbucketPublicationClaim(Base):
    """Short detached lease that serializes external comments for one Run."""

    __tablename__ = "bitbucket_publication_claims"
    __table_args__ = (
        ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        CheckConstraint("generation > 0", name="ck_bitbucket_publication_claim_generation"),
    )

    run_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    generation: Mapped[int] = mapped_column(BIGINT, nullable=False)
    lease_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BitbucketStatusPublication(Base):
    """Durable desired status and per-key external write lease."""

    __tablename__ = "bitbucket_status_publications"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        Index("ix_bitbucket_status_replay", "lease_until", "run_id"),
        CheckConstraint("pr_number > 0", name="ck_bitbucket_status_pr_number"),
        CheckConstraint("generation >= 0", name="ck_bitbucket_status_generation"),
        CheckConstraint("desired_rank BETWEEN 0 AND 3", name="ck_bitbucket_status_rank"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    repository_uuid: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    pr_number: Mapped[int] = mapped_column(INTEGER, primary_key=True)
    head_sha: Mapped[str] = mapped_column(String(40), primary_key=True)
    run_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    run_order: Mapped[int] = mapped_column(BIGINT, nullable=False)
    run_url: Mapped[str] = mapped_column(Text, nullable=False)
    desired_state: Mapped[str] = mapped_column(String(16), nullable=False)
    desired_rank: Mapped[int] = mapped_column(INTEGER, nullable=False)
    applied_run_id: Mapped[UUID | None] = mapped_column(Uuid)
    applied_state: Mapped[str | None] = mapped_column(String(16))
    blocked_generation: Mapped[int | None] = mapped_column(BIGINT)
    last_error: Mapped[str | None] = mapped_column(String(64))
    owner_id: Mapped[UUID | None] = mapped_column(Uuid)
    generation: Mapped[int] = mapped_column(BIGINT, nullable=False, server_default=text("0"))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_check_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
