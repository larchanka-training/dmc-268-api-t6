"""Shared fakes for execute-review tests."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from app.modules.reviews.application.conventions import CachedConventions, GeneratedConventions
from app.modules.reviews.application.policy_snapshot import (
    POLICY_SNAPSHOT_SCHEMA_VERSION,
    ExecutionProfile,
    PolicySnapshot,
    PromptBindings,
)
from app.modules.reviews.application.prompt_builder import file_block_id
from tests.policy_snapshot_fakes import (
    make_complete_execution_profile,
    make_complete_prompt_bindings,
    make_policy_document_missing,
    make_policy_document_present,
)

RUN_ID = UUID("00000000-0000-0000-0000-000000000200")
MAIN_BLOCK_ID = file_block_id("app/main.py")


def conventions() -> GeneratedConventions:
    return GeneratedConventions(
        CachedConventions(
            repository_id=RUN_ID,
            agents_md_sha=None,
            prompt_version_id=RUN_ID,
            key_patterns=("Use services.",),
            recommendations=("Test paths.",),
            languages={},
        ),
        None,
        True,
    )


def _make_test_snapshot(
    *,
    is_ready: bool = True,
    max_input_tokens: int = 60_000,
    max_policy_tokens: int = 12_000,
) -> PolicySnapshot:
    from dataclasses import replace

    rules = make_policy_document_present(
        path=".review/rules.md",
        text="# Rules\nAlways sanitize input.\n",
        blob_oid="r" * 40,
    )
    agents = make_policy_document_missing("AGENTS.md")
    bindings = (
        make_complete_prompt_bindings()
        if is_ready
        else PromptBindings("a" * 64, "b" * 64, "c" * 64, review_system_id=None)
    )
    profile = (
        make_complete_execution_profile("fast", review_model="model-a", verifier_model="model-b")
        if is_ready
        else ExecutionProfile.fast("model-a", "model-b")
    )
    if is_ready:
        profile = replace(
            profile,
            max_input_tokens=max_input_tokens,
            max_policy_tokens=max_policy_tokens,
        )
    return PolicySnapshot(
        schema_version=POLICY_SNAPSHOT_SCHEMA_VERSION,
        snapshot_id=UUID("00000000-0000-0000-0000-000000000003"),
        run_id=UUID("00000000-0000-0000-0000-000000000002"),
        repository_id=UUID("00000000-0000-0000-0000-000000000001"),
        policy_base_sha="b" * 40,
        rules=rules,
        agents=agents,
        ignore_patterns=(),
        ignore_digest="0" * 64,
        parser_profile="rules-markdown/v1",
        parser_version="4.0.0",
        matcher_version="review-ignore/v1",
        limits_version="policy-limits/v1",
        builtin_exclusions_version="builtin-exclusions/v1",
        priority_matcher_version="priority-matcher/v1",
        service_defaults_rule_version_id=UUID("00000000-0000-0000-0000-000000000004"),
        defaults_checksum="282d61c2da897152046db742fbd9bfda061ba1c71ce32759d47e042c06b0ff52",
        prompt_bindings=bindings,
        execution_profile=profile,
        policy_digest="3" * 64,
    )


class FakeVerifierModel:
    def __init__(self, decisions_payload: Mapping[str, Any]) -> None:
        self.decisions_payload = decisions_payload
        self.call_count = 0

    async def verify(
        self, *, system_prompt: str, user_prompt: str, **_: object
    ) -> Mapping[str, Any]:
        self.call_count += 1
        cand_ids = re.findall(r'candidate candidate_id="([^"]+)"', user_prompt)
        payload = dict(self.decisions_payload)
        if "decisions" in payload and cand_ids:
            new_decisions = []
            for idx, d in enumerate(payload["decisions"]):
                d_copy = dict(d)
                if idx < len(cand_ids):
                    d_copy["candidate_id"] = cand_ids[idx]
                new_decisions.append(d_copy)
            payload["decisions"] = new_decisions
        return payload


class _AcceptingCheckpointPublisher:
    """Unit publisher fake for snapshot tests that exercise stages after checkpointing."""

    async def checkpoint_candidates(self, *args: object, **kwargs: object) -> bool:
        return True
