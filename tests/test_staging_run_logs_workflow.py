"""The read-only lookup of one run's JSON log lines on staging (#395 AC4).

Nobody on the team has SSH to the course VPS, so a manual workflow prints, for one ``run_id``, the
JSON log lines of the staging ``api``, ``worker`` and ``webhook-worker`` containers that carry it.
It reads only: no Environment, no VPS lock, no deploy. Text contracts on the workflow file, and its
shell steps run as scripts with a stub ``docker`` on PATH.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from app.common.infrastructure.telemetry import JsonFormatter
from tests.deploy_host_fakes import _run_script, _workflow_step
from tests.deploy_prod_workflow_fakes import (
    CI_WORKFLOW,
    REPO_ROOT,
    VPS_LOGIN,
    _ssh_script,
)
from tests.deploy_prod_workflow_fakes import (
    WORKFLOW as DEPLOY_PROD,
)
from tests.monitoring_acceptance_workflow import DRONE_SSH_BANNER

WORKFLOW = DEPLOY_PROD.parent / "staging-run-logs.yml"

VALIDATE = "Validate the run id"
TARGET = "Resolve the course VPS target"
LOOKUP = "Read the run log lines"
SHOW = "Show the run log lines"

RUN_ID = "72d3162e-cc78-11e3-81ab-4c9367dc0958"
OTHER_RUN_ID = "00000000-0000-0000-0000-000000000000"
SERVICES = ("api", "worker", "webhook-worker")
CONTAINER = {"api": "a" * 64, "worker": "b" * 64, "webhook-worker": "c" * 64}
NOT_A_RUN_ID = [
    "",
    "latest",
    "72d3162e-cc78-11e3-81ab-4c9367dc095",  # one digit short
    "72d3162e-cc78-11e3-81ab-4c9367dc0958x",
    "72d3162e-cc78-11e3-81ab-4c9367dc0958\n",
    f'{RUN_ID}"\n{{"ts": "x", "run_id": "{OTHER_RUN_ID}"}}',
    "72d3162e-cc78-11e3-81ab-4c9367dc0958 extra",
    "$(id)",
    "72d3162e-cc78-11e3-81ab-4c9367dc095g",
    "72d3162ecc7811e381ab4c9367dc0958",  # no dashes
]

# Answers the docker calls of the lookup from files in STUB_DIR: ps-<service> holds the container
# ids, log-<id> the log (written to stderr, as a container's logging is) and exit-<id> the exit code
# of `docker logs` after printing, as a daemon error or a removed container.
STUB_DOCKER = """#!/bin/bash
printf '%s\\n' "$*" >> "${STUB_LOG}"
case "$1" in
  ps)
    for arg in "$@"; do
      case "${arg}" in label=com.docker.compose.service=*) service="${arg#*service=}" ;; esac
    done
    if [[ -f "${STUB_DIR}/ps-${service}" ]]; then cat "${STUB_DIR}/ps-${service}"; fi
    exit 0 ;;
  logs)
    id="${*: -1}"
    if [[ -f "${STUB_DIR}/log-${id}" ]]; then cat "${STUB_DIR}/log-${id}" >&2; fi
    if [[ -f "${STUB_DIR}/exit-${id}" ]]; then exit "$(cat "${STUB_DIR}/exit-${id}")"; fi
    exit 0 ;;
esac
echo "unexpected docker call: $*" >&2
exit 99
"""


def _text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _step(name: str) -> str:
    return _workflow_step(name, WORKFLOW)


def _script(name: str) -> str:
    return _run_script(_step(name))


def _json_line(service: str, message: str, run_id: str | None = None, **extra: str) -> str:
    """One line exactly as the app's formatter writes it."""
    record = logging.LogRecord("app.test", logging.INFO, __file__, 1, message, None, None)
    if run_id is not None:
        record.__dict__["run_id"] = run_id
    record.__dict__.update(extra)
    return JsonFormatter(service).format(record)


def _bash(
    script: str, tmp_path: Path, env: Mapping[str, str]
) -> tuple[subprocess.CompletedProcess[str], str]:
    """Run `script` as GitHub does; returns the result and the step summary."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text(STUB_DOCKER)
    docker.chmod(0o755)
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir(exist_ok=True)
    summary = tmp_path / "step-summary"
    summary.write_text("")
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
        cwd=tmp_path,
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "GITHUB_STEP_SUMMARY": str(summary),
            "STUB_LOG": str(tmp_path / "docker.log"),
            "STUB_DIR": str(stub_dir),
            **env,
        },
        capture_output=True,
        text=True,
        # A cut multi-byte character reaches the runner as U+FFFD, as the SSH action's JSON does.
        errors="replace",
        check=False,
    )
    return result, summary.read_text(encoding="utf-8")


def _docker_calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "docker.log"
    return log.read_text().splitlines() if log.exists() else []


def _containers(
    tmp_path: Path,
    logs: Mapping[str, Sequence[Sequence[str]]],
    exits: Mapping[str, int] | None = None,
) -> None:
    """Put containers on the stub host: `logs[service]` lists the lines of each replica's log.

    Container ids are CONTAINER[service], then the same letter repeated (`d`, `e`, ...) for more
    replicas of one service.
    """
    stub = tmp_path / "stub"
    stub.mkdir(exist_ok=True)
    spare = iter("defghij")
    for service, replicas in logs.items():
        ids = []
        for index, lines in enumerate(replicas):
            container = CONTAINER[service] if index == 0 else next(spare) * 64
            ids.append(container)
            (stub / f"log-{container}").write_text("".join(f"{line}\n" for line in lines))
            if exits and container in exits:
                (stub / f"exit-{container}").write_text(str(exits[container]))
        (stub / f"ps-{service}").write_text("".join(f"{container}\n" for container in ids))


def _lookup(tmp_path: Path, **env: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    result, _ = _bash(_ssh_script(_step(LOOKUP)), tmp_path, {"RUN_ID": RUN_ID, **env})
    return result, _docker_calls(tmp_path)


def _records(stdout: str, kind: str) -> list[list[str]]:
    return [line.split("\t") for line in stdout.splitlines() if line.startswith(f"{kind}\t")]


def test_only_a_manual_dispatch_with_a_required_run_id_starts_it() -> None:
    text = _text()
    assert text.startswith("name: Staging run logs\n")
    trigger = text.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
    assert re.findall(r"(?m)^  (\w+):", trigger) == ["workflow_dispatch"]
    assert re.findall(r"(?m)^      (\w+):$", trigger) == ["run_id"]
    assert re.search(
        r'(?m)^      run_id:\n        description: "[^"\n]+"\n'
        r"        required: true\n        type: string\n(?!        default:)",
        trigger,
    )


def test_it_reads_the_staging_project_and_never_prod() -> None:
    text = _text()
    deploy_sh = (REPO_ROOT / "deploy" / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    ci = CI_WORKFLOW.read_text(encoding="utf-8")
    staging = re.split(r"(?m)^  [\w-]+:\n", ci.split("\n  deploy-staging:\n", 1)[1], maxsplit=1)[0]

    # deploy.sh names the compose project after the app dir and the staging deploy sets no override.
    assert 'COMPOSE_PROJECT="${COMPOSE_PROJECT:-$(basename "${APP_DIR}")}"' in deploy_sh
    assert "            app_dir=/opt/dmc-268-api-staging\n" in staging
    assert "COMPOSE_PROJECT" not in staging
    assert 'project="dmc-268-api-staging"\n' in _ssh_script(_step(LOOKUP))
    # The target step is deploy-prod's, copied (host and port only); nothing else names prod.
    mine = text.replace(_step(TARGET), "")
    assert "dmc-268-api-prod" not in mine
    assert "stack" not in mine.lower()


def test_it_only_reads_and_stays_out_of_the_environment_and_the_lock() -> None:
    text = _text()
    assert "\npermissions:\n  contents: read\n\n" in text
    assert text.count("permissions:") == 1
    assert re.search(r":\s*write\b", text) is None
    assert re.search(r"(?m)^\s*(environment|concurrency):", text) is None
    assert "staging-deploy" not in text
    jobs = re.findall(r"(?m)^  ([\w-]+):\n", text.split("\njobs:\n", 1)[1])
    assert len(jobs) == 1
    assert "    if: github.ref == 'refs/heads/main'\n" in text
    assert "    runs-on: ubuntu-latest\n" in text


def test_the_steps_run_in_order() -> None:
    names = re.findall(r"(?m)^      - name: (.+)$", _text())

    assert names == [VALIDATE, TARGET, LOOKUP, SHOW]


def test_the_target_is_the_one_deploy_prod_resolves() -> None:
    # Prod and staging run on the same course VPS and the lookup uses only host and port.
    def code(script: str) -> str:
        # Comments differ by design: the lookup job has no Environment.
        return "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))

    ours, theirs = _step(TARGET), _workflow_step("Resolve prod target", DEPLOY_PROD)

    assert code(_run_script(ours)) == code(_run_script(theirs))
    assert (
        ours.split("        run: |\n", 1)[0].split("\n", 1)[1]
        == (theirs.split("        run: |\n", 1)[0].split("\n", 1)[1])
    )
    assert "        id: target\n" in ours


def test_expressions_reach_the_shell_only_through_env() -> None:
    text = _text()
    bodies = re.findall(r"(?m)^        (?:run|script): \|\n((?:          .*\n|\n)*)", text)
    assert len(bodies) >= 3
    for body in bodies:
        assert "${{" not in body
    assert "${{" not in _ssh_script(_step(LOOKUP))
    assert [line.strip() for line in text.splitlines() if "inputs." in line] == [
        "RUN_ID: ${{ inputs.run_id }}"
    ] * 3


@pytest.mark.parametrize("run_id", NOT_A_RUN_ID)
def test_a_run_id_that_is_not_one_uuid_is_refused(tmp_path: Path, run_id: str) -> None:
    result, _ = _bash(_script(VALIDATE), tmp_path, {"RUN_ID": run_id})

    assert result.returncode == 1
    assert result.stderr.startswith("::error::run_id must be one UUID")
    assert _docker_calls(tmp_path) == []


@pytest.mark.parametrize("run_id", [RUN_ID, RUN_ID.upper()])
def test_a_uuid_in_either_case_is_accepted(tmp_path: Path, run_id: str) -> None:
    result, _ = _bash(_script(VALIDATE), tmp_path, {"RUN_ID": run_id})

    assert result.returncode == 0, result.stderr


def test_the_ssh_step_logs_in_like_deploy_prod_and_forwards_only_the_run_id() -> None:
    step = _step(LOOKUP)
    prod = _workflow_step("Prepare host", DEPLOY_PROD)
    uses = re.search(r"(?m)^        (uses: appleboy/ssh-action@.+)$", step)
    pin = re.search(r"(?m)^        (uses: appleboy/ssh-action@.+)$", prod)

    assert uses is not None and pin is not None and uses.group(1) == pin.group(1)
    assert VPS_LOGIN in step
    assert "          debug: false\n" in step
    assert "          capture_stdout: true\n" in step
    assert "          envs: RUN_ID\n" in step
    assert "        env:\n          RUN_ID: ${{ inputs.run_id }}\n" in step
    assert "        id: lookup\n" in step
    assert _ssh_script(step).startswith("set -euo pipefail\nset +o xtrace\n")


def test_the_host_script_changes_nothing_on_the_host() -> None:
    script = _ssh_script(_step(LOOKUP))
    code = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))

    assert set(re.findall(r"\bdocker (\w+)", code)) == {"ps", "logs"}
    # No redirect into a file, no temp file, no lock: stderr and the pipe are the only plumbing.
    assert re.search(r">>?\s*[\"$/~.]|\btee\b|\bmktemp\b|\bflock\b|\brm\b|\bmkdir\b", code) is None


def test_the_lookup_prints_only_the_json_lines_that_carry_the_run_id(tmp_path: Path) -> None:
    mine = _json_line("worker", "step done", RUN_ID, error_type="X")
    # `api` writes uvicorn access lines through the same handler: JSON, without a run_id.
    access = _json_line("api", 'GET /api/runs?token=SECRET-A "HTTP/1.1" 200')
    other = _json_line("api", "queued", OTHER_RUN_ID)
    # The text of a message cannot pose as the field: json.dumps escapes its quotes.
    quoted = _json_line("worker", f'"run_id": "{RUN_ID}" in a message')
    # Not JSON: a raw line (a traceback, a query string) is counted and never printed.
    raw_access = f'10.0.0.1 - "GET /x?a=SECRET-B&q="run_id": "{RUN_ID}"" 200'
    traceback = "Traceback (most recent call last): SECRET-C"
    _containers(
        tmp_path,
        {
            "api": [[access, other, raw_access]],
            "worker": [[mine, quoted, traceback, access]],
            "webhook-worker": [[]],
        },
    )

    result, calls = _lookup(tmp_path)

    assert result.returncode == 0, result.stderr
    assert _records(result.stdout, "LINE") == [["LINE", "worker", mine]]
    assert _records(result.stdout, "STAT") == [
        ["STAT", "api", "2", "1", "0"],
        ["STAT", "worker", "3", "1", "1"],
        ["STAT", "webhook-worker", "0", "0", "0"],
    ]
    assert _records(result.stdout, "CONTAINERS") == [
        ["CONTAINERS", "api", "1"],
        ["CONTAINERS", "worker", "1"],
        ["CONTAINERS", "webhook-worker", "1"],
    ]
    assert "SECRET" not in result.stdout
    assert result.stderr == ""
    assert calls == [
        item
        for service in SERVICES
        for item in (
            "ps -aq --no-trunc --filter label=com.docker.compose.project=dmc-268-api-staging"
            f" --filter label=com.docker.compose.service={service}",
            f"logs --since 168h {CONTAINER[service]}",
        )
    ]


def test_the_run_id_is_matched_as_the_field_the_formatter_writes(tmp_path: Path) -> None:
    # The real formatter's serialization, whatever the other fields hold: run_id after `message`.
    line = _json_line("api", "queue publish failed", RUN_ID, error_type="AmqpError")
    assert f'"run_id": "{RUN_ID}"' in line
    _containers(tmp_path, {"api": [[line]], "worker": [[]], "webhook-worker": [[]]})

    result, _ = _lookup(tmp_path)

    assert _records(result.stdout, "LINE") == [["LINE", "api", line]]
    assert _records(result.stdout, "STAT")[0] == ["STAT", "api", "1", "0", "1"]


def test_an_uppercase_run_id_finds_the_lowercase_field(tmp_path: Path) -> None:
    line = _json_line("worker", "done", RUN_ID)
    _containers(tmp_path, {"api": [[]], "worker": [[line]], "webhook-worker": [[]]})

    result, _ = _lookup(tmp_path, RUN_ID=RUN_ID.upper())

    assert result.returncode == 0, result.stderr
    assert _records(result.stdout, "LINE") == [["LINE", "worker", line]]


def test_a_service_without_a_match_says_zero_lines_and_how_many_it_read(tmp_path: Path) -> None:
    _containers(
        tmp_path,
        {
            "api": [[_json_line("api", "ok"), "plain text"]],
            "worker": [[_json_line("worker", "ok")]],
            "webhook-worker": [[_json_line("webhook-worker", "ok")]],
        },
    )

    result, _ = _lookup(tmp_path)

    assert result.returncode == 0, result.stderr
    assert _records(result.stdout, "LINE") == []
    assert [stat[4] for stat in _records(result.stdout, "STAT")] == ["0", "0", "0"]
    assert [stat[2:4] for stat in _records(result.stdout, "STAT")] == [
        ["1", "1"],
        ["1", "0"],
        ["1", "0"],
    ]


def test_the_lookup_reads_every_replica_of_a_service(tmp_path: Path) -> None:
    first, second = _json_line("worker", "one", RUN_ID), _json_line("worker", "two", RUN_ID)
    _containers(
        tmp_path, {"api": [[]], "worker": [[first], [second, "raw"]], "webhook-worker": [[]]}
    )

    result, calls = _lookup(tmp_path)

    assert result.returncode == 0, result.stderr
    assert _records(result.stdout, "LINE") == [
        ["LINE", "worker", first],
        ["LINE", "worker", second],
    ]
    assert _records(result.stdout, "STAT") == [
        ["STAT", "api", "0", "0", "0"],
        ["STAT", "worker", "1", "0", "1"],
        ["STAT", "worker", "1", "1", "1"],
        ["STAT", "webhook-worker", "0", "0", "0"],
    ]
    assert ["CONTAINERS", "worker", "2"] in _records(result.stdout, "CONTAINERS")
    assert len([call for call in calls if call.startswith("logs ")]) == 4


def test_the_host_prints_a_few_lines_per_container_and_counts_all(tmp_path: Path) -> None:
    lines = [_json_line("worker", f"step {number}", RUN_ID) for number in range(8)]
    _containers(tmp_path, {"api": [[]], "worker": [lines], "webhook-worker": [[]]})

    result, _ = _lookup(tmp_path)

    assert result.returncode == 0, result.stderr
    assert [line[2] for line in _records(result.stdout, "LINE")] == lines[:5]
    assert ["STAT", "worker", "8", "0", "8"] in _records(result.stdout, "STAT")


def _with_exc_info(line: str, exc_info: str) -> str:
    """The record with an `exc_info` after its `run_id`, as the formatter orders the keys."""
    record = json.loads(line)
    record["exc_info"] = exc_info
    return json.dumps(record, ensure_ascii=False)


# A long value before the field (message), a long value after it (exc_info), and a multi-byte
# message across every cut: the evidence line must keep the field and stay bounded.
LONG_LINES = {
    "message-before": lambda: _json_line("worker", "m" * 200, RUN_ID),
    "huge-message-before": lambda: _json_line("worker", "m" * 5000, RUN_ID),
    "exc-info-after": lambda: _with_exc_info(_json_line("worker", "boom", RUN_ID), "e" * 5000),
    "both": lambda: _with_exc_info(_json_line("worker", "m" * 3000, RUN_ID), "e" * 3000),
    "cyrillic-0": lambda: _json_line("worker", "ж" * 400, RUN_ID),
    "cyrillic-1": lambda: _json_line("worker", "я" + "ж" * 400, RUN_ID),
    "cyrillic-exc": lambda: _with_exc_info(_json_line("worker", "ж" * 400, RUN_ID), "щ" * 400),
    "cyrillic-exc-1": lambda: _with_exc_info(
        _json_line("worker", "я" + "ж" * 400, RUN_ID), "щ" * 401
    ),
}


@pytest.mark.parametrize("name", LONG_LINES)
def test_a_long_line_keeps_the_run_id_field_and_stays_bounded(tmp_path: Path, name: str) -> None:
    line = LONG_LINES[name]()
    field = f'"run_id": "{RUN_ID}"'
    assert len(line.encode()) > 300  # the old cut of the summary
    _containers(tmp_path, {"api": [[]], "worker": [[line]], "webhook-worker": [[]]})
    result, _ = _lookup(tmp_path)
    assert result.returncode == 0, result.stderr
    (record,) = _records(result.stdout, "LINE")

    code, stderr, summary = _show(tmp_path, result.stdout)

    assert code == 0, stderr
    (shown,) = [row for row in summary.splitlines() if row.startswith('{"ts"')]
    assert field in shown
    assert shown.startswith(line[:100])
    assert shown.endswith(line[-20:]) or "[...]" in shown
    assert len(shown.encode()) <= 900
    assert len("".join(record[2:]).encode()) <= 800
    assert "\ufffd" not in summary  # no half of a multi-byte character
    summary.encode("utf-8")  # valid UTF-8


def test_a_line_that_fits_is_shown_whole(tmp_path: Path) -> None:
    line = _json_line("worker", "ж" * 100, RUN_ID)
    assert len(line.encode()) <= 700
    _containers(tmp_path, {"api": [[]], "worker": [[line]], "webhook-worker": [[]]})
    result, _ = _lookup(tmp_path)

    _, _, summary = _show(tmp_path, result.stdout)

    assert f"\n{line}\n" in summary


def test_a_failing_docker_logs_fails_the_lookup_and_names_the_container(tmp_path: Path) -> None:
    _containers(
        tmp_path,
        {"api": [[]], "worker": [[_json_line("worker", "x", RUN_ID)]], "webhook-worker": [[]]},
        exits={CONTAINER["worker"]: 125},
    )

    result, calls = _lookup(tmp_path)

    assert result.returncode == 1
    assert f"docker logs failed (exit 125) for container {CONTAINER['worker']}" in result.stderr
    assert not any(f"{CONTAINER['webhook-worker']}" in call for call in calls)


def test_a_failing_filter_fails_the_lookup_while_no_match_does_not(tmp_path: Path) -> None:
    _containers(tmp_path, {"api": [[]], "worker": [["raw"]], "webhook-worker": [[]]})
    (tmp_path / "bin").mkdir(exist_ok=True)
    awk = tmp_path / "bin" / "awk"
    awk.write_text("#!/bin/bash\ncat >/dev/null\nexit 2\n")
    awk.chmod(0o755)

    result, _ = _lookup(tmp_path)

    assert result.returncode == 1
    assert f"filter failed (exit 2) on the log of container {CONTAINER['api']}" in result.stderr


def test_a_service_without_a_container_is_said_so_and_does_not_fail(tmp_path: Path) -> None:
    _containers(tmp_path, {"api": [[]], "webhook-worker": [[]]})

    result, _ = _lookup(tmp_path)

    assert result.returncode == 0, result.stderr
    assert ["CONTAINERS", "worker", "0"] in _records(result.stdout, "CONTAINERS")
    assert [stat[1] for stat in _records(result.stdout, "STAT")] == ["api", "webhook-worker"]


def test_the_lookup_fails_when_the_project_runs_none_of_the_three(tmp_path: Path) -> None:
    result, calls = _lookup(tmp_path)

    assert result.returncode == 1
    assert "no api, worker or webhook-worker container in dmc-268-api-staging\n" in result.stderr
    assert not any(call.startswith("logs ") for call in calls)


@pytest.mark.parametrize("run_id", NOT_A_RUN_ID[:1] + ["x; docker rm -f all", "$(id)"])
def test_the_host_refuses_an_id_that_is_not_one_uuid(tmp_path: Path, run_id: str) -> None:
    _containers(tmp_path, {"api": [[]], "worker": [[]], "webhook-worker": [[]]})

    result, calls = _lookup(tmp_path, RUN_ID=run_id)

    assert result.returncode == 1
    assert result.stdout == ""
    assert calls == []


def test_the_host_reads_nothing_when_the_run_id_did_not_arrive(tmp_path: Path) -> None:
    _containers(tmp_path, {"api": [[]], "worker": [[]], "webhook-worker": [[]]})

    result, _ = _bash(_ssh_script(_step(LOOKUP)), tmp_path, {})

    assert result.returncode != 0
    assert result.stdout == ""
    assert _docker_calls(tmp_path) == []


def _stdout(
    *,
    containers: Mapping[str, int] | None = None,
    stats: Sequence[tuple[str, int, int, int]] = (),
    lines: Sequence[tuple[str, str]] = (),
) -> str:
    """What the host prints, as the SSH action captures it (with its banner)."""
    counts = containers or dict.fromkeys(SERVICES, 1)
    out = [f"CONTAINERS\t{service}\t{count}" for service, count in counts.items()]
    out += [f"STAT\t{service}\t{json}\t{raw}\t{found}" for service, json, raw, found in stats]
    out += [f"LINE\t{service}\t{line}" for service, line in lines]
    return "\n".join(out) + "\n" + DRONE_SSH_BANNER


def _show(tmp_path: Path, stdout: str, run_id: str = RUN_ID) -> tuple[int, str, str]:
    result, summary = _bash(_script(SHOW), tmp_path, {"RUN_ID": run_id, "LOOKUP_STDOUT": stdout})
    return result.returncode, result.stderr, summary


def test_the_summary_counts_every_service_even_with_zero_lines(tmp_path: Path) -> None:
    worker_line = _json_line("worker", "done", RUN_ID)
    stdout = _stdout(
        stats=[("api", 40, 3, 0), ("worker", 10, 0, 1), ("webhook-worker", 5, 1, 0)],
        lines=[("worker", worker_line)],
    )

    code, stderr, summary = _show(tmp_path, stdout)

    assert code == 0, stderr
    assert summary == (
        "### Staging run logs\n\n"
        f"- Run: `{RUN_ID}`\n"
        "- Window: the last 168 h of each container's log; a container recreated by a later deploy"
        " has lost its log\n\n"
        "| Service | Containers | JSON lines | Non-JSON lines | Lines with this `run_id` |\n"
        "|---|---:|---:|---:|---:|\n"
        "| api | 1 | 40 | 3 | 0 |\n"
        "| worker | 1 | 10 | 0 | 1 |\n"
        "| webhook-worker | 1 | 5 | 1 | 0 |\n\n"
        "#### worker: 1 line\n\n"
        f"```text\n{worker_line}\n```\n"
    )
    assert "SSH" not in summary and "====" not in summary


def test_the_summary_adds_up_replicas_and_shows_at_most_five_lines_per_service(
    tmp_path: Path,
) -> None:
    lines = [("worker", _json_line("worker", f"step {n}", RUN_ID)) for n in range(5)]
    lines += [("worker", _json_line("worker", "from the second replica", RUN_ID))]
    stdout = _stdout(
        containers={"api": 1, "worker": 2, "webhook-worker": 1},
        stats=[
            ("api", 1, 0, 0),
            ("worker", 4, 0, 4),
            ("worker", 4, 2, 4),
            ("webhook-worker", 0, 0, 0),
        ],
        lines=lines,
    )

    code, stderr, summary = _show(tmp_path, stdout)

    assert code == 0, stderr
    assert "| worker | 2 | 8 | 2 | 8 |\n" in summary
    assert "#### worker: 8 lines, the first 5 shown\n" in summary
    assert summary.count('"message": "step ') == 5
    assert "from the second replica" not in summary
    assert "#### api" not in summary and "#### webhook-worker" not in summary


def test_the_summary_joins_the_pieces_of_a_long_line_and_drops_cut_characters(
    tmp_path: Path,
) -> None:
    field = f'"run_id": "{RUN_ID}"'
    stdout = _stdout(
        stats=[("api", 0, 0, 0), ("worker", 1, 0, 1), ("webhook-worker", 0, 0, 0)],
        lines=[
            ("worker", f'{{"ts": "t", "message": "ж\ufffd\t\ufffdm{field}ж\ufffd\ufffd\t\ufffd"}}')
        ],
    )

    _, _, summary = _show(tmp_path, stdout)

    assert f'\n{{"ts": "t", "message": "ж [...] m{field}ж [...] "}}\n' in summary


def test_the_summary_says_when_a_service_has_no_container(tmp_path: Path) -> None:
    stdout = _stdout(
        containers={"api": 1, "worker": 0, "webhook-worker": 1},
        stats=[("api", 4, 0, 0), ("webhook-worker", 2, 0, 0)],
    )

    code, stderr, summary = _show(tmp_path, stdout)

    assert code == 0, stderr
    assert "| worker | 0 | 0 | 0 | 0 |\n" in summary
    assert "- No `worker` container in `dmc-268-api-staging`: nothing was read\n" in summary


def test_the_summary_cannot_be_forged_with_a_closing_fence(tmp_path: Path) -> None:
    forged = _json_line("worker", "x```", RUN_ID)
    stdout = _stdout(
        stats=[("api", 0, 0, 0), ("worker", 1, 0, 1), ("webhook-worker", 0, 0, 0)],
        lines=[("worker", forged)],
    ).replace("\nSTAT", "\n## forged\nSTAT", 1)

    _, _, summary = _show(tmp_path, stdout)

    assert summary.count("```") == 2
    assert "forged" not in summary


def test_a_lookup_output_without_the_three_services_fails_the_summary(tmp_path: Path) -> None:
    code, stderr, summary = _show(tmp_path, DRONE_SSH_BANNER)

    assert code == 1
    assert "no lookup output" in stderr
    assert summary == ""


def test_the_docs_describe_the_lookup_its_secrets_and_its_window() -> None:
    cicd = (REPO_ROOT / "docs" / "CICD.md").read_text(encoding="utf-8")
    (paragraph,) = [p for p in cicd.split("\n\n") if p.startswith("**Staging run logs**")]
    secrets = (REPO_ROOT / "docs" / "SECRETS.md").read_text(encoding="utf-8")
    monitoring = (REPO_ROOT / "docs" / "MONITORING.md").read_text(encoding="utf-8")
    logs = monitoring.split("\n### JSON-логи\n", 1)[1].split("\n### ", 1)[0]

    assert cicd.index("**Prod webhook outcome**") < cicd.index("**Staging run logs**")
    assert "`staging-run-logs.yml`" in paragraph
    assert "`run_id`" in paragraph and "`dmc-268-api-staging`" in paragraph
    assert "`api`, `worker` и `webhook-worker`" in paragraph
    assert "168" in paragraph and "пересоздан более поздним выкатом" in paragraph
    assert "Ничего на хосте не меняет" in paragraph
    for name in ("VPS_DMC268_U", "VPS_DMC268_P"):
        (row,) = [line for line in secrets.splitlines() if line.startswith(f"| `{name}` |")]
        assert "staging-run-logs.yml" in row, name
    assert "`staging-run-logs.yml`" in logs and "Без SSH" in logs
