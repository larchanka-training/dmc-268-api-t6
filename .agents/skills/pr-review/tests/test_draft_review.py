"""Contract tests for scripts/draft_review.py.

Offline: no network, no tokens, no live `gh`. An autouse guard puts a fake `gh` first on PATH
that fails with exit 97, so any request that escapes the fixture seam (`PR_REVIEW_FIXTURE_DIR`
with `PR_REVIEW_WRITE_LOG`) or a test's own fake is an error, never a call.

Covers: every draft case of `examples/` (stdout byte-equal to `expected.json`, the write log
byte-equal to `expected-writes.jsonl`, no log at all when the case writes nothing); what can never
be written (a verdict, a submit); the diff's commentable lines, the thread text and the proposed
verdict as pure functions; the stops, the partial write and the read-back; the shape of the
findings file (exit 2), exit 3 and the argv handed to `gh`.
"""
# SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/pr-review/tests/test_draft_review.py

from __future__ import annotations

import copy
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
sys.path.insert(0, str(HERE))
from gh_fakes import fake_gh, prepend_path, refuse_live_gh  # noqa: E402

SKILL = HERE.parent
SCRIPT = SKILL / "scripts" / "draft_review.py"
EXAMPLES = SKILL / "examples"
DIFF = EXAMPLES / "ui130.diff"
SEAM = "PR_REVIEW_FIXTURE_DIR"
LOG = "PR_REVIEW_WRITE_LOG"
HEAD = "21b8d99b895956a7c0dcd2ee809b401552aad806"
OTHER = "4c1d0f6a7e2b9385d6f1a0c47be93d1285fa6e07"
PATH = "src/widgets/run-inspector/ui/RunControls.tsx"
RID = "PRR_kwDOUOEIjs8AAAABV8_WIQ"
NULL_THREAD = {"data": {"addPullRequestReviewThread": {"thread": None}}}


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("draft_review_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


draft = _load()


@pytest.fixture(autouse=True)
def no_live_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts with a `gh` that refuses to run, no fixture dir and no write log."""
    refuse_live_gh(tmp_path, monkeypatch)
    monkeypatch.delenv(SEAM, raising=False)
    monkeypatch.delenv(LOG, raising=False)


def run(
    *args: str, fixtures: Path | None = None, log: Path | None = None, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if fixtures is not None:
        env[SEAM] = str(fixtures)
    if log is not None:
        env[LOG] = str(log)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        encoding="utf-8",
        env=env,
        cwd=cwd,
        timeout=30,
    )


def doc_of(r: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    value = json.loads(r.stdout)
    assert isinstance(value, dict)
    return value


def error_of(r: subprocess.CompletedProcess[str]) -> str:
    assert r.stdout == ""
    message = json.loads(r.stderr)["error"]
    assert isinstance(message, str)
    return message


# ─── the examples ───────────────────────────────────────────────────────────
CASES = sorted(p.parent.name for p in EXAMPLES.glob("*/findings.json"))
WRITES = sorted(EXAMPLES.glob("*/expected-writes.jsonl"))


def invocation(case: str) -> list[str]:
    readme = (EXAMPLES / case / "README.md").read_text(encoding="utf-8")
    block = re.search(r"```sh\n(draft_review\.py [^\n]+)\n```", readme)
    assert block, f"{case}/README.md has no ```sh draft_review.py ...``` block"
    return shlex.split(block.group(1))[1:]


def test_there_are_six_draft_examples() -> None:
    assert CASES == [
        "05-preview",
        "06-new-draft",
        "07-existing-draft",
        "08-head-moved",
        "09-out-of-scope",
        "10-no-findings",
    ]


@pytest.mark.parametrize("case", CASES)
def test_example(case: str, tmp_path: Path) -> None:
    folder = EXAMPLES / case
    readme = (folder / "README.md").read_text(encoding="utf-8")
    assert readme.startswith(
        f"<!-- SYNC: mirrored in dmc-268-{{ui,api}}-t6/.agents/skills/pr-review/"
        f"examples/{case}/README.md -->\n\n"
    )
    log = tmp_path / "writes.jsonl"
    r = run(*invocation(case), fixtures=folder / "fixtures", log=log, cwd=folder)
    expected = (folder / "expected.json").read_text(encoding="utf-8")
    assert r.stdout == expected
    assert r.stderr == ""
    assert r.returncode == (0 if json.loads(expected)["verdict"] == "go" else 1)
    writes = folder / "expected-writes.jsonl"
    if writes.exists():
        assert log.read_text(encoding="utf-8") == writes.read_text(encoding="utf-8")
    else:
        assert not log.exists()


# ─── what can never be written ──────────────────────────────────────────────
def test_the_examples_do_write() -> None:
    assert len(WRITES) == 4


@pytest.mark.parametrize("path", WRITES, ids=lambda p: p.parent.name)
def test_a_write_never_carries_a_verdict(path: Path) -> None:
    for line in path.read_text(encoding="utf-8").splitlines():
        write = json.loads(line)
        assert write["op"] in {
            "addPullRequestReview",
            "addPullRequestReviewThread",
            "updatePullRequestReview",
        }
        assert "event" not in write


@pytest.mark.parametrize(
    "name", ["READ", "CREATE", "UPDATE_BODY", "THREAD_ON_LINE", "THREAD_ON_FILE"]
)
def test_no_graphql_document_can_set_a_verdict(name: str) -> None:
    assert "event" not in getattr(draft, name)


@pytest.mark.parametrize(
    "word",
    ["submitPullRequestReview", "APPROVE", "deletePullRequestReview"],
)
def test_the_script_has_no_way_to_submit_or_change_a_review(word: str) -> None:
    assert word not in SCRIPT.read_text(encoding="utf-8")


def test_a_preview_writes_nothing(tmp_path: Path) -> None:
    folder = EXAMPLES / "05-preview"
    log = tmp_path / "writes.jsonl"
    r = run(*invocation("05-preview"), fixtures=folder / "fixtures", log=log, cwd=folder)
    assert r.returncode == 0
    assert doc_of(r)["applied"] is False
    assert not log.exists()


def test_a_preview_needs_no_write_log() -> None:
    folder = EXAMPLES / "05-preview"
    r = run(*invocation("05-preview"), fixtures=folder / "fixtures", cwd=folder)
    assert r.returncode == 0


def test_apply_under_the_seam_needs_a_write_log() -> None:
    folder = EXAMPLES / "06-new-draft"
    r = run(*invocation("06-new-draft"), fixtures=folder / "fixtures", cwd=folder)
    assert r.returncode == 3
    assert error_of(r) == f"--apply against {SEAM} needs {LOG}"


# ─── pure functions ─────────────────────────────────────────────────────────
HEADER = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"


@pytest.mark.parametrize(
    ("diff", "expected"),
    [
        pytest.param(
            HEADER + "@@ -1,2 +1,3 @@\n one\n+two\n three\n",
            {"a.py": {1, 2, 3}},
            id="added-context",
        ),
        pytest.param(
            HEADER + "@@ -5,3 +5,2 @@\n x\n-y\n z\n", {"a.py": {5, 6}}, id="a-removed-line-has-none"
        ),
        pytest.param(HEADER + "@@ -1 +1 @@\n-a\n+b\n", {"a.py": {1}}, id="counts-omitted"),
        pytest.param(
            HEADER + "@@ -0,0 +1,3 @@\n+++ b/evil.py\n+@@ -1 +9 @@\n+x\n",
            {"a.py": {1, 2, 3}},
            id="added-text-that-looks-like-a-header",
        ),
        pytest.param(
            HEADER + "@@ -1 +1 @@\n-a\n+b\n\\ No newline at end of file\n",
            {"a.py": {1}},
            id="no-newline-marker",
        ),
        pytest.param(
            HEADER
            + "@@ -1 +1 @@\n-a\n+b\n@@ -9 +9,2 @@\n c\n+d\n"
            + "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -3 +3 @@\n-e\n+f\n",
            {"a.py": {1, 9, 10}, "b.py": {3}},
            id="two-hunks-two-files",
        ),
        pytest.param(
            "diff --git a/n.py b/n.py\nnew file mode 100644\n--- /dev/null\n+++ b/n.py\n"
            "@@ -0,0 +1,2 @@\n+a\n+b\n",
            {"n.py": {1, 2}},
            id="new-file",
        ),
        pytest.param(
            "diff --git a/d.py b/d.py\ndeleted file mode 100644\n--- a/d.py\n+++ /dev/null\n"
            "@@ -1 +0,0 @@\n-a\n",
            {"d.py": set()},
            id="deleted-file",
        ),
        pytest.param(
            "diff --git a/a b.py b/a b.py\n--- a/a b.py\t\n+++ b/a b.py\t\n@@ -1 +1 @@\n-a\n+b\n",
            {"a b.py": {1}},
            id="a-path-with-a-space",
        ),
        pytest.param(
            "diff --git a/s.sh b/s.sh\nold mode 100644\nnew mode 100755\n",
            {"s.sh": set()},
            id="only-the-mode",
        ),
        pytest.param(
            "diff --git a/p.png b/p.png\nindex 1..2 100644\n"
            "Binary files a/p.png and b/p.png differ\n",
            {"p.png": set()},
            id="binary",
        ),
        pytest.param(
            "diff --git a/old.py b/new.py\nsimilarity index 100%\n"
            "rename from old.py\nrename to new.py\n",
            {"new.py": set()},
            id="only-renamed",
        ),
        pytest.param(
            "diff --git a/o.py b/c.py\nsimilarity index 100%\ncopy from o.py\ncopy to c.py\n",
            {"c.py": set()},
            id="only-copied",
        ),
        pytest.param(
            HEADER + "@@ -0,0 +1,2 @@\n+diff --git a/x b/x\n+rename to y\n",
            {"a.py": {1, 2}},
            id="added-text-that-looks-like-a-file-header",
        ),
        pytest.param("", {}, id="empty"),
    ],
)
def test_commentable_lines(diff: str, expected: dict[str, set[int]]) -> None:
    assert draft.commentable_lines(diff) == expected


def test_commentable_lines_of_a_real_diff() -> None:
    lines = draft.commentable_lines(DIFF.read_text(encoding="utf-8"))
    assert sorted(lines) == [
        "docs/FRONTEND_ARCHITECTURE.md",
        "src/widgets/run-inspector/ui/RunControls.test.tsx",
        PATH,
    ]
    assert lines[PATH] == {*range(1, 6), *range(21, 28), *range(35, 60)}


def finding(**changes: Any) -> dict[str, Any]:
    return {
        "body": "Тело.",
        "category": "correctness",
        "decision": None,
        "line": 41,
        "path": PATH,
        "scope": "in",
        "severity": "high",
        "title": "Заголовок",
        **changes,
    }


def test_a_finding_on_a_file_outside_the_diff_names_its_place() -> None:
    assert draft.thread_body(finding(path="package.json", line=3), "other") == (
        "**P1 · high · correctness.** Заголовок\n\n"
        "Замечание к `package.json:3`: этого файла нет в диффе, тред на первом файле PR.\n\nТело."
    )


@pytest.mark.parametrize(
    ("severity", "expected"),
    [
        ("critical", "**P1 · critical · correctness.** Заголовок\n\nТело."),
        ("high", "**P1 · high · correctness.** Заголовок\n\nТело."),
        ("medium", "**P2 · medium · correctness.** Заголовок\n\nТело."),
        ("low", "nit: **P3 · low · correctness.** Заголовок\n\nТело."),
        ("info", "nit: **P3 · info · correctness.** Заголовок\n\nТело."),
    ],
)
def test_the_thread_names_the_priority(severity: str, expected: str) -> None:
    assert draft.thread_body(finding(severity=severity), "line") == expected


@pytest.mark.parametrize(
    ("findings", "statuses", "has_remarks", "is_author", "expected"),
    [
        pytest.param([], [], False, False, "Approve", id="nothing"),
        pytest.param([], ["covered", "can't tell"], False, False, "Approve", id="can-t-tell-only"),
        pytest.param([], ["not covered"], False, False, "Request changes", id="ac-not-covered"),
        pytest.param([finding()], [], True, False, "Request changes", id="in-scope-high"),
        pytest.param(
            [finding(severity="critical")],
            [],
            True,
            False,
            "Request changes",
            id="in-scope-critical",
        ),
        pytest.param(
            [finding(severity="medium")], [], True, False, "Request changes", id="in-scope-medium"
        ),
        pytest.param([finding(severity="low")], [], True, False, "Comment", id="in-scope-low"),
        pytest.param([finding(severity="info")], [], True, False, "Comment", id="in-scope-info"),
        pytest.param(
            [finding(scope="out", decision={"kind": "rejected", "why": "нет"})],
            [],
            True,
            False,
            "Comment",
            id="out-of-scope-high-in-a-thread",
        ),
        pytest.param(
            [finding(scope="out", decision={"kind": "follow-up", "ref": "api#1"})],
            [],
            False,
            False,
            "Approve",
            id="out-of-scope-follow-up-only",
        ),
        pytest.param(
            [finding()], [], True, True, "Comment", id="the-author-cannot-request-changes"
        ),
        pytest.param([], [], False, True, "Comment", id="the-author-cannot-approve"),
    ],
)
def test_proposed_verdict(
    findings: list[dict[str, Any]],
    statuses: list[str],
    has_remarks: bool,
    is_author: bool,
    expected: str,
) -> None:
    ac = [{"key": f"ui#1 AC{k}", "status": s} for k, s in enumerate(statuses, 1)]
    assert draft.proposed_verdict(findings, ac, has_remarks, is_author) == expected


# ─── scenarios built on the examples ────────────────────────────────────────
def scenario(
    tmp_path: Path,
    case: str = "06-new-draft",
    findings: dict[str, Any] | None = None,
    **fixtures: Any,
) -> tuple[Path, list[str]]:
    """A copy of an example's fixtures with some replaced, and the arguments to run it."""
    source = EXAMPLES / case
    target = tmp_path / "fixtures"
    target.mkdir()
    for file in (source / "fixtures").iterdir():
        (target / file.name).write_text(file.read_text(encoding="utf-8"), encoding="utf-8")
    for name, value in fixtures.items():
        (target / f"{name.replace('_', '-')}.json").write_text(json.dumps(value), encoding="utf-8")
    plan = tmp_path / "findings.json"
    if findings is None:
        findings = json.loads((source / "findings.json").read_text(encoding="utf-8"))
    plan.write_text(json.dumps(findings), encoding="utf-8")
    return target, ["ui#130", "--findings", str(plan), "--diff", str(DIFF), "--apply"]


def example(case: str, name: str) -> dict[str, Any]:
    value = json.loads((EXAMPLES / case / "fixtures" / f"{name}.json").read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def plan_of(*findings: dict[str, Any]) -> dict[str, Any]:
    return {"ac": [], "findings": list(findings), "head_sha": HEAD, "schema": 1}


def pull(state: dict[str, Any]) -> dict[str, Any]:
    node = state["data"]["repository"]["pullRequest"]
    assert isinstance(node, dict)
    return node


def state_with_draft(**changes: Any) -> dict[str, Any]:
    state = copy.deepcopy(example("07-existing-draft", "state"))
    pull(state)["reviews"]["nodes"][0].update(changes)
    return state


# ─── stops ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("fixtures", "findings", "reason"),
    [
        pytest.param(
            {"state": state_with_draft(commit={"oid": OTHER})},
            None,
            f"your draft review is on {OTHER}, not on the head {HEAD}: "
            "submit or delete it on GitHub, then run again",
            id="a-draft-on-another-commit",
        ),
        pytest.param(
            {"state": state_with_draft(comments={"pageInfo": {"hasNextPage": True}, "nodes": []})},
            None,
            "your draft review has over 100 comments: finish it on GitHub, then run again",
            id="a-draft-too-long-to-compare",
        ),
        pytest.param(
            {"state": state_with_draft(body="Рукописный текст ревьюера")},
            None,
            "your draft review has text of its own: submit or delete it on GitHub, then run again",
            id="a-draft-with-text-of-its-own",
        ),
        pytest.param(
            {},
            plan_of(finding(scope="out"), finding(scope="out", line=50)),
            "findings out of scope need a decision (follow-up, accepted or rejected): "
            f"{PATH}:41, {PATH}:50",
            id="out-of-scope-without-a-decision",
        ),
    ],
)
def test_a_stop_writes_nothing(
    tmp_path: Path,
    fixtures: dict[str, Any],
    findings: dict[str, Any] | None,
    reason: str,
) -> None:
    target, args = scenario(tmp_path, findings=findings, **fixtures)
    log = tmp_path / "writes.jsonl"
    r = run(*args, fixtures=target, log=log)
    assert r.returncode == 1
    doc = doc_of(r)
    assert doc["verdict"] == "stop"
    assert doc["reasons"] == [reason]
    assert doc["threads"] == []
    assert not log.exists()


def test_every_stop_reason_is_reported(tmp_path: Path) -> None:
    state = copy.deepcopy(example("06-new-draft", "state"))
    pull(state)["headRefOid"] = OTHER
    target, args = scenario(tmp_path, findings=plan_of(finding(scope="out")), state=state)
    doc = doc_of(run(*args, fixtures=target, log=tmp_path / "writes.jsonl"))
    assert doc["reasons"] == [
        f"the pull request's head moved to {OTHER}: the findings are about {HEAD}; "
        "run the review again",
        f"findings out of scope need a decision (follow-up, accepted or rejected): {PATH}:41",
    ]


@pytest.mark.parametrize(
    ("old", "expected"),
    [
        pytest.param("", 1, id="an-empty-text-takes-the-summary"),
        pytest.param("Итог на `21b8d99b`: прежний итог.", 1, id="an-earlier-summary-is-replaced"),
    ],
)
def test_a_rerun_brings_the_summary_of_the_draft_up_to_date(
    tmp_path: Path, old: str, expected: int
) -> None:
    plan = plan_of()
    plan["ac"] = [{"key": "ui#111 AC1", "status": "not covered"}]
    summary = (
        "Итог на `21b8d99b`: critical 0, high 0, medium 0, low 0, info 0. "
        "AC: covered 0, not covered 1, can't tell 0.\n\n"
        "Предложенный вердикт: Request changes. Вердикт и Submit — решение ревьюера.\n\n"
        "Не покрыто (not covered): ui#111 AC1"
    )
    readback = state_with_draft(body=summary)
    target, args = scenario(
        tmp_path,
        "07-existing-draft",
        findings=plan,
        state=state_with_draft(body=old),
        readback=readback,
    )
    log = tmp_path / "writes.jsonl"
    r = run(*args, fixtures=target, log=log)
    assert r.returncode == 0, r.stdout + r.stderr
    writes = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [w for w in writes if w["op"] == "updatePullRequestReview"] == [
        {"body": summary, "op": "updatePullRequestReview", "pullRequestReviewId": RID}
    ]
    assert len(writes) == expected


def test_a_draft_already_up_to_date_is_not_written_again(tmp_path: Path) -> None:
    folder = EXAMPLES / "07-existing-draft"
    expected = json.loads((folder / "expected.json").read_text(encoding="utf-8"))
    readback = example("07-existing-draft", "readback")
    target, args = scenario(tmp_path, "07-existing-draft", state=readback, readback=readback)
    log = tmp_path / "writes.jsonl"
    doc = doc_of(run(*args, fixtures=target, log=log))
    assert doc["summary"] == expected["summary"]
    assert doc["warnings"] == ["the pull request is merged: the draft comments on its final head"]
    assert [t["status"] for t in doc["threads"]] == ["exists", "exists"]
    assert not log.exists()


def test_a_reviewer_who_is_the_author_is_warned(tmp_path: Path) -> None:
    state = copy.deepcopy(example("05-preview", "state"))
    state["data"]["viewer"]["login"] = "axyi"
    target, args = scenario(tmp_path, "05-preview", state=state)
    doc = doc_of(run(*args[:-1], fixtures=target))
    assert doc["proposed_verdict"] == "Comment"
    assert (
        "you are the author of this pull request: GitHub accepts only a comment from you"
        in (doc["warnings"])
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("OPEN", [], id="open"),
        pytest.param(
            "MERGED",
            ["the pull request is merged: the draft comments on its final head"],
            id="merged",
        ),
        pytest.param("CLOSED", ["the pull request is closed without a merge"], id="closed"),
    ],
)
def test_the_state_warnings(tmp_path: Path, value: str, expected: list[str]) -> None:
    state = copy.deepcopy(example("05-preview", "state"))
    pull(state)["state"] = value
    target, args = scenario(tmp_path, "05-preview", state=state)
    assert doc_of(run(*args[:-1], fixtures=target))["warnings"] == expected


def test_someone_elses_draft_is_not_used(tmp_path: Path) -> None:
    state = state_with_draft(viewerDidAuthor=False)
    target, args = scenario(tmp_path, "05-preview", state=state)
    doc = doc_of(run(*args[:-1], fixtures=target))
    assert doc["review"] is None
    assert [t["status"] for t in doc["threads"]] == ["new"]


def test_a_finding_on_a_file_outside_the_diff_is_a_thread_on_the_first_file(
    tmp_path: Path,
) -> None:
    target, args = scenario(
        tmp_path, "05-preview", findings=plan_of(finding(path="package.json", line=3))
    )
    doc = doc_of(run(*args[:-1], fixtures=target))
    assert doc["in_body"] == []
    assert [(t["path"], t["line"], t["kind"]) for t in doc["threads"]] == [
        ("docs/FRONTEND_ARCHITECTURE.md", None, "file")
    ]


@pytest.mark.parametrize(
    ("findings", "state_case", "expected"),
    [
        pytest.param(
            plan_of(finding(path="package.json", line=3, severity="low")),
            "05-preview",
            "Comment",
            id="in-scope-on-a-file-outside-the-diff",
        ),
        pytest.param(
            plan_of(
                finding(
                    path="package.json",
                    line=3,
                    scope="out",
                    decision={"kind": "rejected", "why": "нет"},
                )
            ),
            "05-preview",
            "Comment",
            id="record-on-a-file-outside-the-diff",
        ),
        pytest.param(plan_of(), "07-existing-draft", "Comment", id="comments-already-in-the-draft"),
        pytest.param(
            plan_of(finding(scope="out", decision={"kind": "follow-up", "ref": "api#500"})),
            "05-preview",
            "Approve",
            id="only-a-follow-up",
        ),
    ],
)
def test_the_verdict_sees_every_remark(
    tmp_path: Path, findings: dict[str, Any], state_case: str, expected: str
) -> None:
    target, args = scenario(
        tmp_path, "05-preview", findings=findings, state=example(state_case, "state")
    )
    assert doc_of(run(*args[:-1], fixtures=target))["proposed_verdict"] == expected


def test_the_summary_names_the_criteria_not_covered(tmp_path: Path) -> None:
    plan = plan_of()
    plan["ac"] = [
        {"key": "ui#111 AC1", "status": "not covered"},
        {"key": "ui#111 AC2", "status": "can't tell"},
    ]
    target, args = scenario(tmp_path, "05-preview", findings=plan)
    doc = doc_of(run(*args[:-1], fixtures=target))
    assert doc["summary"] == (
        "Итог на `21b8d99b`: critical 0, high 0, medium 0, low 0, info 0. "
        "AC: covered 0, not covered 1, can't tell 1.\n\n"
        "Предложенный вердикт: Request changes. Вердикт и Submit — решение ревьюера.\n\n"
        "Не покрыто (not covered): ui#111 AC1\n\n"
        "Проверить руками (can't tell): ui#111 AC2"
    )


# ─── a partial write ────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(NULL_THREAD, id="a-null-thread"),
        pytest.param({"data": {"addPullRequestReviewThread": None}}, id="a-null-answer"),
        pytest.param({"data": {}}, id="no-answer"),
    ],
)
def test_a_thread_github_did_not_create_is_reported(tmp_path: Path, answer: dict[str, Any]) -> None:
    readback = copy.deepcopy(example("06-new-draft", "readback"))
    pull(readback)["reviews"]["nodes"][0]["comments"]["nodes"] = []
    target, args = scenario(tmp_path, thread_1=answer, readback=readback)
    r = run(*args, fixtures=target, log=tmp_path / "writes.jsonl")
    assert r.returncode == 1
    doc = doc_of(r)
    assert doc["verdict"] == "partial"
    assert doc["reasons"] == [
        "1 thread(s) not created: fix the cause and run again; those already there are skipped"
    ]
    assert [(t["status"], t["error"]) for t in doc["threads"]] == [
        ("failed", "GitHub did not create the thread")
    ]


def test_a_thread_missing_from_the_read_back_is_failed(tmp_path: Path) -> None:
    readback = copy.deepcopy(example("06-new-draft", "readback"))
    pull(readback)["reviews"]["nodes"][0]["comments"]["nodes"] = []
    target, args = scenario(tmp_path, readback=readback)
    r = run(*args, fixtures=target, log=tmp_path / "writes.jsonl")
    assert r.returncode == 1
    assert [(t["status"], t["error"]) for t in doc_of(r)["threads"]] == [
        ("failed", "not in the read-back")
    ]


def test_a_draft_missing_from_the_read_back_is_never_claimed(tmp_path: Path) -> None:
    target, args = scenario(tmp_path, readback=example("06-new-draft", "state"))
    r = run(*args, fixtures=target, log=tmp_path / "writes.jsonl")
    assert r.returncode == 1
    doc = doc_of(r)
    assert doc["verdict"] == "partial"
    assert doc["review"] is None
    assert doc["reasons"][0] == "the draft is not on GitHub after the write"


def moved_read_back(**changes: Any) -> dict[str, Any]:
    readback = copy.deepcopy(example("06-new-draft", "readback"))
    draft_node = pull(readback)["reviews"]["nodes"][0]
    if "head" in changes:
        pull(readback)["headRefOid"] = changes["head"]
    if "commit" in changes:
        draft_node["commit"] = {"oid": changes["commit"]}
    if "body" in changes:
        draft_node["body"] = changes["body"]
    return readback


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        pytest.param(
            {"head": OTHER},
            f"the pull request's head moved to {OTHER} during the write: "
            f"the draft is about {HEAD}; run the review again",
            id="the-head-moved",
        ),
        pytest.param(
            {"commit": OTHER},
            f"the draft is on {OTHER}, not on the commit the review read, {HEAD}; "
            "run the review again",
            id="the-draft-on-another-commit",
        ),
        pytest.param(
            {"body": "Другой текст"},
            "the draft does not hold the summary after the write",
            id="another-text",
        ),
    ],
)
def test_a_read_back_that_differs_from_the_review_is_partial(
    tmp_path: Path, changes: dict[str, str], reason: str
) -> None:
    target, args = scenario(tmp_path, readback=moved_read_back(**changes))
    r = run(*args, fixtures=target, log=tmp_path / "writes.jsonl")
    assert r.returncode == 1
    doc = doc_of(r)
    assert doc["verdict"] == "partial"
    assert doc["reasons"] == [reason]


DELETED_ONLY = (
    "diff --git a/gone.py b/gone.py\ndeleted file mode 100644\n--- a/gone.py\n+++ /dev/null\n"
    "@@ -1,2 +0,0 @@\n-a\n-b\n"
)


def test_a_pull_request_that_only_deletes_previews_and_applies(tmp_path: Path) -> None:
    gone = finding(path="gone.py", line=1, severity="low")
    body = "nit: **P3 · low · correctness.** Заголовок\n\nСтрока 1 не входит в дифф.\n\nТело."
    readback = copy.deepcopy(example("06-new-draft", "readback"))
    node = pull(readback)["reviews"]["nodes"][0]
    node["comments"]["nodes"] = [
        {"path": "gone.py", "line": None, "subjectType": "FILE", "body": body}
    ]
    summary = (
        "Итог на `21b8d99b`: critical 0, high 0, medium 0, low 1, info 0. "
        "AC: covered 0, not covered 0, can't tell 0.\n\n"
        "Предложенный вердикт: Comment. Вердикт и Submit — решение ревьюера."
    )
    node["body"] = summary
    target, args = scenario(tmp_path, findings=plan_of(gone), readback=readback)
    diff = tmp_path / "deleted.diff"
    diff.write_text(DELETED_ONLY, encoding="utf-8")
    args[args.index("--diff") + 1] = str(diff)
    log = tmp_path / "writes.jsonl"
    preview = doc_of(run(*args[:-1], fixtures=target, log=log))
    assert [(t["path"], t["kind"], t["status"]) for t in preview["threads"]] == [
        ("gone.py", "file", "new")
    ]
    assert not log.exists()
    r = run(*args, fixtures=target, log=log)
    assert r.returncode == 0, r.stdout + r.stderr
    assert [t["status"] for t in doc_of(r)["threads"]] == ["created"]
    writes = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert writes[1] == {
        "body": body,
        "op": "addPullRequestReviewThread",
        "path": "gone.py",
        "pullRequestReviewId": RID,
        "subjectType": "FILE",
    }


def test_no_finding_on_a_pull_request_that_only_deletes_still_makes_a_draft(
    tmp_path: Path,
) -> None:
    target, args = scenario(tmp_path, "05-preview", findings=plan_of())
    diff = tmp_path / "deleted.diff"
    diff.write_text(DELETED_ONLY, encoding="utf-8")
    args[args.index("--diff") + 1] = str(diff)
    r = run(*args[:-1], fixtures=target)
    assert r.returncode == 0, r.stderr
    assert doc_of(r)["proposed_verdict"] == "Approve"


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(None, id="no-answer"),
        pytest.param(
            {"data": {"viewer": {"login": "a"}, "repository": {"pullRequest": {"id": "x"}}}},
            id="another-shape",
        ),
    ],
)
def test_a_failed_read_back_is_partial(tmp_path: Path, answer: dict[str, Any] | None) -> None:
    target, args = scenario(tmp_path)
    if answer is None:
        (target / "readback.json").unlink()
    else:
        (target / "readback.json").write_text(json.dumps(answer), encoding="utf-8")
    r = run(*args, fixtures=target, log=tmp_path / "writes.jsonl")
    assert r.returncode == 1
    doc = doc_of(r)
    assert doc["verdict"] == "partial"
    assert doc["reasons"][0].startswith("cannot read the draft back: ")
    assert [t["status"] for t in doc["threads"]] == ["created"]


# ─── the findings file: exit 2 ──────────────────────────────────────────────
@pytest.mark.parametrize(
    ("change", "message"),
    [
        pytest.param({"schema": 2}, "schema must be 1", id="schema"),
        pytest.param({"head_sha": ""}, "head_sha must be a text", id="head"),
        pytest.param({"extra": 1}, "keys must be ac, findings, head_sha, schema", id="unknown-key"),
        pytest.param({"findings": {}}, "findings must be a list", id="findings-not-a-list"),
        pytest.param({"ac": {}}, "ac must be a list", id="ac-not-a-list"),
        pytest.param(
            {"ac": [{"key": "ui#1 AC1", "status": "done"}]},
            "ac[0].status must be one of covered, not covered, can't tell",
            id="ac-status",
        ),
        pytest.param(
            {"ac": [{"key": "", "status": "covered"}]}, "ac[0].key must be a text", id="ac-key"
        ),
        pytest.param(
            {"findings": [finding(severity="blocker")]},
            "findings[0].severity must be one of critical, high, medium, low, info",
            id="severity",
        ),
        pytest.param(
            {"findings": [finding(severity=["high"])]},
            "findings[0].severity must be one of critical, high, medium, low, info",
            id="severity-a-list",
        ),
        pytest.param(
            {"findings": [finding(scope={"in": 1})]},
            "findings[0].scope must be one of in, out",
            id="scope-an-object",
        ),
        pytest.param(
            {"findings": [finding(scope="maybe")]},
            "findings[0].scope must be one of in, out",
            id="scope",
        ),
        pytest.param(
            {"findings": [finding(line=0)]},
            "findings[0].line must be a positive integer",
            id="line-zero",
        ),
        pytest.param(
            {"findings": [finding(line=True)]},
            "findings[0].line must be a positive integer",
            id="line-bool",
        ),
        pytest.param(
            {"findings": [finding(title="а\nб")]},
            "findings[0].title must be one line",
            id="title-two-lines",
        ),
        pytest.param(
            {"findings": [finding(path="")]}, "findings[0].path must be a text", id="path"
        ),
        pytest.param(
            {"findings": [finding(decision={"kind": "rejected", "why": "нет"})]},
            "findings[0].decision must be null for a finding in scope",
            id="decision-in-scope",
        ),
        pytest.param(
            {"findings": [finding(scope="out", decision={"kind": "maybe"})]},
            "findings[0].decision.kind must be one of follow-up, accepted, rejected",
            id="decision-kind",
        ),
        pytest.param(
            {"findings": [finding(scope="out", decision={"kind": "rejected", "why": ""})]},
            "findings[0].decision.why must be a text",
            id="decision-without-a-reason",
        ),
        pytest.param(
            {"findings": [finding(scope="out", decision={"kind": "follow-up", "ref": "soon"})]},
            "findings[0].decision.ref must name an issue: api#N, ui#N or owner/name#N",
            id="follow-up-without-a-ref",
        ),
        pytest.param(
            {"findings": [{**finding(), "extra": 1}]},
            "findings[0] keys must be body, category, decision, line, path, scope, severity, title",
            id="finding-unknown-key",
        ),
    ],
)
def test_a_bad_findings_file_is_exit_2(
    tmp_path: Path, change: dict[str, Any], message: str
) -> None:
    plan = tmp_path / "findings.json"
    plan.write_text(json.dumps({**plan_of(), **change}), encoding="utf-8")
    r = run("ui#130", "--findings", str(plan), "--diff", str(DIFF))
    assert r.returncode == 2
    assert error_of(r) == f"{plan}: {message}"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        pytest.param("{", "{plan}: not JSON", id="not-json"),
        pytest.param("[]", "{plan}: must be a JSON object", id="json-array"),
    ],
)
def test_a_findings_file_that_is_no_object_is_exit_2(
    tmp_path: Path, text: str, message: str
) -> None:
    plan = tmp_path / "findings.json"
    plan.write_text(text, encoding="utf-8")
    r = run("ui#130", "--findings", str(plan), "--diff", str(DIFF))
    assert r.returncode == 2
    assert error_of(r).startswith(message.format(plan=plan))


@pytest.mark.parametrize(
    "args",
    [
        pytest.param(["ui#130"], id="no-findings-or-diff"),
        pytest.param(["bad-ref", "--findings", "f", "--diff", "d"], id="bad-ref"),
        pytest.param(
            ["ui#130", "--findings", "/nonexistent", "--diff", "d"], id="no-findings-file"
        ),
    ],
)
def test_usage_is_exit_2(args: list[str]) -> None:
    r = run(*args)
    assert r.returncode == 2
    assert error_of(r) != ""


def test_a_diff_that_names_no_file_is_exit_2(tmp_path: Path) -> None:
    plan = tmp_path / "findings.json"
    plan.write_text(json.dumps(plan_of()), encoding="utf-8")
    diff = tmp_path / "empty.diff"
    diff.write_text("\n", encoding="utf-8")
    r = run("ui#130", "--findings", str(plan), "--diff", str(diff))
    assert r.returncode == 2
    assert error_of(r) == f"{diff}: the diff names no file"


def test_a_missing_diff_is_exit_2(tmp_path: Path) -> None:
    plan = tmp_path / "findings.json"
    plan.write_text(json.dumps(plan_of()), encoding="utf-8")
    r = run("ui#130", "--findings", str(plan), "--diff", "/nonexistent.diff")
    assert r.returncode == 2
    assert error_of(r).startswith("cannot read /nonexistent.diff: ")


# ─── transport: exit 3 and argv ─────────────────────────────────────────────
def args_of(tmp_path: Path) -> list[str]:
    plan = tmp_path / "findings.json"
    plan.write_text(json.dumps(plan_of()), encoding="utf-8")
    return ["ui#130", "--findings", str(plan), "--diff", str(DIFF)]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param(
            'import sys\nsys.stderr.write("gh: boom\\n")\nsys.exit(1)\n',
            "gh api graphql exited 1: gh: boom",
            id="gh-fails",
        ),
        pytest.param(
            'import sys\nsys.stdout.buffer.write(b"\\xff")\n',
            "gh api graphql: cannot read the output: ",
            id="not-utf-8",
        ),
        pytest.param('print("nope")\n', "gh api graphql: response is not JSON: ", id="not-json"),
        pytest.param(
            'print("[]")\n', "gh api graphql: response is not a JSON object", id="json-array"
        ),
        pytest.param(
            'import json, sys\nprint(json.dumps({"errors": [{"message": "no"}]}))\nsys.exit(1)\n',
            "gh api graphql: no",
            id="graphql-errors",
        ),
        pytest.param(
            'import json\nprint(json.dumps({"data": {"viewer": {"login": "a"}, '
            '"repository": {"pullRequest": None}}}))\n',
            "repository.pullRequest: expected an object",
            id="no-pull-request",
        ),
        pytest.param(
            'import json\nprint(json.dumps({"data": {"viewer": {"login": "a"}, '
            '"repository": {"pullRequest": {"id": "x"}}}}))\n',
            "unexpected answer from GitHub: ",
            id="an-answer-of-another-shape",
        ),
    ],
)
def test_a_bad_gh_answer_is_exit_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str, expected: str
) -> None:
    args = args_of(tmp_path)
    prepend_path(monkeypatch, fake_gh(tmp_path / "bin", body))
    r = run(*args)
    assert r.returncode == 3
    assert error_of(r).startswith(expected)


def test_no_gh_is_exit_3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = args_of(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "nothing"))
    r = run(*args)
    assert r.returncode == 3
    assert error_of(r).startswith("cannot run gh: ")


def test_a_gh_timeout_is_exit_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def timed_out(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, 120)

    monkeypatch.setattr(draft.subprocess, "run", timed_out)
    with pytest.raises(SystemExit) as stopped:
        draft.main(args_of(tmp_path))
    assert stopped.value.code == 3
    assert json.loads(capsys.readouterr().err)["error"] == "gh api graphql timed out after 120 s"


def test_a_missing_fixture_is_exit_3(tmp_path: Path) -> None:
    empty = tmp_path / "fixtures"
    empty.mkdir()
    r = run(*args_of(tmp_path), fixtures=empty)
    assert r.returncode == 3
    assert error_of(r).startswith("gh api graphql: cannot read fixture ")


def test_an_empty_seam_is_exit_3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = args_of(tmp_path)
    monkeypatch.setenv(SEAM, "")
    r = run(*args)
    assert r.returncode == 3
    assert error_of(r) == f"{SEAM} is set but empty"


# A `gh` that records its argv and answers each document with a live shape.
RECORDING_GH = """
import json, sys
from pathlib import Path

folder = Path(__file__).parent
with (folder / "argv.jsonl").open("a", encoding="utf-8") as out:
    out.write(json.dumps(sys.argv[1:]) + "\\n")
query = next(a for a in sys.argv if a.startswith("query="))
name = "create" if "addPullRequestReview(" in query else (
    "thread" if "addPullRequestReviewThread(" in query else "state"
)
if name == "state":
    marker = folder / "read-once"
    name = "readback" if marker.exists() else "state"
    marker.touch()
answer = folder / f"{name}.txt"
if answer.exists():
    text = answer.read_text(encoding="utf-8")
    print(text)
    sys.exit(1 if '"errors"' in text else 0)
print((folder / f"{name}.json").read_text(encoding="utf-8"))
"""


def recording_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str = PATH) -> Path:
    bin_dir = fake_gh(tmp_path / "bin", RECORDING_GH)
    (bin_dir / "state.json").write_text(
        json.dumps(example("06-new-draft", "state")), encoding="utf-8"
    )
    (bin_dir / "create.json").write_text(
        json.dumps(example("06-new-draft", "create")), encoding="utf-8"
    )
    (bin_dir / "thread.json").write_text(
        json.dumps(example("06-new-draft", "thread-1")), encoding="utf-8"
    )
    readback = copy.deepcopy(example("06-new-draft", "readback"))
    comment = pull(readback)["reviews"]["nodes"][0]["comments"]["nodes"][0]
    comment["path"] = path
    comment["body"] = "**P1 · high · correctness.** Заголовок\n\nТело."
    pull(readback)["reviews"]["nodes"][0]["body"] = (
        "Итог на `21b8d99b`: critical 0, high 1, medium 0, low 0, info 0. "
        "AC: covered 0, not covered 0, can't tell 0.\n\n"
        "Предложенный вердикт: Request changes. Вердикт и Submit — решение ревьюера."
    )
    (bin_dir / "readback.json").write_text(json.dumps(readback), encoding="utf-8")
    prepend_path(monkeypatch, bin_dir)
    return bin_dir


def test_the_argv_of_an_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Texts go by `-f`: `-F` would read a value that starts with `@` as a file."""
    path = "@scope/a.ts"
    diff = tmp_path / "scoped.diff"
    diff.write_text(
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -41 +41 @@\n-a\n+b\n",
        encoding="utf-8",
    )
    plan = tmp_path / "findings.json"
    plan.write_text(json.dumps(plan_of(finding(path=path))), encoding="utf-8")
    bin_dir = recording_gh(tmp_path, monkeypatch, path)
    r = run("ui#130", "--findings", str(plan), "--diff", str(diff), "--apply")
    assert r.returncode == 0, r.stderr
    calls = [
        json.loads(line)
        for line in (bin_dir / "argv.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(calls) == 4
    assert all(call[:2] == ["api", "graphql"] for call in calls)
    thread = calls[2]
    assert ["-f", f"path={path}"] == thread[thread.index(f"path={path}") - 1 :][:2]
    assert thread[thread.index("line=41") - 1 :][:2] == ["-F", "line=41"]
    body = "body=**P1 · high · correctness.** Заголовок\n\nТело."
    assert ["-f", body] == thread[thread.index(body) - 1 :][:2]
    assert not any("event" in argument for call in calls for argument in call)


def test_a_refused_draft_stops_before_any_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = tmp_path / "findings.json"
    plan.write_text(json.dumps(plan_of(finding())), encoding="utf-8")
    bin_dir = recording_gh(tmp_path, monkeypatch)
    (bin_dir / "create.txt").write_text(
        json.dumps({"errors": [{"message": "User can only have one pending review"}]}),
        encoding="utf-8",
    )
    r = run("ui#130", "--findings", str(plan), "--diff", str(DIFF), "--apply")
    assert r.returncode == 3
    assert error_of(r) == "gh api graphql: User can only have one pending review"
    assert len((bin_dir / "argv.jsonl").read_text(encoding="utf-8").splitlines()) == 2


def test_a_refused_thread_is_failed_and_the_others_go_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = tmp_path / "findings.json"
    plan.write_text(json.dumps(plan_of(finding())), encoding="utf-8")
    bin_dir = recording_gh(tmp_path, monkeypatch)
    (bin_dir / "thread.txt").write_text(
        json.dumps({"errors": [{"message": "was submitted too quickly"}]}), encoding="utf-8"
    )
    r = run("ui#130", "--findings", str(plan), "--diff", str(DIFF), "--apply")
    assert r.returncode == 1
    assert [(t["status"], t["error"]) for t in doc_of(r)["threads"]] == [
        ("failed", "gh api graphql: was submitted too quickly")
    ]
