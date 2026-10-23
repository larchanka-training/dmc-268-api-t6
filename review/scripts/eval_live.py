#!/usr/bin/env python3
"""Record each validated gold case's first raw provider answer through the gateway.

Usage: uv run --env-file .env python review/scripts/eval_live.py

Run only after model funding and prompt inputs are frozen. Responses are first-call
content bytes, including malformed JSON; an absent answer becomes an empty file.
The manifest contains redacted call status and an effective model-settings snapshot,
never prompts or provider envelopes. The snapshot records only an SHA-256 digest
of canonical extra_body JSON; it does not persist its keys or values. Provider
identity uses an allowlisted label and digest, and endpoint identity uses only
a digest of its canonical URL, never raw credentials or query text. Effective
prices and gateway policy limits are recorded as safe numeric metadata.
Each case also records the first response's actual serving provider as a safe
allowlisted label and domain-separated digest, or null when no provider answered.

--policy-arm (#163) selects the repository policy shown to the model: ``legacy`` (default)
renders no policy snapshot, exactly the prompt before #163; ``none``, ``valid`` and
``malicious`` render a snapshot whose rules document is missing, the case's
``.review/rules.md``, or its ``policy.arms.malicious`` file, with AGENTS.md from the case
base. The manifest records the arm, a digest of the arm files and the token and cost
usage of every paid call. --responses-dir keeps arms and repeated runs apart.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid5

from jsonschema import Draft202012Validator

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.bootstrap.llm_gateway import (  # noqa: E402
    DEFAULT_SYSTEM_PROMPT,
    ReviewCase,
    ReviewCaseFailed,
    review_case,
)
from app.modules.reviews.application.commit_prefix_limits import (  # noqa: E402
    CommitMessagePrefix,
)
from app.modules.reviews.application.llm import (  # noqa: E402
    EngineName,
    LlmCallRecord,
    LlmUsage,
)
from app.modules.reviews.application.policy_documents import normalize_document  # noqa: E402
from app.modules.reviews.application.policy_errors import PolicyError  # noqa: E402
from app.modules.reviews.application.policy_snapshot import (  # noqa: E402
    AGENTS_DOCUMENT_PATH,
    POLICY_SNAPSHOT_SCHEMA_VERSION,
    RULES_DOCUMENT_PATH,
    PolicyDocument,
    PolicySnapshot,
    make_complete_execution_profile,
    make_complete_prompt_bindings,
    make_policy_document_missing,
)
from app.modules.reviews.application.prompt_builder import (  # noqa: E402
    PullRequestMeta,
    ReviewRule,
    parse_unified_diff,
    review_rule_from_stored,
)
from app.modules.reviews.application.review_ignore import (  # noqa: E402
    BUILTIN_EXCLUSIONS_VERSION,
    LIMITS_VERSION,
    MATCHER_VERSION,
    PARSER_PROFILE,
    PARSER_VERSION,
    parse_review_ignore,
)
from app.modules.reviews.infrastructure.llm.settings import (  # noqa: E402
    LlmConfigError,
    LlmSettings,
    ModelProfile,
)
from app.modules.reviews.infrastructure.llm.transport import (  # noqa: E402
    ChatTransport,
    extract_text_content,
)
from review.scripts.commit_context import (  # noqa: E402
    CommitContextError,
    bind_commit_context,
    load_canonical_identities,
    parse_commit_context,
)
from review.scripts.eval_provenance import (  # noqa: E402
    DigestInputError,
    canonical_identity_paths,
    corpus_input_paths,
    digest_files,
    rule_json_paths,
    static_input_paths,
)
from review.scripts.eval_replay import (  # noqa: E402
    RECORDED_POLICY_ARMS,
    ReplayError,
    format_console,
    nonpublishable_case_ids,
    replay,
)
from review.scripts.validate_dataset import (  # noqa: E402
    DEFAULT_ROOT,
    SCHEMA_PATH,
    validate_case,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_RULES = REPO_ROOT / "review/rules/default-backend.v1.json"
FRONTEND_RULES = REPO_ROOT / "review/rules/default-frontend.v1.json"
_PROMPT_VERSION = re.compile(r"^version: (\d+)$", re.MULTILINE)
_EXTRA_BODY_MAGIC = b"review-eval-extra-body-v1\0"
_PROVIDER_MAGIC = b"review-eval-provider-v1\0"
_SERVING_PROVIDER_MAGIC = b"review-eval-serving-provider-v1\0"
_ENDPOINT_MAGIC = b"review-eval-endpoint-v1\0"
# Fixed identities of the in-memory eval snapshots; none of them is rendered into the prompt.
_EVAL_POLICY_NAMESPACE = UUID("6f1d2c43-1633-4d7e-9a35-163000000163")
_PUBLIC_PROVIDER_LABELS = frozenset({"eurouter", "self-hosted", "fake"})
_PUBLIC_SERVING_PROVIDERS = frozenset(
    {
        "mistral",
        "mistral ai",
        "ovhcloud",
        "scaleway",
        "greenpt",
        "regolo",
        "aki.io",
        "lyceum",
        "infercom",
    }
)


class RecorderError(Exception):
    """The corpus or recorder configuration cannot produce a complete baseline."""


def _validated_cases(root: Path) -> list[tuple[Path, dict[str, Any]]]:
    cases_root = root / "cases"
    if cases_root.is_symlink() or not cases_root.is_dir():
        raise RecorderError("cases directory is missing or is a symlink")
    case_dirs = sorted(path for path in cases_root.iterdir() if path.is_dir())
    if not case_dirs:
        raise RecorderError("no cases found")
    validator = Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))
    records = []
    for case_dir in case_dirs:
        errors, record = validate_case(case_dir, validator)
        if errors or record is None:
            raise RecorderError(f"invalid case {case_dir.name}: {'; '.join(errors)}")
        records.append((case_dir, record))
    return records


def _rules(path: Path) -> tuple[ReviewRule, ...]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("rules"), list):
        raise RecorderError(f"rule file has no rules array: {path.name}")
    return tuple(review_rule_from_stored(item) for item in value["rules"])


def _first_text(record: LlmCallRecord | None) -> str:
    """Use the transport's text-part semantics on the first raw provider envelope."""
    if record is None or not isinstance(record.response, dict):
        return ""
    choices = record.response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ""
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return ""
    return extract_text_content(message.get("content")) or ""


def _first_serving_provider(record: LlmCallRecord | None) -> tuple[str | None, str | None]:
    """Record the first actual route without trusting a provider-supplied string."""
    if record is None or not isinstance(record.response, dict):
        return None, None
    provider = record.response.get("provider")
    if not isinstance(provider, str) or not provider:
        return None, None
    label = provider.strip().casefold()
    if label not in _PUBLIC_SERVING_PROVIDERS:
        label = "custom"
    digest = hashlib.sha256(
        _SERVING_PROVIDER_MAGIC + provider.encode("utf-8", errors="surrogatepass")
    ).hexdigest()
    return label, "sha256-v1:" + digest


def _commit_context(records: list[tuple[Path, dict[str, Any]]]) -> dict[str, CommitMessagePrefix]:
    """Validate evidence, then bind all cohort identities and active configured cases."""
    try:
        sidecar = parse_commit_context(REPO_ROOT / "review/eval/pr-commit-messages.json")
        identity_paths = canonical_identity_paths(REPO_ROOT)
        digest_files(REPO_ROOT, identity_paths)
        cohort = load_canonical_identities(REPO_ROOT, identity_paths)
        return bind_commit_context(sidecar, [record for _, record in records], cohort)
    except (CommitContextError, DigestInputError, OSError):
        raise RecorderError("invalid commit context evidence or case binding") from None


def _policy_document(path: Path, snapshot_path: str) -> PolicyDocument:
    """Normalize one arm file with the production loader, as the policy snapshot stores it."""
    raw = path.read_bytes()
    blob_oid = hashlib.sha1(b"blob %d\0" % len(raw) + raw, usedforsecurity=False).hexdigest()
    return normalize_document(snapshot_path, raw, blob_oid=blob_oid)


def _policy_snapshot(
    case_dir: Path,
    record: Mapping[str, Any],
    arm: str,
    settings: LlmSettings,
    engine: EngineName,
) -> PolicySnapshot | None:
    """Build the in-memory snapshot of one arm; ``legacy`` keeps the pre-#163 envelope."""
    if arm == "legacy":
        return None
    base = case_dir / record["base_path"]
    try:
        if arm == "none":
            rules = make_policy_document_missing(RULES_DOCUMENT_PATH)
        else:
            # The valid file is the case's .review/rules.md; the malicious one is rendered at
            # the same fixed path, exactly where an attacker's rules.md would be.
            rules = _policy_document(base / record["policy"]["arms"][arm], RULES_DOCUMENT_PATH)
        agents_path = base / AGENTS_DOCUMENT_PATH
        agents = (
            _policy_document(agents_path, AGENTS_DOCUMENT_PATH)
            if agents_path.is_file()
            else make_policy_document_missing(AGENTS_DOCUMENT_PATH)
        )
        ignore = parse_review_ignore(rules.text)
    except (OSError, PolicyError) as exc:
        raise RecorderError(f"case {record['id']} has an unusable {arm} policy: {exc}") from None
    identity = f"{record['id']}:{arm}"
    digest_input = json.dumps(
        [arm, rules.raw_sha256, agents.raw_sha256, ignore.digest], separators=(",", ":")
    )
    return PolicySnapshot(
        schema_version=POLICY_SNAPSHOT_SCHEMA_VERSION,
        snapshot_id=uuid5(_EVAL_POLICY_NAMESPACE, f"snapshot:{identity}"),
        run_id=uuid5(_EVAL_POLICY_NAMESPACE, f"run:{identity}"),
        repository_id=uuid5(_EVAL_POLICY_NAMESPACE, f"repository:{record['id']}"),
        policy_base_sha="0" * 40,
        rules=rules,
        agents=agents,
        ignore_patterns=ignore.patterns,
        ignore_digest=ignore.digest,
        parser_profile=PARSER_PROFILE,
        parser_version=PARSER_VERSION,
        matcher_version=MATCHER_VERSION,
        limits_version=LIMITS_VERSION,
        builtin_exclusions_version=BUILTIN_EXCLUSIONS_VERSION,
        priority_matcher_version="priority-matcher/v1",
        service_defaults_rule_version_id=uuid5(_EVAL_POLICY_NAMESPACE, "service-defaults"),
        defaults_checksum="eval-default-rule-files",
        prompt_bindings=make_complete_prompt_bindings(),
        execution_profile=make_complete_execution_profile(
            engine,
            review_model=settings.primary.model,
            verifier_model=settings.primary.model,
        ),
        policy_digest=hashlib.sha256(digest_input.encode("utf-8")).hexdigest(),
    )


def _policy_arm_digest(
    root: Path, records: list[tuple[Path, dict[str, Any]]], arm: str
) -> str | None:
    """Digest the arm files shown to the model; ``none`` and ``legacy`` show no rules file."""
    if arm not in ("valid", "malicious"):
        return None
    paths = [
        (case_dir / record["base_path"] / record["policy"]["arms"][arm]).relative_to(root)
        for case_dir, record in records
    ]
    try:
        return digest_files(root, [path.as_posix() for path in paths])
    except DigestInputError as exc:
        raise RecorderError(str(exc)) from exc


def _usage_json(usage: tuple[LlmUsage, ...]) -> list[dict[str, object]]:
    """Token and USD usage of every paid call, as the gateway ledger records it.

    ``cost_usd`` is the provider-reported cost when the answer carried one, else the
    configured model price applied to the token counts. Model and provider strings are not
    copied: they come from the provider envelope.
    """
    return [
        {
            "operation": item.operation,
            "tokens_in": item.tokens_in,
            "tokens_out": item.tokens_out,
            "cache_read_tokens": item.cache_read_tokens,
            "cost_usd": _decimal_text(item.cost_usd),
        }
        for item in usage
    ]


def _case_input(
    case_dir: Path,
    record: Mapping[str, Any],
    *,
    system: str,
    rules: tuple[ReviewRule, ...],
    engine: EngineName,
    commit_prefix: CommitMessagePrefix,
    policy_snapshot: PolicySnapshot | None = None,
) -> ReviewCase:
    diff = (case_dir / record["patch_path"]).read_text(encoding="utf-8")
    files = parse_unified_diff(diff)
    return ReviewCase(
        diff=diff,
        system=system,
        rules=rules,
        pr_meta=PullRequestMeta(
            title="Review proposed change",
            description=None,
            author="gold-corpus",
            source_branch="eval",
            target_branch="main",
            labels=(),
            files_changed=len(files),
            lines_added=sum(line.type == "added" for file in files for line in file.lines),
            lines_removed=sum(line.type == "removed" for file in files for line in file.lines),
            is_draft=False,
            is_fork=False,
            head_sha=None,
            commit_messages=commit_prefix.messages,
            commit_messages_incomplete=commit_prefix.incomplete,
        ),
        engine=engine,
        policy_snapshot=policy_snapshot,
    )


def _canonical_endpoint(raw: str) -> str:
    """Normalize endpoint identity while discarding URL credentials and fragments."""
    try:
        parsed = urlsplit(raw)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise RecorderError("model base_url must be a valid HTTP endpoint") from exc
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or host is None:
        raise RecorderError("model base_url must be a valid HTTP endpoint")
    authority = host.lower()
    if ":" in authority:
        authority = f"[{authority}]"
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        authority += f":{port}"
    return urlunsplit((scheme, authority, parsed.path, parsed.query, ""))


def _decimal_text(value: Decimal) -> str:
    """Use the same decimal text for numerically equivalent configured prices."""
    return format(value.normalize(), "f")


def _effective_profile(
    profile: ModelProfile, settings: LlmSettings, engine: EngineName
) -> dict[str, object]:
    """Record message-shaping settings without persisting extra_body values or keys."""
    try:
        encoded_body = json.dumps(
            profile.extra_body,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RecorderError("model extra_body must be JSON serializable") from exc
    context_limit = min(profile.context_window, settings.policy.input_token_limit[engine])
    endpoint = _canonical_endpoint(profile.base_url).encode("utf-8")
    provider = profile.provider.encode("utf-8")
    return {
        "model_id": profile.model,
        "provider_label": (
            profile.provider if profile.provider in _PUBLIC_PROVIDER_LABELS else "custom"
        ),
        "provider_digest": "sha256-v1:" + hashlib.sha256(_PROVIDER_MAGIC + provider).hexdigest(),
        "endpoint_digest": "sha256-v1:" + hashlib.sha256(_ENDPOINT_MAGIC + endpoint).hexdigest(),
        "context_window": profile.context_window,
        "max_output_tokens": profile.max_output_tokens,
        "input_budget_tokens": context_limit - profile.max_output_tokens,
        "structured_output": profile.structured_output,
        "chars_per_token": profile.chars_per_token,
        "price_usd_per_mtok": {
            "input": _decimal_text(profile.price.input_per_mtok),
            "output": _decimal_text(profile.price.output_per_mtok),
            "cache_read": _decimal_text(profile.price.cache_read_per_mtok),
        },
        "extra_body_digest": "sha256-v1:"
        + hashlib.sha256(_EXTRA_BODY_MAGIC + encoded_body).hexdigest(),
    }


def _effective_settings(settings: LlmSettings, engine: EngineName) -> dict[str, object]:
    """Snapshot the effective profiles and policy used for this capture."""
    policy = settings.policy
    return {
        "primary": _effective_profile(settings.primary, settings, engine),
        "fallback": (
            _effective_profile(settings.fallback, settings, engine)
            if settings.fallback is not None
            else None
        ),
        "policy": {
            "input_token_limit": policy.input_token_limit[engine],
            "call_timeout_s": policy.call_timeout_s[engine],
            "run_cost_limit_usd": _decimal_text(policy.run_cost_limit_usd[engine]),
            "max_calls_per_attempt": policy.max_calls_per_attempt,
            "timeout_retry_delays_s": list(policy.timeout_retry_delays_s),
            "unavailable_retry_delays_s": list(policy.unavailable_retry_delays_s),
            "max_jitter_s": policy.max_jitter_s,
            "max_retry_after_s": policy.max_retry_after_s,
            "rate_limit_default_delay_s": policy.rate_limit_default_delay_s,
        },
    }


@dataclass(frozen=True)
class _CaptureInputs:
    """Provenance and prompt inputs, resolved and validated before the first model call."""

    prompt_path: str
    prompt_sha: str
    prompt_version: str
    system: str
    static_inputs: list[str]
    static_digest: str
    corpus_digest: str
    rules_by_language: dict[str, tuple[ReviewRule, ...]]
    effective_settings: dict[str, object]
    recorded_at: str


def _relative_input(path: Path) -> str:
    try:
        return path.absolute().relative_to(REPO_ROOT).as_posix()
    except ValueError as exc:
        raise RecorderError("static input must be inside the repository") from exc


def _selected_rule_sets(
    records: list[tuple[Path, dict[str, Any]]], backend_rules: Path, frontend_rules: Path
) -> list[tuple[Path, tuple[str, ...]]]:
    """Each default rule file whose languages occur in the corpus, with those languages."""
    languages = {record["language"] for _, record in records}
    return [
        (rule_path, matching)
        for rule_path, supported in (
            (backend_rules, ("python",)),
            (frontend_rules, ("typescript", "tsx")),
        )
        if (matching := tuple(language for language in supported if language in languages))
    ]


def _input_digests(
    root: Path,
    records: list[tuple[Path, dict[str, Any]]],
    relative_prompt: str,
    selected_rules: list[str],
) -> tuple[list[str], str, str]:
    """Return the static input paths, the static digest and the corpus digest."""
    try:
        static_inputs = static_input_paths(
            relative_prompt, [*rule_json_paths(REPO_ROOT), *selected_rules], repo_root=REPO_ROOT
        )
        static_digest = digest_files(REPO_ROOT, static_inputs)
        corpus_digest = digest_files(root, corpus_input_paths(root, records))
    except DigestInputError as exc:
        raise RecorderError(str(exc)) from exc
    return static_inputs, static_digest, corpus_digest


def _rules_by_language(
    rule_sets: list[tuple[Path, tuple[str, ...]]],
) -> dict[str, tuple[ReviewRule, ...]]:
    by_language: dict[str, tuple[ReviewRule, ...]] = {}
    for rule_path, matching in rule_sets:
        parsed_rules = _rules(rule_path)
        for language in matching:
            by_language[language] = parsed_rules
    return by_language


def _capture_inputs(
    root: Path,
    records: list[tuple[Path, dict[str, Any]]],
    settings: LlmSettings,
    *,
    system_prompt: Path,
    backend_rules: Path,
    frontend_rules: Path,
    recorded_at: datetime | None,
    engine: EngineName,
) -> _CaptureInputs:
    relative_prompt = _relative_input(system_prompt)
    rule_sets = _selected_rule_sets(records, backend_rules, frontend_rules)
    static_inputs, static_digest, corpus_digest = _input_digests(
        root, records, relative_prompt, [_relative_input(path) for path, _ in rule_sets]
    )
    prompt_bytes = system_prompt.read_bytes()
    system = prompt_bytes.decode("utf-8")
    version = _PROMPT_VERSION.search(system)
    if version is None:
        raise RecorderError("system prompt has no version")
    rules_by_language = _rules_by_language(rule_sets)
    effective_settings = _effective_settings(settings, engine)
    timestamp = (
        (recorded_at or datetime.now(UTC)).astimezone(UTC).isoformat().replace("+00:00", "Z")
    )
    return _CaptureInputs(
        prompt_path=relative_prompt,
        prompt_sha=hashlib.sha256(prompt_bytes).hexdigest(),
        prompt_version=f"v{version.group(1)}",
        system=system,
        static_inputs=static_inputs,
        static_digest=static_digest,
        corpus_digest=corpus_digest,
        rules_by_language=rules_by_language,
        effective_settings=effective_settings,
        recorded_at=timestamp,
    )


def _first_call_status(first: LlmCallRecord | None, content: str) -> str:
    if first is None:
        return "no_call"
    if not isinstance(first.response, dict):
        return "no_content"
    return "answer" if content else "empty_answer"


async def _record_case(
    case_dir: Path,
    record: dict[str, Any],
    inputs: _CaptureInputs,
    settings: LlmSettings,
    *,
    engine: EngineName,
    transport: ChatTransport | None,
    staging_dir: Path,
    commit_prefix: CommitMessagePrefix,
    policy_snapshot: PolicySnapshot | None,
) -> tuple[dict[str, str | bool | None], list[dict[str, object]]]:
    """Stage one case's first-call text; return its redacted call status and usage."""
    case = _case_input(
        case_dir,
        record,
        system=inputs.system,
        rules=inputs.rules_by_language[record["language"]],
        engine=engine,
        commit_prefix=commit_prefix,
        policy_snapshot=policy_snapshot,
    )
    try:
        result = await review_case(case, settings, transport=transport)
        calls = result.calls
        usage = result.usage
        gateway_status = "accepted"
    except ReviewCaseFailed as failure:
        calls = failure.trace
        usage = failure.usage
        gateway_status = failure.error_code.value
    first = calls[0] if calls else None
    content = _first_text(first)
    provider_label, provider_digest = _first_serving_provider(first)
    (staging_dir / f"{record['id']}.json").write_bytes(content.encode("utf-8"))
    status: dict[str, str | bool | None] = {
        "first_call": _first_call_status(first, content),
        "first_model": first.model if first else None,
        "first_kind": first.kind.value if first else None,
        "first_provider_label": provider_label,
        "first_provider_digest": provider_digest,
        "gateway_status": gateway_status,
        "paid_metadata_error": any(call.paid_metadata_error for call in calls),
    }
    return status, _usage_json(usage)


def _manifest(
    inputs: _CaptureInputs,
    settings: LlmSettings,
    *,
    engine: EngineName,
    statuses: dict[str, dict[str, str | bool | None]],
    paths: dict[str, str],
    policy_arm: str,
    policy_arm_digest: str | None,
    usage: dict[str, list[dict[str, object]]],
) -> dict[str, Any]:
    nonpublishable_ids = nonpublishable_case_ids(statuses)
    return {
        "schema_version": 1,
        "model_id": settings.primary.model,
        "prompt_path": inputs.prompt_path,
        "prompt_sha": inputs.prompt_sha,
        "prompt_version": inputs.prompt_version,
        "static_inputs": inputs.static_inputs,
        "static_digest": inputs.static_digest,
        "corpus_digest": inputs.corpus_digest,
        "run_metadata": {
            "recorded_at": inputs.recorded_at,
            "engine": engine,
            "fallback_model_id": settings.fallback.model if settings.fallback else None,
            "effective_settings": inputs.effective_settings,
            "baseline_publishable": not nonpublishable_ids,
            "nonpublishable_case_ids": nonpublishable_ids,
            "cases": statuses,
            "policy_arm": policy_arm,
            "policy_arm_digest": policy_arm_digest,
            "usage": usage,
        },
        "responses": paths,
    }


def _publish_capture(staging_dir: Path, responses_dir: Path, manifest: dict[str, Any]) -> None:
    """Write the manifest and move the complete capture into a still-empty responses dir."""
    (staging_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    if responses_dir.is_symlink():
        raise RecorderError("responses directory cannot be a symlink")
    if responses_dir.exists():
        if any(responses_dir.iterdir()):
            raise RecorderError("responses directory is not empty; preserve the existing baseline")
        responses_dir.rmdir()
    staging_dir.rename(responses_dir)


async def record_live(
    root: Path,
    settings: LlmSettings,
    *,
    system_prompt: Path,
    backend_rules: Path,
    frontend_rules: Path,
    transport: ChatTransport | None = None,
    recorded_at: datetime | None = None,
    engine: EngineName = "fast",
    policy_arm: str = "legacy",
    responses_dir: Path | None = None,
) -> dict[str, Any]:
    """Call ``review_case`` once per validated case and save only first-call text.

    ``responses_dir`` defaults to ``<root>/responses``; it must be new or empty.
    """
    if policy_arm not in RECORDED_POLICY_ARMS:
        raise RecorderError(f"unknown policy arm: {policy_arm}")
    records = _validated_cases(root)
    commit_context = _commit_context(records)
    if policy_arm in ("valid", "malicious"):
        without_policy = [record["id"] for _, record in records if "policy" not in record]
        if without_policy:
            raise RecorderError(
                f"--policy-arm {policy_arm} needs case policy arms; missing in "
                + ", ".join(without_policy)
            )
    responses_dir = root / "responses" if responses_dir is None else responses_dir
    if responses_dir.exists() and any(responses_dir.iterdir()):
        raise RecorderError("responses directory is not empty; preserve the existing baseline")
    if responses_dir.is_symlink():
        raise RecorderError("responses directory cannot be a symlink")
    if responses_dir.absolute().resolve().is_relative_to((root / "cases").resolve()):
        raise RecorderError("responses directory must be outside the cases directory")
    if not responses_dir.absolute().parent.is_dir():
        raise RecorderError("responses directory parent must already exist")
    snapshots = {
        record["id"]: _policy_snapshot(case_dir, record, policy_arm, settings, engine)
        for case_dir, record in records
    }
    policy_arm_digest = _policy_arm_digest(root, records, policy_arm)
    inputs = _capture_inputs(
        root,
        records,
        settings,
        system_prompt=system_prompt,
        backend_rules=backend_rules,
        frontend_rules=frontend_rules,
        recorded_at=recorded_at,
        engine=engine,
    )
    statuses: dict[str, dict[str, str | bool | None]] = {}
    usage: dict[str, list[dict[str, object]]] = {}
    paths: dict[str, str] = {}
    # Staged next to the target, so the final rename never crosses a filesystem.
    staging_parent = responses_dir.absolute().parent
    with TemporaryDirectory(prefix=".responses-", dir=staging_parent) as staging_name:
        staging_dir = Path(staging_name)
        for case_dir, record in records:
            case_id = record["id"]
            statuses[case_id], usage[case_id] = await _record_case(
                case_dir,
                record,
                inputs,
                settings,
                engine=engine,
                transport=transport,
                staging_dir=staging_dir,
                commit_prefix=commit_context[case_id],
                policy_snapshot=snapshots[case_id],
            )
            paths[case_id] = f"responses/{case_id}.json"
        manifest = _manifest(
            inputs,
            settings,
            engine=engine,
            statuses=statuses,
            paths=paths,
            policy_arm=policy_arm,
            policy_arm_digest=policy_arm_digest,
            usage=usage,
        )
        _publish_capture(staging_dir, responses_dir, manifest)
    return manifest


def _validate_export_destination(path: Path, root: Path, label: str) -> Path:
    """Reserve an unused export path outside inputs before any provider request."""
    absolute = path.absolute()
    if ".." in absolute.parts:
        raise RecorderError(f"{label} path must not contain parent traversal")
    if any(part.is_symlink() for part in (absolute, *absolute.parents)):
        raise RecorderError(f"{label} path must not contain a symlink")
    resolved = absolute.resolve()
    if resolved.is_relative_to(root.resolve()) or resolved.is_relative_to(REPO_ROOT.resolve()):
        raise RecorderError(f"{label} path must be outside dataset and repository")
    if absolute.exists():
        raise RecorderError(f"{label} path already exists")
    if not absolute.parent.is_dir():
        raise RecorderError(f"{label} parent directory must already exist")
    return resolved


def main(
    argv: list[str] | None = None,
    *,
    transport: ChatTransport | None = None,
    model_settings: LlmSettings | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--system", type=Path, default=DEFAULT_SYSTEM_PROMPT)
    parser.add_argument("--backend-rules", type=Path, default=BACKEND_RULES)
    parser.add_argument("--frontend-rules", type=Path, default=FRONTEND_RULES)
    parser.add_argument("--engine", choices=("fast", "deep"), default="fast")
    parser.add_argument("--report-json", type=Path, metavar="PATH")
    parser.add_argument("--redacted-responses", type=Path, metavar="DIR")
    parser.add_argument("--policy-arm", choices=RECORDED_POLICY_ARMS, default="legacy")
    parser.add_argument("--responses-dir", type=Path, metavar="DIR")
    args = parser.parse_args(argv)
    responses_dir: Path = args.responses_dir or args.root / "responses"
    try:
        if args.report_json and args.redacted_responses:
            report = args.report_json.absolute().resolve()
            exported = args.redacted_responses.absolute().resolve()
            if (
                report == exported
                or report.is_relative_to(exported)
                or exported.is_relative_to(report)
            ):
                raise RecorderError("report JSON and redacted export paths overlap")
        if args.report_json:
            _validate_export_destination(args.report_json, args.root, "report JSON")
        if args.redacted_responses:
            _validate_export_destination(args.redacted_responses, args.root, "redacted export")
        capture = responses_dir.absolute().resolve()
        for output in (args.report_json, args.redacted_responses):
            if output is not None and (
                (resolved := output.absolute().resolve()).is_relative_to(capture)
                or capture.is_relative_to(resolved)
            ):
                raise RecorderError("report or export path overlaps the responses directory")
        settings = model_settings or LlmSettings.from_env(os.environ)
        manifest = asyncio.run(
            record_live(
                args.root,
                settings,
                system_prompt=args.system,
                backend_rules=args.backend_rules,
                frontend_rules=args.frontend_rules,
                transport=transport,
                engine=args.engine,
                policy_arm=args.policy_arm,
                responses_dir=responses_dir,
            )
        )
        report = replay(args.root, responses_dir=responses_dir)
        if args.report_json:
            # Validator diagnostics can quote model text, so the uploaded report
            # contains metrics and statuses but no diagnostic/model content.
            redacted = {
                **report,
                "responses": [
                    {key: value for key, value in item.items() if key != "validator_output"}
                    for item in report["responses"]
                ],
            }
            with args.report_json.open("x", encoding="utf-8") as output:
                output.write(
                    json.dumps(redacted, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
                )
        if args.redacted_responses:
            args.redacted_responses.mkdir()
            statuses = manifest["run_metadata"]["cases"]
            for item in report["responses"]:
                case_id = item["case_id"]
                raw = (responses_dir / Path(item["path"]).name).read_bytes()
                metadata = {
                    "case_id": case_id,
                    "raw_sha256": hashlib.sha256(raw).hexdigest(),
                    "raw_bytes": len(raw),
                    "first_call": statuses[case_id]["first_call"],
                    "gateway_status": statuses[case_id]["gateway_status"],
                    "valid": item["valid"],
                    "validator_exit_code": item["validator_exit_code"],
                }
                with (args.redacted_responses / f"{case_id}.json").open(
                    "x", encoding="utf-8"
                ) as output:
                    output.write(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    except (LlmConfigError, RecorderError, ReplayError, OSError, ValueError) as exc:
        print(f"eval live: {exc}", file=sys.stderr)
        return 1
    print(format_console(report))
    if not manifest["run_metadata"]["baseline_publishable"]:
        print(
            "eval live: not publishable; provider or infrastructure failures occurred in "
            + ", ".join(manifest["run_metadata"]["nonpublishable_case_ids"]),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
