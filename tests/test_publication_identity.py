"""Immutable publication payload equality is independent of live credential availability."""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest

from app.modules.reviews.application.process_run import RunVcsInput
from app.modules.reviews.application.publish_run_review import (
    PublicationIdentity,
    PublishContext,
    publication_matches,
)
from app.modules.reviews.application.review_output import PublishedFinding
from app.modules.reviews.application.vcs_diff import PullRequestLocator


def context() -> PublishContext:
    return PublishContext(
        run_id=UUID(int=1),
        state="publishing",
        cancel_requested=False,
        head_sha="a" * 40,
        pr_head_sha="a" * 40,
        pr_open=True,
        origin_active=False,
        installation_id=None,
        repository_full_name="group/project",
        pr_number=12,
        review_body="review",
        findings=(),
        findings_hash="hash",
        provider="gitlab",
        project_id=17,
        publication_identity=PublicationIdentity(
            UUID(int=2), UUID(int=3), 71, "gitlab", UUID(int=4), UUID(int=5), "b" * 40
        ),
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", UUID(int=99)),
        ("head_sha", "c" * 40),
        ("findings_hash", "different"),
        ("review_body", "changed"),
        (
            "findings",
            (
                PublishedFinding(
                    "a.py", 1, None, "high", "correctness", "Bug", "Fix", None, 0.9, None
                ),
            ),
        ),
        ("provider", "github"),
        ("repository_full_name", "group/other"),
        ("pr_number", 13),
        ("project_id", 18),
    ],
)
def test_each_immutable_payload_pin_rejects_publication(field: str, value: Any) -> None:
    expected = context()
    assert publication_matches(expected, expected)
    assert not publication_matches(expected, replace(expected, **{field: value}))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("origin_provider_installation_id", UUID(int=99)),
        ("origin_workspace_id", UUID(int=99)),
        ("origin_installation_external_id", 72),
        ("origin_provider", "github"),
        ("repository_id", UUID(int=99)),
        ("code_change_id", UUID(int=99)),
        ("base_sha", "c" * 40),
    ],
)
def test_each_raw_publication_identity_pin_rejects_publication(field: str, value: Any) -> None:
    expected = context()
    assert expected.publication_identity is not None
    changed = replace(expected.publication_identity, **{field: value})
    assert not publication_matches(expected, replace(expected, publication_identity=changed))


def test_credential_availability_is_outside_immutable_payload_equality() -> None:
    inactive = context()
    active = replace(
        inactive,
        origin_active=True,
        installation_id=71,
        workspace_id=UUID(int=3),
        vcs_input=RunVcsInput(
            UUID(int=5),
            UUID(int=4),
            "a" * 40,
            "b" * 40,
            PullRequestLocator(71, "group/project", 12, "gitlab", UUID(int=2), 17),
        ),
    )
    assert publication_matches(active, inactive)
    assert publication_matches(inactive, active)
    assert not publication_matches(active, replace(active, installation_id=72))
    assert not publication_matches(inactive, replace(inactive, workspace_id=UUID(int=3)))
