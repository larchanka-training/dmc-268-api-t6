"""The #170 body packer retains #213 finalized coverage and safe path disclosure."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from uuid import UUID

import pytest

from app.modules.reviews.application.findings_post_processor import ProcessedReviewOutput
from app.modules.reviews.application.review_output import (
    PublishReviewOutput,
    ReviewOutput,
    ReviewPublication,
    VerifiedReviewOutput,
)
from app.modules.reviews.application.run_coverage import RunCoverage
from app.modules.reviews.application.store_review_output import StoreReviewOutput
from tests.test_clean_without_text_result import Queue, Repository, Uow, answer
from tests.test_review_output import RecordingFactory, RecordingProvider, RecordingRepository
from tests.test_run_coverage import code_snapshot, coverage

_RUN = UUID(int=213)


def overflowing_answer() -> VerifiedReviewOutput:
    value = answer()
    value["findings"] = [
        {
            "path": "src/app.py",
            "line": 1,
            "start_line": None,
            "severity": "info",
            "category": "correctness",
            "title": f"Note {index}",
            "body": f"Observation {index}: " + "x" * 1150,
            "suggestion": None,
            "confidence": 0.4,
            "rule_name": None,
        }
        for index in range(6)
    ]
    return VerifiedReviewOutput(value, incomplete_paths=("src/app.py",))


@pytest.mark.parametrize("inline", [False, True])
def test_body_budget_preserves_partial_backend_result_and_accounts_for_all_findings(
    inline: bool,
) -> None:
    stored: list[ProcessedReviewOutput] = []
    trusted = coverage([code_snapshot()])
    raw = overflowing_answer()
    run_url = "https://ui.test/runs/" + "r" * 1800
    if inline:

        @dataclass
        class InlineRepository(RecordingRepository):
            async def store_review_output(
                self,
                run_id: UUID,
                model_output: dict[str, object],
                parsed: ReviewOutput,
                processed: ProcessedReviewOutput,
            ) -> ReviewPublication:
                stored.append(processed)
                assert parsed.summary == processed.summary
                return ReviewPublication("a" * 40, (), processed.review_body)

        context = asyncio.run(Repository(trusted).get_post_processing_input(_RUN))
        repository = InlineRepository(context, None)
        provider = RecordingProvider()
        assert asyncio.run(
            PublishReviewOutput(RecordingFactory(repository), provider).execute(UUID(int=1032), raw)
        )
        result = stored[0]
        body = provider.calls[0][1]
        assert body.endswith(f"**Ещё {result.omitted_body_only_count} находки**")
    else:

        class QueueRepository(Repository):
            async def store_review_output(
                self,
                run_id: UUID,
                model_output: dict[str, object],
                parsed: ReviewOutput,
                processed: ProcessedReviewOutput,
            ) -> ReviewPublication:
                stored.append(processed)
                assert parsed.summary == processed.summary
                return await super().store_review_output(run_id, model_output, parsed, processed)

        queued = QueueRepository(trusted)
        commits: list[str] = []
        queue = Queue(commits)
        store = StoreReviewOutput(
            lambda: Uow(queued, commits), queue, worker_id="worker", run_url=lambda _: run_url
        )
        assert asyncio.run(store.execute(_RUN, raw))
        result = stored[0]
        body = queued.body
        assert body.endswith(run_url)
        assert queued.response["coverage"]["status"] == "partial"
        assert queued.response["coverage"]["reviewed_files"] == 0
        assert queued.response["verdict"] is None
        assert queued.response["summary"]["problem"] == (
            "Подтверждено замечаний: 6 (correctness: 6); Проверка неполная: "
            "модель получила не весь patch (1)."
        )
        assert queued.response["omitted_body_only_count"] == result.omitted_body_only_count
        assert queue.pointers[0].review_event == "COMMENT"
    assert len(body) <= 4000
    assert result.coverage is not None
    assert result.coverage.status.value == "partial"
    assert result.coverage.reason_counts == {"review_input_incomplete": 1}
    assert result.summary is not None
    assert "модель получила не весь patch" in result.summary.problem
    assert "All changes were checked" not in body
    assert "**Verdict:** проверка неполная" in body
    assert "**Проверено файлов с текстовыми изменениями:** 0" in body
    assert "**Checked files:**\n- src/app.py" in body
    shown = sum(line.startswith("- **Note ") for line in body.splitlines())
    assert shown + result.omitted_body_only_count == 6
    assert result.omitted_body_only_count > 0
    for line in body.splitlines():
        if line.startswith("- **Note "):
            assert line.endswith("x" * 1150)


@pytest.mark.parametrize("private_scope", ["unsupported", "ignored", "unknown"])
def test_author_path_list_never_discloses_ignored_unsupported_or_unknown_scope(
    private_scope: str,
) -> None:
    from app.modules.reviews.application.findings_post_processor import FindingsPostProcessor
    from app.modules.reviews.application.review_output import parse_review_output

    secret = "../private-token.py" if private_scope == "unsupported" else "private/secret.py"
    snapshots = [code_snapshot(), replace(code_snapshot(), filename="private/secret.py")]
    trusted: RunCoverage | None = coverage(
        snapshots,
        rules="```review-ignore\nprivate/**\n```\n" if private_scope == "ignored" else "",
    )
    if private_scope == "unknown":
        trusted = None
    context = asyncio.run(Repository(trusted).get_post_processing_input(_RUN))
    result = FindingsPostProcessor.from_default_patterns().process(
        parse_review_output(answer()),
        diff_map=context.diff_map,
        checked_paths=frozenset({"src/app.py", secret}),
        rule_names=frozenset(),
        coverage=trusted,
    )
    assert secret not in result.review_body
    assert "private-token" not in result.review_body
    if private_scope != "unsupported":
        assert "**Checked files:**\n(none)" in result.review_body
    else:
        assert "**Checked files:**\n- src/app.py" in result.review_body
