"""Unit tests for Policy Token Budget and Pre-Send Guards (Issue #120)."""

from __future__ import annotations

import uuid

import pytest

from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.policy_snapshot import (
    CallManifest,
    ExecutionProfile,
    ManifestBlock,
    PolicySnapshot,
    PromptBindings,
    SourceRole,
    make_policy_document_missing,
    make_policy_document_present,
)
from app.modules.reviews.application.prompt_budget import (
    EstimatedTokenCounter,
    LegacyTokenCounter,
    PolicyBudgetExceededError,
    PreSendGuardError,
    policy_tokens,
    token_counter_for,
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


class CharCounter:
    """1 char = 1 token for predictable assertions."""

    def count(self, text: str) -> int:
        return len(text)


def make_snapshot(
    rules_text: str = "",
    agents_text: str = "",
    ignore_patterns: tuple[str, ...] = (),
) -> PolicySnapshot:
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
        ignore_patterns=ignore_patterns,
        ignore_digest="id_0",
        parser_profile="rules-markdown/v1",
        parser_version="4.0.0",
        matcher_version="review-ignore/v1",
        limits_version="policy-limits/v1",
        builtin_exclusions_version="builtin-exclusions/v1",
        priority_matcher_version="priority-matcher/v1",
        service_defaults_rule_version_id=uuid.uuid4(),
        defaults_checksum="defaults_0",
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
    bad_block = ManifestBlock(
        block_id="b1",
        source_role=SourceRole.DIFF,
        source_kind="disallowed_kind",
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
            "PR", None, "u", "b", "main", (), 1, 1, 0, False, False, head_sha="sha_head_123"
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
    assert isinstance(token_counter_for("estimated/v2"), EstimatedTokenCounter)
    assert isinstance(token_counter_for(None), EstimatedTokenCounter)
    assert isinstance(token_counter_for("counter/v1"), LegacyTokenCounter)
    assert isinstance(token_counter_for("estimated/v1"), LegacyTokenCounter)

    with pytest.raises(PolicyError) as exc_info:
        token_counter_for("unknown/version")
    assert exc_info.value.code == PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR
    assert "Unknown counter_version" in exc_info.value.message


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
