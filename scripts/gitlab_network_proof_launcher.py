"""Offline artifact checks or one explicitly authorized local proof and independent audit.

Only --offline-check is appropriate before the source reviews and separate runtime GO.
All runtime children use private cwd/environment, never the checkout's .env.
"""

from __future__ import annotations

import argparse
import ast
import base64
import contextlib
import hashlib
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/gitlab_network"
QUEUES = [
    "reviews.dlq",
    "review.publish",
    "code_index.dlq",
    "index.repo",
    "review.run.fast",
    "retry.30s.fast",
    "retry.2m.fast",
    "retry.10m.fast",
    "review.run.deep",
    "retry.30s.deep",
    "retry.2m.deep",
    "retry.10m.deep",
    "index.repo.retry.30s",
    "index.repo.retry.2m",
    "index.repo.retry.10m",
]
EXCHANGES = [
    "reviews",
    "reviews.retry",
    "reviews.dlx",
    "code_index",
    "code_index.retry",
    "code_index.dlx",
]


def write_private(path: Path, value: Any) -> None:
    descriptor, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    temporary = Path(name)
    os.close(descriptor)
    try:
        temporary.chmod(0o600)
        temporary.write_text(json.dumps(value, indent=2) + "\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def offline_check() -> dict[str, Any]:
    """Parse data/source only; never import the proof or start Node/Postman/app code."""
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    for item in manifest["blobs"].values():
        raw = item["text"].encode()
        assert len(raw) == item["size"]
        assert hashlib.sha256(raw).hexdigest() == item["sha256"]
        assert hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest() == item["oid"]
    assert [
        manifest["blobs"][key]["size"] for key in ("new", "old", "javascript", "unknown", "empty")
    ] == [300, 300, 100, 57, 0]
    assert manifest["expected"]["languages"] == {"Python": 75, "JavaScript": 25}
    tenants = manifest["tenants"]
    assert [tenants[name]["project"] for name in "ABCDE"] == [17, 17, 18, 19, 20]
    assert [tenants[name]["source_project"] for name in "ABCDE"] == [29, 29, 30, 31, 32]
    assert tenants["A"]["host"] == "localhost" and tenants["B"]["host"] == "127.0.0.1"
    assert len({(row["host"], row["project"]) for row in tenants.values()}) == 5
    finding = manifest["review"]["findings"]
    assert len(finding) == 1 and finding[0]["path"] == manifest["paths"]["new"]
    assert finding[0]["line"] == 2 and finding[0]["rule_name"] == "Error Handling Standards"
    assert finding[0]["confidence"] == 0.99
    for name in (
        "gitlab_network_proof.py",
        "gitlab_network_proof_launcher.py",
        "gitlab_network_proof_audit.py",
    ):
        ast.parse((ROOT / "scripts" / name).read_text(), filename=name)
    simulation = (FIXTURES / "local.sim.yaml").read_text()
    assert "routing: header" in simulation
    assert simulation.count("routeKey:") == 2
    assert "./gitlab.js" in simulation and "./llm.js" in simulation
    assert "DNS:localhost,IP:127.0.0.1" in (FIXTURES / "server.ext").read_text()
    return {
        "offline_only": True,
        "blob_manifests": len(manifest["blobs"]),
        "python_sources": 3,
        "queues": QUEUES,
        "exchanges": EXCHANGES,
    }


def remaining(deadline: float) -> float:
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError("owned cleanup deadline exhausted")
    return seconds


def identity(pid: int, deadline: float | None = None) -> str:
    deadline = deadline if deadline is not None else time.monotonic() + 2
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart=,pgid="],
        capture_output=True,
        text=True,
        timeout=min(2, remaining(deadline)),
    )
    remaining(deadline)
    return result.stdout.strip()


def retain_owned(record: dict[str, Any], deadline: float | None = None) -> dict[int, str]:
    """Retain matching identities and new descendants of matching retained members.

    Same PGID alone never establishes ownership. Unknown/reused members fail closed.
    Call at spawn, throughout readiness and before teardown, not only after readiness.
    """
    deadline = deadline if deadline is not None else time.monotonic() + 5
    listing = subprocess.run(
        ["ps", "-ax", "-o", "pid=,ppid=,pgid="],
        capture_output=True,
        text=True,
        timeout=min(2, remaining(deadline)),
    )
    remaining(deadline)
    listing.check_returncode()
    parents = {
        int(parts[0]): int(parts[1])
        for line in listing.stdout.splitlines()
        if len(parts := line.split()) == 3 and int(parts[2]) == record["pgid"]
    }
    current = {pid: birth for pid in parents if (birth := identity(pid, deadline))}
    retained = {
        int(pid): birth
        for pid, birth in record.get("members", {record["pid"]: record["identity"]}).items()
    }
    proven = set()
    for pid, birth in current.items():
        if pid in retained:
            if not birth or retained[pid] != birth:
                raise RuntimeError("owned group identity changed; refusing signal")
            proven.add(pid)
    pending = set(current) - proven
    while pending:
        descendants = {pid for pid in pending if parents[pid] in proven}
        if not descendants:
            raise RuntimeError("unattributed group identity; refusing adoption or signal")
        proven.update(descendants)
        pending -= descendants
    remaining(deadline)
    retained.update(current)
    record["members"] = retained
    record["remaining"] = current
    return current


def identify_child(record: dict[str, Any], process: subprocess.Popen[Any], deadline: float) -> None:
    """The unreaped Popen handle proves this direct child; metadata proves its group."""
    if process.poll() is not None:
        return
    birth = identity(process.pid, deadline)
    if process.poll() is not None:
        return
    if not birth or os.getpgid(process.pid) != record["pgid"]:
        raise RuntimeError("initial child identity unavailable; refusing group adoption")
    record["identity"] = birth
    record.setdefault("members", {})[process.pid] = birth


def reap_child(record: dict[str, Any], process: subprocess.Popen[Any], deadline: float) -> None:
    """Fallback signals only the original unreaped direct child, never an unproved group."""
    try:
        for sig, seconds in ((signal.SIGINT, 0.2), (signal.SIGTERM, 0.2), (signal.SIGKILL, 2)):
            if process.poll() is not None:
                break
            remaining(deadline)
            process.send_signal(sig)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=min(seconds, remaining(deadline)))
        if process.poll() is None:
            raise TimeoutError("direct child has not exited")
    finally:
        record["exit"] = process.poll()


def stop_owned(
    record: dict[str, Any],
    process: subprocess.Popen[bytes] | None = None,
    *,
    deadline: float | None = None,
) -> None:
    """Synchronous cleanup: one absolute deadline bounds all probes, waits and signals."""
    deadline = deadline if deadline is not None else time.monotonic() + 20
    record["group_empty"] = False
    if process is not None and not record.get("identity"):
        try:
            identify_child(record, process, deadline)
        except BaseException:
            reap_child(record, process, deadline)
            raise
    for sig, seconds in ((signal.SIGINT, 8), (signal.SIGTERM, 4), (signal.SIGKILL, 3)):
        current = retain_owned(record, deadline)
        if not current:
            record["group_empty"] = True
            if process is not None:
                process.poll()
            return
        for pid in current:
            remaining(deadline)
            if os.getpgid(pid) != record["pgid"]:
                raise RuntimeError("owned group identity changed; refusing signal")
        remaining(deadline)
        try:
            os.killpg(record["pgid"], sig)
        except ProcessLookupError:
            if retain_owned(record, deadline):
                raise
        until = min(deadline, time.monotonic() + seconds)
        while time.monotonic() < until:
            phase_left = until - time.monotonic()
            if phase_left <= 0:
                break
            if process is not None and process.poll() is None:
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=min(0.1, phase_left, remaining(deadline)))
            if not retain_owned(record, deadline):
                record["group_empty"] = True
                return
            phase_left = until - time.monotonic()
            if phase_left <= 0:
                break
            time.sleep(min(0.05, phase_left, remaining(deadline)))
    raise TimeoutError("owned group did not exit before cleanup deadline")


def checked(command: list[str], cwd: Path, env: dict[str, str]) -> None:
    target = cwd / "setup-processes.json"
    records = json.loads(target.read_text()) if target.exists() else []
    deadline = time.monotonic() + 20
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    record: dict[str, Any] = {
        "argv": command,
        "pid": process.pid,
        "pgid": process.pid,
        "identity": "",
        "started": time.time(),
        "exit": None,
        "group_empty": False,
    }
    records.append(record)
    try:
        write_private(target, records)
        identify_child(record, process, deadline)
        while True:
            retain_owned(record, min(deadline - 5, time.monotonic() + 2))
            write_private(target, records)
            try:
                process.communicate(timeout=min(0.2, remaining(deadline - 5)))
                break
            except subprocess.TimeoutExpired:
                remaining(deadline - 5)
        stop_owned(record, process, deadline=deadline)
    except BaseException:
        try:
            stop_owned(record, process, deadline=deadline)
        except BaseException as error:
            record["cleanup_error"] = type(error).__name__
        raise
    finally:
        record["exit"] = process.poll()
        if record["exit"] is not None:
            record["leader_actual_end"] = time.time()
        if record["exit"] is not None and record["group_empty"]:
            record["actual_end"] = time.time()
        write_private(target, records)
    if process.returncode or not record["group_empty"]:
        raise RuntimeError("private certificate generation or owned cleanup failed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline-check", action="store_true")
    parser.add_argument("--scenario", choices=("all", "retirement"), default="all")
    parser.add_argument("--scratch", type=Path)
    parser.add_argument("--database-url-env", default="TEST_DATABASE_URL")
    parser.add_argument("--rabbitmq-url-env", default="TEST_RABBITMQ_URL")
    parser.add_argument("--postman-port", type=int, default=49400)
    parser.add_argument("--tls-port", type=int, default=49443)
    parser.add_argument("--runtime-timeout", type=int, default=600)
    parser.add_argument("--audit-timeout", type=int, default=30)
    parser.add_argument("--source-manifest", type=Path)
    parser.add_argument("--rabbitmq-container", default="dmc-268-169-rabbitmq")
    args = parser.parse_args()
    if args.offline_check:
        print(json.dumps(offline_check()), flush=True)
        return 0
    if args.scratch is None or args.source_manifest is None:
        parser.error("--scratch and --source-manifest are required for authorized runtime")
    assert (args.postman_port, args.tls_port, args.runtime_timeout, args.audit_timeout) == (
        49400,
        49443,
        600,
        30,
    )
    scratch = args.scratch.resolve()
    assert not any((ancestor / ".git").exists() for ancestor in (scratch, *scratch.parents)), (
        "runtime artifacts must be outside every Git tree"
    )
    scratch.mkdir(mode=0o700, parents=False, exist_ok=False)
    os.chmod(scratch, 0o700)
    database = os.environ[args.database_url_env]
    broker = os.environ[args.rabbitmq_url_env]
    assert "@127.0.0.1:49323/dmc169_test" in database
    parsed = urlsplit(broker)
    assert (parsed.hostname, parsed.port, parsed.path) == ("127.0.0.1", 58447, "/dmc169-test")
    scope = json.loads(args.source_manifest.read_text())
    assert scope["checkout"] == str(ROOT)
    for name, sha in scope["hashes"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == sha
    for path in FIXTURES.iterdir():
        if path.is_file():
            shutil.copyfile(path, scratch / path.name)
    kept = ("HOME", "home", "CODEX_HOME", "PATH", "LANG", "LC_ALL", "USER", "LOGNAME")
    env = {name: value for name, value in os.environ.items() if name in kept}
    env.update(
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONPATH=str(ROOT),
        UV_CACHE_DIR=str(scratch / "uv-cache"),
        TMPDIR=str(scratch),
        T15_PRIVATE_CONFIG=str(scratch / "runtime.json"),
    )
    assert all(env.get(name) == os.environ.get(name) for name in ("HOME", "home", "CODEX_HOME"))
    config: dict[str, Any] = {
        "source": str(ROOT),
        "scenario": args.scenario,
        "scratch": str(scratch),
        "source_manifest": str(args.source_manifest.resolve()),
        "database_url": database,
        "rabbitmq_url": broker,
        "postman_port": args.postman_port,
        "tls_port": args.tls_port,
        "runtime_timeout": args.runtime_timeout,
        "rabbitmq_container": args.rabbitmq_container,
        "queues": QUEUES,
        "exchanges": EXCHANGES,
        "control_key": secrets.token_urlsafe(32),
        "model_key": secrets.token_urlsafe(32),
        "credential_key": base64.b64encode(secrets.token_bytes(32)).decode(),
        "tenants": {
            name: {
                "origin": f"https://{tenant['host']}:{args.tls_port}",
                "project": tenant["project"],
                "source_project": tenant["source_project"],
                "tokens": [secrets.token_urlsafe(32), secrets.token_urlsafe(32)],
                "webhook_secret": secrets.token_urlsafe(32),
            }
            for name, tenant in json.loads((FIXTURES / "manifest.json").read_text())[
                "tenants"
            ].items()
        },
    }
    write_private(scratch / "runtime.json", config)
    for name in ("ca", "untrusted"):
        checked(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                name + ".key",
                "-out",
                name + ".pem",
                "-days",
                "1",
                "-subj",
                "/CN=Synthetic T15 " + name,
                "-addext",
                "keyUsage=critical,keyCertSign,cRLSign",
            ],
            scratch,
            env,
        )
    checked(
        [
            "openssl",
            "req",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            "server.key",
            "-out",
            "server.csr",
            "-subj",
            "/CN=localhost",
        ],
        scratch,
        env,
    )
    checked(
        [
            "openssl",
            "x509",
            "-req",
            "-in",
            "server.csr",
            "-CA",
            "ca.pem",
            "-CAkey",
            "ca.key",
            "-CAcreateserial",
            "-out",
            "server.pem",
            "-days",
            "1",
            "-extfile",
            "server.ext",
        ],
        scratch,
        env,
    )
    # Valid synthetic App signing key, generated privately; no real App credentials.
    checked(
        [
            "openssl",
            "genpkey",
            "-algorithm",
            "RSA",
            "-pkeyopt",
            "rsa_keygen_bits:2048",
            "-out",
            "github.key",
        ],
        scratch,
        env,
    )
    for file in scratch.iterdir():
        if file.suffix in {".key", ".pem", ".csr", ".srl"}:
            file.chmod(0o600)
    python = ROOT / ".venv/bin/python"
    base = ["uv", "run", "--no-project", "--python", str(python), "python"]
    record: dict[str, Any] = {
        "supervisor_pid": os.getpid(),
        "supervisor_pgid": os.getpgid(0),
        "cwd": str(scratch),
        "environment_keys": sorted(env),
        "home_keys_preserved": True,
        "children": [],
        "runtime_actual_end": False,
    }

    def run_child(script: str, log: str, limit: int, *, cleanup_reserve: int) -> int:
        argv = base + [str(ROOT / "scripts" / script), "--config", str(scratch / "runtime.json")]
        child_deadline = time.monotonic() + limit
        if script == "gitlab_network_proof_audit.py":
            argv += [
                "--deadline-monotonic",
                repr(child_deadline),
                "--cleanup-reserve-seconds",
                str(cleanup_reserve),
            ]
        with (scratch / log).open("wb") as output:
            process = subprocess.Popen(
                argv,
                cwd=scratch,
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            child: dict[str, Any] = {
                "argv": argv,
                "pid": process.pid,
                "pgid": process.pid,
                "identity": "",
                "exit": None,
                "group_empty": False,
                "deadline_monotonic": child_deadline,
                "started": time.time(),
                "timeout_seconds": limit,
                "cleanup_reserve_seconds": cleanup_reserve,
            }
            record["children"].append(child)
            outcome = 0
            child["cleanup_errors"] = []
            work_deadline = child_deadline - cleanup_reserve
            try:
                write_private(scratch / "command.json", record)
                identify_child(child, process, work_deadline)
                while True:
                    retain_owned(child, min(work_deadline, time.monotonic() + 5))
                    write_private(scratch / "command.json", record)
                    try:
                        outcome = process.wait(timeout=min(0.2, remaining(work_deadline)))
                        break
                    except subprocess.TimeoutExpired:
                        remaining(work_deadline)
            except BaseException as error:
                outcome = (
                    130
                    if isinstance(error, KeyboardInterrupt)
                    else 124
                    if isinstance(error, (TimeoutError, subprocess.TimeoutExpired))
                    else 1
                )
                child["timeout"] = outcome == 124
                child["primary_error"] = type(error).__name__
                registry = scratch / "processes.json"
                owned: list[dict[str, Any]] = []
                try:
                    owned = json.loads(registry.read_text()) if registry.exists() else []
                except (OSError, ValueError) as registry_error:
                    child["cleanup_errors"].append(type(registry_error).__name__)
                # No executor: every probe and signal finishes before the audit can start.
                # Stop the root first, then retained peers, all sharing this exact deadline.
                for row, handle in [(child, process), *((item, None) for item in owned)]:
                    try:
                        stop_owned(row, handle, deadline=child_deadline)
                    except BaseException as cleanup_error:
                        child["cleanup_errors"].append(type(cleanup_error).__name__)
                child["cleanup_actual_end"] = time.time()
            # Audit commands share this group. Their persisted birth records also
            # preserve attribution if interruption reparents a helper before our next poll.
            helpers = scratch / "audit-processes.json"
            try:
                if script == "gitlab_network_proof_audit.py" and helpers.exists():
                    for helper in json.loads(helpers.read_text()):
                        if helper.get("identity") and helper["pgid"] == child["pgid"]:
                            child.setdefault("members", {})[helper["pid"]] = helper["identity"]
                stop_owned(child, process, deadline=child_deadline)
            except BaseException as cleanup_error:
                child["cleanup_errors"].append(type(cleanup_error).__name__)
            child["cleanup_actual_end"] = time.time()
            child["exit"] = process.poll()
            child["outcome"] = outcome
            if child["exit"] is not None:
                child["actual_end"] = time.time()
            if not child["group_empty"] and outcome == 0:
                outcome = child["outcome"] = 1
            write_private(scratch / "command.json", record)
            return outcome

    runtime = run_child(
        "gitlab_network_proof.py", "proof.log", args.runtime_timeout, cleanup_reserve=25
    )
    record["runtime_actual_end"] = record["children"][0]["exit"] is not None
    write_private(scratch / "command.json", record)
    if not record["runtime_actual_end"]:
        record["runtime_exit"] = runtime
        record["audit_withheld"] = "runtime has not actually exited"
        write_private(scratch / "command.json", record)
        print(json.dumps({"runtime_exit": runtime, "actual_end": False}), flush=True)
        return runtime or 1
    # Exactly one independent observer after confirmed runtime exit, including timeout.
    audit = run_child(
        "gitlab_network_proof_audit.py", "audit.log", args.audit_timeout, cleanup_reserve=5
    )
    record.update(runtime_exit=runtime, audit_exit=audit)
    observation = scratch / "final-independent-cleanup.json"
    try:
        observed = json.loads(observation.read_text()) if observation.exists() else {}
        record["owned_groups_confirmed_empty"] = (
            "groups" in observed
            and not any(observed["groups"].values())
            and all(
                child["exit"] is not None and child["group_empty"] for child in record["children"]
            )
        )
    except (OSError, ValueError, TypeError) as error:
        record["owned_groups_confirmed_empty"] = False
        record["observation_error"] = type(error).__name__
    if record["owned_groups_confirmed_empty"]:
        record["actual_end"] = time.time()
    write_private(scratch / "command.json", record)
    print(
        json.dumps(
            {
                "runtime_exit": runtime,
                "audit_exit": audit,
                "artifacts": str(scratch),
                "actual_end": "actual_end" in record,
            }
        ),
        flush=True,
    )
    return runtime or audit or (0 if "actual_end" in record else 1)


if __name__ == "__main__":
    sys.exit(main())
