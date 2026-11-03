"""GitLab configuration on the staging host: preflight, retention and failure handling (#335)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.deploy_host_fakes import (
    A_ID,
    CI_SECRETS,
    ENV_FILES,
    IMAGE_A,
    IMAGE_B,
    IMAGE_BAD,
    IMAGE_C,
    IMAGE_EXPLICIT,
    Host,
    _bundle,
    _keyring,
    _mode,
    _read_dotenv,
    _read_env_file,
    _run_ci_gitlab_pipeline,
)
from tests.deploy_host_fakes import host as _host_fixture

host = _host_fixture


def _gitlab_enabled_settings() -> dict[str, str]:
    return {
        "GITLAB_CREDENTIAL_KEYS": _keyring(),
        "GITLAB_ACTIVE_KEY_ID": "current",
        "GITLAB_ALLOWED_ORIGINS": '["https://gitlab.com"]',
        "GITLAB_WEBHOOK_INTAKE_ENABLED": "true",
    }


@pytest.mark.parametrize("kind", ["current", "legacy"])
def test_host_gitlab_preflight_rejects_candidate_before_any_live_mutation(
    host: Host, kind: str
) -> None:
    assert (
        host.deploy(IMAGE_A, bundle=_bundle(CI_SECRETS), env={"DEPLOY_RUN": "101-1"}).returncode
        == 0
    )
    (host.app_dir / "api.env").chmod(0o640)
    before = host.files()
    modes = {path.name: _mode(path) for path in host.app_dir.iterdir()}
    calls_before = len(host.calls())
    result = host.deploy(
        IMAGE_B,
        bundle=_bundle({**CI_SECRETS, "GITLAB_WEBHOOK_INTAKE_ENABLED": "true"}),
        env={
            "DEPLOY_RUN": "102-1",
            "STUB_GITLAB_STDERR": "PROBE_SECRET_CANARY",
            "STUB_GITLAB_IMAGE_KIND": kind,
        },
    )
    assert result.returncode != 0
    assert "configuration_invalid" in result.stderr
    assert "PROBE_SECRET_CANARY" not in result.stdout + result.stderr
    assert host.files() == before
    assert {path.name: _mode(path) for path in host.app_dir.iterdir()} == modes
    assert not any(
        call.startswith("compose ") or call.startswith("rm ")
        for call in host.calls()[calls_before:]
    )
    assert list(host.tmp.iterdir()) == []


def test_host_gitlab_deploy_accepts_legacy_parsers_without_new_helper(host: Host) -> None:
    result = host.deploy(
        IMAGE_A,
        bundle=_bundle(_gitlab_enabled_settings()),
        env={"STUB_GITLAB_IMAGE_KIND": "legacy"},
    )
    assert result.returncode == 0, result.stderr
    assert _read_env_file(host.app_dir / "gitlab.env")["GITLAB_ACTIVE_KEY_ID"] == "current"
    assert (host.log.parent / "up-image").read_text().strip() == A_ID


@pytest.mark.parametrize("mode", ["manual", "auto"])
def test_host_gitlab_rollback_refuses_invalid_retained_configuration_without_mutation(
    host: Host, mode: str
) -> None:
    assert host.deploy(IMAGE_A, bundle=_bundle(_gitlab_enabled_settings())).returncode == 0
    assert host.deploy(IMAGE_B).returncode == 0
    path = host.app_dir / "gitlab.env"
    path.write_text("GITLAB_CREDENTIAL_KEYS='CONFIG_CANARY'\n")
    path.chmod(0o640)
    before = host.files()
    modes = {file.name: _mode(file) for file in host.app_dir.iterdir()}
    calls_before = len(host.calls())
    result = host.run(
        "rollback.sh", env={"ROLLBACK_MODE": mode, "STUB_GITLAB_STDERR": "PROBE_SECRET_CANARY"}
    )
    assert result.returncode != 0
    assert "configuration_invalid" in result.stderr
    assert "PROBE_SECRET_CANARY" not in result.stdout + result.stderr
    assert "CONFIG_CANARY" not in result.stdout + result.stderr
    assert host.files() == before
    assert {file.name: _mode(file) for file in host.app_dir.iterdir()} == modes
    assert not any(" up " in call or call.startswith("rm ") for call in host.calls()[calls_before:])
    assert list(host.tmp.iterdir()) == []


@pytest.mark.parametrize("kind", ["current", "legacy"])
def test_host_gitlab_deploy_accepts_supported_target(host: Host, kind: str) -> None:
    assert host.deploy(IMAGE_A, bundle=_bundle(CI_SECRETS)).returncode == 0
    settings = _gitlab_enabled_settings()
    settings["GITLAB_CREDENTIAL_KEYS"] = _keyring(indent=2)
    result = host.deploy(IMAGE_B, bundle=_bundle(settings), env={"STUB_GITLAB_IMAGE_KIND": kind})
    assert result.returncode == 0, result.stderr
    assert (
        _read_env_file(host.app_dir / "gitlab.env")["GITLAB_CREDENTIAL_KEYS"]
        == settings["GITLAB_CREDENTIAL_KEYS"]
    )
    assert list(host.tmp.iterdir()) == []


def test_host_gitlab_deploy_rejects_unsupported_target_without_live_changes(host: Host) -> None:
    assert host.deploy(IMAGE_A, bundle=_bundle(CI_SECRETS)).returncode == 0
    before = host.files()
    modes = {path.name: _mode(path) for path in host.app_dir.iterdir()}
    calls_before = len(host.calls())
    result = host.deploy(
        IMAGE_B,
        bundle=_bundle(_gitlab_enabled_settings()),
        env={"STUB_GITLAB_IMAGE_KIND": "unsupported"},
    )
    assert result.returncode != 0
    assert "configuration_invalid" in result.stderr
    assert host.files() == before
    assert {path.name: _mode(path) for path in host.app_dir.iterdir()} == modes
    assert not any(" up " in call or call.startswith("rm ") for call in host.calls()[calls_before:])
    assert list(host.tmp.iterdir()) == []


def test_host_gitlab_retained_files_are_validated_as_data_before_replacement(host: Host) -> None:
    assert host.deploy(IMAGE_A, bundle=_bundle(_gitlab_enabled_settings())).returncode == 0
    marker = host.log.parent / "must-not-execute"
    (host.app_dir / "gitlab.env").write_text(f"GITLAB_ACTIVE_KEY_ID=$(touch {marker})\n")
    (host.app_dir / "gitlab.env").chmod(0o640)
    before = host.files()
    modes = {path.name: _mode(path) for path in host.app_dir.iterdir()}
    result = host.deploy(IMAGE_B)
    assert result.returncode != 0
    assert "configuration_invalid" in result.stderr
    assert not marker.exists()
    assert host.files() == before
    assert {path.name: _mode(path) for path in host.app_dir.iterdir()} == modes
    assert list(host.tmp.iterdir()) == []


def test_host_gitlab_deploy_pins_tag_probe_and_startup_to_one_image(host: Host) -> None:
    tag = "ghcr.io/test/api:mutable"
    result = host.deploy(
        tag,
        bundle=_bundle(_gitlab_enabled_settings()),
        env={
            "STUB_SELECTED_REF": tag,
            "STUB_MOVE_TAG_AT_GITLAB": "true",
            "STUB_REPODIGESTS": "ghcr.io/unrelated/api@sha256:" + "d" * 64 + "\n" + IMAGE_A,
        },
    )
    assert result.returncode == 0, result.stderr
    probes = [
        json.loads(line)
        for line in (host.log.parent / "gitlab-probes.jsonl").read_text().splitlines()
    ]
    assert probes[-1]["image"] == A_ID
    assert (host.log.parent / "tag-moved").exists()
    assert (host.log.parent / "up-image").read_text().strip() == A_ID
    assert _read_dotenv(host.env_file)["IMAGE"] == IMAGE_A
    assert _read_dotenv(host.app_dir / ".deploy-state")["current_image"] == IMAGE_A
    assert host.calls().count(f"pull {tag}") == 1
    assert any(" up --pull missing " in call for call in host.calls())


@pytest.mark.parametrize("mode", ["manual", "auto"])
@pytest.mark.parametrize("kind", ["current", "legacy"])
def test_host_gitlab_rollback_retains_config_and_uses_the_admitted_image(
    host: Host, mode: str, kind: str
) -> None:
    assert host.deploy(IMAGE_A, bundle=_bundle(_gitlab_enabled_settings())).returncode == 0
    assert host.deploy(IMAGE_B).returncode == 0
    before_config = {name: (host.app_dir / name).read_bytes() for name in ENV_FILES}
    result = host.run("rollback.sh", env={"ROLLBACK_MODE": mode, "STUB_GITLAB_IMAGE_KIND": kind})
    assert result.returncode == 0, result.stderr
    probes = [
        json.loads(line)
        for line in (host.log.parent / "gitlab-probes.jsonl").read_text().splitlines()
    ]
    assert probes[-1]["image"] == A_ID
    assert (host.log.parent / "probe-image").read_text().strip() == A_ID
    assert (host.log.parent / "up-image").read_text().strip() == A_ID
    assert {name: (host.app_dir / name).read_bytes() for name in ENV_FILES} == before_config
    assert list(host.tmp.iterdir()) == []


@pytest.mark.parametrize("mode", ["manual", "auto"])
def test_host_gitlab_rollback_rejects_unsupported_target_without_live_changes(
    host: Host, mode: str
) -> None:
    assert host.deploy(IMAGE_A, bundle=_bundle(_gitlab_enabled_settings())).returncode == 0
    assert host.deploy(IMAGE_B).returncode == 0
    before = host.files()
    modes = {path.name: _mode(path) for path in host.app_dir.iterdir()}
    calls_before = len(host.calls())
    result = host.run(
        "rollback.sh", env={"ROLLBACK_MODE": mode, "STUB_GITLAB_IMAGE_KIND": "unsupported"}
    )
    assert result.returncode != 0
    assert "configuration_invalid" in result.stderr
    assert host.files() == before
    assert {path.name: _mode(path) for path in host.app_dir.iterdir()} == modes
    assert not any(" up " in call or call.startswith("rm ") for call in host.calls()[calls_before:])
    assert list(host.tmp.iterdir()) == []


@pytest.mark.parametrize("mode", ["manual", "auto"])
@pytest.mark.parametrize("kind", ["current", "legacy"])
def test_host_gitlab_rollback_rejects_enabled_intake_without_runtime(
    host: Host, mode: str, kind: str
) -> None:
    assert host.deploy(IMAGE_A, bundle=_bundle(_gitlab_enabled_settings())).returncode == 0
    assert host.deploy(IMAGE_B).returncode == 0
    # An otherwise generated retained configuration has intake enabled in api.env,
    # but no worker runtime in gitlab.env. Both target parser generations must refuse it.
    (host.app_dir / "gitlab.env").write_text("")
    (host.app_dir / "gitlab.env").chmod(0o640)
    (host.app_dir / "api.env").chmod(0o640)
    before = host.files()
    modes = {path.name: _mode(path) for path in host.app_dir.iterdir()}
    calls_before = len(host.calls())
    result = host.run("rollback.sh", env={"ROLLBACK_MODE": mode, "STUB_GITLAB_IMAGE_KIND": kind})
    assert result.returncode != 0
    assert "configuration_invalid" in result.stderr
    assert host.files() == before
    assert {path.name: _mode(path) for path in host.app_dir.iterdir()} == modes
    assert not any(" up " in call or call.startswith("rm ") for call in host.calls()[calls_before:])
    assert list(host.tmp.iterdir()) == []


def test_host_gitlab_redeploy_disable_and_explicit_removal_preserve_bundle_semantics(
    host: Host,
) -> None:
    settings = _gitlab_enabled_settings()
    assert host.deploy(IMAGE_A, bundle=_bundle(settings)).returncode == 0
    keyring = (host.app_dir / "gitlab.env").read_bytes()
    assert host.deploy(IMAGE_B).returncode == 0
    assert (host.app_dir / "gitlab.env").read_bytes() == keyring
    assert _read_env_file(host.app_dir / "api.env")["GITLAB_WEBHOOK_INTAKE_ENABLED"] == "true"
    settings["GITLAB_WEBHOOK_INTAKE_ENABLED"] = "false"
    assert host.deploy(IMAGE_C, bundle=_bundle(settings)).returncode == 0
    assert (host.app_dir / "gitlab.env").read_bytes() == keyring
    assert _read_env_file(host.app_dir / "api.env")["GITLAB_WEBHOOK_INTAKE_ENABLED"] == "false"
    assert host.deploy(IMAGE_A, bundle="").returncode == 0
    for name in ENV_FILES:
        assert (host.app_dir / name).read_bytes() == b""
        assert _mode(host.app_dir / name) == 0o600


def test_host_gitlab_failed_stack_start_retains_current_configuration_after_auto_rollback(
    host: Host,
) -> None:
    settings = _gitlab_enabled_settings()
    assert host.deploy(IMAGE_A, bundle=_bundle(settings)).returncode == 0
    assert host.deploy(IMAGE_B).returncode == 0
    before_config = {name: (host.app_dir / name).read_bytes() for name in ENV_FILES}
    result = host.deploy(IMAGE_BAD, env={"STUB_FAILING_IMAGE": IMAGE_BAD})
    assert result.returncode != 0
    assert f"rolled back to {IMAGE_B}" in result.stdout
    assert {name: (host.app_dir / name).read_bytes() for name in ENV_FILES} == before_config
    assert list(host.tmp.iterdir()) == []


IO_FAILURE_WRAPPER = """\
#!/usr/bin/env bash
command_name="$(basename "$0")"
case "$STUB_IO_FAILURE:$command_name" in
  temporary-directory:mktemp)
    if [[ "$*" == *gitlab-validation.* ]]; then
      printf '%s\\n' "$STUB_OUTSIDE_DIR"
      exit 1
    fi
    ;;
  temporary-file:mktemp)
    if [[ "$*" == *gitlab-validation.* && "$*" != *"-d "* ]]; then
      printf '%s\\n' "$STUB_OUTSIDE_DIR/nonexistent/candidate"
      exit 1
    fi
    ;;
  retained-read:cat)
    if [[ "${1:-}" == */gitlab.env ]]; then exit 1; fi
    ;;
  permission:chmod)
    if [[ "$*" == *gitlab-validation.* ]]; then exit 1; fi
    ;;
  rename:mv)
    destination="${@: -1}"
    if [[ "${destination}" == */gitlab-validation.*/gitlab.env ]]; then exit 1; fi
    ;;
  filter:tr|encode:base64)
    cat >/dev/null
    exit 1
    ;;
esac
exec "__REAL_COMMAND__" "$@"
"""


def _inject_io_failure(host: Host, failure: str) -> dict[str, str]:
    command = {
        "temporary-directory": "mktemp",
        "temporary-file": "mktemp",
        "write": "printf",
        "preflight-write": "printf",
        "retained-read": "cat",
        "permission": "chmod",
        "rename": "mv",
        "filter": "tr",
        "encode": "base64",
    }[failure]
    outside = host.tmp.parent / "outside-private-temporary-area"
    outside.mkdir()
    environment = {"STUB_IO_FAILURE": failure, "STUB_OUTSIDE_DIR": str(outside)}
    if command == "printf":
        bash_environment = host.tmp.parent / "fail-write.bash"
        bash_environment.write_text(
            "printf() {\n"
            "  if [[ $STUB_IO_FAILURE == write && $1 == \"%s='%s'\\n\" ]] || "
            "[[ $STUB_IO_FAILURE == preflight-write && "
            "$1 == 'GITLAB_DEPLOYMENT_BUNDLE=%s\\n' ]]; then\n"
            "    return 1\n"
            "  fi\n"
            '  builtin printf "$@"\n'
            "}\n"
        )
        environment["BASH_ENV"] = str(bash_environment)
    else:
        real_command = shutil.which(command)
        assert real_command is not None
        wrapper = host.bin_dir / command
        wrapper.write_text(IO_FAILURE_WRAPPER.replace("__REAL_COMMAND__", real_command))
        wrapper.chmod(0o755)
    return environment


@pytest.mark.parametrize("operation", ["deploy", "manual-rollback", "auto-rollback"])
@pytest.mark.parametrize(
    "failure",
    [
        "temporary-directory",
        "temporary-file",
        "write",
        "preflight-write",
        "retained-read",
        "permission",
        "rename",
        "filter",
        "encode",
    ],
)
def test_host_refuses_gitlab_io_failures_before_validation_or_live_changes(
    host: Host, operation: str, failure: str
) -> None:
    assert host.deploy(IMAGE_A, bundle=_bundle(_gitlab_enabled_settings())).returncode == 0
    assert host.deploy(IMAGE_B).returncode == 0
    before = host.files()
    modes = {path.name: _mode(path) for path in host.app_dir.iterdir()}
    calls_before = len(host.calls())
    environment = _inject_io_failure(host, failure)
    # Supply infrastructure settings independently of GitLab transport failure injection.
    environment.update(
        {
            "POSTGRES_PASSWORD": "db",
            "RABBITMQ_PASSWORD": "broker",
            "REDIS_PASSWORD": "cache",
            "DEPLOY_MODE": "edge",
            "EDGE_ALIAS": "api-staging",
            "POSTGRES_USER": "app",
            "POSTGRES_DB": "app",
            "RABBITMQ_USER": "app",
        }
    )
    if operation == "deploy":
        result = host.deploy(IMAGE_EXPLICIT, env=environment)
    else:
        result = host.run(
            "rollback.sh",
            IMAGE_A,
            env={**environment, "ROLLBACK_MODE": operation.removesuffix("-rollback")},
        )
    assert result.returncode != 0
    assert host.files() == before
    assert {path.name: _mode(path) for path in host.app_dir.iterdir()} == modes
    assert not any(
        call == "run" or call.startswith("run ") or " up " in call or call.startswith("rm ")
        for call in host.calls()[calls_before:]
    )
    outside = Path(environment["STUB_OUTSIDE_DIR"])
    assert outside.is_dir()
    assert list(outside.iterdir()) == []
    assert list(host.tmp.iterdir()) == []


@pytest.mark.parametrize("operation", ["deploy", "manual-rollback", "auto-rollback"])
def test_host_rejects_unavailable_docker_config_directory_without_live_changes(
    host: Host, operation: str
) -> None:
    # Missing TMPDIR refuses Docker auth setup before any Docker command can run.
    assert host.deploy(IMAGE_A, bundle=_bundle(_gitlab_enabled_settings())).returncode == 0
    assert host.deploy(IMAGE_B).returncode == 0
    before = host.files()
    modes = {path.name: _mode(path) for path in host.app_dir.iterdir()}
    calls_before = len(host.calls())
    environment = {"TMPDIR": str(host.tmp / "missing")}
    if operation == "deploy":
        result = host.deploy(IMAGE_EXPLICIT, env=environment)
    else:
        result = host.run(
            "rollback.sh",
            IMAGE_A,
            env={**environment, "ROLLBACK_MODE": operation.removesuffix("-rollback")},
        )
    assert result.returncode != 0
    assert host.files() == before
    assert {path.name: _mode(path) for path in host.app_dir.iterdir()} == modes
    assert host.calls()[calls_before:] == []
    assert list(host.tmp.iterdir()) == []


def test_host_stages_recipient_files_on_destination_filesystem(host: Host) -> None:
    real_mv = shutil.which("mv")
    assert real_mv is not None
    wrapper = host.bin_dir / "mv"
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        'destination="${@: -1}"\n'
        'if [[ "${destination%/*}" == "$APP_DIR" && "${destination##*/}" == *.env ]]; then\n'
        '  printf "%s\\n" "${@: -2:1}" >> "$STUB_STAGE_SOURCES"\n'
        "fi\n"
        f'exec "{real_mv}" "$@"\n'
    )
    wrapper.chmod(0o755)
    sources = host.tmp.parent / "stage-sources"
    result = host.deploy(
        IMAGE_A,
        bundle=_bundle(_gitlab_enabled_settings()),
        env={"STUB_STAGE_SOURCES": str(sources)},
    )
    assert result.returncode == 0, result.stderr
    paths = [Path(value) for value in sources.read_text().splitlines()]
    assert len(paths) == len(ENV_FILES)
    assert {path.name for path in paths} == set(ENV_FILES)
    assert all(
        path.parent.parent == host.app_dir and path.parent.name.startswith(".stage.")
        for path in paths
    )
    assert not list(host.app_dir.glob(".stage.*"))
    assert list(host.tmp.iterdir()) == []


@pytest.mark.parametrize(
    "failure",
    [
        "temporary-directory",
        "temporary-file",
        "write",
        "preflight-write",
        "permission",
        "rename",
        "filter",
    ],
)
def test_gitlab_validation_helper_fails_in_conditional_context(host: Host, failure: str) -> None:
    environment = _inject_io_failure(host, failure)
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$ENV_HELPER"; '
            'if ! validate_gitlab_deployment "$TARGET_IMAGE" "$APPLICATION_BUNDLE"; '
            "then exit 23; fi",
        ],
        env={
            "PATH": f"{host.bin_dir}{os.pathsep}{os.environ['PATH']}",
            "ENV_HELPER": str(host.app_dir / "env-file.sh"),
            "TARGET_IMAGE": IMAGE_A,
            "APPLICATION_BUNDLE": _bundle(_gitlab_enabled_settings()),
            "TMPDIR": str(host.tmp),
            "STUB_LOG": str(host.log),
            **environment,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 23
    assert not host.calls()
    assert list(host.tmp.iterdir()) == []
    outside = Path(environment["STUB_OUTSIDE_DIR"])
    assert outside.is_dir()
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("kind", ["current", "legacy"])
@pytest.mark.parametrize("variant", ["default-off", "key-newline", "multiline-newline", "partial"])
def test_ci_and_host_validate_identical_post_generator_values(
    tmp_path: Path, host: Host, kind: str, variant: str
) -> None:
    complete = _gitlab_enabled_settings()
    multiline = _keyring(indent=2)
    settings, expected, valid = {
        "default-off": ({}, {}, True),
        "key-newline": ({**complete, "GITLAB_ACTIVE_KEY_ID": "current\n"}, complete, True),
        "multiline-newline": (
            {**complete, "GITLAB_CREDENTIAL_KEYS": multiline + "\n"},
            {**complete, "GITLAB_CREDENTIAL_KEYS": multiline},
            True,
        ),
        "partial": (
            {"GITLAB_WEBHOOK_INTAKE_ENABLED": "true"},
            {"GITLAB_WEBHOOK_INTAKE_ENABLED": "true"},
            False,
        ),
    }[variant]
    ci_directory = tmp_path / "ci"
    ci_directory.mkdir()
    result, mutations, calls, temporary = _run_ci_gitlab_pipeline(
        ci_directory, {**CI_SECRETS, **settings}
    )
    assert (result.returncode == 0) is valid
    assert bool(mutations) is valid
    assert calls[-1]["gitlab_values"] == expected
    expected_names = [
        "GITLAB_CREDENTIAL_KEYS",
        "GITLAB_ACTIVE_KEY_ID",
        "GITLAB_ALLOWED_ORIGINS",
        "GITLAB_PRIVATE_ORIGINS",
        "GITLAB_WEBHOOK_INTAKE_ENABLED",
    ]
    assert calls[-1]["allowed_names"] == expected_names
    host_result = host.deploy(
        IMAGE_A,
        bundle=_bundle({**CI_SECRETS, **settings}),
        env={"STUB_GITLAB_IMAGE_KIND": kind},
    )
    assert (host_result.returncode == 0) is valid
    probes = [
        json.loads(line)
        for line in (host.log.parent / "gitlab-probes.jsonl").read_text().splitlines()
    ]
    assert probes[-1]["gitlab_values"] == expected
    assert probes[-1]["allowed_names"] == expected_names
    assert list(temporary.iterdir()) == []
    assert list(host.tmp.iterdir()) == []


@pytest.mark.parametrize("operation", ["deploy", "manual", "auto"])
@pytest.mark.parametrize("filename", ["gitlab.env", "api.env"])
@pytest.mark.parametrize("failure", ["unquoted", "missing-newline", "read"])
def test_retained_role_files_report_only_static_basename_and_category(
    host: Host, operation: str, filename: str, failure: str
) -> None:
    assert host.deploy(IMAGE_A, bundle=_bundle(CI_SECRETS)).returncode == 0
    assert host.deploy(IMAGE_B).returncode == 0
    key = "GITLAB_ACTIVE_KEY_ID" if filename == "gitlab.env" else "GITHUB_CLIENT_ID"
    contents = {
        "unquoted": f"{key}=CONFIG_SECRET_CANARY\n",
        "missing-newline": f"{key}='CONFIG_SECRET_CANARY'",
        "read": "",
    }
    (host.app_dir / filename).write_text(contents[failure])
    (host.app_dir / filename).chmod(0o640)
    environment = {}
    if failure == "read":
        real_cat = shutil.which("cat")
        assert real_cat is not None
        wrapper = host.bin_dir / "cat"
        wrapper.write_text(
            "#!/usr/bin/env bash\n"
            'if [[ "${1:-}" == */"$STUB_UNREADABLE_FILE" ]]; then\n'
            '  echo "IO_SECRET_CANARY $1" >&2; exit 1\n'
            "fi\n"
            f'exec "{real_cat}" "$@"\n'
        )
        wrapper.chmod(0o755)
        environment["STUB_UNREADABLE_FILE"] = filename
    before = host.files()
    modes = {path.name: _mode(path) for path in host.app_dir.iterdir()}
    calls_before = len(host.calls())
    if operation == "deploy":
        result = host.deploy(IMAGE_EXPLICIT, env=environment)
    else:
        result = host.run("rollback.sh", env={**environment, "ROLLBACK_MODE": operation})
    category = "read_failed" if failure == "read" else "format_invalid"
    assert f"GitLab configuration_invalid ({filename}: {category})" in result.stderr
    assert result.returncode != 0
    assert "SECRET_CANARY" not in result.stdout + result.stderr
    assert str(host.app_dir) not in result.stdout + result.stderr
    assert host.files() == before
    assert {path.name: _mode(path) for path in host.app_dir.iterdir()} == modes
    assert not any(
        call == "run" or call.startswith("run ") or " up " in call or call.startswith("rm ")
        for call in host.calls()[calls_before:]
    )
    assert list(host.tmp.iterdir()) == []


@pytest.mark.parametrize("operation", ["deploy", "manual", "auto"])
@pytest.mark.parametrize("kind", ["current", "legacy"])
def test_host_validates_exact_retained_values_without_second_normalization(
    host: Host, operation: str, kind: str
) -> None:
    assert host.deploy(IMAGE_A, bundle=_bundle(_gitlab_enabled_settings())).returncode == 0
    assert host.deploy(IMAGE_B).returncode == 0
    path = host.app_dir / "gitlab.env"
    content = path.read_text()
    assert "GITLAB_ACTIVE_KEY_ID='current'\n" in content
    path.write_text(
        content.replace("GITLAB_ACTIVE_KEY_ID='current'\n", "GITLAB_ACTIVE_KEY_ID='current\n'\n")
    )
    path.chmod(0o640)
    before = host.files()
    modes = {file.name: _mode(file) for file in host.app_dir.iterdir()}
    calls_before = len(host.calls())
    environment = {"STUB_GITLAB_IMAGE_KIND": kind}
    if operation == "deploy":
        result = host.deploy(IMAGE_EXPLICIT, env=environment)
    else:
        result = host.run("rollback.sh", env={**environment, "ROLLBACK_MODE": operation})
    assert result.returncode != 0
    assert "configuration_invalid" in result.stderr
    assert host.files() == before
    assert {file.name: _mode(file) for file in host.app_dir.iterdir()} == modes
    assert not any(" up " in call or call.startswith("rm ") for call in host.calls()[calls_before:])
    assert list(host.tmp.iterdir()) == []


REGISTRY_CAPTURE_PROBE = """\
from __future__ import annotations
import json
import os
import stat
import sys
from pathlib import Path
mode = os.fstat(2).st_mode
record = {
    'command': sys.argv[1],
    'regular': stat.S_ISREG(mode),
    'mode': stat.S_IMODE(mode),
}
if sys.argv[1] == 'login':
    record['token_stdin_correct'] = (
        sys.stdin.read() == os.environ['STUB_EXPECTED_REGISTRY_TOKEN'] + '\\n'
    )
with Path(os.environ['STUB_REGISTRY_CAPTURE_LOG']).open('a') as log:
    log.write(json.dumps(record) + '\\n')
"""


def _registry_failure_with_safety_checks(
    host: Host, operation: str, stage: str, message: str
) -> subprocess.CompletedProcess[str]:
    assert host.deploy(IMAGE_A, bundle=_bundle(CI_SECRETS)).returncode == 0
    assert host.deploy(IMAGE_B).returncode == 0
    helper = host.log.parent / "registry-capture-probe.py"
    helper.write_text(REGISTRY_CAPTURE_PROBE)
    capture_log = host.log.parent / "registry-capture.jsonl"
    shared_config = host.log.parent / "shared-docker-config"
    shared_config.mkdir(mode=0o700)
    error = f"REGISTRY_STDERR_CANARY https://user:URL_SECRET_CANARY@registry.test/ {message}"
    environment = {
        "GHCR_TOKEN": "REGISTRY_TOKEN_CANARY",
        "STUB_EXPECTED_REGISTRY_TOKEN": "REGISTRY_TOKEN_CANARY",
        "STUB_REGISTRY_HELPER": str(helper),
        "STUB_REGISTRY_CAPTURE_LOG": str(capture_log),
        "DOCKER_CONFIG": str(shared_config),
    }
    target = IMAGE_EXPLICIT if operation == "deploy" else IMAGE_A
    if stage == "login":
        environment["STUB_LOGIN_FAILURE"] = error
    else:
        environment.update({"STUB_PULL_FAILURE": target, "STUB_UNSAFE_ERROR": error})
    before = host.files()
    modes = {path.name: _mode(path) for path in host.app_dir.iterdir()}
    calls_before = len(host.calls())
    if operation == "deploy":
        result = host.deploy(target, env=environment)
    else:
        result = host.run("rollback.sh", env={**environment, "ROLLBACK_MODE": operation})
    assert result.returncode != 0
    for canary in ("REGISTRY_STDERR_CANARY", "URL_SECRET_CANARY", "REGISTRY_TOKEN_CANARY"):
        assert canary not in result.stdout + result.stderr + "\n".join(host.calls())
    captures = [json.loads(line) for line in capture_log.read_text().splitlines()]
    assert captures and all(record["regular"] and record["mode"] == 0o600 for record in captures)
    assert captures[0]["token_stdin_correct"] is True
    assert host.files() == before
    assert {path.name: _mode(path) for path in host.app_dir.iterdir()} == modes
    assert not any(
        call.startswith("run ") or " up " in call or call.startswith("rm ")
        for call in host.calls()[calls_before:]
    )
    assert list(shared_config.iterdir()) == []
    assert list(host.tmp.iterdir()) == []
    return result


@pytest.mark.parametrize("operation", ["deploy", "manual", "auto"])
@pytest.mark.parametrize(
    ("stage", "message", "expected_diagnostic"),
    [
        (
            "login",
            "unauthorized: authentication required",
            "registry login failed (authentication_denied:",
        ),
        ("login", "dial tcp: i/o timeout", "registry login failed (network_unavailable:"),
        (
            "pull",
            "denied: requested access to the resource is denied",
            "target image pull failed (authentication_denied:",
        ),
        (
            "pull",
            "manifest unknown: requested manifest was not found",
            "target image pull failed (manifest_missing:",
        ),
        ("pull", "no space left on device", "target image pull failed (insufficient_storage:"),
        ("pull", "TLS handshake timeout", "target image pull failed (network_unavailable:"),
    ],
    ids=["auth-login", "network-login", "auth-pull", "manifest", "disk", "network-pull"],
)
def test_known_registry_failures_keep_static_categories_and_stdin_credentials(
    host: Host, operation: str, stage: str, message: str, expected_diagnostic: str
) -> None:
    result = _registry_failure_with_safety_checks(host, operation, stage, message)

    assert expected_diagnostic in result.stderr


@pytest.mark.parametrize(
    ("operation", "expected_prefix"),
    [
        ("deploy", "deployment refused:"),
        ("manual", "rollback refused:"),
        ("auto", "rollback refused:"),
    ],
)
@pytest.mark.parametrize(
    ("stage", "expected_diagnostic"),
    [("login", "registry login failed"), ("pull", "target image pull failed")],
)
def test_unknown_registry_failures_keep_plain_static_diagnostic_and_stdin_credentials(
    host: Host, operation: str, expected_prefix: str, stage: str, expected_diagnostic: str
) -> None:
    result = _registry_failure_with_safety_checks(
        host, operation, stage, "unrecognized registry response"
    )

    assert f"{expected_prefix} {expected_diagnostic}" in result.stderr.splitlines()


def _inject_registry_capture_failure(host: Host, failure: str) -> dict[str, str]:
    environment = {"STUB_CAPTURE_FAILURE": failure}
    if failure == "write":
        bash_environment = host.log.parent / "fail-registry-write.bash"
        bash_environment.write_text(
            "printf() {\n"
            '  if [[ $1 == "" ]]; then echo CAPTURE_IO_CANARY >&2; return 1; fi\n'
            '  builtin printf "$@"\n'
            "}\n"
        )
        environment["BASH_ENV"] = str(bash_environment)
    else:
        command = {"temporary-file": "mktemp", "permission": "chmod", "read": "cat"}[failure]
        real_command = shutil.which(command)
        assert real_command is not None
        wrapper = host.bin_dir / command
        wrapper.write_text(
            "#!/usr/bin/env bash\n"
            'if [[ "$*" == *registry-error.* ]]; then\n'
            "  echo CAPTURE_IO_CANARY >&2; exit 1\n"
            "fi\n"
            f'exec "{real_command}" "$@"\n'
        )
        wrapper.chmod(0o755)
    return environment


@pytest.mark.parametrize("operation", ["deploy", "manual", "auto"])
@pytest.mark.parametrize("failure", ["temporary-file", "permission", "write", "read"])
def test_registry_capture_io_failure_refuses_before_docker_or_live_changes(
    host: Host, operation: str, failure: str
) -> None:
    assert host.deploy(IMAGE_A, bundle=_bundle(CI_SECRETS)).returncode == 0
    assert host.deploy(IMAGE_B).returncode == 0
    shared_config = host.log.parent / "shared-docker-config"
    shared_config.mkdir(mode=0o700)
    environment = {
        **_inject_registry_capture_failure(host, failure),
        "GHCR_TOKEN": "REGISTRY_TOKEN_CANARY",
        "DOCKER_CONFIG": str(shared_config),
    }
    before = host.files()
    modes = {path.name: _mode(path) for path in host.app_dir.iterdir()}
    calls_before = len(host.calls())
    if operation == "deploy":
        result = host.deploy(IMAGE_EXPLICIT, env=environment)
    else:
        result = host.run("rollback.sh", env={**environment, "ROLLBACK_MODE": operation})
    assert result.returncode != 0
    assert "registry login failed" in result.stderr
    assert "CANARY" not in result.stdout + result.stderr
    assert host.files() == before
    assert {path.name: _mode(path) for path in host.app_dir.iterdir()} == modes
    assert not any(
        call.startswith(("login ", "pull ", "run ", "rm ")) or " up " in call
        for call in host.calls()[calls_before:]
    )
    assert list(shared_config.iterdir()) == []
    assert list(host.tmp.iterdir()) == []
