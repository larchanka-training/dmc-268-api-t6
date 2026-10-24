"""Contract tests for scripts/issue-link.sh.

Offline — no network, no tokens. Two seams, as for issue-create.sh:

* ISSUE_FIXTURE_DIR: every REST call reads a file named after its path, and every
  call is appended to calls.log, so a test sees which POSTs ran and with which id;
* the stand-in `gh` (tests/fake_gh.py) first on PATH: the script's real command
  lines, recorded — the endpoints, `-F`, and the internal ids in the POST bodies.

What carries weight: the endpoints take the internal `id`, never the number (the
fixtures make the two differ, so a number sent by mistake fails the assertion);
every link is decided by reading it back; a rerun on an issue that is already
linked posts nothing; a parent is never replaced; and anything that can be
refused is refused before the first POST.
"""
# SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/create-issue/tests/test_link.py

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "scripts" / "issue-link.sh"
FAKE_GH = HERE / "fake_gh.py"

CHILD = "acme/sandbox#13"
CHILD_PATH = "repos/acme/sandbox/issues/13"
BLOCKED_PATH = f"{CHILD_PATH}/dependencies/blocked_by"
SUB_ISSUES_5 = "repos/acme/sandbox/issues/5/sub_issues"


def api_url(ref: str) -> str:
    repo, num = ref.split("#")
    return f"https://api.github.com/repos/{repo}/issues/{num}"


def gh_issue(ref: str, parent: str | None = None, **extra: Any) -> dict[str, Any]:
    """A REST issue object. The internal id is 9_000_000 + the number: an id and a
    number never coincide, so a POST that sends the number is caught."""
    repo, num = ref.split("#")
    row: dict[str, Any] = {
        "id": 9_000_000 + int(num),
        "number": int(num),
        "title": f"issue {num}",
        "repository_url": f"https://api.github.com/repos/{repo}",
        "parent_issue_url": api_url(parent) if parent else None,
    }
    row.update(extra)
    return row


def issue_file(ref: str, parent: str | None = None, **extra: Any) -> dict[str, Any]:
    repo, num = ref.split("#")
    return {f"repos/{repo}/issues/{num}.json": gh_issue(ref, parent, **extra)}


# ─── fixture seam ───────────────────────────────────────────────────────────
def run(
    tmp_path: Path, *args: str, **files: Any
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """`files` maps a fixture path (relative to ISSUE_FIXTURE_DIR, `/` written as
    `__`) to its content; dicts and lists are written as JSON, None leaves the
    file out (a failed call)."""
    fix = tmp_path / "fix"
    fix.mkdir(exist_ok=True)
    for rel, val in files.items():
        if val is None:
            continue
        path = fix / rel.replace("__", "/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(val if isinstance(val, str) else json.dumps(val), encoding="utf-8")
    r = subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "ISSUE_FIXTURE_DIR": str(fix)},
        timeout=60,
    )
    log = fix / "calls.log"
    return r, log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def fx(*groups: dict[str, Any]) -> dict[str, Any]:
    """Merge fixture groups into run() keyword arguments (`/` → `__`)."""
    out: dict[str, Any] = {}
    for group in groups:
        out.update({k.replace("/", "__"): v for k, v in group.items()})
    return out


def world(**extra: Any) -> dict[str, Any]:
    """The child acme/sandbox#13 with no links yet, the parent acme/sandbox#5 and the
    blocker other/lib#7, all existing; the read-backs show both links in place."""
    return fx(
        issue_file(CHILD),
        issue_file("acme/sandbox#5"),
        issue_file("other/lib#7"),
        {f"{BLOCKED_PATH}.json": []},
        {f"{CHILD_PATH}.after.json": gh_issue(CHILD, parent="acme/sandbox#5")},
        {f"{BLOCKED_PATH}.after.json": [gh_issue("other/lib#7")]},
        extra,
    )


LINK_BOTH = ("--parent", "acme/sandbox#5", "--blocked-by", "other/lib#7")


def out(r: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(r.stdout)
    return parsed


def error(r: subprocess.CompletedProcess[str]) -> str:
    return str(json.loads(r.stderr.splitlines()[-1])["error"])


def posts(calls: list[str]) -> list[str]:
    return [c for c in calls if c.startswith("POST ")]


def test_links_parent_and_blocker_by_internal_id_and_reads_each_back(tmp_path: Path) -> None:
    r, calls = run(tmp_path, CHILD, *LINK_BOTH, **world())
    assert r.returncode == 0, r.stderr
    assert out(r) == {
        "issue": CHILD,
        "links": {
            "parent": {"ref": "acme/sandbox#5", "ok": True, "action": "linked"},
            "blocked_by": [{"ref": "other/lib#7", "ok": True, "action": "linked"}],
        },
    }
    assert r.stderr == ""
    # every lookup first, then each link followed by its read-back
    assert calls == [
        f"GET {CHILD_PATH}",
        "GET repos/acme/sandbox/issues/5",
        "GET repos/other/lib/issues/7",
        f"GET {BLOCKED_PATH}?per_page=100",
        f"POST {SUB_ISSUES_5} sub_issue_id=9000013",
        f"GET {CHILD_PATH}",
        f"POST {BLOCKED_PATH} issue_id=9000007",
        f"GET {BLOCKED_PATH}?per_page=100",
    ]


def test_a_short_ref_resolves_to_the_issues_own_repo(tmp_path: Path) -> None:
    r, calls = run(tmp_path, CHILD, "--parent", "#5", **world())
    assert r.returncode == 0, r.stderr
    assert out(r)["links"]["parent"]["ref"] == "acme/sandbox#5"
    assert out(r)["links"]["blocked_by"] == []
    assert posts(calls) == [f"POST {SUB_ISSUES_5} sub_issue_id=9000013"]
    assert f"GET {BLOCKED_PATH}?per_page=100" not in calls, "no blockers, no list"


def test_a_rerun_on_a_linked_issue_posts_nothing(tmp_path: Path) -> None:
    """The repair path: rerunning the same command leaves what is in place alone."""
    linked = fx(
        issue_file(CHILD, parent="acme/sandbox#5"),
        {f"{BLOCKED_PATH}.json": [gh_issue("other/lib#7")]},
    )
    r, calls = run(tmp_path, CHILD, *LINK_BOTH, **world(**linked))
    assert r.returncode == 0, r.stderr
    assert out(r)["links"] == {
        "parent": {"ref": "acme/sandbox#5", "ok": True, "action": "exists"},
        "blocked_by": [{"ref": "other/lib#7", "ok": True, "action": "exists"}],
    }
    assert posts(calls) == []


def test_the_parent_match_ignores_case(tmp_path: Path) -> None:
    linked = fx(issue_file(CHILD, parent_issue_url=api_url("Acme/Sandbox#5")))
    r, calls = run(tmp_path, CHILD, "--parent", "acme/sandbox#5", **world(**linked))
    assert r.returncode == 0, r.stderr
    assert out(r)["links"]["parent"]["action"] == "exists"
    assert posts(calls) == []


def test_a_blocker_is_matched_by_repository_and_number_never_number_alone(
    tmp_path: Path,
) -> None:
    """acme/sandbox#7 in the list is not other/lib#7."""
    same_number = fx({f"{BLOCKED_PATH}.json": [gh_issue("acme/sandbox#7")]})
    r, calls = run(tmp_path, CHILD, "--blocked-by", "other/lib#7", **world(**same_number))
    assert r.returncode == 0, r.stderr
    assert out(r)["links"]["blocked_by"][0]["action"] == "linked"
    assert posts(calls) == [f"POST {BLOCKED_PATH} issue_id=9000007"]


def test_a_different_parent_is_never_replaced(tmp_path: Path) -> None:
    other = fx(issue_file(CHILD, parent="acme/sandbox#4"))
    r, calls = run(tmp_path, CHILD, *LINK_BOTH, **world(**other))
    assert r.returncode == 7
    parent = out(r)["links"]["parent"]
    assert parent["ok"] is False and parent["action"] is None
    assert "already has parent acme/sandbox#4; never replaced" in parent["error"]
    # the other link still goes through, and no sub-issue POST is made
    assert out(r)["links"]["blocked_by"][0]["ok"] is True
    assert posts(calls) == [f"POST {BLOCKED_PATH} issue_id=9000007"]
    assert "1 of 2 link(s)" in error(r)


def test_a_parent_in_another_repo_of_the_same_owner_is_linked_there(tmp_path: Path) -> None:
    """ui ↔ api: the sub-issue POST goes to the parent's repository, not the child's."""
    cross = fx(
        issue_file("acme/api#5"),
        {f"{CHILD_PATH}.after.json": gh_issue(CHILD, parent="acme/api#5")},
    )
    r, calls = run(tmp_path, CHILD, "--parent", "acme/api#5", **world(**cross))
    assert r.returncode == 0, r.stderr
    assert out(r)["links"]["parent"] == {"ref": "acme/api#5", "ok": True, "action": "linked"}
    assert posts(calls) == ["POST repos/acme/api/issues/5/sub_issues sub_issue_id=9000013"]


def test_a_parent_with_the_same_number_in_another_repo_is_a_different_parent(
    tmp_path: Path,
) -> None:
    other = fx(issue_file(CHILD, parent="acme/api#5"))
    r, calls = run(tmp_path, CHILD, "--parent", "acme/sandbox#5", **world(**other))
    assert r.returncode == 7
    assert "already has parent acme/api#5; never replaced" in out(r)["links"]["parent"]["error"]
    assert posts(calls) == []


def test_a_failed_link_call_is_exit_7_with_gh_text_and_the_result_on_stdout(
    tmp_path: Path,
) -> None:
    failing = fx(
        {f"{SUB_ISSUES_5}.post.code": "1"},
        {f"{SUB_ISSUES_5}.post.err": "gh: Validation Failed (HTTP 422)"},
        {f"{CHILD_PATH}.after.json": gh_issue(CHILD)},
    )
    r, _ = run(tmp_path, CHILD, *LINK_BOTH, **world(**failing))
    assert r.returncode == 7
    parent = out(r)["links"]["parent"]
    assert parent["ok"] is False
    assert "link call failed: gh: Validation Failed (HTTP 422)" in parent["error"]
    assert "rerun" in error(r)


def test_a_link_call_that_errored_but_landed_counts_as_linked(tmp_path: Path) -> None:
    """The read-back decides, as it does for the create: a timeout after the
    server applied the link is a success, not a failure to retry."""
    timed_out = fx(
        {f"{SUB_ISSUES_5}.post.code": "1"},
        {f"{SUB_ISSUES_5}.post.err": "gh: HTTP 504"},
    )
    r, _ = run(tmp_path, CHILD, "--parent", "acme/sandbox#5", **world(**timed_out))
    assert r.returncode == 0, r.stderr
    assert out(r)["links"]["parent"]["action"] == "linked"


@pytest.mark.parametrize(
    "missing,needle",
    [
        ({f"{CHILD_PATH}.after.json": gh_issue(CHILD)}, "not set on read-back"),
        ({f"{BLOCKED_PATH}.after.json": []}, "not listed on read-back"),
    ],
)
def test_a_link_call_that_returned_ok_but_is_not_there_is_exit_7(
    tmp_path: Path, missing: dict[str, Any], needle: str
) -> None:
    r, _ = run(tmp_path, CHILD, *LINK_BOTH, **world(**fx(missing)))
    assert r.returncode == 7
    links = out(r)["links"]
    failed = [x for x in [links["parent"], *links["blocked_by"]] if not x["ok"]]
    assert len(failed) == 1
    assert needle in failed[0]["error"]


def test_a_failed_read_back_is_a_failed_link(tmp_path: Path) -> None:
    r, _ = run(tmp_path, CHILD, "--parent", "#5", **world(**{f"{CHILD_PATH}.after.json": None}))
    assert r.returncode == 7
    assert "read-back" in out(r)["links"]["parent"]["error"]


# ─── refused before the first POST ──────────────────────────────────────────
NOT_FOUND = "gh: Not Found (HTTP 404)"


@pytest.mark.parametrize(
    "broken,needle",
    [
        (fx({"repos/other/lib/issues/7.json": None, "repos/other/lib/issues/7.err": NOT_FOUND}),
         "other/lib#7 not found"),
        (fx(issue_file("other/lib#7", pull_request={"url": "https://x/pull/7"})),
         "other/lib#7 is a pull request"),
        (fx(issue_file("acme/sandbox#5", pull_request={"url": "https://x/pull/5"})),
         "acme/sandbox#5 is a pull request"),
        (fx({f"{CHILD_PATH}.json": None, f"{CHILD_PATH}.err": NOT_FOUND}),
         "acme/sandbox#13 not found"),
        (fx(issue_file(CHILD, pull_request={"url": "https://x/pull/13"})),
         "acme/sandbox#13 is a pull request"),
    ],
)  # fmt: skip
def test_a_missing_or_wrong_kind_of_issue_is_exit_2_and_nothing_is_posted(
    tmp_path: Path, broken: dict[str, Any], needle: str
) -> None:
    r, calls = run(tmp_path, CHILD, *LINK_BOTH, **world(**broken))
    assert r.returncode == 2
    assert r.stdout == ""
    assert needle in error(r)
    assert posts(calls) == []


@pytest.mark.parametrize(
    "err,code",
    [
        ("gh: Bad credentials (HTTP 401)", 3),
        ("gh: Resource not accessible (HTTP 403)", 3),
        ("gh: Server Error (HTTP 502)", 5),
        ("dial tcp: lookup api.github.com: no such host", 5),
        # the class comes from the HTTP status, not from digits anywhere in the text
        ("gh: Server Error (HTTP 500) for issue 403 / 404", 5),
    ],
)
def test_a_failed_lookup_is_classified_by_its_http_status(
    tmp_path: Path, err: str, code: int
) -> None:
    broken = fx({"repos/other/lib/issues/7.json": None, "repos/other/lib/issues/7.err": err})
    r, calls = run(tmp_path, CHILD, *LINK_BOTH, **world(**broken))
    assert r.returncode == code
    assert r.stdout == ""
    assert posts(calls) == []


def test_an_unreadable_blocked_by_list_stops_before_any_link(tmp_path: Path) -> None:
    """All lookups precede all writes: the parent is not linked either."""
    broken = fx({f"{BLOCKED_PATH}.json": None, f"{BLOCKED_PATH}.err": "gh: HTTP 500"})
    r, calls = run(tmp_path, CHILD, *LINK_BOTH, **world(**broken))
    assert r.returncode == 5
    assert posts(calls) == []


@pytest.mark.parametrize(
    "args,needle",
    [
        ((CHILD,), "nothing to link"),
        (("acme/sandbox", "--parent", "#5"), "usage"),
        (("acme/sandbox#0", "--parent", "#5"), "usage"),
        ((), "usage"),
        ((CHILD, "--parent"), "--parent needs a value"),
        ((CHILD, "--parent", "#5", "--parent", "#6"), "one parent"),
        ((CHILD, "--parent", "5"), "not an issue ref"),
        ((CHILD, "--blocked-by", "https://github.com/a/b/issues/1"), "not an issue ref"),
        ((CHILD, "--blocked-by", "#7", "--blocked-by", "acme/sandbox#7"), "more than once"),
        ((CHILD, "--parent", "other/sandbox#5"), "owner"),
        ((CHILD, "--frobnicate"), "unknown argument"),
        (("acme/sandbox#13", "--check", "--parent", "#5"), "usage"),
    ],
)
def test_usage_errors_exit_2_before_any_call(
    tmp_path: Path, args: tuple[str, ...], needle: str
) -> None:
    r, calls = run(tmp_path, *args, **world())
    assert r.returncode == 2
    assert needle in error(r)
    assert calls == []


def test_the_parent_owner_rule_ignores_case(tmp_path: Path) -> None:
    upper = fx(
        issue_file("Acme/sandbox#13"),
        {"repos/Acme/sandbox/issues/13.after.json": gh_issue(CHILD, parent="acme/sandbox#5")},
    )
    r, _ = run(tmp_path, "Acme/sandbox#13", "--parent", "acme/sandbox#5", **world(**upper))
    assert r.returncode == 0, r.stderr


# ─── --check: the targets of an issue that does not exist yet ───────────────
def test_check_looks_up_the_targets_only_and_posts_nothing(tmp_path: Path) -> None:
    r, calls = run(tmp_path, "acme/sandbox", "--check", *LINK_BOTH, **world())
    assert r.returncode == 0, r.stderr
    assert out(r) == {
        "repo": "acme/sandbox",
        "targets": [
            {"kind": "parent", "ref": "acme/sandbox#5", "id": 9000005},
            {"kind": "blocked_by", "ref": "other/lib#7", "id": 9000007},
        ],
    }
    assert calls == ["GET repos/acme/sandbox/issues/5", "GET repos/other/lib/issues/7"]


def test_check_refuses_a_target_that_is_a_pull_request(tmp_path: Path) -> None:
    pull = fx(issue_file("other/lib#7", pull_request={"url": "https://x/pull/7"}))
    r, _ = run(tmp_path, "acme/sandbox", "--check", *LINK_BOTH, **world(**pull))
    assert r.returncode == 2
    assert "pull request" in error(r)


# ─── the real command line, through the stand-in gh ─────────────────────────
def run_live(
    tmp_path: Path, *args: str, **cfg: Any
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "gh").write_text(FAKE_GH.read_text(encoding="utf-8"))
    (bindir / "gh").chmod(0o755)
    config: dict[str, Any] = {
        "repo": "acme/sandbox",
        "issues": {ref: gh_issue(ref) for ref in (CHILD, "acme/sandbox#5", "other/lib#7")},
        **cfg,
    }
    (tmp_path / "cfg.json").write_text(json.dumps(config))
    log = tmp_path / "calls.jsonl"
    r = subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{bindir}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "STUB_LOG": str(log),
            "STUB_CFG": str(tmp_path / "cfg.json"),
        },
        timeout=60,
    )
    calls = [json.loads(line)[1:] for line in log.read_text().splitlines()] if log.exists() else []
    return r, calls


def test_the_real_calls_post_internal_ids_with_typed_fields(tmp_path: Path) -> None:
    r, gh = run_live(tmp_path, CHILD, *LINK_BOTH)
    assert r.returncode == 0, r.stderr
    assert gh == [
        ["api", CHILD_PATH],
        ["api", "repos/acme/sandbox/issues/5"],
        ["api", "repos/other/lib/issues/7"],
        ["api", f"{BLOCKED_PATH}?per_page=100"],
        # `-F`, not `-f`: the id goes as an integer — and it is the id, not 13 or 7
        ["api", "-X", "POST", SUB_ISSUES_5, "-F", "sub_issue_id=9000013"],
        ["api", CHILD_PATH],
        ["api", "-X", "POST", BLOCKED_PATH, "-F", "issue_id=9000007"],
        ["api", f"{BLOCKED_PATH}?per_page=100"],
    ]
    assert [x["action"] for x in [out(r)["links"]["parent"], *out(r)["links"]["blocked_by"]]] == [
        "linked",
        "linked",
    ]


def test_a_real_rerun_posts_nothing(tmp_path: Path) -> None:
    issues = {
        CHILD: gh_issue(CHILD, parent="acme/sandbox#5"),
        "acme/sandbox#5": gh_issue("acme/sandbox#5"),
        "other/lib#7": gh_issue("other/lib#7"),
    }
    r, gh = run_live(
        tmp_path, CHILD, *LINK_BOTH, issues=issues, blocked_by={CHILD: [gh_issue("other/lib#7")]}
    )
    assert r.returncode == 0, r.stderr
    assert not any(c[:3] == ["api", "-X", "POST"] for c in gh)


def test_a_real_post_that_does_not_land_is_exit_7(tmp_path: Path) -> None:
    r, _ = run_live(tmp_path, CHILD, *LINK_BOTH, blocked_by_post={"lands": False})
    assert r.returncode == 7
    links = out(r)["links"]
    assert links["parent"]["ok"] is True
    assert links["blocked_by"][0]["ok"] is False
