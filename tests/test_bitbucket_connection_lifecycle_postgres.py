"""Move-only baseline Bitbucket connection tests."""

from __future__ import annotations

from tests.bitbucket_connection_postgres_support import (
    UUID,
    AsyncSession,
    AuthScope,
    BitbucketBindingGenerationChanged,
    BitbucketCredentialCipher,
    BitbucketCredentialCodecAdapter,
    BitbucketCredentialGenerationChanged,
    BitbucketHttpClient,
    BitbucketRepositoryIdentity,
    BitbucketRepositoryProvider,
    BitbucketRepositoryWebhookSecret,
    BitbucketRequestCredentials,
    BitbucketTargetInactive,
    Connection,
    CredentialContext,
    CredentialPurpose,
    IntegrityError,
    ManageBitbucketConnection,
    Repository,
    ReviewsApiResources,
    SqlAlchemyBitbucketConnectionStore,
    SqlAlchemyBitbucketManagementUnitOfWork,
    SqlAlchemyBitbucketTargetReader,
    TestClient,
    _config,
    app,
    async_sessionmaker,
    asyncio,
    bitbucket_repository_external_id_seq,
    cast,
    command,
    create_async_engine,
    get_auth_scope,
    get_bitbucket_management,
    httpx,
    make_url,
    os,
    pytest,
    select,
    text,
    uuid4,
)

pytest_plugins = ("tests.bitbucket_connection_postgres_support",)


@pytest.mark.integration
def test_two_workspace_credentials_rotate_and_revoke_independently(
    bitbucket_connection_schema: Connection,
) -> None:
    db = bitbucket_connection_schema
    command.upgrade(_config(db), "head")
    first_workspace, second_workspace = uuid4(), uuid4()
    for workspace in (first_workspace, second_workspace):
        db.execute(
            text("INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, :name, 7)"),
            {"id": str(workspace), "name": str(workspace)},
        )
    schema = str(db.scalar(text("SELECT current_schema()")))
    db.commit()
    first_installation, second_installation = uuid4(), uuid4()
    remote_workspace = UUID("b2d0d95e-1f8d-4990-9fb2-f09f87ae5f60")
    cipher = BitbucketCredentialCipher(active_key_id="test", keys={"test": b"a" * 32})

    async def scenario() -> None:
        database_url = make_url(os.environ["TEST_DATABASE_URL"]).set(
            drivername="postgresql+psycopg_async"
        )
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                await connection.execute(text(f'SET search_path TO "{schema}"'))
                await connection.commit()
                async with AsyncSession(bind=connection) as session:
                    store = SqlAlchemyBitbucketConnectionStore(session)
                    for workspace, installation, token in (
                        (first_workspace, first_installation, "first-token"),
                        (second_workspace, second_installation, "second-token"),
                    ):
                        context = CredentialContext(installation, workspace)
                        await store.create_connection(
                            workspace_id=workspace,
                            installation_id=installation,
                            bitbucket_workspace_uuid=remote_workspace,
                            token=cipher.encrypt(
                                token, context=context, purpose=CredentialPurpose.TOKEN
                            ),
                        )
                        await store.bind_repository(
                            workspace_id=workspace,
                            installation_id=installation,
                            repository_uuid=remote_workspace,
                            full_name=f"{workspace}/private",
                            default_branch="main",
                            web_url=f"https://bitbucket.org/{workspace}/private",
                        )
                    await session.commit()
                    first_bindings = await store.list_bindings(workspace_id=first_workspace)
                    second_bindings = await store.list_bindings(workspace_id=second_workspace)
                    assert len(first_bindings) == len(second_bindings) == 1
                    assert first_bindings[0].installation_id == first_installation
                    assert second_bindings[0].installation_id == second_installation
                    assert first_bindings[0].repository_id != second_bindings[0].repository_id
                    rows = (
                        await session.execute(
                            text(
                                "SELECT provider_installation_id, workspace_id, "
                                "credential_generation, token_ciphertext "
                                "FROM bitbucket_connections "
                                "ORDER BY workspace_id"
                            )
                        )
                    ).all()
                    assert len(rows) == 2
                    assert all(row[2] == 1 for row in rows)
                    assert all(b"token" not in row[3] for row in rows)
                    for table, key, identity, uuid_columns in (
                        (
                            "bitbucket_installation_identities",
                            "provider_installation_id",
                            first_installation,
                            (
                                "provider_installation_id",
                                "workspace_id",
                                "bitbucket_workspace_uuid",
                            ),
                        ),
                        (
                            "bitbucket_repository_identities",
                            "repository_id",
                            first_bindings[0].repository_id,
                            (
                                "repository_id",
                                "provider_installation_id",
                                "workspace_id",
                                "repository_uuid",
                            ),
                        ),
                    ):
                        for column in uuid_columns:
                            with pytest.raises(IntegrityError) as immutable:
                                async with session.begin_nested():
                                    await session.execute(
                                        text(
                                            f"UPDATE {table} SET {column}=:changed "
                                            f"WHERE {key}=:identity"
                                        ),
                                        {"changed": uuid4(), "identity": identity},
                                    )
                            assert getattr(immutable.value.orig, "sqlstate", None) == "23514"
                    with pytest.raises(IntegrityError) as error:
                        async with session.begin_nested():
                            await session.execute(
                                text(
                                    "UPDATE bitbucket_connections SET revoked_at=now() "
                                    "WHERE provider_installation_id=:id"
                                ),
                                {"id": first_installation},
                            )
                    assert getattr(error.value.orig, "sqlstate", None) == "23514"
                    with pytest.raises(ValueError, match="connection_inactive"):
                        await store.rotate_token(
                            workspace_id=first_workspace,
                            installation_id=second_installation,
                            token=cipher.encrypt(
                                "cross-workspace-token",
                                context=CredentialContext(second_installation, second_workspace),
                                purpose=CredentialPurpose.TOKEN,
                            ),
                        )
                    assert not await store.revoke_connection(
                        workspace_id=first_workspace, installation_id=second_installation
                    )
                    assert (
                        await store.rotate_token(
                            workspace_id=first_workspace,
                            installation_id=first_installation,
                            token=cipher.encrypt(
                                "rotated-token",
                                context=CredentialContext(first_installation, first_workspace),
                                purpose=CredentialPurpose.TOKEN,
                            ),
                        )
                        == 2
                    )
                    await session.commit()
                    assert await store.revoke_connection(
                        workspace_id=first_workspace, installation_id=first_installation
                    )
                    await session.commit()
                    first = (
                        await session.execute(
                            text(
                                "SELECT credential_generation, token_ciphertext, revoked_at "
                                "FROM bitbucket_connections "
                                "WHERE provider_installation_id=:id"
                            ),
                            {"id": str(first_installation)},
                        )
                    ).one()
                    second = (
                        await session.execute(
                            text(
                                "SELECT credential_generation, token_ciphertext, revoked_at "
                                "FROM bitbucket_connections "
                                "WHERE provider_installation_id=:id"
                            ),
                            {"id": str(second_installation)},
                        )
                    ).one()
                    assert first[0] == 3 and first[1] is None and first[2] is not None
                    assert second[0] == 1 and second[1] is not None and second[2] is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_two_repositories_share_credential_and_disconnect_independently(
    bitbucket_connection_schema: Connection,
) -> None:
    db = bitbucket_connection_schema
    command.upgrade(_config(db), "head")
    workspace, installation = uuid4(), uuid4()
    remote_workspace = UUID("b2d0d95e-1f8d-4990-9fb2-f09f87ae5f60")
    first_remote, second_remote = uuid4(), uuid4()
    db.execute(
        text("INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, 'test', 7)"),
        {"id": workspace},
    )
    schema = str(db.scalar(text("SELECT current_schema()")))
    db.commit()
    cipher = BitbucketCredentialCipher(active_key_id="test", keys={"test": b"a" * 32})
    context = CredentialContext(installation, workspace)

    async def scenario() -> None:
        database_url = make_url(os.environ["TEST_DATABASE_URL"]).set(
            drivername="postgresql+psycopg_async"
        )
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                await connection.execute(text(f'SET search_path TO "{schema}"'))
                await connection.commit()
                async with AsyncSession(bind=connection) as session:
                    store = SqlAlchemyBitbucketConnectionStore(session)
                    await store.create_connection(
                        workspace_id=workspace,
                        installation_id=installation,
                        bitbucket_workspace_uuid=remote_workspace,
                        token=cipher.encrypt(
                            "shared-token", context=context, purpose=CredentialPurpose.TOKEN
                        ),
                    )
                    first, first_created = await store.bind_repository(
                        workspace_id=workspace,
                        installation_id=installation,
                        repository_uuid=first_remote,
                        full_name="nvdovnar/first",
                        default_branch="main",
                        web_url="https://bitbucket.org/nvdovnar/first",
                    )
                    second, second_created = await store.bind_repository(
                        workspace_id=workspace,
                        installation_id=installation,
                        repository_uuid=second_remote,
                        full_name="nvdovnar/second",
                        default_branch="main",
                        web_url="https://bitbucket.org/nvdovnar/second",
                    )
                    for repository_id in (first, second):
                        await store.set_repository_webhook_secret(
                            workspace_id=workspace,
                            installation_id=installation,
                            repository_id=repository_id,
                            secret=cipher.encrypt(
                                "h" * 40,
                                context=CredentialContext(installation, workspace, repository_id),
                                purpose=CredentialPurpose.WEBHOOK_SECRET,
                            ),
                        )
                    await session.commit()
                    reader = SqlAlchemyBitbucketTargetReader(lambda: AsyncSession(bind=connection))
                    first_target = await reader.resolve_target(
                        workspace_id=workspace,
                        installation_id=installation,
                        repository_uuid=first_remote,
                    )
                    second_target = await reader.resolve_target(
                        workspace_id=workspace,
                        installation_id=installation,
                        repository_uuid=second_remote,
                    )
                    assert first_target.workspace_id == workspace
                    assert first_target.bitbucket_workspace_uuid == remote_workspace
                    assert first_target.workspace_id != first_target.bitbucket_workspace_uuid
                    assert first_created and second_created
                    assert first != second
                    assert (
                        await session.scalar(
                            text(
                                "SELECT credential_generation FROM bitbucket_connections "
                                "WHERE provider_installation_id=:id"
                            ),
                            {"id": installation},
                        )
                        == 1
                    )
                    assert await store.disconnect_repository(
                        workspace_id=workspace,
                        installation_id=installation,
                        repository_id=first,
                    )
                    await session.commit()
                    rows = (
                        await session.execute(
                            select(Repository.id, Repository.installation_available)
                            .where(Repository.provider_installation_id == installation)
                            .order_by(Repository.id)
                        )
                    ).all()
                    assert {row[0]: row[1] for row in rows} == {first: False, second: True}
                    await reader.checkpoint(second_target)
                    with pytest.raises(BitbucketTargetInactive):
                        await reader.checkpoint(first_target)
                    rebound, newly_active = await store.bind_repository(
                        workspace_id=workspace,
                        installation_id=installation,
                        repository_uuid=first_remote,
                        full_name="nvdovnar/first",
                        default_branch="main",
                        web_url="https://bitbucket.org/nvdovnar/first",
                    )
                    assert rebound == first and newly_active
                    await store.set_repository_webhook_secret(
                        workspace_id=workspace,
                        installation_id=installation,
                        repository_id=first,
                        secret=cipher.encrypt(
                            "n" * 40,
                            context=CredentialContext(installation, workspace, first),
                            purpose=CredentialPurpose.WEBHOOK_SECRET,
                        ),
                    )
                    await session.commit()
                    await reader.checkpoint(second_target)
                    with pytest.raises(BitbucketBindingGenerationChanged):
                        await reader.checkpoint(first_target)
                    assert (
                        await reader.resolve_target(
                            workspace_id=workspace,
                            installation_id=installation,
                            repository_uuid=first_remote,
                        )
                    ).repository_id == first
                    assert await store.disconnect_repository(
                        workspace_id=workspace,
                        installation_id=installation,
                        repository_id=first,
                    )
                    await session.commit()
                    assert (
                        await session.scalar(
                            text(
                                "SELECT revoked_at FROM bitbucket_connections "
                                "WHERE provider_installation_id=:id"
                            ),
                            {"id": installation},
                        )
                        is None
                    )
                    assert await store.disconnect_repository(
                        workspace_id=workspace,
                        installation_id=installation,
                        repository_id=second,
                    )
                    await session.commit()
                    assert (
                        await session.scalar(
                            text(
                                "SELECT revoked_at FROM bitbucket_connections "
                                "WHERE provider_installation_id=:id"
                            ),
                            {"id": installation},
                        )
                        is not None
                    )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_bitbucket_repository_enters_common_list_only_for_current_workspace_member(
    bitbucket_connection_schema: Connection,
) -> None:
    db = bitbucket_connection_schema
    command.upgrade(_config(db), "head")
    workspace, installation, remote_workspace, remote_repo, other_workspace, other_installation = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    for portal_workspace in (workspace, other_workspace):
        db.execute(
            text("INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, 'test', 7)"),
            {"id": portal_workspace},
        )
    for user_id in (42, 43):
        db.execute(
            text("INSERT INTO github_user_profiles (id, login, name) VALUES (:id, :login, :login)"),
            {"id": user_id, "login": f"user-{user_id}"},
        )
    for portal_workspace in (workspace, other_workspace):
        db.execute(
            text(
                "INSERT INTO github_user_workspace_access (github_user_id, workspace_id) "
                "VALUES (42, :workspace)"
            ),
            {"workspace": portal_workspace},
        )
    schema = str(db.scalar(text("SELECT current_schema()")))
    db.commit()
    cipher = BitbucketCredentialCipher(active_key_id="test", keys={"test": b"a" * 32})
    context = CredentialContext(installation, workspace)
    database_url = make_url(os.environ["TEST_DATABASE_URL"]).set(
        drivername="postgresql+psycopg_async"
    )
    engine = create_async_engine(database_url, connect_args={"options": f"-csearch_path={schema}"})
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def setup() -> tuple[UUID, UUID]:
        async with factory.begin() as session:
            store = SqlAlchemyBitbucketConnectionStore(session)
            await store.create_connection(
                workspace_id=workspace,
                installation_id=installation,
                bitbucket_workspace_uuid=remote_workspace,
                token=cipher.encrypt(
                    "shared-token", context=context, purpose=CredentialPurpose.TOKEN
                ),
            )
            repository_id, created = await store.bind_repository(
                workspace_id=workspace,
                installation_id=installation,
                repository_uuid=remote_repo,
                full_name="nvdovnar/private",
                default_branch="main",
                web_url="https://bitbucket.org/nvdovnar/private",
            )
            assert created
            await store.create_connection(
                workspace_id=other_workspace,
                installation_id=other_installation,
                bitbucket_workspace_uuid=remote_workspace,
                token=cipher.encrypt(
                    "other-token",
                    context=CredentialContext(other_installation, other_workspace),
                    purpose=CredentialPurpose.TOKEN,
                ),
            )
            other_repository_id, other_created = await store.bind_repository(
                workspace_id=other_workspace,
                installation_id=other_installation,
                repository_uuid=remote_repo,
                full_name="other/private",
                default_branch="main",
                web_url="https://bitbucket.org/other/private",
            )
            assert other_created
            return repository_id, other_repository_id

    repository_id, other_repository_id = asyncio.run(setup())
    manager = ManageBitbucketConnection(
        uow_factory=lambda: SqlAlchemyBitbucketManagementUnitOfWork(factory),
        cipher=BitbucketCredentialCodecAdapter(cipher),
        provider=cast(BitbucketRepositoryProvider, object()),
        rule_sets={},
        webhook_base_url="https://portal.example",
    )
    app.state.reviews_api_resources = ReviewsApiResources(engine, factory)
    app.dependency_overrides[get_bitbucket_management] = lambda: manager
    try:
        client = TestClient(app)
        app.dependency_overrides[get_auth_scope] = lambda: AuthScope(
            42, (workspace, other_workspace)
        )
        assert {item["id"] for item in client.get("/api/repos").json()} == {
            str(repository_id),
            str(other_repository_id),
        }
        app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, (workspace,))
        own_connections = client.get(f"/api/workspaces/{workspace}/bitbucket/connections")
        assert own_connections.status_code == 200
        assert [item["repositoryId"] for item in own_connections.json()["connections"]] == [
            str(repository_id)
        ]
        assert (
            client.get(f"/api/workspaces/{other_workspace}/bitbucket/connections").status_code
            == 404
        )
        assert client.get(f"/api/repos/{other_repository_id}").status_code == 404
        app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, (other_workspace,))
        other_connections = client.get(f"/api/workspaces/{other_workspace}/bitbucket/connections")
        assert other_connections.status_code == 200
        assert [item["repositoryId"] for item in other_connections.json()["connections"]] == [
            str(other_repository_id)
        ]
        assert client.get(f"/api/workspaces/{workspace}/bitbucket/connections").status_code == 404
        assert client.get(f"/api/repos/{repository_id}").status_code == 404
        app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, (workspace,))
        visible = client.get("/api/repos")
        assert visible.status_code == 200
        assert [item["id"] for item in visible.json()] == [str(repository_id)]
        assert visible.json()[0]["provider"] == "bitbucket"
        app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, (other_workspace,))
        assert [item["id"] for item in client.get("/api/repos").json()] == [
            str(other_repository_id)
        ]
        app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, (workspace,))
        for body, field in (
            ({"waitForCi": "auto"}, "waitForCi"),
            ({"waitForCi": "always"}, "waitForCi"),
            ({"reviewEvent": "REQUEST_CHANGES"}, "reviewEvent"),
        ):
            rejected = client.patch(f"/api/repos/{repository_id}", json=body)
            assert rejected.status_code == 422
            assert rejected.json()["detail"][0]["loc"] == ["body", field]
        assert tuple(
            db.execute(
                text("SELECT wait_for_ci, review_event FROM repositories WHERE id=:id"),
                {"id": repository_id},
            ).one()
        ) == ("never", "comment")
        accepted = client.patch(
            f"/api/repos/{repository_id}",
            json={"waitForCi": "never", "reviewEvent": "COMMENT"},
        )
        assert accepted.status_code == 200
        assert (accepted.json()["waitForCi"], accepted.json()["reviewEvent"]) == (
            "never",
            "COMMENT",
        )
        app.dependency_overrides[get_auth_scope] = lambda: AuthScope(43, (workspace,))
        assert client.get("/api/repos").json() == []
        app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, (workspace,))
        db.execute(
            text(
                "DELETE FROM github_user_workspace_access "
                "WHERE github_user_id=42 AND workspace_id=:workspace"
            ),
            {"workspace": workspace},
        )
        db.commit()
        assert client.get("/api/repos").json() == []
    finally:
        app.dependency_overrides.clear()
        del app.state.reviews_api_resources
        asyncio.run(engine.dispose())


@pytest.mark.integration
def test_request_target_is_fenced_after_rotation_and_revocation(
    bitbucket_connection_schema: Connection,
) -> None:
    db = bitbucket_connection_schema
    command.upgrade(_config(db), "head")
    workspace, installation, repository_id = uuid4(), uuid4(), uuid4()
    remote_workspace = UUID("b2d0d95e-1f8d-4990-9fb2-f09f87ae5f60")
    remote_repo = UUID("3791f4cc-f59a-4412-9a52-f34a7829ba1c")
    db.execute(
        text("INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, 'test', 7)"),
        {"id": str(workspace)},
    )
    schema = str(db.scalar(text("SELECT current_schema()")))
    db.commit()
    cipher = BitbucketCredentialCipher(active_key_id="test", keys={"test": b"a" * 32})
    context = CredentialContext(installation, workspace)

    async def scenario() -> None:
        database_url = make_url(os.environ["TEST_DATABASE_URL"]).set(
            drivername="postgresql+psycopg_async"
        )
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                await connection.execute(text(f'SET search_path TO "{schema}"'))
                await connection.commit()
                async with AsyncSession(bind=connection) as session:
                    store = SqlAlchemyBitbucketConnectionStore(session)
                    await store.create_connection(
                        workspace_id=workspace,
                        installation_id=installation,
                        bitbucket_workspace_uuid=remote_workspace,
                        token=cipher.encrypt(
                            "original-token", context=context, purpose=CredentialPurpose.TOKEN
                        ),
                    )
                    external_id = await session.scalar(
                        select(bitbucket_repository_external_id_seq.next_value())
                    )
                    session.add(
                        Repository(
                            id=repository_id,
                            provider_installation_id=installation,
                            external_id=external_id,
                            full_name="nvdovnar/test-ai-review",
                            default_branch="main",
                            web_url="https://bitbucket.org/nvdovnar/test-ai-review",
                        )
                    )
                    await session.flush()
                    session.add(
                        BitbucketRepositoryIdentity(
                            repository_id=repository_id,
                            provider_installation_id=installation,
                            workspace_id=workspace,
                            repository_external_id=external_id,
                            repository_uuid=remote_repo,
                        )
                    )
                    session.add(BitbucketRepositoryWebhookSecret(repository_id=repository_id))
                    await session.commit()
                    reader = SqlAlchemyBitbucketTargetReader(lambda: AsyncSession(bind=connection))
                    authority = BitbucketRequestCredentials(reader, cipher)
                    target = await reader.resolve_target(
                        workspace_id=workspace,
                        installation_id=installation,
                        repository_uuid=remote_repo,
                    )
                    assert (await authority.credential_for(target)).token == "original-token"
                    assert not connection.in_transaction()
                    sent: list[str] = []

                    def provider(request: httpx.Request) -> httpx.Response:
                        assert not connection.in_transaction()
                        sent.append(str(request.url))
                        return httpx.Response(200)

                    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
                        response = await BitbucketHttpClient(http, authority).get(
                            target, "/2.0/repositories/nvdovnar/test-ai-review"
                        )
                        assert response.status_code == 200
                    assert sent == [
                        "https://api.bitbucket.org/2.0/repositories/nvdovnar/test-ai-review"
                    ]
                    await store.rotate_token(
                        workspace_id=workspace,
                        installation_id=installation,
                        token=cipher.encrypt(
                            "rotated-token", context=context, purpose=CredentialPurpose.TOKEN
                        ),
                    )
                    await session.commit()
                    with pytest.raises(BitbucketCredentialGenerationChanged):
                        await authority.credential_for(target)
                    fresh = await reader.resolve_target(
                        workspace_id=workspace,
                        installation_id=installation,
                        repository_uuid=remote_repo,
                    )
                    assert (await authority.credential_for(fresh)).token == "rotated-token"
                    assert not connection.in_transaction()
                    await store.revoke_connection(
                        workspace_id=workspace, installation_id=installation
                    )
                    await session.commit()
                    with pytest.raises(BitbucketTargetInactive):
                        await reader.checkpoint(fresh)
                    with pytest.raises(BitbucketTargetInactive):
                        await reader.checkpoint_in_session(session, fresh)
        finally:
            await engine.dispose()

    asyncio.run(scenario())
