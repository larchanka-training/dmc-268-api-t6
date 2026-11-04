"""What the contract says a null `RawFileDiff.patch` means (#222)."""

from __future__ import annotations

from pathlib import Path

from openapi_spec_validator.readers import read_from_filename

SPEC_PATH = Path(__file__).parents[1] / "contracts" / "openapi.yaml"


def test_a_null_patch_does_not_signal_a_summary_only_run() -> None:
    """A summary-only run keeps its UI patches, so `patch` cannot tell the mode."""
    spec, _ = read_from_filename(str(SPEC_PATH))
    components = spec["components"]["schemas"]

    patch = components["RawFileDiff"]["properties"]["patch"]["description"]
    assert "null in a summary-only run" not in patch
    assert "no provider patch is available" in patch
    assert "read `summaryOnly` on the run" in patch

    for schema in ("RunSession", "RunDetail"):
        summary_only = components[schema]["properties"]["summaryOnly"]["description"]
        assert "`patch: null`" not in summary_only
        assert "each file's available patch" in summary_only
