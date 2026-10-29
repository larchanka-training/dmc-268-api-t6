"""The docs size budget workflow (#380): a warning for a PR that grows a top-level docs/*.md
file over the budget, run on a depth-2 clone of a real PR merge commit, never a failure.
"""

from __future__ import annotations

import os
import re
import subprocess
import textwrap
from collections.abc import Callable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github/workflows/docs-size-budget.yml"
REQUIRED_CHECKS = {
    "OpenAPI lint",
    "Python lint / type / test",
    "Webhook container smoke",
    "Edge Caddyfile validate",
}


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _budget() -> int:
    match = re.search(r'(?m)^  DOCS_SIZE_BUDGET_BYTES: "(\d+)"$', _workflow())
    assert match is not None
    return int(match.group(1))


def _step() -> str:
    step = _workflow().split("      - name: Compare top-level docs sizes\n", 1)[1]
    body = step.split("        run: |\n", 1)[1].split("\n      - name: ", 1)[0]
    return textwrap.dedent(body)


def _git_env() -> dict[str, str]:
    """No inherited ``GIT_*`` key (a hook's ``GIT_INDEX_FILE``) and no user or system config."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env |= {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.test",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.test",
    }
    return env


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, env=_git_env(), check=True, capture_output=True)


def _write(repo: Path, path: str, size: int) -> None:
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_bytes(b"x" * size)


def _run(tmp_path: Path, base: dict[str, int], change: Callable[[Path], None]) -> tuple[str, str]:
    """Merge a PR branch with ``--no-ff`` as GitHub does, clone with depth 2, run the step."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "--initial-branch=main")
    for path, size in {**base, "README.md": 1}.items():
        _write(origin, path, size)
    _git(origin, "add", "-A")
    _git(origin, "commit", "-qm", "base")
    _git(origin, "checkout", "-qb", "pr")
    change(origin)
    _git(origin, "add", "-A")
    _git(origin, "commit", "-qm", "pr")
    _git(origin, "checkout", "-q", "main")
    _write(origin, "README.md", 2)
    _git(origin, "commit", "-qam", "main moves on")
    _git(origin, "merge", "-q", "--no-ff", "-m", "merge", "pr")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", "--depth", "2", origin.as_uri(), str(clone))

    summary = tmp_path / "summary.md"
    result = subprocess.run(
        ["bash", "-e", "-c", _step()],
        cwd=clone,
        env={
            **_git_env(),
            "DOCS_SIZE_BUDGET_BYTES": str(_budget()),
            "GITHUB_STEP_SUMMARY": str(summary),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout, summary.read_text(encoding="utf-8")


def test_workflow_is_a_separate_non_required_check_with_an_80_kib_budget() -> None:
    workflow = _workflow()
    assert _budget() == 80 * 1024
    assert "    name: Docs size budget\n" in workflow
    assert "Docs size budget" not in REQUIRED_CHECKS
    assert '      - "docs/*.md"\n' in workflow
    assert "permissions:\n  contents: read\n" in workflow
    assert "fetch-depth: 2" in workflow
    assert "${{" not in _step()


def test_warns_for_top_level_docs_that_grew_over_the_budget(tmp_path: Path) -> None:
    budget = _budget()
    base = {
        "docs/grow_over.md": budget - 900,
        "docs/grow_under.md": 1000,
        "docs/shrink_over.md": budget + 8000,
        "docs/at_budget.md": budget - 1000,
        "docs/cross.md": budget,
        "docs/rename_src.md": budget + 1,
        "docs/with space.md": budget - 100,
        "docs/deleted_over.md": budget + 1,
        "docs/sub/nested_over.md": budget - 900,
        "docs/big.txt": 10,
    }

    def change(repo: Path) -> None:
        _write(repo, "docs/grow_over.md", budget + 100)
        _write(repo, "docs/grow_under.md", 2000)
        _write(repo, "docs/shrink_over.md", budget + 4000)
        _write(repo, "docs/at_budget.md", budget)
        _write(repo, "docs/cross.md", budget + 1)
        _write(repo, "docs/new_over.md", budget + 100)
        _write(repo, "docs/with space.md", budget + 100)
        _write(repo, "docs/sub/nested_over.md", budget + 100)
        _write(repo, "docs/big.txt", budget + 100)
        _git(repo, "mv", "docs/rename_src.md", "docs/rename_dst.md")
        _git(repo, "rm", "-q", "docs/deleted_over.md")

    stdout, summary = _run(tmp_path, base, change)

    warned = re.findall(r"(?m)^::warning file=([^:]+)::", stdout)
    assert warned == [
        "docs/cross.md",
        "docs/grow_over.md",
        "docs/new_over.md",
        "docs/rename_dst.md",
        "docs/with space.md",
    ]
    assert (
        f"::warning file=docs/grow_over.md::docs/grow_over.md grew from {budget - 900} "
        f"to {budget + 100} bytes, over the {budget}-byte budget (#380)"
    ) in stdout
    grown = f"| `docs/grow_over.md` | {budget - 900} | {budget + 100} | +1000 | {budget} |"
    assert grown in summary
    assert f"| `docs/new_over.md` | 0 | {budget + 100} | +{budget + 100} | {budget} |" in summary
    assert summary.count("\n| `docs/") == len(warned)


def test_a_pr_without_a_tripped_top_level_doc_writes_one_summary_line(tmp_path: Path) -> None:
    budget = _budget()

    def change(repo: Path) -> None:
        _write(repo, "docs/sub/nested_over.md", budget + 100)
        _write(repo, "docs/small.md", 10)

    stdout, summary = _run(tmp_path, {"docs/sub/nested_over.md": 1}, change)

    assert "::warning" not in stdout
    assert summary == f"No top-level docs/*.md file grew over the {budget}-byte budget.\n"
