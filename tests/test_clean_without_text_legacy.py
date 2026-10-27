"""The supported inline publisher uses the same honest result finalization."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID

import pytest

from app.modules.reviews.application.findings_post_processor import ProcessedReviewOutput
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.post_processing_diff_map import diff_map_from_snapshots
from app.modules.reviews.application.review_output import (
    FindingPostProcessingInput,
    PublishReviewOutput,
    ReviewOutput,
    ReviewPublication,
)
from app.modules.reviews.application.run_coverage import RunCoverage
from tests.test_clean_without_text_result import answer
from tests.test_review_output import RecordingFactory, RecordingProvider, RecordingRepository
from tests.test_run_coverage import coverage


@pytest.mark.parametrize("no_text", [False, True])
def test_legacy_inline_default_unknown_and_none_have_matching_body_labels(no_text: bool) -> None:
    snapshots = [DiffSnapshot("large.txt", None, omission_reason="too_large")]
    trusted: RunCoverage | None = coverage(snapshots) if no_text else None
    diff = diff_map_from_snapshots(
        repository_id=UUID(int=1),
        code_change_number=7,
        head_sha="a" * 40,
        base_sha="b" * 40,
        diff_old_sha="c" * 40,
        snapshots=snapshots,
    )
    stored: list[ProcessedReviewOutput] = []

    @dataclass
    class Repository(RecordingRepository):
        async def store_review_output(
            self,
            run_id: UUID,
            model_output: dict[str, object],
            parsed: ReviewOutput,
            processed: ProcessedReviewOutput,
        ) -> ReviewPublication:
            stored.append(processed)
            assert processed.summary == parsed.summary
            assert model_output["summary"] == answer()["summary"]
            return ReviewPublication("a" * 40, (), processed.review_body)

    repository = Repository(
        FindingPostProcessingInput(diff, frozenset(), frozenset(), coverage=trusted), None
    )
    factory = RecordingFactory(repository)
    provider = RecordingProvider()
    assert asyncio.run(
        PublishReviewOutput(factory, provider).execute(
            UUID("00000000-0000-0000-0000-000000000408"), answer()
        )
    )
    label = "нет доступных для анализа изменений" if no_text else "проверка неполная"
    assert f"**Verdict:** {label}" in provider.calls[0][1]
    assert "**Verdict:** clean" not in provider.calls[0][1]
    assert "All changes were checked" not in provider.calls[0][1]
    assert stored[0].coverage is not None
    assert stored[0].coverage.status.value == ("none" if no_text else "partial")


def test_direct_postprocessor_without_coverage_withholds_clean() -> None:
    from app.modules.reviews.application.findings_post_processor import FindingsPostProcessor
    from app.modules.reviews.application.review_output import parse_review_output
    from tests.test_run_coverage import code_snapshot

    diff = diff_map_from_snapshots(
        repository_id=UUID(int=1),
        code_change_number=7,
        head_sha="a" * 40,
        base_sha="b" * 40,
        diff_old_sha="c" * 40,
        snapshots=[code_snapshot()],
    )
    result = FindingsPostProcessor.from_default_patterns().process(
        parse_review_output(answer()),
        diff_map=diff,
        checked_paths=frozenset({"src/app.py"}),
        rule_names=frozenset(),
    )
    assert result.coverage is not None
    assert result.coverage.status.value == "partial"
    assert result.coverage.reason_counts == {"classification_unavailable": 1}
    assert result.summary is not None
    assert (
        result.summary.problem == "Подтверждённых замечаний нет; Проверка неполная: "
        "нет достоверного сохранённого покрытия (1)."
    )
    assert "**Verdict:** проверка неполная" in result.review_body
    assert "**Verdict:** clean" not in result.review_body


def test_queue_store_rejects_missing_backend_summary_before_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.modules.reviews.application.findings_post_processor import FindingsPostProcessor
    from app.modules.reviews.application.review_output import InvalidReviewOutput
    from app.modules.reviews.application.store_review_output import StoreReviewOutput
    from tests.test_clean_without_text_result import Queue, Repository, Uow
    from tests.test_run_coverage import code_snapshot

    result = ProcessedReviewOutput(
        (), (), (), "body", summary=None, coverage=coverage([code_snapshot()])
    )
    monkeypatch.setattr(FindingsPostProcessor, "process", lambda *a, **kw: result)
    repository = Repository(coverage([code_snapshot()]))
    committed: list[str] = []
    queue = Queue(committed)
    store = StoreReviewOutput(
        lambda: Uow(repository, committed), queue, worker_id="worker", run_url=lambda _: None
    )
    with pytest.raises(InvalidReviewOutput, match="Missing backend summary for run_id="):
        asyncio.run(store.execute(UUID(int=213), answer()))
    assert repository.response == {}
    assert repository.state == "running"
    assert committed == []
    assert queue.pointers == []
