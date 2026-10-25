"""Replay the Reflection catalog in a disposable copy; default: validate/list only.

Run ``uv run python review/scripts/replay_reflection_mutations.py --help``.
The catalog fingerprints intentionally require review after target-file changes.
No results are reused. SIGKILL can leave only an owned disposable copy behind.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import difflib
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from types import FrameType
from typing import Any

import pytest

CATALOG = "review/scripts/reflection_mutations.json"
PLUGIN = "review.scripts.replay_reflection_mutations"
FORBIDDEN_PARTS = {".git", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


class ReplayError(ValueError):
    """A stale input, unsafe execution or unproved mutation result."""


def safe_path(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ReplayError(f"Unsafe relative path: {relative}")
    if any(
        part in FORBIDDEN_PARTS
        or part == ".env"
        or part.startswith(".env.")
        and part != ".env.example"
        for part in path.parts
    ):
        raise ReplayError(f"Excluded private/runtime path: {relative}")
    result = root / path
    if not result.resolve().is_relative_to(root.resolve()):
        raise ReplayError(f"Symlink escape: {relative}")
    return result


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_hashes(root: Path, paths: Sequence[str]) -> dict[str, str]:
    return {path: digest(safe_path(root, path).read_bytes()) for path in paths}


def snapshot(source: Path, destination: Path, extra: Sequence[str]) -> dict[str, Any]:
    """Copy Git-listed bytes, including dirty tracked inputs, never Git or a linked venv."""
    listed = subprocess.check_output(["git", "ls-files", "-z"], cwd=source).decode().split("\0")
    paths: list[str] = []
    for relative in sorted(set(listed + list(extra)) - {""}):
        if any(part in FORBIDDEN_PARTS for part in Path(relative).parts):
            continue
        # Secret environment files are excluded even if accidentally tracked.
        if (
            Path(relative).name == ".env"
            or Path(relative).name.startswith(".env.")
            and Path(relative).name != ".env.example"
        ):
            continue
        candidate = safe_path(source, relative)
        # Git tracks some tooling-directory symlinks; no linked directories are copied.
        if candidate.is_dir():
            continue
        paths.append(relative)
    before = file_hashes(source, paths)
    destination.mkdir(exist_ok=False)
    (destination.parent / "OWNED_REFLECTION_REPLAY").write_text(
        "Disposable mutation replay snapshot\n"
    )
    for relative in paths:
        target = safe_path(destination, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(safe_path(source, relative).read_bytes())
    if before != file_hashes(source, paths) or before != file_hashes(destination, paths):
        raise ReplayError("Source changed during snapshot capture")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=source, text=True, capture_output=True, check=False
    )
    dirty = subprocess.check_output(["git", "status", "--porcelain", "-z"], cwd=source)
    return {
        "head": head.stdout.strip() if head.returncode == 0 else None,
        "dirty": bool(dirty),
        "dirty_status_sha256": digest(dirty),
        "files": before,
        "input_identity": digest(json.dumps(before, sort_keys=True).encode()),
    }


def edited_bytes(root: Path, edit: Mapping[str, Any], current: bytes | None = None) -> bytes:
    path = safe_path(root, edit["path"])
    original = path.read_bytes()
    if digest(original) != edit["sha256"]:
        raise ReplayError(f"Stale fingerprint: {edit['path']}")
    text = (original if current is None else current).decode()
    if edit["kind"] == "text":
        old = edit["old"]
        if text.count(old) != edit["occurrences"]:
            raise ReplayError(f"Unexpected anchor occurrences: {edit['path']}")
        if edit["context"] not in text:
            raise ReplayError(f"Missing contextual anchor: {edit['path']}")
        selected = edit["selected"]
        if (
            not selected
            or len(set(selected)) != len(selected)
            or any(index < 0 or index >= edit["occurrences"] for index in selected)
        ):
            raise ReplayError("Invalid selected occurrences")
        parts = text.split(old)
        text = parts[0] + "".join(
            (edit["new"] if index in selected else old) + tail
            for index, tail in enumerate(parts[1:])
        )
    elif edit["kind"] == "json":
        value = json.loads(text)
        parent = value
        for key in edit["keys"][:-1]:
            parent = parent[key]
        key = edit["keys"][-1]
        if key not in parent or parent[key] != edit["expected"]:
            raise ReplayError(f"Stale JSON value: {edit['path']}")
        if edit["operation"] == "delete":
            del parent[key]
        elif edit["operation"] == "set":
            parent[key] = edit["value"]
        else:
            raise ReplayError("Unknown JSON operation")
        text = json.dumps(value, indent=2) + "\n"
    else:
        raise ReplayError("Unknown edit kind")
    if path.suffix == ".py":
        ast.parse(text, filename=edit["path"])
    if text.encode() == (original if current is None else current):
        raise ReplayError("Mutation made no change")
    return text.encode()


@contextlib.contextmanager
def mutation(root: Path, edits: Sequence[Mapping[str, Any]]) -> Iterator[str]:
    """Validate an atomic edit set, then always restore its exact input bytes."""
    originals: dict[str, bytes] = {}
    changes: dict[str, bytes] = {}
    for edit in edits:
        relative = edit["path"]
        originals.setdefault(relative, safe_path(root, relative).read_bytes())
        changes[relative] = edited_bytes(root, edit, changes.get(relative))
    patch = "".join(
        "".join(
            difflib.unified_diff(
                originals[path].decode().splitlines(True),
                data.decode().splitlines(True),
                fromfile="a/" + path,
                tofile="b/" + path,
            )
        )
        for path, data in changes.items()
    )
    try:
        for relative, data in changes.items():
            safe_path(root, relative).write_bytes(data)
        yield patch
    finally:
        for relative, data in originals.items():
            safe_path(root, relative).write_bytes(data)
        if any(
            safe_path(root, relative).read_bytes() != data for relative, data in originals.items()
        ):
            raise ReplayError("Exact restoration failed")


def verify_imports(root: Path, modules: Mapping[str, Path]) -> None:
    for name, path in modules.items():
        if name.split(".")[0] in {"app", "review", "tests"} and not path.resolve().is_relative_to(
            root.resolve()
        ):
            raise ReplayError(f"Module imported outside snapshot: {name}")


def _blocked_socket(*args: Any, **kwargs: Any) -> Any:
    raise ReplayError("Network connections are forbidden during unit mutation replay")


def pytest_configure(config: pytest.Config) -> None:
    if os.environ.get("REFLECTION_REPLAY_ROOT"):
        socket.socket.connect = _blocked_socket  # type: ignore[method-assign]
        socket.socket.connect_ex = _blocked_socket  # type: ignore[method-assign]
        socket.create_connection = _blocked_socket


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if os.environ.get("REFLECTION_REPLAY_ROOT") and any(
        item.get_closest_marker("integration") for item in items
    ):
        pytest.exit("Integration tests are forbidden in unit mutation replay", returncode=4)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    root = os.environ.get("REFLECTION_REPLAY_ROOT")
    if root:
        try:
            verify_imports(
                Path(root),
                {
                    name: Path(str(module.__file__))
                    for name, module in sys.modules.copy().items()
                    if getattr(module, "__file__", None)
                },
            )
        except ReplayError:
            session.exitstatus = 4


def inspect_report(
    report: Path,
    code: int,
    nodes: Sequence[str],
    *,
    semantic: bool = False,
    allowed_failures: Sequence[str] = ("AssertionError", "Failed: DID NOT RAISE"),
) -> dict[str, Any]:
    """A named contract failure is a kill; collection/setup/infrastructure failures are not."""
    if not report.exists():
        return {"green": False, "killed": False, "reason": "missing pytest report"}
    tree = ET.parse(report)
    cases = tree.findall(".//testcase")
    errors = tree.findall(".//error")
    skipped = tree.findall(".//skipped")
    failures = [(case, failure) for case in cases for failure in case.findall("failure")]
    names = {node.replace("/", ".").replace(".py::", "::").split("[", 1)[0] for node in nodes}
    forbidden = (
        "SyntaxError",
        "IndentationError",
        "ImportError",
        "ModuleNotFoundError",
        "INTERNALERROR",
        "Network connections are forbidden",
    )
    valid = bool(failures)
    failed_nodes: list[str] = []
    for case, failure in failures:
        node = case.get("classname", "") + "::" + case.get("name", "")
        failed_nodes.append(node)
        detail = (failure.text or "") + failure.get("message", "")
        named = node.split("[", 1)[0] in names
        summary = failure.get("message") or (failure.text or "").strip()
        approved = False
        for family in allowed_failures:
            if family == "AssertionError":
                pattern = r"^(?:assert\b|(?:builtins\.)?AssertionError\b)"
            elif family == "Failed: DID NOT RAISE":
                pattern = r"^(?:Failed: )?DID NOT RAISE\b"
            else:
                pattern = r"^(?:[A-Za-z_]\w*\.)*" + re.escape(family)
            approved = approved or re.match(pattern, summary) is not None
        unexpected_type_error = re.match(r"^(?:[A-Za-z_]\w*\.)*TypeError\b", summary)
        valid = (
            valid
            and named
            and approved
            and not unexpected_type_error
            and not any(word in detail for word in forbidden)
        )
        if semantic:
            valid = valid and "checksum mismatch" not in detail.lower()
    clean = bool(cases) and not errors and not skipped
    return {
        "green": code == 0 and clean and not failures,
        "killed": code == 1 and clean and valid,
        "failed_nodes": failed_nodes,
        "cases": len(cases),
        "errors": len(errors),
        "skipped": len(skipped),
    }


def run_command(
    command: Sequence[str], cwd: Path, env: Mapping[str, str], timeout: float, log: Path
) -> int:
    """Stop and await the whole child group before the caller restores a mutation."""
    with log.open("wb") as output:
        child = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        completed = False
        try:
            code = child.wait(timeout=timeout)
            completed = True
            return code
        finally:
            if not completed:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGTERM)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    child.wait(timeout=3)
                # A descendant can outlive the group leader or ignore SIGTERM.
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGKILL)
                child.wait()


def validate_catalog(root: Path, catalog: Mapping[str, Any]) -> Counter[str]:
    seen: set[str] = set()
    counts: Counter[str] = Counter()
    for probe in catalog["probes"]:
        if probe["id"] in seen or not re.fullmatch(r"[A-Za-z0-9_.-]+", probe["id"]):
            raise ReplayError("Duplicate or unsafe probe ID")
        seen.add(probe["id"])
        counts[probe["classification"]] += 1
        if probe["classification"] != "live":
            if not probe["rationale"]:
                raise ReplayError("Non-live disposition needs a rationale")
            continue
        current: dict[str, bytes] = {}
        for edit in probe["edits"]:
            current[edit["path"]] = edited_bytes(root, edit, current.get(edit["path"]))
        if not probe["nodes"]:
            raise ReplayError("Live probe needs a named test")
        for node in probe["nodes"]:
            relative, _, function = node.partition("::")
            if not relative.startswith("tests/") or not function:
                raise ReplayError(f"Unit test node required: {node}")
            tree = ast.parse(safe_path(root, relative).read_text())
            names = {
                item.name
                for item in ast.walk(tree)
                if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
            }
            if function.split("[", 1)[0] not in names:
                raise ReplayError(f"Missing committed test node: {node}")
    if counts != Counter(catalog["counts"]):
        raise ReplayError("Catalog accounting mismatch")
    return counts


def execute(
    source: Path,
    catalog: Mapping[str, Any],
    ids: Sequence[str],
    output: Path,
    timeout: float,
    extra: Sequence[str],
    cache: Path | None,
) -> None:
    if output.exists():
        raise ReplayError("Output already exists; replay never resumes unverified state")
    if output.resolve().is_relative_to(source.resolve()):
        raise ReplayError("Replay output must be outside the source checkout")
    selected = [
        probe
        for probe in catalog["probes"]
        if probe["classification"] == "live" and (not ids or probe["id"] in ids)
    ]
    if not selected or set(ids) - {probe["id"] for probe in selected}:
        raise ReplayError("Unknown or non-live selected probe")
    output.mkdir(parents=True)
    tree = output / "tree"
    identity = snapshot(source, tree, extra)
    validate_catalog(tree, catalog)
    identity["runner_sha256"] = digest(Path(__file__).read_bytes())
    identity["catalog_sha256"] = digest(json.dumps(catalog, sort_keys=True).encode())
    (output / "identity.json").write_text(json.dumps(identity, indent=2) + "\n")
    home = output / "home"
    home.mkdir()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "PYTHONPATH": str(tree),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "UV_PROJECT_ENVIRONMENT": str(output / "venv"),
        "UV_CACHE_DIR": str(cache or output / "cache"),
        "REFLECTION_REPLAY_ROOT": str(tree),
    }
    if (
        run_command(["uv", "sync", "--locked", "--all-groups"], tree, env, 300, output / "sync.log")
        != 0
    ):
        raise ReplayError("Dependency setup failed; inspect sync.log")
    paths = list(identity["files"])
    results: list[dict[str, Any]] = []
    for probe in selected:
        row: dict[str, Any] = {"id": probe["id"], "input_identity": identity["input_identity"]}

        def phase(name: str, probe: Mapping[str, Any] = probe) -> dict[str, Any]:
            report = output / f"{probe['id']}.{name}.xml"
            command = [
                "uv",
                "run",
                "--offline",
                "--no-sync",
                "pytest",
                "-p",
                PLUGIN,
                "-p",
                "anyio.pytest_plugin",
                "-q",
                "--tb=short",
                "--strict-markers",
                "-p",
                "no:cacheprovider",
                f"--junitxml={report}",
                *probe["nodes"],
            ]
            code = run_command(command, tree, env, timeout, output / f"{probe['id']}.{name}.log")
            return inspect_report(
                report,
                code,
                probe["nodes"],
                semantic=probe["semantic"],
                allowed_failures=probe["allowed_failures"],
            )

        try:
            if file_hashes(tree, paths) != identity["files"]:
                raise ReplayError("Unexpected snapshot input changes before probe")
            row["before"] = phase("before")
            if not row["before"]["green"]:
                raise ReplayError("Baseline is not GREEN")
            with mutation(tree, probe["edits"]) as patch:
                (output / f"{probe['id']}.patch").write_text(patch)
                row["red"] = phase("red")
            if file_hashes(tree, paths) != identity["files"]:
                raise ReplayError("Snapshot input changes after restoration")
            row["restored"] = phase("restored")
            if file_hashes(tree, paths) != identity["files"]:
                raise ReplayError("Restored run modified snapshot inputs")
            row["outcome"] = (
                "killed-restored-green"
                if row["red"]["killed"] and row["restored"]["green"]
                else "blocked-or-survivor"
            )
        except BaseException as error:
            row["outcome"] = "blocked"
            row["reason"] = type(error).__name__ + ": " + str(error)
            raise
        finally:
            row["restored_exact"] = file_hashes(tree, paths) == identity["files"]
            row["live_source_unchanged"] = file_hashes(source, paths) == identity["files"]
            results.append(row)
            (output / "results.json").write_text(json.dumps(results, indent=2) + "\n")
        print(f"{probe['id']}: {row['outcome']}", flush=True)
        if row["outcome"] != "killed-restored-green" or not row["live_source_unchanged"]:
            raise ReplayError("Replay stopped: unproved kill or changed live inputs")


def _interrupt(signum: int, frame: FrameType | None) -> None:
    raise KeyboardInterrupt(f"Signal {signum}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--ids", nargs="*", default=[])
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--extra", nargs="*", default=[], help="Explicit intentional untracked input files"
    )
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    source = args.source.resolve()
    catalog = json.loads(safe_path(source, CATALOG).read_text())
    counts = validate_catalog(source, catalog)
    print(json.dumps(dict(counts), sort_keys=True))
    if not args.execute:
        for probe in catalog["probes"]:
            print(f"{probe['id']} {probe['classification']}: {probe['invariant']}")
        return
    signal.signal(signal.SIGTERM, _interrupt)
    output = args.output or Path(tempfile.mkdtemp(prefix="reflection-replay-")) / "run"
    execute(source, catalog, args.ids, output.resolve(), args.timeout, args.extra, args.cache_dir)


if __name__ == "__main__":
    main()
