"""Read the immutable database-backed prompt context for one review run."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.repositories.infrastructure.models import RuleVersion
from app.modules.reviews.application.conventions import GeneratedConventions
from app.modules.reviews.application.execute_review import ReviewPromptInput
from app.modules.reviews.application.prompt_builder import (
    RepoConventions as PromptConventions,
)
from app.modules.reviews.application.prompt_builder import ReviewRule, parse_unified_diff
from app.modules.reviews.infrastructure.models import CodeChange, CodeChangeDiff, PromptVersion, Run


class SqlAlchemyReviewPromptRepository:
    """Projects the persisted run revision into the pure prompt-builder input."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_review_prompt_input(
        self, run_id: UUID, conventions: GeneratedConventions
    ) -> ReviewPromptInput | None:
        statement = (
            select(
                PromptVersion.content,
                RuleVersion.rules,
                Run.code_change_id,
                Run.head_sha,
            )
            .join(PromptVersion, PromptVersion.id == Run.prompt_version_id)
            .join(RuleVersion, RuleVersion.id == Run.rule_version_id)
            .join(CodeChange, CodeChange.id == Run.code_change_id)
            .where(Run.id == run_id)
        )
        async with self._session_factory() as session:
            row = (await session.execute(statement)).one_or_none()
            if row is None:
                return None
            system, stored_rules, code_change_id, head_sha = row
            snapshots = (
                await session.execute(
                    select(CodeChangeDiff.filename, CodeChangeDiff.patch)
                    .where(
                        CodeChangeDiff.code_change_id == code_change_id,
                        CodeChangeDiff.head_sha == head_sha,
                    )
                    .order_by(CodeChangeDiff.filename.asc())
                )
            ).all()
        changed = tuple(
            item
            for _, patch in snapshots
            if patch is not None
            for item in parse_unified_diff(patch)
        )
        omitted = tuple(filename for filename, patch in snapshots if patch is None)
        return ReviewPromptInput(
            system=system,
            rules=tuple(_to_rule(item) for item in stored_rules),
            agents_md=conventions.agents_md,
            conventions=PromptConventions(
                key_patterns=conventions.conventions.key_patterns,
                recommendations=conventions.conventions.recommendations,
            ),
            changed_files=changed,
            omitted_files=omitted,
        )


def _to_rule(value: dict[str, Any]) -> ReviewRule:
    """Convert the already validated onboarding value without accepting loose shapes."""
    name = value.get("name")
    include = value.get("include")
    exclude = value.get("exclude")
    checks = value.get("checks")
    if not (
        isinstance(name, str)
        and isinstance(include, list)
        and isinstance(exclude, list)
        and isinstance(checks, list)
        and all(isinstance(item, str) for item in include)
        and all(isinstance(item, str) for item in exclude)
        and all(isinstance(item, str) for item in checks)
    ):
        raise ValueError("stored rule has invalid prompt shape")
    return ReviewRule(
        name=name, include=tuple(include), exclude=tuple(exclude), checks=tuple(checks)
    )
