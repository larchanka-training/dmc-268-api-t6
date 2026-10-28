"""The ORM keeps Bitbucket webhook provenance as strict as the database."""

from __future__ import annotations

from typing import cast

from sqlalchemy import CheckConstraint, Table

from app.modules.integrations.webhooks.infrastructure.models import WebhookEvent


def test_bitbucket_webhook_origin_check_requires_both_generations() -> None:
    table = cast(Table, WebhookEvent.__table__)
    constraint = next(
        constraint
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
        and constraint.name == "ck_webhook_events_provider_origin"
    )
    expression = " ".join(str(constraint.sqltext).split())
    bitbucket_clause = expression.split("(provider = 'bitbucket'", 1)[1]

    assert "gitlab_connection_id IS NULL" in bitbucket_clause
    assert "provider_installation_id IS NOT NULL" in bitbucket_clause
    assert "bitbucket_repository_id IS NOT NULL" in bitbucket_clause
    assert "bitbucket_binding_generation IS NOT NULL" in bitbucket_clause
    assert "bitbucket_webhook_generation IS NOT NULL" in bitbucket_clause
    assert "bitbucket_binding_generation > 0" in bitbucket_clause
    assert "bitbucket_webhook_generation > 0" in bitbucket_clause
