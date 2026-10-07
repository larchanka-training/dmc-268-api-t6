"""Environment parity contracts (ui#66, part 2).

The local stack bootstraps the schema like staging, the prod UI host is one origin, hosts without
an upstream answer a stub instead of a bare 502, and the staging stores are pinned by digest.

Text contracts over the compose files and the Caddyfile, in the manner of
test_deploy_staging_services.py: no Docker daemon and no Caddy binary are needed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCAL_COMPOSE = REPO_ROOT / "docker-compose.yml"
STAGING_COMPOSE = REPO_ROOT / "deploy" / "compose" / "staging.yml"
CADDYFILE = REPO_ROOT / "deploy" / "edge" / "Caddyfile"

# Hosts whose service is not deployed yet: prod and the standalone webhook service.
HOSTS_WITHOUT_UPSTREAM = ("api", "webhook", "staging-webhook", "ui")
DEPLOYED_HOSTS = ("staging-api", "staging-ui")
# Upstreams of every host in route order, as in the route table of docs/CICD.md §8.2.
UPSTREAMS = {
    "staging-api": ["api-staging:8000"],
    "api": ["api-prod:8000"],
    "staging-webhook": ["webhook-staging:8000"],
    "webhook": ["webhook-prod:8000"],
    "staging-ui": ["api-staging:8000", "ui-staging:8080"],
    "ui": ["api-prod:8000", "ui-prod:8080"],
}
STORE_MAJORS = {"postgres": "17", "rabbitmq": "4", "redis": "8"}


def _service_block(compose: Path, name: str) -> str:
    text = compose.read_text(encoding="utf-8")
    match = re.search(rf"^  {name}:\n((?:    .*\n|\n)*)", text, re.MULTILINE)
    assert match is not None, f"service {name} is missing in {compose.name}"
    return match.group(1)


def _command(block: str) -> str:
    match = re.search(r"^    command: (.+)$", block, re.MULTILINE)
    assert match is not None
    return match.group(1)


def _healthcheck_test(block: str) -> list[str]:
    """Items of ``healthcheck.test``, written as an inline or as a block YAML list."""
    inline = re.search(r"^      test: \[(.+)\]$", block, re.MULTILINE)
    if inline is not None:
        return [item.strip().strip('"') for item in inline.group(1).split(",")]
    listed = re.search(r"^      test:\n((?:        - .+\n)+)", block, re.MULTILINE)
    assert listed is not None, "healthcheck test is missing"
    return [line.strip()[2:].strip().strip('"') for line in listed.group(1).splitlines()]


def _site(host: str) -> str:
    """Body of the site block ``<host>.{$APP_DOMAIN}``; the apex is the empty host."""
    address = re.escape(f"{host}." if host else "") + r"\{\$APP_DOMAIN\}"
    match = re.search(
        rf"^{address} \{{\n(.*?)^\}}\n", CADDYFILE.read_text(encoding="utf-8"), re.M | re.S
    )
    assert match is not None, f"site {host or 'apex'} is missing"
    return match.group(1)


def test_local_bootstrap_migrates_and_seeds_like_staging() -> None:
    local = _service_block(LOCAL_COMPOSE, "bootstrap")
    staging = _service_block(STAGING_COMPOSE, "bootstrap")

    assert _command(local) == _command(staging)
    assert "alembic upgrade head" in _command(local)
    assert "python -m app.bootstrap.seed_prompts" in _command(local)
    # One shot: a failed migration must stop the stack, not loop.
    assert re.search(r'^    restart: "no"$', local, re.MULTILINE)
    assert "ports:" not in local


@pytest.mark.parametrize("service", ["backend", "worker", "webhook-worker"])
def test_local_processes_start_only_after_a_completed_bootstrap(service: str) -> None:
    block = _service_block(LOCAL_COMPOSE, service)

    assert re.search(
        r"^      bootstrap:\n        condition: service_completed_successfully$",
        block,
        re.MULTILINE,
    ), f"{service} must wait for the migrations and the prompt seed"


def test_local_backend_waits_for_the_broker() -> None:
    block = _service_block(LOCAL_COMPOSE, "backend")

    assert re.search(r"^      rabbitmq:\n        condition: service_healthy$", block, re.MULTILINE)


@pytest.mark.parametrize("compose", [LOCAL_COMPOSE, STAGING_COMPOSE], ids=["local", "staging"])
def test_bootstrap_migrates_only_a_healthy_postgres(compose: Path) -> None:
    block = _service_block(compose, "bootstrap")

    # A started but not yet accepting server fails the migration, and with it the whole `up`.
    assert re.search(r"^      postgres:\n        condition: service_healthy$", block, re.MULTILINE)


def _rabbitmq_start_period(compose: Path) -> str | None:
    block = _service_block(compose, "rabbitmq")
    match = re.search(r"^      start_period: (\S+)$", block, re.MULTILINE)
    return None if match is None else match.group(1)


def test_local_broker_healthcheck_keeps_the_staging_start_period() -> None:
    local = _rabbitmq_start_period(LOCAL_COMPOSE)

    # Probes that fail while the broker boots do not count against the retries that backend
    # and the workers wait on; the local stack keeps the staging grace period.
    assert local is not None
    assert local == _rabbitmq_start_period(STAGING_COMPOSE)


@pytest.mark.parametrize("service", ["worker", "webhook-worker"])
def test_local_worker_healthcheck_reads_its_heartbeat(service: str) -> None:
    block = _service_block(LOCAL_COMPOSE, service)
    heartbeat = re.search(r"^      WORKER_HEARTBEAT_FILE: (\S+)$", block, re.MULTILINE)

    assert heartbeat is not None
    # A disabled healthcheck makes `docker compose up --wait` fail on Compose 2.33 and tells
    # nothing about the consumers; the image HEALTHCHECK polls :8000, which no worker serves.
    assert "disable: true" not in block
    assert _healthcheck_test(block) == [
        "CMD",
        "python",
        "-m",
        "app.common.infrastructure.heartbeat",
        heartbeat.group(1),
        "30",
    ]


def test_prod_ui_host_sends_api_paths_to_the_api_and_the_rest_to_the_ui() -> None:
    site = _site("ui")
    api_route = re.search(r"handle /api/\* \{\n(.*?)\n\t\}", site, re.DOTALL)
    fallback = re.search(r"handle \{\n(.*?)\n\t\}", site, re.DOTALL)

    assert api_route is not None and fallback is not None
    assert api_route.group(1).strip() == "reverse_proxy api-prod:8000"
    assert fallback.group(1).strip() == "reverse_proxy ui-prod:8080"
    assert api_route.start() < fallback.start()


@pytest.mark.parametrize(("host", "upstreams"), UPSTREAMS.items())
def test_host_proxies_only_to_its_own_environment(host: str, upstreams: list[str]) -> None:
    site = _site(host)

    # A prod or webhook host that proxied to staging would serve staging data under its name.
    assert re.findall(r"reverse_proxy (\S+)", site) == upstreams
    # A site-level respond would answer before any route.
    assert not re.search(r"^\trespond ", site, re.MULTILINE)


def test_stub_answers_503_only_when_the_upstream_is_unreachable() -> None:
    caddyfile = CADDYFILE.read_text(encoding="utf-8")
    snippet = re.search(r"^\(not_deployed\) \{\n(.*?)^\}\n", caddyfile, re.M | re.S)

    assert snippet is not None
    # handle_errors runs when the proxy itself fails (no upstream: 502); a response of a running
    # upstream, 5xx included, passes through untouched. Site headers do not reach error routes,
    # so the stub sets HSTS itself.
    stub = re.search(
        r"^\thandle_errors 502 \{\n\t\timport hsts\n\t\trespond (.+) 503\n\t\}$",
        snippet.group(1),
        re.MULTILINE,
    )
    assert stub is not None
    # The stub also answers for a deployed service that is down: it must not send prod users
    # to staging.
    assert "staging" not in stub.group(1).lower()
    # ui.* has two upstreams: with ui-prod up and api-prod down, /api/* gets the stub while the
    # host itself answers, so the text names the service behind the address, not the host.
    assert stub.group(1) == '"The service behind this address is not running or is restarting."'


def test_proxy_errors_stay_in_the_log_next_to_handle_errors() -> None:
    # Once any site has handle_errors, Caddy logs proxy errors at debug level for every site of
    # the server: without this log the cause of a staging 502 is gone from the edge log.
    assert re.match(
        r"\{\n(?:\t#.*\n)*\tlog \w+ \{\n\t\tinclude http\.log\.error\n\t\tlevel DEBUG\n\t\}\n\}\n",
        CADDYFILE.read_text(encoding="utf-8"),
    ), "the global options block with the proxy error log must come first"


@pytest.mark.parametrize("host", HOSTS_WITHOUT_UPSTREAM)
def test_host_without_an_upstream_answers_the_stub(host: str) -> None:
    assert re.search(r"^\timport not_deployed$", _site(host), re.MULTILINE)


@pytest.mark.parametrize("host", DEPLOYED_HOSTS)
def test_deployed_host_keeps_the_plain_proxy_error(host: str) -> None:
    assert "not_deployed" not in _site(host)


def test_apex_redirects_to_the_prod_ui_host() -> None:
    # The chain ends in the stub of ui.*, not in a 502, and needs no change at the prod rollout.
    assert re.search(r"^\tredir https://ui\.\{\$APP_DOMAIN\}\{uri\} permanent$", _site(""), re.M)


def _store_pin(service: str) -> re.Match[str] | None:
    return re.search(
        rf"^    image: {service}:{STORE_MAJORS[service]}\.[\w.-]+@sha256:([0-9a-f]{{64}})$",
        _service_block(STAGING_COMPOSE, service),
        re.MULTILINE,
    )


@pytest.mark.parametrize("service", STORE_MAJORS)
def test_staging_store_is_pinned_by_digest(service: str) -> None:
    assert _store_pin(service) is not None, (
        f"{service} must be pinned as {service}:{STORE_MAJORS[service]}.<version>@sha256:<digest>"
    )


def test_staging_stores_have_distinct_digests() -> None:
    # A digest pasted under another image still looks like a pin. That a digest belongs to its
    # tag needs the registry: docker buildx imagetools inspect <image>:<tag> (docs/CICD.md §3).
    pins = [_store_pin(service) for service in STORE_MAJORS]

    assert all(pin is not None for pin in pins)
    assert len({pin.group(1) for pin in pins if pin is not None}) == len(STORE_MAJORS)
