"""One independent, bounded, read-only observer after the proof process actually exits.

Does not import the runtime harness, change topology or drop schemas. Exact retained
identities/source hashes and the reserved resources alone are examined.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

import psycopg
from gitlab_network_proof_launcher import identify_child, reap_child, remaining, write_private


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--deadline-monotonic", required=True, type=float)
    parser.add_argument("--cleanup-reserve-seconds", required=True, type=int, choices=[5])
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    scratch = Path(config["scratch"])
    command = json.loads((scratch / "command.json").read_text())
    assert command["runtime_actual_end"] and "actual_end" in command["children"][0]
    runtime = json.loads((scratch / "results.json").read_text())
    retained = json.loads((scratch / "processes.json").read_text())
    result: dict[str, Any] = {
        "observer_pid": os.getpid(),
        "observer_pgid": os.getpgrp(),
        "deadline_monotonic": args.deadline_monotonic,
        "groups": {},
        "ports_closed": {},
        "commands": [],
        "observer_closed": False,
    }
    groups = {row["pgid"] for row in retained} | {command["children"][0]["pgid"]}
    setup = scratch / "setup-processes.json"
    if setup.exists():
        groups.update(row["pgid"] for row in json.loads(setup.read_text()))

    # These immutable inputs and our own group are available before the supervisor
    # can append this child to its concurrently updated (atomic) command snapshot.
    deadline = args.deadline_monotonic
    work_deadline = deadline - args.cleanup_reserve_seconds
    helper_registry = scratch / "audit-processes.json"

    def observe(argv: list[str], timeout: int = 5) -> str:
        # Inherit the retained audit group; no separately orphanable observer session.
        process = subprocess.Popen(
            argv,
            cwd=scratch,
            env=os.environ,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        record: dict[str, Any] = {
            "argv": argv,
            "pid": process.pid,
            "pgid": result["observer_pgid"],
            "identity": "",
            "exit": None,
            "started": time.time(),
        }
        result["commands"].append(record)
        try:
            write_private(helper_registry, result["commands"])
            identify_child(record, process, min(work_deadline, time.monotonic() + 2))
            write_private(helper_registry, result["commands"])
            stdout, _ = process.communicate(timeout=min(timeout, remaining(work_deadline)))
        except BaseException:
            try:
                reap_child(record, process, deadline)
            except BaseException as error:
                record["cleanup_error"] = type(error).__name__
            raise
        finally:
            record["exit"] = process.poll()
            if record["exit"] is not None:
                record["actual_end"] = time.time()
            write_private(helper_registry, result["commands"])
        assert process.returncode in (0, 1) and (argv[0] != "docker" or process.returncode == 0)
        return stdout

    # One process-table read covers all retained groups; no repeated per-scenario audit.
    listing = observe(["ps", "-ax", "-o", "pid=,ppid=,pgid="])
    for group in groups:
        result["groups"][str(group)] = [
            row
            for row in listing.splitlines()
            if len(fields := row.split()) == 3 and int(fields[2]) == group
        ]
    for port in (config["postman_port"], config["tls_port"]):
        with socket.socket() as listener:
            listener.settimeout(0.2)
            result["ports_closed"][str(port)] = listener.connect_ex(("127.0.0.1", port)) != 0
    database = config["database_url"].replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(
        database, autocommit=True, connect_timeout=3, application_name="t15-independent-observer"
    ) as connection:
        schema = runtime["schema"]
        row = connection.execute(
            "SELECT count(*) FROM pg_namespace WHERE nspname=%s", (schema,)
        ).fetchone()
        assert row is not None
        result["schema_count"] = row[0]
        result["owned_clients"] = [
            dict(zip(("pid", "application_name", "state"), row, strict=True))
            for row in connection.execute(
                (
                    "SELECT pid,application_name,state FROM pg_stat_activity"
                    " WHERE datname=current_database() AND application_name "
                    "LIKE %s AND pid<>pg_backend_pid()"
                ),
                (schema + "%",),
            )
        ]
    result["observer_closed"] = True
    for kind, fields in (
        ("queues", ["name", "consumers", "messages"]),
        ("exchanges", ["name"]),
        ("connections", ["name", "vhost", "channels"]),
    ):
        argv = ["docker", "exec", config["rabbitmq_container"], "rabbitmqctl", "-q", "list_" + kind]
        if kind != "connections":
            argv.extend(["-p", "dmc169-test"])
        rows = json.loads(observe(argv + fields + ["--formatter=json"], timeout=6))
        result[kind] = (
            [row for row in rows if row.get("vhost") == "dmc169-test"]
            if kind == "connections"
            else [row for row in rows if row.get("name") in config[kind]]
        )
    scope = json.loads(Path(config["source_manifest"]).read_text())
    source = Path(config["source"])
    snapshot = Path(scope["snapshot"])
    result["source_hashes"] = {
        name: hashlib.sha256((source / name).read_bytes()).hexdigest() == digest
        and (source / name).read_bytes() == (snapshot / name).read_bytes()
        for name, digest in scope["hashes"].items()
    }
    result["source_manifest_exact"] = set(scope["paths"]) == set(scope["hashes"]) and all(
        result["source_hashes"].values()
    )
    result["no_owned_runtime_work_remains"] = (
        not any(result["groups"].values())
        and all(result["ports_closed"].values())
        and result["schema_count"] == 0
        and not any(
            result[name] for name in ("owned_clients", "queues", "exchanges", "connections")
        )
        and result["observer_closed"]
        and result["source_manifest_exact"]
    )
    write_private(scratch / "final-independent-cleanup.json", result)
    print(
        json.dumps(
            {
                key: value
                for key, value in result.items()
                if key not in {"commands", "source_hashes"}
            }
        ),
        flush=True,
    )
    return 0 if result["no_owned_runtime_work_remains"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
