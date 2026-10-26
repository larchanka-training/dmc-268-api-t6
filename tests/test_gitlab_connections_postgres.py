"""GitLab configuration and receipt integrity at the real PostgreSQL boundary."""

from __future__ import annotations

import os
from collections.abc import Iterator
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import CheckConstraint, Connection, create_engine, inspect, text
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command


@pytest.fixture
def gitlab_schema() -> Iterator[Connection]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"test_gitlab_connections_{uuid4().hex}"
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


def _installation(
    connection: Connection, *, provider: str = "gitlab", external: int = 1
) -> tuple[str, str]:
    workspace, installation = str(uuid4()), str(uuid4())
    connection.execute(
        text("INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, 'test', 7)"),
        {"id": workspace},
    )
    connection.execute(
        text(
            "INSERT INTO provider_installations (id, workspace_id, provider, "
            "external_id, metadata) VALUES (:id, :workspace, :provider, :external, "
            "'{}')"
        ),
        {"id": installation, "workspace": workspace, "provider": provider, "external": external},
    )
    return workspace, installation


def _connection(
    connection: Connection, workspace: str, installation: str, origin: str = "https://gitlab.com"
) -> None:
    connection.execute(
        text(
            "INSERT INTO gitlab_connections (provider_installation_id, workspace_id, "
            "instance_origin, token_ciphertext, credential_key_id, "
            "webhook_secret_digest) VALUES (:id, :workspace, :origin, :envelope, "
            "'test-key', :digest)"
        ),
        {
            "id": installation,
            "workspace": workspace,
            "origin": origin,
            "envelope": b"encrypted-test-envelope",
            "digest": "a" * 64,
        },
    )


@pytest.mark.integration
def test_gitlab_connection_is_disabled_and_owned_by_its_installation(
    gitlab_schema: Connection,
) -> None:
    connection = gitlab_schema
    command.upgrade(_config(connection), "head")
    workspace, installation = _installation(connection)
    _connection(connection, workspace, installation)
    assert tuple(
        connection.execute(
            text(
                "SELECT provider, instance_origin, credential_generation, intake_enabled,"
                " revoked_at FROM gitlab_connections"
            )
        ).one()
    ) == ("gitlab", "https://gitlab.com", 1, False, None)


def _repository(connection: Connection, installation: str, project: int = 42) -> str:
    repository = str(uuid4())
    connection.execute(
        text(
            "INSERT INTO repositories (id, provider_installation_id, external_id, "
            "full_name, default_branch, web_url) VALUES (:id, :installation, "
            ":project, 'group/project', 'main', 'https://gitlab.com/group/project')"
        ),
        {"id": repository, "installation": installation, "project": project},
    )
    return repository


def _binding(
    connection: Connection,
    repository: str,
    installation: str,
    origin: str = "https://gitlab.com",
    project: int = 42,
) -> None:
    connection.execute(
        text(
            "INSERT INTO gitlab_project_bindings (repository_id, "
            "provider_installation_id, instance_origin, project_external_id) VALUES "
            "(:repository, :installation, :origin, :project)"
        ),
        {
            "repository": repository,
            "installation": installation,
            "origin": origin,
            "project": project,
        },
    )


@pytest.mark.integration
def test_current_project_reservation_prevents_cross_tenant_instance_project_ownership(
    gitlab_schema: Connection,
) -> None:
    connection = gitlab_schema
    command.upgrade(_config(connection), "head")
    workspace, installation = _installation(connection)
    _connection(connection, workspace, installation)
    repository = _repository(connection, installation)
    _binding(connection, repository, installation)
    assert tuple(
        connection.execute(
            text("SELECT repository_id::text, project_external_id FROM gitlab_project_bindings")
        ).one()
    ) == (repository, 42)


def _receipt(
    connection: Connection,
    *,
    provider: str = "gitlab",
    installation: str | None = None,
    origin: str | None = None,
) -> str:
    receipt = str(uuid4())
    connection.execute(
        text(
            "INSERT INTO webhook_events (id, delivery_id, event, payload, provider, "
            "provider_installation_id, gitlab_connection_id) VALUES (:id, :delivery, "
            "'merge_request', '{}', :provider, :installation, :origin)"
        ),
        {
            "id": receipt,
            "delivery": receipt,
            "provider": provider,
            "installation": installation,
            "origin": origin,
        },
    )
    return receipt


@pytest.mark.integration
def test_gitlab_receipt_pins_a_configured_connection(gitlab_schema: Connection) -> None:
    connection = gitlab_schema
    command.upgrade(_config(connection), "head")
    workspace, installation = _installation(connection)
    _connection(connection, workspace, installation)
    receipt = _receipt(connection, installation=installation, origin=installation)
    assert tuple(
        connection.execute(
            text("SELECT provider, gitlab_connection_id::text FROM webhook_events WHERE id=:id"),
            {"id": receipt},
        ).one()
    ) == ("gitlab", installation)


@pytest.mark.integration
def test_connection_rejects_forged_tenant_provider_credentials_and_origin(
    gitlab_schema: Connection,
) -> None:
    from sqlalchemy.exc import IntegrityError

    connection = gitlab_schema
    command.upgrade(_config(connection), "head")
    workspace, installation = _installation(connection)
    other_workspace, github = _installation(connection, provider="github")
    for owner, tenant in (
        (installation, other_workspace),
        (github, other_workspace),
        (str(uuid4()), workspace),
    ):
        with pytest.raises(IntegrityError), connection.begin_nested():
            _connection(connection, tenant, owner)
    _connection(connection, workspace, installation)
    for assignment in (
        "credential_generation=0",
        "credential_generation=-1",
        "token_ciphertext=NULL",
        "credential_key_id=NULL",
        "token_ciphertext=''::bytea",
        "credential_key_id='  '",
        "token_ciphertext=NULL,credential_key_id=NULL",
        "webhook_secret_digest='ABC'",
        "intake_enabled=true,revoked_at=now()",
    ):
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(text(f"UPDATE gitlab_connections SET {assignment}"))
    # Test the structural origin check on INSERT, independently of identity immutability.
    for index, origin in enumerate(
        (
            "http://gitlab.com",
            "https://GitLab.com",
            "https://user@gitlab.com",
            "https://gitlab.com/path",
            "https://gitlab.com/",
            "https://gitlab.com?x=1",
            "https://gitlab.com#x",
            "https://gitlab.com:443",
        ),
        start=2,
    ):
        tenant, owner = _installation(connection, external=index)
        with pytest.raises(IntegrityError), connection.begin_nested():
            _connection(connection, tenant, owner, origin)
    for assignment in (
        "workspace_id=:other",
        "provider_installation_id=:other",
        "provider='github'",
        "instance_origin='https://other.example'",
    ):
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(
                text(f"UPDATE gitlab_connections SET {assignment}"), {"other": other_workspace}
            )
    for assignment in ("workspace_id=:other", "provider='github'"):
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(
                text(f"UPDATE provider_installations SET {assignment} WHERE id=:id"),
                {"other": other_workspace, "id": installation},
            )
    # Rotation is permitted, but a retired installation UUID can never be revived.
    connection.execute(
        text(
            "UPDATE gitlab_connections SET credential_generation=2,credential_key_id="
            "'rotated',token_ciphertext=:envelope,intake_enabled=true"
        ),
        {"envelope": b"rotated-envelope"},
    )
    connection.execute(
        text(
            "UPDATE gitlab_connections SET intake_enabled=false,revoked_at=now(),toke"
            "n_ciphertext=NULL,credential_key_id=NULL"
        )
    )
    for assignment in (
        "revoked_at=NULL",
        "intake_enabled=true",
        "revoked_at=revoked_at+interval '1 second'",
    ):
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(text(f"UPDATE gitlab_connections SET {assignment}"))


@pytest.mark.integration
def test_bindings_enforce_repository_project_instance_and_exclusive_current_ownership(
    gitlab_schema: Connection,
) -> None:
    from sqlalchemy.exc import IntegrityError

    connection = gitlab_schema
    command.upgrade(_config(connection), "head")
    workspace, installation = _installation(connection)
    other_workspace, other = _installation(connection, external=2)
    third_workspace, third = _installation(connection, external=3)
    _connection(connection, workspace, installation)
    _connection(connection, other_workspace, other)
    _connection(connection, third_workspace, third, "https://gitlab.example")
    repository = _repository(connection, installation)
    other_repository = _repository(connection, other)
    third_repository = _repository(connection, third)
    zero_project = _repository(connection, other, 0)
    for repo, owner, origin, project in (
        (repository, other, "https://gitlab.com", 42),
        (repository, installation, "https://gitlab.com", 43),
        (repository, installation, "https://gitlab.example", 42),
        (str(uuid4()), installation, "https://gitlab.com", 42),
        (zero_project, other, "https://gitlab.com", 0),
    ):
        with pytest.raises(IntegrityError), connection.begin_nested():
            _binding(connection, repo, owner, origin, project)
    _binding(connection, repository, installation)
    with pytest.raises(IntegrityError), connection.begin_nested():
        _binding(connection, other_repository, other)
    _binding(connection, third_repository, third, "https://gitlab.example")
    for assignment in ("provider_installation_id=:other", "external_id=43"):
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(
                text(f"UPDATE repositories SET {assignment} WHERE id=:id"),
                {"id": repository, "other": other},
            )
    assert connection.scalar(text("SELECT count(*) FROM gitlab_project_bindings")) == 2


@pytest.mark.integration
def test_receipt_origin_rejects_null_unknown_mismatched_unconfigured_and_mutable_identity(
    gitlab_schema: Connection,
) -> None:
    from sqlalchemy.exc import IntegrityError

    connection = gitlab_schema
    command.upgrade(_config(connection), "head")
    workspace, installation = _installation(connection)
    other_workspace, other = _installation(connection, external=2)
    _, github = _installation(connection, provider="github")
    _, unconfigured = _installation(connection, external=3)
    _connection(connection, workspace, installation)
    _connection(connection, other_workspace, other)
    for provider, owner, origin in (
        ("gitlab", None, None),
        ("gitlab", installation, None),
        ("gitlab", None, installation),
        ("gitlab", installation, other),
        ("gitlab", github, github),
        ("gitlab", unconfigured, unconfigured),
        ("gitlab", str(uuid4()), str(uuid4())),
        ("github", github, installation),
        ("unknown", None, None),
    ):
        with pytest.raises(IntegrityError), connection.begin_nested():
            _receipt(connection, provider=provider, installation=owner, origin=origin)
    receipt = _receipt(connection, installation=installation, origin=installation)
    _receipt(connection, provider="github")  # Unknown legacy GitHub installation is allowed.
    for assignment in (
        "provider='github',gitlab_connection_id=NULL",
        "gitlab_connection_id=NULL",
        "provider_installation_id=:other,gitlab_connection_id=:other",
    ):
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(
                text(f"UPDATE webhook_events SET {assignment} WHERE id=:id"),
                {"id": receipt, "other": other},
            )
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(
            text("DELETE FROM gitlab_connections WHERE provider_installation_id=:id"),
            {"id": installation},
        )
    # Receipt progress fields remain mutable under the origin guard.
    connection.execute(
        text(
            "UPDATE webhook_events SET projection_attempt_count=1,projected_at=now() WHERE id=:id"
        ),
        {"id": receipt},
    )
    assert (
        connection.scalar(
            text("SELECT projection_attempt_count FROM webhook_events WHERE id=:id"),
            {"id": receipt},
        )
        == 1
    )


@pytest.mark.integration
def test_empty_gitlab_migration_round_trips_preserving_github_receipts_and_settings(
    gitlab_schema: Connection,
) -> None:
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from app.bootstrap.db_metadata import metadata

    connection = gitlab_schema
    config = _config(connection)
    command.upgrade(config, "20261026_0041")
    _, installation = _installation(connection, provider="github")
    _repository(connection, installation)
    connection.execute(
        text(
            "UPDATE repositories SET enabled=false,installation_available=false,branc"
            "h_filter=ARRAY['release/*']"
        )
    )
    receipt = str(uuid4())
    connection.execute(
        text(
            "INSERT INTO webhook_events (id,delivery_id,event,payload) VALUES "
            "(:id,'legacy-delivery','installation',CAST(:payload AS jsonb))"
        ),
        {"id": receipt, "payload": '{"unchanged":true}'},
    )
    tables = ("workspaces", "provider_installations", "repositories", "webhook_events")
    before = {
        table: connection.execute(text(f"SELECT to_jsonb(t) FROM {table} t")).scalar_one()
        for table in tables
    }
    connection.commit()
    for _ in range(2):
        command.upgrade(config, "head")
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20261026_0042"
        for table in tables:
            after = connection.execute(text(f"SELECT to_jsonb(t) FROM {table} t")).scalar_one()
            if table == "webhook_events":
                assert after.pop("provider") == "github"
                assert after.pop("gitlab_connection_id") is None
            assert after == before[table]
        # Alembic does not compare CHECK constraints: retain explicit physical and
        # metadata names; the adversarial tests above exercise their SQL semantics.
        expected_checks = {
            "gitlab_connections": {
                "ck_gitlab_connections_provider",
                "ck_gitlab_connections_generation",
                "ck_gitlab_connections_webhook_digest",
                "ck_gitlab_connections_origin",
                "ck_gitlab_connections_credential_pair",
                "ck_gitlab_connections_enabled",
            },
            "gitlab_project_bindings": {"ck_gitlab_project_bindings_project_positive"},
            "webhook_events": {
                "ck_webhook_events_provider",
                "ck_webhook_events_gitlab_origin",
                "ck_webhook_events_payload_present",
                "ck_webhook_events_projection_claim_pair",
                "ck_webhook_events_projection_attempt_count_nonnegative",
            },
        }
        for table, expected in expected_checks.items():
            assert {
                item["name"] for item in inspect(connection).get_check_constraints(table)
            } == expected
            assert {
                constraint.name
                for constraint in metadata.tables[table].constraints
                if isinstance(constraint, CheckConstraint)
            } == expected
        assert (
            compare_metadata(
                MigrationContext.configure(
                    connection, opts={"compare_type": True, "compare_server_default": True}
                ),
                metadata,
            )
            == []
        )
        connection.commit()
        command.downgrade(config, "20261026_0041")
        assert {
            table: connection.execute(text(f"SELECT to_jsonb(t) FROM {table} t")).scalar_one()
            for table in tables
        } == before
        connection.commit()
    command.upgrade(config, "head")
    connection.execute(
        text(
            "INSERT INTO webhook_events (id,delivery_id,event,payload) VALUES "
            "(:id,'legacy-insert-after-upgrade','installation','{}')"
        ),
        {"id": str(uuid4())},
    )
    assert connection.scalars(text("SELECT DISTINCT provider FROM webhook_events")).all() == [
        "github"
    ]
    assert connection.scalar(text("SELECT nextval('gitlab_installation_external_id_seq')")) == 1


@pytest.mark.integration
def test_installation_sequence_starts_beyond_legacy_gitlab_ids_and_does_not_cycle(
    gitlab_schema: Connection,
) -> None:
    from sqlalchemy.exc import DBAPIError

    connection = gitlab_schema
    command.upgrade(_config(connection), "20261026_0041")
    _installation(connection, external=700)
    _installation(connection, provider="github", external=900)
    connection.commit()
    command.upgrade(_config(connection), "head")
    assert [
        connection.scalar(text("SELECT nextval('gitlab_installation_external_id_seq')"))
        for _ in range(2)
    ] == [701, 702]
    assert tuple(
        connection.execute(
            text(
                "SELECT seqmin,seqmax,seqcycle FROM pg_sequence WHERE "
                "seqrelid='gitlab_installation_external_id_seq'::regclass"
            )
        ).one()
    ) == (1, 9223372036854775807, False)
    connection.execute(
        text("SELECT setval('gitlab_installation_external_id_seq',9223372036854775807,true)")
    )
    with pytest.raises(DBAPIError, match="maximum value"), connection.begin_nested():
        connection.execute(text("SELECT nextval('gitlab_installation_external_id_seq')"))


@pytest.mark.integration
def test_downgrade_refuses_to_silently_delete_retired_gitlab_configuration(
    gitlab_schema: Connection,
) -> None:
    connection = gitlab_schema
    config = _config(connection)
    command.upgrade(config, "head")
    workspace, installation = _installation(connection)
    _connection(connection, workspace, installation)
    receipt = _receipt(connection, installation=installation, origin=installation)
    connection.execute(
        text(
            "UPDATE gitlab_connections SET "
            "revoked_at=now(),token_ciphertext=NULL,credential_key_id=NULL"
        )
    )
    connection.commit()
    with pytest.raises(RuntimeError, match="configuration and receipts"):
        command.downgrade(config, "20261026_0041")
    connection.rollback()
    assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20261026_0042"
    assert connection.scalar(text("SELECT count(*) FROM gitlab_connections")) == 1
    connection.execute(text("DELETE FROM webhook_events WHERE id=:id"), {"id": receipt})
    connection.commit()
    with pytest.raises(RuntimeError, match="configuration and receipts"):
        command.downgrade(config, "20261026_0041")
    connection.rollback()
    connection.execute(text("DELETE FROM gitlab_connections"))
    connection.commit()
    command.downgrade(config, "20261026_0041")


@pytest.mark.integration
def test_two_transactions_competing_for_one_project_have_exactly_one_owner(
    gitlab_schema: Connection,
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlalchemy.exc import IntegrityError

    connection = gitlab_schema
    command.upgrade(_config(connection), "head")
    contenders = []
    for external in (1, 2):
        workspace, installation = _installation(connection, external=external)
        _connection(connection, workspace, installation)
        contenders.append((_repository(connection, installation), installation))
    schema = connection.scalar(text("SELECT current_schema()"))
    connection.commit()
    barrier = Barrier(2)

    def reserve(candidate: tuple[str, str]) -> bool:
        with connection.engine.connect() as contender:
            contender.execute(text(f'SET search_path TO "{schema}"'))
            barrier.wait(timeout=10)
            try:
                _binding(contender, *candidate)
                contender.commit()
                return True
            except IntegrityError as error:
                assert (
                    getattr(getattr(error.orig, "diag", None), "constraint_name", None)
                    == "uq_gitlab_project_bindings_project"
                )
                contender.rollback()
                return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(reserve, contenders)) == [False, True]
    assert connection.scalar(text("SELECT count(*) FROM gitlab_project_bindings")) == 1


@pytest.mark.integration
def test_retire_and_rebind_reservation_preserves_historical_run_and_receipt_origin(
    gitlab_schema: Connection,
) -> None:
    from sqlalchemy.exc import IntegrityError

    connection = gitlab_schema
    command.upgrade(_config(connection), "head")
    workspace, installation = _installation(connection)
    other_workspace, other = _installation(connection, external=2)
    _connection(connection, workspace, installation)
    _connection(connection, other_workspace, other)
    repository = _repository(connection, installation)
    _binding(connection, repository, installation)
    receipt = _receipt(connection, installation=installation, origin=installation)
    prompt, rule, change, run = (str(uuid4()) for _ in range(4))
    connection.execute(
        text(
            "INSERT INTO prompt_versions (id,key,version,content,checksum) VALUES "
            "(:id,'review',1,'test',:checksum)"
        ),
        {"id": prompt, "checksum": "a" * 64},
    )
    connection.execute(
        text(
            "INSERT INTO rule_versions (id,repository_id,version,rules,checksum) "
            "VALUES (:id,:repo,1,'[]',:checksum)"
        ),
        {"id": rule, "repo": repository, "checksum": "b" * 64},
    )
    connection.execute(
        text(
            "INSERT INTO code_changes (id,repository_id,external_id,external_number,t"
            "itle,source_branch,target_branch,base_sha,head_sha,state,web_url) VALUES"
            " (:id,:repo,17,1,'historical MR','feature','main',:base,:head,'open','ht"
            "tps://gitlab.com/group/project/-/merge_requests/1')"
        ),
        {"id": change, "repo": repository, "base": "b" * 40, "head": "a" * 40},
    )
    connection.execute(
        text(
            "INSERT INTO runs (id,code_change_id,origin_provider_installation_id,base"
            "_sha,head_sha,state,trigger,idempotency_key,engine,rule_version_id,promp"
            "t_version_id,available_at) VALUES (:id,:change,:origin,:base,:head,'succ"
            "eeded','webhook',:key,'fast',:rule,:prompt,now())"
        ),
        {
            "id": run,
            "change": change,
            "origin": installation,
            "base": "b" * 40,
            "head": "a" * 40,
            "key": "c" * 64,
            "rule": rule,
            "prompt": prompt,
        },
    )
    before = connection.scalar(text("SELECT to_jsonb(r) FROM runs r WHERE id=:id"), {"id": run})
    connection.execute(
        text(
            "UPDATE gitlab_connections SET "
            "revoked_at=now(),token_ciphertext=NULL,credential_key_id=NULL WHERE "
            "provider_installation_id=:id"
        ),
        {"id": installation},
    )
    # Reservation remains held until the management transaction explicitly removes it.
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(
            text("UPDATE repositories SET provider_installation_id=:other WHERE id=:repo"),
            {"other": other, "repo": repository},
        )
    connection.execute(
        text("DELETE FROM gitlab_project_bindings WHERE repository_id=:id"), {"id": repository}
    )
    connection.execute(
        text("UPDATE repositories SET provider_installation_id=:other WHERE id=:repo"),
        {"other": other, "repo": repository},
    )
    _binding(connection, repository, other)
    assert (
        connection.scalar(text("SELECT to_jsonb(r) FROM runs r WHERE id=:id"), {"id": run})
        == before
    )
    assert (
        connection.scalar(
            text("SELECT gitlab_connection_id::text FROM webhook_events WHERE id=:id"),
            {"id": receipt},
        )
        == installation
    )
    assert connection.scalar(text("SELECT count(*) FROM code_changes")) == 1
    assert connection.scalar(text("SELECT count(*) FROM gitlab_connections")) == 2
