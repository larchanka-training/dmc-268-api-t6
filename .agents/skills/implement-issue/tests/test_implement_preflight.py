"""Contract tests for scripts/preflight.py.

Offline: no network, no tokens, no live `gh`. An autouse guard puts a fake `gh`
first on PATH that fails with exit 97, so any request that escapes the fixture
seam (`IMPLEMENT_ISSUE_FIXTURE_DIR`) or a test's own fake is an error, never a call.

Covers: every `examples/*/` case (stdout byte-equal to `expected.json`, exit code from
the verdict, the invocation read from the case's README); synthetic issues for the
selection rules, the `Refs` matcher, the way a pull request names a part, malformed
numbering, paging of blockers and of the timeline past one page, the Kickoff and the
board Status; the REF grammar, exit codes 2 and 3, and the argv and queries the script
hands to `gh` (read-only, never a mutation).
"""
# SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/implement-issue/tests/test_implement_preflight.py  # noqa: E501

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
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


def pr(
    number: int,
    *,
    title: str = "feat: x",
    body: str = "",
    state: str = "MERGED",
    repo: str = API,
) -> dict[str, Any]:
    """A CrossReferencedEvent whose source is a pull request."""
    return {
        "__typename": "CrossReferencedEvent",
        "source": {
            "__typename": "PullRequest",
            "number": number,
            "title": title,
            "url": f"https://github.com/{repo}/pull/{number}",
            "body": body,
            "state": state,
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


def issue_response(
    *,
    body: str = "",
    state: str = "OPEN",
    blocked: list[dict[str, Any]] | None = None,
    timeline: list[dict[str, Any]] | None = None,
    items: list[dict[str, Any]] | None = None,
    assignees: tuple[str, ...] = ("axyi",),
    blocked_next: str | None = None,
    timeline_next: str | None = None,
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
                    "projectItems": {
                        "pageInfo": page_info(None),
                        "nodes": [project_item("Todo")] if items is None else items,
                    },
                    "blockedBy": {"pageInfo": page_info(blocked_next), "nodes": blocked or []},
                    "timelineItems": {
                        "pageInfo": page_info(timeline_next),
                        "nodes": timeline or [],
                    },
                }
            }
        }
    }


def follow_up(field: str, nodes: list[dict[str, Any]], next_cursor: str | None) -> dict[str, Any]:
    connection = {"pageInfo": page_info(next_cursor), "nodes": nodes}
    return {"data": {"repository": {"issue": {field: connection}}}}


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


def test_the_examples_cover_the_four_decisions() -> None:
    seen = set()
    for case in CASES:
        doc = json.loads((EXAMPLES / case / "expected.json").read_text(encoding="utf-8"))
        seen.add((doc["verdict"], (doc["selected"] or {}).get("reason")))
    assert {
        ("stop", None),
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
    assert counts(tmp_path, refs_pr(30, "Part 1 of #13: x", refs=f"Refs {API}#13", repo=UI))
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


def named_in(tmp_path: Path, node: dict[str, Any], parts: int = 2) -> list[int]:
    scope = "".join(f"- Ч.{n} (PR): scope {n}\n" for n in range(1, parts + 1))
    response = issue_response(body=f"## Scope\n\n{scope}", timeline=[node])
    _, doc = facts(tmp_path, response)
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
        ("Parts 1 and 2", "", []),  # only `Part K` / `Ч.K` forms
        ("Part 2.5", "", [2]),
        ("Counterpart 2", "", []),
        ("Part 3 of #13: nonexistent", "", []),
        ("feat: y", "", []),
        ("feat: y", "First line.\nPart 2 of #13: second line", []),  # first line only
        ("feat: y", "<!-- Part 2 -->\nPart 1 of #13: x", [1]),  # comments are not text
        ("feat: y", "```\nPart 2\n```\nPart 1 of #13: x", [1]),  # nor are fences
        # What names only the first part it mentions: the rest is context
        ("feat: y", UI_177_WHAT, [1]),
        ("docs: y", API_358_WHAT, [2]),
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
        # a plural or a range names no single part, whatever the label
        ("Parts 1–3", "", []),
        ("Parts 1 and 2", "", []),
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


def test_a_merged_pull_request_that_mentions_the_next_part_does_not_finish_it(
    tmp_path: Path,
) -> None:
    # ui#177 is Part 1 only, though its What also says "part 2": a two-part issue goes on to
    # Part 2 (not "every part has a merged pull request"), a three-part one to Part 2 (not 3).
    ui_177 = refs_pr(
        177,
        UI_177_WHAT,
        title="feat(agents): add the issue-graph script",
        refs=f"Refs {API}#13",
        repo=UI,
    )
    for body in (TWO_PARTS, THREE_PARTS):
        code, doc = facts(tmp_path, issue_response(body=body, timeline=[ui_177]))
        assert (code, doc["selected"]) == (0, {"part": 2, "reason": "first-todo"})
        assert [p["ref"] for p in parts_of(doc)[1]["prs"]] == [f"{UI}#177"]
        assert parts_of(doc)[2]["status"] == "todo"
        assert doc["warnings"] == []


def test_a_pull_request_that_looks_back_at_part_1_is_part_2_only(tmp_path: Path) -> None:
    # api#358: "Part 2 of #273: … Part 1, … is merged in #357." — Part 1 stays todo.
    api_358 = refs_pr(358, API_358_WHAT, title="docs(contributing): the sprint start ritual")
    code, doc = facts(tmp_path, issue_response(body=TWO_PARTS, timeline=[api_358]))
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


def test_every_part_done_stops(tmp_path: Path) -> None:
    code, doc = two_part_run(tmp_path, {1: ["MERGED"], 2: ["MERGED"]})
    assert code == 1
    assert doc["reasons"] == ["every part has a merged pull request — acceptance is next"]
    assert doc["selected"] is None


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


@pytest.mark.parametrize("state", ["OPEN", "MERGED"])
def test_no_parts_and_a_pull_request_with_refs_stops(tmp_path: Path, state: str) -> None:
    timeline = [refs_pr(50, "the thing", state=state), refs_pr(51, "no refs", refs="-")]
    code, doc = facts(tmp_path, issue_response(body="## Scope\n\n- x\n", timeline=timeline))
    assert code == 1
    assert doc["reasons"] == [
        "the task has no parts and already has a pull request with Refs: "
        f"{API}#50 ({state}) — a rework PR is a human decision"
    ]
    assert [p["ref"] for p in doc["task_prs"]] == [f"{API}#50"]


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
        "board_status": "Todo",
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


@pytest.mark.parametrize("name", ["blocked-p2", "timeline-p2"])
def test_a_missing_second_page_exits_3(tmp_path: Path, name: str) -> None:
    if name.startswith("blocked"):
        response = issue_response(blocked_next="C1")
    else:
        response = issue_response(timeline_next="C1")
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
names = {None: "issue-p1", "B1": "blocked-p2", "B2": "blocked-p3", "T1": "timeline-p2"}
sys.stdout.write(Path(os.environ["FAKE_DIR"], names[cursor] + ".json").read_text(encoding="utf-8"))
"""


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
    # The query text itself: dropping any of these silently loses a fact.
    query = next(a for a in first if a.startswith("query="))
    for needle in (
        "state",
        "assignees(first: 20)",
        "projectItems(first: 100)",
        'fieldValueByName(name: "Status")',
        "project {",
        "number",
        "blockedBy(first: 100)",
        "timelineItems(first: 100, itemTypes: [CROSS_REFERENCED_EVENT])",
        "... on PullRequest {",
        "body",
        "repository { nameWithOwner }",
    ):
        assert needle in query, needle
    assert "timelineItems(last" not in query
    assert "CLOSED_EVENT" not in query  # the closing history is accept-issue's, not ours
    blocked_query = next(a for a in second if a.startswith("query="))
    assert "blockedBy(first: 100, after: $c)" in blocked_query
    assert "state" in blocked_query and "repository { nameWithOwner }" in blocked_query
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
    query = next(a for a in calls[1] if a.startswith("query="))
    assert "timelineItems(first: 100, after: $c, itemTypes: [CROSS_REFERENCED_EVENT])" in query
    assert (
        "... on PullRequest { number title url body state repository { nameWithOwner } }" in query
    )
    assert "mutation" not in query
