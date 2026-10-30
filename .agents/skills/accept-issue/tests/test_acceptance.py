"""Contract tests for scripts/acceptance.py.

Offline: no network, no tokens, no live `gh`. An autouse guard puts a fake `gh` first
on PATH that fails with exit 97, so any request that escapes the fixture seam is an
error, never a call. The golden cases live in `examples/` and run in test_examples.py;
this file covers what they do not: the row validation, the card rules, the write seam
and the live `gh api` argv.
"""
# SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/accept-issue/tests/test_acceptance.py

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
SCRIPT = SKILL / "scripts" / "acceptance.py"
EXAMPLES = SKILL / "examples"
STEP_AUTHOR = EXAMPLES / "01-not-done-then-ac-check" / "step-2"  # table, all done, In Progress
STEP_CLOSED = EXAMPLES / "06-closed-by-merge" / "step-1"
STEP_ACCEPT = EXAMPLES / "03-accept-and-close" / "step-1"  # accept, AC Check, all done
STEP_RETURN = EXAMPLES / "04-return-with-fail" / "step-1"  # return, AC3 not done
REAL_272 = HERE / "fixtures" / "real-272"
API = "larchanka-training/dmc-268-api-t6"
REF = f"{API}#500"


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


acc = _load(SCRIPT, "accept_issue_acceptance")


@pytest.fixture(autouse=True)
def no_live_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    guard = tmp_path / "guard-bin"
    guard.mkdir()
    gh = guard / "gh"
    gh.write_text(
        f"#!{sys.executable}\nimport sys\n"
        "sys.stderr.write('unexpected gh call\\n')\nsys.exit(97)\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{guard}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.delenv("ACCEPT_ISSUE_FIXTURE_DIR", raising=False)
    monkeypatch.delenv("ACCEPT_ISSUE_WRITE_LOG", raising=False)


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def put(directory: Path, name: str, data: Any) -> None:
    (directory / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")


def fixtures_of(step: Path, tmp_path: Path) -> Path:
    target = tmp_path / "fx"
    shutil.copytree(step / "fixtures", target)
    return target


def run(
    *args: str, fixtures: Path | None = None, log: Path | None = None, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if fixtures is not None:
        env["ACCEPT_ISSUE_FIXTURE_DIR"] = str(fixtures)
    if log is not None:
        env["ACCEPT_ISSUE_WRITE_LOG"] = str(log)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        encoding="utf-8",
        env=env,
        cwd=cwd,
        check=False,
    )


def error_of(r: subprocess.CompletedProcess[str]) -> str:
    assert r.stdout == ""
    assert "Traceback" not in r.stderr
    assert r.stderr.count("\n") == 1, r.stderr
    message = json.loads(r.stderr)["error"]
    assert isinstance(message, str)
    return message


def rows_file(tmp_path: Path, spec: Any) -> Path:
    path = tmp_path / "rows.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    return path


def good_rows() -> dict[str, Any]:
    return read(STEP_AUTHOR / "rows.json")  # type: ignore[no-any-return]


def table(
    tmp_path: Path, spec: Any, *flags: str, fixtures: Path | None = None, log: Path | None = None
) -> subprocess.CompletedProcess[str]:
    fx = fixtures or fixtures_of(STEP_AUTHOR, tmp_path)
    return run("table", "api#500", str(rows_file(tmp_path, spec)), *flags, fixtures=fx, log=log)


# ─── row validation ─────────────────────────────────────────────────────────
def stops_of(r: subprocess.CompletedProcess[str]) -> list[str]:
    assert r.returncode == 1, r.stderr
    doc = json.loads(r.stdout)
    assert doc["verdict"] == "stop"
    assert doc["rows"] == [] and doc["comment"] is None
    stops = doc["stops"]
    assert isinstance(stops, list)
    return [str(s) for s in stops]


def test_a_missing_row_and_an_unknown_id_are_stops(tmp_path: Path) -> None:
    spec = good_rows()
    del spec["AC2"]
    spec["AC9"] = {"status": "not_done"}
    assert stops_of(table(tmp_path, spec)) == [
        "AC9: no such AC or DoD in the issue body",
        "AC2: no row for this item",
    ]


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"status": "done"}, "AC1: status done needs a non-empty evidence"),
        ({"status": "done", "evidence": "  "}, "AC1: status done needs a non-empty evidence"),
        ({"status": "finished"}, "AC1: status must be done, not_done or moved"),
        ({"evidence": "x"}, "AC1: status must be done, not_done or moved"),
        ({"status": "moved"}, 'AC1: status moved needs "to": "#M"'),
        ({"status": "moved", "to": "285"}, 'AC1: status moved needs "to": "#M"'),
        (
            {"status": "moved", "to": "#285"},
            "AC1: status moved needs «перенесён в #285» in the issue body item; "
            "the tech lead writes it, the executor cannot move an AC",
        ),
        ("done", "AC1: the row must be an object"),
    ],
)
def test_a_bad_row_is_a_stop(tmp_path: Path, entry: Any, message: str) -> None:
    spec = good_rows()
    spec["AC1"] = entry
    assert stops_of(table(tmp_path, spec)) == [message]


def test_moved_needs_the_same_target_as_the_body_note(tmp_path: Path) -> None:
    fx = fixtures_of(STEP_AUTHOR, tmp_path)
    for name in ("issue-p1", "acceptance-p1"):
        doc = read(fx / f"{name}.json")
        issue = doc["data"]["repository"]["issue"]
        issue["body"] = issue["body"].replace(
            "Правило включено в ruleset", "Правило перенесён в #285"
        )
        put(fx, name, doc)
    spec = good_rows()
    spec["AC3"] = {"status": "moved", "to": "#286"}
    assert stops_of(table(tmp_path, spec, fixtures=fx)) == [
        "AC3: status moved needs «перенесён в #286» in the issue body item; "
        "the tech lead writes it, the executor cannot move an AC"
    ]
    spec["AC3"] = {"status": "moved", "to": "#285"}
    r = table(tmp_path, spec, fixtures=fx)
    assert r.returncode == 0, r.stderr
    rows = {row["id"]: row for row in json.loads(r.stdout)["rows"]}
    assert rows["AC3"]["evidence"] == "перенесён в #285"


def test_a_body_without_checkboxes_is_a_stop(tmp_path: Path) -> None:
    fx = fixtures_of(STEP_AUTHOR, tmp_path)
    for name in ("issue-p1", "acceptance-p1"):
        doc = read(fx / f"{name}.json")
        doc["data"]["repository"]["issue"]["body"] = "## Background\n\nNothing to check.\n"
        put(fx, name, doc)
    assert stops_of(table(tmp_path, good_rows(), fixtures=fx)) == [
        f"issue {REF} has no AC or DoD checkboxes: there is nothing to check"
    ]


@pytest.mark.parametrize("content", ["", "{", "[]", '"AC1"'])
def test_a_bad_rows_file_exits_2(tmp_path: Path, content: str) -> None:
    path = tmp_path / "rows.json"
    path.write_text(content, encoding="utf-8")
    r = run("table", "api#500", str(path), fixtures=fixtures_of(STEP_AUTHOR, tmp_path))
    assert r.returncode == 2
    assert "ROWS_JSON" in error_of(r)


def test_a_missing_rows_file_exits_2(tmp_path: Path) -> None:
    r = run(
        "table", "api#500", str(tmp_path / "none.json"), fixtures=fixtures_of(STEP_AUTHOR, tmp_path)
    )
    assert r.returncode == 2
    assert "cannot read ROWS_JSON" in error_of(r)


# ─── the issue and the card ─────────────────────────────────────────────────
def test_a_closed_issue_is_a_stop_for_the_author(tmp_path: Path) -> None:
    r = table(tmp_path, good_rows(), fixtures=fixtures_of(STEP_CLOSED, tmp_path))
    assert stops_of(r) == [f"issue {REF} is CLOSED: the self-check applies to an open issue"]


def _card(tmp_path: Path, status: str | None) -> Path:
    fx = fixtures_of(STEP_AUTHOR, tmp_path)
    doc = read(fx / "item.json")
    node = doc["data"]["repository"]["issue"]["projectItems"]["nodes"][0]
    node["fieldValueByName"] = (
        None
        if status is None
        else {"__typename": "ProjectV2ItemFieldSingleSelectValue", "name": status}
    )
    put(fx, "item", doc)
    return fx


@pytest.mark.parametrize("status", ["Todo", "Done", None])
def test_ac_check_from_a_card_elsewhere_is_a_stop(tmp_path: Path, status: str | None) -> None:
    r = table(tmp_path, good_rows(), fixtures=_card(tmp_path, status))
    assert r.returncode == 1
    doc = json.loads(r.stdout)
    assert doc["stops"] == [
        f"the card is in {status or 'no Status'}: AC Check is set from On Review, "
        "In Progress or AC Check"
    ]
    assert doc["board"] == {"action": "none", "from": status, "readback": None, "to": None}
    assert doc["comment"] is None and len(doc["rows"]) == 5


@pytest.mark.parametrize(
    ("status", "action", "target"),
    [
        ("On Review", "set", "In Progress"),
        ("In Progress", "none", "In Progress"),
        ("AC Check", "left", None),
        ("Todo", "none", None),
    ],
)
def test_not_done_moves_the_card_only_from_on_review(
    tmp_path: Path, status: str, action: str, target: str | None
) -> None:
    spec = good_rows()
    spec["AC2"] = {"status": "not_done"}
    r = table(tmp_path, spec, fixtures=_card(tmp_path, status))
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    assert doc["verdict"] == "not_done" and doc["ac_check"] is False
    assert doc["board"] == {"action": action, "from": status, "readback": None, "to": target}
    assert doc["rework_rule"] == acc.RULE
    assert doc["warnings"] == ([f"the card is in {status}: not moved"] if status == "Todo" else [])


def test_ac_check_already_there_writes_no_card_move(tmp_path: Path) -> None:
    log = tmp_path / "writes.jsonl"
    r = table(tmp_path, good_rows(), "--apply", fixtures=_card(tmp_path, "AC Check"), log=log)
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    assert doc["board"] == {
        "action": "none",
        "from": "AC Check",
        "readback": None,
        "to": "AC Check",
    }
    assert [json.loads(line)["op"] for line in log.read_text(encoding="utf-8").splitlines()] == [
        "comment"
    ]


def test_a_viewer_without_assignees_is_named_in_the_warning(tmp_path: Path) -> None:
    fx = fixtures_of(STEP_AUTHOR, tmp_path)
    doc = read(fx / "acceptance-p1.json")
    doc["data"]["repository"]["issue"]["assignees"]["nodes"] = []
    put(fx, "acceptance-p1", doc)
    r = table(tmp_path, good_rows(), fixtures=fx)
    assert json.loads(r.stdout)["warnings"] == [
        "viewer executor is not an assignee (assignees: none)"
    ]


# ─── writes ─────────────────────────────────────────────────────────────────
def test_a_dry_run_writes_nothing_even_with_a_log(tmp_path: Path) -> None:
    log = tmp_path / "writes.jsonl"
    r = table(tmp_path, good_rows(), log=log)
    assert r.returncode == 0, r.stderr
    assert not log.exists()
    assert json.loads(r.stdout)["apply"] is False


def test_apply_against_the_seam_needs_the_write_log(tmp_path: Path) -> None:
    r = table(tmp_path, good_rows(), "--apply")
    assert r.returncode == 3
    assert "ACCEPT_ISSUE_WRITE_LOG" in error_of(r)


def test_a_stop_writes_nothing_with_apply(tmp_path: Path) -> None:
    log = tmp_path / "writes.jsonl"
    spec = good_rows()
    del spec["AC2"]
    r = table(tmp_path, spec, "--apply", log=log)
    assert r.returncode == 1
    assert not log.exists()


def test_the_same_table_is_not_posted_twice(tmp_path: Path) -> None:
    log = tmp_path / "writes.jsonl"
    first = table(tmp_path, good_rows(), "--apply", log=log)
    comment = json.loads(first.stdout)["comment"]
    fx = tmp_path / "fx"
    doc = read(fx / "acceptance-p1.json")
    doc["data"]["repository"]["issue"]["comments"]["nodes"].append(
        {
            "databaseId": 9001,
            "author": {"login": "executor"},
            "createdAt": "2026-10-30T11:00:00Z",
            "body": comment,
        }
    )
    put(fx, "acceptance-p1", doc)
    log.unlink()
    second = table(tmp_path, good_rows(), "--apply", fixtures=fx, log=log)
    out = json.loads(second.stdout)
    assert out["warnings"] == [
        "the same table is already the latest table comment (#9001): not posted again"
    ]
    assert [json.loads(line)["op"] for line in log.read_text(encoding="utf-8").splitlines()] == [
        "set-status"
    ]


def test_a_read_back_that_differs_exits_3(tmp_path: Path) -> None:
    fx = fixtures_of(STEP_AUTHOR, tmp_path)
    doc = read(fx / "readback.json")
    doc["data"]["repository"]["issue"]["projectItems"]["nodes"][0]["fieldValueByName"]["name"] = (
        "Todo"
    )
    put(fx, "readback", doc)
    r = table(tmp_path, good_rows(), "--apply", fixtures=fx, log=tmp_path / "w.jsonl")
    assert r.returncode == 3
    assert "reads back as 'Todo', not 'AC Check'" in error_of(r)


# ─── the table text ─────────────────────────────────────────────────────────
def test_cells_escape_pipes_and_flatten_lines() -> None:
    assert acc.esc("a | b\n  c") == "a \\| b c"


def test_render_marks_the_comment_and_names_the_open_items() -> None:
    rows = [
        {"id": "AC1", "kind": "ac", "text": "one | two", "status": "done", "evidence": "PR #1"},
        {
            "id": "DoD1",
            "kind": "dod",
            "text": "gate",
            "status": "not_done",
            "evidence": "не выполнено",
        },
    ]
    text = acc.render_table(rows, None)
    assert text.startswith("<!-- accept-issue:table v1 -->\n")
    assert "| AC1 | one \\| two | PR #1 |" in text
    assert "| DoD1 | gate | не выполнено |" in text
    assert text.endswith(
        "Итог: не выполнено — DoD1. AC Check не ставлю, доработка идёт в этом же issue.\n"
    )


def test_the_latest_marked_comment_wins() -> None:
    mark = acc.TABLE_MARK
    comments = [
        acc.Comment(1, "a", "t", f"{mark}\nold"),
        acc.Comment(2, "a", "t", "chat"),
        acc.Comment(3, "a", "t", f"  {mark}\nnew"),
    ]
    latest = acc.latest_marked(comments, mark)
    assert latest is not None and latest.id == 3
    assert acc.latest_marked(comments[1:2], mark) is None


# ─── live writes (argv only) ────────────────────────────────────────────────
def test_a_live_comment_is_a_rest_post_with_a_raw_string_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    monkeypatch.setattr(acc.subprocess, "run", fake_run)
    acc.post_comment(API, 500, "@someone <!-- x -->")
    assert calls == [
        ["gh", "api", f"repos/{API}/issues/500/comments", "-f", "body=@someone <!-- x -->"]
    ]


def test_a_failing_live_write_exits_3(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, "", "HTTP 403")

    monkeypatch.setattr(acc.subprocess, "run", fake_run)
    with pytest.raises(SystemExit) as stop:
        acc.post_comment(API, 500, "x")
    assert stop.value.code == 3
    assert "HTTP 403" in capsys.readouterr().err


def test_usage_errors_exit_2() -> None:
    for args in ([], ["table"], ["table", "api#1"], ["frobnicate", "api#1"], ["table", "x", "y"]):
        r = run(*args)
        assert r.returncode == 2, args
        error_of(r)


MOVED_WARNING = (
    "AC3 засчитан как перенесённый в #285 по пометке в теле issue: проверьте, что пометку "
    "дописал техлид — тело issue может править и исполнитель"
)


# ─── accept and return: shared helpers ──────────────────────────────────────
def accept_fx(tmp_path: Path, step: Path = STEP_ACCEPT) -> Path:
    return fixtures_of(step, tmp_path)


def comments_of(fx: Path) -> list[dict[str, Any]]:
    nodes = read(fx / "acceptance-p1.json")["data"]["repository"]["issue"]["comments"]["nodes"]
    assert isinstance(nodes, list)
    return nodes


def edit_acceptance(fx: Path, edit: Any, name: str = "acceptance-p1") -> None:
    doc = read(fx / f"{name}.json")
    edit(doc["data"]["repository"]["issue"], doc["data"])
    put(fx, name, doc)


def accepter(
    tmp_path: Path, *args: str, fx: Path | None = None, log: Path | None = None
) -> subprocess.CompletedProcess[str]:
    return run(*args, fixtures=fx or accept_fx(tmp_path), log=log)


def ops(log: Path) -> list[str]:
    if not log.exists():
        return []
    return [json.loads(x)["op"] for x in log.read_text(encoding="utf-8").splitlines()]


# ─── the live smoke fixture: api#272, closed by hand ────────────────────────
def test_real_272_is_already_closed_by_hand_so_accept_and_return_stop(tmp_path: Path) -> None:
    for command, message in (
        ("accept", "nothing to accept"),
        ("return", "nothing to return"),
    ):
        r = run(command, "api#272", fixtures=REAL_272)
        assert r.returncode == 1, r.stderr
        doc = json.loads(r.stdout)
        assert doc["verdict"] == "stop"
        assert doc["stops"] == [f"issue {API}#272 is already CLOSED by a manual close: {message}"]
        assert doc["viewer"] == "axyi" and doc["rows"] == []
    log = tmp_path / "w.jsonl"
    r = run("accept", "api#272", "--apply", fixtures=REAL_272, log=log)
    assert r.returncode == 1 and not log.exists()


# ─── reading a table ────────────────────────────────────────────────────────
def test_split_row_unescapes_pipes_and_ignores_prose() -> None:
    assert acc.split_row("| AC1 | a \\| b | `x \\| y` |") == ["AC1", "a | b", "`x | y`"]
    assert acc.split_row("no table here") is None
    assert acc.split_row("|a|b|") == ["a", "b"]


def test_table_rows_drop_the_header_and_the_separator() -> None:
    body = "text\n\n| # | Пункт | Доказательство |\n| --- | :-: | ---: |\n| AC1 | a | b |\n"
    assert acc.table_rows(body) == [["AC1", "a", "b"]]


@pytest.mark.parametrize(
    ("cell", "status"),
    [
        ("не выполнено", "not_done"),
        ("**Не выполнено**", "not_done"),
        ("не выполнено — позже", "not_done"),
        ("перенесён в #285", "moved"),
        ("", "missing"),
        ("  ", "missing"),
        ("PR #1", "done"),
        ("выполнено, не выполнено лишь A", "done"),
    ],
)
def test_evidence_status(cell: str, status: str) -> None:
    assert acc.evidence_status(cell)[0] == status


def snap_of(body: str, comments: list[Any]) -> Any:
    return acc.Snapshot(API, 500, {}, body, acc.body_items(body), ["executor"], "axyi", comments)


BODY = "## Acceptance Criteria\n\n- [ ] one | two\n- [ ] three\n\n## DoD Checklist\n\n- [ ] gate\n"


def table_comment(rows: list[dict[str, str]], cid: int = 1) -> Any:
    return acc.Comment(cid, "executor", "t", acc.render_table(rows, None))


def row(item_id: str, text: str, status: str, evidence: str) -> dict[str, str]:
    kind = "ac" if item_id.startswith("AC") else "dod"
    return {"id": item_id, "kind": kind, "text": text, "status": status, "evidence": evidence}


def test_the_table_this_script_renders_reads_back_row_for_row() -> None:
    rows = [
        row("AC1", "one | two", "done", "PR #1 | a"),
        row("AC2", "three", "not_done", "не выполнено"),
        row("DoD1", "gate", "done", "CI"),
    ]
    snap = snap_of(BODY, [table_comment(rows)])
    read_back = acc.read_table(snap, None)
    assert read_back.via == "marker"
    assert [(r["id"], r["status"], r["evidence"]) for r in read_back.rows] == [
        ("AC1", "done", "PR #1 | a"),
        ("AC2", "not_done", "не выполнено"),
        ("DoD1", "done", "CI"),
    ]
    assert [i["id"] for i in read_back.returned] == ["AC2"]


def test_a_missing_row_and_a_changed_item_return_but_a_longer_item_does_not() -> None:
    rows = [row("AC1", "one | two", "done", "x"), row("DoD1", "gate", "done", "y")]
    snap = snap_of(BODY, [table_comment(rows)])
    returned = {i["id"]: i["reason"] for i in acc.read_table(snap, None).returned}
    assert returned == {"AC2": "в таблице нет строки"}
    longer = BODY.replace("- [ ] gate", "- [ ] gate, stricter")
    assert acc.read_table(snap_of(longer, [table_comment(rows)]), None).returned[0]["id"] == "AC2"
    assert [
        i["id"] for i in acc.read_table(snap_of(longer, [table_comment(rows)]), None).returned
    ] == ["AC2"]
    changed = BODY.replace("- [ ] gate", "- [ ] other gate")
    returned = {
        i["id"]: i["reason"]
        for i in acc.read_table(snap_of(changed, [table_comment(rows)]), None).returned
    }
    assert returned["DoD1"] == "пункт в теле issue изменён после таблицы"


def test_a_row_the_issue_does_not_have_is_a_stop_on_the_marker_path() -> None:
    rows = [row("AC1", "one | two", "done", "x"), row("AC9", "ghost", "done", "y")]
    with pytest.raises(acc.TableError, match="AC9 | ghost | y"):
        acc.read_table(snap_of(BODY, [table_comment(rows)]), None)


def test_moved_needs_the_note_in_the_body_to_pass() -> None:
    rows = [
        row("AC1", "one | two", "moved", "перенесён в #285"),
        row("AC2", "three", "done", "x"),
        row("DoD1", "gate", "done", "y"),
    ]
    returned = acc.read_table(snap_of(BODY, [table_comment(rows)]), None).returned
    assert [i["id"] for i in returned] == ["AC1"]
    assert "#285" in returned[0]["reason"]
    noted = BODY.replace("one | two", "one | two, перенесён в #285 решением техлида")
    rows[0]["text"] = "one | two, перенесён в #285 решением техлида"
    assert acc.read_table(snap_of(noted, [table_comment(rows)]), None).returned == []


HAND_WRITTEN = (
    "Проверил:\n\n"
    "| Пункт | Что проверено |\n| --- | --- |\n"
    "| **AC1** | видно в PR #3 |\n"
    "| three | **не выполнено** |\n"
    "| gate | зелёный |\n"
)


def test_a_hand_written_table_is_read_by_id_or_by_text() -> None:
    comments = [acc.Comment(77, "executor", "t", HAND_WRITTEN)]
    snap = snap_of(BODY, comments)
    read_back = acc.read_table(snap, 77)
    assert read_back.via == "flag"
    assert [(r["id"], r["status"]) for r in read_back.rows] == [
        ("AC1", "done"),
        ("AC2", "not_done"),
        ("DoD1", "done"),
    ]


def test_a_hand_written_table_with_a_gap_is_incomplete_not_a_pass() -> None:
    comments = [acc.Comment(77, "executor", "t", HAND_WRITTEN.replace("| gate | зелёный |\n", ""))]
    returned = acc.read_table(snap_of(BODY, comments), 77).returned
    assert [(i["id"], i["reason"]) for i in returned] == [
        ("AC2", acc.NOT_DONE),
        ("DoD1", "в таблице нет строки"),
    ]


def test_table_comment_errors_are_stops() -> None:
    snap = snap_of(BODY, [acc.Comment(5, "executor", "t", "just text")])
    with pytest.raises(acc.TableError, match="comment 9 not found"):
        acc.read_table(snap, 9)
    with pytest.raises(acc.TableError, match="comment 5 has no table"):
        acc.read_table(snap, 5)
    with pytest.raises(acc.TableError, match="no --table-comment ID"):
        acc.read_table(snap, None)


def test_table_comment_flag_through_the_cli(tmp_path: Path) -> None:
    fx = accept_fx(tmp_path)
    marker_id = comments_of(fx)[0]["databaseId"]
    r = accepter(tmp_path, "accept", "api#500", "--table-comment", str(marker_id), fx=fx)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["table"]["via"] == "flag"
    r = accepter(tmp_path, "accept", "api#500", "--table-comment", "424242", fx=fx)
    assert json.loads(r.stdout)["stops"] == ["comment 424242 not found in the issue"]


# ─── ticking ────────────────────────────────────────────────────────────────
def test_tick_keeps_crlf_and_changes_only_the_boxes() -> None:
    body = (
        "## Acceptance Criteria\r\n\r\n- [ ] a\r\n- [x] b\r\n* [ ] c\r\n\r\n"
        "## DoD Checklist\r\n\r\n  - [ ] d\r\ntail without newline"
    )
    items = acc.body_items(body)
    ticked = acc.tick_body(body, items, [i.id for i in items if not i.checked])
    assert ticked == body.replace("[ ]", "[x]")
    assert ticked.count("\r\n") == body.count("\r\n")


def test_tick_refuses_a_line_it_cannot_rewrite_safely() -> None:
    body = "## Acceptance Criteria\n\n<!-- note --> - [ ] a\n"
    items = acc.body_items(body)
    assert [i.id for i in items] == ["AC1"]
    with pytest.raises(acc.TickError, match="cannot tick AC1: line 3"):
        acc.tick_body(body, items, ["AC1"])


def test_a_dry_run_accept_stops_where_apply_would_exit_3(tmp_path: Path) -> None:
    fx = accept_fx(tmp_path)

    def hide_a_box(issue: Any, _: Any) -> None:
        issue["body"] = issue["body"].replace(
            "- [ ] `GET /health`", "<!-- n --> - [ ] `GET /health`"
        )

    edit_acceptance(fx, hide_a_box)
    edit_acceptance(fx, hide_a_box, "acceptance-p1")
    doc = read(fx / "issue-p1.json")
    issue = doc["data"]["repository"]["issue"]
    issue["body"] = issue["body"].replace("- [ ] `GET /health`", "<!-- n --> - [ ] `GET /health`")
    put(fx, "issue-p1", doc)
    r = accepter(tmp_path, "accept", "api#500", fx=fx)
    assert r.returncode == 1, r.stderr
    out = json.loads(r.stdout)
    assert out["verdict"] == "stop" and out["tick"] == [] and out["comment"] is None
    assert out["stops"][0].startswith("cannot tick AC1: line ")


# ─── accept ─────────────────────────────────────────────────────────────────
def test_accept_dry_run_writes_nothing_and_ticks_only_unchecked(tmp_path: Path) -> None:
    log = tmp_path / "w.jsonl"
    r = accepter(tmp_path, "accept", "api#500", log=log)
    doc = json.loads(r.stdout)
    assert (r.returncode, doc["verdict"], doc["tick"]) == (
        0,
        "accept",
        ["AC1", "AC2", "DoD1", "DoD2"],
    )
    assert not log.exists()


def test_accept_apply_on_a_return_verdict_refuses(tmp_path: Path) -> None:
    log = tmp_path / "w.jsonl"
    fx = accept_fx(tmp_path, STEP_RETURN)
    r = accepter(tmp_path, "accept", "api#500", "--apply", fx=fx, log=log)
    doc = json.loads(r.stdout)
    assert r.returncode == 1 and doc["verdict"] == "stop"
    assert doc["stops"] == ["the verdict is return, not accept: nothing is written; run return"]
    assert [i["id"] for i in doc["return_items"]] == ["AC3"]
    assert not log.exists()


def test_accept_stops_when_the_body_changed_under_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx = accept_fx(tmp_path)
    log = tmp_path / "w.jsonl"
    monkeypatch.setenv("ACCEPT_ISSUE_FIXTURE_DIR", str(fx))
    monkeypatch.setenv("ACCEPT_ISSUE_WRITE_LOG", str(log))
    real = acc.fetch_acceptance
    calls = {"n": 0}

    def changing(repo: str, number: int, prefix: str = "") -> Any:
        viewer, body, assignees, comments = real(repo, number, prefix)
        calls["n"] += 1
        return (
            viewer,
            body + "\nedited meanwhile\n" if calls["n"] > 1 else body,
            assignees,
            comments,
        )

    monkeypatch.setattr(acc, "fetch_acceptance", changing)
    with pytest.raises(SystemExit) as stop:
        acc.run_accepter("accept", "api#500", None, [], {}, True)
    assert stop.value.code == 3
    assert not log.exists()


def test_an_identical_closing_comment_is_not_posted_twice(tmp_path: Path) -> None:
    fx = accept_fx(tmp_path)
    log = tmp_path / "w.jsonl"
    first = accepter(tmp_path, "accept", "api#500", fx=fx, log=log)
    comment = json.loads(first.stdout)["comment"]
    edit_acceptance(
        fx,
        lambda issue, _: issue["comments"]["nodes"].append(
            {
                "databaseId": 9,
                "author": {"login": "axyi"},
                "createdAt": "2026-10-30T12:00:00Z",
                "body": comment,
            }
        ),
    )
    r = accepter(tmp_path, "accept", "api#500", "--apply", fx=fx, log=log)
    assert r.returncode == 0, r.stderr
    assert ops(log) == ["patch-body", "close"]
    assert "already the latest one (#9)" in json.loads(r.stdout)["warnings"][-1]


def test_a_read_back_that_is_not_closed_exits_3(tmp_path: Path) -> None:
    fx = accept_fx(tmp_path)
    doc = read(fx / "after-issue-p1.json")
    issue = doc["data"]["repository"]["issue"]
    issue["state"] = "OPEN"
    issue["timelineItems"]["nodes"] = issue["timelineItems"]["nodes"][:-1]
    put(fx, "after-issue-p1", doc)
    r = accepter(tmp_path, "accept", "api#500", "--apply", fx=fx, log=tmp_path / "w.jsonl")
    assert r.returncode == 3
    assert "read-back failed after the writes" in error_of(r)


def test_a_read_back_with_a_different_body_exits_3(tmp_path: Path) -> None:
    fx = accept_fx(tmp_path)
    edit_acceptance(
        fx, lambda issue, _: issue.update(body=issue["body"] + "x"), "after-acceptance-p1"
    )
    r = accepter(tmp_path, "accept", "api#500", "--apply", fx=fx, log=tmp_path / "w.jsonl")
    assert r.returncode == 3
    assert "body reads back different" in error_of(r)


def test_accept_warns_about_unresolved_threads_and_red_ci(tmp_path: Path) -> None:
    fx = accept_fx(tmp_path)
    doc = read(fx / "issue-p1.json")
    pull = doc["data"]["repository"]["issue"]["timelineItems"]["nodes"][0]["source"]
    pull["reviewThreads"]["nodes"] = [{"isResolved": False}, {"isResolved": False}]
    pull["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["state"] = "FAILURE"
    put(fx, "issue-p1", doc)
    r = accepter(tmp_path, "accept", "api#500", fx=fx)
    assert json.loads(r.stdout)["warnings"] == [
        f"PR {API}#901 has 2 unresolved review thread(s)",
        f"PR {API}#901 CI state is FAILURE, not SUCCESS",
        MOVED_WARNING,
    ]


def test_script_comments_after_the_table_are_not_listed(tmp_path: Path) -> None:
    fx = accept_fx(tmp_path)
    edit_acceptance(
        fx,
        lambda issue, _: issue["comments"]["nodes"].extend(
            [
                {
                    "databaseId": 20,
                    "author": {"login": "axyi"},
                    "createdAt": "t",
                    "body": acc.ACCEPTED_MARK + "\nold",
                },
                {"databaseId": 21, "author": None, "createdAt": "t", "body": "\n\n" + "x" * 100},
            ]
        ),
    )
    doc = json.loads(accepter(tmp_path, "accept", "api#500", fx=fx).stdout)
    assert doc["after_table"] == [{"author": "ghost", "excerpt": "x" * 79 + "…", "id": 21}]


def test_a_follow_up_without_a_parent_is_a_stop(tmp_path: Path) -> None:
    fx = accept_fx(tmp_path)
    put(
        fx,
        f"parent-{API.replace('/', '_')}-511",
        {"data": {"repository": {"issue": {"parent": None}}}},
    )
    r = accepter(
        tmp_path, "accept", "api#500", "--followup", "api#511", "--apply", fx=fx, log=tmp_path / "w"
    )
    assert json.loads(r.stdout)["stops"] == [
        f"follow-up {API}#511 has no parent, not this issue {API}#500"
    ]
    assert r.returncode == 1 and not (tmp_path / "w").exists()


# ─── return ─────────────────────────────────────────────────────────────────
def test_return_with_nothing_to_return_refuses(tmp_path: Path) -> None:
    r = accepter(tmp_path, "return", "api#500")
    doc = json.loads(r.stdout)
    assert r.returncode == 1
    assert doc["stops"] == [
        "nothing to return: the table has no «не выполнено» and no --fail was given"
    ]


def test_fail_overrides_the_table_reason_and_unknown_ids_are_stops(tmp_path: Path) -> None:
    fx = accept_fx(tmp_path, STEP_RETURN)
    r = accepter(tmp_path, "return", "api#500", "--fail", "AC3=не прошло на staging", fx=fx)
    items = {i["id"]: i["reason"] for i in json.loads(r.stdout)["return_items"]}
    assert items == {"AC3": "доказательство не подтвердилось: не прошло на staging"}
    r = accepter(tmp_path, "return", "api#500", "--fail", "AC9=x", fx=fx)
    assert json.loads(r.stdout)["stops"] == ["--fail AC9: no such AC or DoD in the issue body"]


@pytest.mark.parametrize("value", ["AC1", "AC1=", "=x", " = "])
def test_a_bad_fail_exits_2(tmp_path: Path, value: str) -> None:
    r = accepter(tmp_path, "return", "api#500", "--fail", value)
    assert r.returncode == 2
    assert "bad --fail" in error_of(r)


@pytest.mark.parametrize(
    ("status", "action", "warned"),
    [("AC Check", "set", False), ("In Progress", "none", True), ("On Review", "set", True)],
)
def test_return_moves_the_card_unless_it_is_already_back(
    tmp_path: Path, status: str, action: str, warned: bool
) -> None:
    fx = accept_fx(tmp_path, STEP_RETURN)
    doc = read(fx / "item.json")
    doc["data"]["repository"]["issue"]["projectItems"]["nodes"][0]["fieldValueByName"]["name"] = (
        status
    )
    put(fx, "item", doc)
    log = tmp_path / "w.jsonl"
    r = accepter(tmp_path, "return", "api#500", "--fail", "AC1=x", "--apply", fx=fx, log=log)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["board"]["action"] == action and out["board"]["to"] == "In Progress"
    assert ops(log) == (["comment", "set-status"] if action == "set" else ["comment"])
    assert (f"the card is in {status}, not AC Check" in out["warnings"]) is warned


def test_an_identical_returned_comment_is_not_posted_twice(tmp_path: Path) -> None:
    fx = accept_fx(tmp_path, STEP_RETURN)
    log = tmp_path / "w.jsonl"
    args = ("return", "api#500", "--fail", "AC1=x", "--apply")
    first = accepter(tmp_path, *args, fx=fx, log=log)
    comment = json.loads(first.stdout)["comment"]
    edit_acceptance(
        fx,
        lambda issue, _: issue["comments"]["nodes"].append(
            {"databaseId": 9, "author": {"login": "axyi"}, "createdAt": "t", "body": comment}
        ),
    )
    log.unlink()
    second = accepter(tmp_path, *args, fx=fx, log=log)
    assert second.returncode == 0, second.stderr
    assert ops(log) == ["set-status"]


# ─── live writes: argv ──────────────────────────────────────────────────────
def test_live_body_and_close_are_rest_patches(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    monkeypatch.setattr(acc.subprocess, "run", fake_run)
    acc.patch_issue(API, 500, {"body": "- [x] a\n"}, {"body": "- [x] a\n", "op": "patch-body"})
    acc.patch_issue(
        API,
        500,
        {"state": "closed", "state_reason": "completed"},
        {"op": "close", "state_reason": "completed"},
    )
    base = ["gh", "api", f"repos/{API}/issues/500", "-X", "PATCH"]
    assert calls == [
        [*base, "-f", "body=- [x] a\n"],
        [*base, "-f", "state=closed", "-f", "state_reason=completed"],
    ]


# ─── review round B ─────────────────────────────────────────────────────────
def set_body(fx: Path, body: str, names: tuple[str, ...] = ("issue-p1", "acceptance-p1")) -> None:
    for name in names:
        doc = read(fx / f"{name}.json")
        doc["data"]["repository"]["issue"]["body"] = body
        put(fx, name, doc)


def body_of_fx(fx: Path) -> str:
    return str(read(fx / "acceptance-p1.json")["data"]["repository"]["issue"]["body"])


@pytest.mark.parametrize("command", ["accept", "return"])
def test_an_issue_without_ac_or_dod_is_never_accepted_or_returned(
    tmp_path: Path, command: str
) -> None:
    fx = accept_fx(tmp_path)
    marker_id = comments_of(fx)[0]["databaseId"]
    set_body(fx, "## Background\n\nNo boxes here.\n")
    log = tmp_path / "w.jsonl"
    r = accepter(
        tmp_path, command, "api#500", "--table-comment", str(marker_id), "--apply", fx=fx, log=log
    )
    assert r.returncode == 1, r.stderr
    doc = json.loads(r.stdout)
    assert doc["verdict"] == "stop"
    assert doc["stops"] == [
        f"issue {API}#500 has no AC or DoD checkboxes: there is nothing to {command}"
    ]
    assert not log.exists()


def test_return_stops_on_an_open_pr_too(tmp_path: Path) -> None:
    fx = accept_fx(tmp_path, STEP_RETURN)
    doc = read(fx / "issue-p1.json")
    node = doc["data"]["repository"]["issue"]["timelineItems"]["nodes"][0]
    node["source"]["state"], node["source"]["mergedAt"] = "OPEN", None
    put(fx, "issue-p1", doc)
    log = tmp_path / "w.jsonl"
    r = accepter(tmp_path, "return", "api#500", "--fail", "AC1=x", "--apply", fx=fx, log=log)
    assert r.returncode == 1
    assert "is OPEN and has a Refs line" in json.loads(r.stdout)["stops"][0]
    assert not log.exists()


def test_accept_apply_ticks_only_the_ac_and_dod_boxes_of_a_crlf_body(tmp_path: Path) -> None:
    fx = accept_fx(tmp_path)
    original = body_of_fx(fx)
    extra = "```\n- [ ] fenced\n```\n\n## Notes\n\n- [ ] outside the sections\n"
    body = original.replace("\n## DoD Checklist", "\n" + extra + "\n## DoD Checklist")
    body = body.replace(
        "## Acceptance Criteria\n", "## Acceptance Criteria\n\n```\n- [ ] in a fence\n```\n", 1
    )
    body = body.replace("\n", "\r\n")
    assert body.count("- [ ]") == original.count("- [ ]") + 3
    set_body(fx, body)
    expected = body
    for text in acc_items_text(original):
        expected = expected.replace(f"- [ ] {text}", f"- [x] {text}")
    ticked = expected
    set_body(fx, ticked, ("after-issue-p1", "after-acceptance-p1"))
    log = tmp_path / "w.jsonl"
    r = accepter(tmp_path, "accept", "api#500", "--apply", fx=fx, log=log)
    assert r.returncode == 0, r.stderr
    written = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    assert [w["op"] for w in written] == ["patch-body", "comment", "close"]
    assert written[0]["body"] == ticked
    assert ticked.count("- [ ]") == 3 and "\r\n" in ticked
    assert json.loads(r.stdout)["readback"]["unchecked"] == 0


def acc_items_text(body: str) -> list[str]:
    return [item.text for item in acc.body_items(body) if not item.checked]


def test_a_moved_item_with_an_unchecked_box_is_ticked(tmp_path: Path) -> None:
    fx = accept_fx(tmp_path)
    set_body(fx, body_of_fx(fx).replace("- [x] Проверка", "- [ ] Проверка"))
    r = accepter(tmp_path, "accept", "api#500", fx=fx)
    assert json.loads(r.stdout)["tick"] == ["AC1", "AC2", "AC3", "DoD1", "DoD2"]


def test_accept_apply_with_the_card_outside_ac_check_writes_and_only_warns(
    tmp_path: Path,
) -> None:
    fx = accept_fx(tmp_path)
    doc = read(fx / "item.json")
    doc["data"]["repository"]["issue"]["projectItems"]["nodes"][0]["fieldValueByName"]["name"] = (
        "On Review"
    )
    put(fx, "item", doc)
    log = tmp_path / "w.jsonl"
    r = accepter(tmp_path, "accept", "api#500", "--apply", fx=fx, log=log)
    assert r.returncode == 0, r.stderr
    assert ops(log) == ["patch-body", "comment", "close"]
    assert "the card is in On Review, not AC Check" in json.loads(r.stdout)["warnings"]


def test_a_read_back_error_names_the_writes_already_made(tmp_path: Path) -> None:
    fx = accept_fx(tmp_path)
    doc = read(fx / "after-issue-p1.json")
    issue = doc["data"]["repository"]["issue"]
    issue["state"] = "OPEN"
    put(fx, "after-issue-p1", doc)
    r = accepter(tmp_path, "accept", "api#500", "--apply", fx=fx, log=tmp_path / "w.jsonl")
    assert r.returncode == 3
    assert "(written: patch-body, comment, close)" in error_of(r)


def test_the_read_back_is_retried_with_a_pause_until_it_is_clean() -> None:
    answers = [({}, ["not closed yet"]), ({}, ["not closed yet"]), ({"ok": True}, [])]
    slept: list[float] = []
    result = acc.retry_readback(lambda: answers.pop(0), 1.0, slept.append)
    assert result == ({"ok": True}, []) and slept == [1.0, 1.0]


def test_the_read_back_gives_up_after_three_attempts() -> None:
    calls: list[int] = []

    def check() -> tuple[dict[str, Any], list[str]]:
        calls.append(1)
        return ({}, ["still open"])

    slept: list[float] = []
    assert acc.retry_readback(check, 1.0, slept.append) == ({}, ["still open"])
    assert len(calls) == acc.READBACK_ATTEMPTS == 3 and slept == [1.0, 1.0]
    calls.clear()
    slept.clear()
    acc.retry_readback(check, 0.0, slept.append)  # the offline seam: no pause
    assert len(calls) == 3 and slept == []


@pytest.mark.parametrize(
    ("evidence", "read_as"), [("не выполнено", "not_done"), ("перенесён в #285", "moved")]
)
def test_a_done_row_whose_evidence_reads_as_something_else_is_a_stop(
    tmp_path: Path, evidence: str, read_as: str
) -> None:
    spec = good_rows()
    spec["AC1"] = {"status": "done", "evidence": evidence}
    assert stops_of(table(tmp_path, spec)) == [
        f"AC1: this evidence reads as {read_as} in the table; use status {read_as} "
        "or write another evidence"
    ]


def test_dedupe_ignores_surrounding_blanks_but_not_a_newer_script_comment() -> None:
    mark, other = acc.TABLE_MARK, acc.RETURNED_MARK
    old = acc.Comment(1, "a", "t", f"{mark}\nbody\n")
    returned = acc.Comment(2, "a", "t", f"{other}\nback")
    assert acc.already_posted([old], mark, f"\n{mark}\nbody") is old
    assert acc.already_posted([old, returned], mark, f"{mark}\nbody") is None
    assert acc.already_posted([], mark, "x") is None
    assert acc.already_posted([old], acc.ACCEPTED_MARK, f"{mark}\nbody") is None


# ─── review round C ─────────────────────────────────────────────────────────
def one_json_line(r: subprocess.CompletedProcess[str]) -> str:
    return error_of(r)


def test_a_failing_read_back_fixture_names_the_writes_already_made(tmp_path: Path) -> None:
    fx = accept_fx(tmp_path)
    (fx / "after-issue-p1.json").unlink()
    r = accepter(tmp_path, "accept", "api#500", "--apply", fx=fx, log=tmp_path / "w.jsonl")
    assert r.returncode == 3
    message = one_json_line(r)
    assert message.startswith("cannot read fixture") and message.endswith(
        "(written: patch-body, comment, close)"
    )


def test_a_failing_card_write_after_the_comment_names_the_comment_in_table(
    tmp_path: Path,
) -> None:
    fx = fixtures_of(STEP_AUTHOR, tmp_path)
    (fx / "set-status.json").unlink()
    r = table(tmp_path, good_rows(), "--apply", fixtures=fx, log=tmp_path / "w.jsonl")
    assert r.returncode == 3
    assert one_json_line(r).endswith("(written: comment)")


def test_a_failing_card_write_after_the_comment_names_the_comment_in_return(
    tmp_path: Path,
) -> None:
    fx = accept_fx(tmp_path, STEP_RETURN)
    (fx / "set-status.json").unlink()
    r = accepter(
        tmp_path, "return", "api#500", "--fail", "AC1=x", "--apply", fx=fx, log=tmp_path / "w"
    )
    assert r.returncode == 3
    assert one_json_line(r).endswith("(written: comment)")


def test_a_card_read_back_that_differs_names_comment_and_card(tmp_path: Path) -> None:
    fx = accept_fx(tmp_path, STEP_RETURN)
    doc = read(fx / "readback.json")
    doc["data"]["repository"]["issue"]["projectItems"]["nodes"][0]["fieldValueByName"]["name"] = (
        "Todo"
    )
    put(fx, "readback", doc)
    r = accepter(
        tmp_path, "return", "api#500", "--fail", "AC1=x", "--apply", fx=fx, log=tmp_path / "w"
    )
    assert r.returncode == 3
    assert one_json_line(r).endswith("(written: comment, set-status)")


def test_an_error_before_any_write_has_no_written_note(tmp_path: Path) -> None:
    fx = accept_fx(tmp_path)
    (fx / "item.json").unlink()
    r = accepter(tmp_path, "accept", "api#500", "--apply", fx=fx, log=tmp_path / "w")
    assert r.returncode == 3
    assert "written" not in one_json_line(r)


def add_returned_after_the_table(fx: Path) -> None:
    edit_acceptance(
        fx,
        lambda issue, _: issue["comments"]["nodes"].append(
            {
                "databaseId": 8003,
                "author": {"login": "axyi"},
                "createdAt": "2026-10-30T11:00:00Z",
                "body": acc.RETURNED_MARK + "\nold return",
            }
        ),
    )


def test_return_only_warns_when_the_table_is_older_than_a_return(tmp_path: Path) -> None:
    fx = accept_fx(tmp_path, STEP_RETURN)
    add_returned_after_the_table(fx)
    r = accepter(tmp_path, "return", "api#500", "--fail", "AC1=x", fx=fx)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["warnings"][0] == (
        "таблица старше возврата #8003: исполнитель не выложил новую таблицу "
        "(таблицу, выложенную вручную после возврата, укажите через --table-comment ID)"
    )


def test_a_bad_shaped_card_response_after_the_mutation_still_names_set_status(
    tmp_path: Path,
) -> None:
    fx = accept_fx(tmp_path, STEP_RETURN)
    put(fx, "set-status", {"data": {"updateProjectV2ItemFieldValue": None}})
    r = accepter(
        tmp_path, "return", "api#500", "--fail", "AC1=x", "--apply", fx=fx, log=tmp_path / "w"
    )
    assert r.returncode == 3
    message = one_json_line(r)
    assert "updateProjectV2ItemFieldValue" in message
    assert message.endswith("(written: comment, set-status)")


def test_a_moved_row_accepted_on_a_body_note_always_warns(tmp_path: Path) -> None:
    for command, step in (("accept", STEP_ACCEPT), ("return", STEP_RETURN)):
        fx = accept_fx(tmp_path / command, step)
        extra = ("--fail", "AC1=x") if command == "return" else ()
        # step 04's table has AC3 not done; make AC3 a moved row for the return case
        if command == "return":
            moved = read(STEP_ACCEPT / "fixtures" / "acceptance-p1.json")
            edit_acceptance(
                fx,
                lambda issue, _, src=moved: issue.update(
                    body=src["data"]["repository"]["issue"]["body"],
                    comments=src["data"]["repository"]["issue"]["comments"],
                ),
            )
        r = accepter(tmp_path, command, "api#500", *extra, fx=fx)
        assert r.returncode == 0, r.stderr
        assert MOVED_WARNING in json.loads(r.stdout)["warnings"], command


def test_a_moved_row_that_is_returned_does_not_also_warn(tmp_path: Path) -> None:
    fx = accept_fx(tmp_path)
    set_body(fx, body_of_fx(fx).replace(" перенесён в #285 решением техлида", ""))
    out = json.loads(accepter(tmp_path, "accept", "api#500", fx=fx).stdout)
    assert [i["id"] for i in out["return_items"]] == ["AC3"]
    assert MOVED_WARNING not in out["warnings"]
