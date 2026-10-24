"""Contract tests for scripts/issue-batch.py and the golden examples.

Offline — no network, nothing is ever created. Two seams:

* ISSUE_BATCH_SCRIPTS_DIR points the driver at tests/fake_issue_scripts.py, copied
  as issue-create.sh and issue-link.sh: it records each call (argv, the spec it was
  given, the state file at that moment) and answers from a config, so order, `@key`
  resolution, state writes, resume, adoption and exit aggregation are asserted;
* the examples run the REAL scripts with the ISSUE_FIXTURE_DIR seam: the preview in
  `examples/<case>/preview.md` is what `--dry-run` prints, byte for byte.

What carries weight: nothing runs before the whole batch is valid; parents and
blockers are created before what depends on them; every result is in the state file
before the next item starts, so a rerun never creates an item twice — an item already
recorded only gets its links ensured, and an exact-title duplicate is adopted, never
created again.
"""
# SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/create-issue/tests/test_batch.py

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
SCRIPT = SKILL / "scripts" / "issue-batch.py"
EXAMPLES = SKILL / "examples"
FAKE = HERE / "fake_issue_scripts.py"
CASES = ["01-single", "02-two-prs", "03-ui-api"]
API, UI = "acme/api", "acme/ui"


def item(key: str, repo: str = API, **fields: Any) -> dict[str, Any]:
    """The smallest valid batch item; its summary names its key."""
    return {
        "key": key,
        "repo": repo,
        "lang": "en",
        "summary": f"[x] {key}",
        "acceptance_criteria": ["ok"],
        **fields,
    }


def batch(*items: dict[str, Any], bid: str = "b1") -> dict[str, Any]:
    return {"id": bid, "items": list(items)}


def state_path(tmp_path: Path, bid: str = "b1") -> Path:
    return tmp_path / "cache" / "create-issue" / "batches" / f"{bid}.json"


def run(
    tmp_path: Path,
    data: Any,
    *flags: str,
    cfg: dict[str, Any] | None = None,
    env: dict[str, str | None] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[dict[str, Any]]]:
    """Run the driver against the stand-in scripts; return the result and the calls
    made during THIS run (the log is shared by reruns in the same tmp_path)."""
    stubs = tmp_path / "stubs"
    if not stubs.exists():
        stubs.mkdir()
        for name in ("issue-create.sh", "issue-link.sh"):
            shutil.copy(FAKE, stubs / name)
            (stubs / name).chmod(0o755)
    log = tmp_path / "calls.jsonl"
    seen = len(log.read_text().splitlines()) if log.exists() else 0
    (tmp_path / "cfg.json").write_text(json.dumps(cfg or {}))
    path = tmp_path / "batch.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    bid = data.get("id") if isinstance(data, dict) else None
    full: dict[str, str] = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "ISSUE_BATCH_SCRIPTS_DIR": str(stubs),
        "STUB_CFG": str(tmp_path / "cfg.json"),
        "STUB_LOG": str(log),
        "STUB_STATE": str(state_path(tmp_path, bid if isinstance(bid, str) else "none")),
    }
    for key, val in (env or {}).items():
        if val is None:
            full.pop(key, None)
        else:
            full[key] = val
    r = subprocess.run(
        [sys.executable, str(SCRIPT), str(path), *flags],
        capture_output=True,
        text=True,
        env=full,
        timeout=120,
        check=False,
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return r, calls[seen:]


def creates(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The real creates, in order (not the --dry-run validations)."""
    return [c for c in calls if c["script"] == "create" and "--dry-run" not in c["argv"]]


def created_keys(calls: list[dict[str, Any]]) -> list[str]:
    return [c["spec"]["summary"].removeprefix("[x] ") for c in creates(calls)]


def links(calls: list[dict[str, Any]]) -> list[list[str]]:
    return [c["argv"] for c in calls if c["script"] == "link"]


def out(r: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(r.stdout)
    return parsed


def error(r: subprocess.CompletedProcess[str]) -> str:
    return str(json.loads(r.stderr.strip().splitlines()[-1])["error"])


def state(tmp_path: Path, bid: str = "b1") -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(state_path(tmp_path, bid).read_text())
    return parsed


def rec(repo: str, num: int) -> dict[str, Any]:
    """One state-file entry."""
    return {"url": f"https://github.com/{repo}/issues/{num}", "number": num, "repo": repo}


def cfg_create(**by_key: dict[str, Any]) -> dict[str, Any]:
    """Stub answers keyed by item key (the stub sees the summary)."""
    return {"create": {f"[x] {key}": val for key, val in by_key.items()}}


# ─── validation: nothing runs before the whole batch is valid ───────────────
@pytest.mark.parametrize(
    "data,needle",
    [
        ("{not json", "not valid JSON"),
        (["a"], "batch must be a JSON object"),
        ({"items": [item("a")]}, '"id"'),
        (batch(item("a"), bid="Bad_Id"), '"id" must match'),
        ({"id": "b1", "items": [item("a")], "extra": 1}, "unknown top-level key(s): extra"),
        ({"id": "b1", "items": []}, '"items" must be a non-empty list'),
        ({"id": "b1", "items": "a"}, '"items" must be a non-empty list'),
        ({"id": "b1", "items": ["a"]}, "item 1 must be an object"),
        (batch({"repo": API, "summary": "[x] a"}), 'item 1 has no "key"'),
        (batch(item("Not ok")), 'item 1 "key" must match'),
        (batch(item("a"), item("a")), 'duplicate key "a"'),
        # local refs: known, not the item itself, no cycle
        (batch(item("a", parent="@nope")), 'item "a": unknown local ref "@nope"'),
        (batch(item("a", blocked_by=["@b"]), item("b")), None),
        (batch(item("a", blocked_by=["@zz"])), 'unknown local ref "@zz"'),
        (batch(item("a", parent="@a")), 'item "a" refers to itself'),
        (batch(item("a", blocked_by=["@a"])), 'item "a" refers to itself'),
        (batch(item("a", blocked_by=["@b", "@b"]), item("b")), 'lists "@b" more than once'),
        (
            batch(item("a", parent="@b"), item("b", blocked_by=["@c"]), item("c", parent="@a")),
            "dependency cycle: a → b → c → a",
        ),
        (batch(item("a", blocked_by=["@b"]), item("b", blocked_by=["@a"])), "cycle"),
        # GitHub: a sub-issue shares its parent's owner
        (
            batch(item("a", repo="other/api"), item("b", parent="@a")),
            'item "b": parent "@a" is in other/api, but a sub-issue must share its',
        ),
        # every item is a valid spec for the builder (its message, prefixed)
        (batch(item("a"), item("b", estimate=3)), 'item "b": unknown top-level key(s): estimate'),
        (batch(item("a", parent="5")), 'item "a": "parent" is not an issue ref'),
        (batch(item("a", parent=5)), 'item "a": "parent" must be a string'),
        (batch(item("a", blocked_by="@b"), item("b")), '"blocked_by" must be a list of strings'),
        # two items with one title in one repo: the second create would hit the
        # duplicate guard and adopt the first item's issue
        (batch(item("a"), item("b", summary="[x] a")), 'items "a" and "b" share the title'),
        (batch(item("a"), item("b", summary=" [x] a ")), 'items "a" and "b" share the title'),
        (
            batch(item("a"), item("b", repo="ACME/api", summary="[x] a")),
            'items "a" and "b" share the title',
        ),
        # a local ref in the body would render as an @mention of a stranger
        (
            batch(item("a"), item("b", background="Needs @a first")),
            'item "b": the body mentions "@a"',
        ),
    ],
)
def test_an_invalid_batch_exits_2_before_anything_runs(
    tmp_path: Path, data: Any, needle: str | None
) -> None:
    r, calls = run(tmp_path, data, cfg={})
    if needle is None:  # the control case: a valid forward ref
        assert r.returncode == 0, r.stderr
        return
    assert r.returncode == 2, r.stderr
    assert needle in error(r)
    assert "Traceback" not in r.stderr
    assert r.stdout == ""
    assert calls == []
    assert not (tmp_path / "cache").exists()


def test_one_title_in_two_repos_is_two_issues(tmp_path: Path) -> None:
    r, calls = run(tmp_path, batch(item("a"), item("b", repo=UI, summary="[x] a")))
    assert r.returncode == 0, r.stderr
    assert len(creates(calls)) == 2


def test_an_invalid_batch_also_stops_the_dry_run(tmp_path: Path) -> None:
    r, calls = run(tmp_path, batch(item("a", parent="@a")), "--dry-run")
    assert r.returncode == 2
    assert calls == []


def test_usage_errors_exit_2(tmp_path: Path) -> None:
    for argv in ([], ["a.json", "b.json"], ["--bogus", "a.json"], ["--compare-shape", "a"]):
        r = subprocess.run(
            [sys.executable, str(SCRIPT), *argv], capture_output=True, text=True, check=False
        )
        assert r.returncode == 2, argv
        assert "usage" in json.loads(r.stderr)["error"]


def test_a_missing_batch_file_exits_2(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), str(missing)], capture_output=True, text=True, check=False
    )
    assert r.returncode == 2
    assert "cannot read" in json.loads(r.stderr)["error"]


# ─── order ──────────────────────────────────────────────────────────────────
def test_parents_and_blockers_are_created_before_what_depends_on_them(tmp_path: Path) -> None:
    """File order c, x, b, p: c waits for p (parent) and b (blocker), b for p; x is
    free. The earliest item in file order whose dependencies exist goes next."""
    data = batch(
        item("c", parent="@p", blocked_by=["@b"]),
        item("x"),
        item("b", parent="@p"),
        item("p"),
    )
    r, calls = run(tmp_path, data)
    assert r.returncode == 0, r.stderr
    assert created_keys(calls) == ["x", "p", "b", "c"]
    assert [i["key"] for i in out(r)["items"]] == ["x", "p", "b", "c"]


def test_independent_items_keep_their_file_order(tmp_path: Path) -> None:
    r, calls = run(tmp_path, batch(item("z"), item("m"), item("a")))
    assert r.returncode == 0, r.stderr
    assert created_keys(calls) == ["z", "m", "a"]


# ─── @key resolution ────────────────────────────────────────────────────────
def test_local_refs_become_the_created_issue_in_the_spec_passed_on(tmp_path: Path) -> None:
    data = batch(
        item("p"),
        item("c", repo=UI, parent="@p", blocked_by=["@p", "#7", "x/y#3"], milestone="S1"),
    )
    r, calls = run(tmp_path, data, cfg=cfg_create(p={"number": 41}, c={"number": 12}))
    assert r.returncode == 0, r.stderr
    spec = creates(calls)[1]["spec"]
    assert spec["parent"] == "acme/api#41"
    assert spec["blocked_by"] == ["acme/api#41", "#7", "x/y#3"]
    assert "key" not in spec
    assert {k: v for k, v in spec.items() if k not in ("parent", "blocked_by")} == {
        k: v for k, v in data["items"][1].items() if k not in ("key", "parent", "blocked_by")
    }
    assert out(r)["items"][1] == {
        "key": "c",
        "url": "https://github.com/acme/ui/issues/12",
        "number": 12,
        "repo": UI,
        "created": True,
        "adopted": False,
        "verified": True,
        "board": "added",
        "links": {
            "parent": {"ref": "acme/api#41", "ok": True, "action": "linked"},
            "blocked_by": [
                {"ref": "acme/api#41", "ok": True, "action": "linked"},
                {"ref": "#7", "ok": True, "action": "linked"},
                {"ref": "x/y#3", "ok": True, "action": "linked"},
            ],
        },
    }
    assert out(r)["items"][0]["links"] is None


# ─── state file ─────────────────────────────────────────────────────────────
def test_each_result_is_recorded_before_the_next_item_starts(tmp_path: Path) -> None:
    data = batch(item("a"), item("b", parent="@a"), item("c"))
    r, calls = run(tmp_path, data, cfg=cfg_create(a={"number": 1}, b={"number": 2}))
    assert r.returncode == 0, r.stderr
    seen = [c["state"] for c in creates(calls)]
    assert seen == [None, {"a": rec(API, 1)}, {"a": rec(API, 1), "b": rec(API, 2)}]
    assert state(tmp_path) == {"a": rec(API, 1), "b": rec(API, 2), "c": rec(API, 103)}
    assert out(r)["state_file"] == str(state_path(tmp_path))
    assert out(r)["id"] == "b1"


@pytest.mark.parametrize("xdg", [None, ""])
def test_without_xdg_cache_home_the_state_lives_under_home(tmp_path: Path, xdg: str | None) -> None:
    r, _ = run(tmp_path, batch(item("a")), env={"XDG_CACHE_HOME": xdg})
    assert r.returncode == 0, r.stderr
    path = tmp_path / "home" / ".cache" / "create-issue" / "batches" / "b1.json"
    assert json.loads(path.read_text()) == {"a": rec(API, 101)}
    assert out(r)["state_file"] == str(path)
    assert not (tmp_path / "cache").exists()


def test_the_state_file_is_written_atomically(tmp_path: Path) -> None:
    """Nothing but the state file is left behind: no temp file next to it."""
    r, _ = run(tmp_path, batch(item("a"), item("b")))
    assert r.returncode == 0, r.stderr
    assert sorted(p.name for p in state_path(tmp_path).parent.iterdir()) == ["b1.json"]


@pytest.mark.parametrize(
    "content,needle",
    [
        ("{broken", "is not valid JSON"),
        ('["a"]', "is not an object"),
        ('{"a": {"url": "u"}}', 'entry "a"'),
        ('{"a": {"url": "u", "number": "1", "repo": "acme/api"}}', 'entry "a"'),
    ],
)
def test_an_unreadable_state_file_stops_before_anything_runs(
    tmp_path: Path, content: str, needle: str
) -> None:
    state_path(tmp_path).parent.mkdir(parents=True)
    state_path(tmp_path).write_text(content)
    r, calls = run(tmp_path, batch(item("a")))
    assert r.returncode == 2
    assert needle in error(r)
    assert calls == []
    assert state_path(tmp_path).read_text() == content, "never overwritten"


def test_a_state_entry_in_another_repo_than_the_item_stops_the_run(tmp_path: Path) -> None:
    state_path(tmp_path).parent.mkdir(parents=True)
    state_path(tmp_path).write_text(json.dumps({"a": rec(UI, 5)}))
    r, calls = run(tmp_path, batch(item("a")))
    assert r.returncode == 2
    assert 'item "a" is recorded as acme/ui#5' in error(r)
    assert calls == []


# ─── rerun: resume, never create twice ──────────────────────────────────────
def test_a_rerun_after_a_failure_creates_nothing_twice_and_ensures_links(
    tmp_path: Path,
) -> None:
    data = batch(item("a", blocked_by=["#7"]), item("b", parent="@a"), item("c", blocked_by=["@b"]))
    first, calls = run(tmp_path, data, cfg=cfg_create(a={"number": 1}, b={"code": 5}))
    assert first.returncode == 5
    assert created_keys(calls) == ["a", "b"]
    assert state(tmp_path) == {"a": rec(API, 1)}
    assert 'item "b"' in error(first)
    assert [i["key"] for i in out(first)["items"]] == ["a"], "what was done is still reported"

    again, calls = run(tmp_path, data, cfg=cfg_create(b={"number": 2}, c={"number": 3}))
    assert again.returncode == 0, again.stderr
    assert created_keys(calls) == ["b", "c"]
    assert links(calls) == [["acme/api#1", "--blocked-by", "#7"]]
    assert creates(calls)[0]["spec"]["parent"] == "acme/api#1"
    assert creates(calls)[1]["spec"]["blocked_by"] == ["acme/api#2"]
    items = out(again)["items"]
    assert items[0] == {
        "key": "a",
        "url": "https://github.com/acme/api/issues/1",
        "number": 1,
        "repo": API,
        "created": False,
        "adopted": False,
        "verified": None,
        "board": None,
        "links": {
            "parent": None,
            "blocked_by": [{"ref": "#7", "ok": True, "action": "exists"}],
        },
    }
    assert [i["created"] for i in items] == [False, True, True]
    assert state(tmp_path) == {"a": rec(API, 1), "b": rec(API, 2), "c": rec(API, 3)}


def test_a_recorded_item_gets_its_local_refs_from_the_state(tmp_path: Path) -> None:
    state_path(tmp_path).parent.mkdir(parents=True)
    state_path(tmp_path).write_text(json.dumps({"p": rec(API, 4), "c": rec(UI, 9)}))
    r, calls = run(tmp_path, batch(item("p"), item("c", repo=UI, parent="@p", blocked_by=["@p"])))
    assert r.returncode == 0, r.stderr
    assert creates(calls) == []
    # p has no links: issue-link.sh is not called for it (it refuses an empty call)
    assert links(calls) == [["acme/ui#9", "--parent", "acme/api#4", "--blocked-by", "acme/api#4"]]


def test_a_rerun_of_a_finished_batch_creates_nothing(tmp_path: Path) -> None:
    data = batch(item("a"), item("b", parent="@a"))
    first, _ = run(tmp_path, data)
    assert first.returncode == 0, first.stderr
    again, calls = run(tmp_path, data)
    assert again.returncode == 0, again.stderr
    assert creates(calls) == []
    assert links(calls) == [["acme/api#102", "--parent", "acme/api#101"]]
    assert [i["created"] for i in out(again)["items"]] == [False, False]


def test_state_entries_of_other_keys_are_kept(tmp_path: Path) -> None:
    state_path(tmp_path).parent.mkdir(parents=True)
    state_path(tmp_path).write_text(json.dumps({"gone": rec(API, 3)}))
    r, _ = run(tmp_path, batch(item("a")))
    assert r.returncode == 0, r.stderr
    assert state(tmp_path) == {"gone": rec(API, 3), "a": rec(API, 101)}


# ─── an exact-title duplicate is adopted, never created again ───────────────
def test_a_duplicate_is_adopted_recorded_and_linked(tmp_path: Path) -> None:
    data = batch(item("a"), item("b", parent="@a"), item("c", blocked_by=["@b"]))
    cfg = cfg_create(a={"number": 1}, b={"code": 4, "duplicate": 55}, c={"number": 3})
    r, calls = run(tmp_path, data, cfg=cfg)
    assert r.returncode == 0, r.stderr
    assert state(tmp_path) == {"a": rec(API, 1), "b": rec(API, 55), "c": rec(API, 3)}
    assert links(calls) == [["acme/api#55", "--parent", "acme/api#1"]]
    assert creates(calls)[2]["spec"]["blocked_by"] == ["acme/api#55"]
    b = out(r)["items"][1]
    assert (b["created"], b["adopted"], b["number"], b["board"]) == (False, True, 55, None)
    assert b["links"]["parent"] == {"ref": "acme/api#1", "ok": True, "action": "exists"}
    assert "already exists" in r.stderr, "the guard's message is passed on"


def test_an_adopted_item_without_links_is_only_recorded(tmp_path: Path) -> None:
    r, calls = run(tmp_path, batch(item("a")), cfg=cfg_create(a={"code": 4, "duplicate": 8}))
    assert r.returncode == 0, r.stderr
    assert links(calls) == []
    assert state(tmp_path) == {"a": rec(API, 8)}
    assert out(r)["items"][0]["links"] is None


def test_an_adopted_item_whose_link_fails_is_exit_7(tmp_path: Path) -> None:
    data = batch(item("a"), item("b", parent="@a"))
    cfg = cfg_create(a={"number": 1}, b={"code": 4, "duplicate": 55})
    cfg["link"] = {"acme/api#55": {"code": 7}}
    r, _ = run(tmp_path, data, cfg=cfg)
    assert r.returncode == 7
    assert state(tmp_path)["b"] == rec(API, 55)


# ─── exit codes: 6 and 7 continue, 2/3/4/5 stop ─────────────────────────────
@pytest.mark.parametrize("board_first", [True, False])
def test_board_and_link_failures_do_not_stop_the_batch(tmp_path: Path, board_first: bool) -> None:
    """7 wins over 6 whichever item failed first."""
    board, link = item("a"), item("b", blocked_by=["#7"])
    data = batch(*((board, link) if board_first else (link, board)), item("c"))
    r, calls = run(tmp_path, data, cfg=cfg_create(a={"code": 6}, b={"code": 7}))
    assert r.returncode == 7
    assert set(created_keys(calls)) == {"a", "b", "c"}
    assert set(state(tmp_path)) == {"a", "b", "c"}
    items = {i["key"]: i for i in out(r)["items"]}
    assert items["a"]["board"] == "failed"
    assert items["b"]["links"]["blocked_by"][0]["ok"] is False


def test_a_board_failure_alone_is_exit_6(tmp_path: Path) -> None:
    r, calls = run(tmp_path, batch(item("a"), item("b")), cfg=cfg_create(a={"code": 6}))
    assert r.returncode == 6
    assert created_keys(calls) == ["a", "b"]


def test_a_failed_link_on_a_recorded_item_is_exit_7(tmp_path: Path) -> None:
    state_path(tmp_path).parent.mkdir(parents=True)
    state_path(tmp_path).write_text(json.dumps({"a": rec(API, 1)}))
    r, calls = run(
        tmp_path,
        batch(item("a", blocked_by=["#7"]), item("b")),
        cfg={"link": {"acme/api#1": {"code": 7}}},
    )
    assert r.returncode == 7
    assert created_keys(calls) == ["b"]


@pytest.mark.parametrize("code", [3, 5, 2])
def test_a_link_step_that_cannot_run_on_a_recorded_item_stops_the_batch(
    tmp_path: Path, code: int
) -> None:
    """Auth, network or a target that no longer exists is not a link that failed to
    take: the batch stops there, as for a create."""
    state_path(tmp_path).parent.mkdir(parents=True)
    state_path(tmp_path).write_text(json.dumps({"a": rec(API, 1)}))
    r, calls = run(
        tmp_path,
        batch(item("a", blocked_by=["#7"]), item("b")),
        cfg={"link": {"acme/api#1": {"code": code}}},
    )
    assert r.returncode == code
    assert creates(calls) == []
    assert 'item "a"' in error(r)


@pytest.mark.parametrize("code", [2, 3, 4, 5])
def test_a_failed_create_stops_the_batch_and_keeps_what_was_created(
    tmp_path: Path, code: int
) -> None:
    data = batch(item("a"), item("b"), item("c"))
    r, calls = run(tmp_path, data, cfg=cfg_create(a={"number": 1}, b={"code": code}))
    assert r.returncode == code
    assert created_keys(calls) == ["a", "b"], "c never runs"
    assert state(tmp_path) == {"a": rec(API, 1)}
    assert [i["key"] for i in out(r)["items"]] == ["a"]
    assert 'item "b"' in error(r)
    assert "rerun" in error(r)


def test_an_unexpected_exit_code_of_an_item_stops_the_batch_as_5(tmp_path: Path) -> None:
    r, calls = run(tmp_path, batch(item("a"), item("b")), cfg=cfg_create(a={"code": 1}))
    assert r.returncode == 5
    assert created_keys(calls) == ["a"]
    assert "exit 1" in error(r)


def test_a_create_that_exits_0_without_a_url_stops_with_5(tmp_path: Path) -> None:
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "issue-create.sh").write_text("#!/bin/sh\necho '{}'\n")
    (stubs / "issue-create.sh").chmod(0o755)
    shutil.copy(FAKE, stubs / "issue-link.sh")
    r, _ = run(tmp_path, batch(item("a"), item("b")))
    assert r.returncode == 5
    assert "no url" in error(r)
    assert not state_path(tmp_path).exists()


# ─── --no-board and --dry-run ───────────────────────────────────────────────
def test_no_board_is_passed_to_every_item(tmp_path: Path) -> None:
    r, calls = run(tmp_path, batch(item("a"), item("b")), "--no-board")
    assert r.returncode == 0, r.stderr
    assert all(c["argv"][1:] == ["--no-board"] for c in creates(calls))
    assert [i["board"] for i in out(r)["items"]] == ["skipped", "skipped"]


def test_without_no_board_no_flag_is_passed(tmp_path: Path) -> None:
    r, calls = run(tmp_path, batch(item("a")))
    assert r.returncode == 0, r.stderr
    assert creates(calls)[0]["argv"][1:] == []


def test_dry_run_validates_every_item_without_local_refs_and_creates_nothing(
    tmp_path: Path,
) -> None:
    data = batch(item("p"), item("c", repo=UI, parent="@p", blocked_by=["@p", "#7"]))
    r, calls = run(tmp_path, data, "--dry-run", "--no-board")
    assert r.returncode == 0, r.stderr
    assert creates(calls) == []
    assert [c["argv"][1:] for c in calls] == [["--dry-run", "--no-board"]] * 2
    child = calls[1]["spec"]
    assert "parent" not in child and child["blocked_by"] == ["#7"]
    assert "parent" not in calls[0]["spec"] and "blocked_by" not in calls[0]["spec"]
    assert not (tmp_path / "cache").exists(), "a preview writes no state"
    assert r.stdout.startswith("Batch `b1`: 2 issues, in creation order.\n")
    assert "| 2 | `c` | acme/ui | Story | [x] c | @p | @p, acme/ui#7 | — | — |" in r.stdout
    assert "\n# 2. `c` — [x] c\n\nbody of [x] c\n" in r.stdout


def test_a_dry_run_that_fails_for_one_item_fails_the_preview(tmp_path: Path) -> None:
    data = batch(item("a"), item("b"), item("c"))
    r, calls = run(tmp_path, data, "--dry-run", cfg=cfg_create(b={"dry_code": 2}))
    assert r.returncode == 2
    assert r.stdout == ""
    assert 'item "b"' in error(r)
    assert len(calls) == 2, "stops at the first failure"


def test_a_preview_names_the_items_already_created(tmp_path: Path) -> None:
    state_path(tmp_path).parent.mkdir(parents=True)
    state_path(tmp_path).write_text(json.dumps({"a": rec(API, 1)}))
    r, _ = run(tmp_path, batch(item("a"), item("b")), "--dry-run")
    assert r.returncode == 0, r.stderr
    assert (
        "\nAlready created, only their links are ensured: "
        "`a` → https://github.com/acme/api/issues/1\n"
    ) in r.stdout


def test_a_title_with_a_pipe_does_not_break_the_table(tmp_path: Path) -> None:
    r, _ = run(tmp_path, batch(item("a", summary="[x] a | b")), "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "| [x] a \\| b |" in r.stdout


# ─── the golden examples ────────────────────────────────────────────────────
PROJECT_VIEW = {"id": "PVT_board12", "number": 12, "title": "dmc-268-t6"}
PROJECT_FIELDS = {
    "fields": [
        {
            "id": "PVTSSF_status",
            "name": "Status",
            "type": "ProjectV2SingleSelectField",
            "options": [{"id": "opt_todo", "name": "Todo"}, {"id": "opt_done", "name": "Done"}],
        }
    ],
    "totalCount": 1,
}


def example(case: str) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads((EXAMPLES / case / "batch.json").read_text())
    return parsed


def drop_sync_header(path: Path) -> str:
    """SYNC line, blank line, then the content: drop exactly two lines, no strip."""
    return path.read_bytes().decode("utf-8").split("\n", 2)[2]


@pytest.mark.parametrize("case", CASES)
def test_the_example_preview_is_what_the_dry_run_prints(tmp_path: Path, case: str) -> None:
    """The real scripts end to end (driver, issue-create.sh, preflight, builder),
    every call answered by the ISSUE_FIXTURE_DIR seam."""
    fix = tmp_path / "fix"
    fix.mkdir()
    for name, val in {
        "reach.code": "0",
        "reach.http": "200",
        "auth_user.code": "0",
        "auth_user.json": '{"login": "octocat"}',
        "repo_perms.json": "true",
        "project_view.json": json.dumps(PROJECT_VIEW),
        "project_fields.json": json.dumps(PROJECT_FIELDS),
    }.items():
        (fix / name).write_text(val)
    r = subprocess.run(
        [sys.executable, str(SCRIPT), str(EXAMPLES / case / "batch.json"), "--dry-run"],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
            "ISSUE_FIXTURE_DIR": str(fix),
        },
        timeout=120,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout == drop_sync_header(EXAMPLES / case / "preview.md")
    assert not (tmp_path / "cache").exists()


def test_every_example_case_is_complete() -> None:
    found = sorted(p.name for p in EXAMPLES.iterdir() if p.is_dir())
    assert found == CASES
    for case in CASES:
        names = sorted(p.name for p in (EXAMPLES / case).iterdir())
        assert names == ["batch.json", "expected.md", "input.md", "preview.md"], case


def test_every_example_markdown_carries_the_sync_header() -> None:
    files = sorted(EXAMPLES.glob("*/*.md"))
    assert len(files) == 3 * len(CASES)
    for path in files:
        first, second, _ = path.read_bytes().decode("utf-8").split("\n", 2)
        rel = path.relative_to(SKILL).as_posix()
        assert first == (
            f"<!-- SYNC: mirrored in dmc-268-{{ui,api}}-t6/.agents/skills/create-issue/{rel} -->"
        )
        assert second == "", rel


def test_the_examples_have_the_shape_their_expected_md_states() -> None:
    """The three cases the slicing rule is accepted by (references/slicing.md)."""
    single, two, ui_api = (example(c)["items"] for c in CASES)
    assert len(single) == 1 and "parts" not in single[0]
    assert len(two) == 1 and len(two[0]["parts"]) == 2
    assert [i["repo"].split("/")[1] for i in ui_api] == ["dmc-268-api-t6", "dmc-268-ui-t6"]
    assert ui_api[1]["parent"] == "@" + ui_api[0]["key"]
    assert ui_api[1]["blocked_by"] == ["@" + ui_api[0]["key"]]
    for items in (single, two, ui_api):
        for it in items:
            assert it["kickoff"]["read_first"] and it["kickoff"]["mines"], it["key"]


# ─── --compare-shape ────────────────────────────────────────────────────────
def compare(tmp_path: Path, golden: Any, candidate: Any) -> subprocess.CompletedProcess[str]:
    g, c = tmp_path / "golden.json", tmp_path / "candidate.json"
    g.write_text(json.dumps(golden), encoding="utf-8")
    c.write_text(json.dumps(candidate), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--compare-shape", str(g), str(c)],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("case", CASES)
def test_compare_shape_is_equal_on_itself(tmp_path: Path, case: str) -> None:
    r = compare(tmp_path, example(case), example(case))
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads(r.stdout) == {"same_shape": True, "differences": []}


def renamed(data: dict[str, Any]) -> dict[str, Any]:
    """Other keys, other titles, other wording, items in another file order: the shape
    is the same."""
    out = copy.deepcopy(data)
    out["id"] = "other-id"
    names = {it["key"]: f"k{n}" for n, it in enumerate(out["items"])}
    for it in out["items"]:
        it["key"] = names[it["key"]]
        it["summary"] = "[z] reworded"
        it["acceptance_criteria"] = ["reworded"] * len(it.get("acceptance_criteria", []))
        if "parent" in it:
            it["parent"] = "@" + names[it["parent"][1:]]
        if "blocked_by" in it:
            it["blocked_by"] = ["@" + names[r[1:]] for r in it["blocked_by"]]
        for part in it.get("parts", []):
            part["scope"] = part["boundary"] = part["done_when"] = "reworded"
    out["items"].reverse()
    return out


@pytest.mark.parametrize("case", CASES)
def test_compare_shape_ignores_keys_titles_wording_and_file_order(
    tmp_path: Path, case: str
) -> None:
    r = compare(tmp_path, example(case), renamed(example(case)))
    assert r.returncode == 0, r.stdout + r.stderr


def mutate(case: str, change: Any) -> dict[str, Any]:
    data = copy.deepcopy(example(case))
    change(data["items"])
    return data


def _set(obj: dict[str, Any], key: str, value: Any) -> None:
    obj[key] = value


def _repoint(items: list[dict[str, Any]]) -> None:
    api, ui = items
    del ui["parent"], ui["blocked_by"]
    api["parent"] = "@" + ui["key"]


@pytest.mark.parametrize(
    "case,change,needle",
    [
        ("01-single", lambda i: i.append(dict(i[0], key="more")), "items: 1 != 2"),
        ("01-single", lambda i: _set(i[0], "repo", "larchanka-training/dmc-268-api-t6"), "repo"),
        ("01-single", lambda i: _set(i[0], "type", "Story"), "type: 'Task' != 'Story'"),
        ("02-two-prs", lambda i: i[0]["parts"].pop(), "parts (AC per part): [2, 2] != [2]"),
        (
            "02-two-prs",
            lambda i: i[0]["parts"][1]["acceptance_criteria"].pop(),
            "parts (AC per part): [2, 2] != [2, 1]",
        ),
        ("02-two-prs", lambda i: i[0]["kickoff"].pop("mines"), "kickoff mines: True != False"),
        ("02-two-prs", lambda i: i[0]["kickoff"].pop("read_first"), "kickoff read_first"),
        ("02-two-prs", lambda i: i[0].pop("parts"), "parts (AC per part): [2, 2] != []"),
        (
            "02-two-prs",
            lambda i: i[0]["parts"][0].pop("done_when"),
            "parts (done_when): [True, True] != [False, True]",
        ),
        ("03-ui-api", lambda i: i[1].pop("blocked_by"), "blocked_by: ['@1'] != []"),
        ("03-ui-api", lambda i: i[1].pop("parent"), "parent: '@1' != None"),
        ("03-ui-api", lambda i: _set(i[1], "repo", i[0]["repo"]), "item 2: repo"),
        # the edge re-pointed: the api issue becomes the ui issue's sub-issue instead
        ("03-ui-api", _repoint, "item 1: repo"),
    ],
)
def test_compare_shape_reports_each_difference(
    tmp_path: Path, case: str, change: Any, needle: str
) -> None:
    r = compare(tmp_path, example(case), mutate(case, change))
    assert r.returncode == 1, r.stdout + r.stderr
    result = json.loads(r.stdout)
    assert result["same_shape"] is False
    assert any(needle in d for d in result["differences"]), result["differences"]


def test_compare_shape_refuses_a_file_that_is_not_a_batch(tmp_path: Path) -> None:
    r = compare(tmp_path, example("01-single"), {"items": "x"})
    assert r.returncode == 2
    assert "candidate" in json.loads(r.stderr)["error"]


def test_a_status_together_with_no_board_is_refused_before_anything_runs(tmp_path: Path) -> None:
    """issue-create.sh would refuse it for that item only, after the earlier items
    were created; refused here, the batch does not stop halfway."""
    r, calls = run(tmp_path, batch(item("a"), item("b", status="Todo")), "--no-board")
    assert r.returncode == 2
    assert 'item "b" sets "status"' in error(r)
    assert calls == []
