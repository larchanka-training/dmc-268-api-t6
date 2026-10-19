"""Installation credentials and current GitLab project ownership reservations."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BIGINT,
    BOOLEAN,
    CHAR,
    TEXT,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    LargeBinary,
    Sequence,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.common.infrastructure.db.base import Base
from app.common.infrastructure.db.columns import timestamp_column

# Allocated explicitly by management, never a default on the shared GitHub parent.
gitlab_installation_external_id_seq = Sequence(
    "gitlab_installation_external_id_seq",
    metadata=Base.metadata,
    start=1,
    minvalue=1,
    maxvalue=9223372036854775807,
    cycle=False,
    data_type=BIGINT,
)


class GitLabConnection(Base):
    __tablename__ = "gitlab_connections"
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
            "provider_installation_id", "instance_origin", name="uq_gitlab_connections_origin"
        ),
        CheckConstraint("provider = 'gitlab'", name="ck_gitlab_connections_provider"),
        CheckConstraint("credential_generation > 0", name="ck_gitlab_connections_generation"),
        CheckConstraint(
            "webhook_secret_digest ~ '^[0-9a-f]{64}$'", name="ck_gitlab_connections_webhook_digest"
        ),
        CheckConstraint(
            (
                "instance_origin ~ "
                "'^https://([a-z0-9][a-z0-9.-]*|\\[[0-9a-f:]+\\])(:[1-9][0-9]{0,4})?$' AND "
                "instance_origin !~ ':[4]43$'"
            ),
            name="ck_gitlab_connections_origin",
        ),
        CheckConstraint(
            (
                "(token_ciphertext IS NOT NULL AND octet_length(token_ciphertext) > 0 AND"
                " credential_key_id IS NOT NULL AND length(trim(credential_key_id)) > 0) "
                "OR (token_ciphertext IS NULL AND credential_key_id IS NULL AND "
                "revoked_at IS NOT NULL)"
            ),
            name="ck_gitlab_connections_credential_pair",
        ),
        CheckConstraint(
            (
                "NOT intake_enabled OR (revoked_at IS NULL AND token_ciphertext IS NOT "
                "NULL AND credential_key_id IS NOT NULL)"
            ),
            name="ck_gitlab_connections_enabled",
        ),
    )

    provider_installation_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False, server_default="gitlab")
    instance_origin: Mapped[str] = mapped_column(TEXT, nullable=False)
    credential_generation: Mapped[int] = mapped_column(
        BIGINT, nullable=False, server_default=text("1")
    )
    token_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    credential_key_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    webhook_secret_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    intake_enabled: Mapped[bool] = mapped_column(
        BOOLEAN, nullable=False, server_default=text("false")
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class GitLabProjectBinding(Base):
    """Current reservations only; deleting one leaves repository/run history intact."""

    __tablename__ = "gitlab_project_bindings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["provider_installation_id", "instance_origin"],
            ["gitlab_connections.provider_installation_id", "gitlab_connections.instance_origin"],
        ),
        ForeignKeyConstraint(
            ["repository_id", "provider_installation_id", "project_external_id"],
            [
                "repositories.id",
                "repositories.provider_installation_id",
                "repositories.external_id",
            ],
        ),
        UniqueConstraint(
            "instance_origin", "project_external_id", name="uq_gitlab_project_bindings_project"
        ),
        CheckConstraint(
            "project_external_id > 0", name="ck_gitlab_project_bindings_project_positive"
        ),
    )

    repository_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    provider_installation_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    instance_origin: Mapped[str] = mapped_column(TEXT, nullable=False)
    project_external_id: Mapped[int] = mapped_column(BIGINT, nullable=False)
