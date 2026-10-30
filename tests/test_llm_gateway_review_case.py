"""The prompt envelope ``review_case`` sends for the eval cases that predate the #146 RAG arms.

The digests below were taken from the code before ``ReviewCase`` got its optional
``context_blocks`` / ``retrieved`` fields: a case that does not set them must render the
same bytes, or the recorded 2026-10-28 v5 baseline silently becomes a different input (#82).
The inputs are a main case (SEC-01), a rules case (RUL-04) and the pinned v5 prompt; a deliberate
change of one of them regenerates its digest. Taken at f4ec08b6, before the ReviewCase change.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.bootstrap.llm_gateway import ReviewCase
from app.modules.reviews.application.prompt_builder import PullRequestMeta
from tests.test_eval_live import (
    REPO_ROOT,
    VALID,
    FakeTransport,
    answer,
    fixture_root,
    record,
    record_arm,
    rules_fixture_root,
)

_SYSTEM_V5 = REPO_ROOT / "review" / "prompts" / "review.system.v5.md"


def _envelope_digest(transport: FakeTransport) -> str:
    """SHA-256 of everything a request carries except the profile (a test double)."""
    (request,) = transport.requests
    envelope = {
        "messages": [[item.role, item.content] for item in request.messages],
        "response_schema": repr(request.response_schema),
        "timeout_s": request.timeout_s,
    }
    raw = json.dumps(envelope, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def test_the_main_case_envelope_is_byte_identical(tmp_path: Path) -> None:
    transport = FakeTransport([answer("primary-model", VALID)])

    record(fixture_root(tmp_path, ("SEC-01",)), transport)

    assert (
        _envelope_digest(transport)
        == "7da54783516465fb555aa89c4f16c8ca1f7cfd9b1d064241a5c95d0aced9e0e5"
    )


def test_the_rules_case_envelope_is_byte_identical(tmp_path: Path) -> None:
    transport = FakeTransport([answer("primary-model", VALID)])

    record_arm(rules_fixture_root(tmp_path, ("RUL-04",)), transport, "valid")

    assert (
        _envelope_digest(transport)
        == "d061a55b2a336150d3e65f5a3f6d3e12eefb4ba523ea029f280dc518b8c20d41"
    )


def test_the_v5_rules_case_envelope_with_its_empty_retrieved_section_is_byte_identical(
    tmp_path: Path,
) -> None:
    transport = FakeTransport([answer("primary-model", VALID)])

    record_arm(
        rules_fixture_root(tmp_path, ("RUL-04",)), transport, "valid", system_prompt=_SYSTEM_V5
    )

    (request,) = transport.requests
    assert "<retrieved_excerpts>" in request.messages[1].content
    assert (
        _envelope_digest(transport)
        == "0c2d1e8786e5de7990373d6ce2e9de8bfef1d601cb724c5fc748697a00a51bde"
    )


def test_the_v5_baseline_envelope_of_a_main_case_without_policy_files_is_byte_identical(
    tmp_path: Path,
) -> None:
    """The input of the 2026-10-28 baseline: a main case, v5, policy arm ``none``."""
    transport = FakeTransport([answer("primary-model", VALID)])

    record_arm(fixture_root(tmp_path, ("SEC-01",)), transport, "none", system_prompt=_SYSTEM_V5)

    assert (
        _envelope_digest(transport)
        == "c65edc4fc3eb6768101e4f777b46ab3d795c99da6223abb08326e08b7c160d3e"
    )


def test_a_case_without_excerpts_keeps_the_defaults_of_the_envelope() -> None:
    case = ReviewCase(diff="", system="", pr_meta=_meta())

    assert (case.context_blocks, case.retrieved) == ((), ())


def _meta() -> PullRequestMeta:
    return PullRequestMeta(
        title="t",
        description=None,
        author="a",
        source_branch="s",
        target_branch="m",
        labels=(),
        files_changed=0,
        lines_added=0,
        lines_removed=0,
        is_draft=False,
        is_fork=False,
    )
