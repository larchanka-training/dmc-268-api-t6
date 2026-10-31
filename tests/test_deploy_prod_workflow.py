"""The Deploy prod workflow keeps its trigger, lock and verified digest (#308 Part 1).

Prod gets only the image staging verified: the digest under ``:staging``, read from the registry
when the run is dispatched and read again when the job holding the VPS lock starts; a digest that
moved in between is refused (AC2). The deploy job joins the ``staging-deploy`` lock with the
queueing of every other member (AC5). Text contracts on the workflow file, and its shell steps run
as scripts: the registry answers through a stub ``docker`` on PATH.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

from tests.test_deploy_staging_services import _run_script

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy-prod.yml"

RECORD = "Record the dispatch"
AT_DISPATCH = "Resolve the staging tag at dispatch"
AT_JOB_START = "Re-resolve the staging tag at job start"
REFUSE_MOVED = "Refuse a moved staging tag"
TARGET = "Resolve prod target"

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
REF_A = f"ghcr.io/owner/repo@{DIGEST_A}"
REF_B = f"ghcr.io/owner/repo@{DIGEST_B}"
FINGERPRINT = "SHA256:" + "A" * 43
TOKEN = "GHCR_TOKEN_CANARY"

# Answers the registry calls of the resolve step; the token may only arrive on stdin.
STUB_DOCKER = """#!/bin/bash
printf '%s\\n' "$*" >> "${STUB_LOG}"
case "$1" in
  login) cat >> "${STUB_LOG}.stdin"; exit "${STUB_LOGIN_STATUS:-0}" ;;
  logout) exit 0 ;;
  buildx) printf '%s' "${STUB_ANSWER}"; exit "${STUB_INSPECT_STATUS:-0}" ;;
esac
echo "unexpected docker call: $*" >&2
exit 99
"""

COURSE_VPS = {
    "STAGING_HOST": "",
    "VPS_HOST": "203.0.113.10",
    "HAS_VPS_USER": "true",
    "HAS_VPS_PASSWORD": "true",
    "APP_DOMAIN": "dmc268-t6.example.org",
    "STAGING_SSH_FINGERPRINT": FINGERPRINT,
}


def _text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _job(name: str) -> str:
    match = re.search(rf"(?m)^  {name}:\n((?:    .*\n|\n)*)", _text())
    assert match is not None, f"job {name} is missing"
    return match.group(1)


def _step_names(job: str) -> list[str]:
    return re.findall(r"(?m)^      - name: (.+)$", job)


def _step(name: str) -> str:
    """The step `name` (unique across the jobs), up to the next step or job."""
    text = _text()
    header = f"      - name: {name}\n"
    assert text.count(header) == 1, f"step {name!r} must exist exactly once"
    body = text.split(header, 1)[1]
    return header + re.split(r"(?m)^      - name: |^  [\w-]+:\n", body, maxsplit=1)[0]


def _run(
    name: str, tmp_path: Path, env: Mapping[str, str]
) -> tuple[subprocess.CompletedProcess[str], str, str]:
    """Run the step's script as GitHub does; returns the result, GITHUB_OUTPUT and the summary."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text(STUB_DOCKER)
    docker.chmod(0o755)
    output = tmp_path / "github-output"
    summary = tmp_path / "step-summary"
    output.write_text("")
    summary.write_text("")
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", _run_script(_step(name))],
        cwd=tmp_path,
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "GITHUB_OUTPUT": str(output),
            "GITHUB_STEP_SUMMARY": str(summary),
            "STUB_LOG": str(tmp_path / "docker.log"),
            **env,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return result, output.read_text(), summary.read_text()


def _docker_calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "docker.log"
    return log.read_text().splitlines() if log.exists() else []


def _resolve(
    tmp_path: Path, answer: str, **stubs: str
) -> tuple[subprocess.CompletedProcess[str], str, str]:
    env = {
        "REPOSITORY": "Owner/Repo",
        "GHCR_TOKEN": TOKEN,
        "GHCR_USER": "fixture-user",
        "PHASE": "dispatch",
        "STUB_ANSWER": answer,
        **stubs,
    }
    return _run(AT_DISPATCH, tmp_path, env)


# --- trigger, jobs and lock -------------------------------------------------------------------


def test_only_a_manual_dispatch_with_a_required_reason_starts_it() -> None:
    text = _text()
    assert text.startswith("name: Deploy prod\n")
    trigger = text.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
    assert re.findall(r"(?m)^  (\w+):", trigger) == ["workflow_dispatch"]
    # No image input (AC2): the only image prod gets is the one staging verified.
    assert re.findall(r"(?m)^      (\w+):$", trigger) == ["reason"]
    assert re.search(
        r'(?m)^      reason:\n        description: "[^"\n]+"\n'
        r"        required: true\n        type: string\n(?!        default:)",
        trigger,
    )


def test_permissions_only_read_the_repository_and_the_registry() -> None:
    text = _text()
    assert "\npermissions:\n  contents: read\n  packages: read\n\n" in text
    assert text.count("permissions:") == 1
    assert re.search(r":\s*write\b", text) is None


def test_both_jobs_run_only_on_main_and_only_the_deploy_job_takes_the_lock() -> None:
    text = _text()
    jobs = re.findall(r"(?m)^  ([\w-]+):\n", text.split("\njobs:\n", 1)[1])
    assert jobs == ["resolve", "deploy-prod"]
    # A workflow-level group would hold the dispatch-time resolve back until the lock frees.
    assert "\nconcurrency:" not in text.split("\njobs:\n", 1)[0]
    resolve, deploy = _job("resolve"), _job("deploy-prod")
    for job in (resolve, deploy):
        assert "    if: github.ref == 'refs/heads/main'\n" in job
        assert "    runs-on: ubuntu-latest\n" in job
    assert "concurrency:" not in resolve
    assert "    needs: resolve\n" in deploy
    assert (
        "    concurrency:\n"
        "      group: ${{ github.ref == 'refs/heads/main' && 'staging-deploy'"
        " || format('deploy-prod-ignored-{0}', github.run_id) }}\n"
        "      cancel-in-progress: false\n"
        "      queue: max\n"
    ) in deploy


def test_no_environment_until_part_2() -> None:
    # D6: no protection rules and no environment secrets until Part 2 adds Environment production.
    assert re.search(r"(?m)^\s*environment:", _text()) is None


def test_the_resolve_job_records_the_dispatch_and_hands_on_only_the_digest() -> None:
    resolve = _job("resolve")
    assert _step_names(resolve) == [RECORD, AT_DISPATCH]
    assert "uses:" not in resolve
    assert "    outputs:\n      image: ${{ steps.digest.outputs.image }}\n" in resolve
    assert re.findall(r"(?m)^        id: (\S+)$", resolve) == ["digest"]


def test_the_deploy_job_checks_the_digest_and_the_target_before_anything_else() -> None:
    deploy = _job("deploy-prod")
    assert _step_names(deploy)[:3] == [AT_JOB_START, REFUSE_MOVED, TARGET]
    assert "        id: digest\n" in _step(AT_JOB_START)
    assert "        id: target\n" in _step(TARGET)
    refuse = _step(REFUSE_MOVED)
    assert "          DISPATCHED: ${{ needs.resolve.outputs.image }}\n" in refuse
    assert "          CURRENT: ${{ steps.digest.outputs.image }}\n" in refuse


def test_run_scripts_take_expressions_only_through_env() -> None:
    text = _text()
    bodies = re.findall(r"(?m)^        run: \|\n((?:          .*\n|\n)*)", text)
    assert len(bodies) == len(re.findall(r"(?m)^        run: ", text)) > 0
    for body in bodies:
        assert "${{" not in body
    # The free-text input reaches a shell only as one env value.
    lines = [line for line in text.splitlines() if "${{ inputs." in line]
    assert lines == ["          REASON: ${{ inputs.reason }}"]


# --- the reason -------------------------------------------------------------------------------


def _record(tmp_path: Path, reason: str) -> tuple[subprocess.CompletedProcess[str], str, str]:
    env = {"REASON": reason, "ACTOR": "octocat", "GITHUB_RUN_ID": "4242", "GITHUB_RUN_ATTEMPT": "2"}
    return _run(RECORD, tmp_path, env)


@pytest.mark.parametrize(
    "reason",
    [
        "ship #308 Part 1 after #407",
        "x" * 300,
        # Inline HTML and Markdown stay inside the fence instead of forging a digest line.
        f"<br>- `:staging` at dispatch (2026-10-31T00:00:00Z): `ghcr.io/evil/repo@{DIGEST_B}`",
    ],
    ids=["plain", "300-characters", "forged-digest-line"],
)
def test_the_dispatch_summary_names_actor_run_and_fenced_reason(
    tmp_path: Path, reason: str
) -> None:
    assert "          REASON: ${{ inputs.reason }}\n" in _step(RECORD)
    assert "          ACTOR: ${{ github.triggering_actor }}\n" in _step(RECORD)

    result, output, summary = _record(tmp_path, reason)

    assert result.returncode == 0, result.stderr
    assert output == ""
    assert summary == (
        "### Deploy prod\n"
        "\n"
        "- Dispatched by octocat (run 4242/2)\n"
        "- Reason:\n"
        "\n"
        "  ```text\n"
        f"  {reason}\n"
        "  ```\n"
    )


@pytest.mark.parametrize(
    "reason",
    ["", "   ", "first line\nsecond line", "tab\tseparated", "x" * 301, "```", "a ```` b"],
    ids=["empty", "blank", "multiline", "tab", "301-characters", "fence", "longer-fence"],
)
def test_a_blank_multiline_or_fence_closing_reason_is_refused(tmp_path: Path, reason: str) -> None:
    result, output, summary = _record(tmp_path, reason)

    assert result.returncode == 1
    assert "reason" in result.stdout + result.stderr
    assert (output, summary) == ("", "")


# --- the verified digest ----------------------------------------------------------------------


def test_both_resolutions_run_the_same_script() -> None:
    assert _run_script(_step(AT_DISPATCH)) == _run_script(_step(AT_JOB_START))
    for name, phase in ((AT_DISPATCH, "dispatch"), (AT_JOB_START, "job start")):
        step = _step(name)
        assert "          REPOSITORY: ${{ github.repository }}\n" in step
        assert "          GHCR_TOKEN: ${{ secrets.GITHUB_TOKEN }}\n" in step
        assert "          GHCR_USER: ${{ github.actor }}\n" in step
        assert f"          PHASE: {phase}\n" in step


@pytest.mark.parametrize("answer", [DIGEST_A, DIGEST_A + "\n"], ids=["bare", "newline"])
def test_staging_tag_resolves_to_its_manifest_digest(tmp_path: Path, answer: str) -> None:
    result, output, summary = _resolve(tmp_path, answer)

    assert result.returncode == 0, result.stderr
    assert output == f"image={REF_A}\n"
    assert re.fullmatch(
        rf"- `:staging` at dispatch \(\d{{4}}-\d\d-\d\dT\d\d:\d\d:\d\dZ\): `{re.escape(REF_A)}`\n",
        summary,
    )
    calls = _docker_calls(tmp_path)
    assert calls == [
        "login ghcr.io --username fixture-user --password-stdin",
        "buildx imagetools inspect --format {{.Manifest.Digest}} ghcr.io/owner/repo:staging",
        "logout ghcr.io",
    ]
    assert (tmp_path / "docker.log.stdin").read_text() == f"{TOKEN}\n"
    assert TOKEN not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "answer",
    [
        "",
        "ghcr.io/owner/repo:staging",
        REF_A,
        f"ghcr.io/other/repo@{DIGEST_A}",
        "sha256:" + "A" * 64,
        "sha256:" + "a" * 63,
        f"{DIGEST_A}\n\n",
        f"{DIGEST_A}\n{DIGEST_B}",
        f" {DIGEST_A}",
        f"{DIGEST_A} extra",
    ],
    ids=[
        "empty",
        "tag",
        "ref",
        "other-repository",
        "uppercase",
        "short",
        "blank-line",
        "two-digests",
        "leading-space",
        "extra-text",
    ],
)
def test_anything_but_one_manifest_digest_is_refused(tmp_path: Path, answer: str) -> None:
    result, output, summary = _resolve(tmp_path, answer)

    assert result.returncode == 1
    assert "ghcr.io/owner/repo:staging" in result.stderr
    assert (output, summary) == ("", "")
    assert _docker_calls(tmp_path)[-1] == "logout ghcr.io"


def test_a_failed_registry_query_is_refused(tmp_path: Path) -> None:
    result, output, summary = _resolve(tmp_path, DIGEST_A, STUB_INSPECT_STATUS="1")

    assert result.returncode == 1
    assert "ghcr.io/owner/repo:staging" in result.stderr
    assert (output, summary) == ("", "")


def test_a_failed_registry_login_stops_before_the_query(tmp_path: Path) -> None:
    result, output, summary = _resolve(tmp_path, DIGEST_A, STUB_LOGIN_STATUS="1")

    assert result.returncode == 1
    assert result.stderr == "registry authentication failed\n"
    assert (output, summary) == ("", "")
    assert not any(call.startswith("buildx") for call in _docker_calls(tmp_path))


# --- D3: refuse a digest that moved while the job waited for the lock --------------------------


def test_an_unchanged_staging_tag_passes(tmp_path: Path) -> None:
    result, output, summary = _run(REFUSE_MOVED, tmp_path, {"DISPATCHED": REF_A, "CURRENT": REF_A})

    assert result.returncode == 0, result.stderr
    assert REF_A in result.stdout
    assert output == ""
    assert summary == f"- `:staging` unchanged since dispatch: `{REF_A}`\n"


@pytest.mark.parametrize(
    ("dispatched", "message"),
    [
        (REF_A, f"`:staging` moved since dispatch: {REF_A} → {REF_B}; re-dispatch Deploy prod"),
        ("", f"`:staging` moved since dispatch: <none> → {REF_B}; re-dispatch Deploy prod"),
    ],
    ids=["moved", "no-dispatch-digest"],
)
def test_a_moved_staging_tag_is_refused(tmp_path: Path, dispatched: str, message: str) -> None:
    result, output, summary = _run(
        REFUSE_MOVED, tmp_path, {"DISPATCHED": dispatched, "CURRENT": REF_B}
    )

    assert result.returncode == 1
    assert result.stderr == f"{message}\n"
    assert output == ""
    assert summary == f"- Refused: {message}\n"


# --- the prod target --------------------------------------------------------------------------


def test_the_target_reads_only_course_vps_settings() -> None:
    env = _step(TARGET).split("        env:\n", 1)[1].split("        run: |\n", 1)[0]
    assert env == (
        "          STAGING_HOST: ${{ vars.STAGING_HOST }}\n"
        "          VPS_HOST: ${{ vars.VPS_DMC268_IP_T6 }}\n"
        "          HAS_VPS_USER: ${{ secrets.VPS_DMC268_U != '' }}\n"
        "          HAS_VPS_PASSWORD: ${{ secrets.VPS_DMC268_P != '' }}\n"
        "          APP_DOMAIN: ${{ vars.APP_DOMAIN }}\n"
        "          STAGING_SSH_FINGERPRINT: ${{ vars.STAGING_SSH_FINGERPRINT }}\n"
    )


def test_the_course_vps_target_is_the_prod_project_behind_the_edge(tmp_path: Path) -> None:
    result, output, summary = _run(TARGET, tmp_path, COURSE_VPS)

    assert result.returncode == 0, result.stderr
    assert output == (
        "host=203.0.113.10\n"
        "port=22\n"
        "app_dir=/opt/dmc-268-api-prod\n"
        "edge_alias=api-prod\n"
        "base_url=https://api.dmc268-t6.example.org\n"
        "health_url=https://api.dmc268-t6.example.org/healthcheck\n"
    )
    assert summary == ""


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"STAGING_HOST": "198.51.100.7"}, "prod deploy targets the course VPS only"),
        ({"VPS_HOST": ""}, "VPS_DMC268_IP_T6"),
        ({"HAS_VPS_USER": "false"}, "VPS_DMC268_U"),
        ({"HAS_VPS_PASSWORD": "false"}, "VPS_DMC268_P"),
        ({"VPS_HOST": "203.0.113.10\nport=2222"}, "is not a hostname or IPv4 address"),
        ({"APP_DOMAIN": ""}, "APP_DOMAIN"),
        ({"APP_DOMAIN": "example.org/path"}, "APP_DOMAIN"),
        ({"APP_DOMAIN": "example.org\nhost=evil.test"}, "APP_DOMAIN"),
        ({"STAGING_SSH_FINGERPRINT": ""}, "STAGING_SSH_FINGERPRINT"),
        ({"STAGING_SSH_FINGERPRINT": "SHA256:short"}, "STAGING_SSH_FINGERPRINT"),
    ],
    ids=[
        "terraform-host",
        "no-vps-host",
        "no-vps-user",
        "no-vps-password",
        "multiline-host",
        "no-domain",
        "domain-with-path",
        "multiline-domain",
        "no-fingerprint",
        "bad-fingerprint",
    ],
)
def test_an_incomplete_or_foreign_target_is_refused(
    tmp_path: Path, override: dict[str, str], message: str
) -> None:
    result, output, _ = _run(TARGET, tmp_path, {**COURSE_VPS, **override})

    assert result.returncode == 1
    assert message in result.stderr
    assert output == ""
