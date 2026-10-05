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
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from jsonschema import Draft202012Validator

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.bootstrap.llm_gateway import (  # noqa: E402
    DEFAULT_SYSTEM_PROMPT,
    ReviewCase,
    ReviewCaseFailed,
    review_case,
)
from app.modules.reviews.application.llm import EngineName, LlmCallRecord  # noqa: E402
from app.modules.reviews.application.prompt_builder import (  # noqa: E402
    PullRequestMeta,
    ReviewRule,
    parse_unified_diff,
    review_rule_from_stored,
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
from review.scripts.eval_provenance import (  # noqa: E402
    DigestInputError,
    corpus_input_paths,
    digest_files,
    rule_json_paths,
    static_input_paths,
)
from review.scripts.eval_replay import (  # noqa: E402
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


def _case_input(
    case_dir: Path,
    record: Mapping[str, Any],
    *,
    system: str,
    rules: tuple[ReviewRule, ...],
    engine: EngineName,
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
            commit_messages=(),
        ),
        engine=engine,
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
) -> dict[str, Any]:
    """Call ``review_case`` once per validated case and save only first-call text."""
    records = _validated_cases(root)
    responses_dir = root / "responses"
    if responses_dir.exists() and any(responses_dir.iterdir()):
        raise RecorderError("responses directory is not empty; preserve the existing baseline")
    if responses_dir.is_symlink():
        raise RecorderError("responses directory cannot be a symlink")

    def relative_input(path: Path) -> str:
        try:
            return path.absolute().relative_to(REPO_ROOT).as_posix()
        except ValueError as exc:
            raise RecorderError("static input must be inside the repository") from exc

    relative_prompt = relative_input(system_prompt)
    languages = {record["language"] for _, record in records}
    selected_rules = []
    if "python" in languages:
        selected_rules.append(relative_input(backend_rules))
    if languages & {"typescript", "tsx"}:
        selected_rules.append(relative_input(frontend_rules))
    try:
        static_inputs = static_input_paths(
            relative_prompt, [*rule_json_paths(REPO_ROOT), *selected_rules]
        )
        static_digest = digest_files(REPO_ROOT, static_inputs)
        corpus_digest = digest_files(root, corpus_input_paths(root, records))
    except DigestInputError as exc:
        raise RecorderError(str(exc)) from exc
    prompt_bytes = system_prompt.read_bytes()
    system = prompt_bytes.decode("utf-8")
    version = _PROMPT_VERSION.search(system)
    if version is None:
        raise RecorderError("system prompt has no version")
    by_language: dict[str, tuple[ReviewRule, ...]] = {}
    if "python" in languages:
        by_language["python"] = _rules(backend_rules)
    if languages & {"typescript", "tsx"}:
        frontend = _rules(frontend_rules)
        by_language["typescript"] = frontend
        by_language["tsx"] = frontend
    policy = settings.policy
    effective_settings = {
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
    timestamp = (
        (recorded_at or datetime.now(UTC)).astimezone(UTC).isoformat().replace("+00:00", "Z")
    )
    statuses: dict[str, dict[str, str | bool | None]] = {}
    paths: dict[str, str] = {}
    with TemporaryDirectory(prefix=".responses-", dir=root) as staging_name:
        staging_dir = Path(staging_name)
        for case_dir, record in records:
            case_id = record["id"]
            case = _case_input(
                case_dir,
                record,
                system=system,
                rules=by_language[record["language"]],
                engine=engine,
            )
            try:
                result = await review_case(case, settings, transport=transport)
                calls = result.calls
                gateway_status = "accepted"
            except ReviewCaseFailed as failure:
                calls = failure.trace
                gateway_status = failure.error_code.value
            first = calls[0] if calls else None
            content = _first_text(first)
            provider_label, provider_digest = _first_serving_provider(first)
            relative_response = f"responses/{case_id}.json"
            (staging_dir / f"{case_id}.json").write_bytes(content.encode("utf-8"))
            paths[case_id] = relative_response
            statuses[case_id] = {
                "first_call": (
                    "no_call"
                    if first is None
                    else "no_content"
                    if not isinstance(first.response, dict)
                    else "answer"
                    if content
                    else "empty_answer"
                ),
                "first_model": first.model if first else None,
                "first_kind": first.kind.value if first else None,
                "first_provider_label": provider_label,
                "first_provider_digest": provider_digest,
                "gateway_status": gateway_status,
                "paid_metadata_error": any(call.paid_metadata_error for call in calls),
            }
        nonpublishable_ids = nonpublishable_case_ids(statuses)
        manifest: dict[str, Any] = {
            "schema_version": 1,
            "model_id": settings.primary.model,
            "prompt_path": relative_prompt,
            "prompt_sha": hashlib.sha256(prompt_bytes).hexdigest(),
            "prompt_version": f"v{version.group(1)}",
            "static_inputs": static_inputs,
            "static_digest": static_digest,
            "corpus_digest": corpus_digest,
            "run_metadata": {
                "recorded_at": timestamp,
                "engine": engine,
                "fallback_model_id": settings.fallback.model if settings.fallback else None,
                "effective_settings": effective_settings,
                "baseline_publishable": not nonpublishable_ids,
                "nonpublishable_case_ids": nonpublishable_ids,
                "cases": statuses,
            },
            "responses": paths,
        }
        (staging_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        if responses_dir.is_symlink():
            raise RecorderError("responses directory cannot be a symlink")
        if responses_dir.exists():
            if any(responses_dir.iterdir()):
                raise RecorderError(
                    "responses directory is not empty; preserve the existing baseline"
                )
            responses_dir.rmdir()
        staging_dir.rename(responses_dir)
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
    args = parser.parse_args(argv)
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
            )
        )
        report = replay(args.root)
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
                raw = (args.root / item["path"]).read_bytes()
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
