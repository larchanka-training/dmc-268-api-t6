"""Replay recorded RAG evidence and reject tampering."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import review.scripts.eval_live as eval_live_module
import review.scripts.eval_rag_context as eval_rag_context_module
import review.scripts.eval_replay as eval_replay_module
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.infrastructure.llm.settings import LlmSettings
from review.scripts.eval_provenance import (
    static_input_paths,
)
from review.scripts.eval_replay import ReplayError, format_console, replay
from review.scripts.rag_snapshot import build_case_snapshot, render_snapshot
from tests.eval_live_fakes import settings as rag_settings
from tests.eval_replay_fakes import (
    DATASET_ROOT,
    REPO_ROOT,
    edit_manifest,
)
from tests.test_eval_live_rag import (
    CASES,
    rag_root,
    record_rag,
    valid_answers,
    window_without_rag,
)

# --- RAG captures (#146): replay re-derives the recorded evidence from the frozen snapshot ---


def record_rag_arm(
    tmp_path: Path, arm: str, *, cases: dict[str, tuple[str, str]] | None = None, **kwargs: Any
) -> tuple[Path, Path, dict[str, Any]]:
    """Record ``arm`` over fixture RAG cases with a fake transport (root, responses, manifest)."""
    root = rag_root(tmp_path, cases)
    responses = tmp_path / f"arm-{arm}"
    manifest = record_rag(
        root, valid_answers(len(cases or CASES)), arm, responses_dir=responses, **kwargs
    )
    return root, responses, manifest


@pytest.mark.parametrize("arm", ["none", "graph"])
def test_replay_of_a_recorded_rag_capture_reproduces_it_without_a_warning(
    tmp_path: Path, arm: str
) -> None:
    root, responses, manifest = record_rag_arm(tmp_path, arm)

    report = replay(root, responses_dir=responses)

    # The static digest of a RAG capture covers the RAG modules: no path-set warning.
    assert report["warnings"] == []
    assert report["corpus"] == "rag"
    assert report["provenance"]["run_metadata"]["rag_arm"] == arm
    assert report["provenance"]["run_metadata"] == manifest["run_metadata"]
    assert "RAG arm: " + arm in format_console(report).splitlines()


def test_replay_of_a_capture_whose_rag_slot_the_gateway_fitted_away_reproduces_it(
    tmp_path: Path,
) -> None:
    """The recorded prompt budget lets the replay redo the fit, so a fitted case replays too."""
    cases = {"RAG-01": ("spike", "reverse_edge")}
    root = rag_root(tmp_path, cases)
    window = window_without_rag(root, "RAG-01")
    responses = tmp_path / "fitted"
    manifest = record_rag(
        root, valid_answers(1), "graph", model_settings=window, responses_dir=responses
    )
    assert manifest["run_metadata"]["rag_evidence"]["RAG-01"]["trimmed"] is True

    report = replay(root, responses_dir=responses)

    assert report["warnings"] == []
    # A different budget is a different fit: the recorded evidence no longer reproduces.
    edit_manifest(
        responses,
        lambda value: value["run_metadata"]["effective_settings"]["primary"].update(
            input_budget_tokens=10**6
        ),
    )
    with pytest.raises(ReplayError, match=r"RAG-01 \(.*prompt_digest"):
        replay(root, responses_dir=responses)


def test_replay_needs_no_network_and_no_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, responses, _ = record_rag_arm(tmp_path, "graph")
    for name in list(os.environ):
        if name.startswith(("LLM_", "OPENAI_", "ANTHROPIC_")) or name.endswith("_API_KEY"):
            monkeypatch.delenv(name)

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("replay opened a connection")

    # asyncio's self-pipe needs socket construction; only reaching out is refused.
    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)

    assert replay(root, responses_dir=responses)["warnings"] == []


@pytest.mark.parametrize(
    ("field", "edit"),
    [
        ("prompt_digest", lambda item: item.update(prompt_digest="sha256-v1:" + "0" * 64)),
        (
            "non_rag_prompt_digest",
            lambda item: item.update(non_rag_prompt_digest="sha256-v1:" + "0" * 64),
        ),
        ("retrieved", lambda item: item["retrieved"][0].update(tokens=1)),
        ("retrieved", lambda item: item.update(retrieved=[])),
        ("offline_retrieved_ids", lambda item: item.update(offline_retrieved_ids=[])),
        ("trimmed", lambda item: item.update(trimmed=True)),
    ],
    ids=["prompt", "non-rag", "tokens", "dropped", "offline-ids", "trimmed"],
)
def test_a_tampered_evidence_record_fails_the_replay_naming_the_case(
    tmp_path: Path, field: str, edit: Callable[[dict[str, Any]], None]
) -> None:
    root, responses, _ = record_rag_arm(tmp_path, "graph")
    edit_manifest(responses, lambda value: edit(value["run_metadata"]["rag_evidence"]["RAG-02"]))

    with pytest.raises(ReplayError) as raised:
        replay(root, responses_dir=responses)

    message = str(raised.value)
    assert "RAG-02" in message
    assert "RAG-01" not in message
    assert field in message


def test_a_tampered_index_fails_the_replay_naming_the_case(tmp_path: Path) -> None:
    """The case validation every replay runs requires ``index.json`` to be what the builder
    renders from the case tree, so an edited index is refused before any evidence is compared."""
    root, responses, _ = record_rag_arm(tmp_path, "graph")
    index = root / "cases" / "RAG-01" / "index.json"
    document = json.loads(index.read_text(encoding="utf-8"))
    document["edges"] = [edge for edge in document["edges"] if edge["kind"] != "uses_imported"]
    index.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(ReplayError) as raised:
        replay(root, responses_dir=responses)

    message = str(raised.value)
    assert "RAG-01" in message
    assert "index.json" in message
    assert "RAG-02" not in message


def test_a_case_that_cannot_be_rebuilt_fails_the_replay_naming_the_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, responses, _ = record_rag_arm(tmp_path, "graph")
    original = eval_rag_context_module.build_arm_input

    async def build(case_dir: Path, arm: Any, *, review_model: str, version: str) -> Any:
        # The case validation builds with its own model name; the replay with the recorded one.
        if review_model == "primary-model":
            raise ValueError("the recorded model cannot be built for")
        return await original(case_dir, arm, review_model=review_model, version=version)

    monkeypatch.setattr(eval_rag_context_module, "build_arm_input", build)

    with pytest.raises(ReplayError, match="RAG case RAG-01: the offline build failed"):
        replay(root, responses_dir=responses)


def test_a_regenerated_snapshot_that_changes_no_answer_is_a_digest_warning(
    tmp_path: Path,
) -> None:
    """A case whose tree and index were regenerated together passes the validation; what the
    capture recorded then differs from the files by digest only."""
    root, responses, _ = record_rag_arm(tmp_path, "graph")
    case_dir = root / "cases" / "RAG-02"
    # A file no block shows: the prompt is the same, the snapshot is not.
    (case_dir / "base" / "zz_unused.py").write_text("UNUSED = 1\n", encoding="utf-8")
    (case_dir / "index.json").write_bytes(render_snapshot(build_case_snapshot(case_dir)))

    warnings = replay(root, responses_dir=responses)["warnings"]

    assert "Corpus input digest mismatch; refresh the complete baseline" in warnings
    assert "RAG index digest mismatch; refresh the complete baseline" in warnings


def test_a_tampered_pseudo_revision_or_missing_budget_fails_the_replay(tmp_path: Path) -> None:
    root, responses, _ = record_rag_arm(tmp_path, "none")
    original = (responses / "manifest.json").read_text(encoding="utf-8")

    edit_manifest(
        responses,
        lambda value: value["run_metadata"]["snapshot_pseudo_revisions"]["RAG-01"].update(
            base="0" * 40
        ),
    )
    with pytest.raises(ReplayError, match="snapshot_pseudo_revisions.*RAG-01"):
        replay(root, responses_dir=responses)

    (responses / "manifest.json").write_text(original, encoding="utf-8")
    edit_manifest(responses, lambda value: value["run_metadata"].pop("effective_settings"))
    with pytest.raises(ReplayError, match="input_budget_tokens"):
        replay(root, responses_dir=responses)


def test_a_case_without_evidence_must_be_a_nonpublishable_one(tmp_path: Path) -> None:
    root, responses, _ = record_rag_arm(tmp_path, "graph")

    edit_manifest(
        responses,
        lambda value: value["run_metadata"]["rag_evidence"].update({"RAG-01": None}),
    )

    with pytest.raises(ReplayError, match="evidence is missing for publishable cases: RAG-01"):
        replay(root, responses_dir=responses)


def test_a_capture_that_never_reached_the_provider_replays_as_nonpublishable(
    tmp_path: Path,
) -> None:
    cases = {"RAG-01": ("spike", "reverse_edge")}
    root = rag_root(tmp_path, cases)
    configured = rag_settings()
    nothing_fits = LlmSettings(
        replace(configured.primary, context_window=1_001, max_output_tokens=1_000), None
    )
    responses = tmp_path / "none-sent"
    record_rag(
        root, valid_answers(0), "graph", model_settings=nothing_fits, responses_dir=responses
    )

    report = replay(root, responses_dir=responses)

    assert report["warnings"] == [
        "Recorded capture is not publishable: provider, infrastructure or policy failure"
    ]


@pytest.mark.parametrize(
    "edit",
    [
        lambda metadata: metadata.pop("rag_evidence"),
        lambda metadata: metadata.update(rag_arm="vector"),
        lambda metadata: metadata.update(policy_arm="legacy"),
        lambda metadata: metadata.update(rag_index_digest="nope"),
        lambda metadata: metadata.update(latency_ms={}),
        lambda metadata: metadata["latency_ms"].update({"RAG-01": True}),
        lambda metadata: metadata["rag_evidence"].pop("RAG-01"),
        lambda metadata: metadata["rag_evidence"]["RAG-01"].pop("trimmed"),
        lambda metadata: metadata["rag_evidence"]["RAG-01"]["retrieved"][0].pop("tokens"),
        lambda metadata: metadata["snapshot_pseudo_revisions"].update({"RAG-01": {}}),
        lambda metadata: metadata["snapshot_pseudo_revisions"]["RAG-01"].update(extra="x"),
        lambda metadata: metadata["snapshot_pseudo_revisions"]["RAG-01"].update(base=1),
        lambda metadata: metadata["snapshot_pseudo_revisions"]["RAG-01"].update(head=None),
        lambda metadata: metadata["latency_ms"].update({"RAG-01": -1}),
        lambda metadata: metadata["latency_ms"].update({"RAG-01": 1.5}),
        lambda metadata: metadata["rag_evidence"]["RAG-01"].update(prompt_digest="x"),
        lambda metadata: metadata["rag_evidence"]["RAG-01"].update(prompt_digest=5),
        lambda metadata: metadata["rag_evidence"]["RAG-01"].update(non_rag_prompt_digest="x"),
        lambda metadata: metadata["rag_evidence"]["RAG-01"].update(trimmed=0),
        lambda metadata: metadata["rag_evidence"]["RAG-01"].update(fallback_called="no"),
        lambda metadata: metadata["rag_evidence"]["RAG-01"].update(repair_called=None),
        lambda metadata: metadata["rag_evidence"]["RAG-01"].update(offline_retrieved_ids="x"),
        lambda metadata: metadata["rag_evidence"]["RAG-01"].update(offline_retrieved_ids=[1]),
        lambda metadata: metadata["rag_evidence"]["RAG-01"].update(retrieved="x"),
        lambda metadata: metadata["rag_evidence"]["RAG-01"].update(retrieved=["x"]),
        lambda metadata: metadata["rag_evidence"].update({"RAG-01": []}),
        lambda metadata: metadata["rag_evidence"]["RAG-01"].update(extra=1),
    ],
    ids=[
        "partial-fields",
        "unknown-arm",
        "policy-arm",
        "index-digest",
        "latency-keys",
        "latency-bool",
        "evidence-keys",
        "evidence-fields",
        "block-fields",
        "revisions",
        "revisions-extra-key",
        "revisions-base-int",
        "revisions-head-null",
        "latency-negative",
        "latency-float",
        "prompt-digest-format",
        "prompt-digest-type",
        "non-rag-digest-format",
        "trimmed-int",
        "fallback-str",
        "repair-null",
        "offline-ids-str",
        "offline-ids-int",
        "retrieved-str",
        "retrieved-item-str",
        "evidence-list",
        "evidence-extra-field",
    ],
)
def test_malformed_rag_metadata_is_refused(tmp_path: Path, edit: Callable[[Any], None]) -> None:
    root, responses, _ = record_rag_arm(tmp_path, "graph")
    edit_manifest(responses, lambda value: edit(value["run_metadata"]))

    with pytest.raises(ReplayError, match="response manifest run_metadata"):
        replay(root, responses_dir=responses)


def test_a_rag_corpus_and_a_rag_capture_replay_only_together(tmp_path: Path) -> None:
    root, responses, _ = record_rag_arm(tmp_path, "graph")

    # A RAG corpus with a capture that has no RAG arm.
    def strip_rag(value: dict[str, Any]) -> None:
        for name in ("rag_arm", "rag_index_digest", "snapshot_pseudo_revisions", "latency_ms"):
            del value["run_metadata"][name]
        del value["run_metadata"]["rag_evidence"]

    edit_manifest(responses, strip_rag)
    with pytest.raises(ReplayError, match="need a capture recorded with a RAG arm"):
        replay(root, responses_dir=responses)

    # A RAG capture over a corpus with another case in it.
    root, responses, _ = record_rag_arm(tmp_path / "again", "graph")
    shutil.copytree(DATASET_ROOT / "cases" / "SEC-01", root / "cases" / "SEC-01")
    with pytest.raises(ReplayError, match="needs a corpus of RAG cases only, not: SEC-01"):
        replay(root, responses_dir=responses)


def test_importing_the_replay_loads_no_rag_stack() -> None:
    code = (
        "import sys, review.scripts.eval_replay;"
        "bad = [m for m in sys.modules if m.startswith(('tree_sitter', 'review.scripts.eval_rag', "
        "'review.scripts.rag_snapshot'))];"
        "assert not bad, bad"
    )
    subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, check=True)


def test_the_labels_of_the_two_rag_arms_are_the_arms_the_recorder_knows() -> None:
    assert set(eval_replay_module.RAG_ARM_LABELS) == set(eval_live_module.RAG_ARMS)


def test_replaying_the_main_baseline_asks_for_no_rag_code_and_adds_no_rag_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A capture without ``rag_arm`` replays as it did before #146: the main static path set,
    the main corpus label, and none of the RAG keys in its report."""
    asked: list[Any] = []
    original = static_input_paths

    def spy(*args: Any, **kwargs: Any) -> list[str]:
        asked.append(kwargs.get("rag_code", False))
        return original(*args, **kwargs)

    monkeypatch.setattr(eval_replay_module, "static_input_paths", spy)

    report = replay(DATASET_ROOT)

    assert asked == [False]
    assert report["corpus"] == "main"
    assert set(report["provenance"]["run_metadata"]) & set(eval_replay_module._RAG_KEYS) == set()
    # The verifier pins changed both static path set and digest of this historical capture.
    assert report["warnings"] == [
        "Static input path set changed; refresh the complete baseline",
        "Static input digest mismatch; refresh the complete baseline",
    ]


@pytest.mark.parametrize("budget", [0, -5, 1.5, "100", True, None])
def test_a_recorded_prompt_budget_that_is_not_a_positive_integer_is_refused(
    tmp_path: Path, budget: object
) -> None:
    root, responses, _ = record_rag_arm(tmp_path, "graph")
    edit_manifest(
        responses,
        lambda value: value["run_metadata"]["effective_settings"]["primary"].update(
            input_budget_tokens=budget
        ),
    )

    with pytest.raises(ReplayError, match="input_budget_tokens"):
        replay(root, responses_dir=responses)


@pytest.mark.parametrize(
    "error",
    [
        ValueError("a value"),
        OSError("a file"),
        PolicyError(PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR, "a pin"),
    ],
    ids=["value", "os", "policy"],
)
def test_a_rebuild_that_raises_is_a_replay_error_naming_the_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    root, responses, _ = record_rag_arm(tmp_path, "graph")
    original = eval_rag_context_module.build_arm_input

    async def build(case_dir: Path, arm: Any, *, review_model: str, version: str) -> Any:
        if review_model == "primary-model":
            raise error
        return await original(case_dir, arm, review_model=review_model, version=version)

    monkeypatch.setattr(eval_rag_context_module, "build_arm_input", build)

    with pytest.raises(ReplayError, match="RAG case RAG-01: the offline build failed"):
        replay(root, responses_dir=responses)
