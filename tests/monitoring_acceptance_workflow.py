"""Helpers shared by the tests of the acceptance workflow.

The workflow file (.github/workflows/monitoring-acceptance.yml), the steps run from it as shell
scripts, and the SSH action output they read.

Used by tests/test_monitoring_acceptance.py and tests/test_monitoring_acceptance_status_page.py.
"""

from __future__ import annotations

import os
import re
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "monitoring-acceptance.yml"
# What drone-ssh v1.8.2 (plugin.go) prints after a session; appleboy/ssh-action v1.2.5 captures it
# into the stdout output together with the output of the script.
DRONE_SSH_BANNER = (
    "===============================================\n"
    "\u2705 Successfully executed commands to all hosts.\n"
    "===============================================\n"
)

TERRAFORM_TARGET = {
    "STAGING_HOST": "203.0.113.7",
    "STAGING_SSH_PORT": "22022",
    "STAGING_SSH_USER": "root",
    "HAS_STAGING_SSH_KEY": "true",
}
COURSE_TARGET = {
    "VPS_HOST": "203.0.113.8",
    "HAS_VPS_USER": "true",
    "HAS_VPS_PASSWORD": "true",
    "APP_DOMAIN": "example.test",
}


def _target_step(tmp_path: Path, **env: str) -> tuple[int, str, str]:
    output = tmp_path / "output"
    output.touch()
    result = subprocess.run(
        ["/bin/bash", "-e", "-c", _step("Resolve staging target")],
        env={
            "PATH": os.environ["PATH"],
            "GITHUB_OUTPUT": str(output),
            "STAGING_SSH_FINGERPRINT": "SHA256:" + "A" * 43,
            **env,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode, result.stderr, output.read_text(encoding="utf-8")


def _step(name: str) -> str:
    text = WORKFLOW.read_text(encoding="utf-8")
    block = text.split(f"      - name: {name}\n", 1)[1].split("\n      - name:", 1)[0]
    match = re.search(r"(?m)^        run: \|\n(?P<body>(?:^          .*\n|^\n)*)", block)
    assert match is not None
    return textwrap.dedent(match.group("body"))


def _report_step(tmp_path: Path, report: str, **env: str) -> tuple[int, str]:
    summary = tmp_path / "summary.md"
    result = subprocess.run(
        ["/bin/bash", "-e", "-c", _step("Report")],
        env={
            **os.environ,
            "GITHUB_STEP_SUMMARY": str(summary),
            "REPORT": report,
            "UPLOAD": "success",
            "CHECKS": "success",
            "GRAFANA": "ok",
            **env,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode, summary.read_text(encoding="utf-8")
