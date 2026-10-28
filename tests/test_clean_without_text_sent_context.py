"""Final results describe the complete patches actually sent to the model."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping
from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest

from app.modules.reviews.application.execute_review import (
    ExecuteReviewRun,
    ReviewPromptInput,
    ReviewReply,
)
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.policy_snapshot import PolicySnapshot
from app.modules.reviews.application.prompt_builder import (
    ChangedFile,
    DiffLine,
    PromptBuilder,
    PullRequestMeta,
    RepoConventions,
    ReviewContext,
    file_block_id,
)
from app.modules.reviews.application.reflection_pass import ReflectionPass
from app.modules.reviews.application.review_context import ReviewContextPayload
from app.modules.reviews.application.review_output import VerifiedReviewOutput
from app.modules.reviews.application.store_review_output import StoreReviewOutput
from app.modules.reviews.infrastructure.llm.models import GatewayReviewModel
from tests.test_clean_without_text_result import Queue, Repository, Uow, answer, finalize
from tests.test_execute_review import FakeVerifierModel, _make_test_snapshot, conventions
from tests.test_gateway_review_model import RUN, Transport, _gateway
from tests.test_run_coverage import code_snapshot, coverage

_RUN = UUID(int=213)


def finding(path: str, severity: str = "high") -> dict[str, object]:
    return {
        "path": path,
        "line": 1,
        "start_line": None,
        "severity": severity,
        "category": "security",
        "title": "Unchecked authorization",
        "body": "A remote caller can bypass the permission check.",
        "suggestion": None,
        "confidence": 0.9,
        "rule_name": None,
    }


@pytest.mark.parametrize("severity", [None, "info", "high", "medium"])
def test_unknown_path_drop_downgrades_before_summary_verdict_and_storage(
    severity: str | None,
) -> None:
    findings = [finding("@private/hidden.py")]
    if severity is not None:
        findings.append(finding("src/app.py", severity))
        findings.sort(key=lambda item: {"high": 0, "medium": 1, "info": 2}[str(item["severity"])])
    repository, queue = finalize(coverage([code_snapshot()]), findings)
    saved = repository.response
    assert saved["coverage"]["status"] == "partial"
    assert saved["coverage"]["reason_counts"] == {"unknown_path": 1}
    assert saved["coverage"]["reasons"] == [
        {"path": None, "reason": "unknown_path", "change_type": None}
    ]
    assert (
        saved["verdict"]
        == {None: None, "info": None, "high": "blocking", "medium": "attention"}[severity]
    )
    assert "не удалось сопоставить замечание с доступным diff" in saved["summary"]["problem"]
    assert "@private" not in repository.body
    assert "**Verdict:** clean" not in repository.body
    assert queue.pointers[0].review_event == (
        "REQUEST_CHANGES" if severity == "high" else "COMMENT"
    )


class _Withheld:
    """The context stage as pass 2 sees it: a ``generated/**`` mask, a file the fetch
    marked generated and a binary file.

    Every name of a changed file under the mask or marked generated is a proven
    ignored file; the binary file is withheld but is not one of them.
    """

    diff_old_sha: str | None = None
    head_sha: str | None = None
    snapshot_for_llm: PolicySnapshot | None = None
    payload: ReviewContextPayload | None = None
    ignored_file_paths = frozenset(
        {
            "generated/api.py",
            "generated/gone.py",
            "src/moved.py",
            "generated/moved.py",
            "src/schema.py",
        }
    )

    def excluded(self, path: str) -> bool:
        return path.startswith("generated/") or path in {
            "src/moved.py",
            "src/schema.py",
            "assets/logo.png",
        }


def _review_beside_withheld_files(anchor: str) -> Repository:
    """Run a snapshot-path review whose only candidate is anchored on ``anchor``."""
    repository = Repository(
        coverage(
            [
                code_snapshot(),
                replace(code_snapshot(), filename="generated/api.py"),
                DiffSnapshot("generated/gone.py", "@@ -1 +0,0 @@\n-old", status="removed"),
                replace(
                    code_snapshot(),
                    filename="src/moved.py",
                    status="renamed",
                    previous_filename="generated/moved.py",
                ),
                DiffSnapshot("src/schema.py", None, omission_reason="generated"),
                DiffSnapshot("assets/logo.png", None, omission_reason="binary"),
            ],
            rules="```review-ignore\ngenerated/**\n```\n",
        )
    )
    assert repository.coverage is not None and repository.coverage.status.value == "complete"
    committed: list[str] = []
    store = StoreReviewOutput(
        lambda: Uow(repository, committed),
        Queue(committed),
        worker_id="worker",
        run_url=lambda _: None,
    )

    class Processor:
        async def prepare(self, run_id: UUID) -> Any:
            return conventions()

    class Prompts:
        async def get_review_prompt_input(self, run_id: UUID, saved: Any) -> ReviewPromptInput:
            return ReviewPromptInput(
                "Review carefully.",
                (),
                None,
                RepoConventions((), ()),
                (
                    ChangedFile("src/app.py", "modified", (DiffLine(1, "added", "new"),)),
                    ChangedFile("generated/api.py", "modified", (DiffLine(1, "added", "new"),)),
                ),
                ("assets/logo.png",),
                policy_snapshot=_make_test_snapshot(),
            )

    class Model:
        async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
            return PullRequestMeta("PR", None, "u", "b", "m", (), 6, 3, 3, False, False)

        async def draft_review(self, *, context: ReviewContext) -> dict[str, object]:
            assert [file.path for file in context.changed_files] == ["src/app.py"]
            value = answer()
            item = finding(anchor)
            item["evidence_refs"] = [
                {"block_id": file_block_id("src/app.py"), "start_line": 1, "end_line": 1}
            ]
            value["findings"] = [item]
            return value

    verifier = FakeVerifierModel({"decisions": []})
    assert asyncio.run(
        ExecuteReviewRun(
            Processor(),
            Prompts(),
            Model(),
            store,
            context_filter=_Withheld(),
            reflection_pass=ReflectionPass(verifier),
        ).execute(_RUN)
    )
    assert verifier.call_count == 0
    return repository


@pytest.mark.parametrize(
    "anchor",
    ["generated/api.py", "generated/gone.py", "src/moved.py", "src/schema.py"],
    ids=["masked", "masked-deleted", "renamed-out-of-mask", "fetch-generated"],
)
def test_anchor_on_an_ignored_changed_file_is_dropped_without_lowering_coverage(
    anchor: str,
) -> None:
    # CVS §6.2 pass 2: the file's text is withheld by design, so the anchor is
    # ignored_file, not unknown_path, and that drop alone does not make coverage partial.
    repository = _review_beside_withheld_files(anchor)

    saved = repository.response
    assert saved["dropped"] == [{"index": 0, "drop_reason": "ignored_file"}]
    assert saved["coverage"]["status"] == "complete"
    assert saved["coverage"]["reason_counts"] == {}
    assert (saved["inline"], saved["body_only"]) == (0, 0)
    assert anchor not in repository.body
    assert "Unchecked authorization" not in repository.body


def test_invented_path_under_the_mask_stays_unknown_path_and_partial() -> None:
    # A mask match does not prove that the path is a changed file.
    saved = _review_beside_withheld_files("generated/invented.py").response

    assert saved["dropped"] == [{"index": 0, "drop_reason": "unknown_path"}]
    assert saved["coverage"]["status"] == "partial"
    assert saved["coverage"]["reason_counts"] == {"unknown_path": 1}


def test_binary_anchor_is_not_an_ignored_file_and_keeps_coverage_partial() -> None:
    saved = _review_beside_withheld_files("assets/logo.png").response

    [dropped] = saved["dropped"]
    assert dropped["drop_reason"] != "ignored_file"
    assert saved["coverage"]["status"] == "partial"


def test_actual_budget_fit_flows_through_reflection_early_return_and_finalization() -> None:
    lines = tuple(DiffLine(i + 1, "added", f"line_{i} = {i}") for i in range(400))
    patch = "@@ -0,0 +1,400 @@\n" + "\n".join("+" + line.content for line in lines)
    snapshot = replace(
        code_snapshot(),
        patch=patch,
        review_patch=patch,
        status="added",
        additions=400,
        deletions=0,
        changes=400,
    )
    repository = Repository(coverage([snapshot]))
    committed: list[str] = []
    queue = Queue(committed)
    store = StoreReviewOutput(
        lambda: Uow(repository, committed), queue, worker_id="worker", run_url=lambda _: None
    )

    class Processor:
        async def prepare(self, run_id: UUID) -> Any:
            return conventions()

    class Prompts:
        async def get_review_prompt_input(self, run_id: UUID, saved: Any) -> ReviewPromptInput:
            return ReviewPromptInput(
                system="Review carefully.",
                rules=(),
                agents_md=None,
                conventions=RepoConventions((), ()),
                changed_files=(ChangedFile("src/app.py", "added", lines),),
                omitted_files=(),
                policy_snapshot=_make_test_snapshot(max_input_tokens=12_000),
            )

    class Model:
        async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
            return PullRequestMeta("PR", None, "u", "b", "m", (), 1, 400, 0, False, False)

    transport = Transport(json.dumps(answer()))
    model = GatewayReviewModel(_gateway(transport), RUN, Model())

    class Publisher:
        async def execute(
            self, run_id: UUID, raw_output: Mapping[str, object] | str | bytes
        ) -> bool:
            assert isinstance(raw_output, VerifiedReviewOutput)
            assert raw_output.incomplete_paths == ("src/app.py",)
            return await store.execute(run_id, raw_output)

    pipeline = ExecuteReviewRun(
        Processor(),
        Prompts(),
        model,
        Publisher(),
        reflection_pass=ReflectionPass(FakeVerifierModel({"decisions": []})),
    )
    assert asyncio.run(pipeline.execute(_RUN)) is True
    [request] = transport.requests
    shown = request.messages[1].content.count('type="added"')
    assert 0 < shown < 400
    assert "line_399" not in request.messages[1].content
    assert repository.response["coverage"]["status"] == "partial"
    assert repository.response["coverage"]["reviewed_files"] == 0
    assert repository.response["coverage"]["reason_counts"] == {"review_input_incomplete": 1}
    assert repository.response["verdict"] is None
    assert "Проанализировано файлов с текстовыми изменениями: 0" in repository.body
    assert "**Verdict:** clean" not in repository.body


@pytest.mark.parametrize("omit_allowed", [False, True])
def test_actual_fit_marks_whole_allowed_omission_but_not_auxiliary_omitted_names(
    omit_allowed: bool,
) -> None:
    captured: list[VerifiedReviewOutput] = []

    class Processor:
        async def prepare(self, run_id: UUID) -> Any:
            return conventions()

    class Prompts:
        async def get_review_prompt_input(self, run_id: UUID, saved: Any) -> ReviewPromptInput:
            files: tuple[ChangedFile, ...] = (
                ChangedFile("src/app.py", "added", (DiffLine(1, "added", "x = 1"),)),
            )
            if omit_allowed:
                files += (
                    ChangedFile("src/huge.py", "added", (DiffLine(1, "added", "x" * 50_000),)),
                )
            return ReviewPromptInput(
                system="Review carefully.",
                rules=(),
                agents_md=None,
                conventions=RepoConventions((), ()),
                changed_files=files,
                omitted_files=("auxiliary/context.py",),
                policy_snapshot=_make_test_snapshot(max_input_tokens=12_000),
            )

    class Model:
        async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
            return PullRequestMeta("PR", None, "u", "b", "m", (), 2, 2, 0, False, False)

    transport = Transport(json.dumps(answer()))
    model = GatewayReviewModel(_gateway(transport), RUN, Model())

    class Publisher:
        async def execute(self, run_id: UUID, raw_output: Any) -> bool:
            assert isinstance(raw_output, VerifiedReviewOutput)
            captured.append(raw_output)
            return True

    assert asyncio.run(
        ExecuteReviewRun(
            Processor(),
            Prompts(),
            model,
            Publisher(),
            reflection_pass=ReflectionPass(FakeVerifierModel({"decisions": []})),
        ).execute(_RUN)
    )
    [request] = transport.requests
    assert 'path="src/app.py"' in request.messages[1].content
    assert 'path="src/huge.py"' not in request.messages[1].content
    assert captured[0].incomplete_paths == (("src/huge.py",) if omit_allowed else ())


@pytest.mark.parametrize("supported", [False, True])
@pytest.mark.parametrize("reply_kind", ["mapping", "snapshot_reply"])
def test_model_boundary_loss_survives_both_reflection_returns_without_outer_fit_loss(
    supported: bool,
    reply_kind: str,
) -> None:
    repository = Repository(coverage([code_snapshot()]))
    committed: list[str] = []
    queue = Queue(committed)
    store = StoreReviewOutput(
        lambda: Uow(repository, committed), queue, worker_id="worker", run_url=lambda _: None
    )

    class Processor:
        async def prepare(self, run_id: UUID) -> Any:
            return conventions()

    class Prompts:
        async def get_review_prompt_input(self, run_id: UUID, saved: Any) -> ReviewPromptInput:
            return ReviewPromptInput(
                "Review carefully.",
                (),
                None,
                RepoConventions((), ()),
                (ChangedFile("src/app.py", "modified", (DiffLine(1, "added", "new"),)),),
                (),
                policy_snapshot=_make_test_snapshot(),
            )

    class Model:
        async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
            return PullRequestMeta("PR", None, "u", "b", "m", (), 1, 1, 1, False, False)

        async def draft_review(
            self, *, context: ReviewContext
        ) -> VerifiedReviewOutput | ReviewReply:
            assert context.changed_files[0].lines == (DiffLine(1, "added", "new"),)
            value = answer()
            if supported:
                item = finding("src/app.py", "info")
                item["evidence_refs"] = [
                    {"block_id": file_block_id("src/app.py"), "start_line": 1, "end_line": 1}
                ]
                value["findings"] = [item]
            carrier = VerifiedReviewOutput(value, incomplete_paths=("src/app.py",))
            if reply_kind == "snapshot_reply":
                return ReviewReply(
                    carrier,
                    context,
                    PromptBuilder().build_manifest(context, call_id=UUID(int=7)),
                )
            return carrier

    class Verifier:
        async def verify(
            self, *, system_prompt: str, user_prompt: str, **_: object
        ) -> dict[str, object]:
            ids = re.findall(r'candidate_id="([^"]+)"', user_prompt)
            return {
                "decisions": [
                    {
                        "candidate_id": cid,
                        "status": "supported",
                        "reason": "OK",
                        "evidence_refs": [
                            {
                                "block_id": file_block_id("src/app.py"),
                                "start_line": 1,
                                "end_line": 1,
                            }
                        ],
                        "requirement_refs": [],
                    }
                    for cid in ids
                ]
            }

    assert asyncio.run(
        ExecuteReviewRun(
            Processor(),
            Prompts(),
            Model(),
            store,
            reflection_pass=ReflectionPass(Verifier()),
        ).execute(_RUN)
    )
    assert repository.response["coverage"]["status"] == "partial"
    assert repository.response["coverage"]["reviewed_files"] == 0
    assert repository.response["coverage"]["reason_counts"] == {"review_input_incomplete": 1}
    assert repository.response["severity_counts"]["info"] == int(supported)
    assert repository.response["verdict"] is None
    assert "**Verdict:** clean" not in repository.body


@pytest.mark.parametrize("supported", [False, True])
def test_outer_context_loss_survives_both_reflection_returns_without_carrier_loss(
    supported: bool,
) -> None:
    repository = Repository(
        coverage([code_snapshot(), replace(code_snapshot(), filename="src/lost.py")])
    )
    committed: list[str] = []
    queue = Queue(committed)
    store = StoreReviewOutput(
        lambda: Uow(repository, committed), queue, worker_id="worker", run_url=lambda _: None
    )

    class Processor:
        async def prepare(self, run_id: UUID) -> Any:
            return conventions()

    class Prompts:
        async def get_review_prompt_input(self, run_id: UUID, saved: Any) -> ReviewPromptInput:
            return ReviewPromptInput(
                "Review carefully.",
                (),
                None,
                RepoConventions((), ()),
                (
                    ChangedFile("src/app.py", "modified", (DiffLine(1, "added", "new"),)),
                    ChangedFile("src/lost.py", "modified", (DiffLine(1, "added", "new"),)),
                ),
                (),
                policy_snapshot=_make_test_snapshot(),
            )

    class Model:
        async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
            return PullRequestMeta("PR", None, "u", "b", "m", (), 2, 2, 2, False, False)

        async def draft_review(self, *, context: ReviewContext) -> ReviewReply:
            assert context.changed_files[0].lines == (DiffLine(1, "added", "new"),)
            value = answer()
            if supported:
                item = finding("src/app.py", "high")
                item["evidence_refs"] = [
                    {"block_id": file_block_id("src/app.py"), "start_line": 1, "end_line": 1}
                ]
                value["findings"] = [item]
            fitted = replace(context, changed_files=context.changed_files[:1])
            carrier = VerifiedReviewOutput(value, incomplete_paths=())
            return ReviewReply(
                carrier,
                fitted,
                PromptBuilder().build_manifest(fitted, call_id=UUID(int=8)),
            )

    class Verifier:
        async def verify(
            self, *, system_prompt: str, user_prompt: str, **_: object
        ) -> dict[str, object]:
            ids = re.findall(r'candidate_id="([^"]+)"', user_prompt)
            return {
                "decisions": [
                    {
                        "candidate_id": cid,
                        "status": "supported",
                        "reason": "OK",
                        "evidence_refs": [
                            {
                                "block_id": file_block_id("src/app.py"),
                                "start_line": 1,
                                "end_line": 1,
                            }
                        ],
                        "requirement_refs": [],
                    }
                    for cid in ids
                ]
            }

    assert asyncio.run(
        ExecuteReviewRun(
            Processor(),
            Prompts(),
            Model(),
            store,
            reflection_pass=ReflectionPass(Verifier()),
        ).execute(_RUN)
    )
    assert repository.response["coverage"]["status"] == "partial"
    assert repository.response["coverage"]["reviewed_files"] == 1
    assert repository.response["coverage"]["reason_counts"] == {"review_input_incomplete": 1}
    assert repository.response["severity_counts"]["high"] == int(supported)
    assert repository.response["verdict"] == ("blocking" if supported else None)
    assert "**Verdict:** clean" not in repository.body


@pytest.mark.parametrize("loss", ["omitted", "prefix_cut", "original_truncated", "sent_truncated"])
def test_each_independent_patch_loss_withholds_reviewability(loss: str) -> None:
    from app.modules.reviews.application.review_output import incomplete_review_paths

    lines = (DiffLine(1, "added", "one"), DiffLine(2, "added", "two"))
    original = ChangedFile("src/app.py", "added", lines)
    sent = original
    shown: tuple[ChangedFile, ...]
    if loss == "omitted":
        shown = ()
    elif loss == "prefix_cut":
        sent = replace(sent, lines=lines[:1])
        shown = (sent,)
    elif loss == "original_truncated":
        original = replace(original, total_lines=3)
        shown = (sent,)
    else:
        shown = (replace(sent, total_lines=3),)
    assert incomplete_review_paths((original,), shown) == ("src/app.py",)
    assert incomplete_review_paths((sent,), (sent,)) == ()
