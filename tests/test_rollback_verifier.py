"""Safety contracts at the opt-in verifier's CLI and report boundaries."""

from __future__ import annotations

import os
import runpy
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

VERIFIER = Path(__file__).resolve().parents[1] / "scripts/verify_rollback.py"


@pytest.mark.parametrize("collision", ["network", "volume", "container", "daemon-error"])
def test_verifier_refuses_unowned_resources_before_any_docker_mutation(
    tmp_path: Path, collision: str
) -> None:
    docker = tmp_path / "docker"
    log = tmp_path / "calls"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >> "$CALLS"\n'
        'case "$1" in\n'
        "  network|volume|ps)\n"
        '    [[ "$COLLISION" != daemon-error ]] || exit 1\n'
        '    [[ "$1" != "$COLLISION" ]] || echo existing-owned-by-someone-else\n'
        '    [[ "$1" != ps || "$COLLISION" != container ]] || echo existing-container\n'
        "    ;;\n"
        "  *) echo forbidden-mutation; exit 9 ;;\n"
        "esac\n"
    )
    docker.chmod(0o755)
    result = subprocess.run(
        [
            "uv",
            "run",
            "python",
            str(VERIFIER),
            "--docker",
            str(docker),
            "--project",
            "rollback110-0123456789abcdef",
            "--image-a",
            "prepared:a",
            "--expect-image-a",
            "sha256:" + "a" * 64,
            "--report",
            str(tmp_path / "report.json"),
        ],
        env={**os.environ, "CALLS": str(log), "COLLISION": collision},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "verification failed: resource isolation could not be established" in result.stderr
    assert all(
        line.startswith(("network ls ", "volume ls ", "ps "))
        for line in log.read_text().splitlines()
    )
    assert not (tmp_path / "report.json").exists()


def boundary(name: str) -> Callable[..., Any]:
    return cast(Callable[..., Any], runpy.run_path(str(VERIFIER))[name])


@pytest.mark.parametrize("changed", ["files", "containers", "revision", "sentinel"])
def test_refusal_evidence_rejects_each_mutation_even_with_nonzero_exit(changed: str) -> None:
    before = {"files": "hash-a", "containers": "id-b", "revision": "N", "sentinel": "intact"}
    after = {**before, changed: "changed"}
    result = subprocess.CompletedProcess(
        [],
        1,
        "",
        "rollback refused: rollback revision check: refused: database revision is unknown\n",
    )
    with pytest.raises(Exception, match="refusal invariants changed"):
        boundary("assert_refusal")(before, after, result, "database revision is unknown")


@pytest.mark.parametrize(
    ("code", "stdout", "stderr"),
    [
        (0, "", "database revision is unknown"),
        (1, "rolled back to A", "database revision is unknown"),
        (1, 'api environment keys: ["PATH"]', "database revision is unknown"),
        (1, "", "raw secret failure"),
    ],
)
def test_refusal_requires_failure_reason_and_no_success_diagnostics(
    code: int, stdout: str, stderr: str
) -> None:
    with pytest.raises(Exception, match="refusal outcome was unexpected"):
        boundary("assert_refusal")(
            {},
            {},
            subprocess.CompletedProcess([], code, stdout, stderr),
            "database revision is unknown",
        )


def test_key_evidence_checks_exact_roles_order_names_and_values() -> None:
    expected = {"api": ["AUTH_KEY", "PATH"], "worker": ["APP_KEY"], "webhook-worker": ["BOT_KEY"]}
    safe = (
        'api environment keys: ["AUTH_KEY", "PATH"]\n'
        'worker environment keys: ["APP_KEY"]\n'
        'webhook-worker environment keys: ["BOT_KEY"]\nrolled back to A\n'
    )
    assert boundary("validate_diagnostics")(safe, expected) == expected
    for hostile in (
        safe.replace('"AUTH_KEY", "PATH"', '"PATH", "AUTH_KEY"'),
        safe.replace('"BOT_KEY"', '"APP_KEY"'),
        safe.replace('"APP_KEY"', '"bad\\nSECRET-CANARY"'),
        safe + 'api environment keys: ["AUTH_KEY", "PATH"]\n',
    ):
        with pytest.raises(Exception, match="environment key evidence was unexpected"):
            boundary("validate_diagnostics")(hostile, expected)


def test_local_wrapper_checks_image_identity_and_forwards_real_probe_and_up(tmp_path: Path) -> None:
    real = tmp_path / "real-docker"
    calls = tmp_path / "calls"
    image = "sha256:" + "a" * 64
    real.write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$CALLS"\n'
        'if [[ "$1 $2" == "image inspect" ]]; then echo "$ACTUAL_ID"; fi\n'
        'if [[ "$1" == compose && " $* " == *" run "* ]]; then cat > "$STDIN_COPY"; fi\n'
    )
    real.chmod(0o755)
    wrapper = tmp_path / "docker"
    plugin = tmp_path / "real-compose"
    plugin.write_text("real plugin fixture")
    config = tmp_path / "private-config"
    boundary("write_docker_wrapper")(wrapper, str(real), [image], str(plugin))
    environment = {
        **os.environ,
        "CALLS": str(calls),
        "ACTUAL_ID": image,
        "STDIN_COPY": str(tmp_path / "stdin"),
        "DOCKER_CONFIG": str(config),
    }

    def execute(args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(wrapper), *args],
            env=environment,
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
        )

    assert execute(["pull", image]).returncode == 0
    assert calls.read_text().splitlines() == [f"image inspect --format {{{{.Id}}}} {image}"]
    environment["ACTUAL_ID"] = "sha256:" + "b" * 64
    assert execute(["pull", image]).returncode != 0
    assert execute(["pull", "registry.example/unrelated:a"]).returncode == 0
    assert execute(["compose", "-p", "isolated", "up", "-d", "--wait"]).returncode == 0
    assert (
        execute(
            [
                "compose",
                "-p",
                "isolated",
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "bootstrap",
                "python",
                "-",
            ],
            "real-checker-source",
        ).returncode
        == 0
    )
    assert calls.read_text().splitlines()[-3:] == [
        "pull registry.example/unrelated:a",
        "compose -p isolated up --pull never -d --wait",
        "compose -p isolated run --rm --no-deps -T bootstrap python -",
    ]
    assert (tmp_path / "stdin").read_text() == "real-checker-source"
    assert (config / "cli-plugins/docker-compose").resolve() == plugin
    assert not (config / "config.json").exists()
    assert execute(["compose", "-p", "isolated", "up", "--pull", "never", "-d"]).returncode == 0
    assert calls.read_text().splitlines()[-1] == "compose -p isolated up --pull never -d"


@pytest.mark.parametrize("failure", ["timeout", "secret-output"])
def test_cleanup_attempts_all_owned_resources_after_one_operation_fails(
    failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = runpy.run_path(str(VERIFIER))
    verifier = api["LiveVerifier"]("/fake/docker", "rollback110-0123456789abcdef", "A", "A")
    verifier.compose = ["/fake/docker", "compose", "-p", verifier.project]
    verifier.offline_created = True
    verifier.b_owned = True
    verifier.secrets = ["SECRET-CANARY"]
    calls: list[list[str]] = []

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if "down" in args:
            if failure == "timeout":
                raise subprocess.TimeoutExpired(args, 1)
            return subprocess.CompletedProcess(args, 0, "SECRET-CANARY", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    try:
        assert verifier.cleanup() is False
        assert any(args[1:3] == ["network", "rm"] for args in calls)
        assert any(args[1:3] == ["image", "rm"] for args in calls)
        assert verifier.report["cleanup"] == "failed"
    finally:
        shutil.rmtree(verifier.root)
