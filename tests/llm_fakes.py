"""Explicit checkpoints for standalone LLM tests without persisted Runs."""

from __future__ import annotations

from app.modules.reviews.application.llm import RunCallContext


async def context_checkpoint(context: RunCallContext) -> None:
    """Trust the standalone test context; production uses its persisted Run."""
