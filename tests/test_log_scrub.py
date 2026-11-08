"""System log scrubber in JsonFormatter (#468 Part 2).

Verifies that GitHub tokens, JWTs, Authorization headers, and configured process secrets
(e.g. LLM API keys) are scrubbed from messages, args, and exceptions before writing to stderr.
"""

from __future__ import annotations

import io
import json
import logging
import sys
from typing import Any

import pytest

from app.common.infrastructure.telemetry import (
    JsonFormatter,
    configure_logging,
)


def _parse_stderr_lines(stderr_text: str) -> list[dict[str, Any]]:
    lines = []
    for line in stderr_text.strip().splitlines():
        if line.strip():
            lines.append(json.loads(line))
    return lines


def _make_fake_token(kind: str, val: str) -> str:
    return f"{kind}_{val}"


def test_github_tokens_are_scrubbed_from_message_and_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_stderr = io.StringIO()
    monkeypatch.setattr(sys, "stderr", fake_stderr)

    configure_logging("test-service")
    logger = logging.getLogger("test.logger.github")

    token_pat = _make_fake_token("gh" + "p", "PersonalAccessToken1234567890ABCDEF")
    token_server = _make_fake_token("gh" + "s", "ServerToServerToken1234567890ABCDEF")
    token_oauth = _make_fake_token("gh" + "o", "OAuthAccessToken1234567890123ABCDEF")
    token_user = _make_fake_token("gh" + "u", "UserToServerToken123456789012ABCDEF")
    token_refresh = _make_fake_token("gh" + "r", "RefreshToken1234567890123456ABCDEF")
    token_pat_fine = _make_fake_token(
        "github" + "_pat", "11AAAAABBBBBCCCCCDDDDD12345678901234567890"
    )

    logger.info("Direct message with %s", token_pat)
    logger.warning("Token in args: %s and %s", token_server, token_oauth)
    logger.error("Multiple tokens: %s, %s, %s", token_user, token_refresh, token_pat_fine)

    output = fake_stderr.getvalue()
    for token in (token_pat, token_server, token_oauth, token_user, token_refresh, token_pat_fine):
        assert token not in output

    entries = _parse_stderr_lines(output)
    assert len(entries) == 3
    assert entries[0]["service"] == "test-service"
    assert entries[0]["level"] == "INFO"
    assert "***" in entries[0]["message"]
    assert "***" in entries[1]["message"]
    assert "***" in entries[2]["message"]


def test_jwt_tokens_are_scrubbed(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_stderr = io.StringIO()
    monkeypatch.setattr(sys, "stderr", fake_stderr)

    configure_logging("test-service")
    logger = logging.getLogger("test.logger.jwt")

    jwt_token = ".".join(
        [
            "ey" + "JhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9",
            "ey" + "Jpc3MiOiIxMjM0NTYiLCJzdWIiOiJ0ZXN0In0",
            "SignatureBase64UrlSample1234567890",
        ]
    )
    logger.info("Bearer token issued: %s", jwt_token)

    output = fake_stderr.getvalue()
    assert jwt_token not in output

    entries = _parse_stderr_lines(output)
    assert len(entries) == 1
    assert "***" in entries[0]["message"]


def test_authorization_header_is_scrubbed(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_stderr = io.StringIO()
    monkeypatch.setattr(sys, "stderr", fake_stderr)

    configure_logging("test-service")
    logger = logging.getLogger("test.logger.auth")

    secret_val = "raw-secret-credential-value"
    logger.info("Sending request with Authorization: Bearer %s to endpoint", secret_val)
    logger.info("Basic auth header: Authorization: Basic dXNlcjpwYXNzd29yZA==")
    logger.info('JSON headers payload: {"Authorization": "Bearer secret-payload-val"}')

    output = fake_stderr.getvalue()
    assert secret_val not in output
    assert "dXNlcjpwYXNzd29yZA==" not in output
    assert "secret-payload-val" not in output

    entries = _parse_stderr_lines(output)
    assert len(entries) == 3
    assert "Authorization: Bearer ***" in entries[0]["message"]
    assert "Authorization: Basic ***" in entries[1]["message"]
    assert '{"Authorization": "Bearer ***"}' in entries[2]["message"]


def test_unprefixed_llm_key_is_scrubbed_by_configured_process_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_val = "replace-with-random-llm-key-777"
    monkeypatch.setenv("LLM_API_KEYS", f"{secret_val},replace-with-secondary-888")

    fake_stderr = io.StringIO()
    monkeypatch.setattr(sys, "stderr", fake_stderr)

    configure_logging("test-service")
    logger = logging.getLogger("test.logger.llm")

    logger.info("Calling provider with key %s for prompt execution", secret_val)
    logger.warning("Secondary key is replace-with-secondary-888")

    output = fake_stderr.getvalue()
    assert secret_val not in output
    assert "replace-with-secondary-888" not in output

    entries = _parse_stderr_lines(output)
    assert len(entries) == 2
    assert "***" in entries[0]["message"]
    assert "***" in entries[1]["message"]


def test_exception_containing_secret_is_scrubbed_in_message_and_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = _make_fake_token("gh" + "p", "ExceptionToken1234567890abcdef")
    llm_val = "replace-with-llm-in-exception-999"
    monkeypatch.setenv("LLM_API_KEYS", llm_val)

    fake_stderr = io.StringIO()
    monkeypatch.setattr(sys, "stderr", fake_stderr)

    configure_logging("test-service")
    logger = logging.getLogger("test.logger.exc")

    try:
        raise ValueError(f"Connection failed using token {token} and {llm_val}")
    except ValueError as exc:
        logger.error("Failed with error: %s", exc, exc_info=exc)

    output = fake_stderr.getvalue()
    assert token not in output
    assert llm_val not in output

    entries = _parse_stderr_lines(output)
    assert len(entries) == 1
    assert entries[0]["error_type"] == "ValueError"
    assert "***" in entries[0]["message"]
    if "exc_info" in entries[0]:
        assert token not in entries[0]["exc_info"]
        assert llm_val not in entries[0]["exc_info"]


def test_custom_secrets_passed_to_formatter_are_scrubbed() -> None:
    custom_secret = "custom-runtime-secret-key-444"
    formatter = JsonFormatter("service-custom", secrets=[custom_secret])

    record = logging.LogRecord(
        name="test.custom",
        level=logging.ERROR,
        pathname="test.py",
        lineno=1,
        msg="Error with %s",
        args=(custom_secret,),
        exc_info=None,
    )
    formatted = formatter.format(record)
    assert custom_secret not in formatted
    entry = json.loads(formatted)
    assert entry["message"] == "Error with ***"
