"""Contract tests for scripts/issue_graph.py.

Offline: no network, no tokens, no live `gh`. An autouse guard puts a fake `gh`
first on PATH that fails with exit 97, so any request that escapes the fixture
seam (`ISSUE_GRAPH_FIXTURE_DIR`) or a test's own fake is an error, never a call.

Covers: byte-exact golden reports for six GraphQL fixture sets (a cycle, a
milestone found in one repo only, a full sprint milestone, an epic, nested
pagination, explicit issues across both repos), label stripping/cutting/escaping,
REF parsing, the one-mode rule, the cycle walk, exit codes 2 and 3, and the argv
the script hands to `gh`.
"""
# SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/issue-graph/tests/test_issue_graph.py

from __future__ import annotations

import importlib.util
import json
import os
import re
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
    """Every test starts with a `gh` that refuses to run and no fixture dir."""
    guard = fake_gh(
        tmp_path / "guard-bin",
        'import sys\nsys.stderr.write("unexpected gh call\\n")\nsys.exit(97)\n',
    )
    prepend_path(monkeypatch, guard)
    monkeypatch.delenv("ISSUE_GRAPH_FIXTURE_DIR", raising=False)


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
