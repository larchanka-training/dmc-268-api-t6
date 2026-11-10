"""Structural contract for splitting run_coverage by responsibility (#392 Part 2)."""

from __future__ import annotations

import importlib
from pathlib import Path

_APP_REVIEWS = Path(__file__).resolve().parents[1] / "app" / "modules" / "reviews" / "application"
_MODULES = (
    "run_coverage",
    "coverage_model",
    "coverage_projection",
    "coverage_finalization",
    "coverage_restoration",
)


def test_run_coverage_modules_stay_under_300_lines() -> None:
    for name in _MODULES:
        path = _APP_REVIEWS / f"{name}.py"
        assert path.is_file(), f"Missing split module: {path}"
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) < 300, f"{name}.py has {len(lines)} lines (>= 300)"


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
    for name in (
        "project_run_coverage",
        "restore_context_coverage",
        "restore_run_coverage",
        "unavailable_run_coverage",
    ):
        assert callable(getattr(facade, name))
    assert callable(projection.project_run_coverage)
    assert callable(restoration.restore_context_coverage)
    assert callable(restoration.restore_run_coverage)
    assert callable(restoration.unavailable_run_coverage)
