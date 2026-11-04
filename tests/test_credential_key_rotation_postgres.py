"""Operator key retirement survives interruption and concurrent credential changes."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.credential_key_rotation import KINDS, KeyRotation, Kind
from app.modules.integrations.bitbucket.infrastructure.credential_cipher import (
    BitbucketCredentialCipher,
    CredentialPurpose,
)
from app.modules.integrations.bitbucket.infrastructure.credential_cipher import (
    CredentialContext as BitbucketContext,
)
from app.modules.integrations.bitbucket.infrastructure.credential_cipher import (
    EncryptedCredential as BitbucketEncrypted,
)
from app.modules.integrations.gitlab.infrastructure.credential_cipher import (
    CredentialCipher,
)
from app.modules.integrations.gitlab.infrastructure.credential_cipher import (
    CredentialContext as GitLabContext,
)
from app.modules.integrations.gitlab.infrastructure.credential_cipher import (
    EncryptedCredential as GitLabEncrypted,
)

pytestmark = pytest.mark.integration
_OLD = b"o" * 32
_NEW = b"n" * 32


def test_rotation_inventory_covers_all_stored_ciphertext_columns(
    rotation_database: tuple[str, Connection, UUID, UUID, UUID, UUID],
) -> None:
    _, db, _, _, _, _ = rotation_database
    columns = db.execute(
        text(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND column_name LIKE '%ciphertext%'"
        )
    ).all()
    assert set(columns) == {
        ("bitbucket_connections", "token_ciphertext"),
        ("bitbucket_repository_webhook_secrets", "secret_ciphertext"),
        ("gitlab_connections", "token_ciphertext"),
    }


@pytest.fixture
def rotation_database() -> Iterator[tuple[str, Connection, UUID, UUID, UUID, UUID]]:
    url = os.environ.get("TEST_DATABASE_URL")
    if url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"test_key_rotation_{uuid4().hex}"
    engine = create_engine(url, hide_parameters=True)
    try:
        with engine.connect() as db:
            db.execute(CreateSchema(schema))
            db.execute(text(f'SET search_path TO "{schema}"'))
            db.commit()
            config = Config("alembic.ini")
            config.attributes["connection"] = db
            command.upgrade(config, "head")
            workspace, bb_installation, gl_installation, repository = (uuid4() for _ in range(4))
            db.execute(
                text("INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, 'test', 7)"),
                {"id": workspace},
            )
            for provider, installation, external in (
                ("bitbucket", bb_installation, 1),
                ("gitlab", gl_installation, 2),
            ):
                db.execute(
                    text(
                        "INSERT INTO provider_installations (id, workspace_id, provider, "
                        "external_id, metadata) "
                        "VALUES (:id, :workspace, :provider, :external, '{}')"
                    ),
                    {
                        "id": installation,
                        "workspace": workspace,
                        "provider": provider,
                        "external": external,
                    },
                )
            db.execute(
                text(
                    "INSERT INTO bitbucket_installation_identities "
                    "(provider_installation_id, workspace_id, bitbucket_workspace_uuid) "
                    "VALUES (:id, :workspace, :external)"
                ),
                {"id": bb_installation, "workspace": workspace, "external": uuid4()},
            )
            db.execute(
                text(
                    "INSERT INTO repositories (id, provider_installation_id, external_id, "
                    "full_name, default_branch, web_url) VALUES "
                    "(:id, :installation, 3, 'test/repo', 'main', 'https://example.test/repo')"
                ),
                {"id": repository, "installation": bb_installation},
            )
            db.execute(
                text(
                    "INSERT INTO bitbucket_repository_identities "
                    "(repository_id, provider_installation_id, workspace_id, "
                    "repository_external_id, repository_uuid) "
                    "VALUES (:repository, :installation, :workspace, 3, :external)"
                ),
                {
                    "repository": repository,
                    "installation": bb_installation,
                    "workspace": workspace,
                    "external": uuid4(),
                },
            )
            bb_old = BitbucketCredentialCipher(active_key_id="old", keys={"old": _OLD})
            gl_old = CredentialCipher(active_key_id="old", keys={"old": _OLD})
            token = bb_old.encrypt(
                "test-token",
                context=BitbucketContext(bb_installation, workspace),
                purpose=CredentialPurpose.TOKEN,
            )
            secret = bb_old.encrypt(
                "s" * 40,
                context=BitbucketContext(bb_installation, workspace, repository),
                purpose=CredentialPurpose.WEBHOOK_SECRET,
            )
            gl_token = gl_old.encrypt(
                "gitlab-token",
                context=GitLabContext(gl_installation, workspace, "https://gitlab.com"),
            )
            db.execute(
                text(
                    "INSERT INTO bitbucket_connections (provider_installation_id, workspace_id, "
                    "token_ciphertext, token_key_id) VALUES (:id, :workspace, :value, 'old')"
                ),
                {"id": bb_installation, "workspace": workspace, "value": token.ciphertext},
            )
            db.execute(
                text(
                    "INSERT INTO bitbucket_repository_webhook_secrets "
                    "(repository_id, secret_ciphertext, secret_key_id) "
                    "VALUES (:id, :value, 'old')"
                ),
                {"id": repository, "value": secret.ciphertext},
            )
            db.execute(
                text(
                    "INSERT INTO gitlab_connections (provider_installation_id, workspace_id, "
                    "instance_origin, token_ciphertext, credential_key_id, webhook_secret_digest) "
                    "VALUES (:id, :workspace, 'https://gitlab.com', :value, 'old', :digest)"
                ),
                {
                    "id": gl_installation,
                    "workspace": workspace,
                    "value": gl_token.token_ciphertext,
                    "digest": "a" * 64,
                },
            )
            db.commit()
            yield url, db, workspace, bb_installation, gl_installation, repository
            db.rollback()
            db.execute(text("SET search_path TO public"))
            db.execute(DropSchema(schema, cascade=True))
            db.commit()
    finally:
        engine.dispose()


def test_bounded_resume_verify_and_rollback(
    rotation_database: tuple[str, Connection, UUID, UUID, UUID, UUID],
) -> None:
    url, db, workspace, bb_installation, gl_installation, repository = rotation_database
    schema = db.scalar(text("SELECT current_schema()"))
    assert isinstance(schema, str)

    async def exercise() -> None:
        engine = create_async_engine(url, connect_args={"options": f"-csearch_path={schema}"})
        try:
            bb = BitbucketCredentialCipher(active_key_id="new", keys={"old": _OLD, "new": _NEW})
            gl = CredentialCipher(active_key_id="new", keys={"old": _OLD, "new": _NEW})
            operator = KeyRotation(
                engine, bitbucket=bb, gitlab=gl, bitbucket_active="new", gitlab_active="new"
            )
            assert await operator.counts() == {
                "bitbucket_tokens": {"old": 1},
                "bitbucket_webhook_secrets": {"old": 1},
                "gitlab_tokens": {"old": 1},
            }
            assert not await operator.verify()
            assert await operator.rotate_batch("bitbucket_tokens", batch_size=1) == 1
            # A stopped operator resumes from remaining old-key rows, without a cursor file.
            resumed = KeyRotation(
                engine, bitbucket=bb, gitlab=gl, bitbucket_active="new", gitlab_active="new"
            )
            assert await resumed.rotate_batch("bitbucket_tokens", batch_size=1) == 0
            assert await resumed.rotate_batch("bitbucket_webhook_secrets", batch_size=1) == 1
            assert await resumed.rotate_batch("gitlab_tokens", batch_size=1) == 1
            assert await resumed.verify()
            assert await resumed.counts() == {
                "bitbucket_tokens": {"new": 1},
                "bitbucket_webhook_secrets": {"new": 1},
                "gitlab_tokens": {"new": 1},
            }
            async with engine.connect() as read:
                bb_row = (
                    await read.execute(
                        text("SELECT token_key_id, token_ciphertext FROM bitbucket_connections")
                    )
                ).one()
                secret_row = (
                    await read.execute(
                        text(
                            "SELECT secret_key_id, secret_ciphertext "
                            "FROM bitbucket_repository_webhook_secrets"
                        )
                    )
                ).one()
                gl_row = (
                    await read.execute(
                        text("SELECT credential_key_id, token_ciphertext FROM gitlab_connections")
                    )
                ).one()
            assert (
                bb.decrypt(
                    BitbucketEncrypted(*bb_row),
                    context=BitbucketContext(bb_installation, workspace),
                    purpose=CredentialPurpose.TOKEN,
                )
                == "test-token"
            )
            assert (
                bb.decrypt(
                    BitbucketEncrypted(*secret_row),
                    context=BitbucketContext(bb_installation, workspace, repository),
                    purpose=CredentialPurpose.WEBHOOK_SECRET,
                )
                == "s" * 40
            )
            assert (
                gl.decrypt(
                    GitLabEncrypted(*gl_row),
                    context=GitLabContext(gl_installation, workspace, "https://gitlab.com"),
                )
                == "gitlab-token"
            )
            # Rollback is possible while both keys remain available: reverse the active ID.
            reverse = KeyRotation(
                engine,
                bitbucket=BitbucketCredentialCipher(
                    active_key_id="old", keys={"old": _OLD, "new": _NEW}
                ),
                gitlab=CredentialCipher(active_key_id="old", keys={"old": _OLD, "new": _NEW}),
                bitbucket_active="old",
                gitlab_active="old",
            )
            for kind in ("bitbucket_tokens", "bitbucket_webhook_secrets", "gitlab_tokens"):
                assert await reverse.rotate_batch(kind, batch_size=1) == 1
            assert await reverse.verify()
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_concurrent_revocation_cannot_be_overwritten(
    rotation_database: tuple[str, Connection, UUID, UUID, UUID, UUID],
) -> None:
    url, db, workspace, bb_installation, gl_installation, _ = rotation_database
    schema = db.scalar(text("SELECT current_schema()"))
    assert isinstance(schema, str)
    db.rollback()

    class RevokingCipher(BitbucketCredentialCipher):
        def encrypt(
            self, value: str, *, context: BitbucketContext, purpose: CredentialPurpose
        ) -> BitbucketEncrypted:
            db.execute(
                text(
                    "UPDATE bitbucket_connections SET token_ciphertext = NULL, "
                    "token_key_id = NULL, credential_generation = credential_generation + 1, "
                    "revoked_at = now(), intake_enabled = false "
                    "WHERE provider_installation_id = :id"
                ),
                {"id": bb_installation},
            )
            db.commit()
            return super().encrypt(value, context=context, purpose=purpose)

    class RotatingCipher(CredentialCipher):
        def encrypt(self, token: str, *, context: GitLabContext) -> GitLabEncrypted:
            credential = super().encrypt(token, context=context)
            db.execute(
                text(
                    "UPDATE gitlab_connections SET token_ciphertext = :ciphertext, "
                    "credential_key_id = 'new', "
                    "credential_generation = credential_generation + 1 "
                    "WHERE provider_installation_id = :id AND workspace_id = :workspace"
                ),
                {
                    "ciphertext": credential.token_ciphertext,
                    "id": gl_installation,
                    "workspace": workspace,
                },
            )
            db.commit()
            return credential

    async def exercise() -> None:
        engine = create_async_engine(url, connect_args={"options": f"-csearch_path={schema}"})
        try:
            operator = KeyRotation(
                engine,
                bitbucket=RevokingCipher(active_key_id="new", keys={"old": _OLD, "new": _NEW}),
                gitlab=RotatingCipher(active_key_id="new", keys={"old": _OLD, "new": _NEW}),
                bitbucket_active="new",
                gitlab_active="new",
            )
            assert await operator.rotate_batch("bitbucket_tokens", batch_size=1) == 0
            assert await operator.rotate_batch("gitlab_tokens", batch_size=1) == 0
            assert await operator.counts() == {
                "bitbucket_tokens": {},
                "bitbucket_webhook_secrets": {"old": 1},
                "gitlab_tokens": {"new": 1},
            }
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("kind", "purpose", "expected"),
    [
        ("bitbucket_tokens", CredentialPurpose.TOKEN, "replacement-token"),
        ("bitbucket_webhook_secrets", CredentialPurpose.WEBHOOK_SECRET, "r" * 40),
    ],
)
def test_concurrent_bitbucket_credential_change_wins_over_operator(
    rotation_database: tuple[str, Connection, UUID, UUID, UUID, UUID],
    kind: Kind,
    purpose: CredentialPurpose,
    expected: str,
) -> None:
    url, db, workspace, bb_installation, _, repository = rotation_database
    schema = db.scalar(text("SELECT current_schema()"))
    assert isinstance(schema, str)
    db.rollback()

    class RotatingCipher(BitbucketCredentialCipher):
        def encrypt(
            self, value: str, *, context: BitbucketContext, purpose: CredentialPurpose
        ) -> BitbucketEncrypted:
            if purpose == target_purpose:
                replacement = super().encrypt(expected, context=context, purpose=purpose)
                if purpose is CredentialPurpose.TOKEN:
                    db.execute(
                        text(
                            "UPDATE bitbucket_connections SET token_ciphertext = :ciphertext, "
                            "token_key_id = 'new', "
                            "credential_generation = credential_generation + 1 "
                            "WHERE provider_installation_id = :id"
                        ),
                        {"id": bb_installation, "ciphertext": replacement.ciphertext},
                    )
                else:
                    db.execute(
                        text(
                            "UPDATE bitbucket_repository_webhook_secrets "
                            "SET secret_ciphertext = :ciphertext, secret_key_id = 'new', "
                            "generation = generation + 1 WHERE repository_id = :id"
                        ),
                        {"id": repository, "ciphertext": replacement.ciphertext},
                    )
                db.commit()
            return super().encrypt(value, context=context, purpose=purpose)

    target_purpose = purpose

    async def exercise() -> None:
        engine = create_async_engine(url, connect_args={"options": f"-csearch_path={schema}"})
        try:
            cipher = RotatingCipher(active_key_id="new", keys={"old": _OLD, "new": _NEW})
            operator = KeyRotation(
                engine,
                bitbucket=cipher,
                gitlab=CredentialCipher(active_key_id="new", keys={"old": _OLD, "new": _NEW}),
                bitbucket_active="new",
                gitlab_active="new",
            )
            assert await operator.rotate_batch(kind, batch_size=1) == 0
            if purpose is CredentialPurpose.TOKEN:
                row = db.execute(
                    text(
                        "SELECT token_key_id, token_ciphertext FROM bitbucket_connections "
                        "WHERE provider_installation_id = :id"
                    ),
                    {"id": bb_installation},
                ).one()
            else:
                row = db.execute(
                    text(
                        "SELECT secret_key_id, secret_ciphertext "
                        "FROM bitbucket_repository_webhook_secrets WHERE repository_id = :id"
                    ),
                    {"id": repository},
                ).one()
            assert (
                cipher.decrypt(
                    BitbucketEncrypted(*row),
                    context=BitbucketContext(
                        bb_installation,
                        workspace,
                        repository if purpose is CredentialPurpose.WEBHOOK_SECRET else None,
                    ),
                    purpose=purpose,
                )
                == expected
            )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_batch_size_limits_each_credential_kind_and_resume_advances(
    rotation_database: tuple[str, Connection, UUID, UUID, UUID, UUID],
) -> None:
    url, db, workspace, _, _, _ = rotation_database
    schema = db.scalar(text("SELECT current_schema()"))
    assert isinstance(schema, str)
    bb_old = BitbucketCredentialCipher(active_key_id="old", keys={"old": _OLD})
    gl_old = CredentialCipher(active_key_id="old", keys={"old": _OLD})
    for index in (1, 2):
        bb_installation, gl_installation, repository = (uuid4() for _ in range(3))
        for provider, installation, external in (
            ("bitbucket", bb_installation, 100 + index),
            ("gitlab", gl_installation, 200 + index),
        ):
            db.execute(
                text(
                    "INSERT INTO provider_installations "
                    "(id, workspace_id, provider, external_id, metadata) "
                    "VALUES (:id, :workspace, :provider, :external, '{}')"
                ),
                {
                    "id": installation,
                    "workspace": workspace,
                    "provider": provider,
                    "external": external,
                },
            )
        db.execute(
            text(
                "INSERT INTO bitbucket_installation_identities "
                "(provider_installation_id, workspace_id, bitbucket_workspace_uuid) "
                "VALUES (:id, :workspace, :external)"
            ),
            {"id": bb_installation, "workspace": workspace, "external": uuid4()},
        )
        name = f"test/repo-{index}"
        db.execute(
            text(
                "INSERT INTO repositories "
                "(id, provider_installation_id, external_id, full_name, default_branch, web_url) "
                "VALUES (:id, :installation, :external, :name, 'main', :url)"
            ),
            {
                "id": repository,
                "installation": bb_installation,
                "external": 300 + index,
                "name": name,
                "url": f"https://example.test/{name}",
            },
        )
        db.execute(
            text(
                "INSERT INTO bitbucket_repository_identities "
                "(repository_id, provider_installation_id, workspace_id, "
                "repository_external_id, repository_uuid) "
                "VALUES (:repository, :installation, :workspace, :external_id, :external_uuid)"
            ),
            {
                "repository": repository,
                "installation": bb_installation,
                "workspace": workspace,
                "external_id": 300 + index,
                "external_uuid": uuid4(),
            },
        )
        token = bb_old.encrypt(
            f"test-token-{index}",
            context=BitbucketContext(bb_installation, workspace),
            purpose=CredentialPurpose.TOKEN,
        )
        secret = bb_old.encrypt(
            "s" * 40,
            context=BitbucketContext(bb_installation, workspace, repository),
            purpose=CredentialPurpose.WEBHOOK_SECRET,
        )
        gl_token = gl_old.encrypt(
            f"gitlab-token-{index}",
            context=GitLabContext(gl_installation, workspace, "https://gitlab.com"),
        )
        db.execute(
            text(
                "INSERT INTO bitbucket_connections "
                "(provider_installation_id, workspace_id, token_ciphertext, token_key_id) "
                "VALUES (:id, :workspace, :value, 'old')"
            ),
            {"id": bb_installation, "workspace": workspace, "value": token.ciphertext},
        )
        db.execute(
            text(
                "INSERT INTO bitbucket_repository_webhook_secrets "
                "(repository_id, secret_ciphertext, secret_key_id) "
                "VALUES (:id, :value, 'old')"
            ),
            {"id": repository, "value": secret.ciphertext},
        )
        db.execute(
            text(
                "INSERT INTO gitlab_connections "
                "(provider_installation_id, workspace_id, instance_origin, token_ciphertext, "
                "credential_key_id, webhook_secret_digest) "
                "VALUES (:id, :workspace, 'https://gitlab.com', :value, 'old', :digest)"
            ),
            {
                "id": gl_installation,
                "workspace": workspace,
                "value": gl_token.token_ciphertext,
                "digest": "a" * 64,
            },
        )
    db.commit()

    async def exercise() -> None:
        engine = create_async_engine(url, connect_args={"options": f"-csearch_path={schema}"})
        try:
            operator = KeyRotation(
                engine,
                bitbucket=BitbucketCredentialCipher(
                    active_key_id="new", keys={"old": _OLD, "new": _NEW}
                ),
                gitlab=CredentialCipher(active_key_id="new", keys={"old": _OLD, "new": _NEW}),
                bitbucket_active="new",
                gitlab_active="new",
            )
            assert await operator.counts() == {kind: {"old": 3} for kind in KINDS}
            for kind in KINDS:
                assert await operator.rotate_batch(kind, batch_size=2) == 2
            assert await operator.counts() == {kind: {"old": 1, "new": 2} for kind in KINDS}
            for kind in KINDS:
                assert await operator.rotate_batch(kind, batch_size=2) == 1
            assert await operator.verify()
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_cli_reencrypts_retained_revoked_gitlab_token_and_reports_counts_only(
    rotation_database: tuple[str, Connection, UUID, UUID, UUID, UUID],
) -> None:
    url, db, _, _, gl_installation, _ = rotation_database
    schema = db.scalar(text("SELECT current_schema()"))
    assert isinstance(schema, str)
    db.execute(
        text(
            "UPDATE gitlab_connections SET revoked_at = now(), intake_enabled = false "
            "WHERE provider_installation_id = :id"
        ),
        {"id": gl_installation},
    )
    db.commit()
    database_url = (
        make_url(url)
        .update_query_dict({"options": f"-csearch_path={schema}"})
        .render_as_string(hide_password=False)
    )
    env = {
        **os.environ,
        "DATABASE_URL": database_url,
        "BITBUCKET_CREDENTIAL_KEYS": json.dumps(
            {
                "old": base64.b64encode(_OLD).decode(),
                "new": base64.b64encode(_NEW).decode(),
            }
        ),
        "BITBUCKET_ACTIVE_KEY_ID": "new",
        "GITLAB_CREDENTIAL_KEYS": json.dumps(
            {
                "old": base64.b64encode(_OLD).decode(),
                "new": base64.b64encode(_NEW).decode(),
            }
        ),
        "GITLAB_ACTIVE_KEY_ID": "new",
    }

    def invoke(command: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "app.credential_key_rotation",
                command,
                "--batch-size",
                "1",
                "--max-batches",
                "1",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    old_counts = {
        "counts": {
            "bitbucket_tokens": {"old": 1},
            "bitbucket_webhook_secrets": {"old": 1},
            "gitlab_tokens": {"old": 1},
        }
    }
    dry = invoke("dry-run")
    assert (dry.returncode, dry.stdout, dry.stderr) == (
        0,
        json.dumps(old_counts, separators=(",", ":")) + "\n",
        "",
    )
    incomplete = invoke("verify")
    assert (incomplete.returncode, incomplete.stdout, incomplete.stderr) == (
        1,
        dry.stdout,
        "",
    )
    run = invoke("run")
    assert run.returncode == 0
    assert run.stderr == ""
    assert json.loads(run.stdout) == {
        "updated": {
            "bitbucket_tokens": 1,
            "bitbucket_webhook_secrets": 1,
            "gitlab_tokens": 1,
        },
        "counts": {
            "bitbucket_tokens": {"new": 1},
            "bitbucket_webhook_secrets": {"new": 1},
            "gitlab_tokens": {"new": 1},
        },
        "ready": True,
    }
    complete = invoke("verify")
    assert (complete.returncode, complete.stdout, complete.stderr) == (
        0,
        json.dumps(
            {
                "counts": {
                    "bitbucket_tokens": {"new": 1},
                    "bitbucket_webhook_secrets": {"new": 1},
                    "gitlab_tokens": {"new": 1},
                }
            },
            separators=(",", ":"),
        )
        + "\n",
        "",
    )
    assert "test-token" not in dry.stdout + incomplete.stdout + run.stdout + complete.stdout
    assert "gitlab-token" not in dry.stdout + incomplete.stdout + run.stdout + complete.stdout
    assert "BBC1" not in dry.stdout + incomplete.stdout + run.stdout + complete.stdout
    assert "GLC1" not in dry.stdout + incomplete.stdout + run.stdout + complete.stdout
    assert db.scalar(text("SELECT revoked_at IS NOT NULL FROM gitlab_connections")) is True


def test_dry_run_identifies_missing_old_key_without_reading_ciphertext(
    rotation_database: tuple[str, Connection, UUID, UUID, UUID, UUID],
) -> None:
    url, db, _, _, _, _ = rotation_database
    schema = db.scalar(text("SELECT current_schema()"))
    assert isinstance(schema, str)
    database_url = (
        make_url(url)
        .update_query_dict({"options": f"-csearch_path={schema}"})
        .render_as_string(hide_password=False)
    )
    env = {
        **os.environ,
        "DATABASE_URL": database_url,
        "BITBUCKET_CREDENTIAL_KEYS": json.dumps({"new": base64.b64encode(_NEW).decode()}),
        "BITBUCKET_ACTIVE_KEY_ID": "new",
        "GITLAB_CREDENTIAL_KEYS": json.dumps(
            {"old": base64.b64encode(_OLD).decode(), "new": base64.b64encode(_NEW).decode()}
        ),
        "GITLAB_ACTIVE_KEY_ID": "new",
    }
    result = subprocess.run(
        [sys.executable, "-m", "app.credential_key_rotation", "dry-run"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "counts": {
            "bitbucket_tokens": {"old": 1},
            "bitbucket_webhook_secrets": {"old": 1},
            "gitlab_tokens": {"old": 1},
        },
        "missing_key_ids": {
            "bitbucket_tokens": ["old"],
            "bitbucket_webhook_secrets": ["old"],
        },
    }
    assert "test-token" not in result.stdout + result.stderr
    for active_variable in ("BITBUCKET_ACTIVE_KEY_ID", "GITLAB_ACTIVE_KEY_ID"):
        invalid = subprocess.run(
            [sys.executable, "-m", "app.credential_key_rotation", "dry-run"],
            env={**env, active_variable: "not-in-keyring"},
            capture_output=True,
            text=True,
            check=False,
        )
        assert (invalid.returncode, invalid.stdout, invalid.stderr) == (
            1,
            "",
            "operation_failed\n",
        )


def test_run_reports_corrupt_row_and_processes_later_rows_and_kinds(
    rotation_database: tuple[str, Connection, UUID, UUID, UUID, UUID],
) -> None:
    url, db, workspace, bb_installation, _, _ = rotation_database
    schema = db.scalar(text("SELECT current_schema()"))
    assert isinstance(schema, str)
    second_installation = UUID(int=bb_installation.int + 1)
    db.execute(
        text(
            "INSERT INTO provider_installations "
            "(id, workspace_id, provider, external_id, metadata) "
            "VALUES (:id, :workspace, 'bitbucket', 99, '{}')"
        ),
        {"id": second_installation, "workspace": workspace},
    )
    db.execute(
        text(
            "INSERT INTO bitbucket_installation_identities "
            "(provider_installation_id, workspace_id, bitbucket_workspace_uuid) "
            "VALUES (:id, :workspace, :external)"
        ),
        {"id": second_installation, "workspace": workspace, "external": uuid4()},
    )
    valid = BitbucketCredentialCipher(active_key_id="old", keys={"old": _OLD}).encrypt(
        "second-token",
        context=BitbucketContext(second_installation, workspace),
        purpose=CredentialPurpose.TOKEN,
    )
    db.execute(
        text(
            "INSERT INTO bitbucket_connections "
            "(provider_installation_id, workspace_id, token_ciphertext, token_key_id) "
            "VALUES (:id, :workspace, :ciphertext, 'old')"
        ),
        {"id": second_installation, "workspace": workspace, "ciphertext": valid.ciphertext},
    )
    db.execute(
        text(
            "UPDATE bitbucket_connections SET token_ciphertext = :corrupt "
            "WHERE provider_installation_id = :id"
        ),
        {"id": bb_installation, "corrupt": b"BBC1" + b"x" * 32},
    )
    db.commit()
    database_url = (
        make_url(url)
        .update_query_dict({"options": f"-csearch_path={schema}"})
        .render_as_string(hide_password=False)
    )
    keyring = json.dumps(
        {"old": base64.b64encode(_OLD).decode(), "new": base64.b64encode(_NEW).decode()}
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.credential_key_rotation",
            "run",
            "--batch-size",
            "1",
            "--max-batches",
            "5",
        ],
        env={
            **os.environ,
            "DATABASE_URL": database_url,
            "BITBUCKET_CREDENTIAL_KEYS": keyring,
            "BITBUCKET_ACTIVE_KEY_ID": "new",
            "GITLAB_CREDENTIAL_KEYS": keyring,
            "GITLAB_ACTIVE_KEY_ID": "new",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "updated": {
            "bitbucket_tokens": 1,
            "bitbucket_webhook_secrets": 1,
            "gitlab_tokens": 1,
        },
        "counts": {
            "bitbucket_tokens": {"old": 1, "new": 1},
            "bitbucket_webhook_secrets": {"new": 1},
            "gitlab_tokens": {"new": 1},
        },
        "ready": False,
        "failure_count": 1,
        "failed_rows": [
            {
                "kind": "bitbucket_tokens",
                "id": str(bb_installation),
                "key_id": "old",
                "error": "ValueError",
            }
        ],
    }
    assert "second-token" not in result.stdout + result.stderr
    assert "BBC1" not in result.stdout + result.stderr


def test_gitlab_revocation_during_batch_keeps_retained_old_envelope(
    rotation_database: tuple[str, Connection, UUID, UUID, UUID, UUID],
) -> None:
    url, db, _, _, gl_installation, _ = rotation_database
    schema = db.scalar(text("SELECT current_schema()"))
    assert isinstance(schema, str)
    db.rollback()

    class RevokingCipher(CredentialCipher):
        def encrypt(self, token: str, *, context: GitLabContext) -> GitLabEncrypted:
            db.execute(
                text(
                    "UPDATE gitlab_connections SET revoked_at = now(), intake_enabled = false "
                    "WHERE provider_installation_id = :id"
                ),
                {"id": gl_installation},
            )
            db.commit()
            return super().encrypt(token, context=context)

    async def exercise() -> None:
        engine = create_async_engine(url, connect_args={"options": f"-csearch_path={schema}"})
        try:
            operator = KeyRotation(
                engine,
                bitbucket=BitbucketCredentialCipher(
                    active_key_id="new", keys={"old": _OLD, "new": _NEW}
                ),
                gitlab=RevokingCipher(active_key_id="new", keys={"old": _OLD, "new": _NEW}),
                bitbucket_active="new",
                gitlab_active="new",
            )
            assert await operator.rotate_batch("gitlab_tokens", batch_size=1) == 0
            assert (await operator.counts())["gitlab_tokens"] == {"old": 1}
        finally:
            await engine.dispose()

    asyncio.run(exercise())
    assert db.scalar(text("SELECT revoked_at IS NOT NULL FROM gitlab_connections")) is True
