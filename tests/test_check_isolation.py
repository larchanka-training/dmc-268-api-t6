"""The read-only storage isolation check of two compose projects on one host (#308, AC1).

deploy/scripts/check-isolation.sh proves that the stores of the prod and the staging project cannot
reach each other: the default networks of the two projects share no container, every postgres,
rabbitmq and redis sits on its own project's default network only, and none publishes a host port.
It runs here against a fake docker CLI that answers from canned `docker inspect` JSON and refuses
every call but the read-only queries of the script.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "deploy" / "scripts" / "check-isolation.sh"
PROD = "dmc-268-api-prod"
STAGING = "dmc-268-api-staging"
EDGE = "dmc268-edge"
MONITORING = "dmc-268-monitoring"
STORE_PORTS = {"postgres": "5432/tcp", "rabbitmq": "5672/tcp", "redis": "6379/tcp"}
# What `docker inspect` without --format would also hand out: none of it may reach the output.
ENV_VALUE = "FAKE_TEST_CREDENTIAL_DO_NOT_USE"
IP_ADDRESS = "172.18.0.7"
MAC_ADDRESS = "02:42:ac:12:00:07"
# The Go templates of the script (checked against Docker 29.9.2); the fake answers only these.
NETWORK_FORMAT = "{{range $id, $container := .Containers}}{{println $id}}{{end}}"
INSPECT_FORMAT = (
    '{{.Name}}{{"\\t"}}{{index .Config.Labels "com.docker.compose.project"}}{{"\\t"}}'
    "{{range $name, $endpoint := .NetworkSettings.Networks}}{{$name}} {{end}}"
    '{{"\\t"}}{{range $port, $bindings := .NetworkSettings.Ports}}'
    "{{range $bindings}}{{.HostIp}}:{{.HostPort}}->{{$port}} {{end}}{{end}}"
)
HEADER = "| container | networks | host ports | verdict |"
USAGE = "usage: check-isolation.sh <project> <other-project>"

FAKE_DOCKER = r"""
# Fake docker CLI: logs the working directory and every call, answers the read-only queries of
# check-isolation.sh from the canned `docker inspect` JSON in STUB_STATE and refuses the rest.
# Faults: "unreachable" fails every call of a subcommand, "gone" an inspect of a container ID.
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as log:
    log.write(f"{os.getcwd()}\t{' '.join(args)}\n")
with open(os.environ["STUB_STATE"], encoding="utf-8") as source:
    state = json.load(source)
containers = {container["Id"]: container for container in state["containers"]}
if args[:1] and args[0] in state.get("unreachable", []):
    sys.exit("Cannot connect to the Docker daemon at unix:///var/run/docker.sock.")


def objects(ids):
    for container_id in ids:
        if container_id not in containers or container_id in state.get("gone", []):
            sys.exit(f"Error: No such object: {container_id}")
    return [containers[container_id] for container_id in ids]


def refuse():
    sys.stderr.write(f"fake docker: refused call: {' '.join(args)}\n")
    sys.exit(97)


match args:
    case ["compose", "-p", project, "ps", "-q", service]:
        for container in state["containers"]:
            labels = container["Config"]["Labels"]
            if labels["com.docker.compose.project"] == project:
                if labels["com.docker.compose.service"] == service:
                    print(container["Id"])
    case ["network", "inspect", "--format", template, network]:
        if template != os.environ["STUB_NETWORK_FORMAT"]:
            refuse()
        if network not in state["networks"]:
            sys.exit(f"Error response from daemon: network {network} not found")
        # Go templates range over a map in key order; docker ends the output with a newline.
        for container_id in sorted(containers):
            if network in containers[container_id]["NetworkSettings"]["Networks"]:
                print(container_id)
        print()
    case ["inspect", "--format", template, *ids] if ids:
        if template != os.environ["STUB_INSPECT_FORMAT"]:
            refuse()
        for container in objects(ids):
            settings = container["NetworkSettings"]
            networks = "".join(f"{name} " for name in sorted(settings["Networks"]))
            ports = "".join(
                f"{binding['HostIp']}:{binding['HostPort']}->{port} "
                for port in sorted(settings["Ports"])
                for binding in settings["Ports"][port] or []
            )
            project = container["Config"]["Labels"].get("com.docker.compose.project", "")
            print(f"{container['Name']}\t{project}\t{networks}\t{ports}")
    case ["inspect", *ids] if ids and not ids[0].startswith("-"):
        # Without --format docker hands out the whole object, the environment included.
        print(json.dumps(objects(ids), indent=4))
    case _:
        refuse()
"""


@dataclass
class Check:
    result: subprocess.CompletedProcess[str]
    # "<working directory>\t<docker arguments>" per docker call.
    calls: list[str]

    @property
    def verdict(self) -> str:
        return self.result.stdout.splitlines()[-1]

    def rows(self) -> dict[str, list[str]]:
        """The table rows by container name: networks, host ports, verdict."""
        lines = [line for line in self.result.stdout.splitlines() if line.startswith("|")]
        assert lines[:2] == [HEADER, "|---|---|---|---|"], self.result.stdout
        rows: dict[str, list[str]] = {}
        for line in lines[2:]:
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            assert len(cells) == 4, line
            assert cells[0] not in rows, f"two rows for {cells[0]}"
            rows[cells[0]] = cells[1:]
        return rows


def _id(project: str, service: str) -> str:
    return hashlib.sha256(f"{project}-{service}-1".encode()).hexdigest()


def _container(
    project: str,
    service: str,
    networks: Iterable[str],
    ports: Mapping[str, list[dict[str, str]] | None] | None = None,
) -> dict[str, Any]:
    """A container as `docker inspect` returns it, cut to the fields the fake reads."""
    name = f"{project}-{service}-1"
    return {
        "Id": _id(project, service),
        "Name": f"/{name}",
        "Config": {
            "Labels": {
                "com.docker.compose.project": project,
                "com.docker.compose.service": service,
            },
            "Env": [f"POSTGRES_PASSWORD={ENV_VALUE}", "PATH=/usr/local/bin:/usr/bin"],
        },
        "NetworkSettings": {
            "Networks": {
                network: {"Aliases": [service], "IPAddress": IP_ADDRESS, "MacAddress": MAC_ADDRESS}
                for network in networks
            },
            "Ports": dict(ports or {}),
        },
    }


def _host() -> list[dict[str, Any]]:
    """Prod and staging side by side; the monitoring stack joins the staging network only."""
    containers: list[dict[str, Any]] = []
    for project in (PROD, STAGING):
        containers += [
            _container(project, store, [f"{project}_default"], {port: None})
            for store, port in STORE_PORTS.items()
        ]
        containers.append(_container(project, "api", [f"{project}_default", EDGE]))
        containers.append(_container(project, "worker", [f"{project}_default"]))
    # deploy/monitoring/compose.yml: networks.api is the staging project's default network.
    containers += [
        _container(MONITORING, service, [f"{MONITORING}_default", f"{STAGING}_default"])
        for service in ("prometheus", "grafana", "postgres-exporter", "redis-exporter", "pg-backup")
    ]
    return containers


def _find(containers: list[dict[str, Any]], project: str, service: str) -> dict[str, Any]:
    name = f"/{project}-{service}-1"
    return next(container for container in containers if container["Name"] == name)


def _join(container: dict[str, Any], *networks: str) -> None:
    for network in networks:
        container["NetworkSettings"]["Networks"][network] = {"IPAddress": IP_ADDRESS}


def _check(
    tmp_path: Path,
    containers: list[dict[str, Any]],
    *args: str,
    networks: Iterable[str] | None = None,
    faults: Mapping[str, list[str]] | None = None,
) -> Check:
    if networks is None:
        networks = {
            network
            for container in containers
            for network in container["NetworkSettings"]["Networks"]
        }
    state = tmp_path / "docker-state.json"
    state.write_text(
        json.dumps({"networks": sorted(networks), "containers": containers, **(faults or {})}),
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text(f"#!{sys.executable}\n{FAKE_DOCKER}", encoding="utf-8")
    docker.chmod(0o755)
    log = tmp_path / "docker.log"
    log.unlink(missing_ok=True)
    result = subprocess.run(
        [str(SCRIPT), *(args or (PROD, STAGING))],
        cwd=tmp_path,
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "STUB_LOG": str(log),
            "STUB_STATE": str(state),
            "STUB_NETWORK_FORMAT": NETWORK_FORMAT,
            "STUB_INSPECT_FORMAT": INSPECT_FORMAT,
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return Check(result, calls)


def _store_rows() -> dict[str, list[str]]:
    return {
        f"{project}-{store}-1": [f"{project}_default", "none", "ok"]
        for project in (PROD, STAGING)
        for store in STORE_PORTS
    }


def test_passes_with_monitoring_on_the_staging_network_only(tmp_path: Path) -> None:
    check = _check(tmp_path, _host())

    assert check.result.returncode == 0, check.result.stderr
    # One row per store of both projects; the api on the edge and the monitoring stack on the
    # staging network are no violation.
    assert check.rows() == _store_rows()
    assert check.verdict.startswith("isolation: pass"), check.result.stdout
    assert check.result.stderr == ""


def test_reads_docker_state_only_and_from_the_root_directory(tmp_path: Path) -> None:
    check = _check(tmp_path, _host())

    assert check.result.returncode == 0, check.result.stderr
    # From / no compose file or .env of the caller's directory can reach `docker compose ps`.
    assert {call.split("\t", 1)[0] for call in check.calls} == {"/"}
    commands = [call.split("\t", 1)[1] for call in check.calls]
    for project in (PROD, STAGING):
        for store in STORE_PORTS:
            assert f"compose -p {project} ps -q {store}" in commands
        assert f"network inspect --format {NETWORK_FORMAT} {project}_default" in commands
    assert all(
        command.startswith(("compose -p ", "network inspect --format ", "inspect --format "))
        for command in commands
    ), commands


def test_fails_when_a_container_is_on_both_default_networks(tmp_path: Path) -> None:
    # The bridge Part 4 must not build: one exporter on the default networks of both projects.
    containers = _host()
    _join(_find(containers, MONITORING, "postgres-exporter"), f"{PROD}_default")

    check = _check(tmp_path, containers)

    assert check.result.returncode == 1
    rows = check.rows()
    exporter = rows.pop(f"{MONITORING}-postgres-exporter-1")
    assert exporter[0] == (f"{PROD}_default, {STAGING}_default, {MONITORING}_default")
    assert exporter[1] == "none"
    assert exporter[2].startswith("FAIL"), exporter
    assert f"{PROD}_default" in exporter[2] and f"{STAGING}_default" in exporter[2]
    assert rows == _store_rows()
    assert check.verdict.startswith("isolation: FAIL"), check.result.stdout


@pytest.mark.parametrize(
    ("project", "other"),
    [
        pytest.param(STAGING, PROD, id="staging-worker-on-the-prod-network"),
        pytest.param(PROD, STAGING, id="prod-worker-on-the-staging-network"),
    ],
)
def test_fails_when_a_container_of_one_project_is_on_the_other_default_network(
    tmp_path: Path, project: str, other: str
) -> None:
    # On the other project's network only, the worker reaches that project's stores directly.
    containers = _host()
    _find(containers, project, "worker")["NetworkSettings"]["Networks"] = {
        f"{other}_default": {"IPAddress": IP_ADDRESS}
    }

    check = _check(tmp_path, containers)

    assert check.result.returncode == 1, check.result.stdout
    rows = check.rows()
    worker = rows.pop(f"{project}-worker-1")
    assert worker[:2] == [f"{other}_default", "none"]
    assert worker[2].startswith("FAIL"), worker
    assert f"compose project {project} on {other}_default" in worker[2], worker
    assert rows == _store_rows()
    assert check.verdict.startswith("isolation: FAIL"), check.result.stdout


@pytest.mark.parametrize(
    ("networks", "reasons"),
    [
        pytest.param([f"{PROD}_default", EDGE], [f"on {EDGE}"], id="also-on-the-edge"),
        pytest.param(
            [f"{PROD}_default", f"{STAGING}_default"],
            [f"on {STAGING}_default"],
            id="also-on-the-staging-network",
        ),
        pytest.param(
            [f"{STAGING}_default"],
            [f"not on {PROD}_default", f"on {STAGING}_default"],
            id="on-the-staging-network-instead",
        ),
    ],
)
def test_fails_when_prod_postgres_leaves_its_own_network(
    tmp_path: Path, networks: list[str], reasons: list[str]
) -> None:
    containers = _host()
    _find(containers, PROD, "postgres")["NetworkSettings"]["Networks"] = {
        network: {"IPAddress": IP_ADDRESS} for network in networks
    }

    check = _check(tmp_path, containers)

    assert check.result.returncode == 1
    rows = check.rows()
    # A store on both default networks breaks two rules but stays one row.
    postgres = rows.pop(f"{PROD}-postgres-1")
    assert postgres[0] == ", ".join(sorted(networks))
    assert postgres[2].startswith("FAIL"), postgres
    for reason in reasons:
        assert reason in postgres[2], postgres
    expected = _store_rows()
    del expected[f"{PROD}-postgres-1"]
    assert rows == expected
    assert check.verdict.startswith("isolation: FAIL"), check.result.stdout


def test_fails_on_a_published_store_port(tmp_path: Path) -> None:
    containers = _host()
    _find(containers, STAGING, "redis")["NetworkSettings"]["Ports"] = {
        "6379/tcp": [
            {"HostIp": "0.0.0.0", "HostPort": "6379"},
            {"HostIp": "::", "HostPort": "6379"},
        ]
    }

    check = _check(tmp_path, containers)

    assert check.result.returncode == 1
    redis = check.rows()[f"{STAGING}-redis-1"]
    assert redis[:2] == [
        f"{STAGING}_default",
        "0.0.0.0:6379->6379/tcp, :::6379->6379/tcp",
    ]
    assert redis[2].startswith("FAIL"), redis
    assert "host port" in redis[2], redis
    assert check.verdict.startswith("isolation: FAIL"), check.result.stdout


def test_output_holds_only_names_networks_and_ports(tmp_path: Path) -> None:
    containers = _host()
    _join(_find(containers, MONITORING, "redis-exporter"), f"{PROD}_default")
    _find(containers, PROD, "rabbitmq")["NetworkSettings"]["Ports"] = {
        "15672/tcp": [{"HostIp": "127.0.0.1", "HostPort": "15672"}]
    }

    check = _check(tmp_path, containers)

    assert check.result.returncode == 1
    for output in (check.result.stdout, check.result.stderr):
        for value in (ENV_VALUE, "POSTGRES_PASSWORD", IP_ADDRESS, MAC_ADDRESS):
            assert value not in output
    # Apart from the table only the verdict line.
    other = [line for line in check.result.stdout.splitlines() if not line.startswith("|")]
    assert [line for line in other if line] == [check.verdict]


@pytest.mark.parametrize(
    ("project", "services", "missing"),
    [
        pytest.param(PROD, set(STORE_PORTS) | {"api", "worker"}, "postgres", id="stack-stopped"),
        pytest.param(STAGING, {"redis"}, "redis", id="one-store-missing"),
    ],
)
def test_exits_2_when_a_project_lacks_a_running_store(
    tmp_path: Path, project: str, services: set[str], missing: str
) -> None:
    # After a stop-mode rollback the prod project has no container at all; a five-row table
    # would not prove the isolation of the store that is not running.
    containers = [
        container
        for container in _host()
        if container["Name"] not in {f"/{project}-{service}-1" for service in services}
    ]

    check = _check(tmp_path, containers)

    assert check.result.returncode == 2
    assert f"no running {missing} container in compose project {project}" in check.result.stderr
    assert check.result.stdout == ""


@pytest.mark.parametrize(
    ("faults", "dropped_network", "message"),
    [
        pytest.param(
            {"unreachable": ["compose"]},
            None,
            "Cannot connect to the Docker daemon",
            id="compose-ps",
        ),
        pytest.param(
            {}, f"{STAGING}_default", f"network {STAGING}_default not found", id="network-inspect"
        ),
        # The container went away between `compose ps` and `docker inspect`.
        pytest.param(
            {"gone": [_id(PROD, "redis")]},
            None,
            f"No such object: {_id(PROD, 'redis')}",
            id="inspect",
        ),
    ],
)
def test_exits_2_when_a_docker_query_fails(
    tmp_path: Path, faults: dict[str, list[str]], dropped_network: str | None, message: str
) -> None:
    containers = _host()
    networks = {
        network for container in containers for network in container["NetworkSettings"]["Networks"]
    } - {dropped_network}

    check = _check(tmp_path, containers, networks=networks, faults=faults)

    # A failed docker query is no verdict: exit 2, never the violation code 1.
    assert check.result.returncode == 2
    assert message in check.result.stderr
    assert "check-isolation: a docker query failed" in check.result.stderr
    assert check.result.stdout == ""


@pytest.mark.parametrize(
    "args",
    [
        pytest.param((PROD,), id="one-project"),
        pytest.param((PROD, PROD), id="same-project"),
        pytest.param((PROD, ""), id="empty-project"),
        pytest.param((PROD, STAGING, EDGE), id="three-arguments"),
    ],
)
def test_usage(tmp_path: Path, args: tuple[str, ...]) -> None:
    check = _check(tmp_path, _host(), *args)

    assert check.result.returncode == 2
    assert check.result.stderr.strip() == USAGE
    assert check.calls == []
