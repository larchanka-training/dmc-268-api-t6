"""Deployment contracts for the staging broker, cache and application secrets (#35)."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_DIR = REPO_ROOT / "deploy" / "compose"


def _read(*parts: str) -> str:
    return REPO_ROOT.joinpath(*parts).read_text(encoding="utf-8")


def _service_block(compose: str, name: str) -> str:
    match = re.search(rf"^  {name}:\n((?:    .*\n|\n)*)", compose, re.MULTILINE)
    assert match is not None, f"service {name} is missing"
    return match.group(1)


def _bash_array(script: str, name: str) -> list[str]:
    match = re.search(rf"^\s*{name}=\(([^)]*)\)", script, re.MULTILINE)
    assert match is not None, f"array {name} is missing"
    return match.group(1).split()


def test_staging_stores_publish_no_host_ports() -> None:
    compose = _read("deploy", "compose", "staging.yml")

    for service in ("postgres", "rabbitmq", "redis"):
        assert "ports:" not in _service_block(compose, service)
    # Overrides may publish the API, never a store.
    for override in COMPOSE_DIR.glob("staging.*.yml"):
        text = override.read_text(encoding="utf-8")
        for service in ("postgres", "rabbitmq", "redis"):
            assert f"\n  {service}:" not in text, f"{override.name} overrides {service}"


def test_rabbitmq_keeps_its_node_name_across_recreates() -> None:
    rabbitmq = _service_block(_read("deploy", "compose", "staging.yml"), "rabbitmq")

    assert "image: rabbitmq:4-management-alpine" in rabbitmq
    assert "hostname: rabbitmq" in rabbitmq
    assert "rabbitmq-data:/var/lib/rabbitmq" in rabbitmq


def test_redis_is_a_password_protected_cache_without_persistence() -> None:
    redis = _service_block(_read("deploy", "compose", "staging.yml"), "redis")

    assert "image: redis:8-alpine" in redis
    assert "requirepass" in redis
    assert "--requirepass" not in redis
    assert "volumes:" not in redis


def test_application_secrets_reach_the_api_only_through_env_files() -> None:
    api = _service_block(_read("deploy", "compose", "staging.yml"), "api")

    assert "- app.env" in api
    assert "- api.env" in api
    assert "GITHUB_" not in api
    assert "AUTH_JWT_PRIVATE_KEY" not in api


def test_ci_bundles_exactly_the_secrets_the_host_accepts() -> None:
    env_file = _read("deploy", "scripts", "env-file.sh")
    workflow = _read(".github", "workflows", "ci-cd.yml")

    host_keys = set(_bash_array(env_file, "APP_ENV_KEYS")) | set(
        _bash_array(env_file, "API_ENV_KEYS")
    )
    ci_keys = set(_bash_array(workflow, "names"))

    assert ci_keys == host_keys
    for key in ci_keys:
        assert re.search(rf"^\s+{key}: \$\{{\{{ secrets\.", workflow, re.MULTILINE)
    assert "APP_SECRETS_B64" in workflow
    assert "::add-mask::" in workflow


def test_rollback_keeps_host_generated_credentials() -> None:
    rollback = _read("deploy", "scripts", "rollback.sh")

    for key in ("RABBITMQ_USER", "RABBITMQ_PASSWORD", "REDIS_PASSWORD"):
        assert f"read_compose_env_var {key}" in rollback


def test_push_image_waits_for_the_required_python_check() -> None:
    workflow = _read(".github", "workflows", "ci-cd.yml")
    push_image = workflow[workflow.index("  push-image:") : workflow.index("  deploy-staging:")]

    assert "- python-lint-type-test" in push_image


def test_ui_host_routes_api_paths_to_the_api() -> None:
    caddyfile = _read("deploy", "edge", "Caddyfile")
    ui_block = caddyfile[caddyfile.index("staging-ui.{$APP_DOMAIN} {") :]
    ui_block = ui_block[: ui_block.index("\n}\n")]

    assert ui_block.index("handle /api/*") < ui_block.index("reverse_proxy ui-staging:8080")
    assert "reverse_proxy api-staging:8000" in ui_block
