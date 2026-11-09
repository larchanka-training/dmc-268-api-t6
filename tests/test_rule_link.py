"""The link from a GitHub comment to the ``.review/rules.md`` lines of its rule (#465)."""

from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from decimal import Decimal
from typing import Any, Literal, cast
from uuid import UUID

import pytest

from app.common.infrastructure.db.enums import FindingCategory, FindingSeverity
from app.modules.reviews.application.conventions import GeneratedConventions
from app.modules.reviews.application.execute_review import ExecuteReviewRun, ReviewPromptInput
from app.modules.reviews.application.findings_post_processor import ProcessedReviewOutput
from app.modules.reviews.application.prompt_builder import (
    ChangedFile,
    DiffLine,
    PullRequestMeta,
    RepoConventions,
    ReviewContext,
)
from app.modules.reviews.application.reflection_pass import (
    CandidateFindingItem,
    ReflectionPass,
    VerifierReply,
)
from app.modules.reviews.application.review_output import (
    EvidenceRef,
    FindingPostProcessingInput,
    ReviewFindingV2,
    ReviewOutput,
    ReviewPublication,
    RuleLineRef,
    VerifiedReviewOutput,
)
from app.modules.reviews.application.store_review_output import (
    PublishingGuard,
    StoreReviewOutput,
)
from app.modules.reviews.application.verdict import HashedFinding, findings_hash
from app.modules.reviews.infrastructure.models import Finding
from app.modules.reviews.infrastructure.publication_findings import published_finding_from_row
from tests.findings_post_processor_fakes import _added_file_diff_map
from tests.policy_snapshot_fakes import make_policy_document_present
from tests.verifier_pipeline_fakes import (
    MAIN_BLOCK_ID,
    RUN_ID,
    SUMMARY,
    FakeModel,
    FakeProcessor,
    FakePublisher,
    FakeQueue,
    FakeRepository,
    FakeUnitOfWork,
    verifier_snapshot,
)

_LINK = (
    "https://github.com/acme/widgets/blob/"
    "1111111111111111111111111111111111111111/.review/rules.md?plain=1#L5-L8"
)


def _hashed(rule_link: str | None = None) -> HashedFinding:
    return HashedFinding(
        path="a.py",
        line_start=3,
        line_end=None,
        severity="high",
        category="correctness",
        title="T",
        body="B",
        suggestion=None,
        inline=True,
        rule_link=rule_link,
    )


def test_a_finding_without_a_link_keeps_its_hash() -> None:
    # Taken from main before the field existed: the pointer of a run already in publishing
    # at the deploy must still match its rows.
    assert findings_hash([_hashed()]) == (
        "190d59b987ba190b4c643c18d98a69f0f12c73f5ee0420ee87a317c807d4ebb1"
    )


def test_a_new_link_makes_a_new_hash() -> None:
    # The marker is looked up across the whole pull request: with the same hash, a run whose
    # comment text differs would take an earlier review for its own.
    other = _LINK.replace("#L5-L8", "#L5-L9")

    assert (
        len(
            {
                findings_hash([_hashed()]),
                findings_hash([_hashed(_LINK)]),
                findings_hash([_hashed(other)]),
            }
        )
        == 3
    )


_REVISION = "b" * 40
_SNAPSHOT = replace(
    verifier_snapshot(),
    rules=make_policy_document_present(
        path=".review/rules.md", text="# Rules\nSanitize.\nValidate.\nLog.\n", blob_oid="r" * 40
    ),
    agents=make_policy_document_present(
        path="AGENTS.md", text="# Agents\nUse uv.\n", blob_oid="a" * 40
    ),
)
_CHANGED = ChangedFile(
    "app/main.py", "modified", (DiffLine(10, "added", "x = 1"), DiffLine(11, "added", "y = 2"))
)


def _ref(block_id: str, start_line: int, end_line: int) -> dict[str, Any]:
    return {"block_id": block_id, "start_line": start_line, "end_line": end_line, "check_id": None}


class _Verifier:
    """Answers each candidate in prompt order with its ``(status, requirement_refs)``."""

    def __init__(self, *answers: tuple[str, list[dict[str, Any]]]) -> None:
        self._answers = answers

    async def verify(self, *, system_prompt: str, user_prompt: str, **_: object) -> VerifierReply:
        ids = re.findall(r'candidate candidate_id="([^"]+)"', user_prompt)
        return VerifierReply(
            output={
                "decisions": [
                    {
                        "candidate_id": candidate_id,
                        "status": status,
                        "reason": "Checked.",
                        "evidence_refs": [
                            {"block_id": MAIN_BLOCK_ID, "start_line": 10, "end_line": 10}
                        ],
                        "requirement_refs": refs,
                    }
                    for candidate_id, (status, refs) in zip(ids, self._answers, strict=True)
                ]
            }
        )


def _candidate(rule_name: str | None) -> CandidateFindingItem:
    return CandidateFindingItem(
        candidate_id="c-1",
        finding=ReviewFindingV2(
            path="app/main.py",
            line=10,
            start_line=None,
            severity="high",
            category="correctness",
            title="Wrong value",
            body="Unsanitized input.",
            suggestion=None,
            confidence=0.9,
            rule_name=rule_name,
            evidence_refs=[EvidenceRef(block_id=MAIN_BLOCK_ID, start_line=10, end_line=10)],
        ),
    )


@pytest.mark.parametrize(
    ("rule_name", "refs", "expected"),
    [
        pytest.param(
            ".review/rules.md",
            [_ref("policy-rules", 2, 3)],
            RuleLineRef(_REVISION, 2, 3),
            id="the-rules-block",
        ),
        pytest.param(
            ".review/rules.md",
            [_ref("policy-agents", 1, 2), _ref("policy-rules", 3, 3), _ref("policy-rules", 1, 1)],
            RuleLineRef(_REVISION, 3, 3),
            id="the-first-rules-ref-after-another-source",
        ),
        pytest.param("AGENTS.md", [_ref("policy-agents", 1, 2)], None, id="another-source"),
        pytest.param(None, [], None, id="no-requirement"),
    ],
)
def test_the_record_names_the_rules_lines_the_verifier_cited(
    rule_name: str | None, refs: list[dict[str, Any]], expected: RuleLineRef | None
) -> None:
    context = ReviewContext(
        system="Review.",
        rules=(),
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=PullRequestMeta("Title", None, "u", "b", "m", (), 1, 1, 0, False, False),
        changed_files=(_CHANGED,),
        omitted_files=(),
        policy_snapshot=_SNAPSHOT,
    )

    record = asyncio.run(
        ReflectionPass(_Verifier(("supported", refs))).verify_recorded(
            candidates=(_candidate(rule_name),), context=context
        )
    )

    assert record.rule_line_refs == ({} if expected is None else {"c-1": expected})


class _SnapshotRepository(FakeRepository):
    def __init__(self, severity_threshold: Literal["all", "warning", "critical"]) -> None:
        self._severity_threshold = severity_threshold

    async def get_review_prompt_input(
        self, run_id: UUID, saved: GeneratedConventions
    ) -> ReviewPromptInput:
        prompt_input = await super().get_review_prompt_input(run_id, saved)
        return replace(
            prompt_input, policy_snapshot=_SNAPSHOT, severity_threshold=self._severity_threshold
        )


def _model_finding(line: int, title: str, severity: str = "critical") -> dict[str, Any]:
    return {
        "path": "app/main.py",
        "line": line,
        "start_line": None,
        "severity": severity,
        "category": "security",
        "title": title,
        "body": "Unsanitized input.",
        "suggestion": None,
        "confidence": 0.9,
        "rule_name": ".review/rules.md",
        "evidence_refs": [{"block_id": MAIN_BLOCK_ID, "start_line": line, "end_line": line}],
    }


def _executed(
    findings: list[dict[str, Any]],
    verifier: _Verifier,
    *,
    severity_threshold: Literal["all", "warning", "critical"] = "all",
) -> VerifiedReviewOutput:
    publisher = FakePublisher()
    pipeline = ExecuteReviewRun(
        FakeProcessor(),
        _SnapshotRepository(severity_threshold),
        FakeModel(findings),
        publisher,
        reflection_pass=ReflectionPass(verifier),
    )
    assert asyncio.run(pipeline.execute(RUN_ID)) is True
    output = publisher.outputs[0]
    assert isinstance(output, VerifiedReviewOutput)
    return output


def test_the_lines_stay_with_their_finding_when_an_earlier_one_is_rejected() -> None:
    output = _executed(
        [_model_finding(10, "Rejected"), _model_finding(11, "Kept")],
        _Verifier(
            ("contradicted", [_ref("policy-rules", 1, 1)]),
            ("supported", [_ref("policy-rules", 2, 3)]),
        ),
    )

    assert [item["title"] for item in output["findings"]] == ["Kept"]  # type: ignore[attr-defined]
    assert output.rule_line_refs == {0: RuleLineRef(_REVISION, 2, 3)}


def test_the_lines_stay_with_their_finding_when_the_threshold_drops_an_earlier_one() -> None:
    output = _executed(
        [_model_finding(10, "Below threshold", "medium"), _model_finding(11, "Kept")],
        _Verifier(
            ("supported", [_ref("policy-rules", 1, 1)]),
            ("supported", [_ref("policy-rules", 2, 3)]),
        ),
        severity_threshold="critical",
    )

    assert [item["title"] for item in output["findings"]] == ["Kept"]  # type: ignore[attr-defined]
    assert output.rule_line_refs == {0: RuleLineRef(_REVISION, 2, 3)}


_FINDING_ID = UUID("00000000-0000-0000-0000-000000046501")
_RULES = ".review/rules.md"
_V2_RULES = frozenset({_RULES, "AGENTS.md"})
_LINES = RuleLineRef(_REVISION, 5, 8)
_REPOSITORY = "acme/widgets"
_LINES_URL = (
    "https://github.com/acme/widgets/blob/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    "/.review/rules.md?plain=1#L5-L8"
)


class _StoringRepository:
    def __init__(self, repository: str | None, max_inline: int | None) -> None:
        self._repository = repository
        self._max_inline = max_inline
        self.processed: ProcessedReviewOutput | None = None
        self.findings_hash: str | None = None

    async def get_post_processing_input(self, run_id: UUID) -> FindingPostProcessingInput:
        return FindingPostProcessingInput(
            diff_map=_added_file_diff_map("app/main.py", 20),
            checked_paths=frozenset({"app/main.py"}),
            rule_names=frozenset(),
            repository_max_inline=self._max_inline,
            github_repository_full_name=self._repository,
        )

    async def lock_for_publishing(self, run_id: UUID) -> PublishingGuard:
        return PublishingGuard(
            state="running",
            worker_id="test-worker",
            cancel_requested=False,
            head_sha="a" * 40,
            head_current=True,
            repository_review_event="COMMENT",
        )

    async def store_review_output(
        self,
        run_id: UUID,
        answer: dict[str, Any],
        parsed: ReviewOutput,
        processed: ProcessedReviewOutput,
    ) -> ReviewPublication:
        self.processed = processed
        return ReviewPublication(head_sha="a" * 40, findings=(), review_body="body")

    async def enter_publishing(self, run_id: UUID, **kwargs: Any) -> None:
        self.findings_hash = kwargs["postprocess_response"]["findings_hash"]


def _stored(
    findings: list[dict[str, Any]],
    rule_line_refs: dict[int, RuleLineRef],
    *,
    repository: str | None = _REPOSITORY,
    allowed_v2_rules: frozenset[str] = _V2_RULES,
    max_inline: int | None = None,
) -> _StoringRepository:
    reviews = _StoringRepository(repository, max_inline)
    store = StoreReviewOutput(
        cast(Any, lambda: FakeUnitOfWork(reviews)),
        FakeQueue(),
        worker_id="test-worker",
        run_url=lambda _: None,
    )
    output = VerifiedReviewOutput(
        {"findings": findings, "summary": SUMMARY},
        allowed_v2_rules=allowed_v2_rules,
        severity_threshold="all",
        rule_line_refs=rule_line_refs,
    )
    assert asyncio.run(store.execute(RUN_ID, output)) is True
    assert reviews.processed is not None
    return reviews


def test_an_inline_finding_on_the_rules_file_gets_the_link_to_its_lines() -> None:
    reviews = _stored([_model_finding(10, "Rule")], {0: _LINES})

    assert reviews.processed is not None
    assert [item.rule_link for item in reviews.processed.inline] == [_LINES_URL]


@pytest.mark.parametrize(
    ("rule_name", "rule_line_refs", "kwargs"),
    [
        pytest.param(_RULES, {}, {}, id="no-valid-requirement"),
        pytest.param("AGENTS.md", {0: _LINES}, {}, id="another-source"),
        pytest.param(None, {0: _LINES}, {}, id="no-attribution"),
        pytest.param(_RULES, {0: _LINES}, {"repository": None}, id="not-a-github-repository"),
    ],
)
def test_any_other_finding_is_stored_and_published_without_a_link(
    rule_name: str | None, rule_line_refs: dict[int, RuleLineRef], kwargs: dict[str, Any]
) -> None:
    finding = {**_model_finding(10, "Rule"), "rule_name": rule_name}

    reviews = _stored([finding], rule_line_refs, **kwargs)

    assert reviews.processed is not None
    assert [item.rule_link for item in reviews.processed.inline] == [None]


def test_a_removed_attribution_takes_the_link_away() -> None:
    reviews = _stored([_model_finding(10, "Rule")], {0: _LINES}, allowed_v2_rules=frozenset())

    assert reviews.processed is not None
    assert [
        (item.finding.rule_name, item.rule_link)
        for item in (*reviews.processed.inline, *reviews.processed.body_only)
    ] == [(None, None)]


def test_a_finding_in_the_review_body_has_no_link() -> None:
    reviews = _stored(
        [_model_finding(10, "Inline"), _model_finding(11, "In the body")],
        {0: _LINES, 1: _LINES},
        max_inline=1,
    )

    assert reviews.processed is not None
    assert [item.rule_link for item in reviews.processed.inline] == [_LINES_URL]
    assert [item.rule_link for item in reviews.processed.body_only] == [None]


def test_the_stored_hash_covers_the_link() -> None:
    with_link = _stored([_model_finding(10, "Rule")], {0: _LINES})
    without = _stored([_model_finding(10, "Rule")], {})

    assert with_link.findings_hash != without.findings_hash


def test_the_publication_payload_keeps_the_stored_link() -> None:
    row = Finding(
        id=_FINDING_ID,
        file_path="app/main.py",
        line_start=10,
        line_end=None,
        severity=FindingSeverity.HIGH,
        confidence=Decimal("0.90"),
        category=FindingCategory.SECURITY,
        suggestion=None,
        title="Rule",
        body="Unsanitized input.",
        rule_name=_RULES,
        rule_link=_LINES_URL,
    )

    assert published_finding_from_row(row, None, provider="github").rule_link == _LINES_URL
