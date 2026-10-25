"""Safety contracts for the portable, isolated Reflection mutation command."""

from __future__ import annotations

import hashlib
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from review.scripts.replay_reflection_mutations import (
    ReplayError,
    execute,
    inspect_report,
    mutation,
    run_command,
    safe_path,
    snapshot,
    verify_imports,
)


def test_mutation_refuses_stale_or_ambiguous_source(tmp_path: Path) -> None:
    target = tmp_path / "policy.py"
    target.write_text("guard\nguard\n")
    edit = {
        "path": "policy.py",
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "kind": "text",
        "old": "guard",
        "new": "relaxed",
        "occurrences": 1,
        "selected": [0],
        "context": "guard",
    }
    with pytest.raises(ReplayError, match="occurrences"), mutation(tmp_path, [edit]):
        pytest.fail("an ambiguous mutation must never be applied")
    edit["occurrences"] = 2
    target.write_text("different")
    with pytest.raises(ReplayError, match="fingerprint"), mutation(tmp_path, [edit]):
        pytest.fail("a stale mutation must never be applied")
    assert target.read_text() == "different"


@pytest.mark.parametrize("relative", ["../outside", "/etc/passwd", ".git/config", ".env"])
def test_snapshot_paths_reject_escape_and_secrets(tmp_path: Path, relative: str) -> None:
    with pytest.raises(ReplayError):
        safe_path(tmp_path, relative)


def test_snapshot_materializes_files_and_rejects_escaping_symlink(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "policy.py").write_text("original")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "policy.py"], check=True)
    destination = tmp_path / "snapshot"
    identity = snapshot(source, destination, [])
    (destination / "policy.py").write_text("mutant")
    assert (source / "policy.py").read_text() == "original"
    assert identity["files"]["policy.py"] == hashlib.sha256(b"original").hexdigest()
    outside = tmp_path / "secret"
    outside.write_text("secret")
    (source / "link.py").symlink_to(outside)
    with pytest.raises(ReplayError, match="escape"):
        snapshot(source, tmp_path / "unsafe", ["link.py"])


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, subprocess.TimeoutExpired])
def test_interruption_restores_exact_bytes(tmp_path: Path, interrupt: type[BaseException]) -> None:
    target = tmp_path / "policy.py"
    original = b"guard\r\n"
    target.write_bytes(original)
    edit = {
        "path": "policy.py",
        "sha256": hashlib.sha256(original).hexdigest(),
        "kind": "text",
        "old": "guard",
        "new": "relaxed",
        "occurrences": 1,
        "selected": [0],
        "context": "guard",
    }
    error = (
        subprocess.TimeoutExpired("pytest", 1)
        if interrupt is subprocess.TimeoutExpired
        else KeyboardInterrupt()
    )
    with pytest.raises(interrupt), mutation(tmp_path, [edit]):
        assert target.read_bytes() == b"relaxed\r\n"
        raise error
    assert target.read_bytes() == original


def test_collection_or_setup_error_and_checksum_failure_are_not_kills(tmp_path: Path) -> None:
    report = tmp_path / "result.xml"
    for body in (
        '<testcase classname="tests.test_policy" name="test_guard"><error>setup</error></testcase>',
        '<testcase classname="tests.test_policy" name="test_guard">'
        "<failure>ImportError</failure></testcase>",
        '<testcase classname="tests.test_policy" name="test_guard">'
        "<failure>AssertionError: checksum mismatch</failure></testcase>",
    ):
        report.write_text(f"<testsuites><testsuite>{body}</testsuite></testsuites>")
        assert not inspect_report(report, 1, ["tests/test_policy.py::test_guard"], semantic=True)[
            "killed"
        ]
    report.write_text(
        '<testsuites><testsuite><testcase classname="tests.test_policy" name="test_guard">'
        "<failure>AssertionError: policy source missing</failure>"
        "</testcase></testsuite></testsuites>"
    )
    assert inspect_report(report, 1, ["tests/test_policy.py::test_guard"], semantic=True)["killed"]


def test_import_provenance_rejects_live_checkout(tmp_path: Path) -> None:
    with pytest.raises(ReplayError, match="outside"):
        verify_imports(tmp_path, {"app.policy": Path(__file__)})
    verify_imports(
        tmp_path, {"app.policy": tmp_path / "app/policy.py", "json": Path(sys.executable)}
    )


def test_timed_out_process_is_awaited_before_restoring_source(tmp_path: Path) -> None:
    target = tmp_path / "policy.py"
    original = b"guard\n"
    target.write_bytes(original)
    edit = {
        "path": "policy.py",
        "sha256": hashlib.sha256(original).hexdigest(),
        "kind": "text",
        "old": "guard",
        "new": "relaxed",
        "occurrences": 1,
        "selected": [0],
        "context": "guard",
    }
    child = "import time; time.sleep(5); open('policy.py', 'w').write('late mutant')"
    with pytest.raises(subprocess.TimeoutExpired), mutation(tmp_path, [edit]):
        run_command([sys.executable, "-c", child], tmp_path, {}, 0.05, tmp_path / "child.log")
    assert target.read_bytes() == original


def test_existing_snapshot_cannot_be_reused_for_changed_identity(tmp_path: Path) -> None:
    output = tmp_path / "previous-replay"
    output.mkdir()
    (output / "identity.json").write_text('{"input_identity":"old-source"}')
    with pytest.raises(ReplayError, match="never resumes"):
        execute(tmp_path, {"probes": []}, [], output, 1, [], None)


@pytest.mark.parametrize(
    ("message", "trace", "expected"),
    [
        (
            "assert 'escaped check' in prompt.user",
            "E   assert 'escaped check' in prompt.user",
            True,
        ),
        (
            "DID NOT RAISE <class 'TypeError'>",
            "with pytest.raises(TypeError):\nE   Failed: DID NOT RAISE <class 'TypeError'>",
            True,
        ),
        ("TypeError: broken fake", "E   TypeError: broken fake", False),
        (
            "RuntimeError: broken fake",
            "assert 'AssertionError' in source\nE   RuntimeError: broken fake",
            False,
        ),
        (
            "RuntimeError: AssertionError appeared in provider response",
            "E   RuntimeError: AssertionError appeared in provider response",
            False,
        ),
    ],
)
def test_rewritten_assertion_kill_uses_actual_failure_family(
    tmp_path: Path, message: str, trace: str, expected: bool
) -> None:
    report = tmp_path / "result.xml"
    suite = ET.Element("testsuite")
    case = ET.SubElement(suite, "testcase", classname="tests.test_policy", name="test_guard")
    ET.SubElement(case, "failure", message=message).text = trace
    ET.ElementTree(suite).write(report)
    nodes = ["tests/test_policy.py::test_guard"]
    assert inspect_report(report, 1, nodes)["killed"] is expected
    assert not inspect_report(report, 1, nodes, allowed_failures=[])["killed"]
