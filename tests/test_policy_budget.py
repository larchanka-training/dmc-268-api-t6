"""Unit tests for Policy Token Budget and Pre-Send Guards (Issue #120)."""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Literal

import pytest

from app.modules.reviews.application import review_ignore
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.policy_snapshot import (
    CallManifest,
    ExecutionProfile,
    ManifestBlock,
    PolicySnapshot,
    PromptBindings,
    SourceRole,
)
from app.modules.reviews.application.prompt_budget import (
    EstimatedTokenCounter,
    LegacyTokenCounter,
    PolicyBudgetExceededError,
    PreSendGuardError,
    policy_tokens,
    send_budget_for,
    token_counter_for,
    validate_execution_profile_versions,
    verify_policy_budget,
    verify_pre_send_guard,
    verify_provenance_pre_send_guard,
)
from app.modules.reviews.application.prompt_builder import (
    ChangedFile,
    DiffLine,
    PromptBuilder,
    PullRequestMeta,
    RepoConventions,
    ReviewContext,
)
from tests.policy_snapshot_fakes import (
    make_policy_document_missing,
    make_policy_document_present,
)


class CharCounter:
    """1 char = 1 token for predictable assertions."""

    def count(self, text: str) -> int:
        return len(text)


def make_snapshot(
    rules_text: str = "",
    agents_text: str = "",
    ignore_patterns: tuple[str, ...] = (),
) -> PolicySnapshot:
    if ignore_patterns:
        # A snapshot stores the masks of its rules text: they go into its review-ignore block.
        masks = "".join(f"{mask}\n" for mask in ignore_patterns)
        rules_text = f"{rules_text}```review-ignore\n{masks}```\n"
    ignore = review_ignore.parse_review_ignore(rules_text)
    return PolicySnapshot(
        schema_version=1,
        snapshot_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        repository_id=uuid.uuid4(),
        policy_base_sha="sha_base",
        rules=(
            make_policy_document_present(".review/rules.md", rules_text)
            if rules_text
            else make_policy_document_missing(".review/rules.md")
        ),
        agents=(
            make_policy_document_present("AGENTS.md", agents_text)
            if agents_text
            else make_policy_document_missing("AGENTS.md")
        ),
        ignore_patterns=ignore.patterns,
        ignore_digest=ignore.digest,
        parser_profile="rules-markdown/v1",
        parser_version="4.0.0",
        matcher_version="review-ignore/v1",
        limits_version="policy-limits/v1",
        builtin_exclusions_version="builtin-exclusions/v1",
        priority_matcher_version="priority-matcher/v1",
        service_defaults_rule_version_id=uuid.uuid4(),
        defaults_checksum="282d61c2da897152046db742fbd9bfda061ba1c71ce32759d47e042c06b0ff52",
        prompt_bindings=PromptBindings("cs1", "cs2", "cs3"),
        execution_profile=ExecutionProfile.fast("m1", "m2"),
        policy_digest="digest_base",
    )


def test_policy_tokens_counts_rendered_documents() -> None:
    counter = CharCounter()
    snapshot = make_snapshot(
        rules_text="# Security\n1. Validate input.\n",
        agents_text="# Architecture\nUse UoW.\n",
    )
    tokens = policy_tokens(snapshot, counter)
    # Exact rendered character length of policy XML blocks per specification
    expected_rules = (
        '<repository_rules block_id="policy-rules" path=".review/rules.md">\n'
        '<line n="1"># Security</line>\n'
        '<line n="2">1. Validate input.</line>\n'
        "</repository_rules>"
    )
    expected_agents = (
        '<project_conventions_document block_id="policy-agents" path="AGENTS.md">\n'
        '<line n="1"># Architecture</line>\n'
        '<line n="2">Use UoW.</line>\n'
        "</project_conventions_document>"
    )
    expected_tokens = counter.count(expected_rules) + counter.count(expected_agents)
    assert tokens == expected_tokens


def test_verify_policy_budget_passes_within_limit() -> None:
    counter = CharCounter()
    snapshot = make_snapshot(rules_text="Short rule\n")
    # Default limit is 12,000 tokens
    verify_policy_budget(snapshot, counter, max_policy_tokens=12_000)


def test_verify_policy_budget_exceeded_raises_error() -> None:
    counter = CharCounter()
    # Create rule text that renders to > 100 characters, with max_policy_tokens=100
    large_rule = "Rule line " * 20 + "\n"
    snapshot = make_snapshot(rules_text=large_rule)
    with pytest.raises(PolicyBudgetExceededError) as exc_info:
        verify_policy_budget(snapshot, counter, max_policy_tokens=100)
    assert "policy_prompt_budget_exceeded" in str(exc_info.value)


def test_pre_send_guard_verifies_policy_and_total_budget() -> None:
    counter = CharCounter()
    snapshot = make_snapshot(rules_text="Small rule\n")
    context = ReviewContext(
        system="SYSTEM",
        rules=(),
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=PullRequestMeta("Title", None, "dev", "b", "main", (), 1, 1, 0, False, False),
        changed_files=(ChangedFile("app/a.py", "modified", (DiffLine(1, "added", "x = 1"),)),),
        omitted_files=(),
        policy_snapshot=snapshot,
    )

    # Should pass with generous limit
    verify_pre_send_guard(context, counter, max_input_tokens=10_000)

    # Should fail if total exceeds limit
    with pytest.raises(PreSendGuardError) as exc_info:
        verify_pre_send_guard(context, counter, max_input_tokens=50)
    assert "llm_context_overflow" in str(exc_info.value)


def test_estimated_token_counter_is_conservative_three_chars() -> None:
    counter = EstimatedTokenCounter()
    assert counter.count("") == 0
    assert counter.count("a") == 1
    assert counter.count("abc") == 1
    assert counter.count("abcd") == 2
    assert counter.count_length(300) == 100
    assert counter.count_length(301) == 101


def test_rules_file_of_39390_chars_exceeds_12000_token_limit_with_estimated_counter() -> None:
    # 400 lines totaling exactly 39,390 chars (210 lines of 98 chars + 190 lines of 99 chars)
    # Under legacy 4 chars/token: 11,965 tokens <= 12,000 limit (passes)
    # Under conservative 3 chars/token (counter/v2): 15,953 tokens > 12,000 limit (exceeds)
    lines = ["x" * 97 + "\n"] * 210 + ["x" * 98 + "\n"] * 190
    rules_content = "".join(lines)
    assert len(rules_content) == 39390
    assert len(rules_content.splitlines()) == 400

    snapshot = make_snapshot(rules_text=rules_content)
    legacy_counter = LegacyTokenCounter()
    v2_counter = EstimatedTokenCounter()

    assert policy_tokens(snapshot, legacy_counter) == 11965
    assert policy_tokens(snapshot, v2_counter) == 15953

    with pytest.raises(PolicyBudgetExceededError) as exc_info:
        verify_policy_budget(snapshot, v2_counter, max_policy_tokens=12_000)
    assert exc_info.value.limit == 12000
    assert exc_info.value.actual == 15953
    assert "policy_prompt_budget_exceeded" in str(exc_info.value)


def test_provenance_guard_passes_valid_manifest() -> None:
    snapshot = make_snapshot(rules_text="1. Rule\n")
    context = ReviewContext(
        system="SYSTEM",
        rules=(),
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=PullRequestMeta(
            "PR", None, "u", "b", "main", (), 1, 1, 0, False, False, head_sha="sha_base"
        ),
        changed_files=(ChangedFile("app/main.py", "modified", (DiffLine(1, "added", "x = 1"),)),),
        omitted_files=(),
        policy_snapshot=snapshot,
    )
    manifest = PromptBuilder().build_manifest(context, call_id=uuid.uuid4(), stage="review")
    verify_provenance_pre_send_guard(manifest, context=context, snapshot=snapshot)


def test_provenance_guard_rejects_disallowed_role() -> None:
    snapshot = make_snapshot()
    bad_block = ManifestBlock(
        block_id="b1",
        source_role="unsupported_role",  # type: ignore[arg-type]
        source_kind="source_code",
    )
    manifest = CallManifest(
        call_id=uuid.uuid4(),
        run_id=snapshot.run_id,
        stage="review",
        blocks=(bad_block,),
    )
    with pytest.raises(PolicyError) as exc_info:
        verify_provenance_pre_send_guard(manifest, snapshot=snapshot)
    assert exc_info.value.code == PolicyErrorCode.CONTEXT_VIOLATION
    assert "disallowed source_role" in exc_info.value.message


def test_provenance_guard_rejects_disallowed_kind() -> None:
    snapshot = make_snapshot()
    for bad_kind in ("disallowed_kind", "diff", "candidate_finding"):
        bad_block = ManifestBlock(
            block_id="b1",
            source_role=SourceRole.DIFF,
            source_kind=bad_kind,
        )
        manifest = CallManifest(
            call_id=uuid.uuid4(),
            run_id=snapshot.run_id,
            stage="review",
            blocks=(bad_block,),
        )
        with pytest.raises(PolicyError) as exc_info:
            verify_provenance_pre_send_guard(manifest, snapshot=snapshot)
        assert exc_info.value.code == PolicyErrorCode.CONTEXT_VIOLATION
        assert "disallowed source_kind" in exc_info.value.message


def test_provenance_guard_rejects_directory_traversal() -> None:
    snapshot = make_snapshot()
    bad_block = ManifestBlock(
        block_id="b1",
        source_role=SourceRole.DIFF,
        source_kind="source_code",
        path="../etc/passwd",
    )
    manifest = CallManifest(
        call_id=uuid.uuid4(),
        run_id=snapshot.run_id,
        stage="review",
        blocks=(bad_block,),
    )
    with pytest.raises(PolicyError) as exc_info:
        verify_provenance_pre_send_guard(manifest, snapshot=snapshot)
    assert exc_info.value.code == PolicyErrorCode.CONTEXT_VIOLATION
    assert "directory traversal" in exc_info.value.message


def test_provenance_guard_rejects_unsupported_paths() -> None:
    snapshot = make_snapshot()

    # Backslash
    bad_block = ManifestBlock(
        block_id="b-bs",
        source_role=SourceRole.DIFF,
        source_kind="source_code",
        path="src\\evil.py",
    )
    manifest = CallManifest(
        call_id=uuid.uuid4(),
        run_id=snapshot.run_id,
        stage="review",
        blocks=(bad_block,),
    )
    with pytest.raises(PolicyError) as exc_info:
        verify_provenance_pre_send_guard(manifest, snapshot=snapshot)
    assert exc_info.value.code == PolicyErrorCode.CONTEXT_VIOLATION
    assert "references unsupported path" in exc_info.value.message

    # NUL / control character
    for bad_path in ("a\x00b.py", "a\x1bb.py"):
        bad_block = ManifestBlock(
            block_id="b-ctrl",
            source_role=SourceRole.DIFF,
            source_kind="source_code",
            path=bad_path,
        )
        manifest = CallManifest(
            call_id=uuid.uuid4(),
            run_id=snapshot.run_id,
            stage="review",
            blocks=(bad_block,),
        )
        with pytest.raises(PolicyError) as exc_info:
            verify_provenance_pre_send_guard(manifest, snapshot=snapshot)
        assert exc_info.value.code == PolicyErrorCode.CONTEXT_VIOLATION
        assert "references unsupported path" in exc_info.value.message

    # Surrogate
    bad_block = ManifestBlock(
        block_id="b-surr",
        source_role=SourceRole.DIFF,
        source_kind="source_code",
        path="a\ud800b.py",
    )
    manifest = CallManifest(
        call_id=uuid.uuid4(),
        run_id=snapshot.run_id,
        stage="review",
        blocks=(bad_block,),
    )
    with pytest.raises(PolicyError) as exc_info:
        verify_provenance_pre_send_guard(manifest, snapshot=snapshot)
    assert exc_info.value.code == PolicyErrorCode.CONTEXT_VIOLATION
    assert (
        "has invalid encoding" in exc_info.value.message
        or "references unsupported path" in exc_info.value.message
    )

    # Legal path containing 'unsupported_path' in directory name is ACCEPTED
    ok_block = ManifestBlock(
        block_id="b-ok",
        source_role=SourceRole.DIFF,
        source_kind="source_code",
        path="src/unsupported_path/ok.py",
    )
    ok_manifest = CallManifest(
        call_id=uuid.uuid4(),
        run_id=snapshot.run_id,
        stage="review",
        blocks=(ok_block,),
    )
    verify_provenance_pre_send_guard(ok_manifest, snapshot=snapshot)


def test_provenance_guard_rejects_ignored_path() -> None:
    snapshot = make_snapshot(ignore_patterns=("secret/**",))
    bad_block = ManifestBlock(
        block_id="b-ignored",
        source_role=SourceRole.DIFF,
        source_kind="source_code",
        path="secret/key.py",
    )
    manifest = CallManifest(
        call_id=uuid.uuid4(),
        run_id=snapshot.run_id,
        stage="review",
        blocks=(bad_block,),
    )
    with pytest.raises(PolicyError) as exc_info:
        verify_provenance_pre_send_guard(manifest, snapshot=snapshot)
    assert exc_info.value.code == PolicyErrorCode.CONTEXT_VIOLATION
    assert "references ignored path matching deny-set" in exc_info.value.message


@pytest.mark.parametrize(
    ("path", "mask", "line"),
    [("secret/key.py", "secret/**", 4), ("docs/guide.md", "docs/**", 5)],
    ids=["first-of-a-repeated-mask", "single-mask"],
)
def test_provenance_guard_names_the_ignore_mask_and_its_rules_line(
    path: str, mask: str, line: int
) -> None:
    # Masks on lines 4-6; secret/** repeats on line 6, and its first line is the one named.
    snapshot = make_snapshot(
        rules_text="# Rules\n\n```review-ignore\nsecret/**\ndocs/**\nsecret/**\n```\n"
    )
    block = ManifestBlock(
        block_id="b-ignored", source_role=SourceRole.DIFF, source_kind="source_code", path=path
    )
    manifest = CallManifest(
        call_id=uuid.uuid4(), run_id=snapshot.run_id, stage="review", blocks=(block,)
    )

    with pytest.raises(PolicyError) as exc_info:
        verify_provenance_pre_send_guard(manifest, snapshot=snapshot)

    error = exc_info.value
    assert error.code is PolicyErrorCode.CONTEXT_VIOLATION
    assert (error.source_path, error.line) == (".review/rules.md", line)
    assert str(error) == (
        "policy_context_violation: Block 'b-ignored' references ignored path matching "
        f"deny-set | file `.review/rules.md`, line {line}, mask `{mask}`"
    )
    # RFS §9 defines pattern_index for a mask parse error only, so it stays null here.
    assert error.diagnostic() == {
        "code": "policy_context_violation",
        "source_path": ".review/rules.md",
        "policy_base_sha": "sha_base",
        "message": "Block 'b-ignored' references ignored path matching deny-set",
        "line": line,
        "column": None,
        "pattern_index": None,
        "limit": None,
        "actual": None,
        "actual_is_lower_bound": False,
        "retryable": False,
    }


def test_provenance_guard_names_a_mask_without_a_line_when_the_rules_text_drifted() -> None:
    # The stored rules text no longer gives back the stored mask: it still applies, unlined.
    snapshot = replace(
        make_snapshot(ignore_patterns=("secret/**",)),
        rules=make_policy_document_missing(".review/rules.md"),
    )
    block = ManifestBlock(
        block_id="b-ignored",
        source_role=SourceRole.DIFF,
        source_kind="source_code",
        path="secret/key.py",
    )
    manifest = CallManifest(
        call_id=uuid.uuid4(), run_id=snapshot.run_id, stage="review", blocks=(block,)
    )

    with pytest.raises(PolicyError) as exc_info:
        verify_provenance_pre_send_guard(manifest, snapshot=snapshot)

    error = exc_info.value
    assert error.code is PolicyErrorCode.CONTEXT_VIOLATION
    assert (error.source_path, error.line) == (".review/rules.md", None)
    assert str(error) == (
        "policy_context_violation: Block 'b-ignored' references ignored path matching "
        "deny-set | file `.review/rules.md`, mask `secret/**`"
    )


def test_provenance_guard_match_budget_error_names_the_policy_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(review_ignore, "MAX_TRANSITIONS_PER_PATH", 1)
    snapshot = make_snapshot(ignore_patterns=("**/*.md",))
    block = ManifestBlock(
        block_id="b-costly",
        source_role=SourceRole.DIFF,
        source_kind="source_code",
        path="src/app.py",
    )
    manifest = CallManifest(
        call_id=uuid.uuid4(), run_id=snapshot.run_id, stage="review", blocks=(block,)
    )
    with pytest.raises(PolicyError) as exc_info:
        verify_provenance_pre_send_guard(manifest, snapshot=snapshot)
    assert exc_info.value.code == PolicyErrorCode.MATCH_BUDGET_EXCEEDED
    assert exc_info.value.policy_base_sha == "sha_base"


def test_provenance_guard_rejects_path_exceeding_byte_limit() -> None:
    snapshot = make_snapshot()
    bad_block = ManifestBlock(
        block_id="b1",
        source_role=SourceRole.DIFF,
        source_kind="source_code",
        path="a" * 1025,
    )
    manifest = CallManifest(
        call_id=uuid.uuid4(),
        run_id=snapshot.run_id,
        stage="review",
        blocks=(bad_block,),
    )
    with pytest.raises(PolicyError) as exc_info:
        verify_provenance_pre_send_guard(manifest, snapshot=snapshot)
    assert exc_info.value.code == PolicyErrorCode.CONTEXT_VIOLATION
    assert "path exceeds 1024 bytes limit" in exc_info.value.message


def test_provenance_guard_rejects_path_exceeding_segment_limit() -> None:
    snapshot = make_snapshot()
    bad_block = ManifestBlock(
        block_id="b1",
        source_role=SourceRole.DIFF,
        source_kind="source_code",
        path="/".join(["dir"] * 130),
    )
    manifest = CallManifest(
        call_id=uuid.uuid4(),
        run_id=snapshot.run_id,
        stage="review",
        blocks=(bad_block,),
    )
    with pytest.raises(PolicyError) as exc_info:
        verify_provenance_pre_send_guard(manifest, snapshot=snapshot)
    assert exc_info.value.code == PolicyErrorCode.CONTEXT_VIOLATION
    assert "path exceeds 128 segments limit" in exc_info.value.message


def test_provenance_guard_rejects_foreign_revision() -> None:
    snapshot = make_snapshot()
    context = ReviewContext(
        system="SYSTEM",
        rules=(),
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=PullRequestMeta(
            "PR",
            None,
            "u",
            "b",
            "main",
            (),
            1,
            1,
            0,
            False,
            False,
            head_sha="sha_head_123",
            diff_old_sha="sha_old_789",
        ),
        changed_files=(),
        omitted_files=(),
        policy_snapshot=snapshot,
    )
    bad_block = ManifestBlock(
        block_id="b1",
        source_role=SourceRole.DIFF,
        source_kind="source_code",
        path="app/main.py",
        revision="foreign_revision_456",
        side="RIGHT",
    )
    manifest = CallManifest(
        call_id=uuid.uuid4(),
        run_id=snapshot.run_id,
        stage="review",
        blocks=(bad_block,),
    )
    with pytest.raises(PolicyError) as exc_info:
        verify_provenance_pre_send_guard(manifest, context=context, snapshot=snapshot)
    assert exc_info.value.code == PolicyErrorCode.CONTEXT_VIOLATION
    assert "does not match PR revisions" in exc_info.value.message

    # LEFT side matching diff_old_sha passes
    ok_left_block = ManifestBlock(
        block_id="b-left-ok",
        source_role=SourceRole.DIFF,
        source_kind="source_code",
        path="app/main.py",
        revision="sha_old_789",
        side="LEFT",
    )
    ok_left_manifest = CallManifest(
        call_id=uuid.uuid4(),
        run_id=snapshot.run_id,
        stage="review",
        blocks=(ok_left_block,),
    )
    verify_provenance_pre_send_guard(ok_left_manifest, context=context, snapshot=snapshot)

    # LEFT side with foreign revision fails
    bad_left_block = ManifestBlock(
        block_id="b-left-bad",
        source_role=SourceRole.DIFF,
        source_kind="source_code",
        path="app/main.py",
        revision="sha_head_123",
        side="LEFT",
    )
    bad_left_manifest = CallManifest(
        call_id=uuid.uuid4(),
        run_id=snapshot.run_id,
        stage="review",
        blocks=(bad_left_block,),
    )
    with pytest.raises(PolicyError) as exc_info:
        verify_provenance_pre_send_guard(bad_left_manifest, context=context, snapshot=snapshot)
    assert exc_info.value.code == PolicyErrorCode.CONTEXT_VIOLATION
    assert "LEFT revision does not match PR revisions" in exc_info.value.message

    # LEFT side when diff_old_sha is None fails closed (even if revision == policy_base_sha)
    context_no_old_sha = ReviewContext(
        system="SYSTEM",
        rules=(),
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=PullRequestMeta(
            "PR",
            None,
            "u",
            "b",
            "main",
            (),
            1,
            1,
            0,
            False,
            False,
            head_sha="sha_head_123",
            diff_old_sha=None,
        ),
        changed_files=(),
        omitted_files=(),
        policy_snapshot=snapshot,
    )
    left_on_base_block = ManifestBlock(
        block_id="b-left-base",
        source_role=SourceRole.DIFF,
        source_kind="source_code",
        path="app/main.py",
        revision="sha_base",
        side="LEFT",
    )
    left_on_base_manifest = CallManifest(
        call_id=uuid.uuid4(),
        run_id=snapshot.run_id,
        stage="review",
        blocks=(left_on_base_block,),
    )
    with pytest.raises(PolicyError) as exc_info:
        verify_provenance_pre_send_guard(
            left_on_base_manifest, context=context_no_old_sha, snapshot=snapshot
        )
    assert exc_info.value.code == PolicyErrorCode.CONTEXT_VIOLATION
    assert (
        "LEFT revision does not match PR revisions: diff_old_sha 'None'" in exc_info.value.message
    )


# D7 (#144): a code block is bound to the PR head and to the snapshot's repository.

_CODE_ROLES = (
    SourceRole.DIFF,
    SourceRole.SOURCE_EXCERPT,
    SourceRole.AST_EXCERPT,
    SourceRole.RETRIEVED_EXCERPT,
)


def _code_context(snapshot: PolicySnapshot, *, head_sha: str | None) -> ReviewContext:
    return ReviewContext(
        system="SYSTEM",
        rules=(),
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=PullRequestMeta(
            "PR",
            None,
            "u",
            "b",
            "main",
            (),
            1,
            1,
            0,
            False,
            False,
            head_sha=head_sha,
            diff_old_sha="sha_old_789",
        ),
        changed_files=(),
        omitted_files=(),
        policy_snapshot=snapshot,
    )


def _code_manifest(snapshot: PolicySnapshot, block: ManifestBlock) -> CallManifest:
    return CallManifest(
        call_id=uuid.uuid4(), run_id=snapshot.run_id, stage="review", blocks=(block,)
    )


@pytest.mark.parametrize("head_sha", [None, "sha_head_123"], ids=["no-head", "other-head"])
@pytest.mark.parametrize("side", ["RIGHT", None], ids=["right", "side-less"])
@pytest.mark.parametrize("role", _CODE_ROLES)
def test_provenance_guard_rejects_a_head_side_code_block_off_the_pr_head(
    role: SourceRole, side: Literal["RIGHT"] | None, head_sha: str | None
) -> None:
    # A missing head no longer lets any revision through: the non-LEFT branch fails closed.
    snapshot = make_snapshot()
    block = ManifestBlock(
        block_id="b-head",
        source_role=role,
        source_kind="source_code",
        path="app/main.py",
        revision="foreign_revision_456",
        side=side,
        repository_id=snapshot.repository_id,
    )
    with pytest.raises(PolicyError) as exc_info:
        verify_provenance_pre_send_guard(
            _code_manifest(snapshot, block),
            context=_code_context(snapshot, head_sha=head_sha),
            snapshot=snapshot,
        )
    assert exc_info.value.code == PolicyErrorCode.CONTEXT_VIOLATION
    assert f"does not match PR revisions: head_sha '{head_sha}'" in exc_info.value.message


@pytest.mark.parametrize("side", ["RIGHT", None], ids=["right", "side-less"])
@pytest.mark.parametrize("role", _CODE_ROLES)
def test_provenance_guard_passes_a_code_block_of_the_pr_head_and_repository(
    role: SourceRole, side: Literal["RIGHT"] | None
) -> None:
    snapshot = make_snapshot()
    block = ManifestBlock(
        block_id="b-head",
        source_role=role,
        source_kind="source_code",
        path="app/main.py",
        revision="sha_head_123",
        side=side,
        repository_id=snapshot.repository_id,
    )
    verify_provenance_pre_send_guard(
        _code_manifest(snapshot, block),
        context=_code_context(snapshot, head_sha="sha_head_123"),
        snapshot=snapshot,
    )


@pytest.mark.parametrize("role", _CODE_ROLES)
def test_provenance_guard_rejects_a_code_block_of_another_repository(role: SourceRole) -> None:
    snapshot = make_snapshot()
    block = ManifestBlock(
        block_id="b-repo",
        source_role=role,
        source_kind="source_code",
        path="app/main.py",
        revision="sha_head_123",
        side="RIGHT",
        repository_id=uuid.uuid4(),
    )
    with pytest.raises(PolicyError) as exc_info:
        verify_provenance_pre_send_guard(
            _code_manifest(snapshot, block),
            context=_code_context(snapshot, head_sha="sha_head_123"),
            snapshot=snapshot,
        )
    assert exc_info.value.code == PolicyErrorCode.CONTEXT_VIOLATION
    assert "repository does not match the policy snapshot" in exc_info.value.message


@pytest.mark.parametrize("with_snapshot", [True, False], ids=["snapshot", "no-snapshot"])
def test_provenance_guard_rejects_a_retrieved_excerpt_without_a_repository(
    with_snapshot: bool,
) -> None:
    snapshot = make_snapshot()
    block = ManifestBlock(
        block_id="b-rag",
        source_role=SourceRole.RETRIEVED_EXCERPT,
        source_kind="source_code",
        path="app/helper.py",
        revision="sha_head_123",
        side="RIGHT",
    )
    with pytest.raises(PolicyError) as exc_info:
        verify_provenance_pre_send_guard(
            _code_manifest(snapshot, block),
            context=_code_context(snapshot, head_sha="sha_head_123"),
            snapshot=snapshot if with_snapshot else None,
        )
    assert exc_info.value.code == PolicyErrorCode.CONTEXT_VIOLATION
    assert "has no repository" in exc_info.value.message


@pytest.mark.parametrize("side", ["RIGHT", "LEFT"], ids=["right", "left"])
@pytest.mark.parametrize("with_snapshot", [True, False], ids=["snapshot", "no-snapshot"])
def test_provenance_guard_rejects_a_retrieved_excerpt_without_a_revision(
    with_snapshot: bool, side: Literal["RIGHT", "LEFT"]
) -> None:
    # A RAG block is read at a side of the PR: without a revision no side check can run.
    snapshot = make_snapshot()
    block = ManifestBlock(
        block_id="b-rag",
        source_role=SourceRole.RETRIEVED_EXCERPT,
        source_kind="source_code",
        path="app/helper.py",
        side=side,
        repository_id=snapshot.repository_id,
    )
    with pytest.raises(PolicyError) as exc_info:
        verify_provenance_pre_send_guard(
            _code_manifest(snapshot, block),
            context=_code_context(snapshot, head_sha="sha_head_123"),
            snapshot=snapshot if with_snapshot else None,
        )
    assert exc_info.value.code == PolicyErrorCode.CONTEXT_VIOLATION
    assert "has no revision" in exc_info.value.message


def test_provenance_guard_passes_a_non_retrieved_code_block_without_a_repository() -> None:
    # The legacy path's code blocks carry no repository: only a RAG block must name one.
    snapshot = make_snapshot()
    block = ManifestBlock(
        block_id="b-diff",
        source_role=SourceRole.DIFF,
        source_kind="source_code",
        path="app/main.py",
        revision="sha_head_123",
        side="RIGHT",
    )
    verify_provenance_pre_send_guard(
        _code_manifest(snapshot, block),
        context=_code_context(snapshot, head_sha="sha_head_123"),
        snapshot=snapshot,
    )


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("../etc/passwd", "directory traversal"),
        ("internal/helper.py", "references ignored path matching deny-set"),
    ],
    ids=["traversal", "ignored"],
)
def test_provenance_guard_still_refuses_a_retrieved_excerpt_of_a_foreign_path(
    path: str, message: str
) -> None:
    # The head and the repository match: the path check alone refuses the block.
    snapshot = make_snapshot(ignore_patterns=("internal/**",))
    block = ManifestBlock(
        block_id="b-rag",
        source_role=SourceRole.RETRIEVED_EXCERPT,
        source_kind="source_code",
        path=path,
        revision="sha_head_123",
        side="RIGHT",
        repository_id=snapshot.repository_id,
    )
    with pytest.raises(PolicyError) as exc_info:
        verify_provenance_pre_send_guard(
            _code_manifest(snapshot, block),
            context=_code_context(snapshot, head_sha="sha_head_123"),
            snapshot=snapshot,
        )
    assert exc_info.value.code == PolicyErrorCode.CONTEXT_VIOLATION
    assert message in exc_info.value.message


def test_provenance_guard_rejects_rules_block_when_absent_in_snapshot() -> None:
    snapshot = make_snapshot()  # rules is missing
    bad_block = ManifestBlock(
        block_id="policy-rules",
        source_role=SourceRole.POLICY_DOCUMENT,
        source_kind="repository_rules",
        path=".review/rules.md",
    )
    manifest = CallManifest(
        call_id=uuid.uuid4(),
        run_id=snapshot.run_id,
        stage="review",
        blocks=(bad_block,),
    )
    with pytest.raises(PolicyError) as exc_info:
        verify_provenance_pre_send_guard(manifest, snapshot=snapshot)
    assert exc_info.value.code == PolicyErrorCode.CONTEXT_VIOLATION
    assert "snapshot has no rules document" in exc_info.value.message


def test_provenance_guard_rejects_rules_block_content_hash_mismatch() -> None:
    snapshot = make_snapshot(rules_text="1. Rule\n")
    bad_block = ManifestBlock(
        block_id="policy-rules",
        source_role=SourceRole.POLICY_DOCUMENT,
        source_kind="repository_rules",
        path=".review/rules.md",
        content_sha256="wrong_checksum" + "0" * 50,
    )
    manifest = CallManifest(
        call_id=uuid.uuid4(),
        run_id=snapshot.run_id,
        stage="review",
        blocks=(bad_block,),
    )
    with pytest.raises(PolicyError) as exc_info:
        verify_provenance_pre_send_guard(manifest, snapshot=snapshot)
    assert exc_info.value.code == PolicyErrorCode.CONTEXT_VIOLATION
    assert "checksum mismatch" in exc_info.value.message


def test_token_counter_for_resolutions_and_integrity_error() -> None:
    assert isinstance(token_counter_for("counter/v2"), EstimatedTokenCounter)
    assert isinstance(token_counter_for(None), EstimatedTokenCounter)
    assert isinstance(token_counter_for("counter/v1"), LegacyTokenCounter)

    for bad_version in ("unknown/version", "estimated/v1", "estimated/v2"):
        with pytest.raises(PolicyError) as exc_info:
            token_counter_for(bad_version)
        assert exc_info.value.code == PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR
        assert "Unknown counter_version" in exc_info.value.message


def test_validate_execution_profile_versions_and_send_budget_for() -> None:
    from dataclasses import replace

    profile = ExecutionProfile.fast("m1", "m2")
    validate_execution_profile_versions(profile)

    for bad_profile in (
        replace(profile, tokenizer_version="tokenizer/v9"),
        replace(profile, recovery_profile_version="recovery/v9"),
        replace(profile, budget_profile_version="budget/v9"),
        replace(profile, counter_version="estimated/v2"),
    ):
        with pytest.raises(PolicyError) as exc_info:
            validate_execution_profile_versions(bad_profile)
        assert exc_info.value.code == PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR

    # Monotonic send_budget_for across max_input_tokens and model_max_prompt_tokens
    # Default: max_input_tokens=60000, context_window=128000, max_output_tokens=8000 -> 52000
    assert send_budget_for(profile, stage="review", model_max_prompt_tokens=None) == 52_000
    assert send_budget_for(profile, stage="review", model_max_prompt_tokens=60_000) == 52_000
    assert send_budget_for(profile, stage="review", model_max_prompt_tokens=52_000) == 52_000
    assert send_budget_for(profile, stage="review", model_max_prompt_tokens=51_999) == 51_999
    assert send_budget_for(profile, stage="review", model_max_prompt_tokens=40_000) == 40_000

    # Strictly monotonic at 52_000 -> 52_001 max_input_tokens (44_000 -> 44_001)
    assert send_budget_for(replace(profile, max_input_tokens=52_000), stage="review") == 44_000
    assert send_budget_for(replace(profile, max_input_tokens=52_001), stage="review") == 44_001

    # Verification stage uses same monotonic calculation
    assert send_budget_for(profile, stage="verification", model_max_prompt_tokens=None) == 52_000
    assert send_budget_for(profile, stage="verification", model_max_prompt_tokens=20_000) == 20_000


def test_verify_verifier_mandatory_budget_missing_prompt_asset_raises_integrity_error() -> None:
    from dataclasses import replace

    from app.modules.reviews.application.reflection_pass import verify_verifier_mandatory_budget

    snapshot = make_snapshot(rules_text="1. Rule\n")
    # Bind an unseeded non-existent verifier prompt version
    bad_bindings = replace(snapshot.prompt_bindings, verifier_version="v999")
    bad_snapshot = replace(snapshot, prompt_bindings=bad_bindings)

    context = ReviewContext(
        system="SYSTEM",
        rules=(),
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=PullRequestMeta("T", None, "u", "b", "main", (), 1, 1, 0, False, False),
        changed_files=(),
        omitted_files=(),
        policy_snapshot=bad_snapshot,
    )

    with pytest.raises(PolicyError) as exc_info:
        verify_verifier_mandatory_budget(context)
    assert exc_info.value.code == PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR
    msg = exc_info.value.message
    assert "Unable to read verifier prompt resource 'review.verifier.v999.md'" in msg


@pytest.mark.parametrize("rule_length", [11_804, 11_805], ids=["exact", "one-over"])
def test_default_policy_cap_accepts_exact_limit_and_rejects_one_over(rule_length: int) -> None:
    # The single-line rules XML and absent AGENTS.md envelope add 196 characters.
    snapshot = make_snapshot(rules_text="x" * rule_length)
    if rule_length == 11_804:
        verify_policy_budget(snapshot, CharCounter())
    else:
        with pytest.raises(PolicyBudgetExceededError) as error:
            verify_policy_budget(snapshot, CharCounter())
        assert error.value.limit == 12_000
        assert error.value.actual == 12_001
        assert str(error.value) == (
            "policy_prompt_budget_exceeded: rendered policy is 12001 tokens, limit is 12000"
        )


@pytest.mark.parametrize(
    "profile",
    [ExecutionProfile.fast("m1", "m2"), ExecutionProfile.deep("m1", "m2")],
    ids=["fast", "deep"],
)
@pytest.mark.parametrize(
    ("cap", "exact_rule_length", "over_rule_length", "over_tokens"),
    [(200, 4, 5, 201), (12_001, 11_805, 11_806, 12_002)],
    ids=["below-default", "above-default"],
)
def test_pre_send_guard_uses_run_policy_cap(
    profile: ExecutionProfile,
    cap: int,
    exact_rule_length: int,
    over_rule_length: int,
    over_tokens: int,
) -> None:
    # The rules XML plus absent AGENTS.md envelope adds 196 character-tokens.
    snapshot = replace(
        make_snapshot(rules_text="x" * exact_rule_length),
        execution_profile=replace(profile, max_policy_tokens=cap),
    )
    context = ReviewContext(
        system="SYSTEM",
        rules=(),
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=PullRequestMeta("T", None, "u", "b", "main", (), 1, 1, 0, False, False),
        changed_files=(),
        omitted_files=(),
        policy_snapshot=snapshot,
    )
    verify_pre_send_guard(context, CharCounter(), max_input_tokens=50_000)

    over_snapshot = replace(
        snapshot,
        rules=make_policy_document_present(".review/rules.md", "x" * over_rule_length),
    )
    with pytest.raises(PolicyBudgetExceededError) as error:
        verify_pre_send_guard(
            replace(context, policy_snapshot=over_snapshot),
            CharCounter(),
            max_input_tokens=50_000,
        )
    assert error.value.limit == cap
    assert error.value.actual == over_tokens


@pytest.mark.parametrize(
    "profile",
    [ExecutionProfile.fast("m1", "m2"), ExecutionProfile.deep("m1", "m2")],
    ids=["fast", "deep"],
)
def test_execution_profiles_keep_default_policy_cap(profile: ExecutionProfile) -> None:
    assert profile.max_policy_tokens == 12_000
