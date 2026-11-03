"""Public CLI contract for the issue #30 case validator: main-corpus checks."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from review.scripts import validate_dataset
from tests.validate_dataset_fakes import PATCH, ROOT, _case, _git_env, _metadata, _run


def _quoted_utf8_patch(tmp_path: Path) -> str:
    """``git diff`` of ``app/café.py`` from a scratch repository with ``core.quotePath``."""
    repo = tmp_path / "source-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], env=_git_env(), check=True)
    subprocess.run(
        ["git", "config", "core.quotePath", "true"], cwd=repo, env=_git_env(), check=True
    )
    source = repo / "app" / "café.py"
    source.parent.mkdir()
    source.write_text("def f():\n    pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "app/café.py"], cwd=repo, env=_git_env(), check=True)
    source.write_text("def f():\n    return 1\n    pass\n", encoding="utf-8")
    generated = subprocess.run(
        ["git", "diff", "--", "app/café.py"],
        cwd=repo,
        env=_git_env(),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert '+++ "b/app/caf\\303\\251.py"' in generated
    return generated


def _repo_state(repo: Path) -> tuple[str, str]:
    """Local config and index of ``repo``, named by flags; no ``GIT_*`` key is inherited.

    An exported ``GIT_INDEX_FILE`` would otherwise redirect ``ls-files`` to another index.
    """
    git = ["git", f"--git-dir={repo / '.git'}", f"--work-tree={repo}"]
    config = subprocess.run(
        [*git, "config", "--local", "--list"],
        env=_git_env(),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    files = subprocess.run(
        [*git, "ls-files"], env=_git_env(), capture_output=True, text=True, check=True
    ).stdout
    return config, files


def test_valid_case_needs_no_recorded_response(tmp_path: Path) -> None:
    dataset = _case(tmp_path, _metadata())

    result = _run(dataset, "--case", "SEC-01")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK SEC-01" in result.stdout


def test_patch_applies_under_hidden_upstream_directory(tmp_path: Path) -> None:
    metadata = _metadata()
    metadata["apply_directory"] = ".upstream"
    dataset = _case(tmp_path, metadata)
    case = dataset / "cases" / "SEC-01"
    source = case / "base" / "app" / "example.py"
    upstream = case / "base" / ".upstream" / "app" / "example.py"
    upstream.parent.mkdir(parents=True)
    source.rename(upstream)

    result = _run(dataset, "--case", "SEC-01")

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("directory", ["../outside", "/tmp/outside"])
def test_rejects_apply_directory_outside_base(tmp_path: Path, directory: str) -> None:
    metadata = _metadata()
    metadata["apply_directory"] = directory
    dataset = _case(tmp_path, metadata)

    result = _run(dataset, "--case", "SEC-01")

    assert result.returncode != 0
    assert "apply_directory" in result.stdout


@pytest.mark.parametrize(
    ("change", "diagnostic"),
    [
        ({"class": "unknown"}, "class"),
        ({"language": "unknown"}, "language"),
        ({"expected_verdict": "APPROVE"}, "expected_verdict"),
        ({"base_path": None}, "base_path"),
        ({"expected_findings": [{"path": "app/example.py", "line": 2}]}, "severity"),
    ],
)
def test_schema_rejects_invalid_or_missing_fields(
    tmp_path: Path, change: dict[str, Any], diagnostic: str
) -> None:
    metadata = _metadata()
    metadata.update(change)
    dataset = _case(tmp_path, metadata)

    result = _run(dataset, "--case", "SEC-01")

    assert result.returncode != 0
    assert "SEC-01" in result.stdout
    assert diagnostic in result.stdout


def test_real_source_requires_license_and_revision(tmp_path: Path) -> None:
    metadata = _metadata()
    metadata["source"] = {"kind": "real", "url": "https://example.org/pr/1"}
    dataset = _case(tmp_path, metadata)

    result = _run(dataset, "--case", "SEC-01")

    assert result.returncode != 0
    assert "license_name" in result.stdout
    assert "revision" in result.stdout


def test_rejects_invalid_range_anchor(tmp_path: Path) -> None:
    metadata = _metadata()
    metadata["expected_findings"][0]["start_line"] = 2
    dataset = _case(tmp_path, metadata)

    result = _run(dataset, "--case", "SEC-01")

    assert result.returncode != 0
    assert "start_line" in result.stdout


def test_rejects_malformed_patch(tmp_path: Path) -> None:
    dataset = _case(tmp_path, _metadata(), patch="not a unified diff\n")

    result = _run(dataset, "--case", "SEC-01")

    assert result.returncode != 0
    assert "patch" in result.stdout


def test_rejects_missing_base_file(tmp_path: Path) -> None:
    dataset = _case(tmp_path, _metadata())
    (dataset / "cases" / "SEC-01" / "base" / "app" / "example.py").unlink()

    result = _run(dataset, "--case", "SEC-01")

    assert result.returncode != 0
    assert "base" in result.stdout


def test_rejects_truth_outside_new_side_added_lines(tmp_path: Path) -> None:
    metadata = _metadata()
    metadata["expected_findings"][0]["line"] = 1
    dataset = _case(tmp_path, metadata)

    result = _run(dataset, "--case", "SEC-01")

    assert result.returncode != 0
    assert "added line" in result.stdout


def test_rejects_wrong_derived_verdict(tmp_path: Path) -> None:
    metadata = _metadata()
    metadata["expected_verdict"] = "clean"
    dataset = _case(tmp_path, metadata)

    result = _run(dataset, "--case", "SEC-01")

    assert result.returncode != 0
    assert "expected_verdict" in result.stdout


def test_category_exception_needs_a_recorded_reason(tmp_path: Path) -> None:
    metadata = _metadata()
    metadata["expected_findings"][0]["category"] = "correctness"
    dataset = _case(tmp_path, metadata)

    result = _run(dataset, "--case", "SEC-01")

    assert result.returncode != 0
    assert "category_reason" in result.stdout


def test_final_mode_reports_incomplete_distribution(tmp_path: Path) -> None:
    dataset = _case(tmp_path, _metadata())

    result = _run(dataset, "--final")

    assert result.returncode != 0
    assert "cases: 1" in result.stdout
    assert "real cases" in result.stdout


def test_final_json_report_has_deterministic_distribution_counts(tmp_path: Path) -> None:
    dataset = _case(tmp_path, _metadata())
    report = tmp_path / "reports" / "distribution.json"

    first = _run(dataset, "--final", "--report-json", str(report))
    first_bytes = report.read_bytes()
    second = _run(dataset, "--final", "--report-json", str(report))
    data = json.loads(report.read_text(encoding="utf-8"))

    assert first.returncode == second.returncode == 1
    assert report.read_bytes() == first_bytes
    assert data["case_count"] == 1
    assert data["valid_case_count"] == 1
    assert data["class_counts"] == {
        "security": 1,
        "resource": 0,
        "logic": 0,
        "syntax": 0,
        "clean": 0,
    }
    assert data["real_case_count"] == 0
    assert data["distinct_real_source_count"] == 0
    assert data["critical_truth_count"] == 1
    assert data["language_counts"] == {"python": 1, "typescript": 0, "tsx": 0}
    assert data["passed"] is False


def test_final_json_report_excludes_invalid_case_from_truth_counts(tmp_path: Path) -> None:
    metadata = _metadata()
    metadata["expected_verdict"] = "clean"
    dataset = _case(tmp_path, metadata)
    report = tmp_path / "distribution.json"

    result = _run(dataset, "--final", "--report-json", str(report))
    data = json.loads(report.read_text(encoding="utf-8"))

    assert result.returncode == 1
    assert data["case_count"] == 1
    assert data["valid_case_count"] == 0
    assert data["critical_truth_count"] == 0
    assert data["class_counts"]["security"] == 0
    assert any("SEC-01" in error for error in data["errors"])


def test_json_report_requires_final_mode(tmp_path: Path) -> None:
    dataset = _case(tmp_path, _metadata())
    report = tmp_path / "distribution.json"

    result = _run(dataset, "--case", "SEC-01", "--report-json", str(report))

    assert result.returncode == 2
    assert "--report-json requires --final" in result.stderr
    assert not report.exists()


def test_committed_corpus_final_json_report_passes(tmp_path: Path) -> None:
    report = tmp_path / "distribution.json"

    result = _run(ROOT / "test-prs-dataset", "--final", "--report-json", str(report))
    data = json.loads(report.read_text(encoding="utf-8"))

    assert result.returncode == 0, result.stdout + result.stderr
    assert data["passed"] is True
    assert data["errors"] == []
    assert data["case_count"] == data["valid_case_count"] == 24
    assert data["class_counts"] == {
        "security": 4,
        "resource": 5,
        "logic": 5,
        "syntax": 5,
        "clean": 5,
    }
    assert data["real_case_count"] == data["distinct_real_source_count"] == 5
    assert all(count == 1 for count in data["real_class_counts"].values())
    assert data["critical_truth_count"] == 5


def test_rejects_case_directory_symlink_without_reading_external_data(tmp_path: Path) -> None:
    outside = _case(tmp_path / "outside", _metadata()) / "cases" / "SEC-01"
    (outside / "case.json").write_text('"EXTERNAL_SECRET_MARKER"', encoding="utf-8")
    dataset = tmp_path / "dataset"
    (dataset / "cases").mkdir(parents=True)
    (dataset / "cases" / "SEC-01").symlink_to(outside, target_is_directory=True)

    result = _run(dataset, "--case", "SEC-01")

    assert result.returncode != 0
    assert "case directory" in result.stdout
    assert "EXTERNAL_SECRET_MARKER" not in result.stdout + result.stderr


def test_rejects_case_json_symlink_without_reading_external_data(tmp_path: Path) -> None:
    dataset = _case(tmp_path, _metadata())
    outside = tmp_path / "outside.json"
    outside.write_text('"EXTERNAL_SECRET_MARKER"', encoding="utf-8")
    metadata = dataset / "cases" / "SEC-01" / "case.json"
    metadata.unlink()
    metadata.symlink_to(outside)

    result = _run(dataset, "--case", "SEC-01")

    assert result.returncode != 0
    assert "case.json" in result.stdout
    assert "EXTERNAL_SECRET_MARKER" not in result.stdout + result.stderr


def test_rejects_cases_root_symlink(tmp_path: Path) -> None:
    outside_cases = _case(tmp_path / "outside", _metadata()) / "cases"
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    (dataset / "cases").symlink_to(outside_cases, target_is_directory=True)

    result = _run(dataset, "--case", "SEC-01")

    assert result.returncode != 0
    assert "cases directory" in result.stdout


@pytest.mark.parametrize("quoted", [False, True])
def test_accepts_valid_patch_filename_with_spaces(tmp_path: Path, quoted: bool) -> None:
    metadata = _metadata()
    metadata["expected_findings"][0]["path"] = "app/name with space.py"
    if quoted:
        old, new = '"a/app/name with space.py"', '"b/app/name with space.py"'
    else:
        old, new = "a/app/name with space.py", "b/app/name with space.py"
    patch = (
        f"diff --git {old} {new}\n"
        f"--- {old}\n"
        f"+++ {new}\n"
        "@@ -1,2 +1,3 @@\n"
        " def f():\n"
        "+    return 1\n"
        "     pass\n"
    )
    dataset = _case(tmp_path, metadata, patch=patch)
    (dataset / "cases" / "SEC-01" / "base" / "app" / "name with space.py").write_text(
        "def f():\n    pass\n", encoding="utf-8"
    )

    result = _run(dataset, "--case", "SEC-01")

    assert result.returncode == 0, result.stdout + result.stderr


def test_accepts_git_octal_quoted_utf8_filename(tmp_path: Path) -> None:
    generated = _quoted_utf8_patch(tmp_path)

    metadata = _metadata()
    metadata["expected_findings"][0]["path"] = "app/café.py"
    dataset = _case(tmp_path, metadata, patch=generated)
    (dataset / "cases" / "SEC-01" / "base" / "app" / "café.py").write_text(
        "def f():\n    pass\n", encoding="utf-8"
    )

    result = _run(dataset, "--case", "SEC-01")

    assert result.returncode == 0, result.stdout + result.stderr


def test_quoted_patch_steps_leave_an_exported_git_dir_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decoy = tmp_path / "decoy"
    subprocess.run(["git", "init", "-q", str(decoy)], env=_git_env(), check=True)
    (decoy / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=decoy, env=_git_env(), check=True)
    before = _repo_state(decoy)
    assert before[1] == "tracked.txt\n"

    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    _quoted_utf8_patch(tmp_path)

    assert _repo_state(decoy) == before


def test_patch_check_does_not_read_an_exported_git_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A trailing blank on the added line: git's default only warns about it.
    dataset = _case(tmp_path, _metadata(), patch=PATCH.replace("return 1\n", "return 1 \n"))
    decoy = tmp_path / "decoy"
    subprocess.run(["git", "init", "-q", str(decoy)], env=_git_env(), check=True)
    subprocess.run(
        ["git", f"--git-dir={decoy / '.git'}", "config", "apply.whitespace", "error"],
        env=_git_env(),
        check=True,
    )

    without = _run(dataset, "--case", "SEC-01")
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    exported = _run(dataset, "--case", "SEC-01")

    assert without.returncode == 0, without.stdout + without.stderr
    assert exported.returncode == 0, exported.stdout + exported.stderr


def test_rejects_duplicate_truth_anchors_and_does_not_count_them_as_critical(
    tmp_path: Path,
) -> None:
    metadata = _metadata()
    metadata["expected_findings"] *= 5
    dataset = _case(tmp_path, metadata)

    single = _run(dataset, "--case", "SEC-01")
    final = _run(dataset, "--final")

    assert single.returncode != 0
    assert "duplicate" in single.stdout
    assert final.returncode != 0
    assert "needs ≥5 critical findings" in final.stdout


def test_patch_check_ignores_a_git_work_tree_above_the_temp_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A context line the base does not have: ``git apply --check`` must reject it. Inside an
    # outer work tree git applies only what the index tracks and exits 0 for the copy.
    stale = PATCH.replace(" def f():", " def g():")
    dataset = _case(tmp_path, _metadata(), patch=stale)
    outer = tmp_path / "outer"
    subprocess.run(["git", "init", "-q", str(outer)], env=_git_env(), check=True)
    scratch = outer / "tmp"
    scratch.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch))

    result = _run(dataset, "--case", "SEC-01")

    assert result.returncode != 0, result.stdout + result.stderr
    assert "patch_path" in result.stdout


@pytest.mark.parametrize("separator", [" ", "\x0c", "\r"])
def test_added_line_numbers_split_on_lf_only(tmp_path: Path, separator: str) -> None:
    patch = tmp_path / "diff.patch"
    patch.write_bytes(
        (
            "diff --git a/app/example.py b/app/example.py\n"
            "--- a/app/example.py\n"
            "+++ b/app/example.py\n"
            "@@ -1,2 +1,5 @@\n"
            " def f():\n"
            f"+    a = 1{separator}+    b = 2\n"
            f"+    c = 3{separator} d = 4\n"
            "+    return 1\n"
            "     pass\n"
        ).encode()
    )

    assert validate_dataset._patch_added_lines(patch) == {"app/example.py": {2, 3, 4}}


def test_changed_paths_split_on_lf_only(tmp_path: Path) -> None:
    name = "app/a b.py"
    patch = tmp_path / "diff.patch"
    patch.write_bytes(
        (
            f"diff --git a/{name} b/{name}\n"
            "new file mode 100644\n"
            f"--- /dev/null\n+++ b/{name}\n"
            "@@ -0,0 +1 @@\n+x\n"
        ).encode()
    )

    assert validate_dataset._patch_changed_paths(patch) == [name]


def test_helpers_read_a_crlf_patch_without_the_carriage_returns(tmp_path: Path) -> None:
    patch = tmp_path / "diff.patch"
    patch.write_bytes(
        b"diff --git a/app/example.py b/app/example.py\r\n"
        b"--- a/app/example.py\r\n"
        b"+++ b/app/example.py\r\n"
        b"@@ -1,2 +1,3 @@\r\n"
        b" def f():\r\n"
        b"+    return 1\r\n"
        b"     pass\r\n"
    )

    assert validate_dataset._patch_added_lines(patch) == {"app/example.py": {2}}
    assert validate_dataset._patch_changed_paths(patch) == ["app/example.py"]


def test_helpers_skip_the_no_newline_marker(tmp_path: Path) -> None:
    patch = tmp_path / "diff.patch"
    patch.write_bytes(
        b"diff --git a/app/example.py b/app/example.py\n"
        b"--- a/app/example.py\n"
        b"+++ b/app/example.py\n"
        b"@@ -1,2 +1,3 @@\n"
        b" def f():\n"
        b"+    return 1\n"
        b"-    pass\n"
        b"\\ No newline at end of file\n"
        b"+    pass\n"
        b"\\ No newline at end of file\n"
    )

    assert validate_dataset._patch_added_lines(patch) == {"app/example.py": {2, 3}}
    assert validate_dataset._patch_changed_paths(patch) == ["app/example.py"]


def test_a_patch_that_is_not_utf8_is_an_error_not_a_pass(tmp_path: Path) -> None:
    dataset = _case(tmp_path, _metadata())
    (dataset / "cases" / "SEC-01" / "diff.patch").write_bytes(
        PATCH.replace("return 1", "return 1  # ÿ").encode("latin-1")
    )

    result = _run(dataset, "--case", "SEC-01")

    assert result.returncode != 0, result.stdout + result.stderr
    assert "cannot read unified patch" in result.stdout
