"""System log scrubber in JsonFormatter (#468 Part 2).

Verifies that GitHub tokens, JWTs, Authorization credentials, and configured process secrets
are scrubbed from formatted JSON logs without modifying trace/run metadata or leaking non-secret
identifiers, and verifies end-to-end logging to real process stderr.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.common.infrastructure.telemetry import (
    JsonFormatter,
    bind_run_id,
    configure_logging,
)

pytest_plugins = ("tests.test_telemetry",)


def _format_record(
    formatter: JsonFormatter,
    msg: str,
    *args: object,
    level: int = logging.INFO,
    exc_info: Any = None,
) -> dict[str, Any]:
    record = logging.LogRecord(
        name="test.log.scrub",
        level=level,
        pathname="test_log_scrub.py",
        lineno=1,
        msg=msg,
        args=args,
        exc_info=exc_info,
    )
    return dict(json.loads(formatter.format(record)))


@pytest.mark.parametrize("prefix", ["ghp", "ghs", "gho", "ghu", "ghr", "github_pat"])
def test_github_tokens_are_scrubbed_from_message_and_args(prefix: str) -> None:
    token = f"{prefix}_SampleTokenValue1234567890ABCDEF"
    formatter = JsonFormatter("test-service")

    direct = _format_record(formatter, f"Direct message with {token}")
    via_args = _format_record(formatter, "Token in args: %s", token, level=logging.WARNING)

    assert token not in json.dumps(direct)
    assert token not in json.dumps(via_args)
    assert direct["message"] == "Direct message with ***"
    assert via_args["message"] == "Token in args: ***"
    assert direct["service"] == "test-service"
    assert direct["level"] == "INFO"


def test_jwt_tokens_are_scrubbed() -> None:
    jwt_token = ".".join(
        [
            "ey" + "JhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9",
            "ey" + "Jpc3MiOiIxMjM0NTYiLCJzdWIiOiJ0ZXN0In0",
            "SignatureBase64UrlSample1234567890",
        ]
    )
    formatter = JsonFormatter("test-service")
    entry = _format_record(formatter, "Bearer token issued: %s", jwt_token)

    assert jwt_token not in entry["message"]
    assert entry["message"] == "Bearer token issued: ***"


@pytest.mark.parametrize(
    ("raw_message", "hidden_secret", "expected_message"),
    [
        (
            "Sending request with Authorization: Bearer raw-secret-value-123 to endpoint",
            "raw-secret-value-123",
            "Sending request with Authorization: Bearer *** to endpoint",
        ),
        (
            "Basic auth header: Authorization: Basic dXNlcjpwYXNzd29yZA==",
            "dXNlcjpwYXNzd29yZA==",
            "Basic auth header: Authorization: Basic ***",
        ),
        (
            'JSON headers payload: {"Authorization": "Bearer secret-payload-val"}',
            "secret-payload-val",
            'JSON headers payload: {"Authorization": "Bearer ***"}',
        ),
        (
            'Quoted scheme payload: {"Authorization": "Digest username=\\"x\\", response=\\"y\\""}',
            "response=",
            'Quoted scheme payload: {"Authorization": "***"}',
        ),
    ],
)
def test_authorization_headers_are_scrubbed(
    raw_message: str, hidden_secret: str, expected_message: str
) -> None:
    formatter = JsonFormatter("test-service")
    entry = _format_record(formatter, raw_message)

    assert hidden_secret not in entry["message"]
    assert entry["message"] == expected_message


def test_auth_quoted_pattern_redos_prevention() -> None:
    """Unclosed quotes with repeated escape sequences must not cause catastrophic backtracking."""
    payload = 'Authorization: "' + "\\n" * 40
    formatter = JsonFormatter("test-service")
    t0 = time.perf_counter()
    entry = _format_record(formatter, payload)
    duration = time.perf_counter() - t0
    assert duration < 0.1
    assert payload in entry["message"]


def test_configured_process_secrets_cached_at_init_and_non_secrets_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm_primary = "replace-with-random-llm-key-777"
    llm_secondary = "replace-with-secondary-888"
    gitlab_key = "replace-with-gitlab-keyring-secret-999"
    monkeypatch.setenv("LLM_API_KEYS", f"{llm_primary},{llm_secondary}")
    monkeypatch.setenv("GITLAB_CREDENTIAL_KEYS", json.dumps({"key-2026-10": gitlab_key}))
    # Malformed keyring JSON is ignored safely without raising or breaking valid secrets.
    monkeypatch.setenv("BITBUCKET_CREDENTIAL_KEYS", "{not-valid-json")
    # Non-secret metadata or short (<8 chars) values must never pollute log messages.
    monkeypatch.setenv("GITLAB_ACTIVE_KEY_ID", "key-2026-10-id")
    monkeypatch.setenv("BITBUCKET_ACTIVE_KEY_ID", "bb-key-id-99")
    monkeypatch.setenv("GNOME_KEYRING_CONTROL", "/run/user/1000/keyring")
    monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", "16384")
    monkeypatch.setenv("POSTGRES_PASSWORD", "short7")

    formatter = JsonFormatter("test-service")

    # Mutating env after JsonFormatter construction proves secrets were cached in __init__.
    monkeypatch.setenv("LLM_API_KEYS", "different-late-secret-value")

    msg = (
        f"keys={llm_primary} {llm_secondary} {gitlab_key}; "
        "meta=key-2026-10-id bb-key-id-99 /run/user/1000/keyring 16384 short7"
    )
    entry = _format_record(formatter, msg)
    assert llm_primary not in entry["message"]
    assert llm_secondary not in entry["message"]
    assert gitlab_key not in entry["message"]
    assert "key-2026-10-id" in entry["message"]
    assert "bb-key-id-99" in entry["message"]
    assert "/run/user/1000/keyring" in entry["message"]
    assert "16384" in entry["message"]
    assert "short7" in entry["message"]


def test_secret_with_comma_scrubbed_as_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    """A secret containing commas (e.g. password) is scrubbed as a whole value."""
    password = "ab,cd,efghijkl"
    monkeypatch.setenv("POSTGRES_PASSWORD", password)
    formatter = JsonFormatter("test-service")
    entry = _format_record(formatter, "Database auth with password: %s", password)
    assert password not in entry["message"]
    assert "efghijkl" not in entry["message"]
    assert entry["message"] == "Database auth with password: ***"


def test_exception_and_trace_metadata_ac3_ac5(
    monkeypatch: pytest.MonkeyPatch, spans: InMemorySpanExporter
) -> None:
    token = "ghp_ExceptionToken1234567890abcdef"
    llm_val = "replace-with-llm-in-exception-999"
    monkeypatch.setenv("LLM_API_KEYS", llm_val)
    formatter = JsonFormatter("test-service")
    run_id = UUID("4fabfd21-5acc-4351-a99c-349dffdc00cf")

    with (
        trace.get_tracer("test.log.scrub").start_as_current_span("test-span"),
        bind_run_id(run_id),
    ):
        try:
            raise ValueError(f"Connection failed using {token} and {llm_val}")
        except ValueError as exc:
            entry = _format_record(
                formatter,
                "Failed with error: %s",
                exc,
                level=logging.ERROR,
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    serialized = json.dumps(entry)
    assert token not in serialized
    assert llm_val not in serialized
    assert entry["service"] == "test-service"
    assert entry["level"] == "ERROR"
    assert entry["run_id"] == str(run_id)
    assert entry["error_type"] == "ValueError"
    assert len(entry["trace_id"]) == 32
    assert len(entry["span_id"]) == 16
    assert "exc_info" in entry
    assert "ValueError" in entry["exc_info"]
    assert "***" in entry["message"]


def test_configure_logging_installs_scrubber_on_root_logger() -> None:
    configure_logging("worker")
    root = logging.getLogger()
    json_handlers = [h for h in root.handlers if h.get_name() == "dmc-json"]
    assert len(json_handlers) == 1
    assert isinstance(json_handlers[0].formatter, JsonFormatter)


def test_configure_logging_scrubs_real_process_stderr() -> None:
    secret_key = "replace-with-subprocess-llm-key-555"
    token = "gho_SubprocessOAuthToken1234567890"
    script = (
        "import logging, os; "
        "from app.common.infrastructure.telemetry import configure_logging; "
        "configure_logging('worker'); "
        "logging.getLogger('app.test').info("
        "'Bearer %s failed with %s', os.environ['SECRET_TOK'], os.environ['LLM_API_KEYS'])"
    )
    env = {
        **os.environ,
        "LLM_API_KEYS": secret_key,
        "SECRET_TOK": token,
    }
    repo_root = str(Path(__file__).resolve().parents[1])
    proc = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
        cwd=repo_root,
    )
    assert proc.stdout == ""
    assert secret_key not in proc.stderr
    assert token not in proc.stderr
    lines = [json.loads(line) for line in proc.stderr.strip().splitlines() if line.strip()]
    assert len(lines) == 1
    assert lines[0]["service"] == "worker"
    assert lines[0]["level"] == "INFO"
    assert lines[0]["message"] == "Bearer *** failed with ***"
