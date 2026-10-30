"""Contract tests for scripts/board_status.py.

Offline: no network, no tokens, no live `gh`. An autouse guard puts a fake `gh`
first on PATH that fails with exit 97, so any request that escapes the fixture
seam (`ACCEPT_ISSUE_FIXTURE_DIR`) or a test's own fake is an error, never a call.

These tests belong to this skill's board script alone (a skill never reads another
skill's files): they cover, besides the Status write, reading the current Status
without STATUS, and assert the board queries here only.

Covers: a live capture of the board (`fixtures/board-live/`: project, Status field
and options of board 12, and the item of api#269) and synthetic variants of it; the
Status lookup by name, the read-back, the single write, and what must not happen
(an unknown Status, an issue that is not on the board, a read-back that differs:
no write, or no success); the REF grammar, exit codes 2 and 3, and the argv and the
queries the script hands to `gh`.
"""
# SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/accept-issue/tests/test_accept_board_status.py  # noqa: E501

from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "scripts" / "board_status.py"
LIVE = HERE / "fixtures" / "board-live"
API = "larchanka-training/dmc-268-api-t6"
UI = "larchanka-training/dmc-268-ui-t6"
OPTIONS = ["Todo", "In Progress", "On Review", "AC Check", "Done"]


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


bs = _load(SCRIPT, "accept_issue_board_status")


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
    monkeypatch.delenv("ACCEPT_ISSUE_FIXTURE_DIR", raising=False)


def run(*args: str, fixtures: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if fixtures is not None:
        env["ACCEPT_ISSUE_FIXTURE_DIR"] = str(fixtures)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        encoding="utf-8",
        env=env,
        timeout=60,
    )


def error_of(r: subprocess.CompletedProcess[str]) -> str:
    """stderr is exactly one JSON line `{"error": "..."}`; stdout stays empty."""
    assert r.stdout == "", "stdout must carry the JSON document only"
    assert "Traceback" not in r.stderr
    assert r.stderr.count("\n") == 1, r.stderr
    message = json.loads(r.stderr)["error"]
    assert isinstance(message, str)
    return message


def load(name: str) -> dict[str, Any]:
    doc = json.loads((LIVE / f"{name}.json").read_text(encoding="utf-8"))
    assert isinstance(doc, dict)
    return doc


def write(directory: Path, name: str, data: Any) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")


def board_dir(
    tmp_path: Path, *, before: str | None = "Todo", after: str | None = "In Progress"
) -> Path:
    """The live capture with the Status of the item before and after the write set."""
    fx = tmp_path / "fx"
    write(fx, "board", load("board"))
    write(fx, "set-status", load("set-status"))
    for name, status in (("item", before), ("readback", after)):
        doc = load(name)
        node = doc["data"]["repository"]["issue"]["projectItems"]["nodes"][0]
        node["fieldValueByName"] = (
            None
            if status is None
            else {"__typename": "ProjectV2ItemFieldSingleSelectValue", "name": status}
        )
        write(fx, name, doc)
    return fx


# ─── the live capture ───────────────────────────────────────────────────────
def test_live_capture_sets_in_progress_and_prints_the_read_back() -> None:
    r = run("api#269", "In Progress", fixtures=LIVE)
    assert r.returncode == 0, r.stderr
    assert r.stderr == ""
    assert r.stdout == (
        json.dumps(
            {"board": 12, "ref": f"{API}#269", "status": "In Progress"},
            sort_keys=True,
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )


def test_live_capture_has_the_five_course_statuses() -> None:
    field = load("board")["data"]["organization"]["projectV2"]["field"]
    assert [o["name"] for o in field["options"]] == OPTIONS


def test_a_status_change(tmp_path: Path) -> None:
    r = run("ui#5", "In Progress", fixtures=board_dir(tmp_path))
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == {"board": 12, "ref": f"{UI}#5", "status": "In Progress"}


def test_the_ref_forms(tmp_path: Path) -> None:
    fx = board_dir(tmp_path)
    for text, ref in (("269", f"{API}#269"), ("api#269", f"{API}#269"), (f"{UI}#8", f"{UI}#8")):
        r = run(text, "In Progress", fixtures=fx)
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["ref"] == ref


@pytest.mark.parametrize("given", ["in progress", "IN PROGRESS", "In progress"])
def test_the_status_is_matched_without_case_and_printed_as_the_board_has_it(
    tmp_path: Path, given: str
) -> None:
    r = run("api#1", given, fixtures=board_dir(tmp_path))
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["status"] == "In Progress"


def test_an_unknown_status_exits_2_and_lists_the_options(tmp_path: Path) -> None:
    r = run("api#1", "Blocked", fixtures=board_dir(tmp_path))
    assert r.returncode == 2
    message = error_of(r)
    assert "unknown Status 'Blocked'" in message
    for name in OPTIONS:
        assert repr(name) in message


def test_an_issue_that_is_not_on_the_board_exits_3(tmp_path: Path) -> None:
    fx = board_dir(tmp_path)
    for items in ([], [{"id": "I", "project": {"id": "PVT_other"}, "fieldValueByName": None}]):
        doc = load("item")
        doc["data"]["repository"]["issue"]["projectItems"]["nodes"] = items
        write(fx, "item", doc)
        r = run("api#1", "In Progress", fixtures=fx)
        assert r.returncode == 3
        assert error_of(r) == f"issue {API}#1 is not on board 12; this script never adds it"


def test_a_read_back_that_differs_exits_3(tmp_path: Path) -> None:
    for after in ("Todo", None):
        r = run("api#1", "In Progress", fixtures=board_dir(tmp_path, after=after))
        assert r.returncode == 3
        assert f"Status of {API}#1 reads back as {after!r}, not 'In Progress'" == error_of(r)


def test_setting_the_status_the_issue_already_has_is_fine(tmp_path: Path) -> None:
    r = run("api#1", "In Progress", fixtures=board_dir(tmp_path, before="In Progress"))
    assert r.returncode == 0, r.stderr


def test_the_status_field_must_be_a_single_select(tmp_path: Path) -> None:
    fx = board_dir(tmp_path)
    doc = load("board")
    doc["data"]["organization"]["projectV2"]["field"] = {"__typename": "ProjectV2Field", "id": "F"}
    write(fx, "board", doc)
    r = run("api#1", "In Progress", fixtures=fx)
    assert r.returncode == 3
    assert "no single-select field named Status" in error_of(r)


def test_issue_not_found_exits_3(tmp_path: Path) -> None:
    fx = board_dir(tmp_path)
    write(fx, "item", {"data": {"repository": {"issue": None}}})
    r = run("api#99", "In Progress", fixtures=fx)
    assert r.returncode == 3
    assert error_of(r) == f"issue {API}#99 not found"


def test_a_second_page_of_board_items_exits_3(tmp_path: Path) -> None:
    fx = board_dir(tmp_path)
    doc = load("item")
    doc["data"]["repository"]["issue"]["projectItems"]["pageInfo"]["hasNextPage"] = True
    write(fx, "item", doc)
    r = run("api#1", "In Progress", fixtures=fx)
    assert r.returncode == 3
    assert "more than one page" in error_of(r)


# ─── REF grammar and exit codes ─────────────────────────────────────────────
@pytest.mark.parametrize(
    ("text", "parsed"),
    [("api#269", (API, 269)), ("ui#7", (UI, 7)), ("269", (API, 269)), ("a/b#4", ("a/b", 4))],
)
def test_parse_ref(text: str, parsed: tuple[str, int]) -> None:
    assert bs.parse_ref(text) == parsed


@pytest.mark.parametrize("text", ["", "api", "api#0", "#5", "web#5", "0", "a b#1"])
def test_bad_ref_exits_2_and_never_reaches_gh(text: str) -> None:
    r = run(text, "In Progress")  # the guard gh would exit 97 and the exit would be 3
    assert r.returncode == 2
    error_of(r)


def test_usage_errors_exit_2() -> None:
    for args in ([], ["api#1", "In Progress", "extra"], ["--bogus"]):
        r = run(*args)
        assert r.returncode == 2, args
        error_of(r)


def test_gh_failure_exits_3() -> None:
    r = run("api#1", "In Progress")  # no fixture seam: the guard gh exits 97
    assert r.returncode == 3
    message = error_of(r)
    assert "exited 97" in message
    assert "unexpected gh call" in message


def test_gh_failure_with_error_response_on_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = (
        "import json, sys\n"
        'print(json.dumps({"errors": [{"type": "NOT_FOUND", "message": "nope"}], "data": None}))\n'
        "sys.exit(1)\n"
    )
    prepend_path(monkeypatch, fake_gh(tmp_path / "bin", body))
    r = run("api#1", "In Progress")
    assert r.returncode == 3
    assert "exited 1" in error_of(r)


def test_gh_timeout_exits_3(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise subprocess.TimeoutExpired("gh", 1)

    monkeypatch.setattr(bs.subprocess, "run", boom)
    with pytest.raises(SystemExit) as exc:
        bs.main(["api#1", "In Progress"])
    assert exc.value.code == 3
    assert "timed out" in json.loads(capsys.readouterr().err)["error"]


def test_undecodable_gh_output_exits_3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body = "import sys\nsys.stdout.buffer.write(b'\\xff\\xfe not utf-8')\n"
    prepend_path(monkeypatch, fake_gh(tmp_path / "bin", body))
    r = run("api#1", "In Progress")
    assert r.returncode == 3
    assert "cannot read the output" in error_of(r)


def test_graphql_errors_exit_3(tmp_path: Path) -> None:
    write(tmp_path, "board", {"errors": [{"message": "boom"}], "data": None})
    r = run("api#1", "In Progress", fixtures=tmp_path)
    assert r.returncode == 3
    assert "boom" in error_of(r)


def test_missing_and_broken_fixtures_exit_3(tmp_path: Path) -> None:
    r = run("api#1", "In Progress", fixtures=tmp_path)
    assert r.returncode == 3
    assert "cannot read fixture" in error_of(r)
    (tmp_path / "board.json").write_text("not json", encoding="utf-8")
    r = run("api#1", "In Progress", fixtures=tmp_path)
    assert r.returncode == 3
    assert "not JSON" in error_of(r)
    write(tmp_path, "board", ["a list"])
    assert run("api#1", "In Progress", fixtures=tmp_path).returncode == 3
    write(tmp_path, "board", {"data": None})
    assert run("api#1", "In Progress", fixtures=tmp_path).returncode == 3
    (tmp_path / "board.json").write_bytes(b'{"data": "\xff\xfe"}')
    assert run("api#1", "In Progress", fixtures=tmp_path).returncode == 3


def test_empty_fixture_dir_variable_exits_3_without_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACCEPT_ISSUE_FIXTURE_DIR", "")
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "api#1", "In Progress"],
        capture_output=True,
        encoding="utf-8",
        timeout=60,
    )
    assert r.returncode == 3
    assert "set but empty" in error_of(r)


def _board_with(path: list[str | int], value: Any) -> dict[str, Any]:
    root = copy.deepcopy(load("board"))
    node: Any = root
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return root


@pytest.mark.parametrize(
    ("name", "response"),
    [
        ("board", _board_with(["data", "organization"], None)),
        ("board", _board_with(["data", "organization", "projectV2"], None)),
        ("board", _board_with(["data", "organization", "projectV2", "field", "options"], "x")),
        ("board", _board_with(["data", "organization", "projectV2", "id"], 5)),
        ("board", _board_with(["data", "organization", "projectV2", "field", "options", 0], 5)),
        ("item", {"data": {"repository": None}}),
        ("item", {"data": {"repository": {"issue": {"projectItems": {"nodes": "x"}}}}}),
        ("set-status", {"data": {"updateProjectV2ItemFieldValue": None}}),
    ],
    ids=["org", "project", "options", "project-id", "option", "repository", "items", "mutation"],
)
def test_unexpected_shape_exits_3(tmp_path: Path, name: str, response: dict[str, Any]) -> None:
    fx = board_dir(tmp_path)
    write(fx, name, response)
    r = run("api#1", "In Progress", fixtures=fx)
    assert r.returncode == 3
    assert "unexpected shape" in error_of(r)


# ─── the argv handed to gh ──────────────────────────────────────────────────
GH_LOG_FAKE = """\
import json, os, sys
from pathlib import Path

argv = sys.argv[1:]
with open(os.environ["GH_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(argv) + "\\n")
query = next(a for a in argv if a.startswith("query="))
fake = Path(os.environ["FAKE_DIR"])
if "mutation" in query:
    name = "set-status"
elif "organization(" in query:
    name = "board"
else:  # the item, or its read-back once the write has been logged
    wrote = "updateProjectV2ItemFieldValue" in Path(os.environ["GH_LOG"]).read_text()
    name = "readback" if wrote else "item"
sys.stdout.write((fake / (name + ".json")).read_text(encoding="utf-8"))
"""


def gh_calls(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def query_of(argv: list[str]) -> str:
    return next(a for a in argv if a.startswith("query="))


def squash(text: str) -> str:
    """Whitespace collapsed to single blanks: a re-indented query is the same query."""
    return " ".join(text.split())


# The expected text of every query, written out: an id or a field dropped from (or added to)
# a query must fail here, not pass because the fixtures still answer with it.
BOARD_QUERY_TEXT = squash(
    """
    query($org: String!, $board: Int!) {
      organization(login: $org) {
        projectV2(number: $board) {
          id
          field(name: "Status") {
            __typename
            ... on ProjectV2SingleSelectField { id name options { id name } }
          }
        }
      }
    }
    """
)
ITEM_QUERY_TEXT = squash(
    """
    query($owner: String!, $name: String!, $number: Int!) {
      repository(owner: $owner, name: $name) {
        issue(number: $number) {
          projectItems(first: 100) {
            pageInfo { hasNextPage endCursor }
            nodes {
              id
              project { id }
              fieldValueByName(name: "Status") {
                __typename
                ... on ProjectV2ItemFieldSingleSelectValue { name }
              }
            }
          }
        }
      }
    }
    """
)
SET_QUERY_TEXT = squash(
    """
    mutation($project: ID!, $item: ID!, $field: ID!, $option: String!) {
      updateProjectV2ItemFieldValue(input: {
        projectId: $project, itemId: $item, fieldId: $field,
        value: {singleSelectOptionId: $option}
      }) { projectV2Item { id } }
    }
    """
)


def test_argv_queries_and_the_single_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "gh.log"
    prepend_path(monkeypatch, fake_gh(tmp_path / "bin", GH_LOG_FAKE))
    monkeypatch.setenv("GH_LOG", str(log))
    monkeypatch.setenv("FAKE_DIR", str(board_dir(tmp_path)))
    r = run("ui#7", "in progress")
    assert r.returncode == 0, r.stderr
    calls = gh_calls(log)
    assert len(calls) == 4  # the board, the item, the write, the read-back
    board, item, write_call, readback = calls
    for argv in calls:
        assert argv[:3] == ["api", "graphql", "-f"]
        assert "-X" not in argv
    # exactly one write, and it is the field update on the existing item
    mutations = [a for a in calls if "mutation" in query_of(a)]
    assert mutations == [write_call]
    assert squash(query_of(write_call).removeprefix("query=")) == SET_QUERY_TEXT
    assert not any("addProjectV2ItemById" in " ".join(a) for a in calls)
    assert not any("deleteProjectV2Item" in " ".join(a) for a in calls)
    # the board is looked up by org and number (variables), the ids by name (from the answer)
    pairs = list(zip(board, board[1:], strict=False))
    assert ("-f", "org=larchanka-training") in pairs and ("-F", "board=12") in pairs
    assert squash(query_of(board).removeprefix("query=")) == BOARD_QUERY_TEXT
    # the item is the issue's own, in its repo, paged by nothing but first: 100
    pairs = list(zip(item, item[1:], strict=False))
    assert ("-f", "owner=larchanka-training") in pairs and ("-f", "name=dmc-268-ui-t6") in pairs
    assert ("-F", "number=7") in pairs
    assert squash(query_of(item).removeprefix("query=")) == ITEM_QUERY_TEXT
    assert query_of(readback) == query_of(item)
    # ids go with -f (strings, never reinterpreted) and come from the board response
    project = load("board")["data"]["organization"]["projectV2"]
    option = next(o for o in project["field"]["options"] if o["name"] == "In Progress")
    wanted = {
        "project": project["id"],
        "item": load("item")["data"]["repository"]["issue"]["projectItems"]["nodes"][0]["id"],
        "field": project["field"]["id"],
        "option": option["id"],
    }
    pairs = list(zip(write_call, write_call[1:], strict=False))
    for key, value in wanted.items():
        assert ("-f", f"{key}={value}") in pairs, key


@pytest.mark.parametrize(
    "args",
    [["api#1", "Blocked"], ["api#1", "In Progress"]],
    ids=["unknown-status", "not-on-board"],
)
def test_no_write_when_the_run_cannot_succeed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> None:
    fx = board_dir(tmp_path)
    if args[1] == "In Progress":
        doc = load("item")
        doc["data"]["repository"]["issue"]["projectItems"]["nodes"] = []
        write(fx, "item", doc)
    log = tmp_path / "gh.log"
    prepend_path(monkeypatch, fake_gh(tmp_path / "bin", GH_LOG_FAKE))
    monkeypatch.setenv("GH_LOG", str(log))
    monkeypatch.setenv("FAKE_DIR", str(fx))
    r = run(*args)
    assert r.returncode in (2, 3)
    assert not any("mutation" in query_of(a) for a in gh_calls(log))


# ─── reading the Status (STATUS omitted) ────────────────────────────────────
def test_reading_the_live_capture_prints_the_current_status() -> None:
    r = run("api#269", fixtures=LIVE)
    assert r.returncode == 0, r.stderr
    assert r.stderr == ""
    assert r.stdout == (
        json.dumps(
            {"board": 12, "ref": f"{API}#269", "status": "In Progress"},
            sort_keys=True,
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )


def test_reading_an_item_without_a_status_prints_null(tmp_path: Path) -> None:
    r = run("ui#5", fixtures=board_dir(tmp_path, before=None))
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == {"board": 12, "ref": f"{UI}#5", "status": None}


def test_reading_an_issue_that_is_not_on_the_board_exits_3(tmp_path: Path) -> None:
    fx = board_dir(tmp_path)
    doc = load("item")
    doc["data"]["repository"]["issue"]["projectItems"]["nodes"] = []
    write(fx, "item", doc)
    r = run("api#1", fixtures=fx)
    assert r.returncode == 3
    assert error_of(r) == f"issue {API}#1 is not on board 12; this script never adds it"


def test_reading_asks_the_board_and_the_item_and_never_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "gh.log"
    prepend_path(monkeypatch, fake_gh(tmp_path / "bin", GH_LOG_FAKE))
    monkeypatch.setenv("GH_LOG", str(log))
    monkeypatch.setenv("FAKE_DIR", str(board_dir(tmp_path, before="AC Check")))
    r = run("ui#7")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["status"] == "AC Check"
    board, item = gh_calls(log)  # exactly two calls: no write, no read-back
    assert squash(query_of(board).removeprefix("query=")) == BOARD_QUERY_TEXT
    assert squash(query_of(item).removeprefix("query=")) == ITEM_QUERY_TEXT
    pairs = list(zip(item, item[1:], strict=False))
    assert ("-f", "name=dmc-268-ui-t6") in pairs and ("-F", "number=7") in pairs


def test_a_bad_ref_without_a_status_exits_2(tmp_path: Path) -> None:
    r = run("api#0", fixtures=board_dir(tmp_path))
    assert r.returncode == 2
    assert "bad REF" in error_of(r)
