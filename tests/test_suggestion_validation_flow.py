"""End-to-end StoreReviewOutput & PublishReviewOutput suggestion validation tests (#444)."""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from types import TracebackType
from typing import Any
from uuid import UUID

import pytest

from app.modules.reviews.application.diff_map import DiffMap
from app.modules.reviews.application.findings_post_processor import ProcessedReviewOutput
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.get_run_file_lines import (
    BlobCache,
    BlobCacheKey,
)
from app.modules.reviews.application.review_output import (
    FindingPostProcessingInput,
    PublishedFinding,
    PublishReviewOutput,
    ReviewOutput,
    ReviewPublication,
    comment_body,
)
from app.modules.reviews.application.run_coverage import snapshot_vcs_file
from app.modules.reviews.application.store_review_output import (
    PublishingGuard,
    StoreReviewOutput,
)
from app.modules.reviews.infrastructure.blob_cache import InMemoryBlobCache
from app.modules.reviews.infrastructure.subprocess_source_parser import SubprocessSourceParser
from tests.findings_post_processor_fakes import _diff_map_from_vcs
from tests.verifier_pipeline_fakes import FakeQueue, InProcessSuggestionParser

_FIXTURES = Path(__file__).parent / "fixtures" / "swapi"
_RUN_ID = UUID("00000000-0000-4000-8000-000000000444")


def _make_parser(flags: list[bool]) -> InProcessSuggestionParser:
    def _assert_uow_closed(_reqs: object) -> None:
        assert all(not active for active in flags)

    return InProcessSuggestionParser(on_validate=_assert_uow_closed)


@dataclass
class _FlowRepo:
    context: FindingPostProcessingInput
    stored_processed: list[ProcessedReviewOutput] = field(default_factory=list)
    stored_outputs: list[ReviewOutput] = field(default_factory=list)
    marked_published: int = 0

    async def get_post_processing_input(self, run_id: UUID) -> FindingPostProcessingInput | None:
        return self.context

    async def lock_for_publishing(self, run_id: UUID) -> PublishingGuard:
        return PublishingGuard(
            state="running",
            worker_id="w1",
            cancel_requested=False,
            head_sha="c" * 40,
            head_current=True,
            repository_review_event="COMMENT",
        )

    async def store_review_output(
        self,
        run_id: UUID,
        raw_output: dict[str, Any] | None,
        parsed: ReviewOutput,
        processed: ProcessedReviewOutput,
    ) -> ReviewPublication:
        self.stored_outputs.append(parsed)
        self.stored_processed.append(processed)
        published_findings = tuple(
            PublishedFinding(
                path=item.finding.path,
                line=item.finding.line,
                start_line=item.finding.start_line,
                severity=item.finding.severity,
                category=item.finding.category,
                title=item.finding.title,
                body=item.finding.body,
                suggestion=item.finding.suggestion,
                confidence=item.finding.confidence,
                rule_name=item.finding.rule_name,
            )
            for item in processed.inline
        )
        return ReviewPublication(
            head_sha="c" * 40,
            findings=published_findings,
            review_body=processed.review_body,
        )

    async def enter_publishing(self, run_id: UUID, **kwargs: Any) -> None:
        return None

    async def try_claim_publish_lease(self, run_id: UUID) -> bool:
        return True

    async def mark_review_published(self, run_id: UUID) -> None:
        self.marked_published += 1


class _FlowUow:
    def __init__(self, repo: _FlowRepo, cache: BlobCache | None, active_flags: list[bool]) -> None:
        self.reviews = repo
        self._cache = cache
        self._flags = active_flags
        self._idx = len(active_flags)
        active_flags.append(False)

    @property
    def blob_cache(self) -> BlobCache | None:
        return self._cache

    async def __aenter__(self) -> _FlowUow:
        self._flags[self._idx] = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._flags[self._idx] = False

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class _Provider:
    def __init__(self, active_flags: list[bool]) -> None:
        self._flags = active_flags
        self.calls: list[tuple[PublishedFinding, ...]] = []

    async def publish_review(
        self,
        *,
        commit_sha: str,
        body: str,
        findings: tuple[PublishedFinding, ...],
        idempotency_key: str,
    ) -> None:
        assert all(not active for active in self._flags)
        self.calls.append(findings)


def _swapi_patch(path: str, status: str, lines: int, start: int = 1) -> DiffSnapshot:
    hunk_start = 1 if status == "added" else start
    hunk_lines = (start + lines - 1) if status == "added" else lines
    body = "\n".join(f"+l{i}" for i in range(hunk_start, hunk_start + hunk_lines))
    old_part = "0,0" if status == "added" else f"{start - 1},0"
    patch = f"@@ -{old_part} +{hunk_start},{hunk_lines} @@\n{body}"
    return DiffSnapshot(
        path,
        patch,
        "1" * 40,
        status=status,  # type: ignore[arg-type]
        additions=hunk_lines,
        deletions=0,
        changes=hunk_lines,
        review_patch=patch,
    )


def _make_context(
    path: str, status: str, start_anchor: int, line_count: int
) -> tuple[FindingPostProcessingInput, DiffMap, InMemoryBlobCache]:
    snap = _swapi_patch(path, status, line_count, start=start_anchor)
    diff_map = _diff_map_from_vcs(snapshot_vcs_file(snap))
    ctx = FindingPostProcessingInput(
        diff_map=diff_map,
        checked_paths=frozenset({path}),
        rule_names=frozenset(),
    )
    return ctx, diff_map, InMemoryBlobCache()


_SWAPI_EXAMPLES = [
    pytest.param(
        "src/tools/starships.ts",
        "added",
        "starships.ts",
        None,
        31,
        (
            "      id: z.union([z.number().int().positive(), z.string()]).describe("
            '"Starship resource ID or full SWAPI starship URL"),'
        ),
        "syntax_error",
        id="starships_ts_added_l31",
    ),
    pytest.param(
        "src/tools/people.ts",
        "added",
        "people.ts",
        None,
        31,
        (
            "      id: z.union([z.number().int().positive(), z.string()]).describe("
            '"Character resource ID or full SWAPI character URL"),'
        ),
        "syntax_error",
        id="people_ts_added_l31",
    ),
    pytest.param(
        "src/tools/planets.ts",
        "added",
        "planets.ts",
        None,
        31,
        (
            "      id: z.union([z.number().int().positive(), z.string()]).describe("
            '"Planet resource ID or full SWAPI planet URL"),'
        ),
        "syntax_error",
        id="planets_ts_added_l31",
    ),
    pytest.param(
        "src/tools/films.ts",
        "modified",
        "films_8bc8b5d.ts",
        93,
        94,
        (
            "      if (filmId === undefined && episodeId === undefined) {\n"
            "        throw new McpError(\n"
            "          ErrorCode.InvalidParams,\n"
            "          \"Provide exactly one of 'film_id' or 'episode_id' to identify"
            ' the episode.",\n'
            "        );\n"
            "      }"
        ),
        "syntax_error",
        id="films_ts_modified_8bc8b5d_l93_94",
    ),
    pytest.param(
        "src/tools/films.ts",
        "modified",
        "films_858e1c1.ts",
        None,
        96,
        'import { McpError, ErrorCode } from "@modelcontextprotocol/sdk/server/mcp.js";',
        "syntax_error",
        id="films_ts_modified_858e1c1_l96",
    ),
    pytest.param(
        "src/tools/common.ts",
        "added",
        "common.ts",
        None,
        17,
        (
            "  if (!(error instanceof ApiError)) {\n"
            "    return {\n"
            "      isError: true,\n"
            '      content: [{ type: "text" as const, text: `Internal error: '
            "${String(error)}` }],\n"
            "    };\n"
            "  }"
        ),
        "duplicate_condition",
        id="common_ts_added_l17_duplicate_condition",
    ),
]


@pytest.mark.parametrize("use_case_kind", ["store", "publish"])
@pytest.mark.parametrize(
    ("path", "status", "fixture_name", "start_line", "line", "suggestion", "expected_reason"),
    _SWAPI_EXAMPLES,
)
def test_swapi_pr3_broken_suggestions_stripped_through_both_use_cases(
    caplog: pytest.LogCaptureFixture,
    use_case_kind: str,
    path: str,
    status: str,
    fixture_name: str,
    start_line: int | None,
    line: int,
    suggestion: str,
    expected_reason: str,
) -> None:
    anchor_start = start_line or line
    span = line - anchor_start + 1
    ctx, diff_map, cache = _make_context(path, status, anchor_start, span)
    source = (_FIXTURES / fixture_name).read_text(encoding="utf-8")
    asyncio.run(
        cache.put(BlobCacheKey(diff_map.repository_id, "1" * 40), source, ttl=timedelta(hours=1))
    )

    payload = {
        "findings": [
            {
                "path": path,
                "start_line": start_line,
                "line": line,
                "severity": "high",
                "category": "correctness",
                "title": "SWAPI issue",
                "body": "Original comment prose stays intact.",
                "suggestion": suggestion,
                "confidence": 0.95,
                "rule_name": None,
            }
        ],
        "summary": {"problem": "One issue.", "done_well": "Clean.", "effort": "small"},
    }

    flags: list[bool] = []
    repo = _FlowRepo(ctx)
    parser = _make_parser(flags)
    provider = _Provider(flags)

    with caplog.at_level(logging.INFO):
        if use_case_kind == "store":
            queue = FakeQueue()
            ok = asyncio.run(
                StoreReviewOutput(
                    lambda: _FlowUow(repo, cache, flags),
                    queue,
                    worker_id="w1",
                    run_url=lambda _: None,
                    parser=parser,
                ).execute(_RUN_ID, payload)
            )
            assert ok is True
            assert len(repo.stored_processed) == 1
            f = repo.stored_processed[0].inline[0].finding
            assert f.suggestion is None
            rendered = comment_body(f.title, f.body, f.suggestion)
        else:
            ok = asyncio.run(
                PublishReviewOutput(
                    lambda: _FlowUow(repo, cache, flags),
                    provider,
                    parser=parser,
                ).execute(_RUN_ID, payload)
            )
            assert ok is True
            published = provider.calls[0][0]
            assert published.suggestion is None
            rendered = comment_body(published.title, published.body, published.suggestion)

    expected_units = 2 if use_case_kind == "store" else 3
    assert len(flags) == expected_units
    assert all(not active for active in flags)
    assert parser.closed is True
    assert "**SWAPI issue**" in rendered
    assert "Original comment prose stays intact." in rendered
    assert "```suggestion" not in rendered
    assert any(
        f"run_id={_RUN_ID}" in r.getMessage() and f"reason={expected_reason}" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.parametrize("use_case_kind", ["store", "publish"])
def test_valid_multiline_suggestion_on_modified_file_reaches_comment_body(
    use_case_kind: str,
) -> None:
    path = "src/tools/films.ts"
    ctx, diff_map, cache = _make_context(path, "modified", 96, 6)
    source = (_FIXTURES / "films_858e1c1.ts").read_text(encoding="utf-8")
    asyncio.run(
        cache.put(BlobCacheKey(diff_map.repository_id, "1" * 40), source, ttl=timedelta(hours=1))
    )

    valid_multiline = (
        "      if (hasFilmId === hasEpisodeId) {\n"
        "        throw new McpError(\n"
        "          ErrorCode.InvalidParams,\n"
        "          \"Pass either 'film_id' or 'episode_id', never both.\",\n"
        "        );\n"
        "      }"
    )
    payload = {
        "findings": [
            {
                "path": path,
                "start_line": 96,
                "line": 101,
                "severity": "medium",
                "category": "readability",
                "title": "Simplify mutually exclusive check",
                "body": "Use direct equality check.",
                "suggestion": valid_multiline,
                "confidence": 0.9,
                "rule_name": None,
            }
        ],
        "summary": {"problem": "Minor clarity.", "done_well": "Good.", "effort": "small"},
    }

    flags: list[bool] = []
    repo = _FlowRepo(ctx)
    parser = _make_parser(flags)
    provider = _Provider(flags)

    if use_case_kind == "store":
        asyncio.run(
            StoreReviewOutput(
                lambda: _FlowUow(repo, cache, flags),
                FakeQueue(),
                worker_id="w1",
                run_url=lambda _: None,
                parser=parser,
            ).execute(_RUN_ID, payload)
        )
        f = repo.stored_processed[0].inline[0].finding
        assert f.suggestion == valid_multiline
        rendered = comment_body(f.title, f.body, f.suggestion)
    else:
        asyncio.run(
            PublishReviewOutput(
                lambda: _FlowUow(repo, cache, flags),
                provider,
                parser=parser,
            ).execute(_RUN_ID, payload)
        )
        published = provider.calls[0][0]
        assert published.suggestion == valid_multiline
        rendered = comment_body(published.title, published.body, published.suggestion)

    assert f"```suggestion\n{valid_multiline}\n```" in rendered
    assert parser.closed is True


@pytest.mark.parametrize("use_case_kind", ["store", "publish"])
def test_cache_failure_fails_open_without_aborting_run(
    caplog: pytest.LogCaptureFixture, use_case_kind: str
) -> None:
    path = "src/tools/films.ts"
    ctx, _, _ = _make_context(path, "modified", 96, 1)

    class _ExplodingCache:
        async def get(self, key: BlobCacheKey) -> Any:
            raise TimeoutError("cache unavailable")

    flags: list[bool] = []
    repo = _FlowRepo(ctx)
    parser = _make_parser(flags)
    payload = {
        "findings": [
            {
                "path": path,
                "start_line": None,
                "line": 96,
                "severity": "medium",
                "category": "correctness",
                "title": "Note",
                "body": "Body.",
                "suggestion": "const x = 1;",
                "confidence": 0.9,
                "rule_name": None,
            }
        ],
        "summary": {"problem": "P.", "done_well": "D.", "effort": "small"},
    }

    with caplog.at_level(logging.INFO):
        if use_case_kind == "store":
            ok = asyncio.run(
                StoreReviewOutput(
                    lambda: _FlowUow(repo, _ExplodingCache(), flags),
                    FakeQueue(),
                    worker_id="w1",
                    run_url=lambda _: None,
                    parser=parser,
                ).execute(_RUN_ID, payload)
            )
            assert ok is True
            assert repo.stored_processed[0].inline[0].finding.suggestion == "const x = 1;"
        else:
            provider = _Provider(flags)
            ok = asyncio.run(
                PublishReviewOutput(
                    lambda: _FlowUow(repo, _ExplodingCache(), flags),
                    provider,
                    parser=parser,
                ).execute(_RUN_ID, payload)
            )
            assert ok is True
            assert provider.calls[0][0].suggestion == "const x = 1;"

    assert parser.closed is True
    assert parser.seen_requests == []
    assert any("reason=file_unavailable" in r.getMessage() for r in caplog.records)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="ast_worker uses POSIX resource")
def test_real_subprocess_parser_strips_broken_suggestion() -> None:
    path = "src/tools/films.ts"
    ctx, diff_map, cache = _make_context(path, "modified", 96, 1)
    source = (_FIXTURES / "films_858e1c1.ts").read_text(encoding="utf-8")
    asyncio.run(
        cache.put(BlobCacheKey(diff_map.repository_id, "1" * 40), source, ttl=timedelta(hours=1))
    )
    flags: list[bool] = []
    repo = _FlowRepo(ctx)
    payload = {
        "findings": [
            {
                "path": path,
                "start_line": None,
                "line": 96,
                "severity": "high",
                "category": "correctness",
                "title": "Missing import",
                "body": "Move import to top level.",
                "suggestion": (
                    'import { McpError, ErrorCode } from "@modelcontextprotocol/sdk/server/mcp.js";'
                ),
                "confidence": 0.9,
                "rule_name": None,
            }
        ],
        "summary": {"problem": "P.", "done_well": "D.", "effort": "small"},
    }
    ok = asyncio.run(
        StoreReviewOutput(
            lambda: _FlowUow(repo, cache, flags),
            FakeQueue(),
            worker_id="w1",
            run_url=lambda _: None,
            parser=SubprocessSourceParser(),
        ).execute(_RUN_ID, payload)
    )
    assert ok is True
    assert repo.stored_processed[0].inline[0].finding.suggestion is None
