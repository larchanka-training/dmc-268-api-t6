"""Host deploys and rollbacks of staging: store passwords, app secrets and refusals (#35)."""

from __future__ import annotations

import json
import re
import textwrap

import pytest

from tests.deploy_host_fakes import (
    A_ID,
    B_ID,
    CI_SECRETS,
    ENV_FILES,
    EXPLICIT_ID,
    FAKE_PEM,
    IMAGE_A,
    IMAGE_B,
    IMAGE_BAD,
    IMAGE_EXPLICIT,
    REPO_ROOT,
    ROLLBACK_WORKFLOW,
    Host,
    _bundle,
    _mode,
    _read_dotenv,
    _read_env_file,
    _run_script,
    _service_block,
    _workflow_step,
)
from tests.deploy_host_fakes import host as _host_fixture

host = _host_fixture


@pytest.mark.parametrize("compatible", [False, True])
def test_manual_ports_refreshes_old_rollback_before_execution(host: Host, compatible: bool) -> None:
    assert host.deploy(IMAGE_A, bundle="").returncode == 0
    assert host.deploy(IMAGE_B).returncode == 0
    (host.app_dir / "rollback.sh").write_text("#!/usr/bin/env bash\necho old-unguarded-rollback\n")
    (host.app_dir / "rollback.sh").chmod(0o600)
    (host.app_dir / "check-rollback-revision.py").unlink()
    upload = _workflow_step("Upload rollback scripts", ROLLBACK_WORKFLOW)
    assert "        if: steps.target.outputs.deploy_mode == 'ports'" in upload.splitlines()
    source = re.search(r'^\s+source: "([^"]+)"$', upload, re.MULTILINE)
    assert source is not None
    assert set(source.group(1).split(",")) == {
        "deploy/scripts/rollback.sh",
        "deploy/scripts/env-file.sh",
        "deploy/scripts/check-rollback-revision.py",
    }
    assert "          strip_components: 2" in upload
    for relative in source.group(1).split(","):
        path = REPO_ROOT / relative
        (host.app_dir / path.name).write_bytes(path.read_bytes())
    rollback = _workflow_step("Rollback", ROLLBACK_WORKFLOW)
    script = re.search(r"^          script: \|\n((?:            .*\n|\n)*)", rollback, re.MULTILINE)
    assert script is not None
    (host.app_dir / "manual-step.sh").write_text(
        "#!/usr/bin/env bash\n" + textwrap.dedent(script.group(1))
    )
    (host.app_dir / "manual-step.sh").chmod(0o755)
    before = host.files()
    calls_before = len(host.calls())

    result = host.run(
        "manual-step.sh",
        env={
            "IMAGE": "",
            "DEPLOY_MODE": "ports",
            "STUB_PROBE_STATUS": "0" if compatible else "1",
            "STUB_PROBE_OUTPUT": (
                "rollback revision check: compatible"
                if compatible
                else "rollback revision check: refused: database revision is unknown"
            ),
        },
    )

    assert "old-unguarded-rollback" not in result.stdout
    assert any(" run --rm --no-deps " in call for call in host.calls()[calls_before:])
    if compatible:
        assert result.returncode == 0, result.stderr
        assert f"rolled back to {IMAGE_A}" in result.stdout
        assert _read_dotenv(host.app_dir / ".deploy-state")["current_image"] == IMAGE_A
    else:
        assert result.returncode != 0
        assert "database revision is unknown" in result.stderr
        assert "rolled back" not in result.stdout
        assert host.files() == before
        assert not any(" up " in call for call in host.calls()[calls_before:])


def test_manual_rollback_failure_keeps_followup_and_promotion_success_gated() -> None:
    for name in ("Rollback", "Read deployed image", "Parse deployed image", "Health check"):
        step = _workflow_step(name, ROLLBACK_WORKFLOW)
        assert "continue-on-error" not in step
        assert re.search(r"^        if:", step, re.MULTILINE) is None
    workflow = ROLLBACK_WORKFLOW.read_text()
    assert "    if: github.ref == 'refs/heads/main'" in workflow
    assert "    needs: rollback" in workflow.split("  promote-staging:", 1)[1]
    assert "format('rollback-ignored-{0}', github.run_id)" in workflow
    assert "  cancel-in-progress: false\n  queue: max\n" in workflow


# --- Host: generated credentials, rollbacks and refusals ----------------------------------------


def test_first_deploy_generates_store_passwords_once(host: Host) -> None:
    assert host.deploy(IMAGE_A, bundle="").returncode == 0
    first = _read_dotenv(host.env_file)
    assert host.deploy(IMAGE_B, bundle="").returncode == 0
    second = _read_dotenv(host.env_file)

    for name in ("POSTGRES_PASSWORD", "RABBITMQ_PASSWORD", "REDIS_PASSWORD"):
        assert re.fullmatch(r"[0-9a-f]{48}", first[name]), name
        assert second[name] == first[name], name
    assert first["RABBITMQ_USER"] == "app"
    assert second["IMAGE"] == IMAGE_B
    assert list(host.tmp.iterdir()) == []  # the per-run DOCKER_CONFIG is removed


def test_rollbacks_keep_store_passwords_and_app_secrets(host: Host) -> None:
    bundle = _bundle(CI_SECRETS)
    assert host.deploy(IMAGE_A, bundle=bundle).returncode == 0
    assert host.deploy(IMAGE_B, bundle=bundle).returncode == 0
    deployed = _read_dotenv(host.env_file)
    secrets = {name: (host.app_dir / name).read_bytes() for name in ENV_FILES}

    manual = host.run("rollback.sh")
    after_manual = _read_dotenv(host.env_file)
    secrets_after_manual = {name: (host.app_dir / name).read_bytes() for name in ENV_FILES}
    # The failing deploy brings rotated secrets, one of them no longer set. They stay after the
    # automatic rollback: secrets are not tied to an image (docs/SECRETS.md §3).
    rotated = {
        **CI_SECRETS,
        "GITHUB_APP_ID": "2",
        "GITHUB_CLIENT_ID": "client",
        "AUTH_JWT_PUBLIC_KEY": "",
        "LLM_API_KEYS": "sk-rotated",
        "GITHUB_APP_BOT_LOGIN": "rotated[bot]",
    }
    failed = host.deploy(
        IMAGE_BAD,
        bundle=_bundle(rotated),
        env={"STUB_FAILING_IMAGE": IMAGE_BAD},
    )
    after_auto = _read_dotenv(host.env_file)

    assert manual.returncode == 0, manual.stderr
    assert after_manual["IMAGE"] == IMAGE_A
    assert failed.returncode != 0
    assert f"rolled back to {IMAGE_A}" in failed.stdout
    assert after_auto["IMAGE"] == IMAGE_A
    for name in ("POSTGRES_PASSWORD", "RABBITMQ_USER", "RABBITMQ_PASSWORD", "REDIS_PASSWORD"):
        assert after_manual[name] == deployed[name], name
        assert after_auto[name] == deployed[name], name
    assert all(secrets[name] for name in ENV_FILES if name != "gitlab.env")
    assert secrets_after_manual == secrets
    for file_name, (_, keys) in ENV_FILES.items():
        assert _read_env_file(host.app_dir / file_name) == {
            name: rotated[name] for name in keys if rotated.get(name)
        }, file_name


@pytest.mark.parametrize("name", ["POSTGRES_PASSWORD", "RABBITMQ_PASSWORD", "REDIS_PASSWORD"])
def test_rollback_refuses_without_a_store_password(host: Host, name: str) -> None:
    assert host.deploy(IMAGE_A, bundle="").returncode == 0
    assert host.deploy(IMAGE_B, bundle="").returncode == 0
    env = host.env_file.read_text(encoding="utf-8")
    host.env_file.write_text(
        "".join(line for line in env.splitlines(True) if not line.startswith(f"{name}=")),
        encoding="utf-8",
    )
    before = host.files()
    calls_before = len(host.calls())

    result = host.run("rollback.sh")

    assert result.returncode != 0
    assert f"{name} is required" in result.stderr
    assert host.files() == before
    assert not any(call.startswith("compose") for call in host.calls()[calls_before:])


def test_rollback_recreates_missing_app_secret_files(host: Host) -> None:
    assert host.deploy(IMAGE_A, bundle="").returncode == 0
    assert host.deploy(IMAGE_B, bundle="").returncode == 0
    for name in ENV_FILES:
        (host.app_dir / name).unlink()

    result = host.run("rollback.sh")

    assert result.returncode == 0, result.stderr
    for name in ENV_FILES:
        assert _mode(host.app_dir / name) == 0o600


@pytest.mark.parametrize("mode", ["manual", "auto"])
@pytest.mark.parametrize("explicit", [False, True])
def test_moved_tag_cannot_change_admitted_runtime_or_promotion_identity(
    host: Host, mode: str, explicit: bool
) -> None:
    selected = "ghcr.io/test/api:requested" if explicit else "ghcr.io/test/api:previous"
    assert host.deploy(selected, bundle="").returncode == 0
    previous = (host.app_dir / ".deploy-state").read_bytes()
    assert host.deploy(IMAGE_B).returncode == 0
    current = (host.app_dir / ".deploy-state").read_bytes()
    calls_before = len(host.calls())

    result = host.run(
        "rollback.sh",
        *([selected] if explicit else []),
        env={
            "ROLLBACK_MODE": mode,
            "STUB_SELECTED_REF": selected,
            "STUB_MOVE_TAG": "true",
            "STUB_REPODIGESTS": "ghcr.io/unrelated/api@sha256:" + "d" * 64 + "\n" + IMAGE_A,
        },
    )

    assert result.returncode == 0, result.stderr
    assert (host.log.parent / "tag-moved").exists()
    assert (host.log.parent / "probe-image").read_text().strip() == A_ID
    assert (host.log.parent / "up-image").read_text().strip() == A_ID
    assert _read_dotenv(host.env_file)["IMAGE"] == IMAGE_A
    assert _read_dotenv(host.app_dir / ".deploy-state")["current_image"] == IMAGE_A
    assert (host.app_dir / ".deploy-state.previous").read_bytes() == (
        current if mode == "manual" else previous
    )
    calls = host.calls()[calls_before:]
    assert calls.count(f"pull {selected if explicit else IMAGE_A}") == 1
    assert any(" up --pull missing " in call for call in calls)
    for role in ("api", "worker", "webhook-worker", "bootstrap"):
        assert "    image: ${IMAGE:?IMAGE is required}" in _service_block(role)
    if mode == "manual":
        promotion = _run_script(
            _workflow_step("Promote rolled-back image to staging", ROLLBACK_WORKFLOW)
        )
        promotion = promotion.replace("${{ secrets.GITHUB_TOKEN }}", "synthetic-token")
        promotion = promotion.replace("${{ github.actor }}", "synthetic-actor")
        step = host.app_dir / "promote-step.sh"
        step.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + promotion)
        step.chmod(0o755)
        promotion_before = len(host.calls())
        promoted = host.run(
            "promote-step.sh",
            env={
                "DEPLOYED_IMAGE": _read_dotenv(host.app_dir / ".deploy-state")["current_image"],
                "STAGING_TAG": "ghcr.io/test/api:staging",
                "PREVIOUS_TAG": "ghcr.io/test/api:staging-previous",
                "REPOSITORY": "test/api",
                "STUB_SELECTED_REF": selected,
            },
        )
        assert promoted.returncode == 0, promoted.stderr
        promotion_calls = host.calls()[promotion_before:]
        assert f"pull {IMAGE_A}" in promotion_calls
        assert f"tag {IMAGE_A} ghcr.io/test/api:staging" in promotion_calls
        assert f"pull {selected}" not in promotion_calls


@pytest.mark.parametrize("mode", ["manual", "auto"])
@pytest.mark.parametrize(
    ("status", "verdict", "reason"),
    [
        (0, "rollback revision check: compatible", None),
        (
            1,
            "rollback revision check: refused: database revision is unknown",
            "database revision is unknown",
        ),
        (1, "rollback revision check: compatible", "target revision probe failed"),
        (0, "rollback revision check: refused: database revision is unknown", "unexpected result"),
        (
            1,
            "rollback revision check: refused: database revision is unknown\nSECRET-CANARY",
            "target revision probe failed",
        ),
        (0, "rollback revision check: compatible\n", "unexpected result"),
        (0, "", "unexpected result"),
    ],
)
def test_probe_stdout_protocol_ignores_secret_bearing_compose_stderr(
    host: Host, mode: str, status: int, verdict: str, reason: str | None
) -> None:
    assert host.deploy(IMAGE_A, bundle="").returncode == 0
    assert host.deploy(IMAGE_B).returncode == 0
    before = host.files()
    modes = {p.name: _mode(p) for p in host.app_dir.iterdir()}
    calls_before = len(host.calls())
    result = host.run(
        "rollback.sh",
        env={
            "ROLLBACK_MODE": mode,
            "STUB_PROBE_STATUS": str(status),
            "STUB_PROBE_OUTPUT": verdict,
            "STUB_PROBE_STDERR": (
                "Container project-bootstrap-run Creating\nSECRET-CANARY\nPEM-CANARY"
            ),
        },
    )
    output = result.stdout + result.stderr
    assert "SECRET-CANARY" not in output and "PEM-CANARY" not in output
    assert "Creating" not in output
    if reason is None:
        assert result.returncode == 0, result.stderr
        assert f"rolled back to {IMAGE_A}" in result.stdout
    else:
        assert result.returncode != 0
        assert reason in result.stderr
        assert host.files() == before
        assert {p.name: _mode(p) for p in host.app_dir.iterdir()} == modes
        assert not any(
            set(c.split()) & {"up", "rm", "down", "exec"} for c in host.calls()[calls_before:]
        )
    assert list(host.tmp.iterdir()) == []


@pytest.mark.parametrize("mode", ["manual", "auto"])
@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("failure", ["pull", "probe", "noisy-probe", "false-success"])
def test_rollback_refuses_before_any_persistent_or_stack_change(
    host: Host, mode: str, explicit: bool, failure: str
) -> None:
    assert host.deploy(IMAGE_A, bundle=_bundle(CI_SECRETS)).returncode == 0
    assert host.deploy(IMAGE_B).returncode == 0
    # Missing role files and unusual permissions must survive a refused probe too.
    (host.app_dir / "worker.env").unlink()
    (host.app_dir / "api.env").chmod(0o640)
    before = host.files()
    modes = {path.name: _mode(path) for path in host.app_dir.iterdir()}
    calls_before = len(host.calls())
    target = IMAGE_EXPLICIT if explicit else IMAGE_A
    secret = "SECRET-CANARY\n-----BEGIN PRIVATE KEY-----\nPEM-CANARY"
    environment = {"ROLLBACK_MODE": mode, "STUB_UNSAFE_ERROR": secret}
    if failure == "pull":
        environment["STUB_PULL_FAILURE"] = target
    else:
        environment["STUB_PROBE_STATUS"] = "0" if failure == "false-success" else "1"
        environment["STUB_PROBE_OUTPUT"] = (
            "rollback revision check: refused: database revision is unknown"
            if failure == "probe"
            else secret
        )

    result = host.run("rollback.sh", *([target] if explicit else []), env=environment)

    assert result.returncode != 0
    assert host.files() == before
    assert {path.name: _mode(path) for path in host.app_dir.iterdir()} == modes
    calls = host.calls()[calls_before:]
    assert f"pull {target}" in calls
    assert not any(set(call.split()) & {"up", "rm", "down", "exec"} for call in calls)
    assert calls[-1] == "logout ghcr.io"
    assert list(host.tmp.iterdir()) == []
    output = result.stdout + result.stderr
    assert "rolled back" not in output
    assert "Select a compatible image" in output
    assert "SECRET-CANARY" not in output and "PEM-CANARY" not in output
    if failure == "probe":
        assert "database revision is unknown" in output


@pytest.mark.parametrize("mode", ["manual", "auto"])
@pytest.mark.parametrize(
    "failure",
    [
        "inspect",
        "short-id",
        "uppercase-id",
        "noisy-id",
        "multiple-ids",
        "extra-line",
        "empty-digests",
        "wrong-repository",
        "malformed-digests",
        "tagged-digest",
        "ambiguous-digests",
        "digest-error",
        "digest-mismatch",
        "local-id-mismatch",
    ],
)
def test_unusable_immutable_identity_refuses_without_file_or_stack_mutation(
    host: Host, mode: str, failure: str
) -> None:
    assert host.deploy(IMAGE_A, bundle="").returncode == 0
    assert host.deploy(IMAGE_B).returncode == 0
    (host.app_dir / "worker.env").unlink()
    (host.app_dir / "api.env").chmod(0o640)
    before = host.files()
    modes = {p.name: _mode(p) for p in host.app_dir.iterdir()}
    calls_before = len(host.calls())
    secret = "IDENTITY-SECRET-CANARY\nPEM-BODY-CANARY"
    selected = A_ID if failure == "local-id-mismatch" else "ghcr.io/test/api:movable"
    options = {
        "inspect": {"STUB_INSPECT_FAILURE": "id"},
        "short-id": {"STUB_ID_OUTPUT": "sha256:abc"},
        "uppercase-id": {"STUB_ID_OUTPUT": "sha256:" + "A" * 64},
        "noisy-id": {"STUB_ID_OUTPUT": secret + "\n" + A_ID},
        "multiple-ids": {"STUB_ID_OUTPUT": A_ID + "\n" + B_ID},
        "extra-line": {"STUB_ID_OUTPUT": A_ID + "\n"},
        "empty-digests": {"STUB_REPODIGESTS": ""},
        "wrong-repository": {"STUB_REPODIGESTS": "ghcr.io/unrelated/api@sha256:" + "a" * 64},
        "malformed-digests": {"STUB_REPODIGESTS": IMAGE_A + "\n" + secret},
        "tagged-digest": {"STUB_REPODIGESTS": "ghcr.io/test/api:tag@sha256:" + "a" * 64},
        "ambiguous-digests": {
            "STUB_REPODIGESTS": IMAGE_A + "\nghcr.io/test/api@sha256:" + "9" * 64
        },
        "digest-error": {"STUB_REPODIGEST_STATUS": "1", "STUB_REPODIGESTS": secret},
        "digest-mismatch": {"STUB_DIGEST_MISMATCH": "true"},
        "local-id-mismatch": {"STUB_ID_OUTPUT": B_ID},
    }[failure]
    result = host.run(
        "rollback.sh",
        selected,
        env={
            "ROLLBACK_MODE": mode,
            "STUB_SELECTED_REF": selected,
            "STUB_UNSAFE_ERROR": secret,
            **options,
        },
    )
    assert result.returncode != 0
    assert "rollback refused: target " in result.stderr
    assert host.files() == before
    assert {p.name: _mode(p) for p in host.app_dir.iterdir()} == modes
    calls = host.calls()[calls_before:]
    assert not any(" run " in call or " up " in call or " exec " in call for call in calls)
    assert calls[-1] == "logout ghcr.io" and list(host.tmp.iterdir()) == []
    assert "rolled back" not in result.stdout
    assert "IDENTITY-SECRET-CANARY" not in result.stdout + result.stderr
    assert "PEM-BODY-CANARY" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("selected", "release"),
    [
        (IMAGE_A, IMAGE_A),
        (A_ID, A_ID),
        ("ghcr.io/test/api:tag@sha256:" + "a" * 64, "ghcr.io/test/api:tag@sha256:" + "a" * 64),
        ("ghcr.io/test/api:" + "4" * 40, IMAGE_A),
        ("registry.test:5000/team/api:version", "registry.test:5000/team/api@sha256:" + "a" * 64),
    ],
)
def test_immutable_resolution_preserves_digest_refs_and_parses_registry_port(
    host: Host, selected: str, release: str
) -> None:
    assert host.deploy(IMAGE_A, bundle="").returncode == 0
    assert host.deploy(IMAGE_B).returncode == 0
    result = host.run("rollback.sh", selected, env={"STUB_REPODIGESTS": release})
    assert result.returncode == 0, result.stderr
    assert _read_dotenv(host.env_file)["IMAGE"] == release
    assert _read_dotenv(host.app_dir / ".deploy-state")["current_image"] == release
    assert (host.log.parent / "probe-image").read_text().strip() == A_ID
    assert (host.log.parent / "up-image").read_text().strip() == A_ID


@pytest.mark.parametrize("mode", ["manual", "auto"])
@pytest.mark.parametrize("explicit", [False, True])
def test_compatible_rollback_probes_target_before_changes_and_keeps_mode_state(
    host: Host, mode: str, explicit: bool
) -> None:
    assert host.deploy(IMAGE_A, bundle=_bundle(CI_SECRETS)).returncode == 0
    previous = (host.app_dir / ".deploy-state").read_bytes()
    assert host.deploy(IMAGE_B).returncode == 0
    current = (host.app_dir / ".deploy-state").read_bytes()
    before_env = host.env_file.read_bytes()
    (host.app_dir / "worker.env").unlink()
    secrets = {
        name: (host.app_dir / name).read_bytes() for name in ENV_FILES if name != "worker.env"
    }
    calls_before = len(host.calls())
    target = IMAGE_EXPLICIT if explicit else IMAGE_A

    result = host.run("rollback.sh", *([target] if explicit else []), env={"ROLLBACK_MODE": mode})

    assert result.returncode == 0, result.stderr
    calls = host.calls()[calls_before:]
    pull = calls.index(f"pull {target}")
    probe = next(i for i, call in enumerate(calls) if " run " in call)
    up = next(i for i, call in enumerate(calls) if " up " in call)
    assert pull < probe < up
    assert calls[probe].endswith("run --rm --no-deps -T bootstrap python -")
    assert (host.log.parent / "probe-source.py").read_bytes() == (
        REPO_ROOT / "deploy/scripts/check-rollback-revision.py"
    ).read_bytes()
    assert (host.log.parent / "probe-image").read_text().strip() == (
        EXPLICIT_ID if explicit else A_ID
    )
    assert (host.log.parent / "probe-dotenv").read_bytes() == before_env
    assert (host.log.parent / "probe-role").read_text() == "absent\n"
    assert (host.log.parent / "probe-config.yml").read_text() == (
        "services:\n"
        "  bootstrap:\n"
        "    image: ${IMAGE:?IMAGE is required}\n"
        "    environment:\n"
        "      DATABASE_URL: postgresql+psycopg://${POSTGRES_USER:-app}:"
        "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"
        "@postgres:5432/${POSTGRES_DB:-app}\n"
        '    restart: "no"\n'
        "networks:\n"
        "  default:\n"
        "    external: true\n"
        "    name: ${COMPOSE_PROJECT:?COMPOSE_PROJECT is required}_default\n"
    )
    assert _read_dotenv(host.env_file)["IMAGE"] == target
    assert (host.app_dir / ".deploy-state.previous").read_bytes() == (
        current if mode == "manual" else previous
    )
    state = _read_dotenv(host.app_dir / ".deploy-state")
    assert state["current_image"] == target and state["rolled_back"] == "true"
    assert {name: (host.app_dir / name).read_bytes() for name in secrets} == secrets
    assert (host.app_dir / "worker.env").read_bytes() == b""
    assert _mode(host.app_dir / "worker.env") == 0o600
    assert list(host.tmp.iterdir()) == []


@pytest.mark.parametrize("mode", ["manual", "auto"])
def test_successful_rollback_prints_only_sorted_running_container_keys(
    host: Host, mode: str
) -> None:
    assert host.deploy(IMAGE_A, bundle=_bundle(CI_SECRETS)).returncode == 0
    assert host.deploy(IMAGE_B).returncode == 0
    canary = "ENV-VALUE-CANARY$HOME\\backslash"
    shared = {
        "LC_CTYPE": "UTF-8",
        "Z_KEY": canary,
        "A_KEY": "BEGIN-SECRET\nPEM-LINE-CANARY\nEND-SECRET",
        "bad\nHOSTILE-KEY-CANARY": "value",
        "bad-key": "value",
        'quote"key': "value",
        "unicodeé": "value",
    }
    (host.log.parent / "container-environments.json").write_text(
        json.dumps(
            {
                "api": {**shared, "AUTH_JWT_PRIVATE_KEY": FAKE_PEM},
                "worker": {**shared, "GITHUB_APP_PRIVATE_KEY": FAKE_PEM, "lower_key": canary},
                "webhook-worker": {**shared, "GITHUB_APP_BOT_LOGIN": canary},
            }
        )
    )
    calls_before = len(host.calls())

    result = host.run("rollback.sh", env={"ROLLBACK_MODE": mode})

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        'api environment keys: ["AUTH_JWT_PRIVATE_KEY", "A_KEY", "LC_CTYPE", "Z_KEY"]',
        'worker environment keys: ["A_KEY", "GITHUB_APP_PRIVATE_KEY", "LC_CTYPE", "Z_KEY", '
        '"lower_key"]',
        'webhook-worker environment keys: ["A_KEY", "GITHUB_APP_BOT_LOGIN", "LC_CTYPE", "Z_KEY"]',
        f"rolled back to {IMAGE_A}",
    ]
    assert result.stderr == ""
    assert (host.log.parent / "diagnostic-services").read_text() == "api\nworker\nwebhook-worker\n"
    assert _read_dotenv(host.log.parent / "diagnostic-before-state")["current_image"] == IMAGE_A
    calls = host.calls()[calls_before:]
    up = next(i for i, call in enumerate(calls) if " up " in call)
    assert all(up < i for i, call in enumerate(calls) if " exec " in call)
    assert sum(" exec -T " in call for call in calls) == 3
    for value in (canary, "PEM-LINE-CANARY", "HOSTILE-KEY-CANARY", *FAKE_PEM.splitlines()):
        assert value not in result.stdout + result.stderr


@pytest.mark.parametrize("mode", ["manual", "auto"])
@pytest.mark.parametrize("service", ["api", "worker", "webhook-worker"])
@pytest.mark.parametrize("failure", ["exec", "noise", "invalid-list", "empty"])
def test_diagnostic_failure_is_secret_safe_and_records_the_healthy_release(
    host: Host, mode: str, service: str, failure: str
) -> None:
    assert host.deploy(IMAGE_A, bundle=_bundle(CI_SECRETS)).returncode == 0
    assert host.deploy(IMAGE_B).returncode == 0
    state = (host.app_dir / ".deploy-state").read_bytes()
    previous = (host.app_dir / ".deploy-state.previous").read_bytes()
    secret = "DOCKER-SECRET-CANARY\nPEM-BODY-CANARY"
    output = {
        "exec": secret,
        "noise": secret + '\n["A_KEY"]',
        "invalid-list": '["A_KEY", "bad\\nPEM-BODY-CANARY"]',
        "empty": "",
    }[failure]

    result = host.run(
        "rollback.sh",
        env={
            "ROLLBACK_MODE": mode,
            "STUB_EXEC_FAILURE_SERVICE": service,
            "STUB_EXEC_STATUS": "1" if failure == "exec" else "0",
            "STUB_EXEC_ERROR": output,
        },
    )

    assert result.returncode != 0
    assert _read_dotenv(host.app_dir / ".deploy-state")["current_image"] == IMAGE_A
    assert _read_dotenv(host.app_dir / ".deploy-state")["rolled_back"] == "true"
    assert (host.app_dir / ".deploy-state.previous").read_bytes() == (
        state if mode == "manual" else previous
    )
    assert (host.log.parent / "diagnostic-before-state").read_bytes() == (
        host.app_dir / ".deploy-state"
    ).read_bytes()
    assert "rolled back" not in result.stdout + result.stderr
    assert f"{service} environment key diagnostics failed" in result.stderr
    assert "rollback completion failed:" in result.stderr
    assert "healthy target release is recorded" in result.stderr
    assert "Inspect service health" in result.stderr
    assert "rollback refused:" not in result.stderr
    for canary in ("DOCKER-SECRET-CANARY", "PEM-BODY-CANARY"):
        assert canary not in result.stdout + result.stderr
    assert list(host.tmp.iterdir()) == []


@pytest.mark.parametrize("mode", ["manual", "auto"])
def test_failed_stack_start_does_not_run_diagnostics_or_record_success(
    host: Host, mode: str
) -> None:
    assert host.deploy(IMAGE_A, bundle="").returncode == 0
    assert host.deploy(IMAGE_B).returncode == 0
    state = (host.app_dir / ".deploy-state").read_bytes()
    previous = (host.app_dir / ".deploy-state.previous").read_bytes()
    calls_before = len(host.calls())

    result = host.run(
        "rollback.sh",
        env={"ROLLBACK_MODE": mode, "STUB_FAILING_IMAGE": IMAGE_A},
    )

    assert result.returncode != 0
    assert not any(" exec " in call for call in host.calls()[calls_before:])
    assert "environment keys" not in result.stdout
    assert "rolled back" not in result.stdout
    assert (host.app_dir / ".deploy-state").read_bytes() == state
    assert (host.app_dir / ".deploy-state.previous").read_bytes() == previous


def test_host_deployed_before_the_broker_keeps_its_postgres_password(host: Host) -> None:
    postgres_password = "0" * 48
    host.env_file.write_text(
        "IMAGE=ghcr.io/test/api@sha256:old\nPOSTGRES_USER=app\n"
        f"POSTGRES_PASSWORD={postgres_password}\nPOSTGRES_DB=app\n"
        "DEPLOY_MODE=edge\nEDGE_ALIAS=api-staging\n",
        encoding="utf-8",
    )
    host.add_volume("postgres-data")

    result = host.run("deploy.sh", IMAGE_A)

    assert result.returncode == 0, result.stderr
    env = _read_dotenv(host.env_file)
    assert env["POSTGRES_PASSWORD"] == postgres_password
    assert re.fullmatch(r"[0-9a-f]{48}", env["RABBITMQ_PASSWORD"])
    assert re.fullmatch(r"[0-9a-f]{48}", env["REDIS_PASSWORD"])
    for name in ENV_FILES:
        assert (host.app_dir / name).read_text(encoding="utf-8") == ""
        assert _mode(host.app_dir / name) == 0o600


def test_broker_volume_without_a_password_is_never_given_a_new_one(host: Host) -> None:
    assert host.deploy(IMAGE_A, bundle="").returncode == 0
    env = host.env_file.read_text(encoding="utf-8")
    host.env_file.write_text(
        "".join(line for line in env.splitlines(True) if not line.startswith("RABBITMQ_PASSWORD=")),
        encoding="utf-8",
    )
    host.add_volume("rabbitmq-data")
    before = host.files()

    result = host.deploy(IMAGE_B, bundle="")

    assert result.returncode != 0
    assert "refusing to generate a new password" in result.stderr
    assert host.files() == before


@pytest.mark.parametrize("mode", ["manual", "auto"])
def test_rollback_without_a_release_stops_the_stack_by_project_name(host: Host, mode: str) -> None:
    # A host whose .env predates the broker: loading compose.yml would fail on RABBITMQ_PASSWORD.
    host.env_file.write_text("DEPLOY_MODE=edge\nEDGE_ALIAS=api-staging\n", encoding="utf-8")

    result = host.run("rollback.sh", env={"ROLLBACK_MODE": mode})

    assert result.returncode == 0, result.stderr
    calls = host.calls()
    down = calls.index(f"compose -p {host.project} down --remove-orphans")
    started = next(i for i, call in enumerate(calls) if call.startswith("run -d --name"))
    assert down < started
    assert "--network-alias api-staging" in calls[started]
    assert not any(
        " exec " in call or " run --rm " in call for call in calls if call.startswith("compose ")
    )
    assert "environment keys" not in result.stdout


def test_rollback_does_not_start_the_bootstrap_next_to_a_running_stack(host: Host) -> None:
    host.env_file.write_text("DEPLOY_MODE=edge\nEDGE_ALIAS=api-staging\n", encoding="utf-8")

    result = host.run("rollback.sh", env={"STUB_DOWN_STATUS": "1"})

    assert result.returncode != 0
    assert "not starting the bootstrap container" in result.stderr
    assert not any(call.startswith("run ") for call in host.calls())
