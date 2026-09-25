import asyncio
from uuid import UUID

from fastapi.testclient import TestClient

from app.main import app, get_run_repository
from app.modules.reviews.application.get_run_diff import (
    DiffSnapshot,
    StoreDiffSnapshot,
)
from app.modules.reviews.application.process_run import (
    ReviewRunProcessor,
    RunDiffInput,
    RunDiffProvider,
)


class FakeDiffRepository:
    def __init__(self, snapshots: list[DiffSnapshot] | None) -> None:
        self.snapshots = snapshots
        self.read_calls: list[UUID] = []
        self.write_calls: list[tuple[UUID, str, list[DiffSnapshot]]] = []

    async def get_run_diff(self, run_id: UUID) -> list[DiffSnapshot] | None:
        self.read_calls.append(run_id)
        return self.snapshots

    async def replace_diff_snapshots(
        self, code_change_id: UUID, head_sha: str, snapshots: list[DiffSnapshot]
    ) -> None:
        self.write_calls.append((code_change_id, head_sha, snapshots))


def test_get_run_diff_returns_saved_normal_binary_and_large_snapshots() -> None:
    run_id = UUID("00000000-0000-0000-0000-000000000100")
    repository = FakeDiffRepository(
        [
            DiffSnapshot(
                filename="app/service.py",
                patch=(
                    "diff --git a/app/service.py b/app/service.py\n"
                    "--- a/app/service.py\n"
                    "+++ b/app/service.py\n"
                    "@@ -1 +1 @@\n-old\n+new"
                ),
            ),
            DiffSnapshot(
                filename="logo.png",
                patch=(
                    "diff --git a/logo.png b/logo.png\n"
                    "Binary files a/logo.png and b/logo.png differ"
                ),
            ),
            DiffSnapshot(filename="generated.lock", patch=None),
        ]
    )
    app.dependency_overrides[get_run_repository] = lambda: repository
    try:
        response = TestClient(app).get(f"/api/runs/{run_id}/diff")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == [
        {
            "filename": "app/service.py",
            "patch": (
                "diff --git a/app/service.py b/app/service.py\n"
                "--- a/app/service.py\n"
                "+++ b/app/service.py\n"
                "@@ -1 +1 @@\n-old\n+new"
            ),
        },
        {
            "filename": "logo.png",
            "patch": (
                "diff --git a/logo.png b/logo.png\nBinary files a/logo.png and b/logo.png differ"
            ),
        },
        {"filename": "generated.lock", "patch": None},
    ]
    assert repository.read_calls == [run_id]


def test_get_run_diff_returns_404_for_missing_run_and_422_for_invalid_id() -> None:
    repository = FakeDiffRepository(None)
    app.dependency_overrides[get_run_repository] = lambda: repository
    try:
        client = TestClient(app)
        missing = client.get("/api/runs/00000000-0000-0000-0000-000000000100/diff")
        invalid = client.get("/api/runs/not-a-uuid/diff")
    finally:
        app.dependency_overrides.clear()

    assert missing.status_code == 404
    assert invalid.status_code == 422


def test_store_diff_snapshot_persists_filenames_only_when_total_diff_exceeds_limit() -> None:
    code_change_id = UUID("00000000-0000-0000-0000-000000000200")
    repository = FakeDiffRepository([])
    first_patch = "\n".join("+line" for _ in range(1501))
    second_patch = "\n".join("+line" for _ in range(1500))

    asyncio.run(
        StoreDiffSnapshot(repository).execute(
            code_change_id=code_change_id,
            head_sha="a" * 40,
            files=[
                DiffSnapshot(filename="app/service.py", patch=first_patch),
                DiffSnapshot(filename="logo.png", patch=None),
                DiffSnapshot(filename="generated.lock", patch=second_patch),
            ],
        )
    )

    assert repository.write_calls == [
        (
            code_change_id,
            "a" * 40,
            [
                DiffSnapshot(filename="app/service.py", patch=None),
                DiffSnapshot(filename="logo.png", patch=None),
                DiffSnapshot(filename="generated.lock", patch=None),
            ],
        )
    ]


def test_store_diff_snapshot_keeps_full_diff_at_aggregate_limit() -> None:
    code_change_id = UUID("00000000-0000-0000-0000-000000000201")
    repository = FakeDiffRepository([])
    first_patch = "\n".join("+line" for _ in range(1500))
    second_patch = "\n".join("+line" for _ in range(1500))

    asyncio.run(
        StoreDiffSnapshot(repository).execute(
            code_change_id=code_change_id,
            head_sha="c" * 40,
            files=[
                DiffSnapshot(filename="app/first.py", patch=first_patch),
                DiffSnapshot(filename="logo.png", patch=None),
                DiffSnapshot(filename="app/second.py", patch=second_patch),
            ],
        )
    )

    assert repository.write_calls == [
        (
            code_change_id,
            "c" * 40,
            [
                DiffSnapshot(
                    filename="app/first.py",
                    patch=(
                        "diff --git a/app/first.py b/app/first.py\n"
                        "--- a/app/first.py\n"
                        "+++ b/app/first.py\n"
                        f"{first_patch}"
                    ),
                ),
                DiffSnapshot(
                    filename="logo.png",
                    patch="diff --git a/logo.png b/logo.png\n"
                    "Binary files a/logo.png and b/logo.png differ",
                ),
                DiffSnapshot(
                    filename="app/second.py",
                    patch=(
                        "diff --git a/app/second.py b/app/second.py\n"
                        "--- a/app/second.py\n"
                        "+++ b/app/second.py\n"
                        f"{second_patch}"
                    ),
                ),
            ],
        )
    ]


def test_processed_run_persists_provider_diff_before_the_diff_api_reads_it() -> None:
    run_id = UUID("00000000-0000-0000-0000-000000000100")
    code_change_id = UUID("00000000-0000-0000-0000-000000000200")

    class Provider(RunDiffProvider):
        async def fetch_diff(self, *, code_change_id: UUID, head_sha: str) -> list[DiffSnapshot]:
            assert code_change_id == UUID("00000000-0000-0000-0000-000000000200")
            assert head_sha == "b" * 40
            return [DiffSnapshot(filename="app/main.py", patch="@@ -1 +1 @@\n-old\n+new")]

        async def fetch_file_content(
            self, *, code_change_id: UUID, head_sha: str, path: str
        ) -> str:
            raise AssertionError("the diff-only processor has no blob cache")

    class LifecycleRepository(FakeDiffRepository):
        async def get_run_diff_input(self, requested_run_id: UUID) -> RunDiffInput | None:
            if requested_run_id != run_id:
                return None
            return RunDiffInput(code_change_id=code_change_id, head_sha="b" * 40)

        async def replace_diff_snapshots(
            self, code_change_id: UUID, head_sha: str, snapshots: list[DiffSnapshot]
        ) -> None:
            await super().replace_diff_snapshots(code_change_id, head_sha, snapshots)
            self.snapshots = snapshots

    repository = LifecycleRepository([])

    asyncio.run(ReviewRunProcessor(repository, Provider()).execute(run_id))

    app.dependency_overrides[get_run_repository] = lambda: repository
    try:
        response = TestClient(app).get(f"/api/runs/{run_id}/diff")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == [
        {
            "filename": "app/main.py",
            "patch": (
                "diff --git a/app/main.py b/app/main.py\n"
                "--- a/app/main.py\n"
                "+++ b/app/main.py\n"
                "@@ -1 +1 @@\n-old\n+new"
            ),
        }
    ]
