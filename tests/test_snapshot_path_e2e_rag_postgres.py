"""RAG and retrieved excerpt behavior in snapshot worker runs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import pytest

from app.modules.reviews.infrastructure.llm.settings import LlmSettings
from tests.code_index_postgres import C1, C2
from tests.llm_gateway_fakes import FALLBACK, PRIMARY
from tests.portal_postgres import REPO_A, Env
from tests.rag_review_context_fakes import _USER_NOTED
from tests.review_prompt_pins_postgres import _V5_CONTENT, _V7_CONTENT
from tests.snapshot_path_e2e_support import (
    _APP_PATH,
    _CALL_LINE,
    _HEAD,
    _LLM,
    _SNAPSHOT_PATH_STEPS,
    Observed,
    _observe,
)

pytest_plugins = ("tests.snapshot_path_e2e_support",)


@dataclass(frozen=True)
class RetrievedRow:
    """A stored call manifest and the retrieved blocks it records."""

    manifest_id: str
    stage: str
    attempt: int
    blocks: list[dict[str, Any]]


def _retrieved_rows(observed: Observed) -> list[RetrievedRow]:
    return [
        RetrievedRow(
            str(manifest_id),
            stage,
            attempt,
            [block for block in blocks if block["source_role"] == "retrieved_excerpt"],
        )
        for manifest_id, (_, stage, attempt), blocks in zip(
            observed.manifest_ids, observed.manifests, observed.manifest_blocks, strict=True
        )
    ]


def _retrieved_section(prompt: str) -> str:
    [section] = re.findall(r"<retrieved_excerpts>\n(.*?)\n</retrieved_excerpts>", prompt, re.S)
    return str(section)


def _assert_retrieved_and_shown_again(
    observed: Observed,
    *,
    index_commit_sha: str,
    review: RetrievedRow,
    verification: RetrievedRow,
    review_body: dict[str, Any],
    verifier_body: dict[str, Any],
) -> None:
    """The accepted attempt: the review shows and records the caller of ``greet`` retrieved
    from the index at ``index_commit_sha``; the verifier shows that block again, recorded with
    the same metadata (ADR 0001 §9, CVS §5.2)."""
    assert review_body["messages"][0]["content"] == _V5_CONTENT
    section = _retrieved_section(review_body["messages"][-1]["content"])
    [rag] = re.findall(
        rf'<excerpt block_id="([^"]+)" level="RAG" path="{_APP_PATH}" revision="{_HEAD}" '
        r'side="RIGHT" representation="exact_lines">',
        section,
    )
    # The snapshot-path RAG id: the head and a digest, never the path (D3).
    assert re.fullmatch(r"RAG:f{12}:[0-9a-f]{16}", rag)
    assert f'<line n="{_CALL_LINE}">    return greet(user_id)</line>' in section
    [block] = review.blocks
    assert block == {
        "block_id": rag,
        "source_role": "retrieved_excerpt",
        "source_kind": "source_code",
        "path": _APP_PATH,
        "revision": _HEAD,
        "side": "RIGHT",
        "content_sha256": None,
        "start_line": 1,
        "end_line": _CALL_LINE,
        "shown_line_count": 3,
        "shown_ranges": [[1, 1], [4, _CALL_LINE]],
        "tokens": block["tokens"],
        "repository_id": str(REPO_A),
        "truncated": False,
        "selection_reason": "rag_reverse_edge",
        "index_commit_sha": index_commit_sha,
        "edge_kind": "uses_imported",
    }
    tokens_by_role = {
        response["manifest_id"]: response["tokens_by_role"]
        for _, response in observed.actions["context.manifest"]
    }
    # AC11: the manifest counts the retrieved block under its own role, no contract change.
    assert set(tokens_by_role[review.manifest_id]) == {
        "diff",
        "ast_excerpt",
        "source_excerpt",
        "policy_document",
        "retrieved_excerpt",
    }
    assert tokens_by_role[review.manifest_id]["retrieved_excerpt"] == block["tokens"]
    # The candidate cites the block, so the verifier is shown it again and its manifest
    # records the same block: same id, ranges, tokens and index commit.
    verifier_prompt = verifier_body["messages"][-1]["content"]
    assert _retrieved_section(verifier_prompt) == section
    assert f'<ref block_id="{rag}" start_line="{_CALL_LINE}" end_line="{_CALL_LINE}" />' in (
        verifier_prompt
    )
    assert verification.blocks == review.blocks
    assert tokens_by_role[verification.manifest_id]["retrieved_excerpt"] == block["tokens"]
    [(verify, _)] = observed.actions["llm.verify"]
    assert verify["manifest_id"] == verification.manifest_id
    # The stored finding keeps its citation of the retrieved block.
    [(_, output)] = observed.actions["llm.review_output"]
    [finding] = output["findings"]
    assert {"block_id": rag, "start_line": _CALL_LINE, "end_line": _CALL_LINE} in (
        finding["evidence_refs"]
    )


@pytest.mark.integration
def test_a_v5_run_shows_the_retrieved_section_through_the_worker_composition(env: Env) -> None:
    observed = _observe(env, version=5, rag=True)

    assert observed.outcomes == ["ack"]
    assert observed.queue.retries == []
    assert observed.state == ("publishing", None, 1)
    steps = list(_SNAPSHOT_PATH_STEPS)
    steps.insert(steps.index("context.build") + 1, "context.rag")
    assert observed.tools == steps
    # The stage reads the ready index at C1 and uses its one candidate.
    [(rag_request, rag_response)] = observed.actions["context.rag"]
    assert (rag_request["index_status"], rag_request["indexed_commit_sha"]) == ("ready", C1)
    assert rag_response["outcome"] == "used"
    rows = _retrieved_rows(observed)
    assert [(row.stage, row.attempt, len(row.blocks)) for row in rows] == [
        ("conventions", 1, 0),
        ("review", 1, 1),
        ("verification", 1, 1),
    ]
    _, review, verification = rows
    _, review_body, verifier_body = observed.bodies
    _assert_retrieved_and_shown_again(
        observed,
        index_commit_sha=C1,
        review=review,
        verification=verification,
        review_body=review_body,
        verifier_body=verifier_body,
    )


@pytest.mark.integration
@pytest.mark.parametrize(
    ("version", "seeded_after", "prompt", "shown"),
    [
        (7, None, _V7_CONTENT, "    return greet(user_id)  # [comment removed]"),
        (5, 7, _V5_CONTENT, "    return greet(user_id)  # note"),
    ],
    ids=["v7-hides-the-comment", "v5-in-flight-when-v7-is-seeded-shows-it"],
)
def test_the_pinned_prompt_decides_whether_a_retrieved_comment_is_shown(
    env: Env, version: int, seeded_after: int | None, prompt: str, shown: str
) -> None:
    # #498: the caller of ``greet`` has a comment. A v7-bound run shows it as a marker on the
    # same line; a run pinned to v5 before the v7 seed keeps v5 and shows the comment.
    observed = _observe(
        env, version=version, rag=True, seeded_after=seeded_after, caller=_USER_NOTED
    )

    assert observed.outcomes == ["ack"]
    [(_, rag_response)] = observed.actions["context.rag"]
    assert rag_response["outcome"] == "used"
    _, review_body, verifier_body = observed.bodies
    assert review_body["messages"][0]["content"] == prompt
    section = _retrieved_section(review_body["messages"][-1]["content"])
    assert f'<line n="{_CALL_LINE}">{shown}</line>' in section
    # The verifier shows the block the review showed.
    assert _retrieved_section(verifier_body["messages"][-1]["content"]) == section


@pytest.mark.integration
def test_a_retry_before_the_accepted_answer_retrieves_from_the_newer_index(env: Env) -> None:
    # D8: the candidate checkpoint is the stored manifest. The first attempt retrieves at C1,
    # then its review call fails on both models before any answer is accepted. A newer
    # publication of the index (C2) lands, and the retry retrieves again: its review and
    # its verifier show the same block, recorded with C2.
    observed = _observe(env, version=5, retry=True, rag=True)

    assert observed.outcomes == ["ack", "ack"]
    assert len(observed.queue.retries) == 1
    assert observed.state == ("publishing", None, 2)
    assert [
        (request["indexed_commit_sha"], response["outcome"])
        for request, response in observed.actions["context.rag"]
    ] == [(C1, "used"), (C2, "used")]
    rows = _retrieved_rows(observed)
    assert [
        (row.stage, row.attempt, [block["index_commit_sha"] for block in row.blocks])
        for row in rows
    ] == [
        ("conventions", 1, []),
        ("review", 1, [C1]),
        ("review", 1, [C1]),
        ("review", 2, [C2]),
        ("verification", 2, [C2]),
    ]
    # The failed attempt and the retry show one block under one id; only the index differs.
    assert {row.blocks[0]["block_id"] for row in rows[1:]} == {rows[1].blocks[0]["block_id"]}
    # Bodies: conventions, the refused review on both models, then the retry's review and
    # verifier (the retry keeps the first attempt's conventions).
    _, refused, _, review_body, verifier_body = observed.bodies
    assert _retrieved_section(refused["messages"][-1]["content"]) == _retrieved_section(
        review_body["messages"][-1]["content"]
    )
    _assert_retrieved_and_shown_again(
        observed,
        index_commit_sha=C2,
        review=rows[3],
        verification=rows[4],
        review_body=review_body,
        verifier_body=verifier_body,
    )


# The review models without a fallback: the one slot a cache-miss attempt leaves after the
# conventions, the review and the verifier goes to a repair of the same model.
_NO_FALLBACK = LlmSettings(primary=PRIMARY, fallback=None)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("llm", "recovery", "recovery_model"),
    [(_LLM, "fallback", FALLBACK.model), (_NO_FALLBACK, "repair", PRIMARY.model)],
    ids=["fallback", "repair"],
)
@pytest.mark.parametrize(
    ("review_kind", "invalid", "valid", "error"),
    [
        # A range past the shown hunk (RIGHT 9-13) of the diff block.
        ("review-v2", "verifier-unshown", "verifier", ":22-24 is not shown in verifier manifest"),
        # RULES_FORMAT_SPEC §8.2: a contradicted rule finding with no requirement of its rule.
        (
            "review-v2-rule",
            "verifier-contradicted",
            "verifier-rule",
            "for candidate with rule_name '.review/rules.md' requires at least one matching "
            "requirement_ref for this rule",
        ),
    ],
    ids=["unshown-range", "rule-without-requirement"],
)
def test_an_invalid_verifier_reference_earns_the_bounded_recovery(
    env: Env,
    llm: LlmSettings,
    recovery: str,
    recovery_model: str,
    review_kind: str,
    invalid: str,
    valid: str,
    error: str,
) -> None:
    # PIPELINE_SPEC :632, TEST_PLAN :139 (#130 hotfix, tech lead 28.10): a verifier answer
    # citing what its manifest does not show earns the bounded repair/fallback of §4.5 within
    # the attempt's slots before verification_invalid_output; the accepted answer publishes.
    observed = _observe(
        env, version=4, llm=llm, review_kind=review_kind, verifier_kinds=(invalid, valid)
    )

    assert observed.state == ("publishing", None, 1)
    calls = [request for request, _ in observed.actions["llm.call"]]
    verification = [call for call in calls if call["stage"] == "verification"]
    assert [(call["kind"], call["model"]) for call in verification] == [
        ("primary", PRIMARY.model),
        (recovery, recovery_model),
    ]
    assert [call["operation"] for call in verification] == ["verification", "verification"]
    # CVS §5.2: the recovery call has a manifest row of its own; llm.verify names the
    # accepted call and that row.
    first, accepted = verification
    assert first["manifest_id"] != accepted["manifest_id"]
    [(verify, decisions)] = observed.actions["llm.verify"]
    assert (verify["accepted_call_id"], verify["manifest_id"]) == (
        accepted["call_id"],
        accepted["manifest_id"],
    )
    assert [decision["status"] for decision in decisions["decisions"]] == ["supported"]
    *_, first_body, recovery_body = observed.bodies
    if recovery == "repair":
        # The error goes back in the escaped validation errors of the bounded repair turn.
        assert [message["role"] for message in recovery_body["messages"]] == [
            "system",
            "user",
            "user",
        ]
        [errors] = re.findall(
            r"<validation_errors>\n(.*?)</validation_errors>",
            recovery_body["messages"][-1]["content"],
            flags=re.DOTALL,
        )
        assert error in errors
    else:
        # The fallback gets the original verifier prompt.
        assert recovery_body["messages"] == first_body["messages"]
    [(_, postprocess)] = observed.actions["review.finalize"]
    assert postprocess["inline"] == 1
    assert [pointer.run_id for pointer in observed.queue.published] == [observed.run_id]
