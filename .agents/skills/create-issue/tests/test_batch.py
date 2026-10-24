"""Contract tests for scripts/issue-batch.py and the golden examples.

Offline — no network, nothing is ever created. Two seams:

* ISSUE_BATCH_SCRIPTS_DIR points the driver at tests/fake_issue_scripts.py, copied
  as issue-create.sh and issue-link.sh: it records each call (argv, the spec it was
  given, the state file at that moment) and answers from a config, so order, `@key`
  resolution, state writes, resume, adoption and exit aggregation are asserted;
* the examples run the REAL scripts with the ISSUE_FIXTURE_DIR seam: the preview in
  `examples/<case>/preview.md` is what `--dry-run` prints, byte for byte.

What carries weight: nothing is created before the whole batch is valid, locally and
remotely; parents and blockers are created before what depends on them; the start and
every result are in the state file before the next create, so a rerun never creates an
item twice — an item already recorded only gets its links ensured, and an exact-title
duplicate is adopted only on a rerun, only open, yours and created since the start.
"""
# SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/create-issue/tests/test_batch.py

from __future__ import annotations

import copy
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
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


# The start an earlier run recorded; fake_issue_scripts.DUP_CREATED is 5 minutes later.
STARTED = "2026-10-24T12:00:00Z"


def entry(
    repo: str, num: int, key: str, board: str | None = "added", links: Any = None
) -> dict[str, Any]:
    """One state-file entry, for the item `key` (whose summary is "[x] key")."""
    return {
        "url": f"https://github.com/{repo}/issues/{num}",
        "number": num,
        "repo": repo,
        "summary": f"[x] {key}",
        "board": board,
        "links": links,
    }


def state_with(a: Any) -> str:
    """A state file whose only entry, for "a", is `a`."""
    return json.dumps({"started_at": STARTED, "items": {"a": a}})


def linked(ref: str) -> dict[str, Any]:
    """A link the stand-in issue-create.sh set."""
    return {"ref": ref, "ok": True, "action": "linked"}


def seed(tmp_path: Path, items: dict[str, Any], bid: str = "b1") -> None:
    """The state an earlier run left: it got as far as its first create."""
    path = state_path(tmp_path, bid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"started_at": STARTED, "items": items}))


def recorded(tmp_path: Path, bid: str = "b1") -> dict[str, Any]:
    parsed: dict[str, Any] = state(tmp_path, bid)["items"]
    return parsed


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
    a = entry(API, 1, "a")
    b = entry(API, 2, "b", links={"parent": linked("acme/api#1"), "blocked_by": []})
    seen = [c["state"] for c in creates(calls)]
    started = seen[0]["started_at"]
    assert [s["items"] for s in seen] == [{}, {"a": a}, {"a": a, "b": b}]
    assert [s["started_at"] for s in seen] == [started] * 3
    assert state(tmp_path) == {
        "started_at": started,
        "items": {"a": a, "b": b, "c": entry(API, 103, "c")},
    }
    assert out(r)["state_file"] == str(state_path(tmp_path))
    assert out(r)["id"] == "b1"


def test_the_start_is_on_disk_before_the_first_create(tmp_path: Path) -> None:
    """A rerun adopts only an issue created at or after it: written after the create,
    the issue it is there to recognise would predate it."""
    before = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    r, calls = run(tmp_path, batch(item("a")))
    after = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    assert r.returncode == 0, r.stderr
    started = creates(calls)[0]["state"]["started_at"]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", started)
    assert before <= started <= after
    assert creates(calls)[0]["state"]["items"] == {}


@pytest.mark.parametrize("xdg", [None, ""])
def test_without_xdg_cache_home_the_state_lives_under_home(tmp_path: Path, xdg: str | None) -> None:
    r, _ = run(tmp_path, batch(item("a")), env={"XDG_CACHE_HOME": xdg})
    assert r.returncode == 0, r.stderr
    path = tmp_path / "home" / ".cache" / "create-issue" / "batches" / "b1.json"
    assert json.loads(path.read_text())["items"] == {"a": entry(API, 101, "a")}
    assert out(r)["state_file"] == str(path)
    assert not (tmp_path / "cache").exists()


def test_the_state_file_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    r, _ = run(tmp_path, batch(item("a"), item("b")))
    assert r.returncode == 0, r.stderr
    assert sorted(p.name for p in state_path(tmp_path).parent.iterdir()) == ["b1.json"]


def load_driver() -> Any:
    """issue-batch.py as a module (its name has a hyphen, so no plain import)."""
    spec = importlib.util.spec_from_file_location("issue_batch", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReplaceFails:
    """The `os` module, except that the rename which publishes the new file fails."""

    def __getattr__(self, name: str) -> Any:
        return getattr(os, name)

    @staticmethod
    def replace(src: str, dst: object) -> None:
        raise OSError(f"disk full while replacing with {src}")


def test_a_failed_write_keeps_the_previous_state_file_and_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Atomic for real: the new content went to a temp file, the rename failed — the
    old file is untouched and the temp file is gone."""
    driver = load_driver()
    path = tmp_path / "b1.json"
    old = json.dumps({"started_at": STARTED, "items": {"a": entry(API, 1, "a")}})
    path.write_text(old)
    monkeypatch.setattr(driver, "os", ReplaceFails())
    with pytest.raises(OSError, match=r"disk full while replacing with .*/\.b1\.json\..*\.tmp"):
        driver.save_state(path, {"started_at": STARTED, "items": {}})
    assert path.read_text() == old
    assert sorted(p.name for p in tmp_path.iterdir()) == ["b1.json"]


@pytest.mark.parametrize(
    "content,needle",
    [
        ("{broken", "is not valid JSON"),
        ('["a"]', 'is not {"started_at", "items": {...}}'),
        # the format before started_at: entries at the top level
        (json.dumps({"a": entry(API, 1, "a")}), 'is not {"started_at", "items": {...}}'),
        (json.dumps({"started_at": STARTED, "items": []}), 'is not {"started_at"'),
        (json.dumps({"started_at": STARTED}), 'is not {"started_at"'),
        (json.dumps({"started_at": "yesterday", "items": {}}), '"started_at" is not an ISO 8601'),
        (json.dumps({"started_at": "2026-10-24T12:00:00", "items": {}}), '"started_at" is not'),
        (json.dumps({"started_at": None, "items": {}}), '"started_at" is not an ISO 8601'),
        (state_with({"url": "u"}), 'entry "a" needs'),
        (
            state_with({k: v for k, v in entry(API, 1, "a").items() if k != "summary"}),
            'entry "a" needs "url", "number", "repo" and "summary"',
        ),
        (state_with(dict(entry(API, 1, "a"), number="1")), 'entry "a"'),
        # CONTRACT of is_record: a JSON true is not an issue number
        (state_with(dict(entry(API, 1, "a"), number=True)), 'entry "a"'),
        (state_with(dict(entry(API, 1, "a"), board=6)), 'entry "a"'),
        (state_with(dict(entry(API, 1, "a"), links=[])), 'entry "a"'),
    ],
    ids=[
        "broken-json", "list", "old-format", "items-list", "no-items", "bad-time", "no-zone",
        "null-time", "entry-url-only", "entry-no-summary", "number-str", "number-bool",
        "board-int", "links-list",
    ],
)  # fmt: skip
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
    seed(tmp_path, {"a": entry(UI, 5, "a")})
    r, calls = run(tmp_path, batch(item("a")))
    assert r.returncode == 2
    assert error(r) == (
        f'item "a" is recorded as acme/ui#5 in {state_path(tmp_path)}, but the batch puts it '
        "in acme/api — fix the batch or remove the entry (references/batch.md § 5)"
    )
    assert calls == []


@pytest.mark.parametrize("flags", [(), ("--dry-run",)])
def test_a_state_entry_with_another_title_than_the_item_stops_the_run(
    tmp_path: Path, flags: tuple[str, ...]
) -> None:
    """The key alone does not identify the issue: a renamed item may be another task."""
    seed(tmp_path, {"a": dict(entry(API, 5, "a"), summary="[x] old title")})
    r, calls = run(tmp_path, batch(item("a")), *flags)
    assert r.returncode == 2
    assert error(r) == (
        f'item "a" is recorded as acme/api#5 in {state_path(tmp_path)} with the title '
        "'[x] old title', but the batch gives it the title '[x] a' — fix the batch or remove "
        "the entry (references/batch.md § 5)"
    )
    assert calls == []


def test_a_recorded_title_matches_without_surrounding_blanks(tmp_path: Path) -> None:
    seed(tmp_path, {"a": dict(entry(API, 5, "a"), summary=" [x] a ")})
    r, calls = run(tmp_path, batch(item("a")))
    assert r.returncode == 0, r.stderr
    assert creates(calls) == []


# ─── rerun: resume, never create twice ──────────────────────────────────────
def test_a_rerun_after_a_failure_creates_nothing_twice_and_ensures_links(
    tmp_path: Path,
) -> None:
    data = batch(item("a", blocked_by=["#7"]), item("b", parent="@a"), item("c", blocked_by=["@b"]))
    first, calls = run(tmp_path, data, cfg=cfg_create(a={"number": 1}, b={"code": 5}))
    assert first.returncode == 5
    assert created_keys(calls) == ["a", "b"]
    a = entry(API, 1, "a", links={"parent": None, "blocked_by": [linked("#7")]})
    assert recorded(tmp_path) == {"a": a}
    assert 'item "b"' in error(first)
    assert [i["key"] for i in out(first)["items"]] == ["a"], "what was done is still reported"
    started = state(tmp_path)["started_at"]

    again, calls = run(tmp_path, data, cfg=cfg_create(b={"number": 2}, c={"number": 3}))
    assert again.returncode == 0, again.stderr
    assert created_keys(calls) == ["b", "c"]
    # a rerun looks for an issue an earlier run opened and did not record — from two
    # minutes before the start: GitHub's clock may run behind this machine's
    since = datetime.strptime(started, "%Y-%m-%dT%H:%M:%SZ") - timedelta(seconds=120)
    since_flag = ["--adopt-since", since.strftime("%Y-%m-%dT%H:%M:%SZ")]
    assert [c["argv"][1:] for c in creates(calls)] == [since_flag] * 2
    assert links(calls) == [["acme/api#1", "--blocked-by", "#7"]]
    assert creates(calls)[0]["spec"]["parent"] == "acme/api#1"
    assert creates(calls)[1]["spec"]["blocked_by"] == ["acme/api#2"]
    items = out(again)["items"]
    ensured = {"parent": None, "blocked_by": [{"ref": "#7", "ok": True, "action": "exists"}]}
    assert items[0] == {
        "key": "a",
        "url": "https://github.com/acme/api/issues/1",
        "number": 1,
        "repo": API,
        "created": False,
        "adopted": False,
        "verified": None,
        "board": "added",
        "links": ensured,
    }
    assert [i["created"] for i in items] == [False, True, True]
    assert state(tmp_path)["started_at"] == started, "the start of the batch, not of this run"
    assert recorded(tmp_path)["a"] == dict(a, links=ensured)
    assert set(recorded(tmp_path)) == {"a", "b", "c"}


def test_a_rerun_records_the_links_it_ensured(tmp_path: Path) -> None:
    failed = {"parent": None, "blocked_by": [{"ref": "#7", "ok": False, "action": None}]}
    seed(tmp_path, {"a": entry(API, 1, "a", links=failed)})
    r, _ = run(tmp_path, batch(item("a", blocked_by=["#7"])))
    assert r.returncode == 0, r.stderr
    assert recorded(tmp_path)["a"]["links"] == {
        "parent": None,
        "blocked_by": [{"ref": "#7", "ok": True, "action": "exists"}],
    }


def test_a_recorded_item_gets_its_local_refs_from_the_state(tmp_path: Path) -> None:
    seed(tmp_path, {"p": entry(API, 4, "p"), "c": entry(UI, 9, "c")})
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
    # nothing left to create: no validation either, only the links are ensured
    assert [c["script"] for c in calls] == ["link"]
    assert links(calls) == [["acme/api#102", "--parent", "acme/api#101"]]
    assert [i["created"] for i in out(again)["items"]] == [False, False]


def test_state_entries_of_other_keys_are_kept(tmp_path: Path) -> None:
    seed(tmp_path, {"gone": entry(API, 3, "gone")})
    r, _ = run(tmp_path, batch(item("a")))
    assert r.returncode == 0, r.stderr
    assert recorded(tmp_path) == {"gone": entry(API, 3, "gone"), "a": entry(API, 101, "a")}


def test_a_rerun_validates_only_the_items_still_to_create_and_first(tmp_path: Path) -> None:
    seed(tmp_path, {"a": entry(API, 1, "a")})
    r, calls = run(tmp_path, batch(item("a", blocked_by=["#7"]), item("b", parent="@a")))
    assert r.returncode == 0, r.stderr
    assert [c["script"] for c in calls] == ["create", "link", "create"]
    assert calls[0]["argv"][1:] == ["--dry-run"]
    assert calls[0]["spec"]["summary"] == "[x] b"
    assert "parent" not in calls[0]["spec"], "local refs are left out of the validation"


@pytest.mark.parametrize(
    "started", ["2026-10-24T12:00:00Z", "2026-10-24T14:00:00+02:00"], ids=["utc", "offset"]
)
def test_a_rerun_looks_for_its_own_issue_from_two_minutes_before_the_start(
    tmp_path: Path, started: str
) -> None:
    """started_at is this machine's clock, createdAt GitHub's: a clock up to 120 s ahead
    must not hide the issue an earlier run opened. The flag is UTC whatever the zone
    of a hand-edited started_at; the state keeps the start itself."""
    path = state_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"started_at": started, "items": {}}))
    r, calls = run(tmp_path, batch(item("a")))
    assert r.returncode == 0, r.stderr
    assert creates(calls)[0]["argv"][1:] == ["--adopt-since", "2026-10-24T11:58:00Z"]
    assert state(tmp_path)["started_at"] == started


# ─── an exact-title duplicate: adopted on a rerun only, and only the batch's own ─
def test_a_rerun_adopts_an_open_duplicate_you_opened_since_the_batch_started(
    tmp_path: Path,
) -> None:
    seed(tmp_path, {"a": entry(API, 1, "a")})
    data = batch(item("a"), item("b", parent="@a"), item("c", blocked_by=["@b"]))
    cfg = cfg_create(b={"code": 4, "duplicate": {"number": 55}}, c={"number": 3})
    r, calls = run(tmp_path, data, cfg=cfg)
    assert r.returncode == 0, r.stderr
    ensured = {"parent": {"ref": "acme/api#1", "ok": True, "action": "exists"}, "blocked_by": []}
    assert recorded(tmp_path)["b"] == entry(API, 55, "b", board="unchecked", links=ensured)
    assert links(calls) == [["acme/api#55", "--parent", "acme/api#1"]]
    assert creates(calls)[1]["spec"]["blocked_by"] == ["acme/api#55"]
    assert out(r)["items"][1] == {
        "key": "b",
        "url": "https://github.com/acme/api/issues/55",
        "number": 55,
        "repo": API,
        "created": False,
        "adopted": True,
        "verified": None,
        "board": "unchecked",
        "links": ensured,
    }
    assert "already exists" not in r.stderr, "the guard's exit-4 error is not passed on"


@pytest.mark.parametrize(
    "dup",
    [
        {"number": 55, "createdAt": STARTED},  # at the start: at or after it
        # 120 s before the start: the margin for this machine's clock running ahead
        {"number": 55, "createdAt": "2026-10-24T11:58:00Z"},
        {"number": 55, "author": "OctoCat"},  # logins compare without case
        {"number": 55, "state": "open"},  # the REST listing's spelling
    ],
    ids=["created-at-the-start", "created-at-the-clock-skew-margin", "login-case", "state-case"],
)
def test_the_adoption_rule_at_its_edges(tmp_path: Path, dup: dict[str, Any]) -> None:
    """The same rule in the validation before the first create and at the create."""
    seed(tmp_path, {})
    cfg = cfg_create(a={"dry_duplicate": dup, "code": 4, "duplicate": dup})
    r, _ = run(tmp_path, batch(item("a")), cfg=cfg)
    assert r.returncode == 0, r.stderr
    assert out(r)["items"][0]["adopted"] is True


OWN_HINT = "if it is this batch's own issue, add it to the state file"


@pytest.mark.parametrize(
    "dup,why,own",
    [
        ({"number": 55, "state": "CLOSED"}, "it is closed", True),
        (
            {"number": 55, "author": "teammate"},
            "it was opened by teammate, not by you (octocat)",
            False,
        ),
        ({"number": 55, "author": None}, "its author or yours is unknown", False),
        # one second past the 120 s allowed for clock skew
        (
            {"number": 55, "createdAt": "2026-10-24T11:57:59Z"},
            f"it was created more than 120 s before this batch started ({STARTED})",
            True,
        ),
        (
            {"number": 55, "createdAt": None},
            "it was created more than 120 s before this batch started",
            True,
        ),
    ],
    ids=["closed", "teammate", "no-author", "older", "no-created-at"],
)
def test_a_rerun_stops_at_a_duplicate_it_may_not_adopt(
    tmp_path: Path, dup: dict[str, Any], why: str, own: bool
) -> None:
    """Yours on a rerun, it may be the batch's own all the same — closed since, or
    created while this machine's clock ran further ahead: the stop says how to record it."""
    seed(tmp_path, {"a": entry(API, 1, "a")})
    data = batch(item("a"), item("b"), item("c"))
    r, calls = run(tmp_path, data, cfg=cfg_create(b={"code": 4, "duplicate": dup}))
    assert r.returncode == 4
    assert created_keys(calls) == ["b"], "c never runs"
    assert recorded(tmp_path) == {"a": entry(API, 1, "a")}
    assert f"; not adopted: {why}" in error(r)
    assert 'item "b" stopped the batch: an issue with exactly this title already exists: ' in error(
        r
    )
    assert (OWN_HINT in error(r)) is own


@pytest.mark.parametrize(
    "dup,tail",
    [
        (
            {"number": 55, "state": "CLOSED"},
            (
                "not adopted: it is closed — what was created so far is recorded in {path}; if "
                "it is this batch's own issue, add it to the state file (`items.a`: url, number, "
                "repo, summary) and rerun; or give the item another title or settle that issue, "
                "then rerun the same command (recorded items are not created again)"
            ),
        ),
        (
            {"number": 55, "state": "CLOSED", "author": "teammate"},
            (
                "not adopted: it is closed — what was created so far is recorded in {path}; fix "
                "the cause and rerun the same command (recorded items are not created again)"
            ),
        ),
    ],
    ids=["yours", "teammates"],
)
def test_the_refused_duplicate_is_named_with_its_state_and_author(
    tmp_path: Path, dup: dict[str, Any], tail: str
) -> None:
    seed(tmp_path, {})
    r, _ = run(tmp_path, batch(item("a")), cfg=cfg_create(a={"code": 4, "duplicate": dup}))
    assert r.returncode == 4
    assert error(r) == (
        'item "a" stopped the batch: an issue with exactly this title already exists: '
        f"https://github.com/acme/api/issues/55 (CLOSED, by {dup.get('author', 'octocat')}, "
        "created 2026-10-24T12:05:00Z); " + tail.format(path=state_path(tmp_path))
    )


def test_the_validation_says_how_to_record_a_refused_issue_that_may_be_the_batchs_own(
    tmp_path: Path,
) -> None:
    """Open and yours, but one second older than the clock-skew margin allows: the way to
    record it comes first, renaming the item second."""
    seed(tmp_path, {"a": entry(API, 1, "a")})
    dup = {"number": 55, "createdAt": "2026-10-24T11:57:59Z"}
    r, calls = run(tmp_path, batch(item("a"), item("b")), cfg=cfg_create(b={"dry_duplicate": dup}))
    assert r.returncode == 4
    assert creates(calls) == [] and links(calls) == [], "nothing runs after the validation"
    assert error(r) == (
        'item "b": an issue with exactly this title already exists: '
        "https://github.com/acme/api/issues/55 (OPEN, by octocat, created "
        "2026-10-24T11:57:59Z); not adopted: it was created more than 120 s before this batch "
        f"started ({STARTED}) — nothing was created; if it is this batch's own issue, add it "
        f"to the state file {state_path(tmp_path)} (`items.b`: url, number, repo, summary) "
        "and rerun; or give the item another title or settle that issue, then rerun"
    )


def test_a_first_run_offers_no_issue_for_the_state_file(tmp_path: Path) -> None:
    """Nothing can be the batch's own yet — and the state file does not exist."""
    cfg = cfg_create(a={"dry_duplicate": {"number": 55}})
    r, _ = run(tmp_path, batch(item("a")), cfg=cfg)
    assert r.returncode == 4
    assert error(r).endswith(
        "did not create it — nothing was created; give the item another title or settle that "
        "issue, then rerun"
    )
    assert not state_path(tmp_path).exists()


def test_a_first_run_never_adopts_a_duplicate(tmp_path: Path) -> None:
    """Even one that is open, yours and newer than the start: on a first run no issue
    can be the batch's own yet."""
    data = batch(item("a"), item("b", parent="@a"), item("c"))
    late = {"number": 55, "createdAt": "2099-01-01T00:00:00Z"}
    r, calls = run(
        tmp_path, data, cfg=cfg_create(a={"number": 1}, b={"code": 4, "duplicate": late})
    )
    assert r.returncode == 4
    assert created_keys(calls) == ["a", "b"]
    assert all("--adopt-since" not in c["argv"] for c in creates(calls))
    assert recorded(tmp_path) == {"a": entry(API, 1, "a")}
    assert (
        "; not adopted: this is the first run of the batch, so the batch did not create it"
        in error(r)
    )
    assert OWN_HINT not in error(r), "yours, but on a first run never the batch's own"
    assert links(calls) == []


def test_a_duplicate_found_by_the_validation_stops_a_first_run_before_any_create(
    tmp_path: Path,
) -> None:
    dup = {"number": 8, "author": "teammate", "state": "CLOSED"}
    r, calls = run(tmp_path, batch(item("a"), item("b")), cfg=cfg_create(b={"dry_duplicate": dup}))
    assert r.returncode == 4
    assert creates(calls) == []
    assert len(calls) == 2, "both items validated, nothing created"
    assert not state_path(tmp_path).exists()
    assert r.stdout == ""
    assert error(r) == (
        'item "b": an issue with exactly this title already exists: '
        "https://github.com/acme/api/issues/8 (CLOSED, by teammate, created "
        "2026-10-24T12:05:00Z); not adopted: this is the first run of the batch, so the batch "
        "did not create it — nothing was created; give the item another title or settle that "
        "issue, then rerun"
    )


def test_on_a_rerun_the_validation_lets_an_adoptable_duplicate_through(tmp_path: Path) -> None:
    seed(tmp_path, {"a": entry(API, 1, "a")})
    dup = {"number": 55}
    cfg = cfg_create(b={"dry_duplicate": dup, "code": 4, "duplicate": dup})
    r, _ = run(tmp_path, batch(item("a"), item("b")), cfg=cfg)
    assert r.returncode == 0, r.stderr
    assert out(r)["items"][1]["adopted"] is True


def test_on_a_rerun_the_validation_stops_at_a_teammates_duplicate_first(tmp_path: Path) -> None:
    seed(tmp_path, {"a": entry(API, 1, "a")})
    cfg = cfg_create(b={"dry_duplicate": {"number": 8, "author": "teammate"}})
    r, calls = run(tmp_path, batch(item("a", blocked_by=["#7"]), item("b"), item("c")), cfg=cfg)
    assert r.returncode == 4
    assert creates(calls) == [] and links(calls) == [], "the validation comes before anything"
    assert "not adopted: it was opened by teammate, not by you (octocat)" in error(r)


def test_an_adopted_item_without_links_is_only_recorded(tmp_path: Path) -> None:
    seed(tmp_path, {})
    r, calls = run(
        tmp_path, batch(item("a")), cfg=cfg_create(a={"code": 4, "duplicate": {"number": 8}})
    )
    assert r.returncode == 0, r.stderr
    assert links(calls) == []
    assert recorded(tmp_path) == {"a": entry(API, 8, "a", board="unchecked")}
    assert out(r)["items"][0]["links"] is None


def test_an_adopted_item_whose_link_fails_is_exit_7(tmp_path: Path) -> None:
    seed(tmp_path, {"a": entry(API, 1, "a")})
    cfg = cfg_create(b={"code": 4, "duplicate": {"number": 55}})
    cfg["link"] = {"acme/api#55": {"code": 7}}
    r, _ = run(tmp_path, batch(item("a"), item("b", parent="@a")), cfg=cfg)
    assert r.returncode == 7
    failed = {"parent": {"ref": "acme/api#1", "ok": False, "action": None}, "blocked_by": []}
    assert recorded(tmp_path)["b"] == entry(API, 55, "b", board="unchecked", links=failed)


@pytest.mark.parametrize("code", [2, 3, 5])
def test_an_adopted_item_whose_link_step_cannot_run_stops_the_batch(
    tmp_path: Path, code: int
) -> None:
    """Recorded before its links, so the rerun finds it in the state."""
    seed(tmp_path, {"a": entry(API, 1, "a")})
    data = batch(item("a"), item("b", parent="@a"), item("c"))
    cfg = cfg_create(b={"code": 4, "duplicate": {"number": 55}})
    cfg["link"] = {"acme/api#55": {"code": code}}
    r, calls = run(tmp_path, data, cfg=cfg)
    assert r.returncode == code
    assert created_keys(calls) == ["b"], "c never runs"
    assert recorded(tmp_path)["b"] == entry(API, 55, "b", board="unchecked")
    assert (
        f'item "b" stopped the batch: issue-link.sh could not ensure its links (exit {code})'
        in error(r)
    )
    assert [i["key"] for i in out(r)["items"]] == ["a"]


# ─── stderr: an adoption is one note, any other outcome passes through as is ─
ADOPTED = "adopted {url} — exact title, opened by you during this batch"


def stderr_lines(r: subprocess.CompletedProcess[str]) -> list[Any]:
    """stderr as JSON lines, after the scripts-dir seam's warning (the first line)."""
    return [json.loads(line) for line in r.stderr.splitlines()[1:]]


def test_an_adopted_item_says_so_in_one_note_instead_of_the_guards_error(
    tmp_path: Path,
) -> None:
    """The guard's exit-4 error is how an adoption arrives, not a failure: a reader of
    stderr must not take a batch that succeeded for one that failed."""
    seed(tmp_path, {"a": entry(API, 1, "a")})
    cfg = cfg_create(b={"code": 4, "duplicate": {"number": 55}})
    r, _ = run(tmp_path, batch(item("a"), item("b", parent="@a")), cfg=cfg)
    assert r.returncode == 0, r.stderr
    note = ADOPTED.format(url="https://github.com/acme/api/issues/55")
    assert stderr_lines(r) == [{"note": note}]


def test_a_duplicate_that_is_not_adopted_passes_the_guards_error_on_unchanged(
    tmp_path: Path,
) -> None:
    seed(tmp_path, {})
    dup = {"number": 55, "state": "CLOSED"}
    r, _ = run(tmp_path, batch(item("a")), cfg=cfg_create(a={"code": 4, "duplicate": dup}))
    assert r.returncode == 4
    guard, stop = stderr_lines(r)
    assert guard == {
        "error": "an issue with exactly this title already exists in acme/api: "
        "https://github.com/acme/api/issues/55"
    }
    assert stop["error"].startswith('item "a" stopped the batch: ')


def test_a_created_items_stderr_passes_through_byte_for_byte(tmp_path: Path) -> None:
    """Warnings and repair commands of an item that is not adopted reach the user as the
    script wrote them, JSON or not."""
    said = '{"warning": "the duplicate search failed"}\nrepair: not JSON — as written\n'
    url = "https://github.com/acme/api/issues/9"
    created = json.dumps({"url": url, "number": 9, "repo": API, "verified": True, "board": "added"})
    own_create_stub(tmp_path, f"printf '%s' '{said}' >&2; echo '{created}'")
    r, _ = run(tmp_path, batch(item("a")))
    assert r.returncode == 0, r.stderr
    assert out(r)["items"][0]["created"] is True
    assert r.stderr.split("\n", 1)[1] == said


def test_the_real_guards_error_on_an_adoption_becomes_the_note(tmp_path: Path) -> None:
    """The live run's case end to end, every call answered by the ISSUE_FIXTURE_DIR seam:
    a lost record, so the real issue-create.sh finds the issue (--adopt-since) and exits 4
    with "… not created again"; the batch succeeds, so stderr is the note alone."""
    url = "https://github.com/acme/api/issues/7"
    row = {
        "number": 7,
        "title": "[x] a",
        "html_url": url,
        "state": "open",
        "user": {"login": "octocat"},
        "created_at": "2026-10-24T12:05:00Z",
    }
    fix = tmp_path / "fix"
    fix.mkdir()
    for name, val in {
        "reach.code": "0",
        "reach.http": "200",
        "auth_user.code": "0",
        "auth_user.json": '{"login": "octocat"}',
        "repo_perms.json": "true",
        "issue_search.json": "[]",
        "issue_recover_before.json": json.dumps([row]),
    }.items():
        (fix / name).write_text(val)
    bin_dir = tmp_path / "bin"  # a gh that only logs: nothing may go live
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(f"#!/bin/sh\necho \"$*\" >> '{tmp_path / 'gh.log'}'\nexit 99\n")
    (bin_dir / "gh").chmod(0o755)
    seed(tmp_path, {})
    path = tmp_path / "batch.json"
    path.write_text(json.dumps(batch(item("a"))), encoding="utf-8")
    r = subprocess.run(
        [sys.executable, str(SCRIPT), str(path), "--no-board"],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HOME": str(tmp_path / "home"),
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
            "ISSUE_FIXTURE_DIR": str(fix),
        },
        timeout=120,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    assert r.stderr == json.dumps({"note": ADOPTED.format(url=url)}, ensure_ascii=False) + "\n"
    assert out(r)["items"][0]["adopted"] is True
    log = fix / "calls.log"
    assert "issue_create" not in (log.read_text() if log.exists() else "")
    assert not (tmp_path / "gh.log").exists()


# ─── the preview of duplicates ──────────────────────────────────────────────
def test_the_preview_says_which_duplicate_a_rerun_adopts_and_which_stops_it(
    tmp_path: Path,
) -> None:
    seed(tmp_path, {"a": entry(API, 1, "a")})
    cfg = cfg_create(
        a={"dry_code": 2},  # recorded: its dry-run never runs
        b={"dry_duplicate": {"number": 55, "createdAt": "2026-10-24T11:58:00Z"}},
        c={"dry_duplicate": {"number": 8, "state": "CLOSED", "author": "teammate"}},
        d={"dry_duplicate": {"number": 9, "createdAt": "2026-10-24T11:57:59Z"}},
    )
    data = batch(item("a"), item("b"), item("c"), item("d"))
    r, calls = run(tmp_path, data, "--dry-run", cfg=cfg)
    assert r.returncode == 0, r.stderr
    assert [c["spec"]["summary"] for c in calls] == ["[x] b", "[x] c", "[x] d"]
    assert (
        "\n\nAlready created, only their links are ensured:\n"
        "- `a` — [x] a — https://github.com/acme/api/issues/1\n"
        "\nAn issue with exactly the same title exists:\n"
        "- `b`: would adopt #55 — https://github.com/acme/api/issues/55\n"
        "- `c`: would stop: duplicate #8 (CLOSED, teammate) — https://github.com/acme/api/issues/8\n"
        "- `d`: would stop: duplicate #9 (OPEN, octocat) — https://github.com/acme/api/issues/9\n"
        "\n# 2. `b` — [x] b\n"
    ) in r.stdout


def test_a_first_run_preview_says_a_duplicate_would_stop_it(tmp_path: Path) -> None:
    r, _ = run(
        tmp_path, batch(item("a")), "--dry-run", cfg=cfg_create(a={"dry_duplicate": {"number": 55}})
    )
    assert r.returncode == 0, r.stderr
    assert (
        "\nAn issue with exactly the same title exists:\n"
        "- `a`: would stop: duplicate #55 (OPEN, octocat) — https://github.com/acme/api/issues/55\n"
    ) in r.stdout
    assert not (tmp_path / "cache").exists()


# ─── exit codes: 6 and 7 continue, 2/3/4/5 stop ─────────────────────────────
@pytest.mark.parametrize("board_first", [True, False])
def test_board_and_link_failures_do_not_stop_the_batch(tmp_path: Path, board_first: bool) -> None:
    """7 wins over 6 whichever item failed first."""
    board, link = item("a"), item("b", blocked_by=["#7"])
    data = batch(*((board, link) if board_first else (link, board)), item("c"))
    r, calls = run(tmp_path, data, cfg=cfg_create(a={"code": 6}, b={"code": 7}))
    assert r.returncode == 7
    assert set(created_keys(calls)) == {"a", "b", "c"}
    assert set(recorded(tmp_path)) == {"a", "b", "c"}
    items = {i["key"]: i for i in out(r)["items"]}
    assert items["a"]["board"] == "failed"
    assert items["b"]["links"]["blocked_by"][0]["ok"] is False


def test_a_board_failure_alone_is_exit_6_and_recorded_for_the_rerun(tmp_path: Path) -> None:
    data = batch(item("a"), item("b"))
    r, calls = run(tmp_path, data, cfg=cfg_create(a={"code": 6}))
    assert r.returncode == 6
    assert created_keys(calls) == ["a", "b"]
    assert recorded(tmp_path)["a"]["board"] == "failed"
    again, calls = run(tmp_path, data)
    assert again.returncode == 6, "the rerun does not forget it"
    assert creates(calls) == []


def test_a_recorded_board_failure_is_carried_into_exit_6_with_the_repair(tmp_path: Path) -> None:
    seed(tmp_path, {"a": entry(API, 1, "a", board="failed")})
    r, calls = run(tmp_path, batch(item("a"), item("b")))
    assert r.returncode == 6
    assert created_keys(calls) == ["b"]
    assert out(r)["items"][0]["board"] == "failed"
    url = "https://github.com/acme/api/issues/1"
    assert {
        "warning": f'item "a": {url} is recorded with a failed board step — gh project item-add '
        f"12 --owner larchanka-training --url {url}, then set its Status on the board"
    } in [json.loads(line) for line in r.stderr.splitlines()]
    assert recorded(tmp_path)["a"]["board"] == "failed"


def test_a_carried_board_failure_loses_to_a_failed_link(tmp_path: Path) -> None:
    seed(tmp_path, {"a": entry(API, 1, "a", board="failed")})
    r, _ = run(
        tmp_path, batch(item("a"), item("b", blocked_by=["#7"])), cfg=cfg_create(b={"code": 7})
    )
    assert r.returncode == 7


@pytest.mark.parametrize("board", ["added", "skipped", "unchecked", None])
def test_other_recorded_board_results_do_not_change_the_exit(
    tmp_path: Path, board: str | None
) -> None:
    seed(tmp_path, {"a": entry(API, 1, "a", board=board)})
    r, _ = run(tmp_path, batch(item("a")))
    assert r.returncode == 0, r.stderr
    assert out(r)["items"][0]["board"] == board


def test_an_entry_without_board_and_links_is_read_as_unknown(tmp_path: Path) -> None:
    bare = {k: v for k, v in entry(API, 1, "a").items() if k not in ("board", "links")}
    seed(tmp_path, {"a": bare})
    r, _ = run(tmp_path, batch(item("a")))
    assert r.returncode == 0, r.stderr
    assert (out(r)["items"][0]["board"], out(r)["items"][0]["links"]) == (None, None)


def test_a_failed_link_on_a_recorded_item_is_exit_7(tmp_path: Path) -> None:
    seed(tmp_path, {"a": entry(API, 1, "a")})
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
    seed(tmp_path, {"a": entry(API, 1, "a")})
    r, calls = run(
        tmp_path,
        batch(item("a", blocked_by=["#7"]), item("b")),
        cfg={"link": {"acme/api#1": {"code": code}}},
    )
    assert r.returncode == code
    assert creates(calls) == []
    assert 'item "a"' in error(r)


UNKNOWN = (
    "the issue may exist: check the newest issues of acme/api first — a rerun adopts an open "
    "issue with this title that you opened since the batch started"
)


@pytest.mark.parametrize("code", [2, 3, 4, 5])
def test_a_failed_create_stops_the_batch_and_keeps_what_was_created(
    tmp_path: Path, code: int
) -> None:
    data = batch(item("a"), item("b"), item("c"))
    r, calls = run(tmp_path, data, cfg=cfg_create(a={"number": 1}, b={"code": code}))
    assert r.returncode == code
    assert created_keys(calls) == ["a", "b"], "c never runs"
    assert recorded(tmp_path) == {"a": entry(API, 1, "a")}
    assert [i["key"] for i in out(r)["items"]] == ["a"]
    assert 'item "b"' in error(r)
    assert "rerun" in error(r)
    # only a 5 can mean the create landed after all
    assert (UNKNOWN in error(r)) is (code == 5)


def test_an_unexpected_exit_code_of_an_item_stops_the_batch_as_5(tmp_path: Path) -> None:
    r, calls = run(tmp_path, batch(item("a"), item("b")), cfg=cfg_create(a={"code": 1}))
    assert r.returncode == 5
    assert created_keys(calls) == ["a"]
    assert "exit 1" in error(r)
    assert UNKNOWN in error(r)


def own_create_stub(tmp_path: Path, real_create: str) -> None:
    """issue-create.sh as a shell script: a valid --dry-run, then `real_create`."""
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "issue-create.sh").write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        """  *--dry-run*) echo '{"dry_run": true, "body": "b", "duplicate": null}' ;;\n"""
        f"  *) {real_create} ;;\n"
        "esac\n"
    )
    (stubs / "issue-create.sh").chmod(0o755)
    shutil.copy(FAKE, stubs / "issue-link.sh")


def test_a_create_that_exits_0_without_a_url_stops_with_5(tmp_path: Path) -> None:
    own_create_stub(tmp_path, "echo '{}'")
    r, _ = run(tmp_path, batch(item("a"), item("b")))
    assert r.returncode == 5
    assert f"issue-create.sh exited 0 but printed no url; {UNKNOWN}" in error(r)
    assert recorded(tmp_path) == {}


def test_a_created_issue_that_cannot_be_recorded_stops_with_5(tmp_path: Path) -> None:
    """The issue exists but the state cannot say so: the rerun's --adopt-since finds it."""
    batches = state_path(tmp_path).parent
    url = "https://github.com/acme/api/issues/9"
    created = json.dumps({"url": url, "number": 9, "repo": API, "verified": True, "board": "added"})
    own_create_stub(tmp_path, f"chmod a-w '{batches}'; echo '{created}'")
    try:
        r, _ = run(tmp_path, batch(item("a"), item("b")))
    finally:
        batches.chmod(0o755)
    assert r.returncode == 5
    assert f"{url} exists but could not be recorded (" in error(r)
    assert error(r).endswith(
        f"; {UNKNOWN} — what was created so far is recorded in {state_path(tmp_path)}; fix the "
        "cause and rerun the same command (recorded items are not created again)"
    )
    assert recorded(tmp_path) == {}


def test_a_state_file_that_cannot_be_started_creates_nothing(tmp_path: Path) -> None:
    (tmp_path / "cache").write_text("not a directory")
    r, calls = run(tmp_path, batch(item("a")))
    assert r.returncode == 5
    assert creates(calls) == []
    assert "— nothing was created" in error(r)


def test_a_real_run_validates_every_item_before_the_first_create(tmp_path: Path) -> None:
    r, calls = run(
        tmp_path, batch(item("a"), item("b"), item("c")), cfg=cfg_create(c={"dry_code": 2})
    )
    assert r.returncode == 2
    assert creates(calls) == []
    assert [c["argv"][1:] for c in calls] == [["--dry-run"]] * 3
    assert not state_path(tmp_path).exists()
    assert r.stdout == ""
    assert error(r) == (
        'item "c": the validation before the first create (nothing created) failed '
        "(issue-create.sh --dry-run, exit 2): stub issue-create.sh --dry-run failed with exit 2"
    )


def test_the_scripts_dir_seam_is_announced(tmp_path: Path) -> None:
    r, _ = run(tmp_path, batch(item("a")))
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stderr.splitlines()[0]) == {
        "warning": f"ISSUE_BATCH_SCRIPTS_DIR is set: the item scripts run from "
        f"{tmp_path / 'stubs'}, not {SKILL / 'scripts'}"
    }


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
    """Validated remotely are the items the run would validate — those still to create;
    a recorded item keeps its row, its body is the issue at its url."""
    seed(tmp_path, {"a": entry(API, 1, "a")})
    cfg = cfg_create(a={"dry_code": 2})  # would fail the preview if it ran
    r, calls = run(tmp_path, batch(item("a"), item("b", parent="@a")), "--dry-run", cfg=cfg)
    assert r.returncode == 0, r.stderr
    assert [(c["argv"][1:], c["spec"]["summary"]) for c in calls] == [(["--dry-run"], "[x] b")]
    assert r.stdout == (
        "Batch `b1`: 2 issues, in creation order.\n"
        "\n"
        "| # | Key | Repo | Type | Title | Parent | Blocked by | Milestone | Status |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
        "| 1 | `a` | acme/api | Story | [x] a | — | — | — | — |\n"
        "| 2 | `b` | acme/api | Story | [x] b | @a | — | — | Todo |\n"
        "\n"
        "Already created, only their links are ensured:\n"
        "- `a` — [x] a — https://github.com/acme/api/issues/1\n"
        "\n"
        "# 2. `b` — [x] b\n"
        "\n"
        "body of [x] b\n"
    )


def test_the_preview_of_a_finished_batch_runs_no_dry_run(tmp_path: Path) -> None:
    seed(tmp_path, {"a": entry(API, 1, "a"), "b": entry(API, 2, "b")})
    cfg = cfg_create(a={"dry_code": 2}, b={"dry_code": 2})
    r, calls = run(tmp_path, batch(item("a"), item("b")), "--dry-run", cfg=cfg)
    assert r.returncode == 0, r.stderr
    assert calls == []
    assert r.stdout.endswith(
        "\n\nAlready created, only their links are ensured:\n"
        "- `a` — [x] a — https://github.com/acme/api/issues/1\n"
        "- `b` — [x] b — https://github.com/acme/api/issues/2\n"
    )


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
        "issue_search.json": "[]",  # no duplicate
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
    assert r.stderr == "", "no warning: neither the scripts-dir seam nor a failed search"


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
    """Other keys, titles, types and wording, another number of criteria per part,
    items in another file order: the shape is the same."""
    out = copy.deepcopy(data)
    out["id"] = "other-id"
    names = {it["key"]: f"k{n}" for n, it in enumerate(out["items"])}
    for it in out["items"]:
        it["key"] = names[it["key"]]
        it["summary"] = "[z] reworded"
        it["acceptance_criteria"] = ["reworded"] * len(it.get("acceptance_criteria", []))
        it["type"] = "Story" if it.get("type") == "Task" else "Task"
        if "parent" in it:
            it["parent"] = "@" + names[it["parent"][1:]]
        if "blocked_by" in it:
            it["blocked_by"] = ["@" + names[r[1:]] for r in it["blocked_by"]]
        for n, part in enumerate(it.get("parts", [])):
            part["scope"] = part["boundary"] = part["done_when"] = "reworded"
            part["acceptance_criteria"] = ["reworded"] * (n + 3)
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
        ("02-two-prs", lambda i: i[0]["parts"].pop(), "parts: 2 != 1"),
        (
            "02-two-prs",
            lambda i: _set(i[0]["parts"][1], "acceptance_criteria", []),
            "parts (has AC): [True, True] != [True, False]",
        ),
        (
            "02-two-prs",
            lambda i: i[0]["parts"][0].pop("acceptance_criteria"),
            "parts (has AC): [True, True] != [False, True]",
        ),
        ("02-two-prs", lambda i: i[0]["kickoff"].pop("mines"), "kickoff mines: True != False"),
        ("02-two-prs", lambda i: i[0]["kickoff"].pop("read_first"), "kickoff read_first"),
        ("02-two-prs", lambda i: i[0].pop("parts"), "parts: 2 != 0"),
        (
            "02-two-prs",
            lambda i: i[0]["parts"][0].pop("done_when"),
            "parts (has done_when): [True, True] != [False, True]",
        ),
        (
            "02-two-prs",
            lambda i: _set(i[0]["parts"][1], "done_when", " "),
            "parts (has done_when): [True, True] != [True, False]",
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


@pytest.mark.parametrize(
    "change",
    [
        lambda i: _set(i[0], "type", "Story"),
        lambda i: i[0]["parts"][0]["acceptance_criteria"].extend(["one more", "and another"]),
        lambda i: i[0]["parts"][1]["acceptance_criteria"].pop(),
    ],
    ids=["type", "more-criteria", "fewer-criteria"],
)
def test_compare_shape_ignores_the_type_and_the_number_of_criteria(
    tmp_path: Path, change: Any
) -> None:
    """The expected result is the number of parts, PR vs sub-issue and the repo; how
    many criteria a part has is the writer's call, as long as it has one."""
    r = compare(tmp_path, example("02-two-prs"), mutate("02-two-prs", change))
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads(r.stdout) == {"same_shape": True, "differences": []}


def test_compare_shape_refuses_a_file_that_is_not_a_batch(tmp_path: Path) -> None:
    r = compare(tmp_path, example("01-single"), {"items": "x"})
    assert r.returncode == 2
    assert "candidate" in json.loads(r.stderr)["error"]


@pytest.mark.parametrize(
    "parts,kind",
    [(5, "int"), ("x", "str"), ({}, "dict"), (None, "null")],
    ids=["number", "string", "object", "null"],
)
def test_compare_shape_refuses_parts_that_are_not_a_list(
    tmp_path: Path, parts: Any, kind: str
) -> None:
    """A string or an object would be counted by its characters or keys."""
    r = compare(tmp_path, example("01-single"), batch(item("a", parts=parts)))
    assert r.returncode == 2
    assert r.stdout == ""
    assert json.loads(r.stderr) == {
        "error": f'candidate: item "a": "parts" must be a list, got {kind}'
    }


def test_a_status_together_with_no_board_is_refused_before_anything_runs(tmp_path: Path) -> None:
    """issue-create.sh would refuse it for that item only, after the earlier items
    were created; refused here, the batch does not stop halfway."""
    r, calls = run(tmp_path, batch(item("a"), item("b", status="Todo")), "--no-board")
    assert r.returncode == 2
    assert 'item "b" sets "status"' in error(r)
    assert calls == []
