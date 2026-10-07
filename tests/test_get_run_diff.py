import asyncio
from uuid import UUID

from app.main import app, get_run_repository
from app.modules.reviews.application.conventions import (
    ActiveConventionsPrompt,
    CachedConventions,
    GeneratedConventions,
)
from app.modules.reviews.application.get_run_diff import (
    DiffSnapshot,
    GetRunDiff,
    StoreDiffSnapshot,
    review_files_from_snapshots,
)
from app.modules.reviews.application.process_run import (
    ReviewRunProcessor,
    RunConventionsInput,
    RunDiffInput,
    RunDiffProvider,
)
from app.modules.reviews.application.prompt_builder import DiffLine, ReviewRule
from tests.portal_test_client import authenticated_test_client as TestClient
from tests.trigger_uow import processing_uow


class FakeDiffRepository:
    def __init__(self, snapshots: list[DiffSnapshot] | None) -> None:
        self.snapshots = snapshots
        self.read_calls: list[UUID] = []
        self.write_calls: list[tuple[UUID, str, list[DiffSnapshot]]] = []

    async def get_run_diff(self, run_id: UUID) -> list[DiffSnapshot] | None:
        self.read_calls.append(run_id)
        return self.snapshots

    async def store_diff_snapshots(
        self, run_id: UUID, code_change_id: UUID, head_sha: str, snapshots: list[DiffSnapshot]
    ) -> list[DiffSnapshot]:
        self.write_calls.append((code_change_id, head_sha, snapshots))
        return snapshots


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


def test_store_diff_snapshot_retains_ui_patches_when_model_summary_limit_is_exceeded() -> None:
    code_change_id = UUID("00000000-0000-0000-0000-000000000200")
    repository = FakeDiffRepository([])
    first_patch = "\n".join("+line" for _ in range(1501))
    second_patch = "\n".join("+line" for _ in range(1500))

    asyncio.run(
        StoreDiffSnapshot(repository).execute(
            run_id=UUID("00000000-0000-0000-0000-000000000100"),
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
                DiffSnapshot(
                    filename="app/service.py",
                    patch=(
                        "diff --git a/app/service.py b/app/service.py\n"
                        "--- a/app/service.py\n+++ b/app/service.py\n"
                        f"{first_patch}"
                    ),
                    summary_only=True,
                ),
                DiffSnapshot(filename="logo.png", patch=None, summary_only=True),
                DiffSnapshot(
                    filename="generated.lock",
                    patch=(
                        "diff --git a/generated.lock b/generated.lock\n"
                        "--- a/generated.lock\n+++ b/generated.lock\n"
                        f"{second_patch}"
                    ),
                    summary_only=True,
                ),
            ],
        )
    ]
    changed, omitted = review_files_from_snapshots(repository.write_calls[0][2])
    assert changed == ()
    assert omitted == ("app/service.py", "logo.png", "generated.lock")


def test_store_diff_snapshot_keeps_full_diff_at_aggregate_limit() -> None:
    code_change_id = UUID("00000000-0000-0000-0000-000000000201")
    repository = FakeDiffRepository([])
    first_patch = "\n".join("+line" for _ in range(1500))
    second_patch = "\n".join("+line" for _ in range(1500))

    asyncio.run(
        StoreDiffSnapshot(repository).execute(
            run_id=UUID("00000000-0000-0000-0000-000000000100"),
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
                    review_patch=first_patch,
                ),
                DiffSnapshot(filename="logo.png", patch=None),
                DiffSnapshot(
                    filename="app/second.py",
                    patch=(
                        "diff --git a/app/second.py b/app/second.py\n"
                        "--- a/app/second.py\n"
                        "+++ b/app/second.py\n"
                        f"{second_patch}"
                    ),
                    review_patch=second_patch,
                ),
            ],
        )
    ]


def test_store_snapshot_excludes_generated_lines_from_summary_limit_and_preserves_metadata() -> (
    None
):
    repository = FakeDiffRepository([])
    code_change_id = UUID("00000000-0000-0000-0000-000000000201")
    source = DiffSnapshot(
        filename="src/new.py",
        patch="@@ -1 +1 @@\n-old\n+new",
        blob_sha="a" * 40,
        status="renamed",
        previous_filename="src/old.py",
        additions=1,
        deletions=1,
        changes=2,
    )
    generated = DiffSnapshot(
        filename="package-lock.json",
        patch="@@ -0,0 +1,3001 @@\n" + "+x\n" * 3001,
        blob_sha="b" * 40,
        status="modified",
        omission_reason="generated",
    )
    missing = DiffSnapshot(filename="assets/logo.png", patch=None, blob_sha="c" * 40)

    asyncio.run(
        StoreDiffSnapshot(repository).execute(
            run_id=UUID("00000000-0000-0000-0000-000000000100"),
            code_change_id=code_change_id,
            head_sha="d" * 40,
            files=[source, generated, missing],
        )
    )

    saved = repository.write_calls[0][2]
    assert [item.filename for item in saved] == [
        "src/new.py",
        "package-lock.json",
        "assets/logo.png",
    ]
    assert saved[0].blob_sha == "a" * 40
    assert saved[0].status == "renamed"
    assert saved[0].previous_filename == "src/old.py"
    assert saved[0].patch is not None and "@@ -1 +1 @@" in saved[0].patch
    assert saved[1].omission_reason == "generated"
    assert saved[1].patch is not None
    assert saved[2].patch is None


def test_run_snapshots_do_not_leak_to_or_rewrite_another_run_at_the_same_head() -> None:
    first_run = UUID("00000000-0000-0000-0000-000000000301")
    second_run = UUID("00000000-0000-0000-0000-000000000302")
    code_change_id = UUID("00000000-0000-0000-0000-000000000303")

    class Repository:
        snapshots: dict[UUID, list[DiffSnapshot]] = {}

        async def store_diff_snapshots(
            self,
            run_id: UUID,
            code_change_id: UUID,
            head_sha: str,
            snapshots: list[DiffSnapshot],
        ) -> list[DiffSnapshot]:
            return self.snapshots.setdefault(run_id, snapshots)

        async def get_run_diff(self, run_id: UUID) -> list[DiffSnapshot]:
            return self.snapshots.get(run_id, [])

    repository = Repository()
    store = StoreDiffSnapshot(repository)
    assert asyncio.run(GetRunDiff(repository).execute(second_run)) == []
    first = asyncio.run(
        store.execute(
            run_id=first_run,
            code_change_id=code_change_id,
            head_sha="a" * 40,
            files=[DiffSnapshot("src/a.py", "@@ -1 +1 @@\n-old\n+first")],
        )
    )
    second = asyncio.run(
        store.execute(
            run_id=second_run,
            code_change_id=code_change_id,
            head_sha="a" * 40,
            files=[DiffSnapshot("src/a.py", "@@ -1 +1 @@\n-old\n+second")],
        )
    )
    retried = asyncio.run(
        store.execute(
            run_id=first_run,
            code_change_id=code_change_id,
            head_sha="a" * 40,
            files=[DiffSnapshot("src/a.py", "@@ -1 +1 @@\n-old\n+later")],
        )
    )
    assert first[0].patch is not None and "first" in first[0].patch
    assert second[0].patch is not None and "second" in second[0].patch
    assert retried == first
    assert asyncio.run(GetRunDiff(repository).execute(first_run)) == first


def test_full_provider_diff_keeps_rename_status_in_model_projection() -> None:
    repository = FakeDiffRepository([])
    full_patch = (
        "diff --git a/src/old.py b/src/new.py\nsimilarity index 50%\n@@ -1 +1 @@\n-old\n+new"
    )
    asyncio.run(
        StoreDiffSnapshot(repository).execute(
            run_id=UUID("00000000-0000-0000-0000-000000000100"),
            code_change_id=UUID("00000000-0000-0000-0000-000000000201"),
            head_sha="a" * 40,
            files=[DiffSnapshot("src/new.py", full_patch, blob_sha="b" * 40, status="renamed")],
        )
    )
    saved = repository.write_calls[0][2]
    changed, omitted = review_files_from_snapshots(saved)
    assert saved[0].patch == full_patch
    assert changed[0].status == "renamed"
    assert changed[0].lines == (
        DiffLine(1, "removed", "old"),
        DiffLine(1, "added", "new"),
    )
    assert omitted == ()


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

        async def store_diff_snapshots(
            self, run_id: UUID, code_change_id: UUID, head_sha: str, snapshots: list[DiffSnapshot]
        ) -> list[DiffSnapshot]:
            await super().store_diff_snapshots(run_id, code_change_id, head_sha, snapshots)
            self.snapshots = snapshots
            return snapshots

    repository = LifecycleRepository([])

    asyncio.run(
        ReviewRunProcessor(repository, Provider(), uow_factory=processing_uow(repository)).execute(
            run_id
        )
    )

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


def test_processor_passes_active_conventions_prompt_not_the_run_system_prompt() -> None:
    run_id = UUID("00000000-0000-0000-0000-000000000110")
    expected_repository_id = UUID("00000000-0000-0000-0000-000000000111")
    system_prompt_id = UUID("00000000-0000-0000-0000-000000000112")
    active_prompt = ActiveConventionsPrompt(
        UUID("00000000-0000-0000-0000-000000000113"), "stored conventions v2"
    )

    class Provider(RunDiffProvider):
        async def fetch_diff(self, *, code_change_id: UUID, head_sha: str) -> list[DiffSnapshot]:
            return []

        async def fetch_file_content(
            self, *, code_change_id: UUID, head_sha: str, path: str
        ) -> str:
            raise AssertionError("no blob cache was configured")

    class Repository(FakeDiffRepository):
        async def get_run_diff_input(self, requested_run_id: UUID) -> RunDiffInput | None:
            assert requested_run_id == run_id
            return RunDiffInput(code_change_id=expected_repository_id, head_sha="a" * 40)

        async def get_run_conventions_input(
            self, requested_run_id: UUID
        ) -> RunConventionsInput | None:
            assert requested_run_id == run_id
            return RunConventionsInput(expected_repository_id, active_prompt)

    class Conventions:
        async def execute(
            self,
            *,
            repository_id: UUID,
            conventions_prompt: ActiveConventionsPrompt,
            run_id: UUID,
            changed_files: tuple[str, ...],
            rules: tuple[ReviewRule, ...],
        ) -> GeneratedConventions:
            assert repository_id == expected_repository_id
            assert conventions_prompt == active_prompt
            assert conventions_prompt.id != system_prompt_id
            assert changed_files == ()
            assert rules == ()
            return GeneratedConventions(
                CachedConventions(
                    expected_repository_id,
                    None,
                    conventions_prompt.id,
                    ("One.", "Two.", "Three."),
                    ("One.", "Two.", "Three.", "Four.", "Five."),
                    {},
                ),
                None,
                False,
            )

    empty_repository = Repository([])
    result = asyncio.run(
        ReviewRunProcessor(
            empty_repository,
            Provider(),
            conventions=Conventions(),  # type: ignore[arg-type]
            uow_factory=processing_uow(empty_repository),
        ).prepare(run_id)
    )

    assert isinstance(result, GeneratedConventions)


def test_summary_only_run_passes_no_file_paths_to_conventions() -> None:
    run_id = UUID("00000000-0000-0000-0000-000000000110")
    repository_id = UUID("00000000-0000-0000-0000-000000000111")
    prompt = ActiveConventionsPrompt(run_id, "prompt")

    class Provider:
        async def fetch_diff(self, *, code_change_id: UUID, head_sha: str) -> list[DiffSnapshot]:
            return [DiffSnapshot("src/large.py", "+line\n" * 3001)]

        async def fetch_file_content(
            self, *, code_change_id: UUID, head_sha: str, path: str
        ) -> str:
            raise AssertionError("no blob cache")

    class Repository(FakeDiffRepository):
        async def get_run_diff_input(self, requested_run_id: UUID) -> RunDiffInput:
            return RunDiffInput(repository_id, "a" * 40)

        async def get_run_conventions_input(self, requested_run_id: UUID) -> RunConventionsInput:
            return RunConventionsInput(repository_id, prompt)

    class Conventions:
        async def execute(
            self,
            *,
            repository_id: UUID,
            conventions_prompt: ActiveConventionsPrompt,
            run_id: UUID,
            changed_files: tuple[str, ...],
            rules: tuple[ReviewRule, ...],
        ) -> GeneratedConventions:
            assert changed_files == ()
            return GeneratedConventions(
                CachedConventions(repository_id, None, prompt.id, (), (), {}), None, False
            )

    repository = Repository([])
    result = asyncio.run(
        ReviewRunProcessor(
            repository,
            Provider(),
            conventions=Conventions(),  # type: ignore[arg-type]
            uow_factory=processing_uow(repository),
        ).prepare(run_id)
    )
    assert isinstance(result, GeneratedConventions)
    assert repository.write_calls[0][2][0].summary_only is True
    assert repository.write_calls[0][2][0].patch is not None
