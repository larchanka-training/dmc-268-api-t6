"""The recorder's RAG arm (#146): ``--rag-arm none|graph`` over a corpus of RAG cases.

The corpus here is built from the fixture cases under ``tests/fixtures/rag_eval``; the
transport is a fake, nothing leaves the process.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import shutil
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import review.scripts.eval_live as eval_live_module
import review.scripts.eval_rag_context as eval_rag_context_module
from app.modules.reviews.application.llm import EngineName
from app.modules.reviews.application.policy_snapshot import PINNED_PROMPT_CHECKSUMS
from app.modules.reviews.application.prompt_budget import (
    fit_review_context,
    prompt_tokens,
    token_counter_for,
)
from app.modules.reviews.infrastructure.llm.settings import LlmSettings
from app.modules.reviews.infrastructure.llm.transport import TransportTimeout, TransportUnavailable
from review.scripts.eval_provenance import static_input_paths
from review.scripts.eval_rag_context import _policy_snapshot, build_arm_input
from review.scripts.rag_snapshot import INDEX_FILE, build_case_snapshot, render_snapshot
from tests.test_eval_live import (
    BACKEND_RULES,
    FRONTEND_RULES,
    INVALID,
    RECORDED_AT,
    REPO_ROOT,
    VALID,
    FakeTransport,
    answer,
    fixture_root,
    settings,
)
from tests.test_validate_dataset import _rag_metadata

SYSTEM_V5 = REPO_ROOT / "review" / "prompts" / "review.system.v5.md"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "rag_eval"
CASES = {"RAG-01": ("spike", "reverse_edge"), "RAG-02": ("clean", "clean")}


def rag_root(tmp_path: Path, cases: dict[str, tuple[str, str]] | None = None) -> Path:
    """A dataset root of RAG cases: copies of fixture cases, their indexes rebuilt."""
    root = tmp_path / "rag"
    for case_id, (fixture, kind) in (cases or CASES).items():
        case_dir = root / "cases" / case_id
        shutil.copytree(FIXTURES / fixture, case_dir)
        (case_dir / "case.json").write_text(
            json.dumps(_rag_metadata(kind, case_id)), encoding="utf-8"
        )
        # The indexed commit id derives from the directory name.
        (case_dir / INDEX_FILE).write_bytes(render_snapshot(build_case_snapshot(case_dir)))
    return root


class Clock:
    """A monotonic clock that advances 0.25 s per reading."""

    def __init__(self) -> None:
        self._now = 0.0

    def __call__(self) -> float:
        self._now += 0.25
        return self._now


def record_rag(
    root: Path,
    transport: FakeTransport,
    rag_arm: str | None,
    *,
    model_settings: LlmSettings | None = None,
    policy_arm: str = "none",
    system_prompt: Path = SYSTEM_V5,
    engine: EngineName = "fast",
    responses_dir: Path | None = None,
) -> dict[str, Any]:
    return asyncio.run(
        eval_live_module.record_live(
            root,
            model_settings or settings(),
            system_prompt=system_prompt,
            backend_rules=BACKEND_RULES,
            frontend_rules=FRONTEND_RULES,
            transport=transport,
            recorded_at=RECORDED_AT,
            engine=engine,
            policy_arm=policy_arm,
            rag_arm=rag_arm,
            responses_dir=responses_dir,
            clock=Clock(),
        )
    )


def valid_answers(count: int = len(CASES)) -> FakeTransport:
    return FakeTransport([answer("primary-model", VALID) for _ in range(count)])


# Literal digests of the prompts arm B sends for the fixture cases (system v5 + context + diff).
PROMPT_RAG_01 = "sha256-v1:f582353540e8d705d7f7470366208330a57af67bff693c0b36804e7ee6d40b7e"
PROMPT_RAG_02 = "sha256-v1:2879b95031889b860ab31f6d424ddbb742aa2b7c6f77eacce88a6d5e40c0a9aa"
PROMPT_RAG_01_FITTED = "sha256-v1:ef66b823a3c2115b765a31b35c80a6e5bbdb661fdc8e82c7a25ed6c78d4b77f7"


def user_messages(transport: FakeTransport) -> list[str]:
    return [request.messages[1].content for request in transport.requests]


def test_the_graph_arm_manifest_carries_every_recorded_field(tmp_path: Path) -> None:
    root = rag_root(tmp_path)
    transport = valid_answers()

    manifest = record_rag(root, transport, "graph")

    metadata = manifest["run_metadata"]
    assert metadata["rag_arm"] == "graph"
    assert metadata["policy_arm"] == "none"
    assert metadata["engine"] == "fast"
    assert manifest["prompt_version"] == "v5"
    assert metadata["rag_index_digest"] == (
        "sha256-v1:81a5270c1e1f642c67ad11fd949e702cbf462a514be2ada6a0f104e836bd743c"
    )
    # Literal ids: sha1 of "<case>:base" / "<case>:head".
    assert metadata["snapshot_pseudo_revisions"] == {
        "RAG-01": {
            "base": "d22f4893f05393ca688aebba25d6cfeeb96eb54e",
            "head": "133714ccf4f81c8d90a391746030037e5d3e0b63",
        },
        "RAG-02": {
            "base": "33d478988633f6066c7fe572e1325146766b6b29",
            "head": "0e01321f5c1a3ffe5b3b87ef1bf61c3da63d42dc",
        },
    }
    # Per-case latency of the review call, from the injected clock (one tick = 250 ms).
    assert metadata["latency_ms"] == {"RAG-01": 250, "RAG-02": 250}
    # The static digest covers the RAG arm's modules; the corpus digest covers index.json.
    assert "review/scripts/eval_rag_context.py" in manifest["static_inputs"]
    assert "cases/RAG-01/index.json" not in manifest["static_inputs"]
    evidence = metadata["rag_evidence"]
    assert set(evidence) == set(CASES)
    spike = evidence["RAG-01"]
    assert set(spike) == {
        "prompt_digest",
        "non_rag_prompt_digest",
        "retrieved",
        "offline_retrieved_ids",
        "trimmed",
        "fallback_called",
        "repair_called",
    }
    assert (spike["fallback_called"], spike["repair_called"], spike["trimmed"]) == (
        False,
        False,
        False,
    )
    # The caller of ``save`` that ``pkg/store.py`` changes, shown whole from the index of the base.
    assert spike["retrieved"] == [
        {
            "block_id": "RAG:133714ccf4f8:325f4ae43ab95354",
            "path": "pkg/api.py",
            "side": "RIGHT",
            "start_line": 4,
            "end_line": 8,
            "tokens": 111,
            "index_commit_sha": "d22f4893f05393ca688aebba25d6cfeeb96eb54e",
            "edge_kind": "uses_imported",
        }
    ]
    assert spike["offline_retrieved_ids"] == ["RAG:133714ccf4f8:325f4ae43ab95354"]
    # The evidence is of the prompt that went out, not of an offline copy of it.
    assert spike["prompt_digest"] == PROMPT_RAG_01
    assert evidence["RAG-02"]["prompt_digest"] == PROMPT_RAG_02


def test_the_none_arm_records_an_empty_retrieved_list(tmp_path: Path) -> None:
    root = rag_root(tmp_path)

    manifest = record_rag(root, valid_answers(), "none")

    metadata = manifest["run_metadata"]
    assert metadata["rag_arm"] == "none"
    assert [item["retrieved"] for item in metadata["rag_evidence"].values()] == [[], []]


def test_the_arms_differ_only_in_the_rag_arm_and_the_retrieved_blocks(tmp_path: Path) -> None:
    """AC2: the evidence that arm B added nothing but the retrieved blocks to arm A's input."""
    root = rag_root(tmp_path)
    sent_a, sent_b = valid_answers(), valid_answers()

    arm_a = record_rag(root, sent_a, "none", responses_dir=tmp_path / "A")
    arm_b = record_rag(root, sent_b, "graph", responses_dir=tmp_path / "B")

    def stripped(manifest: dict[str, Any]) -> dict[str, Any]:
        value = copy.deepcopy(manifest)
        del value["run_metadata"]["rag_arm"]
        for item in value["run_metadata"]["rag_evidence"].values():
            # What the retrieved blocks change: their records and, through them, the prompt.
            del item["retrieved"]
            del item["offline_retrieved_ids"]
            del item["prompt_digest"]
        return value

    assert stripped(arm_a) == stripped(arm_b)
    assert arm_a["run_metadata"]["rag_arm"] != arm_b["run_metadata"]["rag_arm"]
    # The retrieved records are what differ, and only B has any.
    retrieved = {
        arm: [item["retrieved"] for item in manifest["run_metadata"]["rag_evidence"].values()]
        for arm, manifest in (("A", arm_a), ("B", arm_b))
    }
    assert retrieved["A"] == [[], []]
    assert all(retrieved["B"])
    # In the prompts too, the RAG slot is the only place the arms part.
    for prompt_a, prompt_b in zip(user_messages(sent_a), user_messages(sent_b), strict=True):
        before_a, _, after_a = prompt_a.partition("<retrieved_excerpts>")
        before_b, _, after_b = prompt_b.partition("<retrieved_excerpts>")
        assert before_a == before_b
        assert "pkg/api.py" not in after_a
        assert "pkg/api.py" in after_b


def refusal_cases() -> Iterator[Any]:
    yield pytest.param({"rag_arm": None}, "--rag-arm", id="rag-corpus-without-arm")
    yield pytest.param({"rag_arm": "vector"}, "--rag-arm", id="unknown-arm")
    yield pytest.param({"policy_arm": "legacy"}, "--policy-arm none", id="policy-legacy")
    yield pytest.param({"policy_arm": "valid"}, "--policy-arm none", id="policy-valid")
    yield pytest.param({"engine": "deep"}, "fast engine", id="deep-engine")
    for version in ("v2", "v3", "v4"):
        yield pytest.param(
            {"system_prompt": REPO_ROOT / "review" / "prompts" / f"review.system.{version}.md"},
            "review.system.v5",
            id=f"prompt-{version}",
        )


@pytest.mark.parametrize(("kwargs", "message"), list(refusal_cases()))
def test_a_wrong_rag_configuration_fails_before_the_first_call(
    tmp_path: Path, kwargs: dict[str, Any], message: str
) -> None:
    root = rag_root(tmp_path)
    transport = valid_answers()
    arguments: dict[str, Any] = {"rag_arm": "graph", **kwargs}

    with pytest.raises(eval_live_module.RecorderError, match=message):
        record_rag(root, transport, arguments.pop("rag_arm"), **arguments)

    assert transport.requests == []
    assert not (root / "responses").exists()


def test_a_v5_prompt_that_does_not_match_its_pin_fails_before_the_first_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A v5 file that is not the pinned one never reaches a call (the offline build and the
    recorder both check the pin)."""
    monkeypatch.setitem(PINNED_PROMPT_CHECKSUMS, "review.system:v5", "0" * 64)
    transport = valid_answers()

    with pytest.raises(eval_live_module.RecorderError, match="pinned"):
        record_rag(rag_root(tmp_path), transport, "graph")

    assert transport.requests == []


def test_a_rag_arm_on_a_corpus_without_rag_cases_fails_before_the_first_call(
    tmp_path: Path,
) -> None:
    root = fixture_root(tmp_path, ("SEC-01",))
    transport = valid_answers(1)

    with pytest.raises(eval_live_module.RecorderError, match="no RAG cases"):
        record_rag(root, transport, "graph")

    assert transport.requests == []


def test_a_corpus_that_mixes_rag_and_other_cases_fails_before_the_first_call(
    tmp_path: Path,
) -> None:
    root = rag_root(tmp_path)
    shutil.copytree(REPO_ROOT / "test-prs-dataset" / "cases" / "SEC-01", root / "cases" / "SEC-01")
    transport = valid_answers(3)

    with pytest.raises(eval_live_module.RecorderError, match="mixes"):
        record_rag(root, transport, "graph")

    assert transport.requests == []


def test_a_broken_case_is_refused_before_the_first_call(tmp_path: Path) -> None:
    root = rag_root(tmp_path)
    (root / "cases" / "RAG-02" / INDEX_FILE).unlink()
    transport = valid_answers()

    with pytest.raises(eval_live_module.RecorderError, match="RAG-02"):
        record_rag(root, transport, "graph")

    assert transport.requests == []


def window_without_rag(root: Path, case_id: str) -> LlmSettings:
    """A model window the gateway fits arm B of ``case_id`` into only by dropping the RAG slot.

    Found with the production fitting itself: the largest budget at which no retrieved block
    is left (L3 and L2 go first, so a window just under B's prompt would keep the RAG slot).
    """
    configured = settings()
    context = asyncio.run(
        build_arm_input(root / "cases" / case_id, "B", review_model="primary-model")
    ).context
    assert context.retrieved
    counter = token_counter_for("counter/v2")
    for budget in range(prompt_tokens(context, counter), 0, -1):
        fitted = fit_review_context(context, max_prompt_tokens=budget, counter=counter)
        if not fitted.retrieved:
            break
    else:
        raise AssertionError("the RAG slot is never dropped")
    # Budget = window - max_output; the gateway fits the prompt to it.
    return LlmSettings(
        replace(
            configured.primary,
            context_window=budget + configured.primary.max_output_tokens,
        ),
        None,
    )


def test_the_evidence_is_of_the_context_the_gateway_fitted(tmp_path: Path) -> None:
    """The gateway trims excerpts to the model's window: the record is of what was sent."""
    root = rag_root(tmp_path, {"RAG-01": ("spike", "reverse_edge")})
    window = window_without_rag(root, "RAG-01")
    roomy = record_rag(root, valid_answers(1), "graph", responses_dir=tmp_path / "roomy")
    transport = valid_answers(1)

    manifest = record_rag(root, transport, "graph", model_settings=window)

    sent = manifest["run_metadata"]["rag_evidence"]["RAG-01"]
    offline = roomy["run_metadata"]["rag_evidence"]["RAG-01"]
    assert offline["retrieved"]
    assert sent["retrieved"] == []
    # The offline ids stay visible, so a case whose RAG slot was fitted away is not a clean B.
    assert sent["trimmed"] is True
    assert sent["offline_retrieved_ids"] == [item["block_id"] for item in offline["retrieved"]]
    assert offline["trimmed"] is False
    assert sent["prompt_digest"] == PROMPT_RAG_01_FITTED
    assert sent["prompt_digest"] != offline["prompt_digest"]


def test_a_prompt_that_is_not_the_recorded_one_stops_the_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed: evidence that does not describe the bytes sent is never written."""
    root = rag_root(tmp_path, {"RAG-01": ("spike", "reverse_edge")})
    window = window_without_rag(root, "RAG-01")
    # The recorder's copy of the gateway's fitting disagrees with the gateway.
    monkeypatch.setattr(eval_live_module, "fit_review_context", lambda context, **_: context)

    with pytest.raises(eval_live_module.RecorderError, match="RAG-01.*differs from the prompt"):
        record_rag(root, valid_answers(1), "graph", model_settings=window)

    assert not (root / "responses").exists()
    assert list(root.glob(".responses-*")) == []


def test_a_case_that_never_reached_the_provider_has_no_evidence_but_a_latency(
    tmp_path: Path,
) -> None:
    root = rag_root(tmp_path, {"RAG-01": ("spike", "reverse_edge")})
    configured = settings()
    nothing_fits = LlmSettings(
        replace(configured.primary, context_window=1_001, max_output_tokens=1_000), None
    )
    transport = valid_answers(0)

    manifest = record_rag(root, transport, "graph", model_settings=nothing_fits)

    metadata = manifest["run_metadata"]
    assert transport.requests == []
    assert metadata["cases"]["RAG-01"]["first_call"] == "no_call"
    assert metadata["rag_evidence"] == {"RAG-01": None}
    assert metadata["latency_ms"] == {"RAG-01": 250}


def test_a_fallback_call_is_flagged_on_its_case(tmp_path: Path) -> None:
    root = rag_root(tmp_path, {"RAG-01": ("spike", "reverse_edge")})
    transport = FakeTransport(
        [
            TransportUnavailable("provider down", retryable=False),
            answer("fallback-model", VALID),
        ]
    )

    manifest = record_rag(root, transport, "graph")

    assert [request.profile.model for request in transport.requests] == [
        "primary-model",
        "fallback-model",
    ]
    evidence = manifest["run_metadata"]["rag_evidence"]["RAG-01"]
    assert evidence["fallback_called"] is True
    # The evidence is of the first request, the one the recorded answer belongs to.
    assert evidence["prompt_digest"] == PROMPT_RAG_01


def test_the_capture_replays_without_the_rag_fields_being_rejected(tmp_path: Path) -> None:
    from review.scripts.eval_replay import replay

    root = rag_root(tmp_path)
    record_rag(root, valid_answers(), "graph")

    report = replay(root)

    assert report["provenance"]["run_metadata"]["rag_arm"] == "graph"
    assert report["validity"] == 1.0


def test_the_cli_records_a_rag_arm_into_its_responses_directory(tmp_path: Path) -> None:
    root = rag_root(tmp_path)
    responses = tmp_path / "graph-run1"

    code = eval_live_module.main(
        [
            "--root",
            str(root),
            "--system",
            str(SYSTEM_V5),
            "--policy-arm",
            "none",
            "--rag-arm",
            "graph",
            "--responses-dir",
            str(responses),
        ],
        transport=valid_answers(),
        model_settings=settings(),
    )

    assert code == 0
    manifest = json.loads((responses / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["run_metadata"]["rag_arm"] == "graph"
    assert sorted(path.name for path in responses.glob("RAG-*.json")) == [
        "RAG-01.json",
        "RAG-02.json",
    ]


def test_the_cli_without_a_rag_arm_refuses_a_rag_root(tmp_path: Path) -> None:
    transport = valid_answers()

    code = eval_live_module.main(
        ["--root", str(rag_root(tmp_path)), "--system", str(SYSTEM_V5), "--policy-arm", "none"],
        transport=transport,
        model_settings=settings(),
    )

    assert code == 1
    assert transport.requests == []


def test_a_main_capture_carries_none_of_the_rag_fields(tmp_path: Path) -> None:
    root = fixture_root(tmp_path, ("SEC-01",))
    transport = valid_answers(1)

    manifest = asyncio.run(
        eval_live_module.record_live(
            root,
            settings(),
            system_prompt=SYSTEM_V5,
            backend_rules=BACKEND_RULES,
            frontend_rules=FRONTEND_RULES,
            transport=transport,
            recorded_at=RECORDED_AT,
            policy_arm="none",
        )
    )

    rag_fields = {
        "rag_arm",
        "rag_index_digest",
        "snapshot_pseudo_revisions",
        "latency_ms",
        "rag_evidence",
    }
    assert not rag_fields & set(manifest["run_metadata"])
    assert "review/scripts/eval_rag_context.py" not in manifest["static_inputs"]


def test_the_patch_is_read_as_bytes_so_every_line_ending_survives(tmp_path: Path) -> None:
    """``read_text`` would turn CRLF and a lone CR into LF, shifting the lines the validator
    and the offline builder see."""
    case_dir = tmp_path / "SEC-99"
    case_dir.mkdir()
    patch = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1,2 @@\n x = 1\n+y = 2\r\n"
    (case_dir / "diff.patch").write_bytes(patch.encode("utf-8"))

    case = eval_live_module._case_input(
        case_dir,
        {"patch_path": "diff.patch"},
        system="s",
        rules=(),
        engine="fast",
        commit_prefix=cast(Any, SimpleNamespace(messages=(), incomplete=False)),
    )

    assert case.diff == patch


def test_eval_live_and_the_rag_arm_bind_the_same_prompt_pins() -> None:
    """One construction of the prompt bindings: a copy would let the arms drift apart."""
    system = SYSTEM_V5.read_bytes()
    inputs = cast(
        Any,
        SimpleNamespace(
            prompt_version="v5",
            prompt_sha=hashlib.sha256(system).hexdigest(),
            prompt_path="review/prompts/review.system.v5.md",
        ),
    )

    recorder = eval_live_module._prompt_bindings(inputs, "none")
    rag = _policy_snapshot(system, "primary-model").prompt_bindings

    assert recorder == rag


def retry_settings() -> LlmSettings:
    configured = settings()
    return LlmSettings(
        configured.primary,
        configured.fallback,
        replace(configured.policy, timeout_retry_delays_s=(0.0,)),
    )


@pytest.mark.parametrize(
    ("outcomes", "models", "repair", "fallback"),
    [
        pytest.param(
            [answer("primary-model", INVALID), answer("primary-model", VALID)],
            ["primary-model"] * 2,
            True,
            False,
            id="repair",
        ),
        pytest.param(
            [TransportTimeout("slow", retryable=True), answer("primary-model", VALID)],
            ["primary-model"] * 2,
            False,
            False,
            id="retry",
        ),
        pytest.param(
            [TransportUnavailable("down", retryable=False), answer("fallback-model", VALID)],
            ["primary-model", "fallback-model"],
            False,
            True,
            id="fallback",
        ),
    ],
)
def test_every_request_of_a_case_carries_the_recorded_prompt_as_its_prefix(
    tmp_path: Path, outcomes: list[Any], models: list[str], repair: bool, fallback: bool
) -> None:
    """Repair, retry and fallback resend the first prompt (repair adds a turn): all confirmed."""
    root = rag_root(tmp_path, {"RAG-01": ("spike", "reverse_edge")})
    transport = FakeTransport(outcomes)

    manifest = record_rag(root, transport, "graph", model_settings=retry_settings())

    assert [request.profile.model for request in transport.requests] == models
    evidence = manifest["run_metadata"]["rag_evidence"]["RAG-01"]
    assert (evidence["repair_called"], evidence["fallback_called"]) == (repair, fallback)
    first = transport.requests[0].messages
    for request in transport.requests[1:]:
        assert request.messages[: len(first)] == first
    assert evidence["prompt_digest"] == PROMPT_RAG_01


def test_a_later_request_that_does_not_start_with_the_first_prompt_stops_the_capture(
    tmp_path: Path,
) -> None:
    root = rag_root(tmp_path, {"RAG-01": ("spike", "reverse_edge")})
    arm = asyncio.run(build_arm_input(root / "cases" / "RAG-01", "B", review_model="primary-model"))
    first = (("system", arm.prompt.system), ("user", arm.prompt.user))
    tap = eval_live_module._FirstRequestTap(None)

    def evidence(*later: tuple[tuple[str, str], ...]) -> Any:
        tap.requests = [first, *later]
        return eval_live_module._rag_evidence("RAG-01", arm.case, tap, (), settings(), "fast")

    # A repair turn on top of the first prompt is fine.
    assert evidence((*first, ("user", "fix it")))["prompt_digest"]
    with pytest.raises(eval_live_module.RecorderError, match="RAG-01.*request 2"):
        evidence((("system", arm.prompt.system), ("user", "another prompt")))


def test_a_rag_case_builds_no_policy_snapshot_from_its_base_tree(tmp_path: Path) -> None:
    """The RAG call sends no policy document, so one it cannot read must not stop the run."""
    root = rag_root(tmp_path, {"RAG-01": ("spike", "reverse_edge")})
    case_dir = root / "cases" / "RAG-01"
    (case_dir / "base" / "AGENTS.md").write_bytes(b"\xff\xfe not text")
    (case_dir / INDEX_FILE).write_bytes(render_snapshot(build_case_snapshot(case_dir)))

    manifest = record_rag(root, valid_answers(1), "graph")

    assert manifest["run_metadata"]["cases"]["RAG-01"]["gateway_status"] == "accepted"


def test_a_system_file_other_than_the_one_the_rag_build_sends_fails_before_any_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever the pin says, the manifest's ``prompt_sha`` must be of the bytes sent."""
    other = tmp_path / "review.system.v5.md"
    other.write_bytes(SYSTEM_V5.read_bytes() + b"\nExtra.\n")
    monkeypatch.setitem(
        PINNED_PROMPT_CHECKSUMS, "review.system:v5", hashlib.sha256(other.read_bytes()).hexdigest()
    )
    monkeypatch.setattr(eval_rag_context_module, "SYSTEM_PROMPT_V5", other)
    transport = valid_answers()

    with pytest.raises(eval_live_module.RecorderError, match="differs from the prompt the RAG"):
        record_rag(rag_root(tmp_path), transport, "graph")

    assert transport.requests == []


def test_the_prompt_builders_live_in_a_file_both_static_digests_cover() -> None:
    """They shape main and rules captures too: a change must move every static digest, and the
    main path set must stay as recorded (a new path there is a drift warning of its own)."""
    for builder in (
        eval_live_module.eval_prompt_bindings,
        eval_live_module.eval_execution_profile,
    ):
        source = Path(inspect.getsourcefile(builder) or "").relative_to(REPO_ROOT).as_posix()
        assert source in static_input_paths("p.md", [], repo_root=REPO_ROOT)
        assert source in static_input_paths("p.md", [], repo_root=REPO_ROOT, rag_code=True)
    assert "review/scripts/eval_rag_context.py" not in static_input_paths(
        "p.md", [], repo_root=REPO_ROOT
    )


def test_importing_the_recorder_does_not_load_the_rag_stack() -> None:
    code = (
        "import sys, review.scripts.eval_live;"
        "prefixes = ('tree_sitter', 'review.scripts.rag_snapshot', 'review.scripts.eval_rag');"
        "bad = [m for m in sys.modules if m.startswith(prefixes)];"
        "print(bad)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    )

    assert out.stdout.strip() == "[]"


def test_the_recorded_budget_is_the_budget_the_evidence_fits_to() -> None:
    """One formula (``eval_live.model_prompt_budget``) for ``effective_settings`` and for the
    fit of the evidence, which a replay reads back from the former."""
    configured = settings()
    profile = configured.primary

    budget = eval_live_module.model_prompt_budget(profile, configured, "fast")

    assert (
        budget
        == min(profile.context_window, configured.policy.input_token_limit["fast"])
        - profile.max_output_tokens
    )
    recorded = eval_live_module._effective_settings(configured, "fast")["primary"]
    assert cast(dict[str, Any], recorded)["input_budget_tokens"] == budget
