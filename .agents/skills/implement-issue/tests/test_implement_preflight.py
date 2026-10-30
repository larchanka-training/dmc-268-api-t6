"""Contract tests for scripts/preflight.py.

Offline: no network, no tokens, no live `gh`. An autouse guard puts a fake `gh`
first on PATH that fails with exit 97, so any request that escapes the fixture
seam (`IMPLEMENT_ISSUE_FIXTURE_DIR`) or a test's own fake is an error, never a call.

Covers: every `examples/*/` case (stdout byte-equal to `expected.json`, exit code from
the verdict, the invocation read from the case's README); synthetic issues for the
selection rules, the `Refs` matcher, the way a pull request names a part (ranges, lists,
`of <ref>`, the tie of a pull request with several issues, the `What` heading, numbers past
nine digits), malformed numbering, paging of blockers, of the timeline and of the comments
past one page, the Kickoff and the board Status; the third verdict, `accept`, and the issue
returned at acceptance; the REF grammar, exit codes 2 and 3, and the argv and queries the
script hands to `gh` (read-only, never a mutation); the branch pattern of the skill texts.
"""
# SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/implement-issue/tests/test_implement_preflight.py  # noqa: E501

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
SCRIPT = SKILL / "scripts" / "preflight.py"
EXAMPLES = SKILL / "examples"
API = "larchanka-training/dmc-268-api-t6"
UI = "larchanka-training/dmc-268-ui-t6"
REPOS = {"api": API, "ui": UI}


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


pf = _load(SCRIPT, "implement_issue_preflight")


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
    monkeypatch.delenv("IMPLEMENT_ISSUE_FIXTURE_DIR", raising=False)


def run(*args: str, fixtures: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if fixtures is not None:
        env["IMPLEMENT_ISSUE_FIXTURE_DIR"] = str(fixtures)
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


# ─── builders ───────────────────────────────────────────────────────────────
def write(directory: Path, name: str, data: Any) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")


def page_info(next_cursor: str | None) -> dict[str, Any]:
    return {"hasNextPage": next_cursor is not None, "endCursor": next_cursor}


def blocker(number: int, state: str = "OPEN", repo: str = API, title: str = "") -> dict[str, Any]:
    return {
        "number": number,
        "title": title or f"blocker {number}",
        "state": state,
        "repository": {"nameWithOwner": repo},
    }


MERGED_AT = "2026-10-20T12:00:00Z"


def pr(
    number: int,
    *,
    title: str = "feat: x",
    body: str = "",
    state: str = "MERGED",
    repo: str = API,
    merged_at: str | None = MERGED_AT,
) -> dict[str, Any]:
    """A CrossReferencedEvent whose source is a pull request (`merged_at` only counts
    for a MERGED one)."""
    return {
        "__typename": "CrossReferencedEvent",
        "source": {
            "__typename": "PullRequest",
            "number": number,
            "title": title,
            "url": f"https://github.com/{repo}/pull/{number}",
            "body": body,
            "state": state,
            "mergedAt": merged_at if state == "MERGED" else None,
            "repository": {"nameWithOwner": repo},
        },
    }


def refs_pr(
    number: int, what: str, *, refs: str = "Refs #13", title: str = "feat: x", **kw: Any
) -> dict[str, Any]:
    """A pull request in the template's shape: What, then Refs."""
    return pr(number, title=title, body=f"## What\n\n{what}\n\n## Refs\n\n{refs}\n", **kw)


def project_item(
    status: str | None, *, number: int = 12, login: str = "larchanka-training"
) -> dict[str, Any]:
    value = None if status is None else {"__typename": "x", "name": status}
    return {
        "project": {"number": number, "owner": {"__typename": "Organization", "login": login}},
        "fieldValueByName": value,
    }


def comment(body: str, created_at: str = "2026-10-21T09:00:00Z", number: int = 1) -> dict[str, Any]:
    return {
        "url": f"https://github.com/{API}/issues/13#issuecomment-{number}",
        "createdAt": created_at,
        "body": body,
    }


def issue_response(
    *,
    body: str = "",
    state: str = "OPEN",
    blocked: list[dict[str, Any]] | None = None,
    timeline: list[dict[str, Any]] | None = None,
    comments: list[dict[str, Any]] | None = None,
    items: list[dict[str, Any]] | None = None,
    assignees: tuple[str, ...] = ("axyi",),
    blocked_next: str | None = None,
    timeline_next: str | None = None,
    comments_next: str | None = None,
    sub_issues: tuple[int, int] = (0, 0),
) -> dict[str, Any]:
    return {
        "data": {
            "repository": {
                "issue": {
                    "title": "t",
                    "url": "https://github.com/x/y/issues/13",
                    "state": state,
                    "body": body,
                    "assignees": {"nodes": [{"login": a} for a in assignees]},
                    "subIssuesSummary": {"total": sub_issues[0], "completed": sub_issues[1]},
                    "projectItems": {
                        "pageInfo": page_info(None),
                        "nodes": [project_item("In Progress")] if items is None else items,
                    },
                    "blockedBy": {"pageInfo": page_info(blocked_next), "nodes": blocked or []},
                    "timelineItems": {
                        "pageInfo": page_info(timeline_next),
                        "nodes": timeline or [],
                    },
                    "comments": {
                        "pageInfo": page_info(comments_next),
                        "nodes": comments or [],
                    },
                }
            }
        }
    }


def follow_up(field: str, nodes: list[dict[str, Any]], next_cursor: str | None) -> dict[str, Any]:
    connection = {"pageInfo": page_info(next_cursor), "nodes": nodes}
    return {"data": {"repository": {"issue": {field: connection}}}}


TABLE = "<!-- accept-issue:table v1 -->"
ACCEPTED = "<!-- accept-issue:accepted v1 -->"
RETURNED = "<!-- accept-issue:returned v1 -->"
KICKOFF = "\n## Kickoff — с чего начать\n\n**Ветка:** `feat/<n>-x`\n"
TWO_PARTS = (
    "## Scope\n\n"
    "- Ч.1 (PR в ui): first\n"
    "  - Граница PR: only the script\n"
    "  - Готово, когда: pytest green\n"
    "- Ч.2 (PR): second\n"
    "\n## Acceptance Criteria\n\n"
    "- [ ] Ч.1: first works\n- [ ] Ч.2: second works\n- [ ] common one\n"
) + KICKOFF


THREE_PARTS = TWO_PARTS.replace("- Ч.2 (PR): second\n", "- Ч.2 (PR): second\n- Ч.3 (PR): third\n")


def facts(
    tmp_path: Path, response: dict[str, Any], *args: str, ref: str = "api#13"
) -> tuple[int, dict[str, Any]]:
    """Run on a one-page issue; (exit code, parsed stdout). Exits 0 and 1 carry a document."""
    write(tmp_path / "fx", "issue-p1", response)
    r = run(ref, *args, fixtures=tmp_path / "fx")
    assert r.returncode in (0, 1), r.stderr
    assert r.stderr == ""
    doc = json.loads(r.stdout)
    assert isinstance(doc, dict)
    return r.returncode, doc


def parts_of(doc: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {p["n"]: p for p in doc["parts"]}


# ─── the examples ───────────────────────────────────────────────────────────
CASES = sorted(p.name for p in EXAMPLES.iterdir() if p.is_dir())


def invocation(case: str) -> list[str]:
    """The arguments of the case's `preflight.py` line, from its README."""
    readme = (EXAMPLES / case / "README.md").read_text(encoding="utf-8")
    block = re.search(r"```sh\n(preflight\.py [^\n]+)\n```", readme)
    assert block, f"{case}/README.md has no ```sh preflight.py ...``` block"
    return shlex.split(block.group(1))[1:]


def test_there_are_at_least_four_examples() -> None:
    assert len(CASES) >= 4


@pytest.mark.parametrize("case", CASES)
def test_example(case: str) -> None:
    folder = EXAMPLES / case
    readme = (folder / "README.md").read_text(encoding="utf-8")
    assert readme.startswith(
        f"<!-- SYNC: mirrored in dmc-268-{{ui,api}}-t6/.agents/skills/implement-issue/"
        f"examples/{case}/README.md -->\n\n"
    )
    r = run(*invocation(case), fixtures=folder / "fixtures")
    expected = (folder / "expected.json").read_text(encoding="utf-8")
    assert r.stdout == expected
    assert r.stderr == ""
    assert r.returncode == (0 if json.loads(expected)["verdict"] == "go" else 1)


def test_the_examples_cover_every_decision() -> None:
    seen = set()
    for case in CASES:
        doc = json.loads((EXAMPLES / case / "expected.json").read_text(encoding="utf-8"))
        seen.add((doc["verdict"], (doc["selected"] or {}).get("reason")))
    assert {
        ("stop", None),
        ("accept", None),
        ("go", "whole-task"),
        ("go", "first-todo"),
        ("go", "explicit"),
    } <= seen


def test_output_is_stable_json(tmp_path: Path) -> None:
    write(tmp_path / "fx", "issue-p1", issue_response(body=TWO_PARTS))
    r = run("api#13", fixtures=tmp_path / "fx")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    assert r.stdout == json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    assert "Ветка" in r.stdout  # no \u escapes
    assert doc["schema"] == 1
    assert set(doc) == {
        "schema",
        "issue",
        "blockers",
        "parts",
        "common_acceptance_criteria",
        "task_prs",
        "held",
        "selected",
        "kickoff",
        "verdict",
        "reasons",
        "warnings",
    }
    assert set(doc["issue"]) == {
        "repo",
        "number",
        "title",
        "url",
        "state",
        "assignees",
        "board_status",
        "sub_issues",
    }
    assert set(doc["parts"][0]) == {
        "n",
        "label",
        "scope",
        "boundary",
        "done_when",
        "acceptance_criteria",
        "prs",
        "status",
    }


# ─── stop: closed issue and blockers ────────────────────────────────────────
def test_a_closed_issue_stops_before_anything_else(tmp_path: Path) -> None:
    response = issue_response(body=TWO_PARTS, state="CLOSED", blocked=[blocker(5)])
    code, doc = facts(tmp_path, response, "--part", "9")  # not even the bad part is reached
    assert code == 1
    assert doc["verdict"] == "stop"
    assert doc["reasons"] == [f"issue {API}#13 is CLOSED"]
    assert doc["selected"] is None


def test_an_open_blocker_stops_and_is_named_with_its_title(tmp_path: Path) -> None:
    blocked = [
        blocker(265, title="plans", repo=API),
        blocker(7, state="CLOSED"),
        blocker(4, title="ui side", repo=UI),
    ]
    code, doc = facts(tmp_path, issue_response(body=TWO_PARTS, blocked=blocked))
    assert code == 1
    assert doc["reasons"] == [f"blocked by {API}#265 — plans", f"blocked by {UI}#4 — ui side"]
    assert [b["ref"] for b in doc["blockers"]] == [f"{API}#7", f"{API}#265", f"{UI}#4"]
    assert [b["state"] for b in doc["blockers"]] == ["CLOSED", "OPEN", "OPEN"]
    assert doc["selected"] is None
    assert len(doc["parts"]) == 2  # still reported


def test_closed_blockers_do_not_block(tmp_path: Path) -> None:
    code, doc = facts(tmp_path, issue_response(body=TWO_PARTS, blocked=[blocker(7, "CLOSED")]))
    assert code == 0
    assert doc["selected"] == {"part": 1, "reason": "first-todo"}


def test_a_blocker_in_ac_check_still_blocks(tmp_path: Path) -> None:
    # The board column does not matter: only the issue's own state, literally, decides.
    code, doc = facts(tmp_path, issue_response(blocked=[blocker(7, "OPEN", title="in AC Check")]))
    assert code == 1
    assert doc["reasons"] == [f"blocked by {API}#7 — in AC Check"]


def test_blockers_over_three_pages(tmp_path: Path) -> None:
    fx = tmp_path / "fx"
    first = [blocker(n, "CLOSED") for n in range(1, 4)]
    write(fx, "issue-p1", issue_response(blocked=first, blocked_next="B1"))
    write(fx, "blocked-p2", follow_up("blockedBy", [blocker(n, "CLOSED") for n in (4, 5)], "B2"))
    write(fx, "blocked-p3", follow_up("blockedBy", [blocker(6, "OPEN", title="last")], None))
    r = run("api#13", fixtures=fx)
    assert r.returncode == 1, r.stderr
    doc = json.loads(r.stdout)
    assert [b["ref"].split("#")[1] for b in doc["blockers"]] == ["1", "2", "3", "4", "5", "6"]
    assert doc["reasons"] == [f"blocked by {API}#6 — last"]


def test_timeline_over_three_pages(tmp_path: Path) -> None:
    fx = tmp_path / "fx"
    noise = [pr(n, body="no refs here") for n in range(100, 103)]
    write(fx, "issue-p1", issue_response(body=TWO_PARTS, timeline=noise, timeline_next="T1"))
    page2 = [refs_pr(21, "Part 1 of #13: x")]
    write(fx, "timeline-p2", follow_up("timelineItems", noise + page2, "T2"))
    page3 = [refs_pr(22, "Part 2 of #13: y", state="OPEN")]
    write(fx, "timeline-p3", follow_up("timelineItems", page3, None))
    r = run("api#13", fixtures=fx)
    assert r.returncode == 1, r.stderr
    doc = json.loads(r.stdout)
    # the pull requests of pages 2 and 3 both count (the ones of page 1 are listed once)
    assert [(p["n"], p["status"]) for p in doc["parts"]] == [(1, "done"), (2, "open_pr")]
    assert doc["reasons"] == [
        f"Part 2 already has an open pull request: {API}#22 (OPEN) — finish it or pass --part"
    ]


# ─── the Refs matcher ───────────────────────────────────────────────────────
def counts(tmp_path: Path, node: dict[str, Any], ref: str = "api#13") -> bool:
    body = "## Scope\n\n- Ч.1 (PR): one\n- Ч.2 (PR): two\n"
    _, doc = facts(tmp_path, issue_response(body=body, timeline=[node]), ref=ref)
    listed = [p for part in doc["parts"] for p in part["prs"]]
    assert len(listed) in (0, 1)
    return bool(listed)


@pytest.mark.parametrize(
    "refs",
    [
        "Refs #13",
        "refs #13",
        "REFS #13",
        "- Refs #13",
        "Refs: #13",
        "Refs #12, #13",
        "Refs #12 and #13",
        "Refs #12; #13",
        f"Refs {API}#13",
        f"Refs {API.upper()}#13",
        "Refs #13 (the first part)",
    ],
)
def test_refs_forms_that_count(tmp_path: Path, refs: str) -> None:
    assert counts(tmp_path, refs_pr(30, "Part 1 of #13: x", refs=refs))


@pytest.mark.parametrize(
    "refs",
    [
        "Refs #14",
        "**Refs** #13",
        "Refs: [#13](https://github.com/x/y/issues/13)",
        "Refs\n#13",
        "See Refs #13",
        "Closes #13",
        "Part 1 of #13",
        "```\nRefs #13\n```",
        "<!-- Refs #13 -->",
        "> Refs #13",
        "Refs #130",
        f"Refs {UI}#13",
    ],
)
def test_refs_forms_that_do_not_count(tmp_path: Path, refs: str) -> None:
    assert not counts(tmp_path, refs_pr(30, "Part 1 of #13: x", refs=refs))


def test_a_bare_refs_from_the_other_repo_is_not_this_issue(tmp_path: Path) -> None:
    # `Refs #13` in a ui pull request is ui#13; the api issue needs the full form.
    assert not counts(tmp_path, refs_pr(30, "Part 1 of #13: x", repo=UI))
    assert counts(tmp_path, refs_pr(30, "Part 1 of api#13: x", refs=f"Refs {API}#13", repo=UI))
    # ...and the other way round, for a ui issue.
    assert counts(tmp_path, refs_pr(30, "Part 1 of #13: x", repo=UI), ref="ui#13")
    assert not counts(tmp_path, refs_pr(30, "Part 1 of #13: x", repo=API), ref="ui#13")


def test_a_pull_request_is_listed_once_however_often_it_cross_references(tmp_path: Path) -> None:
    node = refs_pr(32, "Part 1 of #13: x")
    _, doc = facts(tmp_path, issue_response(body=TWO_PARTS, timeline=[node, node]))
    assert len(parts_of(doc)[1]["prs"]) == 1


def test_issue_cross_references_are_not_pull_requests(tmp_path: Path) -> None:
    nodes: list[dict[str, Any]] = [
        {"__typename": "CrossReferencedEvent", "source": {"__typename": "Issue"}},
        {"__typename": "SomethingElse"},
    ]
    code, doc = facts(tmp_path, issue_response(body=TWO_PARTS, timeline=nodes))
    assert code == 0
    assert all(p["prs"] == [] for p in doc["parts"])


# ─── markdown scanning ──────────────────────────────────────────────────────
def test_visible_lines_skips_fences_comments_and_quotes() -> None:
    text = (
        "keep 1\n"
        "```\nfenced\n```\n"
        "~~~sh\nalso fenced\n~~~\n"
        "````\n```\nstill inside\n```\n````\n"
        "> quoted\n"
        "<!-- one line -->\n"
        "before <!-- inline --> after\n"
        "<!--\nmulti\nline\n-->\n"
        "keep 2\n"
    )
    assert list(pf.visible_lines(text, skip_quotes=True)) == [
        "keep 1",
        "before  after",
        "keep 2",
    ]
    assert list(pf.visible_lines(text, skip_quotes=False))[1] == "> quoted"


# ─── how a pull request names a part ────────────────────────────────────────
# The real first lines of the `## What` of ui#177 (Part 1 of api#268) and api#358 (Part 2 of
# #273): each also mentions another part, as context.
UI_177_WHAT = (
    "Part 1 of api#268: a deterministic script, "
    "`.agents/skills/issue-graph/scripts/issue_graph.py`, that builds the dependency graph of a "
    "sprint from GitHub native relations. It uses no LLM. The `SKILL.md` and publishing come in "
    "part 2."
)
API_358_WHAT = (
    "Part 2 of #273: the sprint start ritual. Part 1, the `## 9. Спринты` section this extends, "
    "is merged in #357."
)


def named_in(
    tmp_path: Path, node: dict[str, Any], parts: int = 2, ref: str = "api#13"
) -> list[int]:
    scope = "".join(f"- Ч.{n} (PR): scope {n}\n" for n in range(1, parts + 1))
    response = issue_response(body=f"## Scope\n\n{scope}", timeline=[node])
    _, doc = facts(tmp_path, response, ref=ref)
    return [p["n"] for p in doc["parts"] if p["prs"]]


@pytest.mark.parametrize(
    ("title", "what", "expected"),
    [
        ("Part 2 of #13: y", "", [2]),  # the title alone
        ("feat: y", "Part 2 of #13: y", [2]),  # What alone
        ("Part 1 of #13", "Part 2 of #13: y", [1]),  # the title wins over What
        ("feat: y", "SYNC copy of ui#1 (Part 2 of #13): y", [2]),  # the SYNC-copy form
        ("part 2", "", [2]),
        ("PART 2", "", [2]),
        ("Ч.2: y", "", [2]),
        ("ч.2: y", "", [2]),
        ("Ч. 2: y", "", [2]),
        ("Part 1 and Part 2", "", [1, 2]),
        ("Parts 1 and 2", "", [1, 2]),  # a plural label with a list names every part
        ("Part 2.5", "", [2]),
        ("Counterpart 2", "", []),
        ("Part 3 of #13: nonexistent", "", []),
        ("feat: y", "", []),
        ("feat: y", "First line.\nPart 2 of #13: second line", []),  # first line only
        ("feat: y", "<!-- Part 2 -->\nPart 1 of #13: x", [1]),  # comments are not text
        ("feat: y", "```\nPart 2\n```\nPart 1 of #13: x", [1]),  # nor are fences
        # What names only the first part it mentions: the rest is context
        ("feat: y", "Part 1 of #13: x. Part 2 comes later.", [1]),
        ("feat: y", "Part 2 of #13: x. Part 1 is merged.", [2]),  # first by position, not smallest
        ("feat: y", "SYNC copy of ui#1 (Part 2 of #13): y; Part 1 is merged", [2]),
        ("Part 1 of #13", "Part 2 of #13: y", [1]),  # the title still wins
        # create-issue's German label and the team's Russian prose, in any case form
        ("Teil 2 von #13: y", "", [2]),
        ("TEIL 2", "", [2]),
        ("feat: y", "Teil 2 von #13: y. Teil 1 ist gemergt.", [2]),
        ("Часть 2 из #13: y", "", [2]),
        ("Спецификация Части 1", "", [1]),
        ("Реализация Частью 2", "", [2]),
        ("ЧАСТЬ 2", "", [2]),
        ("часть 1", "", [1]),
        ("Часть 1 и Часть 2", "", [1, 2]),
        ("feat: y", "Спецификация Части 1 (#13): y", [1]),
        ("feat: y", "Часть 2 из #13: y. Часть 1 влита.", [2]),
        ("feat: y", "Часть 2 из #13: y; Part 1 is merged", [2]),  # first by position, across forms
        ("feat: y", "Part 2 of #13: y; Часть 1 влита", [2]),
        ("feat: y", "Teil 2 von #13: y; Ч.1 влита", [2]),
        # words that merely contain a label are not labels
        ("Particle 1", "", []),
        ("department 2", "", []),
        ("Anteil 2", "", []),
        ("Teilnehmer 2", "", []),
        ("Teil", "", []),
        ("Частично 2", "", []),
        ("Запчасть 2", "", []),
        ("Участью 2", "", []),
        ("Часть первая", "", []),
        ("feat: y", "Anteil 2 of #13", []),
        # a range names no single part, whatever the label
        ("Parts 1–3", "", []),
        ("Parts 1", "", []),  # a plural label alone is no mention
        ("Части 1–3", "", []),
        ("Части 1-3", "", []),
        ("Ч.1–3: y", "", []),
        ("Teile 1–3", "", []),
        ("Teil 1 – 2", "", []),
        ("Part 1—2", "", []),
        ("feat: y", "Части 1–3 of #13: y", []),
    ],
)
def test_naming_a_part(tmp_path: Path, title: str, what: str, expected: list[int]) -> None:
    assert named_in(tmp_path, refs_pr(40, what, title=title)) == expected


# A range is its own alternative of the pattern: it is the match "first by position" lands
# on, so a single mention after it never stands in for it. The upper end is a whole number
# not followed by a letter, a digit or `-`, so «Part 1 – 2nd step» is not a range.
@pytest.mark.parametrize(
    ("title", "what", "expected"),
    [
        # a range anywhere in the title: the title names nothing, the first line of What decides
        ("Parts 1–2 (Part 3 follows)", "", []),
        ("Parts 1–2 (Part 3 follows)", "Part 2 of #13: y", [2]),
        ("Part 3 follows (Parts 1–2)", "", []),
        ("Part 1-2 and Part 3", "", []),
        ("Ч.1–2, Ч.3 next", "", []),
        # a range as the first mention of What: the line names nothing
        ("feat: y", "Parts 1–2 of #13: both. Part 3 is next.", []),
        ("feat: y", "Parts 1-2 of #13: x. Part 3 follows.", []),
        ("feat: y", "Part 1–2 of #13: both. Part 3 is next.", []),
        ("feat: y", "Parts 1 – 2 of #13: both. Part 3 is next.", []),
        ("feat: y", "Parts 1\xa0–\xa02 of #13: both. Part 3 is next.", []),
        ("feat: y", "Parts 1\t—\t2 of #13: both. Part 3 is next.", []),
        ("feat: y", "Ч.1–2 of #13: both. Ч.3 is next.", []),
        ("feat: y", "Части 1–2 из #13: обе. Часть 3 следующая.", []),
        ("feat: y", "Частей 1–2 из #13: обе. Часть 3 следующая.", []),
        ("feat: y", "Teile 1–2 von #13: beide. Teil 3 folgt.", []),
        ("feat: y", "- Parts 1–2 of #13: both. Part 3 is next.", []),
        # a range after the first single mention does not matter
        ("feat: y", "Part 3 of #13: x. Parts 1–2 are merged.", [3]),
        # not ranges: the upper end is followed by a letter or a `-`
        ("Part 1 – 2nd step", "", [1]),
        ("Part 1 — 2026-10-29", "", [1]),
        ("Part 1-2nd step", "", [1]),
        ("feat: y", "Part 1 – 2nd step. Part 3 comes later.", [1]),
        ("feat: y", "Part 2 — 2026-10-29: x. Part 3 comes later.", [2]),
        # any horizontal whitespace after the label, a no-break space included
        ("Part\xa02", "", [2]),
        ("Part\t2", "", [2]),
        ("Part \xa02", "", [2]),
        ("Ч.\xa02", "", [2]),
        ("Teil\xa02", "", [2]),
        ("Часть\xa02", "", [2]),
        ("feat: y", "Part\xa02 of #13: x", [2]),
    ],
)
def test_a_range_names_no_part_and_no_later_mention_stands_in(
    tmp_path: Path, title: str, what: str, expected: list[int]
) -> None:
    assert named_in(tmp_path, refs_pr(60, what, title=title), parts=3) == expected


def test_a_merged_range_does_not_finish_the_part_that_follows_it(tmp_path: Path) -> None:
    # `Parts 1–2 of #13: both. Part 3 is next.` is a stray, not Part 3: the 3-part issue
    # still starts at Part 1 and nothing is done.
    node = refs_pr(61, "Parts 1–2 of #13: both. Part 3 is next.")
    code, doc = facts(tmp_path, issue_response(body=THREE_PARTS, timeline=[node]))
    assert (code, doc["selected"]) == (0, {"part": 1, "reason": "first-todo"})
    assert [p["status"] for p in doc["parts"]] == ["todo", "todo", "todo"]
    assert doc["warnings"] == [
        f"pull requests with Refs that name no part of this issue: {API}#61 (MERGED)"
    ]


def test_an_open_range_stops_as_a_pull_request_that_names_no_part(tmp_path: Path) -> None:
    node = refs_pr(62, "Parts 1-2 of #13: x. Part 3 follows.", state="OPEN")
    code, doc = facts(tmp_path, issue_response(body=THREE_PARTS, timeline=[node]))
    assert (code, doc["verdict"], doc["selected"]) == (1, "stop", None)
    assert doc["reasons"] == [stray_reason(f"{API}#62 (OPEN)")]
    assert [p["status"] for p in doc["parts"]] == ["todo", "todo", "todo"]


def test_ui_203_is_a_stray_of_ui_186(tmp_path: Path) -> None:
    # The real first line of the What of ui#203: `- Parts 1–3 of ui#186: …`.
    what = (
        "- Parts 1–3 of ui#186: a reason text for known limit and failure codes on the run "
        "list and the run page, an audit log at `/audit`, and a quality dashboard at "
        "`/analytics`.\n- Part 4 (new limit codes, task 24) is not in this PR."
    )
    node = refs_pr(203, what, title="feat(analytics): add limit reasons", refs="Refs #186", repo=UI)
    assert named_in(tmp_path, node, parts=4, ref="ui#186") == []
    node = {**node, "source": {**node["source"], "state": "OPEN"}}
    scope = "".join(f"- Part {n} (PR): scope {n}\n" for n in range(1, 5))
    response = issue_response(body=f"## Scope\n\n{scope}", timeline=[node])
    code, doc = facts(tmp_path, response, ref="ui#186")
    assert (code, doc["selected"]) == (1, None)
    assert doc["reasons"] == [stray_reason(f"{UI}#203 (OPEN)")]


# «Part K of <ref>» is the part of the issue <ref>. When that is not the issue under check the
# mention is skipped as if absent: the next one decides in a title, and a What line whose
# first mention is skipped names nothing. A label without `of <ref>` counts as before.
@pytest.mark.parametrize(
    ("title", "what", "expected"),
    [
        ("Part 2 of #14: x", "", []),
        ("Part 2 of api#14: x", "", []),
        ("Part 2 of ui#13: x", "", []),
        (f"Part 2 of {UI}#13: x", "", []),
        ("Part 2 of #130", "", []),
        ("Part 2 of #1", "", []),
        ("Part 2 of #13: x", "", [2]),
        ("Part 2 of api#13: x", "", [2]),
        ("Part 2 of API#13: x", "", [2]),
        (f"Part 2 of {API}#13: x", "", [2]),
        (f"Part 2 of {API.upper()}#13: x", "", [2]),
        ("Part 2 of\xa0#13: x", "", [2]),
        ("Part 2 of #13.", "", [2]),
        # the next match decides in a title; a skipped title falls back to What
        ("Part 2 of #14 and Part 1 of #13", "", [1]),
        ("Part 1 of #13 and Part 2 of #14", "", [1]),
        ("Part 2 of #14: x", "Part 1 of #13: z", [1]),
        ("Part 2 of #14: x", "Part 2 of #14: z", []),
        # the first mention of What decides alone: skipped, the line names nothing
        ("feat: y", "Part 2 of #14: x. Part 1 of #13 is merged.", []),
        ("feat: y", "SYNC copy of ui#1 (Part 2 of #14): x. Part 1 of #13 is merged.", []),
        ("feat: y", "Part 1 of #13: x. Part 2 of #14 follows.", [1]),
        # German and Russian carry the same ref
        ("Teil 2 von #14: y", "", []),
        ("Часть 2 из #14: y", "", []),
        ("Teil 2 von api#13: y", "", [2]),
        ("Часть 2 из #13: y", "", [2]),
        # a label without `of <ref>` counts as today
        ("Part 2", "", [2]),
        ("Part 2 (#14)", "", [2]),
        ("Part 2 for #14", "", [2]),
        ("Part 2 of the plan in #14", "", [2]),
        ("Part 2 offline #14", "", [2]),
        ("Part 2 of ", "", [2]),
        ("Part 2 of foo#14", "", [2]),
        # a range is a range, whatever follows it
        ("Parts 1–2 of #14: x. Part 2 of #13", "", []),
    ],
)
def test_a_part_of_another_issue_is_not_a_part_of_this_one(
    tmp_path: Path, title: str, what: str, expected: list[int]
) -> None:
    assert named_in(tmp_path, refs_pr(65, what, title=title)) == expected


def test_a_pull_request_with_refs_to_two_issues_names_a_part_of_one(tmp_path: Path) -> None:
    node = refs_pr(66, "does a thing", title="Part 2 of #14: x", refs="Refs #13, #14")
    # for #13 the pull request is a stray ...
    code, doc = facts(tmp_path, issue_response(body=TWO_PARTS, timeline=[node]))
    assert (code, doc["selected"]) == (0, {"part": 1, "reason": "first-todo"})
    assert [p["status"] for p in doc["parts"]] == ["todo", "todo"]
    assert doc["warnings"] == [
        f"pull requests with Refs that name no part of this issue: {API}#66 (MERGED)"
    ]
    # ... for #14 it is Part 2
    code, doc = facts(tmp_path, issue_response(body=TWO_PARTS, timeline=[node]), ref="api#14")
    assert [p["ref"] for p in parts_of(doc)[2]["prs"]] == [f"{API}#66"]
    assert [p["status"] for p in doc["parts"]] == ["todo", "done"]
    assert doc["warnings"] == []


def test_an_open_pull_request_for_another_issues_part_stops_this_one(tmp_path: Path) -> None:
    node = refs_pr(66, "does a thing", title="Part 2 of #14: x", refs="Refs #13, #14", state="OPEN")
    code, doc = facts(tmp_path, issue_response(body=TWO_PARTS, timeline=[node]))
    assert (code, doc["selected"]) == (1, None)
    assert doc["reasons"] == [stray_reason(f"{API}#66 (OPEN)")]
    _, doc = facts(tmp_path, issue_response(body=TWO_PARTS, timeline=[node]), ref="api#14")
    assert doc["selected"] == {"part": 1, "reason": "first-todo"}  # Part 2 is open, Part 1 is free


@pytest.mark.parametrize(
    ("what", "issue_ref", "repo", "refs", "expected"),
    [
        # a ui pull request for an api issue says where the issue is
        ("Part 1 of api#13: x", "api#13", UI, f"Refs {API}#13", [1]),
        (f"Part 1 of {API}#13: x", "api#13", UI, f"Refs {API}#13", [1]),
        ("SYNC copy of api#9 (Part 1 of api#13): x", "api#13", UI, f"Refs {API}#13", [1]),
        # a bare #13 in the ui pull request is ui#13
        ("Part 1 of #13: x", "api#13", UI, f"Refs {API}#13", []),
        ("Part 1 of #13: x", "ui#13", UI, "Refs #13", [1]),
        ("Part 1 of ui#13: x", "ui#13", UI, "Refs #13", [1]),
        ("Part 1 of api#13: x", "ui#13", UI, "Refs #13", []),
        ("Part 1 of ui#13: x", "api#13", API, "Refs #13", []),
    ],
)
def test_a_part_of_is_resolved_against_the_repo_of_the_pull_request(
    tmp_path: Path, what: str, issue_ref: str, repo: str, refs: str, expected: list[int]
) -> None:
    node = refs_pr(67, what, refs=refs, repo=repo)
    assert named_in(tmp_path, node, ref=issue_ref) == expected


def test_a_body_without_what_names_by_its_first_line(tmp_path: Path) -> None:
    node = pr(41, body="Mirror of ui#56 part 2\n\nRefs #13\n")
    assert named_in(tmp_path, node) == [2]
    node = pr(41, body="\n\nSomething\nPart 2\n\nRefs #13\n")
    assert named_in(tmp_path, node) == []


def test_an_empty_what_names_nothing(tmp_path: Path) -> None:
    node = pr(42, body="## What\n\n## Why\n\nPart 2 of #13\n\n## Refs\n\nRefs #13\n")
    assert named_in(tmp_path, node) == []


def test_part_1_is_not_part_12(tmp_path: Path) -> None:
    body = "## Scope\n\n" + "".join(f"- Ч.{n} (PR): scope {n}\n" for n in range(1, 13))
    forms = ("Ч.", "Part ", "Teil ", "Часть ", "Части ")
    for title, expected in [(f"{label}{n}: x", n) for label in forms for n in (1, 12)]:
        node = refs_pr(43, "x", title=title)
        _, doc = facts(tmp_path, issue_response(body=body, timeline=[node]))
        assert [p["n"] for p in doc["parts"] if p["prs"]] == [expected], title


def test_a_title_naming_two_parts_counts_for_both_and_warns(tmp_path: Path) -> None:
    node = refs_pr(44, "x", title="Part 1 and Part 2 of #13")
    assert named_in(tmp_path, node) == [1, 2]
    _, doc = facts(tmp_path, issue_response(body=TWO_PARTS, timeline=[node]))
    assert doc["warnings"] == [
        f"pull request {API}#44 names several parts in its title (1, 2): it counts for each of them"
    ]


def test_a_what_naming_two_parts_counts_for_the_first_and_does_not_warn(tmp_path: Path) -> None:
    node = refs_pr(45, "Part 1 of #13: x. Part 2 comes later.")
    _, doc = facts(tmp_path, issue_response(body=TWO_PARTS, timeline=[node]))
    assert [n for n, part in parts_of(doc).items() if part["prs"]] == [1]
    assert doc["warnings"] == []


def test_the_real_first_lines_name_the_part_of_their_own_issue(tmp_path: Path) -> None:
    ui_177 = refs_pr(177, UI_177_WHAT, title="feat: y", refs=f"Refs {API}#268", repo=UI)
    assert named_in(tmp_path, ui_177, ref="api#268") == [1]
    api_358 = refs_pr(358, API_358_WHAT, title="docs: y", refs="Refs #273")
    assert named_in(tmp_path, api_358, ref="api#273") == [2]


def test_a_merged_pull_request_that_mentions_the_next_part_does_not_finish_it(
    tmp_path: Path,
) -> None:
    # ui#177 is Part 1 only, though its What also says "part 2": a two-part issue goes on to
    # Part 2 (not "every part has a merged pull request"), a three-part one to Part 2 (not 3).
    ui_177 = refs_pr(
        177,
        UI_177_WHAT,
        title="feat(agents): add the issue-graph script",
        refs=f"Refs {API}#268",
        repo=UI,
    )
    for body in (TWO_PARTS, THREE_PARTS):
        code, doc = facts(tmp_path, issue_response(body=body, timeline=[ui_177]), ref="api#268")
        assert (code, doc["selected"]) == (0, {"part": 2, "reason": "first-todo"})
        assert [p["ref"] for p in parts_of(doc)[1]["prs"]] == [f"{UI}#177"]
        assert parts_of(doc)[2]["status"] == "todo"
        assert doc["warnings"] == []


def test_a_pull_request_that_looks_back_at_part_1_is_part_2_only(tmp_path: Path) -> None:
    # api#358: "Part 2 of #273: … Part 1, … is merged in #357." — Part 1 stays todo.
    api_358 = refs_pr(
        358, API_358_WHAT, title="docs(contributing): the sprint start ritual", refs="Refs #273"
    )
    code, doc = facts(tmp_path, issue_response(body=TWO_PARTS, timeline=[api_358]), ref="api#273")
    assert (code, doc["selected"]) == (0, {"part": 1, "reason": "first-todo"})
    assert parts_of(doc)[1]["prs"] == [] and parts_of(doc)[2]["status"] == "done"
    assert doc["warnings"] == []


def test_a_pull_request_with_refs_that_names_no_part_is_a_warning(tmp_path: Path) -> None:
    node = refs_pr(45, "does a thing", title="feat: no part named")
    code, doc = facts(tmp_path, issue_response(body=TWO_PARTS, timeline=[node]))
    assert code == 0
    assert doc["selected"] == {"part": 1, "reason": "first-todo"}  # not attributed to any part
    assert doc["warnings"] == [
        f"pull requests with Refs that name no part of this issue: {API}#45 (MERGED)"
    ]


# An OPEN pull request with Refs that names no part may be the work of any part: the run
# cannot tell which one is taken, so it stops (unless the part is given).
UNATTRIBUTED = "feat: no part named"


def unattributed(number: int, state: str, **kw: Any) -> dict[str, Any]:
    return refs_pr(number, "does a thing", title=UNATTRIBUTED, state=state, **kw)


def stray_reason(*listed: str) -> str:
    return (
        f"open pull requests with Refs that name no part of this issue: {', '.join(listed)}"
        " — say which part each covers, or pass --part"
    )


def test_an_open_pull_request_that_names_no_part_stops(tmp_path: Path) -> None:
    node = unattributed(46, "OPEN")
    code, doc = facts(tmp_path, issue_response(body=TWO_PARTS, timeline=[node]))
    assert code == 1
    assert (doc["verdict"], doc["selected"]) == ("stop", None)
    assert doc["reasons"] == [stray_reason(f"{API}#46 (OPEN)")]
    assert doc["warnings"] == [  # the warning stays: it is the same fact for a reader of the JSON
        f"pull requests with Refs that name no part of this issue: {API}#46 (OPEN)"
    ]


def test_every_open_pull_request_that_names_no_part_is_listed(tmp_path: Path) -> None:
    timeline = [unattributed(48, "OPEN"), unattributed(47, "OPEN", repo=UI, refs=f"Refs {API}#13")]
    timeline.append(unattributed(49, "MERGED"))  # not listed: only the open ones stop the run
    code, doc = facts(tmp_path, issue_response(body=TWO_PARTS, timeline=timeline))
    assert code == 1
    assert doc["reasons"] == [stray_reason(f"{API}#48 (OPEN)", f"{UI}#47 (OPEN)")]


def test_an_explicit_part_goes_past_an_open_pull_request_that_names_no_part(
    tmp_path: Path,
) -> None:
    node = unattributed(46, "OPEN")
    for part in ("1", "2"):
        code, doc = facts(tmp_path, issue_response(body=TWO_PARTS, timeline=[node]), "--part", part)
        assert (code, doc["selected"]) == (0, {"part": int(part), "reason": "explicit"})
        assert doc["warnings"] == [
            f"pull requests with Refs that name no part of this issue: {API}#46 (OPEN)"
        ]


@pytest.mark.parametrize("state", ["MERGED", "CLOSED"])
def test_a_merged_or_closed_pull_request_that_names_no_part_is_only_a_warning(
    tmp_path: Path, state: str
) -> None:
    node = unattributed(46, state)
    code, doc = facts(tmp_path, issue_response(body=TWO_PARTS, timeline=[node]))
    assert (code, doc["selected"]) == (0, {"part": 1, "reason": "first-todo"})
    assert doc["warnings"] == [
        f"pull requests with Refs that name no part of this issue: {API}#46 ({state})"
    ]


@pytest.mark.parametrize(
    "title",
    ["Part 3 of #13: no such part", "Parts 1–2 of #13", "Частей 3 и 4", "Части 1–2 of #13"],
)
def test_an_open_pull_request_naming_a_part_the_issue_lacks_or_a_range_stops(
    tmp_path: Path, title: str
) -> None:
    node = refs_pr(46, "does a thing", title=title, state="OPEN")
    code, doc = facts(tmp_path, issue_response(body=TWO_PARTS, timeline=[node]))
    assert code == 1
    assert doc["reasons"] == [stray_reason(f"{API}#46 (OPEN)")]


def test_the_open_pull_request_that_names_no_part_stops_before_the_part_checks(
    tmp_path: Path,
) -> None:
    timeline = [unattributed(46, "OPEN"), refs_pr(100, "Part 1 of #13: x", state="OPEN")]
    _, doc = facts(tmp_path, issue_response(body=TWO_PARTS, timeline=timeline))
    assert doc["reasons"] == [stray_reason(f"{API}#46 (OPEN)")]  # not "Part 1 already has…"
    # ...but never before the closed issue and the blockers
    _, doc = facts(
        tmp_path, issue_response(body=TWO_PARTS, timeline=timeline, blocked=[blocker(5)])
    )
    assert doc["reasons"] == [f"blocked by {API}#5 — blocker 5"]


def test_an_open_pull_request_that_names_a_part_is_not_a_stray(tmp_path: Path) -> None:
    node = refs_pr(46, "Part 2 of #13: x", state="OPEN")
    code, doc = facts(tmp_path, issue_response(body=TWO_PARTS, timeline=[node]))
    assert (code, doc["selected"]) == (0, {"part": 1, "reason": "first-todo"})
    assert doc["warnings"] == []


# ─── part status and selection ──────────────────────────────────────────────
def two_part_run(
    tmp_path: Path, states: dict[int, list[str]], *args: str, body: str = TWO_PARTS
) -> tuple[int, dict[str, Any]]:
    timeline = [
        refs_pr(100 * part + i, f"Part {part} of #13: x", state=state)
        for part, listed in states.items()
        for i, state in enumerate(listed)
    ]
    return facts(tmp_path, issue_response(body=body, timeline=timeline), *args)


@pytest.mark.parametrize(
    ("states", "status"),
    [
        ([], "todo"),
        (["MERGED"], "done"),
        (["OPEN"], "open_pr"),
        (["CLOSED"], "todo"),  # closed unmerged: ignored
        (["CLOSED", "CLOSED"], "todo"),
        (["OPEN", "MERGED"], "done"),  # merged wins
        (["CLOSED", "OPEN"], "open_pr"),
    ],
)
def test_part_status(tmp_path: Path, states: list[str], status: str) -> None:
    _, doc = two_part_run(tmp_path, {1: states})
    assert parts_of(doc)[1]["status"] == status
    assert len(parts_of(doc)[1]["prs"]) == len(states)  # all are reported


def test_the_first_part_that_is_not_done_is_taken(tmp_path: Path) -> None:
    code, doc = two_part_run(tmp_path, {1: ["MERGED"]})
    assert (code, doc["selected"]) == (0, {"part": 2, "reason": "first-todo"})
    code, doc = two_part_run(tmp_path, {1: ["CLOSED"]})  # a closed PR does not finish the part
    assert (code, doc["selected"]) == (0, {"part": 1, "reason": "first-todo"})


def test_an_open_pull_request_on_the_first_unfinished_part_stops(tmp_path: Path) -> None:
    code, doc = two_part_run(tmp_path, {1: ["OPEN", "OPEN"]})
    assert code == 1
    assert doc["selected"] is None
    assert doc["reasons"] == [
        "Part 1 already has an open pull request: "
        f"{API}#100 (OPEN), {API}#101 (OPEN) — finish it or pass --part"
    ]


def test_an_open_pull_request_on_a_later_part_does_not_skip_an_earlier_todo(
    tmp_path: Path,
) -> None:
    code, doc = two_part_run(tmp_path, {2: ["OPEN"]})
    assert (code, doc["selected"]) == (0, {"part": 1, "reason": "first-todo"})


def test_every_part_done_is_accept(tmp_path: Path) -> None:
    code, doc = two_part_run(tmp_path, {1: ["MERGED"], 2: ["MERGED"]})
    assert (code, doc["verdict"], doc["selected"]) == (1, "accept", None)
    assert doc["reasons"] == ["every part has a merged pull request — acceptance is next"]
    assert [p["status"] for p in doc["parts"]] == ["done", "done"]


def test_a_done_part_with_a_second_open_pull_request_is_not_accept(tmp_path: Path) -> None:
    # status_of says `done` for MERGED + OPEN, but an open rework PR still has to land.
    code, doc = two_part_run(tmp_path, {1: ["MERGED", "OPEN"], 2: ["MERGED"]})
    assert (code, doc["verdict"], doc["selected"]) == (1, "stop", None)
    assert doc["reasons"] == [
        "every part has a merged pull request, but pull requests are still open: "
        f"{API}#101 (OPEN) — finish them first"
    ]
    # ...once per pull request, however many parts it names
    timeline = [
        refs_pr(100, "x", title="Parts 1 and 2 of #13"),
        refs_pr(101, "x", title="Part 1 and Part 2 of #13", state="OPEN"),
    ]
    code, doc = facts(tmp_path, issue_response(body=TWO_PARTS, timeline=timeline))
    assert (code, doc["verdict"]) == (1, "stop")
    assert doc["reasons"] == [
        "every part has a merged pull request, but pull requests are still open: "
        f"{API}#101 (OPEN) — finish them first"
    ]


def test_an_explicit_part_is_taken_when_acceptance_is_next(tmp_path: Path) -> None:
    code, doc = two_part_run(tmp_path, {1: ["MERGED"], 2: ["MERGED"]}, "--part", "1")
    assert (code, doc["verdict"], doc["selected"]) == (0, "go", {"part": 1, "reason": "explicit"})


def test_an_explicit_part_is_always_taken(tmp_path: Path) -> None:
    code, doc = two_part_run(tmp_path, {1: ["OPEN"]}, "--part", "2")
    assert (code, doc["selected"]) == (0, {"part": 2, "reason": "explicit"})
    assert doc["warnings"] == []
    # a part that is done or has an open PR: taken, with a warning that names the PRs
    code, doc = two_part_run(tmp_path, {1: ["MERGED"]}, "--part", "1")
    assert (code, doc["selected"]) == (0, {"part": 1, "reason": "explicit"})
    assert doc["warnings"] == [f"Part 1 already has a merged pull request: {API}#100 (MERGED)"]
    code, doc = two_part_run(tmp_path, {1: ["OPEN"]}, "--part", "1")
    assert code == 0
    assert doc["warnings"] == [f"Part 1 already has an open pull request: {API}#100 (OPEN)"]
    # ...and when every part is done, an explicit part still goes
    code, doc = two_part_run(tmp_path, {1: ["MERGED"], 2: ["MERGED"]}, "--part", "2")
    assert (code, doc["selected"]) == (0, {"part": 2, "reason": "explicit"})


def test_an_explicit_part_does_not_lift_a_blocker(tmp_path: Path) -> None:
    response = issue_response(body=TWO_PARTS, blocked=[blocker(5)])
    code, doc = facts(tmp_path, response, "--part", "2")
    assert (code, doc["verdict"], doc["selected"]) == (1, "stop", None)


@pytest.mark.parametrize("part", ["3", "99"])
def test_a_part_the_issue_does_not_have_exits_2(tmp_path: Path, part: str) -> None:
    write(tmp_path / "fx", "issue-p1", issue_response(body=TWO_PARTS))
    r = run("api#13", "--part", part, fixtures=tmp_path / "fx")
    assert r.returncode == 2
    assert error_of(r) == f"--part {part}: issue {API}#13 has parts 1..2"


def test_a_part_on_an_issue_without_parts_exits_2(tmp_path: Path) -> None:
    write(tmp_path / "fx", "issue-p1", issue_response(body="## Scope\n\n- one thing\n"))
    r = run("api#13", "--part", "1", fixtures=tmp_path / "fx")
    assert r.returncode == 2
    assert error_of(r) == f"--part 1: issue {API}#13 has no parts"


@pytest.mark.parametrize("part", ["0", "-1", "x", "1.5", ""])
def test_a_malformed_part_exits_2_before_any_request(part: str) -> None:
    r = run("api#13", "--part", part)  # the guard gh would exit 97 and the exit would be 3
    assert r.returncode == 2
    error_of(r)


# ─── no parts: the whole task ───────────────────────────────────────────────
def test_no_parts_takes_the_whole_task(tmp_path: Path) -> None:
    code, doc = facts(tmp_path, issue_response(body="## Scope\n\n- one thing\n" + KICKOFF))
    assert code == 0
    assert doc["selected"] == {"part": None, "reason": "whole-task"}
    assert doc["parts"] == [] and doc["task_prs"] == []
    assert doc["warnings"] == []


def test_no_parts_and_an_open_pull_request_with_refs_stops(tmp_path: Path) -> None:
    timeline = [refs_pr(50, "the thing", state="OPEN"), refs_pr(51, "no refs", refs="-")]
    code, doc = facts(tmp_path, issue_response(body="## Scope\n\n- x\n", timeline=timeline))
    assert (code, doc["verdict"], doc["selected"]) == (1, "stop", None)
    assert doc["reasons"] == [
        "the task has no parts and already has a pull request with Refs: "
        f"{API}#50 (OPEN) — a rework PR is a human decision"
    ]
    assert [p["ref"] for p in doc["task_prs"]] == [f"{API}#50"]


def test_no_parts_and_a_merged_pull_request_with_refs_is_accept(tmp_path: Path) -> None:
    timeline = [refs_pr(50, "the thing"), refs_pr(51, "no refs", refs="-")]
    code, doc = facts(tmp_path, issue_response(body="## Scope\n\n- x\n", timeline=timeline))
    assert (code, doc["verdict"], doc["selected"]) == (1, "accept", None)
    assert doc["reasons"] == [
        f"the task has no parts and its pull request with Refs is merged: {API}#50 (MERGED)"
        " — acceptance is next"
    ]
    assert [p["ref"] for p in doc["task_prs"]] == [f"{API}#50"]


def test_no_parts_with_a_merged_and_an_open_pull_request_stops(tmp_path: Path) -> None:
    timeline = [refs_pr(50, "the thing"), refs_pr(52, "a rework", state="OPEN")]
    code, doc = facts(tmp_path, issue_response(body="## Scope\n\n- x\n", timeline=timeline))
    assert (code, doc["verdict"]) == (1, "stop")
    assert doc["reasons"] == [
        "the task has no parts and already has a pull request with Refs: "
        f"{API}#50 (MERGED), {API}#52 (OPEN) — a rework PR is a human decision"
    ]


def test_no_parts_and_only_a_closed_pull_request_goes(tmp_path: Path) -> None:
    timeline = [refs_pr(50, "the thing", state="CLOSED")]
    code, doc = facts(tmp_path, issue_response(body="## Scope\n\n- x\n", timeline=timeline))
    assert code == 0
    assert doc["selected"] == {"part": None, "reason": "whole-task"}
    assert [p["state"] for p in doc["task_prs"]] == ["CLOSED"]  # reported, not counted


# ─── parsing the parts ──────────────────────────────────────────────────────
def test_parts_labels_boundary_and_done_when(tmp_path: Path) -> None:
    body = (
        "## Scope\n\n"
        "- plain bullet, not a part\n"
        "- Ч.1 (отдельный PR): first\n"
        "  - Граница PR: where it stops\n"
        "  - Готово, когда: `pytest` — green\n"
        "- Part 2 (separate PR): second,\n"
        "  continued on the next line\n"
        "  - PR boundary: b2\n"
        "  - Done when: d2\n"
        "- Ч.3: no label\n"
    )
    _, doc = facts(tmp_path, issue_response(body=body))
    one, two, three = (parts_of(doc)[n] for n in (1, 2, 3))
    assert (one["label"], one["scope"]) == ("отдельный PR", "first")
    assert (one["boundary"], one["done_when"]) == ("where it stops", "`pytest` — green")
    assert (two["label"], two["scope"]) == ("separate PR", "second, continued on the next line")
    assert (two["boundary"], two["done_when"]) == ("b2", "d2")
    assert (three["label"], three["boundary"], three["done_when"]) == (None, None, None)


def test_a_part_with_several_bullets_is_one_part(tmp_path: Path) -> None:
    body = "## Scope\n\n- Ч.1 (PR в ui): a\n- Ч.2 (PR): b\n- Ч.2: c\n- Ч.2: d\n"
    _, doc = facts(tmp_path, issue_response(body=body))
    assert [p["n"] for p in doc["parts"]] == [1, 2]
    assert parts_of(doc)[2]["scope"] == "b\nc\nd"
    assert parts_of(doc)[2]["label"] == "PR"


def test_part_bullets_are_found_only_in_scope(tmp_path: Path) -> None:
    body = (
        "## Background\n\n- Ч.7: not here\n\n"
        "## Scope\n\n- Ч.1: yes\n"
        "```\n- Ч.8: fenced\n```\n"
        "<!-- - Ч.9: commented -->\n"
        "> - Ч.10: quoted\n"
        "  - Ч.11: nested, so not a part\n"
        "\n## Dependencies\n\n- Ч.12: not here either\n"
    )
    _, doc = facts(tmp_path, issue_response(body=body))
    assert [p["n"] for p in doc["parts"]] == [1]


def test_the_scope_heading_may_be_any_level(tmp_path: Path) -> None:
    _, doc = facts(
        tmp_path, issue_response(body="### Scope\n\n- Ч.1: yes\n\n## Next\n\n- Ч.2: no\n")
    )
    assert [p["n"] for p in doc["parts"]] == [1]


@pytest.mark.parametrize(
    ("scope", "found"),
    [
        ("- Teil 1: a\n- Teil 3: b\n", "1, 3"),
        ("- Ч.2: a\n- Ч.3: b\n", "2, 3"),
        ("- Ч.1: a\n- Ч.3: b\n", "1, 3"),
        ("- Ч.0: a\n- Ч.1: b\n", "0, 1"),
        ("- Part 1: a\n- Ч.1: b\n- Part 4: c\n", "1, 4"),
    ],
)
def test_malformed_numbering_stops_with_a_reason(tmp_path: Path, scope: str, found: str) -> None:
    code, doc = facts(tmp_path, issue_response(body=f"## Scope\n\n{scope}"))
    assert code == 1
    assert doc["reasons"] == [f"parts in Scope are not numbered 1..k contiguously (found: {found})"]
    assert doc["parts"] == [] and doc["selected"] is None


def test_german_parts_with_their_nested_labels_and_criteria(tmp_path: Path) -> None:
    # What create-issue writes in `de`: Teil N (eigener PR), PR-Grenze, Fertig, wenn, Teil N:
    body = (
        "## Scope\n\n"
        "- Teil 1 (eigener PR): erster\n"
        "  - PR-Grenze: nur das Skript\n"
        "  - Fertig, wenn: pytest grün\n"
        "- Teil 2 (eigener PR): zweiter\n"
        "\n## Acceptance Criteria\n\n"
        "- [ ] Teil 1: eins\n- [ ] Teil 2: zwei\n- [ ] gemeinsam\n"
    )
    _, doc = facts(tmp_path, issue_response(body=body))
    one, two = parts_of(doc)[1], parts_of(doc)[2]
    assert (one["label"], one["scope"]) == ("eigener PR", "erster")
    assert (one["boundary"], one["done_when"]) == ("nur das Skript", "pytest grün")
    assert (two["boundary"], two["done_when"]) == (None, None)
    assert (one["acceptance_criteria"], two["acceptance_criteria"]) == (["eins"], ["zwei"])
    assert doc["common_acceptance_criteria"] == ["gemeinsam"]


def test_the_three_labels_may_be_mixed_in_one_issue(tmp_path: Path) -> None:
    scope = "- Ч.1: a\n- Part 2: b\n- Teil 3: c\n"
    body = f"## Scope\n\n{scope}\n## Acceptance Criteria\n\n- [ ] teil 3: x\n"
    _, doc = facts(tmp_path, issue_response(body=body))
    assert [p["n"] for p in doc["parts"]] == [1, 2, 3]
    assert parts_of(doc)[3]["acceptance_criteria"] == ["x"]


def test_a_part_bullet_without_a_colon_stops_with_a_reason(tmp_path: Path) -> None:
    code, doc = facts(tmp_path, issue_response(body="## Scope\n\n- Ч.1: a\n- Ч.2 - b\n"))
    assert code == 1
    assert doc["reasons"] == ["Scope bullet 'Ч.2 - b' starts with a part number but has no ':'"]


def test_part_criteria_and_common_criteria(tmp_path: Path) -> None:
    body = (
        "## Scope\n\n- Ч.1: a\n- Part 2: b\n\n"
        "## Acceptance Criteria\n\n"
        "- [ ] Ч.1: first A\n"
        "- [x] Ч.1: first B,\n  continued\n"
        "- [ ] Part 2: second A\n"
        "- [ ] Ч.5: no such part\n"
        "- [ ] no prefix\n"
        "- Ч.2: no checkbox\n"
        "> - [ ] quoted\n"
        "```\n- [ ] fenced\n```\n"
        "\n## DoD Checklist\n\n- [ ] Ч.1: in DoD, not AC\n"
    )
    _, doc = facts(tmp_path, issue_response(body=body))
    assert parts_of(doc)[1]["acceptance_criteria"] == ["first A", "first B, continued"]
    assert parts_of(doc)[2]["acceptance_criteria"] == ["second A", "no checkbox"]
    assert doc["common_acceptance_criteria"] == ["no prefix"]
    assert doc["warnings"][0] == "acceptance criterion names Part 5, not in Scope"


# ─── Kickoff, board and issue facts ─────────────────────────────────────────
def test_kickoff_is_read_in_any_language(tmp_path: Path) -> None:
    for heading in ("Kickoff — с чего начать", "Kickoff — where to start", "Kickoff"):
        body = f"## Scope\n\n- x\n\n## {heading}\n\n**Branch:** `feat/1-x`\n\n```\nskipped\n```\n"
        _, doc = facts(tmp_path, issue_response(body=body))
        assert doc["kickoff"] == {"present": True, "text": "**Branch:** `feat/1-x`"}
        assert doc["warnings"] == []


def test_a_missing_kickoff_is_a_warning_not_a_stop(tmp_path: Path) -> None:
    for body in ("", "## Scope\n\n- x\n", "## Kickoff\n\n", "```\n## Kickoff\n\ntext\n```\n"):
        code, doc = facts(tmp_path, issue_response(body=body))
        assert code == 0
        assert doc["kickoff"] == {"present": False, "text": ""}
        assert doc["warnings"] == [
            "the issue has no Kickoff section: read its Background and References"
        ]


def test_the_board_status_is_read_from_board_12_of_the_org(tmp_path: Path) -> None:
    items = [
        project_item("Done", number=3),
        project_item("Done", login="someone"),
        project_item("In Progress"),
    ]
    _, doc = facts(tmp_path, issue_response(body=KICKOFF, items=items))
    assert doc["issue"]["board_status"] == "In Progress"
    assert doc["warnings"] == []


def test_not_on_the_board_is_null_and_a_warning(tmp_path: Path) -> None:
    for items in ([], [project_item("Done", number=3)]):
        code, doc = facts(tmp_path, issue_response(body=KICKOFF, items=items))
        assert code == 0
        assert doc["issue"]["board_status"] is None
        assert doc["warnings"] == ["the issue is not on board 12: the Status step will fail"]


def test_on_the_board_without_a_status_is_null_without_a_warning(tmp_path: Path) -> None:
    _, doc = facts(tmp_path, issue_response(body=KICKOFF, items=[project_item(None)]))
    assert doc["issue"]["board_status"] is None
    assert doc["warnings"] == []


def test_issue_fields(tmp_path: Path) -> None:
    response = issue_response(assignees=("b", "a"))
    _, doc = facts(tmp_path, response, ref="ui#13")
    assert doc["issue"] == {
        "repo": UI,
        "number": 13,
        "title": "t",
        "url": "https://github.com/x/y/issues/13",
        "state": "OPEN",
        "assignees": ["a", "b"],
        "board_status": "In Progress",
        "sub_issues": {"total": 0, "open": 0},
    }


# ─── REF grammar and exit codes ─────────────────────────────────────────────
@pytest.mark.parametrize(
    ("text", "parsed"),
    [
        ("api#269", (API, 269)),
        ("ui#7", (UI, 7)),
        ("269", (API, 269)),
        ("acme/widgets#4", ("acme/widgets", 4)),
        (f"{UI}#9", (UI, 9)),
    ],
)
def test_parse_ref(text: str, parsed: tuple[str, int]) -> None:
    assert pf.parse_ref(text) == parsed


@pytest.mark.parametrize("text", ["", "api", "api#", "api#0", "#5", "web#5", "0", "-3", "a b#1"])
def test_bad_ref_exits_2_and_never_reaches_gh(text: str) -> None:
    r = run(text)  # the guard gh would exit 97 and the exit would be 3
    assert r.returncode == 2
    error_of(r)


def test_usage_errors_exit_2() -> None:
    for args in ([], ["api#1", "api#2"], ["--bogus"], ["api#1", "--part"]):
        r = run(*args)
        assert r.returncode == 2, args
        error_of(r)


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

    monkeypatch.setattr(pf.subprocess, "run", boom)
    with pytest.raises(SystemExit) as exc:
        pf.main(["api#1"])
    assert exc.value.code == 3
    assert "timed out" in json.loads(capsys.readouterr().err)["error"]


def test_undecodable_gh_output_exits_3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body = "import sys\nsys.stdout.buffer.write(b'\\xff\\xfe not utf-8')\n"
    prepend_path(monkeypatch, fake_gh(tmp_path / "bin", body))
    r = run("api#1")
    assert r.returncode == 3
    assert "cannot read the output" in error_of(r)


def test_issue_not_found_exits_3(tmp_path: Path) -> None:
    write(tmp_path, "issue-p1", {"data": {"repository": {"issue": None}}})
    r = run("api#99", fixtures=tmp_path)
    assert r.returncode == 3
    assert error_of(r) == f"issue {API}#99 not found"
    write(tmp_path, "issue-p1", {"data": {"repository": None}})
    assert run("api#99", fixtures=tmp_path).returncode == 3


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
    (tmp_path / "issue-p1.json").write_bytes(b'{"data": "\xff\xfe"}')
    r = run("api#1", fixtures=tmp_path)
    assert r.returncode == 3
    assert "cannot read fixture" in error_of(r)


def test_empty_fixture_dir_variable_exits_3_without_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMPLEMENT_ISSUE_FIXTURE_DIR", "")
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "api#1"], capture_output=True, encoding="utf-8", timeout=60
    )
    assert r.returncode == 3
    assert "set but empty" in error_of(r)


def _corrupt(path: list[str | int], value: Any) -> dict[str, Any]:
    response = issue_response(timeline=[pr(1, body="Refs #1")], blocked=[blocker(2)])
    node: Any = response["data"]["repository"]["issue"]
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return response


@pytest.mark.parametrize(
    "response",
    [
        _corrupt(["state"], None),
        _corrupt(["title"], 5),
        _corrupt(["body"], 5),
        _corrupt(["assignees"], None),
        _corrupt(["projectItems", "nodes"], "not a list"),
        _corrupt(["blockedBy", "nodes"], [blocker(2) | {"number": "2"}]),
        _corrupt(["blockedBy", "nodes"], [blocker(2) | {"state": None}]),
        _corrupt(["timelineItems", "nodes"], [{"__typename": "CrossReferencedEvent"}]),
        _corrupt(["timelineItems", "nodes", 0, "source", "number"], "1"),
        _corrupt(["timelineItems", "nodes", 0, "source", "title"], None),
        _corrupt(["timelineItems", "pageInfo"], {"hasNextPage": True, "endCursor": None}),
        _corrupt(["subIssuesSummary"], None),
        _corrupt(["subIssuesSummary", "total"], "3"),
        _corrupt(["subIssuesSummary", "completed"], None),
        _corrupt(["subIssuesSummary", "completed"], 5),
        _corrupt(["comments"], None),
        _corrupt(["comments", "nodes"], "not a list"),
        _corrupt(["comments", "nodes"], [comment(RETURNED) | {"url": None}]),
        _corrupt(["comments", "nodes"], [comment(RETURNED) | {"createdAt": "yesterday"}]),
        _corrupt(["comments", "nodes"], [comment(RETURNED) | {"createdAt": None}]),
        _corrupt(["timelineItems", "nodes", 0, "source", "mergedAt"], None),
        _corrupt(["timelineItems", "nodes", 0, "source", "mergedAt"], "2026-10-20"),
        _corrupt(["timelineItems", "nodes", 0, "source", "mergedAt"], 5),
    ],
    ids=[
        "state",
        "title",
        "body",
        "assignees",
        "items-not-a-list",
        "blocker-number",
        "blocker-state",
        "event-source",
        "pr-number",
        "pr-title",
        "no-cursor",
        "sub-issues-missing",
        "sub-issues-total",
        "sub-issues-completed-null",
        "sub-issues-completed-above-total",
        "comments-missing",
        "comments-nodes",
        "comment-url",
        "comment-created-at",
        "comment-created-at-null",
        "merged-at-null-for-a-merged-pr",
        "merged-at-not-a-timestamp",
        "merged-at-not-a-string",
    ],
)
def test_unexpected_shape_exits_3(tmp_path: Path, response: dict[str, Any]) -> None:
    write(tmp_path, "issue-p1", response)
    r = run("api#1", fixtures=tmp_path)
    assert r.returncode == 3
    assert "unexpected shape" in error_of(r)


def test_a_board_item_list_with_a_second_page_exits_3(tmp_path: Path) -> None:
    response = issue_response()
    response["data"]["repository"]["issue"]["projectItems"]["pageInfo"] = page_info("more")
    write(tmp_path, "issue-p1", response)
    r = run("api#1", fixtures=tmp_path)
    assert r.returncode == 3
    assert "more than one page" in error_of(r)


@pytest.mark.parametrize("name", ["blocked-p2", "timeline-p2", "comments-p2"])
def test_a_missing_second_page_exits_3(tmp_path: Path, name: str) -> None:
    if name.startswith("blocked"):
        response = issue_response(blocked_next="C1")
    elif name.startswith("timeline"):
        response = issue_response(timeline_next="C1")
    else:
        response = issue_response(comments_next="C1")
    write(tmp_path, "issue-p1", response)
    r = run("api#1", fixtures=tmp_path)
    assert r.returncode == 3
    assert f"{name}.json" in error_of(r)


# ─── the argv and the queries handed to gh ──────────────────────────────────
GH_LOG_FAKE = """\
import json, os, sys
from pathlib import Path

argv = sys.argv[1:]
with open(os.environ["GH_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(argv) + "\\n")
fields = dict(a.split("=", 1) for a in argv if "=" in a and not a.startswith("query="))
cursor = fields.get("c")
names = {
    None: "issue-p1",
    "B1": "blocked-p2",
    "B2": "blocked-p3",
    "T1": "timeline-p2",
    "M1": "comments-p2",
    "M2": "comments-p3",
}
sys.stdout.write(Path(os.environ["FAKE_DIR"], names[cursor] + ".json").read_text(encoding="utf-8"))
"""


def squash(text: str) -> str:
    """Whitespace collapsed to single blanks: a re-indented query is the same query."""
    return " ".join(text.split())


def flat_query(argv: list[str]) -> str:
    """The GraphQL text of one gh call."""
    return squash(next(a for a in argv if a.startswith("query=")).removeprefix("query="))


# The expected text of every query, written out: a field dropped from (or added to) a query
# must fail here, not pass because the same word appears somewhere else in it.
ISSUE_QUERY_TEXT = squash(
    """
    query($owner: String!, $name: String!, $number: Int!) {
      repository(owner: $owner, name: $name) {
        issue(number: $number) {
          title url state body
          assignees(first: 20) { nodes { login } }
          subIssuesSummary { total completed }
          projectItems(first: 100) {
            pageInfo { hasNextPage endCursor }
            nodes {
              project {
                number
                owner { __typename ... on Organization { login } ... on User { login } }
              }
              fieldValueByName(name: "Status") {
                __typename
                ... on ProjectV2ItemFieldSingleSelectValue { name }
              }
            }
          }
          blockedBy(first: 100) {
            pageInfo { hasNextPage endCursor }
            nodes { number title state repository { nameWithOwner } }
          }
          timelineItems(first: 100, itemTypes: [CROSS_REFERENCED_EVENT]) {
            pageInfo { hasNextPage endCursor }
            nodes {
              __typename
              ... on CrossReferencedEvent {
                source {
                  __typename
                  ... on PullRequest {
                    number title url body state mergedAt repository { nameWithOwner }
                  }
                }
              }
            }
          }
          comments(first: 100) {
            pageInfo { hasNextPage endCursor }
            nodes { url createdAt body }
          }
        }
      }
    }
    """
)
BLOCKED_QUERY_TEXT = squash(
    """
    query($owner: String!, $name: String!, $number: Int!, $c: String) {
      repository(owner: $owner, name: $name) {
        issue(number: $number) {
          blockedBy(first: 100, after: $c) {
            pageInfo { hasNextPage endCursor }
            nodes { number title state repository { nameWithOwner } }
          }
        }
      }
    }
    """
)
TIMELINE_QUERY_TEXT = squash(
    """
    query($owner: String!, $name: String!, $number: Int!, $c: String) {
      repository(owner: $owner, name: $name) {
        issue(number: $number) {
          timelineItems(first: 100, after: $c, itemTypes: [CROSS_REFERENCED_EVENT]) {
            pageInfo { hasNextPage endCursor }
            nodes {
              __typename
              ... on CrossReferencedEvent {
                source {
                  __typename
                  ... on PullRequest {
                    number title url body state mergedAt repository { nameWithOwner }
                  }
                }
              }
            }
          }
        }
      }
    }
    """
)
COMMENTS_QUERY_TEXT = squash(
    """
    query($owner: String!, $name: String!, $number: Int!, $c: String) {
      repository(owner: $owner, name: $name) {
        issue(number: $number) {
          comments(first: 100, after: $c) {
            pageInfo { hasNextPage endCursor }
            nodes { url createdAt body }
          }
        }
      }
    }
    """
)


def test_argv_queries_and_paging_through_gh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx = tmp_path / "fx"
    write(fx, "issue-p1", issue_response(blocked=[blocker(1, "CLOSED")], blocked_next="B1"))
    write(fx, "blocked-p2", follow_up("blockedBy", [blocker(2, "CLOSED")], "B2"))
    write(fx, "blocked-p3", follow_up("blockedBy", [blocker(3, "CLOSED")], None))
    log = tmp_path / "gh.log"
    prepend_path(monkeypatch, fake_gh(tmp_path / "bin", GH_LOG_FAKE))
    monkeypatch.setenv("GH_LOG", str(log))
    monkeypatch.setenv("FAKE_DIR", str(fx))
    r = run("ui#7")
    assert r.returncode == 0, r.stderr
    assert len(json.loads(r.stdout)["blockers"]) == 3
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(calls) == 3  # the issue, then two blocker pages
    for argv in calls:
        assert argv[:3] == ["api", "graphql", "-f"]
        # String variables go with -f (never reinterpreted), the number with -F.
        pairs = list(zip(argv, argv[1:], strict=False))
        assert ("-f", "owner=larchanka-training") in pairs
        assert ("-f", "name=dmc-268-ui-t6") in pairs
        assert ("-F", "number=7") in pairs
    first, second, third = calls
    assert not any(a.startswith("c=") for a in first)  # first page: no cursor
    assert "c=B1" in second and "c=B2" in third
    # The query text itself, token by token (whitespace aside): dropping or adding any field
    # silently loses a fact, so the whole text is pinned, not a few needles.
    query = flat_query(first)
    assert query == ISSUE_QUERY_TEXT
    assert "timelineItems(last" not in query
    assert "CLOSED_EVENT" not in query  # the closing history is accept-issue's, not ours
    assert flat_query(second) == BLOCKED_QUERY_TEXT == flat_query(third)
    # Read-only: queries only, never a mutation, never a REST write.
    for argv in calls:
        assert "mutation" not in " ".join(argv)
        assert "-X" not in argv


def test_timeline_follow_up_query(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fx = tmp_path / "fx"
    write(fx, "issue-p1", issue_response(timeline=[pr(1)], timeline_next="T1"))
    write(fx, "timeline-p2", follow_up("timelineItems", [refs_pr(2, "x", state="CLOSED")], None))
    log = tmp_path / "gh.log"
    prepend_path(monkeypatch, fake_gh(tmp_path / "bin", GH_LOG_FAKE))
    monkeypatch.setenv("GH_LOG", str(log))
    monkeypatch.setenv("FAKE_DIR", str(fx))
    r = run("api#13")
    assert r.returncode == 0, r.stderr
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(calls) == 2 and "c=T1" in calls[1]
    assert flat_query(calls[1]) == TIMELINE_QUERY_TEXT
    assert "mutation" not in flat_query(calls[1])


def test_comments_follow_up_query_and_paging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx = tmp_path / "fx"
    write(fx, "issue-p1", issue_response(comments=[comment("a")], comments_next="M1"))
    write(fx, "comments-p2", follow_up("comments", [comment("b")], "M2"))
    write(fx, "comments-p3", follow_up("comments", [comment(RETURNED + "\n\nback")], None))
    log = tmp_path / "gh.log"
    prepend_path(monkeypatch, fake_gh(tmp_path / "bin", GH_LOG_FAKE))
    monkeypatch.setenv("GH_LOG", str(log))
    monkeypatch.setenv("FAKE_DIR", str(fx))
    r = run("api#13")
    assert r.returncode == 0, r.stderr
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(calls) == 3 and "c=M1" in calls[1] and "c=M2" in calls[2]
    assert flat_query(calls[1]) == COMMENTS_QUERY_TEXT == flat_query(calls[2])
    for argv in calls:
        assert "mutation" not in " ".join(argv)


# ─── numbers read from text are bounded (7a) ────────────────────────────────
HUGE = "9" * 5000  # `int()` raises on this; the script must not even try


def _ok(
    tmp_path: Path, response: dict[str, Any], *args: str, ref: str = "api#13"
) -> dict[str, Any]:
    """Run on a one-page issue: a clean exit 0 or 1 with a document, never a traceback."""
    _, doc = facts(tmp_path, response, *args, ref=ref)
    return doc


@pytest.mark.parametrize(
    ("title", "what", "refs", "expected"),
    [
        (f"Part {HUGE}: x", "", "Refs #13", []),
        (f"Ч.{HUGE}: x", "", "Refs #13", []),
        (f"Parts 1 and {HUGE}", "", "Refs #13", [1]),  # the number in range still counts
        (f"Parts {HUGE}–{HUGE}", "", "Refs #13", []),
        ("feat: y", f"Part {HUGE} of #13: x", "Refs #13", []),
        # out of range, the ref names no issue: the part is not this issue's, tied or not
        ("feat: y", f"Part 2 of #{HUGE}: x", "Refs #13", []),
        ("feat: y", f"Part 2 of **#{HUGE}**: x", "Refs #13", []),
        ("feat: y", f"Part 2 of [#{HUGE}](https://example.test/{HUGE}): x", "Refs #13", []),
        ("feat: y", f"#{HUGE} part 2", "Refs #13, #14", []),
        ("feat: y", f"Part 2 of api#{HUGE}", "Refs #13, #14", []),
        # an out-of-range Refs token is no issue: two real ones remain, `Part 2` is untied
        ("feat: y", "Part 2", f"Refs #13, #{HUGE}, #14", []),
    ],
)
def test_a_huge_number_names_nothing_and_never_raises(
    tmp_path: Path, title: str, what: str, refs: str, expected: list[int]
) -> None:
    node = refs_pr(40, what, title=title, refs=refs)
    doc = _ok(tmp_path, issue_response(body=TWO_PARTS, timeline=[node]))
    assert [p["n"] for p in doc["parts"] if p["prs"]] == expected
    assert doc["verdict"] == "go"


def test_a_huge_number_in_a_refs_line_is_no_issue(tmp_path: Path) -> None:
    node = refs_pr(40, "the thing", refs=f"Refs #{HUGE}")
    doc = _ok(tmp_path, issue_response(body="## Scope\n\n- x\n", timeline=[node]))
    assert doc["task_prs"] == []
    assert doc["selected"] == {"part": None, "reason": "whole-task"}
    # one real issue and one out of range: a single-Refs pull request
    node = refs_pr(41, "Part 2 of #13: x", refs=f"Refs #13, #{HUGE}")
    doc = _ok(tmp_path, issue_response(body=TWO_PARTS, timeline=[node]))
    assert [p["n"] for p in doc["parts"] if p["prs"]] == [2]


def test_a_huge_number_in_the_issue_names_no_part(tmp_path: Path) -> None:
    scope = f"## Scope\n\n- Ч.1 (PR): first\n- Ч.{HUGE} (PR): nowhere\n- Ч.2 (PR): second\n"
    criteria = f"\n## Acceptance Criteria\n\n- [ ] Ч.{HUGE}: lost\n- [ ] Ч.1: kept\n"
    doc = _ok(tmp_path, issue_response(body=scope + criteria + KICKOFF))
    assert [p["n"] for p in doc["parts"]] == [1, 2]
    assert doc["parts"][0]["acceptance_criteria"] == ["kept"]
    assert doc["warnings"] == ["acceptance criterion names a part number out of range"]


@pytest.mark.parametrize("ref", [f"api#{HUGE}", HUGE, f"{API}#{HUGE}", "api#1" + "0" * 9])
def test_a_huge_ref_is_a_bad_ref_and_never_reaches_gh(ref: str) -> None:
    r = run(ref)
    assert r.returncode == 2
    assert len(error_of(r)) < 200


def test_a_huge_part_exits_2(tmp_path: Path) -> None:
    write(tmp_path / "fx", "issue-p1", issue_response(body=TWO_PARTS))
    r = run("api#13", "--part", HUGE, fixtures=tmp_path / "fx")
    assert r.returncode == 2
    error_of(r)


def test_the_largest_number_is_still_a_number() -> None:
    assert pf.parse_ref("api#999999999") == (API, 999999999)
    assert pf.to_int("999999999") == 999999999
    assert pf.to_int("0000000001") == 1
    assert pf.to_int("1000000000") is None


# ─── a range needs a lower end below the upper one (7c) ─────────────────────
@pytest.mark.parametrize(
    ("title", "what", "expected"),
    [
        ("Part 2 — 28.10 report", "", [2]),
        ("Part 2 - 28.10", "", [2]),
        ("Part 2 – 10:30", "", [2]),
        ("Part 2 — 3/4", "", [2]),
        ("Part 2 - 3 files", "", []),  # still a range: safe, it names nothing
        ("Part 2 – 3.", "", []),  # a full stop that ends the sentence
        ("Part 2 – 3: x", "", []),  # a colon that is no time
        ("Part 3 - 2 files", "", [3]),
        ("Part 2 — 2", "", [2]),
        ("Part 1 — 2026-10-29", "", [1]),
        ("Part 1 – 2nd step", "", [1]),
        ("Parts 2 — 28.10", "", []),  # a plural label alone names nothing
        ("feat: y", "Part 2 — 28.10: x. Part 3 comes later.", [2]),
        ("feat: y", "Part 2 - 3 files. Part 3 is next.", []),
        ("feat: y", "Part 3 - 2 files. Part 1 is merged.", [3]),
    ],
)
def test_a_range_has_its_lower_end_below_its_upper_end(
    tmp_path: Path, title: str, what: str, expected: list[int]
) -> None:
    assert named_in(tmp_path, refs_pr(70, what, title=title), parts=3) == expected


# ─── a plural label with a list names every listed part (7d) ────────────────
@pytest.mark.parametrize(
    ("title", "what", "expected"),
    [
        ("Parts 2 and 3", "", [2, 3]),
        ("Parts 2, 3", "", [2, 3]),
        ("Parts 1, 2 and 3", "", [1, 2, 3]),
        ("Parts 1, 2, and 3", "", [1, 2, 3]),
        ("PARTS 2 AND 3", "", [2, 3]),
        ("Parts 2 and 3 of #13: y", "", [2, 3]),
        ("Parts 2 and 3 of #14: y", "", []),
        ("Parts 2 and 2", "", [2]),
        ("Parts 2 and 28.10", "", []),  # a date is no part, and `Parts 2` alone no mention
        ("Parts 2 or 3", "", []),
        ("Part 2 and 3", "", [2]),  # a singular label names one part
        ("Parts 2 and Part 3", "", [3]),  # `Parts 2` alone is no mention
        ("Части 2 и 3", "", [2, 3]),
        ("Части 1, 2 и 3", "", [1, 2, 3]),
        ("Частей 2 и 3", "", [2, 3]),
        ("Частях 2 и 3 из #13", "", [2, 3]),
        ("Teile 2 und 3", "", [2, 3]),
        ("Teilen 2 und 3 von #13", "", [2, 3]),
        ("Teile 2 and 3", "", [2, 3]),
        ("Parts 1–2 and 3", "", []),  # a range comes first
        ("feat: y", "Parts 2 and 3 of #13: x. Part 1 is merged.", [2, 3]),
        ("feat: y", "Parts 2 and 3 of #14: x. Part 1 of #13 is merged.", []),
        ("feat: y", "Parts 2, 3 of #13 (tails of #112): x", [2, 3]),
        ("feat: y", "Parts 3 and 4 of #13: x. Part 2 is merged.", [3]),  # Part 4 does not exist
    ],
)
def test_a_plural_label_with_a_list_names_every_part(
    tmp_path: Path, title: str, what: str, expected: list[int]
) -> None:
    assert named_in(tmp_path, refs_pr(71, what, title=title), parts=3) == expected


def test_a_list_in_what_warns_with_its_source(tmp_path: Path) -> None:
    node = refs_pr(72, "Parts 1 and 2 of #13: x")
    _, doc = facts(tmp_path, issue_response(body=TWO_PARTS, timeline=[node]))
    assert [p["status"] for p in doc["parts"]] == ["done", "done"]
    assert doc["warnings"] == [
        f"pull request {API}#72 names several parts in its What (1, 2): it counts for each of them"
    ]
    node = refs_pr(73, "x", title="Parts 1 and 2 of #13")
    _, doc = facts(tmp_path, issue_response(body=TWO_PARTS, timeline=[node]))
    assert doc["warnings"] == [
        f"pull request {API}#73 names several parts in its title (1, 2): it counts for each of them"
    ]


def test_api_193_names_parts_2_and_3_of_174(tmp_path: Path) -> None:
    # The real first line of the What of api#193, and its Refs line.
    what = "Parts 2 and 3 of #174 (acceptance tails of #112):"
    refs = "Refs #174 (parts 2 and 3; part 1 follows after #159)"
    node = refs_pr(193, what, title="test(api): guard UTC on all /api models", refs=refs)
    assert named_in(tmp_path, node, parts=3, ref="api#174") == [2, 3]
    assert named_in(tmp_path, node, parts=3, ref="api#112") == []


# ─── the What section: `## What — Что изменено` and the like (7e) ───────────
@pytest.mark.parametrize(
    ("heading", "expected"),
    [
        ("## What", [2]),
        ("## What — Что изменено", [2]),
        ("## What - Что изменено", [2]),
        ("## What – Что изменено", [2]),
        ("## What—Что", [2]),
        ("## What (Что изменено)", [2]),
        ("### WHAT", [2]),
        ("## Whatever", []),
        ("## What's new", []),
        ("## What: changes", []),
        ("## Whatnot — x", []),
        ("## Что изменено", []),
    ],
)
def test_the_what_heading_may_be_followed_by_a_dash(
    tmp_path: Path, heading: str, expected: list[int]
) -> None:
    body = f"{heading}\n\nPart 2 of #13: y\n\n## Refs\n\nRefs #13\n"
    assert named_in(tmp_path, pr(74, body=body)) == expected


def test_api_121_style_what_reads_the_first_line_of_its_section(tmp_path: Path) -> None:
    body = "## What — Что изменено\n\nPart 1 of #13: Успешный CI-вебхук.\n\n## Why\n\nPart 2.\n"
    node = pr(75, body=body + "\n## Refs\n\nRefs #13\n")
    assert named_in(tmp_path, node) == [1]


# ─── a pull request whose Refs name several issues ties its mentions (7b) ───
def tied(
    tmp_path: Path,
    *,
    title: str = "feat: y",
    what: str = "",
    refs: str,
    ref: str = "api#13",
    repo: str = API,
    parts: int = 6,
) -> list[int]:
    return named_in(tmp_path, refs_pr(80, what, title=title, refs=refs, repo=repo), parts, ref)


@pytest.mark.parametrize(
    ("title", "what", "for_13", "for_14"),
    [
        # tied by `of <ref>`
        ("Part 2 of #13: x", "", [2], []),
        ("Part 2 of #14: x", "", [], [2]),
        ("Part 2 of api#13", "", [2], []),
        ("Part 2 of **#13**", "", [2], []),
        ("Part 2 of `#13`", "", [2], []),
        ("Part 2 of **api#14**", "", [], [2]),
        ("Part 2 of [#13](https://example.test/issues/13)", "", [2], []),
        ("Part 2 of [api#14](https://example.test/issues/14)", "", [], [2]),
        (f"Part 2 of [{API}#13](https://example.test/13)", "", [2], []),
        ("Teil 2 von **#13**", "", [2], []),
        ("Часть 2 из `#14`", "", [], [2]),
        # tied by the ref in front of it
        ("#13 part 2", "", [2], []),
        ("#14 part 2", "", [], [2]),
        ("#13 Part 2", "", [2], []),
        ("#13 Ч.2", "", [2], []),
        ("#13 Ч. 2", "", [2], []),
        ("#13 часть 2", "", [2], []),
        ("#13 Teil 2", "", [2], []),
        ("api#13 part 2", "", [2], []),
        (f"{API}#13 part 2", "", [2], []),
        ("**#13** part 2", "", [2], []),
        ("`#13` part 2", "", [2], []),
        ("**api#13** part 2", "", [2], []),
        ("[#13](https://example.test/issues/13) part 2", "", [2], []),
        ("(#14 part 2)", "", [], [2]),
        ("fix: the items of #13 part 2 of the plan", "", [2], []),
        ("#13 parts 2 and 3", "", [2, 3], []),
        ("#13 part 2 and #14 part 3", "", [2], [3]),
        ("#14 part 3 and #13 part 2", "", [2], [3]),
        # `of <ref>` wins over a ref in front
        ("#14 part 2 of #13", "", [2], []),
        # untied: nothing
        ("Part 2", "", [], []),
        ("Part 2 of the plan", "", [], []),
        ("Part 2 (#13)", "", [], []),
        ("#13, part 2", "", [], []),
        ("#13 and part 2", "", [], []),
        ("#13: part 2", "", [], []),
        ("part 2 #13", "", [], []),
        ("x#13 part 2", "", [], []),
        ("Parts 2 and 3", "", [], []),
        # What: the first mention decides, and it must be tied
        ("feat: y", "Part 2 of #13: x. Part 3 of #14 follows.", [2], []),
        ("feat: y", "#14 part 3: x. Part 2 of #13 is merged.", [], [3]),
        ("feat: y", "Part 2: x. Part 3 of #13 follows.", [], []),
        ("feat: y", "Parts 2 and 3 of #13: x.", [2, 3], []),
        # the title wins over What, as before; an untied title mention is skipped
        ("Part 4 of #14", "Part 2 of #13: x", [2], [4]),
        ("Part 4", "Part 2 of #13: x", [2], []),
    ],
)
def test_a_pull_request_with_two_issues_ties_each_mention(
    tmp_path: Path, title: str, what: str, for_13: list[int], for_14: list[int]
) -> None:
    refs = "Refs #13, #14"
    assert tied(tmp_path, title=title, what=what, refs=refs) == for_13
    assert tied(tmp_path, title=title, what=what, refs=refs, ref="api#14") == for_14


def test_the_refs_of_several_lines_and_repos_count_together(tmp_path: Path) -> None:
    # api#346: `Refs #168`, `Refs #130` and `Refs ui#199` on three lines.
    refs = f"Refs #13\nRefs #14\nRefs {UI}#15"
    what = "The items of #13 part 6 (of #14)"
    assert tied(tmp_path, title="fix: x (#13)", what=what, refs=refs) == [6]
    assert tied(tmp_path, what=what, refs=refs, ref="api#14") == []
    ui_ref = "ui#15"
    assert tied(tmp_path, what=what, refs=refs, ref=ui_ref) == []
    # in the ui, a bare #15 in the PR's own repo is ui#15
    node = refs_pr(81, "#15 part 2: x", refs=f"Refs #15, {API}#13", repo=UI)
    assert named_in(tmp_path, node, parts=3, ref=ui_ref) == [2]
    assert named_in(tmp_path, node, parts=3, ref="api#13") == []


def test_api_346_and_api_353_name_a_part_of_168_only(tmp_path: Path) -> None:
    # Real rows (merged in the #130 remainder). Both are `Refs #130` + `Refs #168`; the part
    # is tied to #168 by `#168 part N`.
    api_346 = refs_pr(
        346,
        "The trace and output items of #168 part 6 (PR F3 of the #130 remainder). Four code "
        "commits after the plan commit:",
        title="fix(reviews): record context.build once per attempt (#168)",
        refs=f"Refs #168\nRefs #130\nRefs {UI}#199",
    )
    api_353 = refs_pr(
        353,
        "G2, the second docs PR of the #130 remainder. The PR also closes the docs part of "
        "#168 part 4.",
        title="docs: describe the runtime after F1–F5 and #168 part 4 (#130)",
        refs="Refs #130\nRefs #168",
    )
    assert named_in(tmp_path, api_346, parts=6, ref="api#168") == [6]
    assert named_in(tmp_path, api_346, parts=6, ref="api#130") == []
    assert named_in(tmp_path, api_353, parts=6, ref="api#168") == [4]
    assert named_in(tmp_path, api_353, parts=6, ref="api#130") == []


def test_the_same_issue_in_two_forms_is_one_issue(tmp_path: Path) -> None:
    for refs in ("Refs #13, #13", f"Refs #13\nRefs {API}#13", f"Refs #13, {API.upper()}#13"):
        assert tied(tmp_path, title="Part 2", refs=refs) == [2], refs  # single-Refs: as before


def test_a_single_refs_pull_request_keeps_the_untied_rule(tmp_path: Path) -> None:
    assert tied(tmp_path, title="Part 2", refs="Refs #13") == [2]
    assert tied(tmp_path, title="Part 2 (#14)", refs="Refs #13") == [2]
    # a ref in front of a part ties only in a multi-Refs pull request
    assert tied(tmp_path, title="#14 part 2", refs="Refs #13") == [2]
    assert tied(tmp_path, title="#13 part 2", refs="Refs #13") == [2]
    # `of <ref>` skips another issue's part, wrapped or not
    assert tied(tmp_path, title="Part 2 of #14", refs="Refs #13") == []
    assert tied(tmp_path, title="Part 2 of **#14**", refs="Refs #13") == []
    assert tied(tmp_path, title="Part 2 of [#14](https://x.test/14)", refs="Refs #13") == []
    assert tied(tmp_path, title="Part 2 of **#13**", refs="Refs #13") == [2]


def test_an_untied_open_pull_request_with_two_issues_is_a_stray_for_both(tmp_path: Path) -> None:
    node = refs_pr(82, "Part 2: x", refs="Refs #13, #14", state="OPEN")
    for ref in ("api#13", "api#14"):
        code, doc = facts(tmp_path, issue_response(body=TWO_PARTS, timeline=[node]), ref=ref)
        assert (code, doc["selected"]) == (1, None)
        assert doc["reasons"] == [stray_reason(f"{API}#82 (OPEN)")]
    merged = refs_pr(82, "Part 2: x", refs="Refs #13, #14")
    code, doc = facts(tmp_path, issue_response(body=TWO_PARTS, timeline=[merged]))
    assert (code, doc["selected"]) == (0, {"part": 1, "reason": "first-todo"})
    assert doc["warnings"] == [
        f"pull requests with Refs that name no part of this issue: {API}#82 (MERGED)"
    ]


# ─── Part 3a counts as Part 3, by decision (7h) ─────────────────────────────
@pytest.mark.parametrize("title", ["Part 3a: x", "Part 3a of #13", "Part 3b", "Part 3.1"])
def test_part_3a_is_part_3(tmp_path: Path, title: str) -> None:
    assert named_in(tmp_path, refs_pr(83, "", title=title), parts=3) == [3]


# ─── accept: the work is done and acceptance is next ────────────────────────
def merged_parts(*, at: str = MERGED_AT) -> list[dict[str, Any]]:
    return [
        refs_pr(100, "Part 1 of #13: x", merged_at=at),
        refs_pr(200, "Part 2 of #13: x", merged_at=at),
    ]


def comment_url(number: int) -> str:
    return f"https://github.com/{API}/issues/13#issuecomment-{number}"


def returned_reason(number: int) -> str:
    return (
        f"the issue was returned at acceptance ({comment_url(number)}) and no pull request "
        "with Refs has been merged since — rework a part with --part K"
    )


@pytest.mark.parametrize("marker", [TABLE, ACCEPTED])
def test_a_table_or_an_accepted_comment_does_not_hold_acceptance_back(
    tmp_path: Path, marker: str
) -> None:
    comments = [comment(RETURNED, number=1), comment(marker, "2026-10-22T09:00:00Z", 2)]
    response = issue_response(body=TWO_PARTS, timeline=merged_parts(), comments=comments)
    code, doc = facts(tmp_path, response)
    assert (code, doc["verdict"], doc["selected"]) == (1, "accept", None)


def test_a_returned_issue_stops_and_links_the_comment(tmp_path: Path) -> None:
    comments = [comment(TABLE, number=1), comment(RETURNED + "\n\nwhat is left", number=2)]
    response = issue_response(body=TWO_PARTS, timeline=merged_parts(), comments=comments)
    code, doc = facts(tmp_path, response)
    assert (code, doc["verdict"], doc["selected"]) == (1, "stop", None)
    assert doc["reasons"] == [returned_reason(2)]


def test_a_pull_request_merged_after_the_return_ends_it(tmp_path: Path) -> None:
    comments = [comment(RETURNED, "2026-10-21T09:00:00Z", 2)]
    before = merged_parts(at="2026-10-21T08:59:59Z")
    after = merged_parts(at="2026-10-21T09:00:01Z")
    code, doc = facts(tmp_path, issue_response(body=TWO_PARTS, timeline=before, comments=comments))
    assert (code, doc["verdict"]) == (1, "stop")
    code, doc = facts(tmp_path, issue_response(body=TWO_PARTS, timeline=after, comments=comments))
    assert (code, doc["verdict"], doc["reasons"]) == (
        1,
        "accept",
        ["every part has a merged pull request — acceptance is next"],
    )
    # the very second is not "after"
    same = merged_parts(at="2026-10-21T09:00:00Z")
    _, doc = facts(tmp_path, issue_response(body=TWO_PARTS, timeline=same, comments=comments))
    assert doc["verdict"] == "stop"


def test_any_pull_request_with_refs_merged_after_the_return_counts(tmp_path: Path) -> None:
    comments = [comment(RETURNED, "2026-10-21T09:00:00Z", 2)]
    late = {"merged_at": "2026-10-22T09:00:00Z"}
    stray = refs_pr(300, "no part named", title="fix: tail", **late)  # names no part
    other_repo = refs_pr(301, "x", refs=f"Refs {API}#13", repo=UI, **late)
    for extra in (stray, other_repo):
        timeline = [*merged_parts(), extra]
        _, doc = facts(
            tmp_path, issue_response(body=TWO_PARTS, timeline=timeline, comments=comments)
        )
        assert doc["verdict"] == "accept"
    # a pull request that was closed without a merge, or is still open, ends nothing
    closed = refs_pr(302, "no part named", state="CLOSED")
    timeline = [*merged_parts(), closed]
    _, doc = facts(tmp_path, issue_response(body=TWO_PARTS, timeline=timeline, comments=comments))
    assert doc["verdict"] == "stop"


def test_the_return_marker_must_open_the_comment(tmp_path: Path) -> None:
    comments = [
        comment(f"quoting {RETURNED} in a remark", number=1),
        comment(f"> {RETURNED}", number=2),
        comment(f"  \n\t{RETURNED}\nwhat is left", "2026-10-22T09:00:00Z", 3),  # leading blanks ok
    ]
    response = issue_response(body=TWO_PARTS, timeline=merged_parts(), comments=comments[:2])
    _, doc = facts(tmp_path, response)
    assert doc["verdict"] == "accept"
    response = issue_response(body=TWO_PARTS, timeline=merged_parts(), comments=comments)
    _, doc = facts(tmp_path, response)
    assert (doc["verdict"], doc["reasons"]) == ("stop", [returned_reason(3)])


def test_the_latest_comment_of_the_three_wins_across_comment_pages(tmp_path: Path) -> None:
    fx = tmp_path / "fx"
    response = issue_response(
        body=TWO_PARTS,
        timeline=merged_parts(),
        comments=[comment(RETURNED, number=1)],
        comments_next="M1",
    )
    write(fx, "issue-p1", response)
    write(fx, "comments-p2", follow_up("comments", [comment("chatter", number=2)], "M2"))
    write(fx, "comments-p3", follow_up("comments", [comment(TABLE, number=3)], None))
    r = run("api#13", fixtures=fx)
    assert (r.returncode, json.loads(r.stdout)["verdict"]) == (1, "accept")
    write(fx, "comments-p3", follow_up("comments", [comment(RETURNED, number=3)], None))
    r = run("api#13", fixtures=fx)
    assert json.loads(r.stdout)["reasons"] == [returned_reason(3)]


def test_a_returned_whole_task_is_manual(tmp_path: Path) -> None:
    comments = [comment(RETURNED, "2026-10-21T09:00:00Z", 2)]
    timeline = [refs_pr(50, "the thing")]
    response = issue_response(body="## Scope\n\n- x\n", timeline=timeline, comments=comments)
    code, doc = facts(tmp_path, response)
    assert (code, doc["verdict"], doc["selected"]) == (1, "stop", None)
    assert doc["reasons"] == [
        f"the issue was returned at acceptance ({comment_url(2)}) and no pull request with "
        "Refs has been merged since — the rework of a task without parts is manual: ask the user"
    ]
    later = [refs_pr(50, "the thing"), refs_pr(51, "rework", merged_at="2026-10-22T09:00:00Z")]
    response = issue_response(body="## Scope\n\n- x\n", timeline=later, comments=comments)
    _, doc = facts(tmp_path, response)
    assert doc["verdict"] == "accept"


def test_a_returned_issue_still_has_its_next_part_and_an_explicit_part(tmp_path: Path) -> None:
    comments = [comment(RETURNED, "2026-10-21T09:00:00Z", 2)]
    # an unfinished part is taken as ever: the return only matters when acceptance is next
    timeline = [refs_pr(100, "Part 1 of #13: x")]
    code, doc = facts(
        tmp_path, issue_response(body=TWO_PARTS, timeline=timeline, comments=comments)
    )
    assert (code, doc["selected"]) == (0, {"part": 2, "reason": "first-todo"})
    # `--part K` is how a returned part is reworked
    response = issue_response(body=TWO_PARTS, timeline=merged_parts(), comments=comments)
    code, doc = facts(tmp_path, response, "--part", "2")
    assert (code, doc["verdict"], doc["selected"]) == (0, "go", {"part": 2, "reason": "explicit"})
    assert doc["warnings"] == [f"Part 2 already has a merged pull request: {API}#200 (MERGED)"]


def test_a_returned_issue_with_an_open_rework_pull_request_waits_for_it(tmp_path: Path) -> None:
    comments = [comment(RETURNED, "2026-10-21T09:00:00Z", 2)]
    timeline = [*merged_parts(), refs_pr(101, "Part 1 of #13: rework", state="OPEN")]
    _, doc = facts(tmp_path, issue_response(body=TWO_PARTS, timeline=timeline, comments=comments))
    assert doc["verdict"] == "stop"
    assert doc["reasons"] == [
        "every part has a merged pull request, but pull requests are still open: "
        f"{API}#101 (OPEN) — finish them first"
    ]


# The table accept-issue's author mode posts (render_table in acceptance.py): its last line says
# whether some row is «не выполнено».
TABLE_HEAD = (
    f"{TABLE}\n\n## Самопроверка: AC → доказательство\n\n"
    "| # | Пункт | Доказательство |\n| --- | --- | --- |\n"
)
TABLE_MET = (
    TABLE_HEAD
    + "| AC1 | first | PR #1 |\n| AC2 | second | PR #2 |\n\n"
    + "Итог: «не выполнено» нет, ставлю AC Check.\n"
)
TABLE_UNMET = (
    TABLE_HEAD
    + "| AC1 | first | PR #1 |\n| AC2 | second | не выполнено |\n\n"
    + "Итог: не выполнено — AC2. AC Check не ставлю, доработка идёт в этом же issue.\n"
)
TABLE_UNMET_IN_AC_CHECK = TABLE_UNMET.replace(
    "AC Check не ставлю, доработка",
    "Карточка остаётся в AC Check: принимающий вернёт её в In Progress; доработка",
)


def selfcheck_reason(number: int, *, parts: bool = True) -> str:
    rework = (
        "rework first: --part K for a part"
        if parts
        else "rework first, by hand: a task without parts has no --part, ask the user"
    )
    return (
        f"the self-check found unmet AC ({comment_url(number)}) and no pull request with Refs "
        f"has been merged since — {rework}"
    )


@pytest.mark.parametrize("table", [TABLE_UNMET, TABLE_UNMET_IN_AC_CHECK])
def test_a_table_with_unmet_rows_holds_acceptance_back(tmp_path: Path, table: str) -> None:
    comments = [comment(table, number=2)]
    response = issue_response(body=TWO_PARTS, timeline=merged_parts(), comments=comments)
    code, doc = facts(tmp_path, response)
    assert (code, doc["verdict"], doc["selected"]) == (1, "stop", None)
    assert doc["reasons"] == [selfcheck_reason(2)]


def test_a_table_with_every_row_met_is_accept(tmp_path: Path) -> None:
    comments = [comment(TABLE_MET, number=2)]
    response = issue_response(body=TWO_PARTS, timeline=merged_parts(), comments=comments)
    code, doc = facts(tmp_path, response)
    assert (code, doc["verdict"], doc["selected"]) == (1, "accept", None)


def test_a_pull_request_merged_after_an_unmet_table_ends_the_hold(tmp_path: Path) -> None:
    comments = [comment(TABLE_UNMET, "2026-10-21T09:00:00Z", 2)]
    for at, verdict in (("2026-10-21T08:59:59Z", "stop"), ("2026-10-21T09:00:01Z", "accept")):
        timeline = merged_parts(at=at)
        response = issue_response(body=TWO_PARTS, timeline=timeline, comments=comments)
        _, doc = facts(tmp_path, response)
        assert doc["verdict"] == verdict, at


def test_the_latest_marked_comment_decides_between_tables(tmp_path: Path) -> None:
    met_after = [comment(TABLE_UNMET, number=1), comment(TABLE_MET, "2026-10-22T09:00:00Z", 2)]
    unmet_after = [comment(TABLE_MET, number=1), comment(TABLE_UNMET, "2026-10-22T09:00:00Z", 2)]
    for comments, verdict in ((met_after, "accept"), (unmet_after, "stop")):
        response = issue_response(body=TWO_PARTS, timeline=merged_parts(), comments=comments)
        _, doc = facts(tmp_path, response)
        assert doc["verdict"] == verdict
        if verdict == "stop":
            assert doc["reasons"] == [selfcheck_reason(2)]
    # a return after an unmet table is a return, and an accepted comment holds nothing back
    later = [comment(TABLE_UNMET, number=1), comment(RETURNED, "2026-10-22T09:00:00Z", 2)]
    response = issue_response(body=TWO_PARTS, timeline=merged_parts(), comments=later)
    assert facts(tmp_path, response)[1]["reasons"] == [returned_reason(2)]
    done = [comment(TABLE_UNMET, number=1), comment(ACCEPTED, "2026-10-22T09:00:00Z", 2)]
    response = issue_response(body=TWO_PARTS, timeline=merged_parts(), comments=done)
    assert facts(tmp_path, response)[1]["verdict"] == "accept"


@pytest.mark.parametrize(
    "summary",
    [
        "see: Итог: не выполнено — AC2",  # not at the start of the line
        " Итог: не выполнено — AC2",  # indented
        "ИТОГ: не выполнено — AC2",
        "Итог: «не выполнено» нет, ставлю AC Check.",
    ],
)
def test_only_a_line_that_starts_with_the_summary_counts(tmp_path: Path, summary: str) -> None:
    body = TABLE_HEAD + "| AC2 | second | не выполнено |\n\n" + summary + "\n"
    response = issue_response(
        body=TWO_PARTS, timeline=merged_parts(), comments=[comment(body, number=2)]
    )
    assert facts(tmp_path, response)[1]["verdict"] == "accept"


def test_an_unmet_table_on_a_task_without_parts_is_manual(tmp_path: Path) -> None:
    comments = [comment(TABLE_UNMET, "2026-10-21T09:00:00Z", 2)]
    response = issue_response(
        body="## Scope\n\n- x\n", timeline=[refs_pr(50, "the thing")], comments=comments
    )
    code, doc = facts(tmp_path, response)
    assert (code, doc["verdict"], doc["selected"]) == (1, "stop", None)
    assert doc["reasons"] == [selfcheck_reason(2, parts=False)]
    later = [refs_pr(50, "the thing"), refs_pr(51, "rework", merged_at="2026-10-22T09:00:00Z")]
    response = issue_response(body="## Scope\n\n- x\n", timeline=later, comments=comments)
    assert facts(tmp_path, response)[1]["verdict"] == "accept"


def test_an_unmet_table_does_not_stop_the_next_part_or_an_explicit_part(tmp_path: Path) -> None:
    comments = [comment(TABLE_UNMET, "2026-10-21T09:00:00Z", 2)]
    timeline = [refs_pr(100, "Part 1 of #13: x")]
    response = issue_response(body=TWO_PARTS, timeline=timeline, comments=comments)
    code, doc = facts(tmp_path, response)
    assert (code, doc["selected"]) == (0, {"part": 2, "reason": "first-todo"})
    response = issue_response(body=TWO_PARTS, timeline=merged_parts(), comments=comments)
    code, doc = facts(tmp_path, response, "--part", "1")
    assert (code, doc["verdict"], doc["selected"]) == (0, "go", {"part": 1, "reason": "explicit"})


def test_the_unmet_line_is_the_one_accept_issue_writes() -> None:
    source = SKILL.parent / "accept-issue" / "scripts" / "acceptance.py"
    if not source.is_file():
        pytest.skip("accept-issue is not in this checkout")
    text = source.read_text(encoding="utf-8")
    assert text.count(f'f"{pf.UNMET_LINE} — {{') == 2  # the two variants with unmet rows
    assert '"Итог: «не выполнено» нет' in text  # the all-met line
    assert not "Итог: «не выполнено» нет".startswith(pf.UNMET_LINE)


def test_the_markers_are_the_ones_accept_issue_writes() -> None:
    source = SKILL.parent / "accept-issue" / "scripts" / "acceptance.py"
    if not source.is_file():
        pytest.skip("accept-issue is not in this checkout")
    text = source.read_text(encoding="utf-8")
    written = {
        kind: re.search(rf'^{name}_MARK = "(.*)"$', text, re.MULTILINE)
        for kind, name in (("table", "TABLE"), ("accepted", "ACCEPTED"), ("returned", "RETURNED"))
    }
    assert all(written.values()), "accept-issue no longer defines its marker constants"
    expected = {m.group(1): kind for kind, m in written.items() if m}
    assert expected == pf.ACCEPT_MARKS
    assert tuple(expected) == (TABLE, ACCEPTED, RETURNED)


# ─── accept needs a card that says the work was done here (H1) ──────────────
# The reviewer's live cases, as offline rows: a whole task whose card is in Todo and whose
# merged pull request(s) with Refs finished it before the issue was taken (api#362, #217,
# #258, #282, #224), and an epic (api#139).
LIVE_TODO_ROWS = [
    (362, API, 364, "docs(report): record the rules.md A/B on review prompt v5 (#163)"),
    (217, API, 364, "docs(report): record the rules.md A/B on review prompt v5 (#163)"),
    (258, API, 230, "feat(reviews): guard, budget, summary and candidate dropping (#219)"),
    (282, API, 284, "ci: split the Python job into unit and four integration shards (#281)"),
    (224, UI, 109, "feat(repository): RAG toggle and index status (#102)"),
]
WHOLE_TASK = "## Scope\n\n- x\n" + KICKOFF


@pytest.mark.parametrize(("issue", "pr_repo", "pr_number", "title"), LIVE_TODO_ROWS)
def test_a_merged_pull_request_on_a_todo_card_is_a_stop_not_accept(
    tmp_path: Path, issue: int, pr_repo: str, pr_number: int, title: str
) -> None:
    refs = f"Refs #{issue}" if pr_repo == API else f"Refs {API}#{issue}"
    node = refs_pr(pr_number, "does a thing", title=title, refs=refs, repo=pr_repo)
    response = issue_response(body=WHOLE_TASK, timeline=[node], items=[project_item("Todo")])
    code, doc = facts(tmp_path, response, ref=f"api#{issue}")
    assert (code, doc["verdict"], doc["selected"]) == (1, "stop", None)
    assert doc["reasons"] == [
        "the card is in Todo: the work was never started here — the merged pull request(s) "
        f"with Refs are {pr_repo}#{pr_number} (MERGED); take the task with the user"
    ]


def test_an_epic_is_a_stop_before_anything_else(tmp_path: Path) -> None:
    # api#139: nine sub-issues, seven closed, three merged pull requests with Refs, In Progress.
    timeline = [refs_pr(n, "x", refs="Refs #139") for n in (157, 158, 161)]
    response = issue_response(body=WHOLE_TASK, timeline=timeline, sub_issues=(9, 7))
    code, doc = facts(tmp_path, response, ref="api#139")
    assert (code, doc["verdict"], doc["selected"]) == (1, "stop", None)
    assert doc["reasons"] == [
        "an epic: it has 9 sub-issues (2 open) — implement its sub-issues, not the epic"
    ]
    assert doc["issue"]["sub_issues"] == {"total": 9, "open": 2}


def test_an_epic_stops_before_a_blocker_a_part_and_a_bad_part(tmp_path: Path) -> None:
    response = issue_response(body=TWO_PARTS, blocked=[blocker(5)], sub_issues=(3, 3))
    for args in ((), ("--part", "1"), ("--part", "99")):  # 99: not an exit 2, the epic comes first
        code, doc = facts(tmp_path, response, *args)
        assert (code, doc["verdict"]) == (1, "stop"), args
        assert doc["reasons"] == [
            "an epic: it has 3 sub-issues (0 open) — implement its sub-issues, not the epic"
        ]


def test_a_closed_epic_issue_is_closed_first(tmp_path: Path) -> None:
    response = issue_response(body=WHOLE_TASK, state="CLOSED", sub_issues=(2, 1))
    _, doc = facts(tmp_path, response)
    assert doc["reasons"] == [f"issue {API}#13 is CLOSED"]


@pytest.mark.parametrize("card", ["In Progress", "On Review", "AC Check"])
def test_accept_needs_a_card_in_progress_on_review_or_ac_check(tmp_path: Path, card: str) -> None:
    items = [project_item(card)]
    whole = issue_response(body=WHOLE_TASK, timeline=[refs_pr(50, "x")], items=items)
    parts = issue_response(body=TWO_PARTS, timeline=merged_parts(), items=items)
    for response in (whole, parts):
        code, doc = facts(tmp_path, response)
        assert (code, doc["verdict"], doc["selected"]) == (1, "accept", None)


@pytest.mark.parametrize(
    ("items", "where"),
    [
        ([project_item("Todo")], "the card is in Todo"),
        ([project_item("Done")], "the card is in Done"),
        ([project_item(None)], f"the issue has no Status on board {pf.BOARD_NUMBER}"),
        ([], f"the issue has no Status on board {pf.BOARD_NUMBER}"),
        (
            [project_item("On Review", number=7)],
            f"the issue has no Status on board {pf.BOARD_NUMBER}",
        ),
    ],
)
def test_any_other_card_is_a_stop_for_parts_and_for_a_whole_task(
    tmp_path: Path, items: list[dict[str, Any]], where: str
) -> None:
    tail = ": the work was never started here — the merged pull request(s) with Refs are "
    whole = issue_response(body=WHOLE_TASK, timeline=[refs_pr(50, "x")], items=items)
    _, doc = facts(tmp_path, whole)
    assert (doc["verdict"], doc["reasons"]) == (
        "stop",
        [f"{where}{tail}{API}#50 (MERGED); take the task with the user"],
    )
    parts = issue_response(body=TWO_PARTS, timeline=merged_parts(), items=items)
    _, doc = facts(tmp_path, parts)
    assert doc["verdict"] == "stop"
    assert doc["reasons"] == [
        f"{where}{tail}{API}#100 (MERGED), {API}#200 (MERGED); take the task with the user"
    ]


def test_the_card_matters_only_when_acceptance_would_be_next(tmp_path: Path) -> None:
    todo = [project_item("Todo")]
    # nothing merged: the whole task is taken from Todo, as ever
    _, doc = facts(tmp_path, issue_response(body=WHOLE_TASK, items=todo))
    assert doc["selected"] == {"part": None, "reason": "whole-task"}
    # a part still open: the next part is taken from Todo, an explicit part too
    one = [refs_pr(100, "Part 1 of #13: x")]
    _, doc = facts(tmp_path, issue_response(body=TWO_PARTS, timeline=one, items=todo))
    assert doc["selected"] == {"part": 2, "reason": "first-todo"}
    response = issue_response(body=TWO_PARTS, timeline=merged_parts(), items=todo)
    _, doc = facts(tmp_path, response, "--part", "2")
    assert doc["selected"] == {"part": 2, "reason": "explicit"}
    # the card is judged before the return: Todo + returned says the card
    comments = [comment(RETURNED, "2026-10-21T09:00:00Z", 2)]
    response = issue_response(
        body=TWO_PARTS, timeline=merged_parts(), comments=comments, items=todo
    )
    _, doc = facts(tmp_path, response)
    assert doc["reasons"][0].startswith("the card is in Todo: ")


# ─── held: the comment the rework has to read (M1) ──────────────────────────
def test_held_names_the_comment_on_a_stop_and_on_a_go(tmp_path: Path) -> None:
    returned = [comment(RETURNED + "\n\nЧто осталось: AC2", "2026-10-21T09:00:00Z", 2)]
    response = issue_response(body=TWO_PARTS, timeline=merged_parts(), comments=returned)
    _, doc = facts(tmp_path, response)
    assert (doc["verdict"], doc["held"]) == ("stop", {"kind": "returned", "url": comment_url(2)})
    # --part K goes, and `held` is there for the run to read first
    code, doc = facts(tmp_path, response, "--part", "2")
    assert (code, doc["verdict"]) == (0, "go")
    assert doc["held"] == {"kind": "returned", "url": comment_url(2)}
    # a part still todo goes as ever, and `held` is there too
    one = [refs_pr(100, "Part 1 of #13: x")]
    response = issue_response(body=TWO_PARTS, timeline=one, comments=returned)
    code, doc = facts(tmp_path, response)
    assert (code, doc["selected"]) == (0, {"part": 2, "reason": "first-todo"})
    assert doc["held"] == {"kind": "returned", "url": comment_url(2)}
    # a table with unmet rows is `unmet_table`
    table = [comment(TABLE_UNMET, "2026-10-21T09:00:00Z", 3)]
    response = issue_response(body=TWO_PARTS, timeline=merged_parts(), comments=table)
    _, doc = facts(tmp_path, response, "--part", "1")
    assert doc["held"] == {"kind": "unmet_table", "url": comment_url(3)}


def test_held_is_null_when_nothing_holds_the_issue_back(tmp_path: Path) -> None:
    cases = {
        "no comment": [],
        "table met": [comment(TABLE_MET, "2026-10-21T09:00:00Z", 2)],
        "accepted": [comment(ACCEPTED, "2026-10-21T09:00:00Z", 2)],
        "merged after": [comment(RETURNED, "2026-10-19T09:00:00Z", 2)],  # merged_parts: 10-20
    }
    for name, comments in cases.items():
        response = issue_response(body=TWO_PARTS, timeline=merged_parts(), comments=comments)
        _, doc = facts(tmp_path, response)
        assert doc["held"] is None, name
        assert doc["verdict"] == "accept", name


# ─── a line regex is linear (I1) ────────────────────────────────────────────
@pytest.mark.parametrize(
    "line",
    [
        "Refs #1" + " " * 65000 + "x",
        "Refs #1" + " " * 65000 + ",",
        "Refs #1" + " \t" * 32000 + "and",
        "- " + " " * 65000 + "Refs #1",
        "## Scope" + " " * 65000 + "x",
        "- Part 1" + " " * 65000 + "x",
        "- Part 1 (label" + " " * 65000 + ":",
    ],
)
def test_the_line_regexes_are_linear(tmp_path: Path, line: str) -> None:
    start = time.monotonic()
    pf.REFS_LINE.match(line)
    pf.HEADING.match(line)
    pf.PART_BULLET.fullmatch(line.lstrip("- "))
    assert time.monotonic() - start < 1
    # ...and end to end: a pull request body and an issue body made of such lines
    body = f"## What\n\nPart 2 of #13: x\n\n## Refs\n\n{line}\nRefs #13\n"
    start = time.monotonic()
    doc = _ok(
        tmp_path,
        issue_response(body=f"## Scope\n\n{line}\n" + TWO_PARTS, timeline=[pr(1, body=body)]),
    )
    assert time.monotonic() - start < 5
    assert doc["schema"] == 1


def test_a_heading_keeps_its_text_without_the_closing_sequence() -> None:
    lines = ["## Scope", "## Scope ##", "## Scope  ##  ", "##\tScope\t#", "  ## Scope"]
    for line in lines:
        got = pf.section_lines(f"{line}\n- a\n## Next\n- b\n", named_scope, skip_quotes=False)
        assert got == ["- a"], line
    assert pf.section_lines("#### Scope\n- a\n", named_scope, skip_quotes=False) == ["- a"]
    assert pf.section_lines("####### Scope\n- a\n", named_scope, skip_quotes=False) is None
    assert pf.section_lines("    ## Scope\n- a\n", named_scope, skip_quotes=False) is None


def named_scope(heading: str) -> bool:
    return heading == "Scope"


# ─── a range whose end does not parse names nothing; `&` joins a list (I2) ──
@pytest.mark.parametrize(
    ("title", "what", "expected"),
    [
        (f"Part 1-{'9' * 11}", "", []),
        (f"Part 1 – {'9' * 5000}", "", []),
        (f"Part {'9' * 11}-3", "", []),
        ("feat: y", f"Part 1-{'9' * 11}: x. Part 2 is next.", []),
        ("Parts 2 & 3", "", [2, 3]),
        ("Parts 1, 2 & 3", "", [1, 2, 3]),
        ("Parts 1 &amp; 2", "", []),
        ("Parts 2 &3", "", []),
        ("Part 2 & 3", "", [2]),  # a singular label names one part
        ("Teile 2 & 3 von #13", "", [2, 3]),
        ("feat: y", "Parts 2 & 3 of #13: x. Part 1 is merged.", [2, 3]),
    ],
)
def test_an_unparsable_range_end_names_nothing_and_an_ampersand_lists(
    tmp_path: Path, title: str, what: str, expected: list[int]
) -> None:
    assert named_in(tmp_path, refs_pr(90, what, title=title), parts=3) == expected


# ─── the branch pattern in the skill texts (7f) ─────────────────────────────
BRANCH_PATTERN = re.compile(r"`(\^\(feat\|fix\|docs\|chore\)/[^`]+\$)`")


def test_the_branch_pattern_is_the_same_in_both_skills_and_git_agrees() -> None:
    texts = [SKILL / "SKILL.md", SKILL.parent / "pull-request" / "SKILL.md"]
    found = {BRANCH_PATTERN.search(t.read_text(encoding="utf-8")) for t in texts}
    assert None not in found
    patterns = {m.group(1) for m in found if m}
    assert len(patterns) == 1, patterns
    rx = re.compile(patterns.pop())
    good = ["feat/269-x", "fix/a.b-c_d", "docs/lock", "chore/a.lockx", "feat/a.lock-b"]
    bad = ["feat/x.lock", "fix/269-a.lock", "feat/a..b", "feat/.a", "feat/a.", "feat/A", "wip/x"]
    assert all(rx.match(n) for n in good)
    assert not any(rx.match(n) for n in bad)
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    for name in good:  # what the pattern lets through, git must accept
        r = subprocess.run(["git", "check-ref-format", "--branch", name], capture_output=True)
        assert r.returncode == 0, name
    for name in ("feat/x.lock", "fix/269-a.lock"):  # the reason for the `.lock` clause
        r = subprocess.run(["git", "check-ref-format", "--branch", name], capture_output=True)
        assert r.returncode != 0, name
