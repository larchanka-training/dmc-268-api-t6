"""The public status page in the acceptance workflow (#309).

The status step of the workflow, run as a shell script against a fake curl, and the opt-in job
that stops the staging worker and waits for the page to show it. The workflow steps and their
helpers are shared with tests/test_monitoring_acceptance.py through
tests/monitoring_acceptance_workflow.py.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

from tests.monitoring_acceptance_workflow import (
    COURSE_TARGET,
    DRONE_SSH_BANNER,
    TERRAFORM_TARGET,
    WORKFLOW,
    _report_step,
    _step,
    _target_step,
)

STATUS_URL = "https://status.example.test"
STAGING_STATUS_URL = "https://staging-status.example.test"
PROMETHEUS_PATHS = (
    "/api/v1/query?query=up",
    "/api/v1/alerts",
    "/api/v2/alerts",
    "/api/v2/silences",
    "/-/ready",
    "/metrics",
    "/api/health",
    "/login",
    "/graph",
)
PAGE_PROD_AND_STAGING = '<section data-env="prod"></section><section data-env="staging"></section>'
PAGE_STAGING_ONLY = '<section data-env="staging"></section>'
FAKE_CURL = """#!/usr/bin/env python3
import json, os, sys
config = json.load(open(os.environ["FAKE_CURL_CONFIG"]))
url = sys.argv[-1]
if "-w" in sys.argv:
    print(config["codes"].get(url, config["default_code"]), end="")
else:
    counter = os.environ["FAKE_CURL_CONFIG"] + ".count"
    count = int(open(counter).read()) if os.path.exists(counter) else 0
    open(counter, "w").write(str(count + 1))
    bodies = config["bodies"].get(url, [""])
    print(bodies[min(count, len(bodies) - 1)], end="")
"""


def _status_step(
    tmp_path: Path,
    step: str,
    codes: dict[str, str],
    bodies: dict[str, list[str]],
    **env: str,
) -> tuple[subprocess.CompletedProcess[str], str, str]:
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    (stubs / "curl").write_text(FAKE_CURL, encoding="utf-8")
    (stubs / "sleep").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    for stub in ("curl", "sleep"):
        (stubs / stub).chmod(0o755)
    config = tmp_path / "curl.json"
    config.write_text(
        json.dumps({"codes": codes, "default_code": "000", "bodies": bodies}), encoding="utf-8"
    )
    summary = tmp_path / "summary.md"
    output = tmp_path / "output"
    output.touch()
    result = subprocess.run(
        ["/bin/bash", "-e", "-c", _step(step)],
        env={
            "PATH": f"{stubs}:{os.environ['PATH']}",
            "FAKE_CURL_CONFIG": str(config),
            "GITHUB_STEP_SUMMARY": str(summary),
            "GITHUB_OUTPUT": str(output),
            "STATUS_URL": STATUS_URL,
            "STAGING_STATUS_URL": STAGING_STATUS_URL,
            "STOP_STARTED_AT": str(int(time.time())),
            **env,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    written = summary.read_text(encoding="utf-8") if summary.exists() else ""
    return result, written, output.read_text(encoding="utf-8")


def _healthy_status_site() -> tuple[dict[str, str], dict[str, list[str]]]:
    codes = {f"{base}/": "200" for base in (STATUS_URL, STAGING_STATUS_URL)}
    for base in (STATUS_URL, STAGING_STATUS_URL):
        codes.update({f"{base}{path}": "404" for path in PROMETHEUS_PATHS})
    bodies = {
        f"{STATUS_URL}/": [PAGE_PROD_AND_STAGING],
        f"{STAGING_STATUS_URL}/": [PAGE_STAGING_ONLY],
    }
    return codes, bodies


def test_status_step_passes_when_only_the_page_is_public_on_both_names(tmp_path: Path) -> None:
    codes, bodies = _healthy_status_site()

    result, summary, output = _status_step(tmp_path, "Public status page", codes, bodies)

    assert result.returncode == 0, result.stderr
    assert "result=ok" in output.splitlines()
    # Every path of Prometheus, Alertmanager and Grafana is in the table, on both names.
    for base in (STATUS_URL, STAGING_STATUS_URL):
        for path in PROMETHEUS_PATHS:
            assert f"| `{base}{path}` | 404 | 404 |" in summary
        assert f"| `{base}/` | 200 | 200 |" in summary


@pytest.mark.parametrize(
    "leaking",
    [f"{base}{path}" for base in (STATUS_URL, STAGING_STATUS_URL) for path in PROMETHEUS_PATHS],
)
def test_status_step_fails_on_any_prometheus_path_that_answers(
    tmp_path: Path, leaking: str
) -> None:
    codes, bodies = _healthy_status_site()
    codes[leaking] = "200"

    result, summary, output = _status_step(tmp_path, "Public status page", codes, bodies)

    # The result is decided in Report, so the host checks still run.
    assert result.returncode == 0, result.stderr
    assert "result=fail" in output.splitlines()
    assert f"| `{leaking}` | 200 | 404 |" in summary


@pytest.mark.parametrize(
    ("prod_page", "staging_page"),
    [
        # staging-status.* must not show prod; status.* must show both.
        (PAGE_PROD_AND_STAGING, PAGE_PROD_AND_STAGING),
        (PAGE_STAGING_ONLY, PAGE_STAGING_ONLY),
        ("", PAGE_STAGING_ONLY),
    ],
)
def test_status_step_fails_on_the_wrong_sections(
    tmp_path: Path, prod_page: str, staging_page: str
) -> None:
    codes, _ = _healthy_status_site()
    bodies = {f"{STATUS_URL}/": [prod_page], f"{STAGING_STATUS_URL}/": [staging_page]}

    result, _, output = _status_step(tmp_path, "Public status page", codes, bodies)

    assert result.returncode == 0, result.stderr
    assert "result=fail" in output.splitlines()


def test_status_step_fails_when_a_name_does_not_connect(tmp_path: Path) -> None:
    result, summary, output = _status_step(tmp_path, "Public status page", {}, {})

    assert result.returncode == 0, result.stderr
    assert "result=fail" in output.splitlines()
    assert f"| `{STATUS_URL}/` | 000 | 200 |" in summary


def test_status_step_runs_on_the_course_vps_only_and_report_fails_on_it(tmp_path: Path) -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    step = workflow[workflow.index("- name: Public status page") :]
    step = step[: step.index("- name: Upload acceptance script")]

    assert "if: steps.target.outputs.status_url != ''" in step
    # After the Grafana step and before the host checks, which run whatever it finds.
    assert workflow.index("- name: Public Grafana endpoints") < workflow.index(
        "- name: Public status page"
    )
    assert "exit 1" not in step
    code, _ = _report_step(tmp_path, "all checks passed\n" + DRONE_SSH_BANNER, STATUS="fail")
    assert code == 1
    code, _ = _report_step(tmp_path, "all checks passed\n" + DRONE_SSH_BANNER, STATUS="ok")
    assert code == 0


def test_target_step_resolves_the_status_names_of_the_course_vps_only(tmp_path: Path) -> None:
    (tmp_path / "course").mkdir()
    (tmp_path / "terraform").mkdir()
    _, _, course = _target_step(tmp_path / "course", **COURSE_TARGET)
    _, _, terraform = _target_step(tmp_path / "terraform", **TERRAFORM_TARGET)

    assert f"status_url={STATUS_URL}" in course.splitlines()
    assert f"staging_status_url={STAGING_STATUS_URL}" in course.splitlines()
    assert "status_url=" in terraform.splitlines()
    assert "staging_status_url=" in terraform.splitlines()


def _outage_job() -> str:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    return workflow[workflow.index("\n  worker-outage:\n") :]


def _outage_page(state: str, alerts: str, since: str = "с 2026-11-03 10:00 UTC") -> str:
    # As render_page lays a row out; test_status_page.py checks the real one.
    return (
        f'<li class="row {state}" data-env="staging" data-component="review" '
        f'data-state="{state}" data-alerts="{alerts}"><span class="name">Ревью PR</span>'
        '<span class="state">x</span>'
        + (f' <span class="since">{since}</span>' if since else "")
        + "</li>"
    )


def test_worker_outage_is_an_opt_in_job_on_main_after_the_acceptance() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    job = _outage_job()

    assert (
        "      stop_worker:\n"
        '        description: "Stop the staging worker (~10 min without reviews)'
    ) in workflow
    inputs = workflow[workflow.index("      stop_worker:\n") :]
    assert "        type: boolean\n        default: false\n" in inputs[:400]
    assert "    needs: acceptance\n" in job
    assert "github.ref == 'refs/heads/main'" in job
    assert "inputs.stop_worker" in job
    assert "      name: staging\n" in job
    # Only after a passed acceptance: no staging outage on a failing stack.
    condition = next(line for line in job.splitlines() if line.startswith("    if: "))
    assert "needs.acceptance.result == 'success'" in condition
    assert "cancelled()" not in condition
    assert "appleboy/ssh-action@0ff4204d59e8e51228ff73bce53f80d53301dee2" in job
    assert "fingerprint: ${{ vars.STAGING_SSH_FINGERPRINT }}" in job


def test_worker_outage_stops_only_the_worker_under_a_planned_silence() -> None:
    job = _outage_job()
    stop = job[job.index("- name: Silence the alerts and stop the worker") :]
    stop = stop[: stop.index("- name: Wait for the page to show the outage")]

    # Each silence carries `environment`: a prod alert of the same name stays loud.
    adds = [line for line in stop.splitlines() if "silence add" in line]
    assert len(adds) == 2
    for line, rule in zip(
        adds, ("ReviewQueueWithoutConsumer", "ReviewRunsStuckInQueue"), strict=True
    ):
        assert f"silence add alertname={rule} environment=staging --duration=20m" in line
        assert line.endswith("--comment=acceptance-309")
    assert 'docker compose -p "${project}" stop worker\n' in stop
    for other in ("api", "webhook-worker", "postgres", "rabbitmq"):
        assert f"stop {other}" not in stop
    # The silences come first: no page to the team chat for a planned stop.
    assert stop.index("silence add") < stop.index("stop worker")


def test_worker_outage_always_starts_the_worker_and_lifts_the_silences_it_made() -> None:
    job = _outage_job()
    start = job[job.index("- name: Start the worker and lift the silences") :]
    start = start[: start.index("- name: Wait for the page to show the worker again")]

    assert "if: ${{ always() && steps.stop.conclusion != 'skipped' }}" in start
    assert 'docker compose -p "${project}" start worker || status=1' in start
    # Only the silences of this run, by their comment; a failed start still lifts them and fails
    # the step afterwards, so a worker that stays down alerts.
    assert "awk '/acceptance-309$/ { print $1 }'" in start
    assert (
        start.index("start worker")
        < start.index("silence expire")
        < start.index('exit "${status}"')
    )


FAKE_COMPOSE_DOCKER = """#!/bin/bash
echo "$*" >> "$DOCKER_LOG"
case "$*" in
  *"silence query"*)
    echo "ID Matchers Ends At Created By Comment"
    echo "id-one alertname=A 2026-11-03 amtool acceptance-309"
    echo "id-two alertname=B 2026-11-03 amtool acceptance-309"
    echo "id-other alertname=C 2026-11-03 amtool somebody-else" ;;
  # docker compose exec forwards its stdin to the command, even with -T.
  *"silence expire"*) cat > /dev/null ;;
esac
"""


def test_the_start_step_expires_every_silence_of_the_run(tmp_path: Path) -> None:
    job = _outage_job()
    script = job[job.index("- name: Start the worker and lift the silences") :]
    script = script.split("          script: |\n", 1)[1].split("\n      - name:", 1)[0]
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "docker").write_text(FAKE_COMPOSE_DOCKER, encoding="utf-8")
    (stubs / "docker").chmod(0o755)
    log = tmp_path / "docker.log"

    run = subprocess.run(
        ["/bin/bash", "-c", textwrap.dedent(script)],
        env={
            "PATH": f"{stubs}:{os.environ['PATH']}",
            "API_DIR": "/opt/dmc-268-api-staging",
            "DOCKER_LOG": str(log),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    calls = log.read_text(encoding="utf-8").splitlines()
    assert run.returncode == 0, run.stderr
    assert any(call.endswith("start worker") for call in calls)
    assert [call.split()[-1] for call in calls if "silence expire" in call] == ["id-one", "id-two"]


def test_worker_outage_times_from_before_the_stop() -> None:
    job = _outage_job()
    names = re.findall(r"- name: (.+)", job)
    clock = names.index("Note the time of the stop")
    assert names[clock - 1 : clock + 2] == [
        "Check that the page shows the review component working",
        "Note the time of the stop",
        "Silence the alerts and stop the worker",
    ]
    waiting = job[job.index("- name: Wait for the page to show the outage") :]
    waiting = waiting[: waiting.index("- name: Start the worker")]
    assert 'start="$(date +%s)"' not in waiting and "STOP_STARTED_AT" in waiting
    assert 'echo "STOP_STARTED_AT=$(date +%s)" >> "${GITHUB_ENV}"' in job


@pytest.mark.parametrize(
    ("page", "code"),
    [(_outage_page("up", ""), 0), (_outage_page("down", "PostgresDown"), 1), ("", 1)],
)
def test_the_worker_stays_up_unless_the_row_works(tmp_path: Path, page: str, code: int) -> None:
    run, _, _ = _status_step(
        tmp_path,
        "Check that the page shows the review component working",
        {},
        {f"{STATUS_URL}/": [page]},
    )

    assert run.returncode == code, run.stderr


def test_outage_step_counts_from_the_stop(tmp_path: Path) -> None:
    page = _outage_page("down", "ReviewQueueWithoutConsumer")
    run, summary, _ = _status_step(
        tmp_path,
        "Wait for the page to show the outage",
        {},
        {f"{STATUS_URL}/": [page]},
        STOP_STARTED_AT=str(int(time.time()) - 500),
    )

    assert run.returncode == 1  # seen at once, but 500 s after the stop
    shown = re.search(r"(\d+) s after the stop", summary)
    assert shown is not None and int(shown[1]) >= 500


def test_outage_step_passes_when_the_page_shows_the_review_component_down_in_time(
    tmp_path: Path,
) -> None:
    url = f"{STATUS_URL}/"
    pages = [
        _outage_page("up", ""),
        _outage_page("up", ""),
        _outage_page("down", "ReviewQueueWithoutConsumer ReviewRunsStuckInQueue"),
    ]

    result, summary, output = _status_step(
        tmp_path, "Wait for the page to show the outage", {}, {url: pages}
    )

    assert result.returncode == 0, result.stderr
    assert "result=ok" in output.splitlines()
    assert 'staging row "Ревью PR" down with ReviewQueueWithoutConsumer' in summary


@pytest.mark.parametrize(
    "page",
    [
        _outage_page("up", ""),
        # Another alert of the row, another component, another environment: not the outage.
        _outage_page("down", "PostgresDown"),
        _outage_page("down", "ReviewQueueWithoutConsumer").replace("review", "api"),
        _outage_page("down", "ReviewQueueWithoutConsumer").replace('"staging"', '"prod"'),
        # No "since" in the row: missing, malformed, next row's.
        _outage_page("down", "ReviewQueueWithoutConsumer", since=""),
        _outage_page("down", "ReviewQueueWithoutConsumer", since="с вчера"),
        _outage_page("down", "ReviewQueueWithoutConsumer", since="") + _outage_page("up", ""),
        "",
    ],
)
def test_outage_step_fails_when_the_page_never_shows_the_outage(tmp_path: Path, page: str) -> None:
    result, summary, output = _status_step(
        tmp_path,
        "Wait for the page to show the outage",
        {},
        {f"{STATUS_URL}/": [page]},
        WAIT_LIMIT_SECONDS="0",
    )

    assert result.returncode == 1
    assert "result=fail" in output.splitlines()
    assert "did not show the outage" in summary


def test_outage_step_fails_when_the_page_is_too_slow(tmp_path: Path) -> None:
    page = _outage_page("down", "ReviewQueueWithoutConsumer")

    result, summary, output = _status_step(
        tmp_path,
        "Wait for the page to show the outage",
        {},
        {f"{STATUS_URL}/": [page]},
        OUTAGE_LIMIT_SECONDS="-1",
    )

    assert result.returncode == 1
    assert "result=fail" in output.splitlines()
    assert "limit -1 s" in summary


def test_recovery_step_reports_the_return_to_working(tmp_path: Path) -> None:
    pages = [_outage_page("down", "ReviewQueueWithoutConsumer"), _outage_page("up", "")]

    result, summary, _ = _status_step(
        tmp_path, "Wait for the page to show the worker again", {}, {f"{STATUS_URL}/": pages}
    )

    assert result.returncode == 0, result.stderr
    assert "is back to working" in summary


def test_recovery_step_fails_when_the_component_stays_down(tmp_path: Path) -> None:
    page = _outage_page("down", "ReviewQueueWithoutConsumer")

    result, summary, _ = _status_step(
        tmp_path,
        "Wait for the page to show the worker again",
        {},
        {f"{STATUS_URL}/": [page]},
        WAIT_LIMIT_SECONDS="0",
    )

    assert result.returncode == 1
    assert "was not working again" in summary
