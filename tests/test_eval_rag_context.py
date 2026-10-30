"""The offline context of the #146 A/B arms: a fixture repository goes through the
production ``BuildReviewContext`` without a database, a network or a model (Tasks 1 and 4)."""

from __future__ import annotations

import asyncio
import re
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from app.bootstrap.llm_gateway import review_case
from app.modules.code_index.domain.enums import EdgeKind
from app.modules.reviews.application.context_provenance import SourceRole
from app.modules.reviews.application.diff_map import DiffSide
from app.modules.reviews.application.policy_errors import PolicyError
from app.modules.reviews.application.policy_snapshot import PINNED_PROMPT_CHECKSUMS
from app.modules.reviews.application.review_context import (
    BuildReviewContext,
    ContextLevel,
    ContextStatus,
    Representation,
)
from app.modules.reviews.application.review_context_stage import FAST_CONTEXT_TOKEN_BUDGET
from app.modules.reviews.application.vcs_diff import is_generated_path
from review.scripts.eval_rag_context import Arm, ArmInput, build_arm_input, build_case_input
from review.scripts.eval_rag_source import commit_id
from tests.test_eval_live import VALID as VALID_ANSWER
from tests.test_eval_live import FakeTransport, answer, settings

_FIXTURES = Path(__file__).parent / "fixtures" / "rag_eval"
_SPIKE = _FIXTURES / "spike"
_RENAMED = _FIXTURES / "renamed"
_MODEL = settings().primary.model
_CALLER = "pkg/api.py"
_EMPTY_SECTION = re.compile(r"<retrieved_excerpts>\s*</retrieved_excerpts>")
_SECTION = re.compile(r"<retrieved_excerpts>.*?</retrieved_excerpts>", re.DOTALL)


def _build(arm: Arm) -> ArmInput:
    return asyncio.run(build_arm_input(_SPIKE, arm, review_model=_MODEL))


def test_arm_b_has_one_retrieved_block_for_the_caller_and_arm_a_has_none() -> None:
    arm_a, arm_b = _build("A"), _build("B")

    assert arm_a.payload.status is ContextStatus.REVIEW
    assert arm_a.payload.retrieved == ()
    assert [(block.level, block.source.path) for block in arm_b.payload.retrieved] == [
        (ContextLevel.RAG, _CALLER)
    ]
    assert arm_b.payload.rag is not None
    assert arm_b.payload.rag.confirmed == 1


def test_the_non_rag_blocks_of_both_arms_are_identical() -> None:
    arm_a, arm_b = _build("A"), _build("B")

    def shown(arm: ArmInput) -> list[tuple[str, str]]:
        return [(block.block_id, block.text) for block in arm.context.context_blocks]

    assert shown(arm_a) == shown(arm_b)
    # Not vacuous: L2/L3 and L4 (the ``Item`` definition) are in both.
    assert {block.level for block in arm_a.context.context_blocks} >= {
        ContextLevel.L3,
        ContextLevel.L4,
    }
    assert arm_a.context.changed_files == arm_b.context.changed_files
    # The whole prompt differs only in the retrieved section.
    assert arm_a.prompt.system == arm_b.prompt.system
    assert _SECTION.sub("", arm_a.prompt.user) == _SECTION.sub("", arm_b.prompt.user)
    assert _SECTION.search(arm_a.prompt.user) and _SECTION.search(arm_b.prompt.user)


def test_the_v5_prompt_shows_an_empty_section_for_arm_a_and_one_excerpt_for_arm_b() -> None:
    arm_a, arm_b = _build("A"), _build("B")

    assert arm_a.prompt.user.count("<excerpt ") == len(arm_a.context.context_blocks)
    assert _EMPTY_SECTION.search(arm_a.prompt.user)
    assert 'level="RAG"' not in arm_a.prompt.user
    assert not _EMPTY_SECTION.search(arm_b.prompt.user)
    assert arm_b.prompt.user.count('level="RAG"') == 1
    assert f'path="{_CALLER}"' in arm_b.prompt.user
    assert "return save(Item(name))" in arm_b.prompt.user


def test_the_manifest_of_arm_b_carries_the_retrieved_block_the_guard_accepted() -> None:
    """``build_arm_input`` runs the gateway's pre-send provenance guard on the head id."""
    arm_a, arm_b = _build("A"), _build("B")

    def retrieved(arm: ArmInput) -> list[tuple[str | None, str | None, str | None]]:
        return [
            (block.path, block.edge_kind, block.index_commit_sha)
            for block in arm.manifest.blocks
            if block.source_role is SourceRole.RETRIEVED_EXCERPT
        ]

    assert retrieved(arm_a) == []
    [(path, edge_kind, index_commit)] = retrieved(arm_b)
    assert (path, edge_kind) == (_CALLER, EdgeKind.USES_IMPORTED.value)
    assert index_commit is not None


def test_the_manifests_of_two_builds_are_identical() -> None:
    """Nothing random reaches the manifest, so a recording can name it by its content."""
    assert _build("B").manifest == _build("B").manifest
    assert _build("A").manifest == _build("A").manifest


def test_a_system_prompt_that_is_not_the_pinned_v5_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(PINNED_PROMPT_CHECKSUMS, "review.system:v5", "0" * 64)
    with pytest.raises(PolicyError, match="review.system:v5"):
        asyncio.run(build_arm_input(_SPIKE, "A", review_model=_MODEL))


def test_an_unknown_arm_is_refused() -> None:
    with pytest.raises(ValueError, match="arm"):
        asyncio.run(build_arm_input(_SPIKE, "C", review_model=_MODEL))  # type: ignore[arg-type]


# --- Task 4: the case of an arm as the gateway takes it ------------------------------------


def _send(arm: Arm) -> tuple[ArmInput, str]:
    """The arm sent through the real ``review_case`` (policy, fitting, pre-send provenance
    guard) on a fake transport: the arm's offline prompt and the user message sent."""
    offline = _build(arm)
    transport = FakeTransport([answer("primary-model", VALID_ANSWER)])
    case = asyncio.run(build_case_input(_SPIKE, arm, review_model=_MODEL))
    result = asyncio.run(review_case(case, settings(), transport=transport))
    assert [call.kind.value for call in result.calls] == ["primary"]
    (request,) = transport.requests
    return offline, request.messages[1].content


@pytest.mark.parametrize("arm", ["A", "B"])
def test_the_gateway_accepts_each_arm_and_sends_the_offline_prompt(arm: Arm) -> None:
    offline, sent = _send(arm)

    assert sent == offline.prompt.user
    assert (arm == "B") == ('level="RAG"' in sent)


@pytest.mark.parametrize("arm", ["A", "B"])
def test_the_case_carries_the_mapped_blocks_and_the_head_id(arm: Arm) -> None:
    offline = _build(arm)
    case = asyncio.run(build_case_input(_SPIKE, arm, review_model=_MODEL))

    assert case.context_blocks == offline.context.context_blocks != ()
    assert case.retrieved == offline.context.retrieved
    assert len(case.retrieved) == (1 if arm == "B" else 0)
    assert case.pr_meta.head_sha == commit_id("spike", "head")
    assert case.policy_snapshot is not None
    assert case.engine == "fast"
    assert case.policy_snapshot.execution_profile.review_model == _MODEL
    assert case.diff == (_SPIKE / "diff.patch").read_text(encoding="utf-8")


def test_the_build_uses_the_production_token_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[int] = []
    execute = BuildReviewContext.execute

    async def spy(self: BuildReviewContext, **kwargs: Any) -> Any:
        seen.append(kwargs["token_budget"])
        return await execute(self, **kwargs)

    monkeypatch.setattr(BuildReviewContext, "execute", spy)

    _build("B")

    assert seen == [FAST_CONTEXT_TOKEN_BUDGET]


def test_a_renamed_export_gives_a_broken_reference_with_its_left_definition() -> None:
    arm_a = asyncio.run(build_arm_input(_RENAMED, "A", review_model=_MODEL))
    arm_b = asyncio.run(build_arm_input(_RENAMED, "B", review_model=_MODEL))

    assert arm_a.payload.retrieved == ()
    place, definition = arm_b.payload.retrieved
    # The unchanged importer at the head, read RIGHT...
    assert (place.level, place.source.path, place.source.side, place.source.reason) == (
        ContextLevel.RAG,
        "pkg/view.py",
        DiffSide.RIGHT,
        "rag_broken_reference",
    )
    assert place.source.revision == commit_id("renamed", "head")
    assert place.edge_kind is EdgeKind.USES_IMPORTED
    # ...with the definition it lost, read LEFT at the base, as a signature.
    assert (definition.source.path, definition.source.side) == ("pkg/store.py", DiffSide.LEFT)
    assert definition.source.revision == commit_id("renamed", "base")
    assert definition.source.reason == "rag_broken_reference"
    assert definition.representation is Representation.SIGNATURE
    assert definition.text == "def load(key: str) -> int:"
    assert place.definitions == (definition.source,)
    # Both reach the prompt B sends and the manifest the pre-send guard checked.
    user = arm_b.prompt.user
    assert user.count('level="RAG"') == 2
    assert 'path="pkg/view.py"' in user and "def load(key: str) -&gt; int:" in user
    assert {
        block.path
        for block in arm_b.manifest.blocks
        if block.source_role is SourceRole.RETRIEVED_EXCERPT
    } == {"pkg/store.py", "pkg/view.py"}
    assert arm_b.case.retrieved == arm_b.context.retrieved
    # The diff alone does not name the caller: arm A never sees it.
    assert 'path="pkg/view.py"' not in arm_a.prompt.user


def _copy_case(tmp_path: Path, source: Path, name: str | None = None) -> Path:
    target = tmp_path / (name or source.name)
    shutil.copytree(source, target)
    return target


def test_a_case_without_an_index_is_an_error_not_an_empty_graph(tmp_path: Path) -> None:
    case = _copy_case(tmp_path, _SPIKE)
    (case / "index.json").unlink()

    with pytest.raises(FileNotFoundError, match="index.json"):
        asyncio.run(build_arm_input(case, "B", review_model=_MODEL))


def test_an_index_of_another_revision_is_refused(tmp_path: Path) -> None:
    """The commit ids derive from the case directory name; an index built under another
    name (or for another base) is stale and would be read as the graph of this case."""
    case = _copy_case(tmp_path, _SPIKE, "renamed-elsewhere")

    with pytest.raises(ValueError, match="index.json.*base"):
        asyncio.run(build_arm_input(case, "B", review_model=_MODEL))


def test_a_changed_generated_file_is_refused_not_silently_dropped(tmp_path: Path) -> None:
    """Production names the count of withheld files in the prompt; the case cannot carry it."""
    case = _copy_case(tmp_path, _RENAMED)
    assert is_generated_path("package-lock.json")
    (case / "base" / "package-lock.json").write_text("{}\n", encoding="utf-8")
    patch = (case / "diff.patch").read_text(encoding="utf-8")
    (case / "diff.patch").write_text(
        patch
        + "diff --git a/package-lock.json b/package-lock.json\n"
        + "index 9e26dfe..0d2e3a1 100644\n"
        + "--- a/package-lock.json\n+++ b/package-lock.json\n"
        + '@@ -1 +1 @@\n-{}\n+{"a": 1}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="withholds"):
        asyncio.run(build_arm_input(case, "A", review_model=_MODEL))


def test_review_case_refuses_excerpts_its_prompt_would_not_show() -> None:
    """A miswired arm B must fail, not run as arm A."""
    case = asyncio.run(build_case_input(_SPIKE, "B", review_model=_MODEL))
    assert case.policy_snapshot is not None
    settings_ = settings()

    with pytest.raises(ValueError, match="context_blocks need a policy snapshot"):
        asyncio.run(review_case(replace(case, policy_snapshot=None), settings_))
    v4 = replace(case.policy_snapshot.prompt_bindings, review_system_version="v4")
    older = replace(case.policy_snapshot, prompt_bindings=v4)
    with pytest.raises(ValueError, match="retrieved blocks need"):
        asyncio.run(review_case(replace(case, policy_snapshot=older), settings_))
