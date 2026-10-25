"""Contract tests for scripts/issue_graph.py.

Offline: no network, no tokens, no live `gh`. An autouse guard puts a fake `gh`
first on PATH that fails with exit 97, so any request that escapes the fixture
seam (`ISSUE_GRAPH_FIXTURE_DIR`) or a test's own fake is an error, never a call.

Covers: byte-exact golden reports for six GraphQL fixture sets (a cycle, a
milestone found in one repo only, a full sprint milestone, an epic, nested
pagination, explicit issues across both repos), label stripping/cutting/escaping,
REF parsing, the one-mode rule, the cycle walk, exit codes 2 and 3, and the argv
the script hands to `gh`; and `--publish-to`: the marker key, comment paging and
the first-line match, the page cap, create vs update in the target's own repo, the
cycle and size guards, 404 vs 403 vs other REST failures, an empty fixture seam,
and the write log that replaces every write in fixture mode.
"""
# SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/issue-graph/tests/test_issue_graph.py

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "scripts" / "issue_graph.py"
FIXTURES = HERE / "fixtures"
GOLDEN = HERE / "golden"
API = "larchanka-training/dmc-268-api-t6"
UI = "larchanka-training/dmc-268-ui-t6"


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # dataclasses resolve string annotations through sys.modules[__module__].
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ig = _load(SCRIPT, "issue_graph")


def fake_gh(bin_dir: Path, body: str) -> Path:
    """Write an executable `gh` (a Python script) into bin_dir."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    gh.chmod(0o755)
    return bin_dir


def prepend_path(monkeypatch: pytest.MonkeyPatch, bin_dir: Path) -> None:
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")


@pytest.fixture(autouse=True)
def no_live_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts with a `gh` that refuses to run, no fixture dir and no write log."""
    guard = fake_gh(
        tmp_path / "guard-bin",
        'import sys\nsys.stderr.write("unexpected gh call\\n")\nsys.exit(97)\n',
    )
    prepend_path(monkeypatch, guard)
    monkeypatch.delenv("ISSUE_GRAPH_FIXTURE_DIR", raising=False)
    monkeypatch.delenv("ISSUE_GRAPH_WRITE_LOG", raising=False)


def run(*args: str, fixtures: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if fixtures is not None:
        env["ISSUE_GRAPH_FIXTURE_DIR"] = str(fixtures)
    # A paging bug loops forever on a fixture page that always has a next page.
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        encoding="utf-8",
        env=env,
        timeout=60,
    )


def error_of(r: subprocess.CompletedProcess[str]) -> str:
    """stderr is exactly one JSON line `{"error": "..."}`; stdout stays empty."""
    assert r.stdout == "", "stdout must carry the report only"
    assert "Traceback" not in r.stderr
    assert r.stderr.count("\n") == 1, r.stderr
    message = json.loads(r.stderr)["error"]
    assert isinstance(message, str)
    return message


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


PAGE_END: dict[str, Any] = {"hasNextPage": False, "endCursor": None}


# ─── golden reports ─────────────────────────────────────────────────────────
CASES: list[tuple[str, list[str], int]] = [
    ("cycle", ["--issues", "api#1", "api#2", "api#3", "api#4"], 1),
    ("no-links", ["--milestone", "Спринт 9"], 0),
    ("milestone", ["--milestone", "Спринт 5"], 0),
    ("epic", ["--epic", "api#139"], 0),
    ("pagination", ["--milestone", "Спринт 7", "--repo", API], 0),
    ("issues", ["--issues", "ui#9", "api#31", "api#32", "api#30"], 0),
]


def golden_body(name: str) -> str:
    """A golden is a SYNC line, a blank line, then the report. Drop exactly those
    two lines and keep the rest byte for byte: no strip, no normalisation. A
    missing golden is an error — it is never written on the fly."""
    text = (GOLDEN / f"{name}.md").read_bytes().decode("utf-8")
    return text.split("\n", 2)[2]


@pytest.mark.parametrize("case,args,code", CASES, ids=[c[0] for c in CASES])
def test_golden_report(case: str, args: list[str], code: int) -> None:
    r = run(*args, fixtures=FIXTURES / case)
    assert r.stderr == ""
    assert r.returncode == code
    assert r.stdout == golden_body(case), f"{case} report drifted"


@pytest.mark.parametrize("case,args,code", CASES, ids=[c[0] for c in CASES])
def test_report_does_not_depend_on_the_hash_seed(
    case: str, args: list[str], code: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Edges live in a set, whose order follows the per-process string hash. A list
    that is not sorted passes under some seeds only; fixed seeds make that a
    deterministic failure instead of a flaky one."""
    for seed in range(8):
        monkeypatch.setenv("PYTHONHASHSEED", str(seed))
        r = run(*args, fixtures=FIXTURES / case)
        assert r.returncode == code
        assert r.stdout == golden_body(case), f"{case} drifted under PYTHONHASHSEED={seed}"


def test_every_fixture_set_has_a_golden_and_a_case() -> None:
    names = sorted(c[0] for c in CASES)
    assert sorted(p.name for p in FIXTURES.iterdir() if p.is_dir()) == names
    assert sorted(p.stem for p in GOLDEN.glob("*.md")) == names


def test_every_golden_carries_the_sync_header() -> None:
    files = sorted(GOLDEN.glob("*.md"))
    assert files, "no golden files found"
    for path in files:
        first, second, body = path.read_bytes().decode("utf-8").split("\n", 2)
        assert first == (
            "<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/issue-graph/"
            f"tests/golden/{path.name} -->"
        )
        assert second == "", path.name
        assert body.endswith("\n") and not body.endswith("\n\n"), path.name


# ─── mermaid label ──────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "title,expected",
    [
        ("[rag][139] RagContextProvider", "api#35;7 RagContextProvider"),
        ("  [a] [b]  Spaced out  ", "api#35;7 Spaced out"),
        ("Инженер 3 - [reviews] Добавьте", "api#35;7 Инженер 3 - [reviews] Добавьте"),
        ("x" * 40, "api#35;7 " + "x" * 40),
        ("x" * 41, "api#35;7 " + "x" * 39 + "…"),
        ("[tag] " + "x" * 41, "api#35;7 " + "x" * 39 + "…"),
        ('A & B # "q" <x>', "api#35;7 A #amp; B #35; #quot;q#quot; #lt;x#gt;"),
        ("&#", "api#35;7 #amp;#35;"),
        # cut first, escape after: 40 characters stay, both specials survive
        ("y" * 38 + "#&", "api#35;7 " + "y" * 38 + "#35;#amp;"),
        # 41 characters: the specials past position 39 are cut away
        ("y" * 39 + "<>", "api#35;7 " + "y" * 39 + "…"),
    ],
)
def test_label_strips_cuts_and_escapes(title: str, expected: str) -> None:
    assert ig.label((API, 7), title) == expected


def test_label_of_another_repo_uses_owner_name() -> None:
    assert ig.label(("octo/tools", 5), "Export") == "octo/tools#35;5 Export"
    assert ig.label((UI, 12), "[ui] Badge") == "ui#35;12 Badge"


# ─── REF parsing ────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "text,key",
    [
        ("api#5", (API, 5)),
        ("ui#7", (UI, 7)),
        ("o/n#3", ("o/n", 3)),
        ("octo/tools.js#12", ("octo/tools.js", 12)),
    ],
)
def test_parse_ref(text: str, key: tuple[str, int]) -> None:
    assert ig.parse_ref(text) == key


@pytest.mark.parametrize(
    "bad",
    [
        "api5",
        "api#",
        "api#x",
        "web#3",
        "o/n/x#3",
        "#3",
        "api#0",
        "api#3a",
        "o/#3",
        # dot-only path parts would point the REST-style URL elsewhere
        "o/..#3",
        "../n#3",
        "./n#3",
    ],
)
def test_bad_ref_exits_2(bad: str) -> None:
    for mode in ("--issues", "--epic"):
        r = run(mode, bad)
        assert r.returncode == 2
        assert bad in error_of(r)


# ─── CLI: exactly one mode, usage errors ────────────────────────────────────
@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--project", "12"],
        ["--epic", "api#1", "--issues", "api#2"],
        ["--milestone", "Спринт 5", "--epic", "api#1"],
        ["--milestone", "Спринт 5", "--issues", "api#1"],
        ["--issues"],
        ["--epic", "api#1", "--repo", API],
        ["--issues", "api#1", "--repo", API],
        ["--milestone", "Спринт 5", "--repo", "not-a-repo"],
        ["--milestone", "Спринт 5", "--repo", "o/.."],
        ["--milestone", "Спринт 5", "--repo", "./n"],
        ["--milestone", "Спринт 5", "--project", "twelve"],
        ["--milestone", "Спринт 5", "--unknown"],
    ],
)
def test_usage_errors_exit_2_with_json(args: list[str]) -> None:
    r = run(*args)
    assert r.returncode == 2
    assert error_of(r)


def test_help_exits_0() -> None:
    r = run("-h")
    assert r.returncode == 0
    assert "--milestone" in r.stdout
    assert r.stderr == ""


# ─── input that resolves to nothing: exit 2 ─────────────────────────────────
def test_milestone_found_in_no_repo_exits_2(tmp_path: Path) -> None:
    decoy = {"data": {"repository": {"milestones": {"nodes": [{"number": 1, "title": "S 9 x"}]}}}}
    write_json(tmp_path / "milestones-api.json", decoy)
    write_json(tmp_path / "milestones-ui.json", decoy)
    r = run("--milestone", "S 9", fixtures=tmp_path)
    assert r.returncode == 2
    assert "S 9" in error_of(r)


def test_unresolvable_issue_exits_2(tmp_path: Path) -> None:
    write_json(tmp_path / "issues-api.json", {"data": {"repository": {"i5": None}}})
    r = run("--issues", "api#5", fixtures=tmp_path)
    assert r.returncode == 2
    assert "api#5" in error_of(r)


def test_unresolvable_epic_exits_2(tmp_path: Path) -> None:
    write_json(tmp_path / "epic-ui-8-p1.json", {"data": {"repository": {"issue": None}}})
    r = run("--epic", "ui#8", fixtures=tmp_path)
    assert r.returncode == 2
    assert "ui#8" in error_of(r)


# ─── transport failures: exit 3 ─────────────────────────────────────────────
def test_missing_fixture_exits_3(tmp_path: Path) -> None:
    r = run("--issues", "api#1", fixtures=tmp_path)
    assert r.returncode == 3
    assert "issues-api.json" in error_of(r)


def test_graphql_errors_exit_3(tmp_path: Path) -> None:
    write_json(tmp_path / "issues-api.json", {"data": None, "errors": [{"message": "boom"}]})
    r = run("--issues", "api#1", fixtures=tmp_path)
    assert r.returncode == 3
    assert "boom" in error_of(r)


def test_unfaked_gh_call_is_caught_by_the_guard() -> None:
    r = run("--issues", "api#1")
    assert r.returncode == 3
    assert "unexpected gh call" in error_of(r)


def test_gh_failure_exits_3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prepend_path(
        monkeypatch,
        fake_gh(tmp_path / "bin", 'import sys\nsys.stderr.write("HTTP 502\\n")\nsys.exit(1)\n'),
    )
    r = run("--issues", "api#5")
    assert r.returncode == 3
    assert "HTTP 502" in error_of(r)


# ─── live transport: argv handed to gh ──────────────────────────────────────
CANNED_ISSUE: dict[str, Any] = {
    "id": "I_kwDOEIOM8AAAABAPI0005",
    "number": 5,
    "title": "[api] Canned",
    "state": "OPEN",
    "repository": {"nameWithOwner": API},
    "parent": None,
    "blockedBy": {
        "totalCount": 0,
        "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": [],
    },
    "blocking": {
        "totalCount": 0,
        "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": [],
    },
    "projectItems": {"nodes": []},
}


def test_live_transport_is_gh_api_graphql_without_a_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "calls.jsonl"
    canned = tmp_path / "canned.json"
    write_json(canned, {"data": {"repository": {"i5": CANNED_ISSUE}}})
    body = (
        "import json, sys\n"
        f"with open({str(log)!r}, 'a', encoding='utf-8') as f:\n"
        "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        f"with open({str(canned)!r}, encoding='utf-8') as f:\n"
        "    sys.stdout.write(f.read())\n"
    )
    prepend_path(monkeypatch, fake_gh(tmp_path / "rec-bin", body))

    r = run("--issues", "api#5")

    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith(
        "### Граф задач: задачи [api#5](https://github.com/larchanka-training/dmc-268-api-t6/issues/5)\n"
    )
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(calls) == 1
    argv = calls[0]
    assert argv[:2] == ["api", "graphql"]
    assert argv[argv.index("owner=larchanka-training") - 1] == "-F"
    assert argv[argv.index("name=dmc-268-api-t6") - 1] == "-F"
    query = next(a for a in argv if a.startswith("query="))
    assert argv[argv.index(query) - 1] == "-f"
    # `$owner` and the braces reach gh verbatim: no shell expanded or split them.
    assert "$owner" in query
    assert re.search(r"i5: issue\(number: ?5\) ?\{", query)


# ─── NOT_FOUND is input (exit 2), any other GraphQL error is a failure (3) ──
def not_found_error(path: list[str], message: str) -> dict[str, Any]:
    return {
        "type": "NOT_FOUND",
        "path": path,
        "locations": [{"line": 3, "column": 5}],
        "message": message,
    }


# The live shape: a missing alias is null in `data` and named by one NOT_FOUND error.
MISSING_ISSUE: dict[str, Any] = {
    "data": {"repository": {"i99999": None, "i268": dict(CANNED_ISSUE, number=268)}},
    "errors": [
        not_found_error(
            ["repository", "i99999"], "Could not resolve to an Issue with the number of 99999."
        )
    ],
}


def test_not_found_in_a_fixture_exits_2(tmp_path: Path) -> None:
    write_json(tmp_path / "issues-api.json", MISSING_ISSUE)
    r = run("--issues", "api#99999", "api#268", fixtures=tmp_path)
    assert r.returncode == 2
    assert error_of(r) == "issue api#99999 not found"


def test_not_found_from_live_gh_exits_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Live gh exits 1 for NOT_FOUND, with the message on stderr and the full
    response on stdout; the response decides, not the exit code."""
    canned = tmp_path / "canned.json"
    write_json(canned, MISSING_ISSUE)
    body = (
        "import sys\n"
        f"with open({str(canned)!r}, encoding='utf-8') as f:\n"
        "    sys.stdout.write(f.read())\n"
        "sys.stderr.write('gh: Could not resolve to an Issue with the number of 99999.\\n')\n"
        "sys.exit(1)\n"
    )
    prepend_path(monkeypatch, fake_gh(tmp_path / "bin", body))
    r = run("--issues", "api#99999", "api#268")
    assert r.returncode == 2
    assert error_of(r) == "issue api#99999 not found"


def test_missing_repository_is_named(tmp_path: Path) -> None:
    write_json(
        tmp_path / "milestones-larchanka_training_nope.json",
        {
            "data": {"repository": None},
            "errors": [
                not_found_error(
                    ["repository"],
                    "Could not resolve to a Repository with the name 'larchanka-training/nope'.",
                )
            ],
        },
    )
    r = run("--milestone", "Спринт 5", "--repo", "larchanka-training/nope", fixtures=tmp_path)
    assert r.returncode == 2
    assert error_of(r) == "repository larchanka-training/nope not found"


def test_not_found_mixed_with_another_error_exits_3(tmp_path: Path) -> None:
    mixed = dict(MISSING_ISSUE)
    mixed["errors"] = [
        *MISSING_ISSUE["errors"],
        {"type": "FORBIDDEN", "path": ["repository", "i268"], "message": "Resource not accessible"},
    ]
    write_json(tmp_path / "issues-api.json", mixed)
    r = run("--issues", "api#99999", "api#268", fixtures=tmp_path)
    assert r.returncode == 3
    assert "Resource not accessible" in error_of(r)


# ─── cycle walk ─────────────────────────────────────────────────────────────
def k(n: int) -> tuple[str, int]:
    return (API, n)


def test_cycle_walk_backtracks_out_of_a_dead_end() -> None:
    """1 → 2 → 3 dead-ends (3 only returns to 2); the walk backs up and closes
    the cycle through 5 instead of looping between 2 and 3."""
    edges = {(k(1), k(2)), (k(2), k(3)), (k(3), k(2)), (k(2), k(5)), (k(5), k(1))}
    assert ig.find_cycles(edges) == [[k(1), k(2), k(5), k(1)]]


def test_self_loop_is_a_cycle_and_cycles_sort_by_first_key() -> None:
    edges = {(k(9), k(9)), (k(4), k(6)), (k(6), k(4)), (k(1), k(4))}
    assert ig.find_cycles(edges) == [[k(4), k(6), k(4)], [k(9), k(9)]]


def test_acyclic_graph_has_no_cycles() -> None:
    assert ig.find_cycles({(k(1), k(2)), (k(2), k(3)), (k(1), k(3))}) == []


def test_cycles_are_ordered_by_first_key_not_by_discovery() -> None:
    """Tarjan completes {3, 4} before {1, 2}; the report still lists 1 first."""
    edges = {(k(1), k(2)), (k(2), k(1)), (k(1), k(3)), (k(3), k(4)), (k(4), k(3))}
    assert ig.find_cycles(edges) == [[k(1), k(2), k(1)], [k(3), k(4), k(3)]]


# ─── Markdown titles: no autolink, no mention ───────────────────────────────
@pytest.mark.parametrize(
    "text,expected",
    [
        ("Остаток #120: guard", "Остаток #&#8203;120: guard"),
        ("ping @octocat and @Bob1", "ping @&#8203;octocat and @&#8203;Bob1"),
        ("#5@octocat", "#&#8203;5@&#8203;octocat"),
        ("a # b, #x, @ c, @-x, end@", "a # b, #x, @ c, @-x, end@"),
        ("@1abc", "@&#8203;1abc"),
        # code spans do not decode entities: their text stays as is
        ("`#12`", "`#12`"),
        ("a `#1` b #2", "a `#1` b #&#8203;2"),
        ("``x`#3``", "``x`#3``"),
        ("a ` #4", "a ` #&#8203;4"),
    ],
)
def test_markdown_title_breaks_autolinks_and_mentions(text: str, expected: str) -> None:
    assert ig.md_text(text) == expected


def test_epic_scope_title_is_guarded(tmp_path: Path) -> None:
    epic = {"title": "Epic #7 for @team", "subIssues": {"pageInfo": PAGE_END, "nodes": []}}
    write_json(tmp_path / "epic-api-9-p1.json", {"data": {"repository": {"issue": epic}}})
    r = run("--epic", "api#9", fixtures=tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split("\n", 1)[0] == (
        "### Граф задач: эпик [api#9](https://github.com/larchanka-training/dmc-268-api-t6/issues/9)"
        " Epic #&#8203;7 for @&#8203;team"
    )


def test_milestone_title_is_guarded_in_scope_and_warning(tmp_path: Path) -> None:
    def milestones(*titles: str) -> dict[str, Any]:
        nodes = [{"number": n, "title": t} for n, t in enumerate(titles, 1)]
        return {"data": {"repository": {"milestones": {"nodes": nodes}}}}

    write_json(tmp_path / "milestones-api.json", milestones("Спринт #8"))
    write_json(tmp_path / "milestones-ui.json", milestones("Спринт #8 (архив)"))
    write_json(
        tmp_path / "milestone-api-1-p1.json",
        {"data": {"repository": {"milestone": {"issues": {"pageInfo": PAGE_END, "nodes": []}}}}},
    )
    r = run("--milestone", "Спринт #8", fixtures=tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split("\n")[:4] == [
        "### Граф задач: майлстоун «Спринт #&#8203;8»",
        "",
        "> Майлстоун «Спринт #&#8203;8» не найден в ui.",
        "",
    ]


# ─── not_found names every ref, once ────────────────────────────────────────
TYPED: dict[str, Any] = {"owner": "larchanka-training", "name": "dmc-268-api-t6"}


@pytest.mark.parametrize(
    "paths,typed,strings,expected",
    [
        ([["repository", "i7"]], TYPED, {}, "issue api#7 not found"),
        (
            [["repository", "issue"]],
            {**TYPED, "number": 9},
            {"after": None},
            "issue api#9 not found",
        ),
        ([["repository"]], TYPED, {}, "repository larchanka-training/dmc-268-api-t6 not found"),
        (
            [["repository", "milestone"]],
            {**TYPED, "number": 6},
            {"after": None},
            "milestone 6 not found in api",
        ),
        ([["node"]], {}, {"id": "I_kwDOX", "after": "MQ"}, "issue I_kwDOX not found"),
        ([["repository", "i7"], ["repository", "i7"]], TYPED, {}, "issue api#7 not found"),
        (
            [["repository", "i8"], ["repository", "i7"]],
            TYPED,
            {},
            "issue api#8 not found; issue api#7 not found",
        ),
        ([["viewer"]], TYPED, {}, "Could not resolve."),
    ],
)
def test_not_found_names_each_ref_once(
    paths: list[list[str]], typed: dict[str, Any], strings: dict[str, Any], expected: str
) -> None:
    errors = [not_found_error(path, "Could not resolve.") for path in paths]
    assert ig.not_found(errors, typed, strings) == expected


# ─── gh timeout ─────────────────────────────────────────────────────────────
def test_gh_call_times_out_after_120_seconds(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, Any] = {}

    def hang(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs)
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(ig.subprocess, "run", hang)
    with pytest.raises(SystemExit) as exit_info:
        ig.request("issues-api", "query { x }", dict(TYPED), {})
    assert exit_info.value.code == 3
    assert seen["timeout"] == 120
    assert "timed out" in json.loads(capsys.readouterr().err)["error"]


# ─── report sections outside the goldens ────────────────────────────────────
def section(report: str, title: str) -> str:
    """Body of `#### <title>` up to the next heading, without the blank lines."""
    body = report.split(f"\n#### {title}\n\n", 1)[1]
    return body.split("\n\n#### ", 1)[0].rstrip("\n")


def class_lines(report: str) -> list[str]:
    return [line for line in report.splitlines() if line.startswith("  class ")]


def test_all_closed_scope_has_no_open_tasks(tmp_path: Path) -> None:
    closed = dict(CANNED_ISSUE, state="CLOSED")
    write_json(tmp_path / "issues-api.json", {"data": {"repository": {"i5": closed}}})
    r = run("--issues", "api#5", fixtures=tmp_path)
    assert r.returncode == 0, r.stderr
    assert section(r.stdout, "Слои: что можно брать параллельно") == "Нет открытых задач."
    assert section(r.stdout, "Критический путь") == "Нет: между открытыми задачами нет связей."
    assert section(r.stdout, "Без связей") == "Нет."


def test_board_is_chosen_by_project_number() -> None:
    """On board 7 api#15 is Todo and nobody else has a status: no alarm, no In Progress."""
    r = run("--milestone", "Спринт 5", "--project", "7", fixtures=FIXTURES / "milestone")
    assert r.returncode == 0, r.stderr
    assert section(r.stdout, "In Progress с открытым блокером") == "Нет."
    assert class_lines(r.stdout) == ["  class api_13,api_80 done", "  class api_90 external"]


def test_board_is_chosen_by_project_owner() -> None:
    """Board 12 owned by `axyi` has only api#11, In Progress there and unblocked."""
    r = run("--milestone", "Спринт 5", "--project-owner", "axyi", fixtures=FIXTURES / "milestone")
    assert r.returncode == 0, r.stderr
    assert section(r.stdout, "In Progress с открытым блокером") == "Нет."
    assert class_lines(r.stdout) == [
        "  class api_13,api_80 done",
        "  class api_11 progress",
        "  class api_90 external",
    ]


def test_repeated_issue_refs_are_deduplicated(tmp_path: Path) -> None:
    write_json(tmp_path / "issues-api.json", {"data": {"repository": {"i5": CANNED_ISSUE}}})
    r = run("--issues", "api#5", "api#5", fixtures=tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split("\n", 1)[0] == (
        "### Граф задач: задачи [api#5](https://github.com/larchanka-training/dmc-268-api-t6/issues/5)"
    )
    assert "Задач: 1 (" in r.stdout


# ─── live wire format: pages and cursors ────────────────────────────────────
def passed(argv: list[str], key: str) -> list[tuple[str, str]]:
    """(flag, value) of every `key=value` argument in a recorded gh argv."""
    return [
        (argv[i - 1], a.split("=", 1)[1]) for i, a in enumerate(argv) if a.startswith(f"{key}=")
    ]


def test_live_paging_sends_cursors_and_renders_like_the_fixture_seam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fake gh serves fixture pages chosen by argv alone: the `pagination` set
    in milestone mode, then the `epic` set in epic mode."""
    log = tmp_path / "calls.jsonl"
    body = (
        "import json, sys\n"
        "from pathlib import Path\n"
        f"FIX = Path({str(FIXTURES)!r})\n"
        "argv = sys.argv[1:]\n"
        f"with open({str(log)!r}, 'a', encoding='utf-8') as f:\n"
        "    f.write(json.dumps(argv) + '\\n')\n"
        "query = next(a for a in argv if a.startswith('query='))\n"
        "args = dict(a.split('=', 1) for a in argv if '=' in a and a != query)\n"
        "if 'subIssues(' in query:\n"
        "    name = 'epic/epic-api-139-p1'\n"
        "elif 'milestones(' in query:\n"
        "    name = 'pagination/milestones-api'\n"
        "elif 'node(id' in query:\n"
        "    field = 'blockedBy' if 'blockedBy(first' in query else 'blocking'\n"
        "    page = {'MQ': 2, 'Ymwx': 2, 'Ymwy': 3}[args['after']]\n"
        "    name = 'pagination/node-' + args['id'] + '-' + field + '-p' + str(page)\n"
        "else:\n"
        "    page = 2 if 'after' in args else 1\n"
        "    name = 'pagination/milestone-api-7-p' + str(page)\n"
        "sys.stdout.write((FIX / (name + '.json')).read_text(encoding='utf-8'))\n"
    )
    prepend_path(monkeypatch, fake_gh(tmp_path / "rec-bin", body))

    def connections(query: str) -> list[tuple[str, str]]:
        """Every `<connection>(first: <n>` in a query, sorted."""
        return sorted(re.findall(r"(\w+)\(first: (\d+)", query))

    # Brief 01: one fragment everywhere; every connection pages by 50,
    # projectItems and the milestone search take the first 20.
    fragment = [("blockedBy", "50"), ("blocking", "50"), ("projectItems", "20")]

    r = run("--milestone", "Спринт 7", "--repo", API)

    assert r.returncode == 0, r.stderr
    assert r.stdout == golden_body("pagination")
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    queries = [next(a for a in c if a.startswith("query=")) for c in calls]
    assert len(calls) == 6

    search = [c for c, q in zip(calls, queries, strict=True) if "milestones(" in q]
    assert len(search) == 1
    assert passed(search[0], "title") == [("-f", "Спринт 7")]
    assert passed(search[0], "after") == []
    search_query = next(q for q in queries if "milestones(" in q)
    assert connections(search_query) == [("milestones", "20")]
    for text in ("query: $title", "states: [OPEN, CLOSED]"):
        assert text in search_query

    pages = [c for c, q in zip(calls, queries, strict=True) if "milestone(number" in q]
    assert [passed(c, "number") for c in pages] == [[("-F", "7")], [("-F", "7")]]
    assert passed(pages[0], "after") == []
    assert passed(pages[1], "after") == [("-f", "Y3Vyc29yOnYyOpHOAAAAAQ==")]
    for page_query in (q for q in queries if "milestone(number" in q):
        assert connections(page_query) == sorted([("issues", "50"), *fragment])
        assert "parent { number repository { nameWithOwner } }" in page_query
        assert 'fieldValueByName(name: "Status")' in page_query

    follow_ups = sorted(
        (passed(c, "id"), connections(q), passed(c, "after"))
        for c, q in zip(calls, queries, strict=True)
        if "node(id" in q
    )
    assert follow_ups == [
        ([("-f", "I_kwDOEIOM8AAAABAPI0040")], [("blocking", "50")], [("-f", "Ymwx")]),
        ([("-f", "I_kwDOEIOM8AAAABAPI0040")], [("blocking", "50")], [("-f", "Ymwy")]),
        ([("-f", "I_kwDOEIOM8AAAABAPI0042")], [("blockedBy", "50")], [("-f", "MQ")]),
    ]

    log.unlink()
    r = run("--epic", "api#139")

    assert r.returncode == 0, r.stderr
    assert r.stdout == golden_body("epic")
    (call,) = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert passed(call, "number") == [("-F", "139")]
    assert passed(call, "after") == []
    epic_query = next(a for a in call if a.startswith("query="))
    assert connections(epic_query) == sorted([("subIssues", "50"), *fragment])
    assert "parent { number repository { nameWithOwner } }" in epic_query


# ─── publish: no target, no REST ────────────────────────────────────────────
FOOTER = (
    "<sub>Сгенерировано скиллом issue-graph (.agents/skills/issue-graph); "
    "повторный запуск с тем же охватом обновит этот комментарий.</sub>"
)
ISSUES_ARGS = next(args for case, args, _ in CASES if case == "issues")
ISSUES_MARKER = f"<!-- issue-graph: issues={API}#30,{API}#31,{API}#32,{UI}#9 -->"
COMMENTS_261 = f"repos/{API}/issues/261/comments"
# What gh 2.46 prints on stdout (exit 1) for a REST 404.
NOT_FOUND_REST: dict[str, Any] = {
    "message": "Not Found",
    "documentation_url": "https://docs.github.com/rest/issues/comments#list-issue-comments",
    "status": "404",
}


def case_copy(tmp_path: Path, case: str) -> Path:
    """A golden case's fixture set copied into tmp_path; a test adds comment pages to it."""
    target = tmp_path / "fixtures" / case
    shutil.copytree(FIXTURES / case, target)
    return target


def write_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log = tmp_path / "writes.jsonl"
    monkeypatch.setenv("ISSUE_GRAPH_WRITE_LOG", str(log))
    return log


def writes(log: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def comment_body(marker: str, report: str) -> str:
    return f"{marker}\n\n{report}\n{FOOTER}\n"


def comment(cid: int, body: str) -> dict[str, Any]:
    return {
        "id": cid,
        "body": body,
        "html_url": f"https://github.com/{API}/issues/261#issuecomment-{cid}",
    }


def comments_page(
    first: int, count: int, bodies: dict[int, str] | None = None
) -> list[dict[str, Any]]:
    """`count` comments with ids from `first`, oldest first; `bodies` overrides some."""
    special = bodies or {}
    return [comment(i, special.get(i, f"Comment {i}.")) for i in range(first, first + count)]


def outcome(r: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    """Publish mode: stderr is exactly one JSON line, the error or the publish result."""
    assert "Traceback" not in r.stderr
    assert r.stderr.count("\n") == 1, r.stderr
    parsed = json.loads(r.stderr)
    assert isinstance(parsed, dict)
    return parsed


@pytest.mark.parametrize("case,args,code", CASES, ids=[c[0] for c in CASES])
def test_without_publish_to_nothing_is_listed_or_written(
    case: str, args: list[str], code: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copied set has no comment pages, so a listing would exit 3; the log stays absent."""
    log = write_log(tmp_path, monkeypatch)
    r = run(*args, fixtures=case_copy(tmp_path, case))
    assert r.stderr == ""
    assert r.returncode == code
    assert r.stdout == golden_body(case)
    assert not log.exists()


# ─── publish: one marker comment, created or updated ────────────────────────
def test_publish_creates_the_comment_when_none_carries_the_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixtures = case_copy(tmp_path, "issues")
    write_json(fixtures / "comments-api-261-p1.json", [])
    write_json(fixtures / "comment-created-api-261.json", comment(9001, "as posted"))
    log = write_log(tmp_path, monkeypatch)

    r = run(*ISSUES_ARGS, "--publish-to", "261", fixtures=fixtures)

    assert r.returncode == 0, r.stderr
    assert r.stdout == golden_body("issues")
    assert outcome(r) == {
        "published": "created",
        "url": f"https://github.com/{API}/issues/261#issuecomment-9001",
    }
    assert writes(log) == [
        {
            "method": "POST",
            "path": COMMENTS_261,
            "body": comment_body(ISSUES_MARKER, golden_body("issues")),
        }
    ]


def test_publish_updates_the_marked_comment_on_page_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full page 1 holds only near misses: the marker quoted, on line 2, followed by
    two CRs, after a space. Full page 2 has the real one in a CRLF body, so only the
    match keeps page 3 (absent: exit 3) from being read."""
    fixtures = case_copy(tmp_path, "issues")
    near_misses = {
        110: f"> {ISSUES_MARKER}\n\nquoted",
        120: f"See below.\n{ISSUES_MARKER}",
        130: f"{ISSUES_MARKER}\r\r\nold",
        140: f" {ISSUES_MARKER}\nold",
    }
    write_json(fixtures / "comments-api-261-p1.json", comments_page(101, 100, near_misses))
    write_json(
        fixtures / "comments-api-261-p2.json",
        comments_page(201, 100, {250: f"{ISSUES_MARKER}\r\n\nold report"}),
    )
    for cid in (*near_misses, 250):
        write_json(fixtures / f"comment-updated-{cid}.json", comment(cid, "as edited"))
    log = write_log(tmp_path, monkeypatch)

    r = run(*ISSUES_ARGS, "--publish-to", "api#261", fixtures=fixtures)

    assert r.returncode == 0, r.stderr
    assert r.stdout == golden_body("issues")
    assert outcome(r) == {
        "published": "updated",
        "url": f"https://github.com/{API}/issues/261#issuecomment-250",
    }
    assert writes(log) == [
        {
            "method": "PATCH",
            "path": f"repos/{API}/issues/comments/250",
            "body": comment_body(ISSUES_MARKER, golden_body("issues")),
        }
    ]


def test_publish_updates_the_oldest_of_two_marked_comments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both matches sit on a full page 1: page 2 (absent: exit 3) is never read."""
    fixtures = case_copy(tmp_path, "issues")
    marked = {105: f"{ISSUES_MARKER}\n\nolder", 160: f"{ISSUES_MARKER}\n\nnewer"}
    write_json(fixtures / "comments-api-261-p1.json", comments_page(101, 100, marked))
    for cid in marked:
        write_json(fixtures / f"comment-updated-{cid}.json", comment(cid, "as edited"))
    log = write_log(tmp_path, monkeypatch)

    r = run(*ISSUES_ARGS, "--publish-to", "261", fixtures=fixtures)

    assert r.returncode == 0, r.stderr
    assert [(w["method"], w["path"]) for w in writes(log)] == [
        ("PATCH", f"repos/{API}/issues/comments/105")
    ]
    assert outcome(r)["url"] == f"https://github.com/{API}/issues/261#issuecomment-105"


def test_publish_ignores_another_scopes_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixtures = case_copy(tmp_path, "milestone")
    others = [
        f"<!-- issue-graph: epic={API}#139 -->",
        "<!-- issue-graph: milestone=Спринт 50 -->",
        "<!-- issue-graph: milestone=Спринт -->",
        f"<!-- issue-graph: issues={API}#5 -->",
    ]
    write_json(
        fixtures / "comments-api-261-p1.json",
        [comment(cid, f"{m}\n\nreport") for cid, m in enumerate(others, 1)],
    )
    for cid in range(1, len(others) + 1):
        write_json(fixtures / f"comment-updated-{cid}.json", comment(cid, "as edited"))
    write_json(fixtures / "comment-created-api-261.json", comment(9002, "as posted"))
    log = write_log(tmp_path, monkeypatch)

    r = run("--milestone", "Спринт 5", "--publish-to", "261", fixtures=fixtures)

    assert r.returncode == 0, r.stderr
    assert r.stdout == golden_body("milestone")
    assert writes(log) == [
        {
            "method": "POST",
            "path": COMMENTS_261,
            "body": comment_body(
                "<!-- issue-graph: milestone=Спринт 5 -->", golden_body("milestone")
            ),
        }
    ]


@pytest.mark.parametrize("marked", [False, True], ids=["created", "updated"])
@pytest.mark.parametrize(
    "target,name,repo,number",
    [("ui#5", "ui-5", UI, 5), ("octo/tools#7", "octo_tools-7", "octo/tools", 7)],
    ids=["ui", "owner-name"],
)
def test_publish_goes_to_the_target_repo(
    target: str,
    name: str,
    repo: str,
    number: int,
    marked: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fixture names come from slug(); the listing and the write both use the target's repo."""
    fixtures = case_copy(tmp_path, "issues")
    listed = [comment(42, f"{ISSUES_MARKER}\n\nold report")] if marked else []
    write_json(fixtures / f"comments-{name}-p1.json", listed)
    response = {"id": 42, "html_url": f"https://github.com/{repo}/issues/{number}#c"}
    write_json(
        fixtures / ("comment-updated-42.json" if marked else f"comment-created-{name}.json"),
        response,
    )
    log = write_log(tmp_path, monkeypatch)

    r = run(*ISSUES_ARGS, "--publish-to", target, fixtures=fixtures)

    assert r.returncode == 0, r.stderr
    assert r.stdout == golden_body("issues")
    assert outcome(r) == {
        "published": "updated" if marked else "created",
        "url": response["html_url"],
    }
    assert writes(log) == [
        {
            "method": "PATCH" if marked else "POST",
            "path": f"repos/{repo}/issues/comments/42"
            if marked
            else f"repos/{repo}/issues/{number}/comments",
            "body": comment_body(ISSUES_MARKER, golden_body("issues")),
        }
    ]


# ─── publish: errors after the report ───────────────────────────────────────
def test_cycle_is_reported_but_not_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No comment pages in the set: a listing would turn the exit into 3."""
    log = write_log(tmp_path, monkeypatch)
    args = next(args for case, args, _ in CASES if case == "cycle")
    r = run(*args, "--publish-to", "261", fixtures=case_copy(tmp_path, "cycle"))
    assert r.returncode == 1
    assert r.stdout == golden_body("cycle")
    assert outcome(r) == {"error": "not published: the graph has a cycle"}
    assert not log.exists()


def test_publish_target_not_found_in_a_fixture_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixtures = case_copy(tmp_path, "issues")
    write_json(fixtures / "comments-api-261-p1.json", NOT_FOUND_REST)
    log = write_log(tmp_path, monkeypatch)
    r = run(*ISSUES_ARGS, "--publish-to", "261", fixtures=fixtures)
    assert r.returncode == 2
    assert r.stdout == golden_body("issues")
    assert outcome(r) == {"error": "issue api#261 not found"}
    assert not log.exists()


def test_listing_page_that_is_not_an_array_exits_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixtures = case_copy(tmp_path, "issues")
    write_json(fixtures / "comments-api-261-p1.json", {"message": "Moved", "status": "301"})
    log = write_log(tmp_path, monkeypatch)
    r = run(*ISSUES_ARGS, "--publish-to", "261", fixtures=fixtures)
    assert r.returncode == 3
    assert r.stdout == golden_body("issues")
    assert "not a JSON array" in outcome(r)["error"]
    assert not log.exists()


def test_fixture_mode_without_a_write_log_never_writes(tmp_path: Path) -> None:
    """The listing is served, so only the write itself can refuse; gh stays the guard."""
    fixtures = case_copy(tmp_path, "issues")
    write_json(fixtures / "comments-api-261-p1.json", [])
    write_json(fixtures / "comment-created-api-261.json", comment(9001, "as posted"))
    r = run(*ISSUES_ARGS, "--publish-to", "261", fixtures=fixtures)
    assert r.returncode == 3
    assert r.stdout == golden_body("issues")
    assert outcome(r) == {"error": "fixture mode cannot publish: set ISSUE_GRAPH_WRITE_LOG"}


def test_write_response_without_html_url_exits_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixtures = case_copy(tmp_path, "issues")
    write_json(fixtures / "comments-api-261-p1.json", [])
    write_json(fixtures / "comment-created-api-261.json", {"id": 9001})
    write_log(tmp_path, monkeypatch)
    r = run(*ISSUES_ARGS, "--publish-to", "261", fixtures=fixtures)
    assert r.returncode == 3
    assert r.stdout == golden_body("issues")
    assert outcome(r)["error"].startswith("unexpected REST response shape: ")


# ─── publish: target and marker ─────────────────────────────────────────────
@pytest.mark.parametrize(
    "text,key",
    [
        ("261", (API, 261)),
        ("api#261", (API, 261)),
        ("ui#5", (UI, 5)),
        ("octo/tools#7", ("octo/tools", 7)),
    ],
)
def test_publish_target_parses(text: str, key: tuple[str, int]) -> None:
    assert ig.parse_args(["--issues", "api#1", "--publish-to", text]).publish_to == key


def test_publish_to_is_optional() -> None:
    assert ig.parse_args(["--issues", "api#1"]).publish_to is None


@pytest.mark.parametrize(
    "bad", ["x", "0", "-3", "261x", "+5", "٣", "", "api#0", "web#3", "o/..#3", "#3"]
)
def test_bad_publish_target_exits_2_before_any_request(bad: str) -> None:
    r = run("--issues", "api#1", "--publish-to", bad)
    assert r.returncode == 2
    assert error_of(r) == (
        f"bad --publish-to {bad!r}: expected N (an api issue) or REF (api#N, ui#N or OWNER/NAME#N)"
    )


@pytest.mark.parametrize(
    "args,key",
    [
        (["--milestone", "50% a>b --> c"], "milestone=50%25 a%3Eb --%3E c"),
        (["--milestone", "%3E"], "milestone=%253E"),
        (["--milestone", "Спринт 5"], "milestone=Спринт 5"),
        (["--milestone", "Спринт 5", "--repo", API], "milestone=Спринт 5"),
        (["--epic", "api#139"], f"epic={API}#139"),
        (["--epic", "octo/tools#3"], "epic=octo/tools#3"),
        (["--issues", "ui#2", "api#10", "api#9", "api#10"], f"issues={API}#9,{API}#10,{UI}#2"),
    ],
)
def test_marker_names_the_scope(args: list[str], key: str) -> None:
    """The repo set is not part of a milestone key; `%` then `>` are escaped."""
    assert ig.marker(ig.parse_args(args)) == f"<!-- issue-graph: {key} -->"


def test_marker_does_not_depend_on_issue_order() -> None:
    first = ig.marker(ig.parse_args(["--issues", "ui#2", "api#1", "api#1"]))
    second = ig.marker(ig.parse_args(["--issues", "api#1", "ui#2"]))
    assert first == second == f"<!-- issue-graph: issues={API}#1,{UI}#2 -->"


@pytest.mark.parametrize(
    "typed,canonical,key",
    [
        (
            ["--epic", "Larchanka-Training/DMC-268-API-T6#139"],
            ["--epic", "api#139"],
            f"epic={API}#139",
        ),
        (
            ["--issues", "Zeta/X#1", "api#2", "ZETA/x#1"],
            ["--issues", "api#2", "zeta/x#1"],
            f"issues={API}#2,zeta/x#1",
        ),
        (
            ["--issues", "A/x#10", "a/x#9"],
            ["--issues", "a/x#9", "a/x#10"],
            "issues=a/x#9,a/x#10",
        ),
    ],
    ids=["epic", "issues-dedup", "issues-numeric-order"],
)
def test_marker_repo_is_case_insensitive(typed: list[str], canonical: list[str], key: str) -> None:
    """GitHub repo names ignore case: lowered first, then deduplicated and sorted by Key."""
    first = ig.marker(ig.parse_args(typed))
    assert first == ig.marker(ig.parse_args(canonical)) == f"<!-- issue-graph: {key} -->"


# ─── publish: live REST transport ───────────────────────────────────────────
LIVE_MARKER = f"<!-- issue-graph: issues={API}#5 -->"
LISTED = ([comment(777, f"{LIVE_MARKER}\n\nold report")], 0, "")
GH_404 = (NOT_FOUND_REST, 1, "gh: Not Found (HTTP 404)\n")
GH_403 = (
    {"message": "Resource not accessible by integration", "status": "403"},
    1,
    "gh: Resource not accessible by integration (HTTP 403)\n",
)
LIST_P1 = ["api", "-X", "GET", f"{COMMENTS_261}?per_page=100&page=1"]
LIST_P2 = ["api", "-X", "GET", f"{COMMENTS_261}?per_page=100&page=2"]
PATCH_777 = ["api", "-X", "PATCH", f"repos/{API}/issues/comments/777", "--input", "-"]
POST_261 = ["api", "-X", "POST", COMMENTS_261, "--input", "-"]
UPDATED_777 = {
    "published": "updated",
    "url": f"https://github.com/{API}/issues/261#issuecomment-777",
}


def live_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, plan: dict[str, Any]) -> Path:
    """A fake gh answering from `plan`: per method (`graphql` for the report) a queue
    of (stdout, exit, stderr), served in call order; an exhausted queue exits 98. It
    logs every argv with its stdin, which it reads only after `--input`."""
    log = tmp_path / "gh-calls.jsonl"
    plan_file = tmp_path / "plan.json"
    write_json(plan_file, plan)
    body = (
        "import json, os, sys\n"
        "argv = sys.argv[1:]\n"
        "stdin = sys.stdin.read() if '--input' in argv else None\n"
        "def kind(a):\n"
        "    return 'graphql' if a[1] == 'graphql' else a[2]\n"
        f"log = {str(log)!r}\n"
        "seen = []\n"
        "if os.path.exists(log):\n"
        "    with open(log, encoding='utf-8') as f:\n"
        "        seen = [json.loads(line)['argv'] for line in f]\n"
        "with open(log, 'a', encoding='utf-8') as f:\n"
        "    f.write(json.dumps({'argv': argv, 'stdin': stdin}) + '\\n')\n"
        f"with open({str(plan_file)!r}, encoding='utf-8') as f:\n"
        "    queue = json.load(f)[kind(argv)]\n"
        "served = sum(kind(a) == kind(argv) for a in seen)\n"
        "if served >= len(queue):\n"
        "    sys.stderr.write('fake gh: queue exhausted\\n')\n"
        "    sys.exit(98)\n"
        "out, code, err = queue[served]\n"
        "sys.stdout.write(json.dumps(out))\n"
        "sys.stderr.write(err)\n"
        "sys.exit(code)\n"
    )
    prepend_path(monkeypatch, fake_gh(tmp_path / "rest-bin", body))
    return log


@pytest.mark.parametrize(
    "plan,code,result,sent",
    [
        pytest.param(
            {"GET": [LISTED], "PATCH": [(comment(777, "as edited"), 0, "")]},
            0,
            UPDATED_777,
            [LIST_P1, PATCH_777],
            id="updated",
        ),
        pytest.param(
            {
                "GET": [
                    (comments_page(101, 100), 0, ""),
                    ([comment(777, f"{LIVE_MARKER}\n\nold report")], 0, ""),
                ],
                "PATCH": [(comment(777, "as edited"), 0, "")],
            },
            0,
            UPDATED_777,
            [LIST_P1, LIST_P2, PATCH_777],
            id="updated-page-2",
        ),
        pytest.param(
            {"GET": [([], 0, "")], "POST": [(comment(778, "as posted"), 0, "")]},
            0,
            {
                "published": "created",
                "url": f"https://github.com/{API}/issues/261#issuecomment-778",
            },
            [LIST_P1, POST_261],
            id="created",
        ),
        pytest.param(
            {"GET": [GH_404]}, 2, {"error": "issue api#261 not found"}, [LIST_P1], id="issue-404"
        ),
        pytest.param(
            {"GET": [([], 0, "")], "POST": [GH_404]},
            2,
            {"error": "issue api#261 not found"},
            [LIST_P1, POST_261],
            id="post-404",
        ),
        pytest.param(
            {"GET": [LISTED], "PATCH": [GH_404]},
            2,
            {"error": "comment 777 not found"},
            [LIST_P1, PATCH_777],
            id="comment-404",
        ),
        pytest.param(
            {"GET": [LISTED], "PATCH": [GH_403]},
            3,
            {
                "error": "comment 777 carries the marker but this account cannot edit it: "
                "gh: Resource not accessible by integration (HTTP 403)"
            },
            [LIST_P1, PATCH_777],
            id="patch-403",
        ),
        pytest.param(
            {"GET": [([], 0, "")], "POST": [GH_403]},
            3,
            {
                "error": f"gh api -X POST {COMMENTS_261} exited 1: "
                "gh: Resource not accessible by integration (HTTP 403)"
            },
            [LIST_P1, POST_261],
            id="post-403",
        ),
    ],
)
def test_live_publish_argv_stdin_and_status_mapping(
    plan: dict[str, Any],
    code: int,
    result: dict[str, str],
    sent: list[list[str]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every REST argv in order: pages until a match or a short page, then at most one
    write — a failed PATCH never falls back to a POST."""
    log = live_gh(
        tmp_path,
        monkeypatch,
        {"graphql": [({"data": {"repository": {"i5": CANNED_ISSUE}}}, 0, "")], **plan},
    )

    r = run("--issues", "api#5", "--publish-to", "261")

    assert r.returncode == code
    assert outcome(r) == result
    assert r.stdout.startswith(
        "### Граф задач: задачи [api#5](https://github.com/larchanka-training/dmc-268-api-t6/issues/5)\n"
    )
    graphql, *calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert graphql["argv"][:2] == ["api", "graphql"]
    assert [c["argv"] for c in calls] == sent
    payload = {"body": comment_body(LIVE_MARKER, r.stdout)}
    assert [None if c["stdin"] is None else json.loads(c["stdin"]) for c in calls] == [
        None if argv[2] == "GET" else payload for argv in sent
    ]


@pytest.mark.parametrize(
    "method,body,suffix",
    [
        ("GET", None, ""),
        ("POST", "body", "; the comment may have been written — re-run to update it"),
        ("PATCH", "body", "; the comment may have been written — re-run to update it"),
    ],
    ids=["GET", "POST", "PATCH"],
)
def test_rest_call_times_out_after_120_seconds(
    method: str,
    body: str | None,
    suffix: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: dict[str, Any] = {}

    def hang(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs)
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(ig.subprocess, "run", hang)
    path = f"repos/{API}/issues/261/comments"
    with pytest.raises(SystemExit) as exit_info:
        ig.rest(method, path, fixture="unused", missing="unused", body=body)
    assert exit_info.value.code == 3
    assert seen["timeout"] == 120
    assert seen["input"] == (None if body is None else json.dumps({"body": body}))
    assert json.loads(capsys.readouterr().err) == {
        "error": f"gh api -X {method} {path} timed out after 120 s{suffix}"
    }


# ─── publish: an empty seam and the page cap ────────────────────────────────
def test_empty_fixture_dir_is_an_error_not_live_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set but empty must not fall through to gh: the guard would answer exit 3 too,
    so only the message tells them apart. GraphQL runs first, so this is request()."""
    monkeypatch.setenv("ISSUE_GRAPH_FIXTURE_DIR", "")
    r = run("--issues", "api#1", "--publish-to", "261")
    assert r.returncode == 3
    assert error_of(r) == "ISSUE_GRAPH_FIXTURE_DIR is set but empty"


@pytest.mark.parametrize("method,body", [("GET", None), ("POST", "body")], ids=["read", "write"])
def test_empty_fixture_dir_stops_rest_before_gh(
    method: str,
    body: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    log = write_log(tmp_path, monkeypatch)
    monkeypatch.setenv("ISSUE_GRAPH_FIXTURE_DIR", "")
    with pytest.raises(SystemExit) as exit_info:
        ig.rest(method, COMMENTS_261, fixture="unused", missing="unused", body=body)
    assert exit_info.value.code == 3
    assert json.loads(capsys.readouterr().err) == {
        "error": "ISSUE_GRAPH_FIXTURE_DIR is set but empty"
    }
    assert not log.exists()


def test_marker_search_stops_at_the_page_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The last page under the cap is still searched to its end; a full one without
    the marker stops the search (page 3 is absent: reading it would be another 3)."""
    assert (ig.COMMENTS_MAX_PAGES, ig.COMMENTS_PAGE) == (100, 100)
    monkeypatch.setattr(ig, "COMMENTS_MAX_PAGES", 2)
    monkeypatch.setenv("ISSUE_GRAPH_FIXTURE_DIR", str(tmp_path))
    page_2 = tmp_path / "comments-api-261-p2.json"
    write_json(tmp_path / "comments-api-261-p1.json", comments_page(101, 100))

    write_json(page_2, comments_page(201, 100, {300: LIVE_MARKER}))
    assert ig.find_marked((API, 261), LIVE_MARKER) == 300
    write_json(page_2, comments_page(201, 99))
    assert ig.find_marked((API, 261), LIVE_MARKER) is None

    write_json(page_2, comments_page(201, 100))
    with pytest.raises(SystemExit) as exit_info:
        ig.find_marked((API, 261), LIVE_MARKER)
    assert exit_info.value.code == 3
    assert json.loads(capsys.readouterr().err) == {
        "error": "issue api#261: more than 200 comments, the marker search stopped"
    }


# ─── publish: comment size limit ────────────────────────────────────────────
def test_body_over_the_comment_limit_is_not_published(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The limit counts characters: a Cyrillic body of exactly 65536 (about twice as
    many bytes) goes out; one character more exits 2 before any request."""
    calls: list[str] = []

    def fake_rest(
        method: str, path: str, *, fixture: str, missing: str, body: str | None = None
    ) -> Any:
        calls.append(method)
        return [] if method == "GET" else {"html_url": "https://example.test/c"}

    monkeypatch.setattr(ig, "rest", fake_rest)
    fill = 65536 - len(comment_body(LIVE_MARKER, ""))

    ig.publish((API, 261), LIVE_MARKER, "я" * fill)
    assert calls == ["GET", "POST"]
    assert json.loads(capsys.readouterr().err) == {
        "published": "created",
        "url": "https://example.test/c",
    }

    calls.clear()
    with pytest.raises(SystemExit) as exit_info:
        ig.publish((API, 261), LIVE_MARKER, "я" * (fill + 1))
    assert exit_info.value.code == 2
    assert calls == []
    assert json.loads(capsys.readouterr().err) == {
        "error": "report is 65537 characters, over GitHub's 65536-character comment limit; "
        "narrow the scope"
    }
