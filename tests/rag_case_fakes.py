"""Shared fixture record of the RAG case-validator and RAG eval tests."""

from __future__ import annotations

from typing import Any


def _rag_metadata(kind: str = "reverse_edge", case_id: str = "RAG-01") -> dict[str, Any]:
    """The ``spike`` fixture: ``save`` of ``pkg/store.py`` gets a required parameter (line 6)."""
    rag: dict[str, Any] = {
        "kind": kind,
        "expected_retrieved": ["pkg/api.py"],
        "absent_from_context": [],
    }
    findings = [
        {
            "path": "pkg/store.py",
            "start_line": None,
            "line": 6,
            "severity": "high",
            "category": "correctness",
        }
    ]
    verdict = "blocking"
    if kind in ("reverse_edge", "injection"):
        rag["defect_form"] = "new_required_param"
    if kind == "clean":
        findings, verdict = [], "clean"
    return {
        "id": case_id,
        "class": "rag",
        "language": "python",
        "source": {"kind": "synthetic", "attribution": "QA team"},
        "base_path": "base",
        "patch_path": "diff.patch",
        "expected_verdict": verdict,
        "expected_findings": findings,
        "rag": rag,
    }
