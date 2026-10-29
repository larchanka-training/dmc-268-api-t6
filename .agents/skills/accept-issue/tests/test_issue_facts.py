"""Contract tests for scripts/issue_facts.py.

Offline: no network, no tokens, no live `gh`. An autouse guard puts a fake `gh`
first on PATH that fails with exit 97, so any request that escapes the fixture
seam (`ACCEPT_ISSUE_FIXTURE_DIR`) or a test's own fake is an error, never a call.

Covers: byte-exact golden output for four recorded issues (api#117 one manual
close, api#35 and api#56 merge -> reopen -> manual, api#268 PRs in both repos)
and six synthetic sets (a timeline over two pages, review threads over three
pages, the Commit / ProjectV2 / unknown closers, an OPEN issue, the forms of a
`Refs` line, checkboxes in blockquotes, fences and other sections); the REF
grammar, exit codes 2 and 3, and the argv the script hands to `gh`.
"""
# SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/accept-issue/tests/test_issue_facts.py

from __future__ import annotations

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
SCRIPT = HERE.parent / "scripts" / "issue_facts.py"
FIXTURES = HERE / "fixtures"
GOLDEN = HERE / "golden"
API = "larchanka-training/dmc-268-api-t6"
UI = "larchanka-training/dmc-268-ui-t6"


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


facts = _load(SCRIPT, "issue_facts")


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
    # A paging bug loops on a page that always has a next page: bound the run.
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


def facts_of(case: str, ref: str = "api#13") -> dict[str, Any]:
    r = run(ref, fixtures=FIXTURES / case)
    assert r.returncode == 0, r.stderr
    assert r.stderr == ""
    doc = json.loads(r.stdout)
    assert isinstance(doc, dict)
    return doc


# ─── golden output ──────────────────────────────────────────────────────────
CASES: list[tuple[str, str]] = [
    ("real-117", "api#117"),
    ("real-35", "api#35"),
    ("real-56", "api#56"),
    ("real-268", "api#268"),
    ("timeline-pages", "api#13"),
    ("threads-pages", "api#13"),
    ("closers", "api#13"),
    ("open-issue", "api#13"),
    ("refs-forms", "api#13"),
    ("checkboxes", "api#13"),
    ("event-order", "api#13"),
]


@pytest.mark.parametrize(("case", "ref"), CASES, ids=[c for c, _ in CASES])
def test_golden(case: str, ref: str) -> None:
    r = run(ref, fixtures=FIXTURES / case)
    assert r.returncode == 0, r.stderr
    assert r.stderr == ""
    assert r.stdout == (GOLDEN / f"{case}.json").read_text(encoding="utf-8")


def test_output_is_stable_json() -> None:
    r = run("api#35", fixtures=FIXTURES / "real-35")
    doc = json.loads(r.stdout)
    assert r.stdout == json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    assert "Staging-окружение" in r.stdout  # no \u escapes
    assert doc["schema"] == 1
    assert set(doc) == {"schema", "issue", "closing", "checkboxes", "prs"}
    keys = [(p["ref"].split("#")[0], int(p["ref"].split("#")[1])) for p in doc["prs"]]
    assert keys == sorted(keys)
    assert keys[0][1] == 10 and keys[1][1] == 36  # numeric, not "#10" < "#148" < "#36"


# ─── recorded issues: the facts the acceptance rules rest on ────────────────
def test_real_117_one_manual_close() -> None:
    closing = facts_of("real-117", "api#117")["closing"]
    assert [e["kind"] for e in closing["events"]] == ["closed"]
    assert closing["last_closer"] == "manual"
    assert closing["ever_closed_by_merge"] is False
    assert closing["events"][0]["closer_ref"] is None


@pytest.mark.parametrize(
    ("case", "ref", "pr"), [("real-35", "api#35", 45), ("real-56", "api#56", 60)]
)
def test_real_merge_reopen_manual(case: str, ref: str, pr: int) -> None:
    closing = facts_of(case, ref)["closing"]
    events = closing["events"]
    assert [e["kind"] for e in events] == ["closed", "reopened", "closed"]
    assert [e.get("closer") for e in events] == ["merge", None, "manual"]
    assert events[0]["closer_ref"] == f"{API}#{pr}"
    assert closing["last_closer"] == "manual"
    assert closing["ever_closed_by_merge"] is True


def test_real_268_prs_in_both_repos() -> None:
    doc = facts_of("real-268", "api#268")
    assert doc["closing"]["last_closer"] == "manual"
    assert doc["closing"]["ever_closed_by_merge"] is False
    prs = {p["ref"]: p for p in doc["prs"]}
    assert {r.split("#")[0] for r in prs} == {API, UI}
    assert sum(1 for p in prs.values() if p["refs"]) == 6
    assert all(len(p["head_sha"]) == 40 for p in prs.values())
    assert doc["checkboxes"]["ac"]["total"] == 6
    assert doc["checkboxes"]["dod"]["total"] == 5


# ─── synthetic sets ─────────────────────────────────────────────────────────
def test_timeline_over_two_pages_is_deduplicated() -> None:
    doc = facts_of("timeline-pages")
    assert [e["kind"] for e in doc["closing"]["events"]] == ["closed", "reopened", "closed"]
    by_ref = {p["ref"]: p for p in doc["prs"]}
    assert list(by_ref) == [f"{API}#50", f"{API}#51", f"{UI}#7"]
    assert by_ref[f"{API}#50"]["will_close"] is True  # seen on both pages, one of them closing
    assert by_ref[f"{API}#51"]["ci"] is None
    assert by_ref[f"{UI}#7"]["ci"] == "FAILURE"


def test_review_threads_over_one_hundred() -> None:
    (pr,) = facts_of("threads-pages")["prs"]
    assert pr["unresolved_threads"] == 5  # 2 on the first page, 2 on the second, 1 on the third


def test_closers_commit_project_and_unknown() -> None:
    closing = facts_of("closers")["closing"]
    closes = [e for e in closing["events"] if e["kind"] == "closed"]
    assert [(e["closer"], e["closer_ref"]) for e in closes] == [
        ("commit", "a" * 40),
        ("project", None),
        ("mystery", None),
    ]
    assert closes[0]["actor"] is None
    assert closes[1]["state_reason"] == "NOT_PLANNED"
    assert closes[2]["state_reason"] is None
    assert closing["ever_closed_by_merge"] is True  # the commit close counts
    assert closing["last_closer"] == "mystery"


def test_events_are_sorted_by_time_not_by_api_order() -> None:
    # The fixture lists the 11:00 close first, then the 09:00 close, then the reopen.
    # Without the sort last_closer would be the 09:00 merge.
    closing = facts_of("event-order")["closing"]
    assert [e["at"] for e in closing["events"]] == [
        "2026-10-03T09:00:00Z",
        "2026-10-03T10:00:00Z",
        "2026-10-03T11:00:00Z",
    ]
    assert [e["kind"] for e in closing["events"]] == ["closed", "reopened", "closed"]
    assert closing["last_closer"] == "manual"


def test_closer_ref_carries_the_closing_prs_own_repo() -> None:
    # api#13 closed by a ui PR: the ref names the ui repo, not the issue's.
    first = facts_of("event-order")["closing"]["events"][0]
    assert (first["closer"], first["closer_ref"]) == ("merge", f"{UI}#70")


def test_open_issue_has_no_last_closer() -> None:
    doc = facts_of("open-issue")
    assert doc["issue"]["state"] == "OPEN"
    assert doc["issue"]["state_reason"] == "REOPENED"
    assert doc["closing"]["last_closer"] is None
    assert doc["closing"]["ever_closed_by_merge"] is True
    (pr,) = doc["prs"]
    assert (pr["state"], pr["merged_at"], pr["ci"], pr["will_close"]) == (
        "OPEN",
        None,
        "PENDING",
        True,
    )
    assert pr["refs"] is False  # a `Closes` line is not a `Refs` line
    assert pr["unresolved_threads"] == 1


def test_refs_forms() -> None:
    refs = {
        p["ref"].replace(API, "api").replace(UI, "ui"): p["refs"]
        for p in facts_of("refs-forms")["prs"]
    }
    assert refs == {
        "api#80": True,  # `Refs #12, #13`
        "api#81": False,  # `Refs #130` is another issue
        "api#85": True,  # the title is searched too; the body is null
        "api#86": False,  # a `Refs` in the middle of a sentence is not a Refs line
        "api#87": True,  # lowercase, colon
        "ui#82": False,  # a bare `Refs #13` in a ui PR is ui#13
        "ui#83": True,  # the full form
        "ui#84": True,  # a bare and a full reference on one line
        "ui#94": True,  # the repo name of a full reference is case-insensitive
        "someone/else-repo#9": False,  # names someone/else-repo#13
        # Forms outside the team grammar: the PR stays listed, refs stays false.
        "api#88": False,  # `**Refs** #13`
        "api#89": False,  # `Refs: [#13](url)`
        "api#90": False,  # `Refs <issue url>`
        "api#91": False,  # `Refs` and `#13` on separate lines
        # A Refs line in fenced code or in an HTML comment is not a Refs line.
        "api#92": False,
        "api#93": False,
    }


def test_refs_depend_on_the_issue_repo() -> None:
    # The same ui PR text: bare `#13` names ui#13 when the issue lives in ui.
    pr = {"title": "t", "body": "Refs #13"}
    assert facts.names_issue(pr, UI, (UI, 13)) is True
    assert facts.names_issue(pr, UI, (API, 13)) is False
    assert facts.names_issue(pr, API, (API, 13)) is True


def test_checkboxes_only_inside_the_two_sections() -> None:
    boxes = facts_of("checkboxes")["checkboxes"]
    assert boxes["outside"] == 4  # before the sections, 2 in Scope, after a `#` heading
    assert [i["text"] for i in boxes["ac"]["items"]] == [
        "AC one is done",
        "AC two is open",
        "AC three, star bullet, capital X",
        "AC four, nested",
        "AC five, under a deeper heading",
        "AC six, the heading repeats",
    ]
    assert (boxes["ac"]["total"], boxes["ac"]["checked"]) == (6, 2)
    assert [i["checked"] for i in boxes["dod"]["items"]] == [True, False, True]
    assert (boxes["dod"]["total"], boxes["dod"]["checked"]) == (3, 2)


def test_checkboxes_edge_cases() -> None:
    assert facts.build_checkboxes("")["outside"] == 0
    crlf = "## DoD Checklist\r\n\r\n- [x] one\r\n- [ ] two\r\n"
    assert facts.build_checkboxes(crlf)["dod"]["total"] == 2
    # A fence closes only with the same character and at least the same length.
    body = (
        "## Acceptance Criteria\n````\n- [ ] in\n```\n- [ ] still in\n````\n"
        "- [ ] out of the fence\n"
    )
    ac = facts.build_checkboxes(body)["ac"]
    assert [i["text"] for i in ac["items"]] == ["out of the fence"]
    # A section ends at a heading of the same level, not at a deeper one.
    body = "## Acceptance Criteria\n### Sub\n- [ ] a\n## Other\n- [ ] b\n"
    boxes = facts.build_checkboxes(body)
    assert boxes["ac"]["total"] == 1
    assert boxes["outside"] == 1
    # A heading inside a fence is text; a hash line without a space is not a heading.
    body = "## Acceptance Criteria\n```\n## Other\n```\n#nospace\n- [ ] a\n"
    assert facts.build_checkboxes(body)["ac"]["total"] == 1


def ac_texts(body: str) -> list[str]:
    ac = facts.build_checkboxes("## Acceptance Criteria\n" + body)["ac"]
    return [i["text"] for i in ac["items"]]


def test_fence_openers_and_closers() -> None:
    # A one-line ```x``` is code span text, not a fence: nothing after it is swallowed.
    assert ac_texts("```x```\n- [ ] a\n") == ["a"]
    # A backtick fence's info string may not hold a backtick; a tilde fence's may.
    assert ac_texts("``` a`b\n- [ ] a\n") == ["a"]
    assert ac_texts("~~~ a`b\n- [ ] hidden\n~~~\n- [ ] b\n") == ["b"]
    # An info string opens a fence but never closes one.
    assert ac_texts("```py\n- [ ] hidden\n```py\n- [ ] still hidden\n```\n- [ ] b\n") == ["b"]
    # A closer is bare: trailing blanks are fine, trailing text is not.
    assert ac_texts("```\n- [ ] hidden\n``` \n- [ ] b\n") == ["b"]
    assert ac_texts("```\n- [ ] hidden\n``` x\n- [ ] still hidden\n```\n- [ ] c\n") == ["c"]
    # The other fence character does not close, a shorter run does not close.
    assert ac_texts("~~~~\n- [ ] hidden\n```\n~~~\n- [ ] hidden\n~~~~\n- [ ] b\n") == ["b"]
    # Up to 3 spaces of indent open and close a fence; 4 do not.
    assert ac_texts("   ```\n- [ ] hidden\n   ```\n- [ ] b\n") == ["b"]
    assert ac_texts("    ```\n- [ ] a\n") == ["a"]
    assert ac_texts("```\n- [ ] hidden\n    ```\n- [ ] still hidden\n```\n- [ ] b\n") == ["b"]


def test_html_comments_are_skipped() -> None:
    assert ac_texts("<!-- - [ ] one line -->\n- [ ] a\n") == ["a"]
    assert ac_texts("<!--\n- [ ] hidden\n## Other\n-->\n- [ ] a\n") == ["a"]
    assert ac_texts("- [ ] a <!-- hint --> b\n") == ["a  b"]
    assert ac_texts("before <!-- one\n- [ ] hidden\n--> after\n- [ ] a\n") == ["a"]
    # Comment markers inside a fence are code, not comments.
    assert ac_texts("```\n<!--\n```\n- [ ] a\n") == ["a"]
    # A comment that never closes hides the rest.
    assert ac_texts("- [ ] a\n<!-- never closed\n- [ ] hidden\n") == ["a"]
    # A heading inside a comment does not end the section.
    body = "## Acceptance Criteria\n<!--\n## Other\n-->\n- [ ] a\n"
    assert facts.build_checkboxes(body)["ac"]["total"] == 1


def pr_node(body: str | None, title: str = "t") -> dict[str, Any]:
    return {"title": title, "body": body}


def test_refs_search_skips_fences_and_comments() -> None:
    def names(body: str | None, title: str = "t") -> bool:
        return bool(facts.names_issue(pr_node(body, title), API, (API, 13)))

    assert names("Refs #13")
    assert not names("```\nRefs #13\n```")
    assert not names("~~~md\nRefs #13\n~~~")
    assert not names("<!--\nRefs #13\n-->")
    assert not names("<!-- Refs #13 -->")
    assert names("<!-- x -->\nRefs #13")
    assert names("```\ncode\n```\nRefs #13")
    # The title and the body are scanned apart: a fence in one never reaches the other.
    assert names("Refs #13", title="```")
    assert names(None, title="Refs #13")
    # Blockquotes are skipped for checkboxes only; a quoted Refs line is simply not
    # a line that starts with `Refs`.
    assert not names("> Refs #13")


def test_setext_headings_are_not_recognised() -> None:
    body = "Acceptance Criteria\n===================\n- [ ] a\n"
    assert facts.build_checkboxes(body)["outside"] == 1


# ─── REF grammar and exit codes ─────────────────────────────────────────────
@pytest.mark.parametrize(
    ("text", "parsed"),
    [
        ("35", (API, 35)),
        ("api#35", (API, 35)),
        ("ui#7", (UI, 7)),
        ("some-org/some.repo#12", ("some-org/some.repo", 12)),
    ],
)
def test_parse_ref(text: str, parsed: tuple[str, int]) -> None:
    assert facts.parse_ref(text) == parsed


@pytest.mark.parametrize(
    "text", ["", "x", "api#", "api#0", "#5", "0", "-3", "a/b/c#1", "web#1", "api #1"]
)
def test_bad_ref_exits_2(text: str) -> None:
    r = run(text)
    assert r.returncode == 2
    assert "bad REF" in error_of(r)


def test_usage_errors_exit_2() -> None:
    for args in ([], ["api#1", "api#2"], ["--bogus"]):
        r = run(*args)
        assert r.returncode == 2, args
        error_of(r)


def test_bad_ref_never_reaches_gh() -> None:
    # The guard `gh` would exit 97 and the error would say so; exit 2 means it never ran.
    assert run("api#0").returncode == 2


def test_gh_failure_exits_3() -> None:
    r = run("api#1")  # no fixture seam: the guard gh exits 97
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
    r = run("api#1")
    assert r.returncode == 3
    assert "exited 1" in error_of(r)


def test_gh_timeout_exits_3(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise subprocess.TimeoutExpired("gh", 1)

    monkeypatch.setattr(facts.subprocess, "run", boom)
    with pytest.raises(SystemExit) as exc:
        facts.main(["api#1"])
    assert exc.value.code == 3
    assert "timed out" in json.loads(capsys.readouterr().err)["error"]


def test_issue_not_found_exits_3(tmp_path: Path) -> None:
    write(tmp_path, "issue-p1", {"data": {"repository": {"issue": None}}})
    r = run("api#99", fixtures=tmp_path)
    assert r.returncode == 3
    assert error_of(r) == f"issue {API}#99 not found"
    write(tmp_path, "issue-p1", {"data": {"repository": None}})
    assert run("api#99", fixtures=tmp_path).returncode == 3


def write(directory: Path, name: str, data: Any) -> None:
    (directory / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")


def test_graphql_errors_exit_3(tmp_path: Path) -> None:
    write(tmp_path, "issue-p1", {"errors": [{"message": "boom"}], "data": None})
    r = run("api#1", fixtures=tmp_path)
    assert r.returncode == 3
    assert "boom" in error_of(r)


def test_missing_and_broken_fixtures_exit_3(tmp_path: Path) -> None:
    r = run("api#1", fixtures=tmp_path)
    assert r.returncode == 3
    assert "cannot read fixture" in error_of(r)
    (tmp_path / "issue-p1.json").write_text("not json", encoding="utf-8")
    r = run("api#1", fixtures=tmp_path)
    assert r.returncode == 3
    assert "not JSON" in error_of(r)
    write(tmp_path, "issue-p1", ["a list"])
    assert run("api#1", fixtures=tmp_path).returncode == 3
    write(tmp_path, "issue-p1", {"data": None})
    assert run("api#1", fixtures=tmp_path).returncode == 3


def test_undecodable_fixture_exits_3(tmp_path: Path) -> None:
    (tmp_path / "issue-p1.json").write_bytes(b'{"data": "\xff\xfe"}')
    r = run("api#1", fixtures=tmp_path)
    assert r.returncode == 3
    assert "cannot read fixture" in error_of(r)


def test_undecodable_gh_output_exits_3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body = "import sys\nsys.stdout.buffer.write(b'\\xff\\xfe not utf-8')\n"
    prepend_path(monkeypatch, fake_gh(tmp_path / "bin", body))
    r = run("api#1")
    assert r.returncode == 3
    assert "cannot read the output" in error_of(r)


def test_empty_fixture_dir_variable_exits_3_without_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACCEPT_ISSUE_FIXTURE_DIR", "")
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "api#1"], capture_output=True, encoding="utf-8", timeout=60
    )
    assert r.returncode == 3
    assert "set but empty" in error_of(r)


def _issue_response(nodes: Any, **extra: Any) -> dict[str, Any]:
    issue = {
        "title": "t",
        "url": "u",
        "state": "OPEN",
        "stateReason": None,
        "body": "",
        "timelineItems": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes},
    }
    issue.update(extra)
    return {"data": {"repository": {"issue": issue}}}


@pytest.mark.parametrize(
    "response",
    [
        _issue_response("not a list"),
        _issue_response([{"__typename": "ClosedEvent", "actor": {"login": "a"}}]),  # no createdAt
        _issue_response([{"__typename": "ClosedEvent", "createdAt": "x", "closer": 5}]),
        _issue_response([{"__typename": "CrossReferencedEvent", "source": None}]),
        _issue_response([], state=None),
        _issue_response([], body=5),
        {
            "data": {
                "repository": {
                    "issue": {
                        "title": "t",
                        "url": "u",
                        "state": "OPEN",
                        "stateReason": None,
                        "body": "",
                        "timelineItems": {
                            "pageInfo": {"hasNextPage": True, "endCursor": None},
                            "nodes": [],
                        },
                    }
                }
            }
        },
    ],
    ids=["nodes", "no-createdAt", "closer", "source", "state", "body", "no-cursor"],
)
def test_unexpected_shape_exits_3(tmp_path: Path, response: dict[str, Any]) -> None:
    write(tmp_path, "issue-p1", response)
    r = run("api#1", fixtures=tmp_path)
    assert r.returncode == 3
    assert "unexpected shape" in error_of(r)


def test_a_missing_second_page_exits_3(tmp_path: Path) -> None:
    page = _issue_response([])
    page["data"]["repository"]["issue"]["timelineItems"]["pageInfo"] = {
        "hasNextPage": True,
        "endCursor": "C1",
    }
    write(tmp_path, "issue-p1", page)
    r = run("api#1", fixtures=tmp_path)
    assert r.returncode == 3
    assert "issue-p2.json" in error_of(r)


# ─── the argv handed to gh ──────────────────────────────────────────────────
GH_LOG_FAKE = """\
import json, os, sys
from pathlib import Path

argv = sys.argv[1:]
with open(os.environ["GH_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(argv) + "\\n")
fields = dict(a.split("=", 1) for a in argv if "=" in a and not a.startswith("query="))
query = next(a for a in argv if a.startswith("query="))
cursor = fields.get("c")
if "pullRequest(number" in query:
    name = "threads-larchanka-training_dmc-268-api-t6-60-" + {"TC1": "p2", "TC2": "p3"}[cursor]
else:
    name = {None: "issue-p1"}[cursor]
sys.stdout.write(Path(os.environ["FAKE_DIR"], name + ".json").read_text(encoding="utf-8"))
"""


def test_argv_and_paging_through_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "gh.log"
    prepend_path(monkeypatch, fake_gh(tmp_path / "bin", GH_LOG_FAKE))
    monkeypatch.setenv("GH_LOG", str(log))
    monkeypatch.setenv("FAKE_DIR", str(FIXTURES / "threads-pages"))
    r = run("ui#7")
    assert r.returncode == 0, r.stderr
    (pr,) = json.loads(r.stdout)["prs"]
    assert pr["unresolved_threads"] == 5
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(calls) == 3  # the issue, then two follow-up thread pages
    for argv in calls:
        assert argv[:3] == ["api", "graphql", "-f"]
        assert "owner=larchanka-training" in argv
        # String variables go with -f (never reinterpreted), the number with -F.
        pairs = list(zip(argv, argv[1:], strict=False))
        assert ("-f", "owner=larchanka-training") in pairs
        assert any(flag == "-f" and v.startswith("name=") for flag, v in pairs)
        assert any(flag == "-F" and v.startswith("number=") for flag, v in pairs)
    issue_call, p2, p3 = calls
    assert "name=dmc-268-ui-t6" in issue_call and "number=7" in issue_call
    assert not any(a.startswith("c=") for a in issue_call)  # first page: no cursor
    assert "name=dmc-268-api-t6" in p2 and "number=60" in p2 and "c=TC1" in p2
    assert "c=TC2" in p3
    # The query text itself: dropping any of these silently loses a fact.
    query = next(a for a in issue_call if a.startswith("query="))
    for needle in (
        "CLOSED_EVENT",
        "REOPENED_EVENT",
        "CROSS_REFERENCED_EVENT",
        "timelineItems(first: 100, after: $c",
        "closer {",
        "stateReason",
        "willCloseTarget",
        "statusCheckRollup",
        "reviewThreads(first: 100",
        "headRefOid",
        "mergedAt",
        "commits(last: 1)",
    ):
        assert needle in query, needle
    assert "timelineItems(last" not in query
    assert query.count("last: 1") == 1  # only the PR's last commit
    threads_query = next(a for a in p2 if a.startswith("query="))
    assert "reviewThreads(first: 100, after: $c)" in threads_query
    assert "isResolved" in threads_query
    # Read-only: queries only, never a mutation, never a REST write.
    for argv in calls:
        assert "mutation" not in " ".join(argv)
        assert "-X" not in argv


def test_timeline_cursor_goes_to_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body = (
        "import os, sys\n"
        "from pathlib import Path\n"
        "name = 'issue-p2' if any(a == 'c=CURSOR1' for a in sys.argv) else 'issue-p1'\n"
        "path = Path(os.environ['FAKE_DIR'], name + '.json')\n"
        "sys.stdout.write(path.read_text(encoding='utf-8'))\n"
    )
    prepend_path(monkeypatch, fake_gh(tmp_path / "bin", body))
    monkeypatch.setenv("FAKE_DIR", str(FIXTURES / "timeline-pages"))
    r = run("13")
    assert r.returncode == 0, r.stderr
    assert r.stdout == (GOLDEN / "timeline-pages.json").read_text(encoding="utf-8")
