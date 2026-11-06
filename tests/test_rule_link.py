"""The link from a GitHub comment to the ``.review/rules.md`` lines of its rule (#465)."""

from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from typing import Any

import pytest

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
    ReviewFindingV2,
    RuleLineRef,
)
from app.modules.reviews.application.verdict import HashedFinding, findings_hash
from tests.policy_snapshot_fakes import make_policy_document_present
from tests.verifier_pipeline_fakes import MAIN_BLOCK_ID, verifier_snapshot

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
