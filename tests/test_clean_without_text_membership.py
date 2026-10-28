"""Compact diagnostics never stand in for durable full-patch membership."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest

from app.modules.reviews.application.execute_review import (
    ExecuteReviewRun,
    ReviewPromptInput,
    ReviewReply,
)
from app.modules.reviews.application.get_run_diff import DiffSnapshot, review_files_from_snapshots
from app.modules.reviews.application.post_processing_diff_map import diff_map_from_snapshots
from app.modules.reviews.application.prompt_builder import (
    PullRequestMeta,
    RepoConventions,
    ReviewContext,
    ReviewRule,
)
from app.modules.reviews.application.reflection_pass import ReflectionPass
from app.modules.reviews.application.review_ignore import (
    IgnoreMatcher,
    apply_ignore,
    parse_review_ignore,
)
from app.modules.reviews.application.review_output import FindingPostProcessingInput
from app.modules.reviews.application.run_coverage import project_run_coverage, restore_run_coverage
from app.modules.reviews.application.saved_review_result import read_saved_review_result
from app.modules.reviews.application.store_review_output import StoreReviewOutput
from app.modules.reviews.infrastructure.llm.models import GatewayReviewModel
from tests.test_clean_without_text_result import Queue, Repository, Uow, answer
from tests.test_execute_review import FakeVerifierModel, _make_test_snapshot, conventions
from tests.test_gateway_review_model import RUN, Transport, _gateway


@pytest.mark.parametrize("fully_reviewable", [False, True])
@pytest.mark.parametrize("with_ignored", [False, True])
def test_restore_bounded_classification_then_actual_fit_reduces_only_full_allowed_patches(
    fully_reviewable: bool,
    with_ignored: bool,
) -> None:
    small = "@@ -0,0 +1 @@\n+new"
    long = "@@ -0,0 +1,2 @@\n+new\n+" + "x" * 50_000
    valid = "@@ -0,0 +1,2 @@\n+first\n+second"
    snapshots = [
        DiffSnapshot(f"aaa{i:02}.py", small, status="added", additions=2, changes=2)
        for i in range(20)
    ]
    snapshots += [
        DiffSnapshot(
            "aaa20.py",
            long,
            status="added",
            additions=2 if fully_reviewable else 3,
            changes=2 if fully_reviewable else 3,
            review_patch=long,
        ),
        DiffSnapshot("zzz.py", valid, status="added", additions=2, changes=2, review_patch=valid),
    ]
    if with_ignored:
        snapshots.append(
            DiffSnapshot(
                "private/hidden.py",
                valid,
                status="added",
                additions=2,
                changes=2,
                review_patch=valid,
            )
        )
    diff = diff_map_from_snapshots(
        repository_id=UUID(int=1),
        code_change_number=7,
        head_sha="a" * 40,
        base_sha="b" * 40,
        diff_old_sha="c" * 40,
        snapshots=snapshots,
    )
    policy = parse_review_ignore("```review-ignore\nprivate/**\n```\n")
    ignore = apply_ignore(IgnoreMatcher(policy), diff)
    serialized = project_run_coverage(snapshots, diff, ignore=ignore).to_json()
    assert len(serialized["reasons"]) == 20
    assert "aaa20.py" not in str(serialized["reasons"])
    assert "private/hidden.py" not in str(serialized)
    restored = restore_run_coverage(serialized, snapshots=snapshots, diff_map=diff)
    assert restored is not None

    class Results(Repository):
        async def get_post_processing_input(self, run_id: UUID) -> FindingPostProcessingInput:
            return FindingPostProcessingInput(
                diff,
                frozenset(item.filename for item in snapshots if item.review_patch is not None),
                frozenset(),
                coverage=self.coverage,
            )

    repository = Results(restored)
    committed: list[str] = []
    queue = Queue(committed)
    store = StoreReviewOutput(
        lambda: Uow(repository, committed), queue, worker_id="worker", run_url=lambda _: None
    )
    changed, omitted = review_files_from_snapshots(snapshots)

    class Processor:
        async def prepare(self, run_id: UUID) -> Any:
            return conventions()

    class Prompts:
        async def get_review_prompt_input(self, run_id: UUID, saved: Any) -> ReviewPromptInput:
            snapshot = replace(
                _make_test_snapshot(max_input_tokens=12_000),
                ignore_patterns=policy.patterns,
                ignore_digest=policy.digest,
            )
            return ReviewPromptInput(
                "Review carefully.",
                (
                    ReviewRule(
                        "Meaningful Comments for Complex Logic", ("zzz.py",), (), ("Check.",) * 3
                    ),
                ),
                None,
                RepoConventions((), ()),
                changed,
                omitted,
                policy_snapshot=snapshot,
            )

    class Filter:
        diff_old_sha = "c" * 40
        head_sha = "a" * 40
        snapshot_for_llm = None
        payload = None
        ignored_file_paths: frozenset[str] = frozenset()

        def excluded(self, path: str) -> bool:
            return path in ignore.denied_paths

    class Model:
        async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
            return PullRequestMeta(
                "PR", None, "u", "b", "m", (), len(snapshots), 1, 0, False, False
            )

        async def draft_review(self, *, context: ReviewContext) -> ReviewReply:
            reply = await GatewayReviewModel(
                _gateway(Transport(json.dumps(answer()))), RUN, self
            ).draft_review(context=context)
            assert isinstance(reply, ReviewReply)
            sent = {file.path: file for file in reply.context.changed_files}
            assert len(sent["aaa20.py"].lines) == 1 and sent["aaa20.py"].total_lines == 2
            assert sent["zzz.py"].lines == next(
                file.lines for file in changed if file.path == "zzz.py"
            )
            assert (
                "private/hidden.py" not in sent
                and "private/hidden.py" not in reply.context.omitted_files
            )
            return reply

    assert asyncio.run(
        ExecuteReviewRun(
            Processor(),
            Prompts(),
            Model(),
            store,
            context_filter=Filter(),
            reflection_pass=ReflectionPass(FakeVerifierModel({"decisions": []})),
        ).execute(UUID(int=213))
    )
    final = repository.response["coverage"]
    assert final["reviewed_files"] == 1
    assert final["reason_counts"] == (
        {"patch_truncated": 20, "review_input_incomplete": 1}
        if fully_reviewable
        else {"patch_truncated": 21}
    )
    assert len(final["reasons"]) == 20 and "private/hidden.py" not in str(final)
    assert repository.response["verdict"] is None
    assert (
        read_saved_review_result(repository.response, head_sha="a" * 40, severities=()) is not None
    )


@pytest.mark.parametrize("known_count", [False, True])
def test_unknown_membership_withholds_counts_without_inventing_reviewed_files(
    known_count: bool,
) -> None:
    from app.modules.reviews.application.review_output import VerifiedReviewOutput
    from tests.test_run_coverage import code_snapshot, coverage

    source = (
        replace(coverage([code_snapshot()]), durable_reviewable_paths=None) if known_count else None
    )
    repository = Repository(source)
    committed: list[str] = []
    store = StoreReviewOutput(
        lambda: Uow(repository, committed),
        Queue(committed),
        worker_id="worker",
        run_url=lambda _: None,
    )
    assert asyncio.run(
        store.execute(
            UUID(int=213),
            VerifiedReviewOutput(
                answer(),
                incomplete_paths=("src/app.py",),
            ),
        )
    )
    result = repository.response
    assert result["coverage"]["status"] == "partial" and result["coverage"]["reviewed_files"] == 0
    assert result["coverage"]["reason_counts"] == {
        "classification_unavailable": 2 if known_count else 1
    }
    assert result["verdict"] is None and "src/app.py" not in str(result["coverage"]["reasons"])
    if known_count:
        assert read_saved_review_result(result, head_sha="a" * 40, severities=()) is not None
