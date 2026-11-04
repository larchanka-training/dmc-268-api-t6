"""Bitbucket credential rows keep secret and tenant authority isolated (#333)."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import Connection, create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.bootstrap.bitbucket import get_bitbucket_management
from app.bootstrap.db_metadata import metadata
from app.bootstrap.portal_auth import get_auth_scope
from app.bootstrap.reviews_api import ReviewsApiResources
from app.main import app
from app.modules.auth.application.scope import AuthScope
from app.modules.integrations.bitbucket.application.manage_connection import (
    BitbucketRepositoryProvider,
    ManageBitbucketConnection,
)
from app.modules.integrations.bitbucket.application.request_credentials import (
    BitbucketBindingGenerationChanged,
    BitbucketCredentialGenerationChanged,
    BitbucketTargetInactive,
)
from app.modules.integrations.bitbucket.infrastructure.connection_store import (
    SqlAlchemyBitbucketConnectionStore,
    SqlAlchemyBitbucketManagementUnitOfWork,
)
from app.modules.integrations.bitbucket.infrastructure.credential_cipher import (
    BitbucketCredentialCipher,
    CredentialContext,
    CredentialPurpose,
)
from app.modules.integrations.bitbucket.infrastructure.credential_codec import (
    BitbucketCredentialCodecAdapter,
)
from app.modules.integrations.bitbucket.infrastructure.models import (
    BitbucketRepositoryIdentity,
    BitbucketRepositoryWebhookSecret,
    bitbucket_repository_external_id_seq,
)
from app.modules.integrations.bitbucket.infrastructure.repository_provider import (
    BitbucketCloudRepositoryProvider,
    BitbucketRepositorySnapshot,
)
from app.modules.integrations.bitbucket.infrastructure.request_credentials import (
    BitbucketHttpClient,
    BitbucketRequestCredentials,
    SqlAlchemyBitbucketTargetReader,
)
from app.modules.repositories.application.onboard_repository import load_default_rule_sets
from app.modules.repositories.infrastructure.models import Repository


@pytest.fixture
def bitbucket_connection_schema() -> Iterator[Connection]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL connection tests")
    schema = f"test_bitbucket_connection_{uuid4().hex}"
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(CreateSchema(schema))
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.commit()
            try:
                yield connection
            finally:
                connection.rollback()
                connection.execute(text("SET search_path TO public"))
                connection.execute(DropSchema(schema, cascade=True))
                connection.commit()
    finally:
        engine.dispose()


def _config(connection: Connection) -> Config:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    return config


__all__ = (
    "AsyncSession",
    "AuthScope",
    "BitbucketBindingGenerationChanged",
    "BitbucketCloudRepositoryProvider",
    "BitbucketCredentialCipher",
    "BitbucketCredentialCodecAdapter",
    "BitbucketCredentialGenerationChanged",
    "BitbucketHttpClient",
    "BitbucketRepositoryIdentity",
    "BitbucketRepositoryProvider",
    "BitbucketRepositorySnapshot",
    "BitbucketRepositoryWebhookSecret",
    "BitbucketRequestCredentials",
    "BitbucketTargetInactive",
    "Config",
    "Connection",
    "CreateSchema",
    "CredentialContext",
    "CredentialPurpose",
    "DropSchema",
    "IntegrityError",
    "Iterator",
    "ManageBitbucketConnection",
    "MigrationContext",
    "Path",
    "Repository",
    "ReviewsApiResources",
    "SqlAlchemyBitbucketConnectionStore",
    "SqlAlchemyBitbucketManagementUnitOfWork",
    "SqlAlchemyBitbucketTargetReader",
    "TestClient",
    "UUID",
    "_config",
    "annotations",
    "app",
    "async_sessionmaker",
    "asyncio",
    "bitbucket_connection_schema",
    "bitbucket_repository_external_id_seq",
    "cast",
    "command",
    "compare_metadata",
    "create_async_engine",
    "create_engine",
    "get_auth_scope",
    "get_bitbucket_management",
    "httpx",
    "load_default_rule_sets",
    "make_url",
    "metadata",
    "os",
    "pytest",
    "select",
    "text",
    "uuid4",
)
