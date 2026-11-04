"""The tracing values per stack (#308): the export endpoint and the environment name.

They travel like the worker replica count: the CI value wins, else the stack's .env, else the
default (today's staging values). Compose reads them from .env; an empty endpoint turns the
export off.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.deploy_host_fakes import (
    IMAGE_A,
    IMAGE_B,
    LEGACY_VALUES,
    Host,
    _ci_auto_rollback,
    _read_dotenv,
    _rendered_services,
    _write_compose_env_file,
    make_host,
)

TRACED_SERVICES = ("api", "worker", "webhook-worker")
STAGING_ENDPOINT = "http://tempo:4318"
STAGING_ATTRIBUTES = "deployment.environment.name=staging"
PROD_ATTRIBUTES = "deployment.environment.name=prod"


def _tracing_of(services: dict[str, dict[str, object]], name: str) -> tuple[object, object]:
    environment = services[name]["environment"]
    assert isinstance(environment, dict)
    return (
        environment["OTEL_EXPORTER_OTLP_ENDPOINT"],
        environment["OTEL_RESOURCE_ATTRIBUTES"],
    )


@pytest.mark.parametrize(
    ("dotenv", "shell", "endpoint", "attributes"),
    [
        pytest.param({}, {}, STAGING_ENDPOINT, STAGING_ATTRIBUTES, id="unset"),
        pytest.param(
            {"DEPLOY_ENVIRONMENT": "prod"}, {}, STAGING_ENDPOINT, PROD_ATTRIBUTES, id="env"
        ),
        pytest.param(
            {"DEPLOY_ENVIRONMENT": ""}, {}, STAGING_ENDPOINT, STAGING_ATTRIBUTES, id="env-empty"
        ),
        pytest.param(
            {}, {"DEPLOY_ENVIRONMENT": "prod"}, STAGING_ENDPOINT, PROD_ATTRIBUTES, id="env-shell"
        ),
        pytest.param({"OTEL_EXPORTER_OTLP_ENDPOINT": ""}, {}, "", STAGING_ATTRIBUTES, id="off"),
        pytest.param(
            {}, {"OTEL_EXPORTER_OTLP_ENDPOINT": ""}, "", STAGING_ATTRIBUTES, id="off-shell"
        ),
        pytest.param(
            {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://t:1"},
            {},
            "http://t:1",
            STAGING_ATTRIBUTES,
            id="other",
        ),
    ],
)
def test_compose_renders_the_tracing_values(
    tmp_path: Path, dotenv: dict[str, str], shell: dict[str, str], endpoint: str, attributes: str
) -> None:
    services = _rendered_services(tmp_path, dotenv=dotenv, shell=shell)

    for name in TRACED_SERVICES:
        assert _tracing_of(services, name) == (endpoint, attributes), name


@pytest.fixture
def host(tmp_path: Path) -> Host:
    return make_host(tmp_path)


def _up_tracing(host: Host) -> list[str]:
    """The endpoint and the environment name the last `compose up` was started with."""
    return (host.log.parent / "up-tracing").read_text(encoding="utf-8").splitlines()


def _deploy(host: Host, image: str, **env: str) -> subprocess.CompletedProcess[str]:
    return host.deploy(image, bundle="", env=env)


def _tracing_in_dotenv(host: Host) -> tuple[str, str]:
    dotenv = _read_dotenv(host.env_file)
    return dotenv["OTEL_EXPORTER_OTLP_ENDPOINT"], dotenv["DEPLOY_ENVIRONMENT"]


def test_fresh_host_without_the_variables_keeps_the_staging_values(host: Host) -> None:
    result = _deploy(host, IMAGE_A)

    assert result.returncode == 0, result.stderr
    assert _tracing_in_dotenv(host) == (STAGING_ENDPOINT, "staging")
    assert _up_tracing(host) == [STAGING_ENDPOINT, "staging"]


def test_deploy_applies_the_ci_tracing_values(host: Host) -> None:
    result = _deploy(host, IMAGE_A, DEPLOY_ENVIRONMENT="prod", OTEL_EXPORTER_OTLP_ENDPOINT="")

    assert result.returncode == 0, result.stderr
    assert _tracing_in_dotenv(host) == ("", "prod")
    assert _up_tracing(host) == ["", "prod"]


def test_ci_values_win_over_the_host_values(host: Host) -> None:
    assert (
        _deploy(host, IMAGE_A, DEPLOY_ENVIRONMENT="prod", OTEL_EXPORTER_OTLP_ENDPOINT="").returncode
        == 0
    )

    result = _deploy(
        host, IMAGE_B, DEPLOY_ENVIRONMENT="staging", OTEL_EXPORTER_OTLP_ENDPOINT="http://t:1"
    )

    assert result.returncode == 0, result.stderr
    assert _tracing_in_dotenv(host) == ("http://t:1", "staging")
    assert _up_tracing(host) == ["http://t:1", "staging"]


def test_manual_deploy_without_the_variables_keeps_the_host_values(host: Host) -> None:
    assert (
        _deploy(host, IMAGE_A, DEPLOY_ENVIRONMENT="prod", OTEL_EXPORTER_OTLP_ENDPOINT="").returncode
        == 0
    )

    result = _deploy(host, IMAGE_B)

    assert result.returncode == 0, result.stderr
    # An empty endpoint on the host is a decision (export off), not a missing value.
    assert _tracing_in_dotenv(host) == ("", "prod")
    assert _up_tracing(host) == ["", "prod"]


def test_host_from_before_the_variables_gets_the_staging_values(host: Host) -> None:
    assert _deploy(host, IMAGE_A).returncode == 0
    lines = host.env_file.read_text(encoding="utf-8").splitlines()
    host.env_file.write_text(
        "".join(
            f"{line}\n"
            for line in lines
            if not line.startswith(("OTEL_EXPORTER_OTLP_ENDPOINT=", "DEPLOY_ENVIRONMENT="))
        )
    )

    result = _deploy(host, IMAGE_B)

    assert result.returncode == 0, result.stderr
    assert _tracing_in_dotenv(host) == (STAGING_ENDPOINT, "staging")


@pytest.mark.parametrize(
    ("source", "value"),
    [
        *[("env", v) for v in ("dev", "production", "Prod", "STAGING", "prod ", "stag ing", "a=b")],
        *[("dotenv", v) for v in ("dev", "production", "Prod")],
    ],
)
def test_invalid_environment_name_is_refused_before_the_host_changes(
    host: Host, source: str, value: str
) -> None:
    assert _deploy(host, IMAGE_A, DEPLOY_ENVIRONMENT="prod").returncode == 0
    if source == "dotenv":
        text = host.env_file.read_text(encoding="utf-8")
        host.env_file.write_text(
            text.replace("DEPLOY_ENVIRONMENT=prod\n", f"DEPLOY_ENVIRONMENT={value}\n"),
            encoding="utf-8",
        )
        env: dict[str, str] = {}
    else:
        env = {"DEPLOY_ENVIRONMENT": value}
    before = host.files()
    calls_before = len(host.calls())

    result = _deploy(host, IMAGE_B, **env)

    assert result.returncode != 0
    assert f"DEPLOY_ENVIRONMENT must be staging or prod, got: {value}" in result.stderr
    assert host.files() == before
    # Only the EXIT trap's credential cleanup reaches docker.
    assert set(host.calls()[calls_before:]) <= {"logout ghcr.io"}


def test_env_file_writer_takes_optional_tracing_values(tmp_path: Path) -> None:
    # After the file: the ten values of the oldest callers, NO_PREVIOUS_RELEASE, the replica count.
    base = [*LEGACY_VALUES, "stop", "3"]
    legacy, current = tmp_path / "legacy.env", tmp_path / "current.env"

    _write_compose_env_file(legacy, *base)
    _write_compose_env_file(current, *base, "", "prod")

    # A caller that does not pass them leaves both lines out: Compose then uses the defaults.
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in legacy.read_text(encoding="utf-8")
    assert "DEPLOY_ENVIRONMENT" not in legacy.read_text(encoding="utf-8")
    written = current.read_text(encoding="utf-8")
    assert "OTEL_EXPORTER_OTLP_ENDPOINT=\n" in written
    assert "DEPLOY_ENVIRONMENT=prod\n" in written


@pytest.mark.parametrize("how", ["manual", "ci-auto", "failed-deploy"])
def test_rollback_keeps_the_tracing_values(host: Host, how: str) -> None:
    assert (
        _deploy(host, IMAGE_A, DEPLOY_ENVIRONMENT="prod", OTEL_EXPORTER_OTLP_ENDPOINT="").returncode
        == 0
    )
    env = {"DEPLOY_RUN": "102-1"}
    if how == "failed-deploy":
        env["STUB_FAILING_IMAGE"] = IMAGE_B
    assert _deploy(host, IMAGE_B, **env).returncode == (1 if how == "failed-deploy" else 0)

    if how == "manual":
        result = host.run("rollback.sh")
    elif how == "ci-auto":
        # The step's `envs:` carry no tracing variable.
        result = _ci_auto_rollback(host, "102-1")
    else:
        result = None

    if result is not None:
        assert result.returncode == 0, result.stderr
    assert _tracing_in_dotenv(host) == ("", "prod")
    assert _up_tracing(host) == ["", "prod"]


def test_rollback_ignores_inherited_tracing_variables(host: Host) -> None:
    assert (
        _deploy(host, IMAGE_A, DEPLOY_ENVIRONMENT="prod", OTEL_EXPORTER_OTLP_ENDPOINT="").returncode
        == 0
    )
    assert _deploy(host, IMAGE_B).returncode == 0

    result = host.run(
        "rollback.sh",
        env={"DEPLOY_ENVIRONMENT": "staging", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://t:1"},
    )

    assert result.returncode == 0, result.stderr
    assert _tracing_in_dotenv(host) == ("", "prod")
    assert _up_tracing(host) == ["", "prod"]


def test_rollback_refuses_an_invalid_environment_name_in_dotenv_before_the_host_changes(
    host: Host,
) -> None:
    assert _deploy(host, IMAGE_A).returncode == 0
    assert _deploy(host, IMAGE_B).returncode == 0
    text = host.env_file.read_text(encoding="utf-8")
    host.env_file.write_text(text.replace("DEPLOY_ENVIRONMENT=staging", "DEPLOY_ENVIRONMENT=dev"))
    before = host.files()
    calls_before = len(host.calls())

    result = host.run("rollback.sh")

    assert result.returncode != 0
    assert "DEPLOY_ENVIRONMENT must be staging or prod, got: dev" in result.stderr
    assert host.files() == before
    assert set(host.calls()[calls_before:]) <= {"logout ghcr.io"}


STAGING_LINE = "tracing: environment=staging, export to http://tempo:4318\n"
PROD_LINE = "tracing: environment=prod, export off\n"


def test_deploy_says_which_tracing_values_it_applies(host: Host) -> None:
    default = _deploy(host, IMAGE_A)
    prod = _deploy(host, IMAGE_B, DEPLOY_ENVIRONMENT="prod", OTEL_EXPORTER_OTLP_ENDPOINT="")

    assert STAGING_LINE in default.stdout
    assert PROD_LINE in prod.stdout
    assert prod.stdout.count("tracing:") == 1


@pytest.mark.parametrize("how", ["manual", "ci-auto"])
@pytest.mark.parametrize(
    ("settings", "line"),
    [
        pytest.param({}, STAGING_LINE, id="staging-default"),
        pytest.param(
            {"DEPLOY_ENVIRONMENT": "prod", "OTEL_EXPORTER_OTLP_ENDPOINT": ""}, PROD_LINE, id="prod"
        ),
    ],
)
def test_rollback_says_which_tracing_values_it_keeps(
    host: Host, how: str, settings: dict[str, str], line: str
) -> None:
    assert _deploy(host, IMAGE_A, **settings).returncode == 0
    assert _deploy(host, IMAGE_B, DEPLOY_RUN="102-1").returncode == 0

    result = host.run("rollback.sh") if how == "manual" else _ci_auto_rollback(host, "102-1")

    assert result.returncode == 0, result.stderr
    assert line in result.stdout
    assert result.stdout.count("tracing:") == 1
