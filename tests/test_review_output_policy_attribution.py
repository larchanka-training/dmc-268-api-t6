"""v2 attribution to a policy document needs that document shown with content (#130).

The v4 review prompt lets a finding name ``.review/rules.md`` or ``AGENTS.md`` only when the
prompt showed that document with content. ``build_manifest`` keeps a zero-line block for an
empty document (its digest is stored with the manifest), and omits the block for a missing
one, so the validator must refuse both, not only the missing case.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from app.modules.reviews.application.policy_snapshot import (
    POLICY_SNAPSHOT_SCHEMA_VERSION,
    CallManifest,
    PolicyDocument,
    PolicySnapshot,
)
from app.modules.reviews.application.prompt_builder import (
    ChangedFile,
    DiffLine,
    PromptBuilder,
    PullRequestMeta,
    RepoConventions,
    ReviewContext,
    ReviewRule,
    file_block_id,
)
from app.modules.reviews.application.review_output import (
    EvidenceRef,
    ReviewFindingV2,
    validate_v2_attribution,
)
from tests.policy_snapshot_fakes import (
    make_complete_execution_profile,
    make_complete_prompt_bindings,
    make_policy_document_empty,
    make_policy_document_missing,
    make_policy_document_present,
)

RULES = ".review/rules.md"
AGENTS = "AGENTS.md"
MAIN = ChangedFile(
    "app/main.py",
    "modified",
    tuple(DiffLine(number, "added", f"value_{number} = {number}") for number in range(1, 6)),
)
META = PullRequestMeta("PR", None, "alice", "feature", "main", (), 1, 5, 0, False, False)


def _document(path: str, state: str) -> PolicyDocument:
    if state == "content":
        return make_policy_document_present(path, "Prefer explicit names.\n")
    if state == "empty":
        return make_policy_document_empty(path)
    if state == "whitespace":
        return make_policy_document_present(path, "  \n\t\n")
    return make_policy_document_missing(path)


def _snapshot(*, rules: str, agents: str) -> PolicySnapshot:
    return PolicySnapshot(
        schema_version=POLICY_SNAPSHOT_SCHEMA_VERSION,
        snapshot_id=UUID("00000000-0000-0000-0000-000000001301"),
        run_id=UUID("00000000-0000-0000-0000-000000001302"),
        repository_id=UUID("00000000-0000-0000-0000-000000001303"),
        policy_base_sha="b" * 40,
        rules=_document(RULES, rules),
        agents=_document(AGENTS, agents),
        ignore_patterns=(),
        ignore_digest="0" * 64,
        parser_profile="rules-markdown/v1",
        parser_version="4.0.0",
        matcher_version="review-ignore/v1",
        limits_version="limits/v1",
        builtin_exclusions_version="builtin-exclusions/v1",
        priority_matcher_version="priority/v1",
        service_defaults_rule_version_id=UUID("00000000-0000-0000-0000-000000001304"),
        defaults_checksum="default-backend.v1",
        prompt_bindings=make_complete_prompt_bindings(),
        execution_profile=make_complete_execution_profile("fast"),
        policy_digest="d" * 64,
    )


def _manifest(
    *,
    rules: str,
    agents: str,
    defaults: tuple[ReviewRule, ...] = (),
    snapshot: PolicySnapshot | None = None,
) -> CallManifest:
    if snapshot is None:
        snapshot = _snapshot(rules=rules, agents=agents)
    context = ReviewContext(
        system="Review carefully.",
        rules=defaults,
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=META,
        changed_files=(MAIN,),
        omitted_files=(),
        policy_snapshot=snapshot,
    )
    return PromptBuilder().build_manifest(
        context, call_id=UUID("00000000-0000-0000-0000-000000001305")
    )


def _finding(rule_name: str | None) -> ReviewFindingV2:
    return ReviewFindingV2(
        path="app/main.py",
        line=3,
        start_line=None,
        severity="medium",
        category="readability",
        title="Name the value",
        body="The value has no explicit name.",
        suggestion=None,
        confidence=0.8,
        rule_name=rule_name,
        evidence_refs=[
            EvidenceRef(block_id=file_block_id("app/main.py"), start_line=2, end_line=3)
        ],
    )


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("content", (True, None)),
        (
            "empty",
            (
                False,
                "evidence_source_disallowed: rule_name is 'AGENTS.md' "
                "but project_conventions_document shows no lines",
            ),
        ),
        (
            "missing",
            (
                False,
                "evidence_source_disallowed: rule_name is 'AGENTS.md' "
                "but no project_conventions_document in manifest",
            ),
        ),
    ],
)
def test_agents_attribution_needs_a_document_shown_with_content(
    state: str, expected: tuple[bool, str | None]
) -> None:
    manifest = _manifest(rules="content", agents=state)

    assert validate_v2_attribution(_finding(AGENTS), manifest, pr_meta=META) == expected


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("content", (True, None)),
        (
            "empty",
            (
                False,
                "evidence_source_disallowed: rule_name is '.review/rules.md' "
                "but repository_rules shows no lines",
            ),
        ),
        (
            "missing",
            (
                False,
                "evidence_source_disallowed: rule_name is '.review/rules.md' "
                "but no repository_rules in manifest",
            ),
        ),
    ],
)
def test_rules_attribution_needs_a_document_shown_with_content(
    state: str, expected: tuple[bool, str | None]
) -> None:
    manifest = _manifest(rules=state, agents="content")

    assert validate_v2_attribution(_finding(RULES), manifest, pr_meta=META) == expected


def test_the_manifest_keeps_a_zero_line_block_for_an_empty_document() -> None:
    # The validator decides on this block; the manifest itself stays as stored (digests).
    manifest = _manifest(rules="empty", agents="empty")

    rules_block = manifest.get_block("policy-rules")
    agents_block = manifest.get_block("policy-agents")
    assert rules_block is not None and rules_block.shown_line_count == 0
    assert agents_block is not None and agents_block.shown_line_count == 0


@pytest.mark.parametrize("state", ["empty", "missing"])
def test_service_defaults_and_null_attribution_ignore_the_policy_documents(state: str) -> None:
    defaults = (ReviewRule("No print", ("app/**",), (), ("Do not print.",)),)
    manifest = _manifest(rules=state, agents=state, defaults=defaults)
    without_defaults = _manifest(rules=state, agents=state)

    assert validate_v2_attribution(_finding("service-defaults:BE-001"), manifest, pr_meta=META) == (
        True,
        None,
    )
    assert validate_v2_attribution(_finding(None), manifest, pr_meta=META) == (True, None)
    assert validate_v2_attribution(
        _finding("service-defaults:BE-001"), without_defaults, pr_meta=META
    ) == (
        False,
        "evidence_source_disallowed: rule_name is 'service-defaults:BE-001' "
        "but no service_defaults in manifest",
    )


@pytest.mark.parametrize(
    ("rule_name", "block_id", "expected_reason"),
    [
        (
            AGENTS,
            "policy-agents",
            "evidence_source_disallowed: rule_name is 'AGENTS.md' but the document has no content",
        ),
        (
            RULES,
            "policy-rules",
            "evidence_source_disallowed: rule_name is '.review/rules.md' "
            "but the document has no content",
        ),
    ],
)
def test_a_whitespace_only_document_is_not_citable_by_the_snapshot_status(
    rule_name: str, block_id: str, expected_reason: str
) -> None:
    # The prompt shows its blank lines, so only the snapshot can tell it is empty.
    snapshot = _snapshot(rules="whitespace", agents="whitespace")
    manifest = _manifest(rules="whitespace", agents="whitespace", snapshot=snapshot)
    block = manifest.get_block(block_id)
    assert block is not None and block.shown_line_count == 2

    assert validate_v2_attribution(
        _finding(rule_name), manifest, pr_meta=META, policy_snapshot=snapshot
    ) == (False, expected_reason)


@pytest.mark.parametrize("rule_name", [AGENTS, RULES])
def test_a_document_with_content_stays_citable_with_the_snapshot(rule_name: str) -> None:
    snapshot = _snapshot(rules="content", agents="content")
    manifest = _manifest(rules="content", agents="content", snapshot=snapshot)

    assert validate_v2_attribution(
        _finding(rule_name), manifest, pr_meta=META, policy_snapshot=snapshot
    ) == (True, None)
