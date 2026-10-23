"""The manual staging ACK load workflow keeps its guards, caps and single secret (#163)."""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github/workflows/webhook-ack-load.yml"
SCRIPT = REPO_ROOT / "scripts/webhook_ack_load.py"


def _block(text: str, name: str) -> str:
    return text.split(f"      - name: {name}\n", 1)[1].split("\n      - name:", 1)[0]


def _step(name: str) -> str:
    match = re.search(
        r"(?m)^        run: \|\n(?P<body>(?:^          .*\n|^\n)*)",
        _block(WORKFLOW.read_text(), name),
    )
    assert match is not None
    return textwrap.dedent(match.group("body"))


def _run(name: str, root: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", "-euo", "pipefail", "-c", _step(name)],
        cwd=root,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        check=False,
    )


def _assert_workflow_contract(text: str) -> None:
    trigger = text.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
    assert re.findall(r"(?m)^  (\w+):", trigger) == ["workflow_dispatch"]
    for name, default in (("count", "200"), ("concurrency", "5"), ("body_bytes", "16384")):
        assert re.search(
            rf'(?m)^      {name}:\n        description: "[^"\n]+"\n'
            rf'        required: false\n        type: string\n        default: "{default}"\n',
            trigger,
        ), name
    assert "permissions:\n  contents: read\n\n" in text
    assert "concurrency:\n  group: webhook-ack-load\n  cancel-in-progress: false\n" in text
    job = text.split("\njobs:\n", 1)[1].split("    steps:\n", 1)[0]
    assert "    if: github.ref == 'refs/heads/main'\n" in job
    assert "    runs-on: ubuntu-latest\n" in job
    assert "    timeout-minutes: 15\n" in job
    assert "    environment: staging\n" in job
    steps = re.findall(r"(?m)^      - name: (.+)$", text)
    assert steps[0] == "Validate inputs"
    # Inputs reach the shell only through env, never through a `run:` body.
    for line in (line for line in text.splitlines() if "${{ inputs." in line):
        assert re.fullmatch(r"          [A-Z_]+: \$\{\{ inputs\.[a-z_]+ \}\}", line), line
    assert text.count("${{ secrets.") == 1
    load = _block(text, "Run webhook ACK load")
    assert "          GITHUB_WEBHOOK_SECRET: ${{ secrets.GH_WEBHOOK_SECRET }}\n" in load
    assert "scripts/webhook_ack_load.py" in load
    assert '--report-json "${RUNNER_TEMP}/webhook-ack-load.json"' in load
    validate = _block(text, "Validate inputs")
    assert 'check count "${COUNT}" 1000\n' in validate
    assert 'check concurrency "${CONCURRENCY}" 10\n' in validate
    assert 'check body_bytes "${BODY_BYTES}" 65536\n' in validate
    assert (
        "        uses: astral-sh/setup-uv@c18668ad3cf93ea998bef934396af7bb5c839dc7 # v10.2.0\n"
        "        with:\n"
        '          version: "0.12.11"\n'
        '          python-version: "3.13"\n'
    ) in text
    assert "        run: uv sync --locked\n" in text
    for name in ("Summarize load", "Upload load report"):
        assert f"      - name: {name}\n        if: always()\n" in text
    assert "GitHub-hosted runner" in _block(text, "Summarize load")
    upload = _block(text, "Upload load report")
    assert "          path: ${{ runner.temp }}/webhook-ack-load.json\n" in upload
    assert "          retention-days: 90\n" in upload


def test_workflow_contract() -> None:
    _assert_workflow_contract(WORKFLOW.read_text())


@pytest.mark.parametrize(
    "before,after",
    [
        ("on:\n  workflow_dispatch:\n", "on:\n  push:\n  workflow_dispatch:\n"),
        ("    if: github.ref == 'refs/heads/main'\n", "    if: true\n"),
        ("    environment: staging\n", "    environment: production\n"),
        ("contents: read", "contents: write"),
        ("cancel-in-progress: false", "cancel-in-progress: true"),
        ("timeout-minutes: 15", "timeout-minutes: 120"),
        ('check count "${COUNT}" 1000', 'check count "${COUNT}" 100000'),
        ('--count "${COUNT}"', '--count "${{ inputs.count }}"'),
        (
            "          URL: ${{ steps.target.outputs.url }}\n",
            "          URL: ${{ steps.target.outputs.url }}\n"
            "          LLM_API_KEYS: ${{ secrets.AI_DMC268_T6 }}\n",
        ),
        ("retention-days: 90", "retention-days: 1"),
        (
            "      - name: Upload load report\n        if: always()\n",
            "      - name: Upload load report\n",
        ),
    ],
    ids=[
        "push-trigger",
        "no-main-guard",
        "other-environment",
        "write-permission",
        "cancel-in-progress",
        "long-timeout",
        "count-cap-raised",
        "input-in-run-body",
        "second-secret",
        "short-retention",
        "upload-only-on-success",
    ],
)
def test_workflow_contract_rejects_unsafe_mutations(before: str, after: str) -> None:
    original = WORKFLOW.read_text()
    assert before in original
    with pytest.raises(AssertionError):
        _assert_workflow_contract(original.replace(before, after))


def test_workflow_caps_match_the_script() -> None:
    spec = importlib.util.spec_from_file_location("webhook_ack_load_caps", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # The dataclasses of the script resolve their annotations through sys.modules.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    validate = _step("Validate inputs")

    assert (module.MAX_COUNT, module.MAX_CONCURRENCY, module.MAX_BODY_BYTES) == (1000, 10, 65536)
    assert f'check count "${{COUNT}}" {module.MAX_COUNT}' in validate
    assert f'check concurrency "${{CONCURRENCY}}" {module.MAX_CONCURRENCY}' in validate
    assert f'check body_bytes "${{BODY_BYTES}}" {module.MAX_BODY_BYTES}' in validate


@pytest.mark.parametrize(
    "inputs",
    [("200", "5", "16384"), ("1000", "10", "65536"), ("1", "1", "1")],
    ids=["defaults", "caps", "minimum"],
)
def test_validate_inputs_accepts_values_within_the_caps(
    tmp_path: Path, inputs: tuple[str, str, str]
) -> None:
    count, concurrency, body_bytes = inputs
    env = {"COUNT": count, "CONCURRENCY": concurrency, "BODY_BYTES": body_bytes}

    result = _run("Validate inputs", tmp_path, env)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "inputs,message",
    [
        (("1001", "5", "16384"), "count must be an integer from 1 to 1000"),
        (("200", "11", "16384"), "concurrency must be an integer from 1 to 10"),
        (("200", "5", "65537"), "body_bytes must be an integer from 1 to 65536"),
        (("0", "5", "16384"), "count must be an integer from 1 to 1000"),
        (("010", "5", "16384"), "count must be an integer from 1 to 1000"),
        (("200", "5; touch pwned", "16384"), "concurrency must be an integer from 1 to 10"),
        (("200", "5", ""), "body_bytes must be an integer from 1 to 65536"),
    ],
    ids=["count", "concurrency", "body", "zero", "leading-zero", "shell-text", "empty"],
)
def test_validate_inputs_rejects_values_outside_the_caps(
    tmp_path: Path, inputs: tuple[str, str, str], message: str
) -> None:
    count, concurrency, body_bytes = inputs
    env = {"COUNT": count, "CONCURRENCY": concurrency, "BODY_BYTES": body_bytes}

    result = _run("Validate inputs", tmp_path, env)

    assert result.returncode == 1
    assert f"::error::{message}" in result.stderr
    assert not (tmp_path / "pwned").exists()


@pytest.mark.parametrize(
    "host,domain,url",
    [
        ("203.0.113.7", "example.org", "http://203.0.113.7/webhooks/github"),
        ("", "example.org", "https://staging-api.example.org/webhooks/github"),
    ],
    ids=["terraform-host", "course-vps"],
)
def test_target_url_is_resolved_like_the_deploy_job(
    tmp_path: Path, host: str, domain: str, url: str
) -> None:
    output = tmp_path / "output"
    env = {"STAGING_HOST": host, "APP_DOMAIN": domain, "GITHUB_OUTPUT": str(output)}

    result = _run("Resolve staging webhook URL", tmp_path, env)

    assert result.returncode == 0, result.stderr
    assert output.read_text() == f"url={url}\n"


@pytest.mark.parametrize(
    "host,domain",
    [("", ""), ("", "localhost"), ("evil.example/x?", "example.org")],
    ids=["no-target", "bare-domain", "bad-host"],
)
def test_target_url_fails_without_a_valid_staging_target(
    tmp_path: Path, host: str, domain: str
) -> None:
    output = tmp_path / "output"
    env = {"STAGING_HOST": host, "APP_DOMAIN": domain, "GITHUB_OUTPUT": str(output)}

    result = _run("Resolve staging webhook URL", tmp_path, env)

    assert result.returncode == 1
    assert "::error::" in result.stderr
    assert not output.exists()


def _summarize(tmp_path: Path, report: str | None) -> str:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "uv"
    shim.write_text('#!/bin/bash\nexec "$PYTHON_EXE" -\n')
    shim.chmod(0o755)
    if report is not None:
        (tmp_path / "webhook-ack-load.json").write_text(report)
    summary = tmp_path / "summary.md"
    env = {
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "PYTHON_EXE": sys.executable,
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_STEP_SUMMARY": str(summary),
    }
    result = _run("Summarize load", tmp_path, env)
    assert result.returncode == 0, result.stderr
    return summary.read_text()


def test_summary_shows_the_report_and_the_runner_caveat(tmp_path: Path) -> None:
    report = (
        '{"started_at": "2026-10-23T10:00:00Z", "finished_at": "2026-10-23T10:00:09Z", '
        '"target_host": "staging-api.example.org", "count": 200, "warmup": 5, '
        '"concurrency": 5, "body_bytes": 16384, "statuses": {"202": 205}, '
        '"latency_ms": {"p50": 41.0, "p95": 88.5, "p99": 120.0, "max": 140.2}, '
        '"verdict": "p95 88.5 ms meets <target>"}'
    )

    content = _summarize(tmp_path, report)

    assert "GitHub-hosted runner" in content
    assert "| Target | staging-api.example.org |\n" in content
    assert "| Window (UTC) | 2026-10-23T10:00:00Z - 2026-10-23T10:00:09Z |\n" in content
    assert "| Timed / warm-up | 200 / 5 |\n" in content
    assert "| Statuses | 202: 205 |\n" in content
    assert "| p95 ms | 88.5 |\n" in content
    assert "| Verdict | p95 88.5 ms meets &lt;target&gt; |\n" in content


def test_summary_without_a_report_says_so(tmp_path: Path) -> None:
    content = _summarize(tmp_path, None)

    assert "No load report was produced." in content
