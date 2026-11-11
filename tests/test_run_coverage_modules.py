"""Structural contract for splitting run_coverage by responsibility (#392 Part 2)."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path
from uuid import UUID

import pytest

from app.modules.reviews.application import run_coverage
from app.modules.reviews.application.coverage_model import RunCoverage
from app.modules.reviews.application.diff_map import DiffMap, VcsProviderName, build_diff_map
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.prompt_builder import PullRequestMeta
from app.modules.reviews.application.vcs_diff import PullRequestLocator, VcsPullRequest

_APP_REVIEWS = Path(__file__).resolve().parents[1] / "app" / "modules" / "reviews" / "application"
_MODULES = (
    "run_coverage",
    "coverage_model",
    "coverage_projection",
    "coverage_finalization",
    "coverage_restoration",
)
_PATCH = "@@ -1 +1 @@\n-old\n+new"


def _sample_inputs() -> tuple[list[DiffSnapshot], DiffMap]:
    snapshot = DiffSnapshot(
        "src/app.py", _PATCH, "d" * 40, additions=1, deletions=1, changes=2, review_patch=_PATCH
    )
    snapshots = [snapshot]
    pr = VcsPullRequest(
        PullRequestLocator(17, "octo/repo", 7),
        "a" * 40,
        "b" * 40,
        PullRequestMeta("PR", None, "octo", "feature", "main", (), 1, 1, 1, False, False),
    )
    diff_map = build_diff_map(
        pr,
        (run_coverage.snapshot_vcs_file(snapshot),),
        provider=VcsProviderName.GITHUB,
        repository_id=UUID(int=1),
        diff_old_sha="c" * 40,
    )
    return snapshots, diff_map


def test_run_coverage_modules_stay_under_300_lines() -> None:
    for name in _MODULES:
        path = _APP_REVIEWS / f"{name}.py"
        assert path.is_file(), f"Missing split module: {path}"
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) < 300, f"{name}.py has {len(lines)} lines (>= 300)"


def test_coverage_modules_are_strictly_acyclic_and_do_not_import_facade() -> None:
    implementation_modules = (
        "coverage_model",
        "coverage_schema",
        "coverage_projection",
        "coverage_finalization",
        "coverage_restoration",
    )
    all_split_modules = implementation_modules + ("run_coverage",)
    graph: dict[str, set[str]] = {name: set() for name in all_split_modules}

    for name in all_split_modules:
        path = _APP_REVIEWS / f"{name}.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            imported: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module.split(".")[-1])
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imported.append(alias.name.split(".")[-1])

            for target in imported:
                if target in all_split_modules and target != name:
                    graph[name].add(target)

    for impl_name in implementation_modules:
        assert "run_coverage" not in graph[impl_name], (
            f"{impl_name}.py violates architectural boundaries by importing run_coverage facade"
        )

    visited: dict[str, int] = {}

    def dfs(node: str, path: list[str]) -> None:
        visited[node] = 0
        for neighbor in graph[node]:
            if visited.get(neighbor) == 0:
                cycle = " -> ".join(path + [neighbor])
                pytest.fail(f"Cyclic dependency detected: {cycle}")
            elif neighbor not in visited:
                dfs(neighbor, path + [neighbor])
        visited[node] = 1

    for mod in all_split_modules:
        if mod not in visited:
            dfs(mod, [mod])


def test_run_coverage_reexports_all_public_symbols_from_dedicated_modules() -> None:
    facade = importlib.import_module("app.modules.reviews.application.run_coverage")
    model = importlib.import_module("app.modules.reviews.application.coverage_model")
    projection = importlib.import_module("app.modules.reviews.application.coverage_projection")
    finalization = importlib.import_module("app.modules.reviews.application.coverage_finalization")
    restoration = importlib.import_module("app.modules.reviews.application.coverage_restoration")
    schema = importlib.import_module("app.modules.reviews.application.coverage_schema")

    assert facade.RunCoverage is model.RunCoverage
    assert facade.finalize_run_coverage is finalization.finalize_run_coverage
    assert (
        facade.coverage_published_for_summary_only
        is finalization.coverage_published_for_summary_only
    )
    assert facade.snapshot_coverage_digest is projection.snapshot_coverage_digest
    assert facade.snapshot_vcs_file is projection.snapshot_vcs_file
    assert facade.CoverageContextPins is restoration.CoverageContextPins
    assert facade.read_context_coverage_pins is restoration.read_context_coverage_pins
    for name in (
        "COVERAGE_EXCLUSION_CODES",
        "COVERAGE_REASON_CODES",
        "CoverageItem",
        "CoverageStatus",
        "read_coverage_counts",
        "read_coverage_items",
    ):
        assert getattr(facade, name) is getattr(schema, name)
    assert not hasattr(projection, "project_run_coverage")
    assert callable(projection.project_run_coverage_with_digest)
    for name in ("restore_context_coverage", "restore_run_coverage", "unavailable_run_coverage"):
        assert not hasattr(restoration, name)
        assert callable(getattr(restoration, f"{name}_with_digest"))


@pytest.mark.parametrize(
    "entrypoint",
    [
        "project_run_coverage",
        "restore_context_coverage",
        "restore_run_coverage",
        "unavailable_run_coverage",
    ],
)
def test_facade_wrappers_delegate_snapshot_digest_to_owner_modules(
    monkeypatch: pytest.MonkeyPatch, entrypoint: str
) -> None:
    snapshots, diff_map = _sample_inputs()
    sentinel = f"sentinel-digest-{entrypoint}"
    calls: list[list[DiffSnapshot]] = []

    def fake_digest(items: list[DiffSnapshot]) -> str:
        calls.append(items)
        return sentinel

    monkeypatch.setattr(run_coverage, "snapshot_coverage_digest", fake_digest)
    saved_payload = run_coverage.project_run_coverage(snapshots, diff_map).to_json()
    calls.clear()

    result: RunCoverage | None
    if entrypoint == "project_run_coverage":
        result = run_coverage.project_run_coverage(snapshots, diff_map)
    elif entrypoint == "restore_context_coverage":
        request = {"head_sha": diff_map.head_sha}
        response = {
            "diff": {"diff_old_sha": diff_map.diff_old_sha, "digest": diff_map.digest},
            "coverage": saved_payload,
        }
        result = run_coverage.restore_context_coverage(
            request, response, snapshots=snapshots, diff_map=diff_map
        )
    elif entrypoint == "restore_run_coverage":
        result = run_coverage.restore_run_coverage(
            saved_payload, snapshots=snapshots, diff_map=diff_map
        )
    else:
        result = run_coverage.unavailable_run_coverage(snapshots, diff_map.head_sha)

    assert calls == [snapshots]
    assert result is not None
    assert result.snapshot_digest == sentinel
