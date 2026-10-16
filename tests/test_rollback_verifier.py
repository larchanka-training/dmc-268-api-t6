"""Safety contracts at the opt-in verifier's CLI and report boundaries."""

from __future__ import annotations

import json
import os
import re
import runpy
import shutil
import subprocess
from collections.abc import Callable, Iterator
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
        safe.replace('"AUTH_KEY", "PATH"', '"PATH"'),
        safe.replace('"APP_KEY"', '"APP_KEY", "UNEXPECTED_KEY"'),
        safe.replace('"BOT_KEY"', '"APP_KEY"'),
        safe.replace('"APP_KEY"', '"bad\\nSECRET-CANARY"'),
        safe.replace('worker environment keys: ["APP_KEY"]\n', ""),
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


@pytest.mark.parametrize(
    "graph",
    [
        {"heads": ["20300101_0099"], "revisions": ["20300101_0099", "20261010_0110"]},
        {"heads": [], "revisions": []},
        {"heads": ["a", "b"], "revisions": ["a", "b"]},
        {"heads": ["bad\nSECRET"], "revisions": ["bad\nSECRET"]},
        {"heads": ["missing"], "revisions": ["other"]},
        {"heads": "a", "revisions": ["a"]},
        {"heads": ["a"], "revisions": "a"},
        ["unexpected"],
    ],
)
def test_prepare_derives_fixture_parent_from_a_graph_or_refuses_before_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, graph: object
) -> None:
    api = runpy.run_path(str(VERIFIER))
    image = "sha256:" + "a" * 64
    verifier = api["LiveVerifier"](
        "/fake/docker", "rollback110-0123456789abcdef", "prepared:a", image
    )
    plugin = tmp_path / "docker-compose"
    plugin.touch()
    builds: list[Path] = []

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        output = ""
        if args[1] == "info":
            output = str(plugin)
        elif args[1:3] == ["image", "inspect"]:
            output = image
        elif args[1] == "run":
            output = json.dumps(graph)
        elif args[1] == "build":
            builds.append(Path(args[-1]))
            raise api["VerificationError"]("fixture build boundary")
        return subprocess.CompletedProcess(args, 0, output, "")

    monkeypatch.setattr(subprocess, "run", run)
    previous_umask = os.umask(0o077)
    try:
        with pytest.raises(api["VerificationError"]):
            verifier.prepare()
        if isinstance(graph, dict) and graph.get("heads") == ["20300101_0099"]:
            assert len(builds) == 1
            assert (builds[0] / "Dockerfile").read_text() == (
                "FROM prepared:a\n"
                "COPY new_revision.py /srv/alembic/versions/20261010_0110_1_rollback_fixture.py\n"
            )
            assert (builds[0] / "new_revision.py").stat().st_mode & 0o777 == 0o644
            fixture = runpy.run_path(str(builds[0] / "new_revision.py"))
            assert fixture["down_revision"] == "20300101_0099"
            assert fixture["revision"] == "20261010_0110_1"
            assert verifier.report["revision_M"] == "20300101_0099"
            assert verifier.report["revision_N"] == "20261010_0110_1"
        else:
            assert builds == []
    finally:
        os.umask(previous_umask)
        shutil.rmtree(verifier.root)


@pytest.fixture
def prepared_verifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[[object, bool], Any]]:
    api = runpy.run_path(str(VERIFIER))
    image_a = "sha256:" + "a" * 64
    image_b = "sha256:" + "b" * 64
    original_config = tmp_path / "user-config"
    original_config.mkdir()
    (original_config / "config.json").write_text(
        '{"auths":{"registry.example":{"auth":"PRIVATE-CANARY"}}}'
    )
    monkeypatch.setenv("DOCKER_CONFIG", str(original_config))
    verifiers: list[Any] = []
    real_run = subprocess.run

    def prepare(graph_b: object, user_plugin: bool) -> Any:
        plugin = (
            original_config if user_plugin else tmp_path / "system"
        ) / "cli-plugins/docker-compose"
        plugin.parent.mkdir(parents=True, exist_ok=True)
        plugin.write_text("plugin fixture")
        verifier = api["LiveVerifier"](
            "/fake/docker", "rollback110-0123456789abcdef", "prepared:a", image_a
        )
        verifiers.append(verifier)

        def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            if args[0] == "bash":
                return real_run(args, **kwargs)
            output = ""
            if args[1] == "info":
                output = str(plugin)
            elif args[1:3] == ["compose", "version"]:
                # User-config-only plugin must remain available in the private config.
                private = Path(verifier.environment["DOCKER_CONFIG"])
                if user_plugin:
                    assert (private / "cli-plugins/docker-compose").resolve() == plugin
                else:
                    assert not (private / "cli-plugins/docker-compose").exists()
                assert not (private / "config.json").exists()
                output = "5.6.0"
            elif args[1:3] == ["image", "inspect"]:
                output = image_a if args[-1] == "prepared:a" else image_b
            elif args[1] == "run":
                if "import os,json" in args[-1]:
                    output = '["PATH", "PYTHON_VERSION"]'
                elif image_a in args:
                    output = json.dumps(
                        {"heads": ["20300101_0099"], "revisions": ["20300101_0099"]}
                    )
                else:
                    output = json.dumps(graph_b)
            return subprocess.CompletedProcess(args, 0, output, "")

        monkeypatch.setattr(subprocess, "run", run)
        return verifier

    yield prepare
    for verifier in verifiers:
        shutil.rmtree(verifier.root)


@pytest.mark.parametrize("role", ["api", "worker", "webhook-worker"])
def test_prepare_expected_keys_match_real_compose_runtime_environment(
    prepared_verifier: Callable[[object, bool], Any], role: str
) -> None:
    verifier = prepared_verifier(
        {
            "heads": ["20261010_0110"],
            "revisions": ["20261010_0110", "20300101_0099"],
            "parents": {"20261010_0110": "20300101_0099"},
        },
        False,
    )
    verifier.prepare()
    compose = (VERIFIER.parents[1] / "deploy/compose/staging.yml").read_text()
    service = re.search(rf"^  {role}:\n(.*?)(?=^  [a-z][a-z0-9-]*:|\Z)", compose, re.M | re.S)
    assert service is not None
    environment = re.search(r"^    environment:\n((?:      .*\n)*)", service[1], re.M)
    env_files = re.search(r"^    env_file:\n((?:      .*\n)*)", service[1], re.M)
    assert environment is not None and env_files is not None
    # Compose is the independent source: compare names only, never fixture secret values.
    runtime_keys = {"PATH", "PYTHON_VERSION"}
    runtime_keys.update(re.findall(r"^      ([A-Za-z_][A-Za-z0-9_]*):", environment[1], re.M))
    for name in re.findall(r"^      - (\S+)$", env_files[1], re.M):
        runtime_keys.update(
            re.findall(r"^([A-Z][A-Z0-9_]*)='[^']*'\n", (verifier.app / name).read_text(), re.M)
        )
    assert verifier.expected_keys[role] == sorted(runtime_keys)


@pytest.mark.parametrize("user_plugin", [False, True])
def test_prepare_accepts_b_child_and_exposes_only_user_config_plugin(
    prepared_verifier: Callable[[object, bool], Any], tmp_path: Path, user_plugin: bool
) -> None:
    verifier = prepared_verifier(
        {
            "heads": ["20261010_0110"],
            "revisions": ["20261010_0110", "20300101_0099"],
            "parents": {"20261010_0110": "20300101_0099"},
        },
        user_plugin,
    )
    verifier.prepare()
    assert verifier.app.is_dir()
    assert verifier.report["revision_M"] == "20300101_0099"
    wrapper = verifier.root / "bin/docker"
    real = tmp_path / "real-docker"
    real.write_text("#!/usr/bin/env bash\nexit 0\n")
    real.chmod(0o700)
    wrapper.write_text(wrapper.read_text().replace("real=/fake/docker", f"real={real}"))
    private = tmp_path / "per-run-config"
    result = subprocess.run(
        ["bash", str(wrapper), "compose", "-p", "isolated", "up", "-d"],
        env={**os.environ, "DOCKER_CONFIG": str(private)},
        capture_output=True,
        text=True,
        check=False,
    )
    # Use the actual subprocess, bypassing only prepare's Docker fake.
    assert result.returncode == 0
    if user_plugin:
        assert (private / "cli-plugins/docker-compose").is_symlink()
    else:
        assert not (private / "cli-plugins").exists()
    assert not (private / "config.json").exists()


@pytest.mark.parametrize(
    "graph_b",
    [
        [],
        {
            "heads": [],
            "revisions": ["20261010_0110", "20300101_0099"],
            "parents": {"20261010_0110": "20300101_0099"},
        },
        {
            "heads": ["wrong"],
            "revisions": ["20261010_0110", "20300101_0099"],
            "parents": {"20261010_0110": "20300101_0099"},
        },
        {
            "heads": ["20261010_0110"],
            "revisions": ["20261010_0110"],
            "parents": {"20261010_0110": "20300101_0099"},
        },
        {
            "heads": ["20261010_0110"],
            "revisions": "malformed",
            "parents": {"20261010_0110": "20300101_0099"},
        },
        {"heads": ["20261010_0110"], "revisions": ["20261010_0110", "20300101_0099"]},
        {
            "heads": ["20261010_0110"],
            "revisions": ["20261010_0110", "20300101_0099"],
            "parents": [],
        },
        {
            "heads": ["20261010_0110"],
            "revisions": ["20261010_0110", "20300101_0099"],
            "parents": {},
        },
        {
            "heads": ["20261010_0110"],
            "revisions": ["20261010_0110", "20300101_0099"],
            "parents": {"20261010_0110": "wrong"},
        },
    ],
)
def test_prepare_refuses_invalid_b_graph_before_application_files(
    prepared_verifier: Callable[[object, bool], Any], graph_b: object
) -> None:
    verifier = prepared_verifier(graph_b, False)
    with pytest.raises(Exception, match="B graph is not the synthetic child of A"):
        verifier.prepare()
    assert not verifier.app.exists()
    assert verifier.compose == []
