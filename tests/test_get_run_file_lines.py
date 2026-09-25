import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi.testclient import TestClient

from app.main import app, get_file_blob_cache, get_run_repository
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.get_run_file_lines import (
    BlobCacheEntry,
    BlobCacheKey,
    BlobCacheStatus,
    FileLinesExpired,
    FileLinesNotFound,
    FileLinesPage,
    GetRunFileLines,
)
from app.modules.reviews.application.process_run import (
    ReviewRunProcessor,
    RunDiffInput,
    RunDiffProvider,
)

RUN_ID = UUID("00000000-0000-0000-0000-000000000100")
KEY = BlobCacheKey(
    code_change_id=UUID("00000000-0000-0000-0000-000000000200"),
    head_sha="a" * 40,
    path="app/service.py",
)


class FakeFileRepository:
    def __init__(self, key: BlobCacheKey | None) -> None:
        self.key = key
        self.calls: list[tuple[UUID, str]] = []

    async def get_run_file_key(self, run_id: UUID, path: str) -> BlobCacheKey | None:
        self.calls.append((run_id, path))
        return self.key


class FakeBlobCache:
    def __init__(self, status: BlobCacheStatus, content: str | None = None) -> None:
        self.status = status
        self.content = content
        self.calls: list[BlobCacheKey] = []

    async def get(self, key: BlobCacheKey) -> BlobCacheEntry:
        self.calls.append(key)
        return BlobCacheEntry(status=self.status, content=self.content)


def test_get_run_file_lines_pages_first_middle_final_and_empty_page() -> None:
    repository = FakeFileRepository(KEY)
    cache = FakeBlobCache(BlobCacheStatus.HIT, "one\ntwo\nthree\nfour\n")

    first = asyncio.run(GetRunFileLines(repository, cache).execute(RUN_ID, "app/service.py", 0, 2))
    middle = asyncio.run(GetRunFileLines(repository, cache).execute(RUN_ID, "app/service.py", 2, 1))
    final = asyncio.run(GetRunFileLines(repository, cache).execute(RUN_ID, "app/service.py", 3, 10))
    empty = asyncio.run(GetRunFileLines(repository, cache).execute(RUN_ID, "app/service.py", 4, 10))

    assert first == FileLinesPage("app/service.py", 1, ["one", "two"], 4, 2)
    assert middle == FileLinesPage("app/service.py", 3, ["three"], 4, 3)
    assert final == FileLinesPage("app/service.py", 4, ["four"], 4, None)
    assert empty == FileLinesPage("app/service.py", 5, [], 4, None)


def test_get_run_file_lines_rejects_unowned_path_and_invalid_offset() -> None:
    cache = FakeBlobCache(BlobCacheStatus.HIT, "one")

    try:
        asyncio.run(
            GetRunFileLines(FakeFileRepository(None), cache).execute(RUN_ID, "missing.py", 0, 1)
        )
    except FileLinesNotFound:
        pass
    else:
        raise AssertionError("unowned path must not be served")

    try:
        asyncio.run(
            GetRunFileLines(FakeFileRepository(KEY), cache).execute(RUN_ID, "app/service.py", 2, 1)
        )
    except ValueError as error:
        assert str(error) == "offset exceeds file length"
    else:
        raise AssertionError("offset beyond EOF must be rejected")


def test_get_run_file_lines_distinguishes_expired_and_missing_cache_entries() -> None:
    repository = FakeFileRepository(KEY)

    try:
        asyncio.run(
            GetRunFileLines(repository, FakeBlobCache(BlobCacheStatus.EXPIRED)).execute(
                RUN_ID, "app/service.py", 0, 1
            )
        )
    except FileLinesExpired:
        pass
    else:
        raise AssertionError("expired blobs must be reported distinctly")

    try:
        asyncio.run(
            GetRunFileLines(repository, FakeBlobCache(BlobCacheStatus.MISS)).execute(
                RUN_ID, "app/service.py", 0, 1
            )
        )
    except FileLinesNotFound:
        pass
    else:
        raise AssertionError("missing blobs must not trigger a provider fetch")


def test_files_api_returns_camel_case_envelope_and_statuses() -> None:
    repository = FakeFileRepository(KEY)
    cache = FakeBlobCache(BlobCacheStatus.HIT, "one\ntwo\nthree")
    app.dependency_overrides[get_run_repository] = lambda: repository
    app.dependency_overrides[get_file_blob_cache] = lambda: cache
    try:
        client = TestClient(app)
        response = client.get(
            f"/api/runs/{RUN_ID}/files", params={"path": "app/service.py", "limit": 2}
        )
        invalid_offset = client.get(
            f"/api/runs/{RUN_ID}/files", params={"path": "app/service.py", "offset": 4}
        )
        invalid_path = client.get(f"/api/runs/{RUN_ID}/files", params={"path": "../secret.py"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "path": "app/service.py",
        "startLine": 1,
        "lines": ["one", "two"],
        "totalLines": 3,
        "nextOffset": 2,
    }
    assert invalid_offset.status_code == 422
    assert invalid_path.status_code == 422


def test_files_api_maps_missing_and_expired_cache_entries_to_404_and_410() -> None:
    repository = FakeFileRepository(KEY)
    app.dependency_overrides[get_run_repository] = lambda: repository
    try:
        client = TestClient(app)
        app.dependency_overrides[get_file_blob_cache] = lambda: FakeBlobCache(BlobCacheStatus.MISS)
        missing = client.get(f"/api/runs/{RUN_ID}/files", params={"path": "app/service.py"})
        app.dependency_overrides[get_file_blob_cache] = lambda: FakeBlobCache(
            BlobCacheStatus.EXPIRED
        )
        expired = client.get(f"/api/runs/{RUN_ID}/files", params={"path": "app/service.py"})
    finally:
        app.dependency_overrides.clear()

    assert missing.status_code == 404
    assert expired.status_code == 410


def test_in_memory_blob_cache_expires_entries_after_seven_days() -> None:
    from app.modules.reviews.infrastructure.blob_cache import InMemoryBlobCache

    now = datetime(2026, 9, 25, tzinfo=UTC)
    clock = [now]
    cache = InMemoryBlobCache(now=lambda: clock[0])
    asyncio.run(cache.put(KEY, "one", ttl=timedelta(days=7)))

    assert asyncio.run(cache.get(KEY)) == BlobCacheEntry(BlobCacheStatus.HIT, "one")
    clock[0] += timedelta(days=7)
    assert asyncio.run(cache.get(KEY)) == BlobCacheEntry(BlobCacheStatus.EXPIRED, None)


def test_processing_populates_immutable_cache_before_file_endpoint_reads_it() -> None:
    class Provider(RunDiffProvider):
        async def fetch_diff(self, *, code_change_id: UUID, head_sha: str) -> list[DiffSnapshot]:
            assert (code_change_id, head_sha) == (KEY.code_change_id, KEY.head_sha)
            return [DiffSnapshot(filename=KEY.path, patch="@@ -1 +1 @@\n-old\n+new")]

        async def fetch_file_content(
            self, *, code_change_id: UUID, head_sha: str, path: str
        ) -> str:
            assert (code_change_id, head_sha, path) == (KEY.code_change_id, KEY.head_sha, KEY.path)
            return "cached\nfile"

    class ProcessingRepository(FakeFileRepository):
        def __init__(self) -> None:
            super().__init__(None)
            self.snapshots: list[DiffSnapshot] = []

        async def get_run_diff_input(self, run_id: UUID) -> RunDiffInput | None:
            return RunDiffInput(KEY.code_change_id, KEY.head_sha) if run_id == RUN_ID else None

        async def replace_diff_snapshots(
            self, code_change_id: UUID, head_sha: str, snapshots: list[DiffSnapshot]
        ) -> None:
            assert (code_change_id, head_sha) == (KEY.code_change_id, KEY.head_sha)
            self.snapshots = snapshots
            self.key = KEY

    repository = ProcessingRepository()
    from app.modules.reviews.infrastructure.blob_cache import InMemoryBlobCache

    cache = InMemoryBlobCache()
    assert asyncio.run(ReviewRunProcessor(repository, Provider(), cache).execute(RUN_ID)) is True

    app.dependency_overrides[get_run_repository] = lambda: repository
    app.dependency_overrides[get_file_blob_cache] = lambda: cache
    try:
        response = TestClient(app).get(f"/api/runs/{RUN_ID}/files", params={"path": KEY.path})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "path": "app/service.py",
        "startLine": 1,
        "lines": ["cached", "file"],
        "totalLines": 2,
        "nextOffset": None,
    }


def test_sqlalchemy_blob_cache_is_readable_from_a_separate_adapter_instance() -> None:
    from app.modules.reviews.infrastructure.blob_cache import SqlAlchemyBlobCache

    class Result:
        def __init__(self, row: tuple[str, datetime] | None) -> None:
            self._row = row

        def one_or_none(self) -> tuple[str, datetime] | None:
            return self._row

    class Session:
        def __init__(self, store: dict[BlobCacheKey, tuple[str, datetime]]) -> None:
            self._store = store

        async def execute(self, statement: object) -> Result:
            compiled = statement.compile()  # type: ignore[attr-defined]
            params = compiled.params
            suffix = "" if "code_change_id" in params else "_1"
            key = BlobCacheKey(
                params[f"code_change_id{suffix}"],
                params[f"head_sha{suffix}"],
                params[f"path{suffix}"],
            )
            if statement.__visit_name__ == "insert":  # type: ignore[attr-defined]
                self._store[key] = (params["content"], params["expires_at"])
                return Result(None)
            return Result(self._store.get(key))

    class SessionContext:
        def __init__(self, session: Session) -> None:
            self._session = session

        async def __aenter__(self) -> Session:
            return self._session

        async def __aexit__(self, *args: object) -> None:
            return None

    class SessionFactory:
        def __init__(self) -> None:
            self.store: dict[BlobCacheKey, tuple[str, datetime]] = {}

        def __call__(self) -> SessionContext:
            return SessionContext(Session(self.store))

        def begin(self) -> SessionContext:
            return SessionContext(Session(self.store))

    factory = SessionFactory()
    clock = [datetime(2026, 9, 25, tzinfo=UTC)]
    producer = SqlAlchemyBlobCache(factory, now=lambda: clock[0])  # type: ignore[arg-type]
    consumer = SqlAlchemyBlobCache(factory, now=lambda: clock[0])  # type: ignore[arg-type]

    asyncio.run(producer.put(KEY, "written-by-worker", ttl=timedelta(days=7)))

    assert asyncio.run(consumer.get(KEY)) == BlobCacheEntry(
        BlobCacheStatus.HIT, "written-by-worker"
    )
    clock[0] += timedelta(days=7)
    assert asyncio.run(consumer.get(KEY)) == BlobCacheEntry(BlobCacheStatus.EXPIRED, None)
