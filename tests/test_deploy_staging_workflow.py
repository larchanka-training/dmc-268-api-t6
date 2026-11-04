"""Compose, workflow and edge contracts of the staging deploy (#35)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.deploy_host_fakes import (
    REPO_ROOT,
    ROLLBACK_WORKFLOW,
    STAGING_COMPOSE,
    WORKFLOW,
    _read,
    _run_script,
    _service_block,
    _workflow_step,
)

EDGE_GUARD = "        if: steps.target.outputs.deploy_mode == 'edge'"


def _steps(workflow_file: Path) -> list[tuple[str, str]]:
    """Every step of a workflow as (name, text), comment lines left out."""
    chunks = workflow_file.read_text(encoding="utf-8").split("\n      - name: ")[1:]
    steps = []
    for chunk in chunks:
        name, _, body = chunk.partition("\n")
        lines = [line for line in body.splitlines() if not line.lstrip().startswith("#")]
        steps.append((name, "\n".join(lines)))
    return steps


def test_staging_worker_uses_nonisolated_default_network_for_ecb_https() -> None:
    compose = STAGING_COMPOSE.read_text(encoding="utf-8")
    worker = compose.split("  worker:\n", 1)[1].split("\n  webhook-worker:", 1)[0]

    assert "network_mode: none" not in worker
    assert "networks:" not in worker  # Compose's project default network has outbound egress.
    assert "internal: true" not in compose


def test_deploy_step_forwards_the_bundle_to_the_host() -> None:
    step = _workflow_step("Deploy image")
    envs = re.search(r"^\s+envs: (\S+)$", step, re.MULTILINE)

    assert "APP_SECRETS_B64: ${{ steps.app_secrets.outputs.bundle }}" in step
    assert envs is not None
    assert "APP_SECRETS_B64" in envs.group(1).split(",")


@pytest.mark.parametrize("workflow", [WORKFLOW, ROLLBACK_WORKFLOW], ids=["ci", "manual-edge"])
def test_workflow_upload_delivers_host_supplied_revision_checker(workflow: Path) -> None:
    step = _workflow_step("Upload deploy files", workflow)
    source = re.search(r'^\s+source: "([^"]+)"$', step, re.MULTILINE)
    assert source is not None
    assert "deploy/scripts/rollback.sh" in source.group(1).split(",")
    assert "deploy/scripts/check-rollback-revision.py" in source.group(1).split(",")


# --- Compose, workflow and edge contracts -------------------------------------------------------


def test_staging_stores_publish_no_host_ports() -> None:
    for service in ("postgres", "rabbitmq", "redis"):
        assert "ports:" not in _service_block(service)
    # Overrides may publish the API, never a store.
    for override in STAGING_COMPOSE.parent.glob("staging.*.yml"):
        text = override.read_text(encoding="utf-8")
        for service in ("postgres", "rabbitmq", "redis"):
            assert f"\n  {service}:" not in text, f"{override.name} overrides {service}"


def test_rabbitmq_keeps_its_node_name_across_recreates() -> None:
    rabbitmq = _service_block("rabbitmq")

    assert re.search(r"image: rabbitmq:4\.[\d.]+-management-alpine@sha256:", rabbitmq)
    assert "hostname: rabbitmq" in rabbitmq
    assert "rabbitmq-data:/var/lib/rabbitmq" in rabbitmq


def test_redis_is_a_bounded_password_protected_cache() -> None:
    redis = _service_block("redis")

    assert re.search(r"image: redis:8\.[\d.]+-alpine@sha256:", redis)
    assert "requirepass %s" in redis
    assert "--requirepass" not in redis  # the password stays out of argv
    assert "maxmemory 128mb" in redis
    assert "maxmemory-policy allkeys-lru" in redis
    assert "volumes:" not in redis


def test_api_starts_without_the_broker_and_the_cache() -> None:
    api = _service_block("api")
    depends_on = api[api.index("    depends_on:\n") :]
    dependencies = re.findall(
        r"^      ([a-z-]+):$", depends_on[: depends_on.index("    restart:")], re.M
    )

    assert dependencies == ["bootstrap", "postgres"]


def test_api_gets_only_its_own_env_file() -> None:
    api = _service_block("api")
    env_files = api[api.index("    env_file:\n") : api.index("    environment:\n")]

    # The API reads no GitHub App credentials: app.env belongs to the workers.
    assert re.findall(r"^      - (\S+)$", env_files, re.MULTILINE) == [
        "api.env",
        "bitbucket.env",
    ]
    assert "GITHUB_" not in api
    assert "AUTH_JWT_PRIVATE_KEY" not in api


def _list_under(block: str, key: str) -> list[str]:
    """Items (``- x``) or keys (``x:``) directly under a top-level key of a service block."""
    match = re.search(rf"^    {key}:\n((?:      .*\n)*)", block, re.MULTILINE)
    assert match is not None, f"{key} is missing"
    return re.findall(r"^      (?:- )?([a-z][a-z.-]*):?$", match.group(1), re.MULTILINE)


@pytest.mark.parametrize(
    ("service", "module", "env_files", "dependencies"),
    [
        (
            "worker",
            "app.worker",
            ["app.env", "gitlab.env", "worker.env", "bitbucket.env"],
            ["bootstrap", "postgres", "rabbitmq"],
        ),
        # The projection creates Runs and publishes them to the review queue (#52).
        (
            "webhook-worker",
            "app.webhook_worker",
            ["app.env", "gitlab.env", "webhook-worker.env", "bitbucket.env"],
            ["bootstrap", "postgres", "rabbitmq"],
        ),
    ],
)
def test_workers_run_from_the_api_image_with_their_own_files_and_dependencies(
    service: str, module: str, env_files: list[str], dependencies: list[str]
) -> None:
    block = _service_block(service)

    assert "image: ${IMAGE:?IMAGE is required}" in block
    assert f'command: ["python", "-m", "{module}"]' in block
    assert _list_under(block, "env_file") == env_files
    assert _list_under(block, "depends_on") == dependencies
    assert "condition: service_completed_successfully" in block  # bootstrap: migrations first
    assert "DATABASE_URL: postgresql+psycopg://" in block
    assert ("RABBITMQ_URL: amqp://" in block) == ("rabbitmq" in dependencies)
    assert "restart: unless-stopped" in block
    assert "ports:" not in block


@pytest.mark.parametrize("service", ["worker", "webhook-worker"])
def test_worker_healthcheck_reads_its_heartbeat_not_the_http_port(service: str) -> None:
    block = _service_block(service)
    heartbeat = re.search(r"^      WORKER_HEARTBEAT_FILE: (\S+)$", block, re.MULTILINE)
    test = re.search(r"^      test:\n        - CMD-SHELL\n        - (.+)$", block, re.MULTILINE)

    assert heartbeat is not None and test is not None
    # The image HEALTHCHECK polls :8000/healthcheck, which no worker serves: it must be replaced.
    assert "8000" not in block and "urllib" not in block
    # The module file is the marker of an image that beats; WORKDIR /srv, app copied to ./app.
    module = REPO_ROOT / "app" / "common" / "infrastructure" / "heartbeat.py"
    assert module.is_file()
    assert test.group(1) == (
        "test ! -f /srv/app/common/infrastructure/heartbeat.py || exec python -m "
        f"app.common.infrastructure.heartbeat {heartbeat.group(1)} 30"
    )
    assert "COPY app ./app" in _read("Dockerfile") and "WORKDIR /srv" in _read("Dockerfile")


def test_workers_are_not_on_the_edge_network() -> None:
    edge = (STAGING_COMPOSE.parent / "staging.edge.yml").read_text(encoding="utf-8")
    ports = (STAGING_COMPOSE.parent / "staging.ports.yml").read_text(encoding="utf-8")

    for service in ("worker", "webhook-worker"):
        assert f"\n  {service}:" not in edge
        assert f"\n  {service}:" not in ports


def test_push_image_waits_for_the_required_python_check() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    push_image = workflow[workflow.index("  push-image:") : workflow.index("  deploy-staging:")]

    assert "- python-lint-type-test" in push_image


def test_ci_lints_the_openapi_contract_before_the_push() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    job = workflow[workflow.index("  openapi-lint:") : workflow.index("  docker-build:")]
    push_image = workflow[workflow.index("  push-image:") : workflow.index("  deploy-staging:")]

    assert "name: OpenAPI lint" in job
    # Pinned CLI, the same command as locally: the repo-root redocly.yaml and
    # .redocly.lint-ignore.yaml apply, no CLI flag overrides them.
    assert re.search(
        r"^        run: npx --yes @redocly/cli@2\.57\.0 lint contracts/openapi\.yaml$",
        job,
        re.MULTILINE,
    )
    assert "--extends" not in job
    # Redocly exits 0 on warnings; the config's strict ruleset turns every warning into an error.
    assert re.search(r"^extends:\n  - recommended-strict$", _read("redocly.yaml"), re.MULTILINE)
    assert "- openapi-lint" in push_image


def test_ci_validates_the_edge_caddyfile_before_the_push() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "\n  edge-validate:\n" in workflow
    next_job = workflow.index("  terraform-lint-security:")
    job = workflow[workflow.index("  edge-validate:") : next_job]
    push_image = workflow[workflow.index("  push-image:") : workflow.index("  deploy-staging:")]
    script = _run_script(_workflow_step("caddy validate"))

    assert "name: Edge Caddyfile validate" in job
    assert "      - name: caddy validate\n" in job
    # Fail closed: a red validation must fail the job and with it push-image.
    assert "continue-on-error" not in job
    assert "|| true" not in script
    # The image the VPS runs comes from deploy/edge/compose.yml: no second tag to keep in step.
    assert "image=\"$(sed -n 's/^    image: //p' deploy/edge/compose.yml)\"" in script.splitlines()
    assert not re.search(r"\bcaddy(?::\d|@sha256:)", workflow)
    # The same command as docs/CICD.md §8.2; unset, APP_DOMAIN leaves the apex site keyless.
    assert (
        "docker run --rm -e APP_DOMAIN=example.test "
        '-v "$PWD/deploy/edge:/etc/caddy:ro" "${image}" \\\n'
        "  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile"
    ) in script
    # The deploy runs caddy reload with this file: a broken one must stop the pipeline first.
    assert "- edge-validate" in push_image


def test_rollback_validates_the_edge_caddyfile_before_it_reaches_the_host() -> None:
    rollback = ROLLBACK_WORKFLOW.read_text(encoding="utf-8")
    step = _workflow_step("caddy validate", ROLLBACK_WORKFLOW)
    order = [
        rollback.index(f"      - name: {name}\n")
        for name in ("Checkout", "caddy validate", "Upload deploy files", "Upload edge proxy files")
    ]

    # Rollback uploads the Caddyfile of its own checkout and reloads Caddy with it: the CI check
    # runs on that revision before the first step that reaches the host, as in the edge mode only.
    assert _run_script(step) == _run_script(_workflow_step("caddy validate"))
    assert "continue-on-error" not in step
    assert "|| true" not in _run_script(step)
    assert order == sorted(order)
    assert not re.search(r"\bcaddy(?::\d|@sha256:)", rollback)
    # The check runs exactly when the Caddyfile reaches the host: every step that uploads the edge
    # files or reloads Caddy carries the same guard as the check.
    edge_steps = [
        (name, text)
        for name, text in _steps(ROLLBACK_WORKFLOW)
        if "deploy/edge" in text or "caddy reload" in text
    ]
    assert {"caddy validate", "Upload edge proxy files", "Prepare host"} <= {
        name for name, _ in edge_steps
    }
    for name, text in edge_steps:
        assert EDGE_GUARD in text.splitlines(), f"step {name!r} lacks the edge guard"


def test_ui_host_sends_api_paths_to_the_api_and_the_rest_to_the_ui() -> None:
    caddyfile = _read("deploy", "edge", "Caddyfile")
    ui_site = caddyfile[caddyfile.index("staging-ui.{$APP_DOMAIN} {") :]
    ui_site = ui_site[: ui_site.index("\n}\n")]
    api_route = re.search(r"handle /api/\* \{\n(.*?)\n\t\}", ui_site, re.DOTALL)
    fallback = re.search(r"handle \{\n(.*?)\n\t\}", ui_site, re.DOTALL)

    assert api_route is not None and fallback is not None
    assert api_route.group(1).strip() == "reverse_proxy api-staging:8000"
    assert fallback.group(1).strip() == "reverse_proxy ui-staging:8080"
    assert api_route.start() < fallback.start()


@pytest.mark.parametrize("compose_path", ["docker-compose.yml", "deploy/compose/staging.yml"])
def test_worker_stop_grace_exceeds_the_application_shutdown_deadline(compose_path: str) -> None:
    compose = _read(compose_path)
    match = re.search(r"(?ms)^  worker:\n(.*?)(?=^  [a-z][a-z-]*:\n|\Z)", compose)
    assert match is not None
    worker = match.group(1)
    assert re.search(r"^    stop_grace_period: 40s$", worker, re.MULTILINE)
    # SIGTERM must reach the Python worker, whose first-signal deadline includes Runner.close.
    assert '    command: ["python", "-m", "app.worker"]' in worker


# The probe runs as root with HOME=/var/lib/rabbitmq. On a fresh volume a bare ping can create a
# root-only .erlang.cookie before the server reads it; the server (uid 100) then exits 1 (eacces).
RABBITMQ_PROBE = "test -f /var/lib/rabbitmq/.erlang.cookie && rabbitmq-diagnostics -q ping"


@pytest.mark.parametrize("compose_path", ["docker-compose.yml", "deploy/compose/staging.yml"])
def test_rabbitmq_probe_waits_for_the_cookie_of_the_server(compose_path: str) -> None:
    compose = _read(compose_path)
    match = re.search(r"(?ms)^  rabbitmq:\n(.*?)(?=^  [a-z][a-z-]*:\n|\Z)", compose)
    assert match is not None
    assert f'      test: ["CMD-SHELL", "{RABBITMQ_PROBE}"]\n' in match.group(1)


def test_ci_rabbitmq_service_probe_waits_for_the_cookie_of_the_server() -> None:
    # Every job gets a fresh anonymous volume. --health-cmd is already the shell form.
    workflow = WORKFLOW.read_text(encoding="utf-8")
    services = re.findall(r"(?m)^      rabbitmq:\n((?:        .*\n)+)", workflow)
    assert services
    for service in services:
        assert f'          --health-cmd "{RABBITMQ_PROBE}"\n' in service
