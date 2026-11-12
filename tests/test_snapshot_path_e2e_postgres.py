"""Snapshot worker, retry, stored-name, and no-text partial scenarios."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.modules.reviews.infrastructure.run_repository import SqlAlchemyReviewOutputRepository
from tests.code_index_postgres import C1
from tests.portal_postgres import Env
from tests.rag_review_context_fakes import _GREET_PATCH
from tests.snapshot_path_e2e_support import (
    _ADDED_LINE,
    _LEGACY_STEPS,
    _PATH,
    _ROTATED,
    _ROTATED_REVIEW,
    _ROTATED_VERIFIER,
    _SERVICE_SHA,
    _TEST_SHA,
    _assert_snapshot_path,
    _observe,
    _rows,
)

pytest_plugins = ("tests.snapshot_path_e2e_support",)


@pytest.mark.integration
def test_a_v4_run_takes_the_snapshot_path_through_the_worker_composition(env: Env) -> None:
    observed = _observe(env, version=4)

    assert observed.outcomes == ["ack"]
    assert observed.queue.retries == []
    _assert_snapshot_path(observed, attempt=1)


@pytest.mark.integration
def test_snapshot_external_calls_and_publish_queue_run_without_worker_transaction(env: Env) -> None:
    observed = _observe(env, version=4, check_transactions=True)

    assert observed.state == ("publishing", None, 1)
    assert {"vcs", "conventions", "review-v2", "verifier", "publish"} <= set(
        observed.transaction_checks
    )


@pytest.mark.integration
def test_snapshot_t8_uses_the_saved_plan_without_legacy_output_write(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def reject_legacy_write(self: SqlAlchemyReviewOutputRepository, *args: object) -> None:
        raise AssertionError("legacy output write was called")

    monkeypatch.setattr(
        SqlAlchemyReviewOutputRepository, "store_review_output", reject_legacy_write
    )
    observed = _observe(env, version=4)
    assert observed.state == ("publishing", None, 1)
    assert len(observed.queue.published) == 1


@pytest.mark.integration
@pytest.mark.parametrize("change", ["cancel", "head", "claim"])
@pytest.mark.parametrize("prior_attempts", [0, 2])
def test_review_checkpoint_rejection_cancels_the_claimed_run(
    env: Env, change: str, prior_attempts: int
) -> None:
    def change_run_during_review(run_id: UUID) -> None:
        engine = create_engine(
            env.database_url, connect_args={"options": f"-csearch_path={env.schema}"}
        )
        try:
            with engine.begin() as connection:
                if change == "cancel":
                    connection.execute(
                        text("UPDATE runs SET cancel_requested=true WHERE id=:id"), {"id": run_id}
                    )
                elif change == "head":
                    connection.execute(
                        text(
                            "UPDATE code_changes SET head_sha=:head WHERE id="
                            "(SELECT code_change_id FROM runs WHERE id=:id)"
                        ),
                        {"id": run_id, "head": "a" * 40},
                    )
                else:
                    connection.execute(
                        text("UPDATE runs SET worker_id='another-worker' WHERE id=:id"),
                        {"id": run_id},
                    )
        finally:
            engine.dispose()

    observed = _observe(
        env,
        version=4,
        on_review=change_run_during_review,
        prior_attempts=prior_attempts,
    )
    assert observed.outcomes == ["ack"]
    expected = {
        "cancel": ("cancelled", "cancelled_by_user", prior_attempts + 1),
        "head": ("cancelled", "superseded", prior_attempts + 1),
        "claim": ("running", None, prior_attempts + 1),
    }
    assert observed.state == expected[change]
    assert observed.queue.retries == []
    assert observed.queue.published == []
    assert "llm.verify" not in observed.tools
    assert "llm.review_output" not in observed.tools
    assert len(observed.bodies) == 2


@pytest.mark.integration
def test_verifier_retry_uses_the_accepted_review_metadata_after_pr_edit(env: Env) -> None:
    observed = _observe(
        env,
        version=4,
        checkpoint_retry=True,
        verifier_kinds=("unavailable", "unavailable", "verifier"),
    )

    assert observed.outcomes == ["ack", "ack"]
    assert observed.state == ("publishing", None, 2)
    assert len(observed.queue.published) == 1
    calls = [request["operation"] for request, _ in observed.actions["llm.call"]]
    assert calls.count("review") == 1
    assert observed.checkpoint_meta[0]["title"] == "Fix parser"
    assert observed.checkpoint_meta[0]["labels"] == ["backend"]
    assert (
        observed.bodies[2]["messages"][1]["content"]
        == observed.bodies[-1]["messages"][1]["content"]
    )


@pytest.mark.integration
def test_verifier_retry_rereviews_when_a_saved_retrieved_block_disappears(env: Env) -> None:
    observed = _observe(
        env,
        version=5,
        rag=True,
        checkpoint_retry=True,
        missing_retrieved_on_retry=True,
        verifier_kinds=("unavailable", "unavailable", "verifier"),
    )

    assert observed.outcomes == ["ack", "ack"]
    assert observed.state == ("publishing", None, 2)
    assert len(observed.queue.published) == 1
    calls = [request["operation"] for request, _ in observed.actions["llm.call"]]
    assert calls.count("review") == 2
    assert len(observed.checkpoint_meta) == 2
    assert observed.checkpoint_attempts == [1, 2]
    assert [meta["title"] for meta in observed.checkpoint_meta] == [
        "Fix parser",
        "Fix parser (edited)",
    ]
    assert len(observed.actions["llm.review_output"]) == 2


@pytest.mark.integration
def test_verifier_retry_rereviews_when_saved_fitted_digest_is_corrupted(env: Env) -> None:
    observed = _observe(
        env,
        version=4,
        checkpoint_retry=True,
        missing_retrieved_on_retry=True,
        tamper_fitted_digest_on_retry=True,
        verifier_kinds=("unavailable", "unavailable", "verifier"),
    )

    assert observed.state == ("publishing", None, 2)
    calls = [request["operation"] for request, _ in observed.actions["llm.call"]]
    assert calls.count("review") == 2
    assert len(observed.queue.published) == 1


@pytest.mark.integration
def test_a_retried_v4_attempt_keeps_the_snapshot_and_the_pins(env: Env) -> None:
    # The first attempt stores the snapshot, then the diff read fails and the run is retried.
    # Newer conventions, review and verifier rows go live in between: the second attempt keeps
    # the stored snapshot and the run's pinned prompts. Its review call fails once at the
    # provider, after the conventions call, and the fallback answers.
    observed = _observe(env, version=4, retry=True)

    assert observed.outcomes == ["ack", "ack"]
    assert len(observed.queue.retries) == 1
    _assert_snapshot_path(
        observed,
        attempt=2,
        earlier=("policy.load", "vcs.fetch_diff"),
        review_fallback=True,
        summarized=False,
    )
    rotated = (_ROTATED, _ROTATED_REVIEW, _ROTATED_VERIFIER)
    assert all(
        prompt not in body["messages"][0]["content"]
        for body in observed.bodies
        for prompt in rotated
    )


@pytest.mark.integration
def test_a_v3_run_stays_on_the_legacy_path_in_the_same_composition(env: Env) -> None:
    observed = _observe(env, version=3)

    assert observed.outcomes == ["ack"]
    assert observed.state == ("publishing", None, 1)
    assert observed.tools == _LEGACY_STEPS
    assert observed.manifests == []
    # A legacy run keeps the composition's counter (four characters per token) for the
    # same blocks.
    [(_, stage_build)] = observed.actions["context.build"]
    assert stage_build["tokens_by_level"] == {"L1": 50, "L4": 11, "L3": 49, "L2": 12}
    assert observed.payloads == [(2, stage_build)]
    calls = [request for request, _ in observed.actions["llm.call"]]
    assert [call["operation"] for call in calls] == ["conventions", "review"]
    # No snapshot-path keys on a legacy call (E-3) and no block ids in the legacy prompt.
    assert all("manifest_id" not in call and "policy_snapshot_id" not in call for call in calls)
    assert len(observed.bodies) == 2
    assert "block_id=" not in observed.bodies[1]["messages"][-1]["content"]
    [(_, output)] = observed.actions["llm.review_output"]
    assert [(finding["path"], finding["line"]) for finding in output["findings"]] == [
        (_PATH, _ADDED_LINE)
    ]
    assert all("evidence_refs" not in finding for finding in output["findings"])
    assert [pointer.run_id for pointer in observed.queue.published] == [observed.run_id]


@pytest.mark.integration
def test_a_v5_run_without_rag_shows_an_empty_retrieved_section(env: Env) -> None:
    # RAG is off for the repository: no ``context.rag`` step, and the bound v5 prompt still
    # shows its retrieved section, empty.
    observed = _observe(env, version=5)

    assert observed.outcomes == ["ack"]
    assert observed.queue.retries == []
    _assert_snapshot_path(observed, attempt=1, version=5)


@pytest.mark.integration
def test_a_v4_run_in_flight_when_v5_is_seeded_keeps_the_v4_prompt(env: Env) -> None:
    # The run is created under the live v4 row; the v5 seed makes v5 live before the worker
    # takes it. RAG is on with a ready index: the run keeps its v4 pin and body, shows no
    # retrieved section, and ``context.rag`` records the usable index as not used.
    observed = _observe(env, version=4, rag=True, seeded_after=5)

    assert observed.outcomes == ["ack"]
    assert observed.queue.retries == []
    [(rag_request, rag_response)] = observed.actions["context.rag"]
    assert (rag_request["index_status"], rag_request["indexed_commit_sha"]) == ("ready", C1)
    assert rag_response == {"outcome": "not_ready", "reason": "prompt_version"}
    _assert_snapshot_path(observed, attempt=1, rag=True)


_LONG_NAME = "d/" * 549 + "x.py"


def _next_to_the_service(change: dict[str, str]) -> tuple[dict[str, Any], ...]:
    """The allowed service file, then a one-line change under the names of ``change``."""
    allowed = {"filename": _PATH, "status": "modified", "sha": _SERVICE_SHA, "patch": _GREET_PATCH}
    other = {"sha": _TEST_SHA, "patch": "@@ -0,0 +1 @@\n+x", **change}
    counts = {"additions": 1, "deletions": 0, "changes": 1}
    return ({**counts, **allowed}, {**counts, **other})


def _stored(env: Env, run_id: UUID) -> tuple[Any, ...]:
    """The run's usage events and stored diff rows, counted, and its error message."""

    async def stored() -> tuple[Any, ...]:
        engine = env.engine()
        try:
            [row] = await _rows(
                async_sessionmaker(engine),
                "SELECT (SELECT count(*) FROM usage_events WHERE run_id=:id), "
                "(SELECT count(*) FROM code_change_diffs WHERE run_id=:id), error_message "
                "FROM runs WHERE id=:id",
                run_id,
            )
            return row
        finally:
            await engine.dispose()

    return asyncio.run(stored())


@pytest.mark.integration
@pytest.mark.parametrize(
    ("change", "name"),
    [
        ({"filename": "a\x00b.py", "status": "added"}, "a\x00b.py"),
        (
            {"filename": "src/new.py", "status": "renamed", "previous_filename": "a\x00b.py"},
            "a\x00b.py",
        ),
        ({"filename": _LONG_NAME, "status": "added"}, _LONG_NAME),
        (
            {"filename": "src/new.py", "status": "renamed", "previous_filename": _LONG_NAME},
            _LONG_NAME,
        ),
    ],
    ids=["nul-new-name", "nul-old-name", "long-new-name", "long-old-name"],
)
def test_a_name_the_run_cannot_store_fails_the_v4_run_before_any_model_call(
    env: Env, change: dict[str, str], name: str
) -> None:
    # Next to the allowed service file, a change under a name with a NUL or over the 1024
    # characters a stored name holds: the source has no stored identity to withhold.
    observed = _observe(env, version=4, files=_next_to_the_service(change))

    usage_events, diff_rows, error_message = _stored(env, observed.run_id)
    # The first attempt ends the run: the code is not retried.
    assert observed.outcomes == ["ack"]
    assert observed.state == ("failed", "policy_snapshot_integrity_error", 1)
    assert observed.tools == ["policy.load", "vcs.fetch_diff"]
    assert observed.manifests == []
    assert observed.bodies == []
    assert usage_events == 0
    # Nothing of the diff is written, the allowed file neither; the message names no path.
    assert diff_rows == 0
    assert error_message == (
        "A changed file name cannot be stored unchanged, "
        "so the isolation of its source cannot be proven"
    )
    assert name not in error_message


@pytest.mark.integration
def test_a_long_name_keeps_the_v3_run_on_the_retried_internal_error(env: Env) -> None:
    # The legacy path withholds no file by its stored names: a new name over the 1024
    # characters a stored name holds still fails the diff write, a retried internal error,
    # until the attempts run out. No attempt reaches a model.
    observed = _observe(
        env,
        version=3,
        files=_next_to_the_service({"filename": _LONG_NAME, "status": "added"}),
        redeliveries=2,
    )

    usage_events, diff_rows, error_message = _stored(env, observed.run_id)
    assert observed.outcomes == ["ack", "ack", "dead_letter"]
    assert observed.state == ("failed", "internal_error", 3)
    assert observed.tools == ["policy.load", "vcs.fetch_diff"] * 3
    assert observed.manifests == []
    assert observed.bodies == []
    assert usage_events == 0
    # The diff write refuses the name, not the GitHub answer: nothing of the diff is stored.
    assert diff_rows == 0
    assert error_message.startswith("DataError: ")


def _summary_only(env: Env, run_id: UUID) -> bool:
    """The run's ``summaryOnly`` as the runs API reads it."""

    async def read() -> bool:
        engine = env.engine()
        try:
            runs = SqlAlchemyRunRepository(async_sessionmaker(engine), allow_unscoped=True)
            item = await runs.get_run(run_id)
            assert item is not None
            return item.summary_only
        finally:
            await engine.dispose()

    return asyncio.run(read())


@pytest.mark.integration
def test_a_v4_run_without_text_is_published_partial_without_a_model_call(env: Env) -> None:
    # The service file without a patch, its blob ordinary text: a complete inventory of three
    # counted lines and no analysable text. CVS §2.2 no-text partial: the backend result,
    # not summary-only, and no model call.
    missing = {
        "filename": _PATH,
        "status": "modified",
        "sha": _SERVICE_SHA,
        "additions": 3,
        "deletions": 0,
        "changes": 3,
    }
    observed = _observe(env, version=4, files=(missing,))

    usage_events, diff_rows, error_message = _stored(env, observed.run_id)
    assert observed.outcomes == ["ack"]
    assert observed.state == ("publishing", None, 1)
    assert observed.tools == [
        "policy.load",
        "vcs.fetch_diff",
        "context.build",
        "review.postprocess",
    ]
    assert observed.manifests == []
    assert observed.bodies == []
    assert usage_events == 0
    assert (diff_rows, error_message) == (1, None)
    [(_, build)] = observed.actions["context.build"]
    assert build["status"] == "no_reviewable_text"
    assert build["coverage"]["status"] == "partial"
    assert build["coverage"]["reviewed_files"] == 0
    assert build["coverage"]["reason_counts"] == {"missing_patch": 1}
    # The attempt's one context.build is also its context_payloads row (schema 2).
    assert observed.payloads == [(2, build)]
    [(_, postprocess)] = observed.actions["review.postprocess"]
    # Restored from the context.build row, not the classification_unavailable fallback.
    assert postprocess["coverage"] == build["coverage"]
    assert postprocess["verdict"] is None
    assert postprocess["inline"] == 0
    assert postprocess["summary"]["problem"] == (
        "Подтверждённых замечаний нет; Проверка неполная: Отсутствует подтверждённый patch (1)."
    )
    assert _summary_only(env, observed.run_id) is False
    [pointer] = observed.queue.published
    assert pointer.run_id == observed.run_id
    assert pointer.review_event == "COMMENT"
