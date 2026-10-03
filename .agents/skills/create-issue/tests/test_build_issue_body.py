"""Contract tests for scripts/build_issue_body.py.

Offline: no network, no tokens, no CLI invocation. Everything here is either a
pure function call or a subprocess exit code.

Covers: golden bodies per type, the two unconditionally-emitted sections,
fail-closed validation (including keys the emitter does not know), markdown
section forgery, the type-to-label mapping and the CLI arg vector, non-ASCII
passthrough and the language-dependent prose.
"""
# SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/create-issue/tests/test_build_issue_body.py

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "scripts" / "build_issue_body.py"
GOLDEN = HERE / "golden"


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bib = _load(SCRIPT, "build_issue_body")


def run(spec: Any, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "-", *extra],
        input=json.dumps(spec),
        capture_output=True,
        text=True,
    )


def mini(**fields: Any) -> dict[str, Any]:
    """The smallest valid spec; `lang` is always explicit."""
    return {"repo": "a/b", "summary": "x", "lang": "en", **fields}


def headings(markdown: str) -> list[str]:
    return re.findall(r"^## (.+)$", markdown, re.M)


# ─── golden bodies ──────────────────────────────────────────────────────────
STORY: dict[str, Any] = {
    "repo": "acme/sandbox",
    "type": "Story",
    "lang": "en",
    "summary": "[api] Add per-node metrics endpoint",
    "as_role": "an operator",
    "i_want": "per-node metrics",
    "so_that": "I can spot a single bad node",
    "background": (
        "The dashboard aggregates across nodes, so one bad node\n"
        "is invisible until it takes the average down."
    ),
    "scope": ["Add GET /metrics/nodes", "Extend the collector"],
    "acceptance_criteria": ["Endpoint returns per-node rows", "Covered by a test"],
    "dependencies": "None",
    "references": ["https://example.invalid/design"],
}
BUG: dict[str, Any] = {
    "repo": "acme/sandbox",
    "type": "Bug",
    "lang": "en",
    "summary": "[ui] Diff viewer drops the last hunk",
    "environment": "staging, Firefox 141",
    "steps": ["Open a PR with 3 hunks", "Scroll to the bottom"],
    "expected": "All three hunks render",
    "actual": "The third hunk is missing",
    "severity": "High",
    "acceptance_criteria": ["All hunks render"],
}
# STORY and BUG deliberately carry NO kickoff: their goldens are what proves an
# absent block renders nothing at all. Anything kickoff-related goes on this
# third fixture instead of being folded into them.
KICKOFF: dict[str, Any] = {
    "repo": "acme/sandbox — plus acme/shared for the collector client",
    "branch": "fix/<n>-metrics-gate — main is protected, open a PR",
    "problem_shape": (
        "The coverage gate fails because the CI job never runs the collector\nmodule's tests."
    ),
    "read_first": ["docs/ci.md#coverage — which job reports coverage"],
    "before_writing_code": ["make test-collector — reproduces 83.3% locally"],
    "mines": ["never run make baseline locally — it overwrites the stored baseline"],
}
STORY_KICKOFF = dict(STORY, kickoff=KICKOFF)


def golden_body(name: str) -> str:
    """A golden is a SYNC line, a blank line, then the body. Drop exactly those
    two lines and keep the rest byte for byte: no strip, no normalisation. A
    missing golden is an error — it is never written on the fly."""
    text = (GOLDEN / f"{name}.md").read_bytes().decode("utf-8")
    return text.split("\n", 2)[2]


@pytest.mark.parametrize(
    "name,spec",
    [
        ("github-story", STORY),
        ("github-bug", BUG),
        ("github-story-kickoff", STORY_KICKOFF),
    ],
)
def test_golden(name: str, spec: dict[str, Any]) -> None:
    assert bib.render_body(spec) == golden_body(name), f"{name} body drifted"


def test_every_golden_carries_the_sync_header() -> None:
    files = sorted(GOLDEN.glob("*.md"))
    assert files, "no golden files found"
    for path in files:
        first, second, _ = path.read_bytes().decode("utf-8").split("\n", 2)
        assert first == (
            "<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/create-issue/"
            f"tests/golden/{path.name} -->"
        )
        assert second == "", path.name


# ─── the two sections that are never optional ───────────────────────────────
def test_minimal_spec_still_has_dependencies_and_dod() -> None:
    body = bib.render_body(mini())
    assert "## Dependencies" in body
    assert "None" in body
    assert "## DoD Checklist" in body
    assert body.count("- [ ]") == len(bib.DEFAULT_DOD)


def test_default_dod_is_the_team_merge_gate() -> None:
    body = bib.render_body(mini())
    for item in (
        "Gates green (AGENTS.md → Commands)",
        "PR approved, all review threads resolved",
        "Issue status updated on board 12",
    ):
        assert f"- [ ] {item}" in body


def test_explicit_dod_replaces_the_default() -> None:
    body = bib.render_body(mini(dod=["Staging sign-off"]))
    assert "- [ ] Staging sign-off" in body
    assert "Gates green" not in body


# ─── fail closed ────────────────────────────────────────────────────────────
# Keys of the sibling tooling this skill was ported from. Built from two halves
# so the self-check grep for the other host's name stays empty over this file.
FOREIGN_BLOCK = "git" + "lab"


@pytest.mark.parametrize(
    "spec,needle",
    [
        (mini(host_kind="github"), "host_kind"),
        (mini(api_host="github.com"), "api_host"),
        (mini(github={"issue_type": "Feature"}), "github"),
        (mini(**{FOREIGN_BLOCK: {"weight": 3}}), FOREIGN_BLOCK),
        (mini(typo_key=1), "typo_key"),
        (mini(estimate_hours=3), "estimate_hours"),  # estimates are out of scope
        (mini(severity="High"), "severity"),
        (mini(type="Docs", environment="staging"), "environment"),
        (mini(type="Epic"), "type"),
        ({"summary": "x"}, "repo"),
        ({"repo": "a/b"}, "summary"),
        (["not", "an", "object"], "JSON object"),
    ],
)
def test_invalid_spec_exits_2_and_names_the_problem(spec: Any, needle: str) -> None:
    r = run(spec)
    assert r.returncode == 2
    assert r.stdout == "", "stdout must carry the deliverable only"
    assert needle in json.loads(r.stderr)["error"]


def test_valid_spec_writes_nothing_to_stderr() -> None:
    r = run(mini())
    assert r.returncode == 0
    assert r.stderr == ""


def test_unreadable_spec_file_exits_2(tmp_path: Path) -> None:
    r = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path / "missing.json")],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 2
    assert "cannot read spec" in json.loads(r.stderr)["error"]


def test_invalid_json_exits_2() -> None:
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "-"],
        input="{not json",
        capture_output=True,
        text=True,
    )
    assert r.returncode == 2
    assert "not valid JSON" in json.loads(r.stderr)["error"]


# ─── value types: a wrong type is an exit 2 with a message, never a traceback ─
@pytest.mark.parametrize(
    "spec,needle",
    [
        (mini(type=["Bug"]), '"type" must be a string'),
        (mini(type=None), '"type" must be a string'),
        (mini(summary=5), '"summary" must be a string'),
        (mini(repo=["a/b"]), '"repo" must be a string'),
        (mini(background=["x"]), '"background" must be a string'),
        (mini(milestone=3), '"milestone" must be a string'),
        (mini(lang=1), '"lang" must be a string'),
        # a string where a list belongs must never be split per character
        (mini(scope="one string"), '"scope" must be a list of strings'),
        (mini(labels="bug"), '"labels" must be a list of strings'),
        (mini(assignees="octocat"), '"assignees" must be a list of strings'),
        (mini(acceptance_criteria="done"), '"acceptance_criteria" must be a list'),
        (mini(references="https://example.invalid"), '"references" must be a list'),
        (mini(dod="Gates green"), '"dod" must be a list'),
        (mini(type="Bug", steps="reproduce it"), '"steps" must be a list'),
        (mini(scope=["fine", 7]), '"scope" must be a list of strings'),
        (mini(scope={"a": "b"}), '"scope" must be a list of strings'),
        (mini(dependencies=5), '"dependencies" must be a string, a list or an object'),
        (mini(dependencies=["fine", 5]), '"dependencies" must be a list of strings'),
        (mini(dependencies={"Blocks": 5}), "dependencies.Blocks"),
        (mini(kickoff="text"), '"kickoff" must be an object'),
        (mini(kickoff={"repo": 5}), "kickoff.repo"),
        (mini(kickoff={"mines": "a string"}), "kickoff.mines"),
        # blank entries are never meaningful (a blank label would become a bare flag)
        (mini(labels=[""]), "must not contain blank entries"),
        (mini(assignees=["  "]), "must not contain blank entries"),
        (mini(scope=["fine", ""]), "must not contain blank entries"),
        (mini(acceptance_criteria=["\t"]), "must not contain blank entries"),
        (mini(dependencies=[" "]), "must not contain blank entries"),
        (mini(dependencies={"Blocks": [""]}), "must not contain blank entries"),
        (mini(kickoff={"mines": [" "]}), "must not contain blank entries"),
        # repo is exactly OWNER/REPO
        (mini(repo="github.com/o/r"), "OWNER/REPO"),
        (mini(repo="https://github.com/o/r"), "OWNER/REPO"),
        (mini(repo="o/r/x"), "OWNER/REPO"),
        (mini(repo="o"), "OWNER/REPO"),
        (mini(repo="/r"), "OWNER/REPO"),
        (mini(repo="o/"), "OWNER/REPO"),
        (mini(repo="o/.."), "OWNER/REPO"),
        (mini(repo="o r/x"), "OWNER/REPO"),
        (mini(repo="o/r\n"), "OWNER/REPO"),
        # one argv element per line
        (mini(summary="two\nlines"), "single line"),
        (mini(labels=["a\nb"]), "single lines"),
        # a layout never silently drops the other layout's fields
        (mini(type="Bug", background="why"), "story-only"),
        (mini(type="Bug", scope=["x"]), "story-only"),
        (mini(type="Bug", as_role="a user"), "story-only"),
    ],
)
def test_wrong_value_types_exit_2_with_a_message(spec: Any, needle: str) -> None:
    r = run(spec)
    assert r.returncode == 2
    assert r.stdout == ""
    assert "Traceback" not in r.stderr
    assert needle in json.loads(r.stderr)["error"]


# ─── code fences ────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "spec",
    [
        mini(background="before\n```bash\nrun it"),
        mini(background="before\n~~~\nrun it"),
        mini(background="````\nlonger fence\n```\nstill open"),
        mini(dependencies="```\nopen"),
        mini(dependencies={"Blocks": "~~~\nopen"}),
        mini(kickoff={"problem_shape": "```\nopen"}),
        mini(kickoff={"repo": "~~~py\nopen"}),
    ],
)
def test_an_unclosed_code_fence_is_rejected(spec: Any) -> None:
    """An open fence turns Dependencies, DoD and everything after it into code."""
    r = run(spec)
    assert r.returncode == 2
    assert "unclosed code fence" in json.loads(r.stderr)["error"]


@pytest.mark.parametrize(
    "text",
    [
        "before\n```bash\nrun it\n```\nafter",
        "~~~\nx\n~~~",
        "````\ninner ```\n````",  # a longer fence is closed only by a long one
        "inline ```code``` span",  # not a fence
        "```a`b\nnot a fence either: the info string has a backtick",
        "   ```\n   x\n   ```",
    ],
)
def test_balanced_fences_are_allowed(text: str) -> None:
    r = run(mini(background=text))
    assert r.returncode == 0, r.stderr
    assert headings(r.stdout) == ["Background", "Dependencies", "DoD Checklist"]


# ─── markdown section forgery ───────────────────────────────────────────────
def test_user_text_cannot_forge_a_section() -> None:
    body = bib.render_body(mini(background="legit\n## Acceptance Criteria\n- [x] pwned"))
    assert headings(body) == ["Background", "Dependencies", "DoD Checklist"]
    # The text survives — we escape structure, we do not censor content. What
    # must not survive is the *rendering*: no line may begin a task item.
    assert "\\- [x] pwned" in body
    assert not any(re.match(r"\s*- \[[ x]\]", ln) and "pwned" in ln for ln in body.split("\n"))


def test_setext_underline_cannot_forge_a_heading() -> None:
    # `---` on the line after text makes that text an h2. Trailing whitespace
    # must not slip past the guard.
    for rule in ("---", "--- ", "===", "===\t"):
        body = bib.render_body(mini(background=f"innocent line\n{rule}"))
        assert f"\n{rule}" not in body, f"unescaped setext rule {rule!r}"


def test_list_item_newlines_collapse() -> None:
    body = bib.render_body(mini(acceptance_criteria=["first\n## Injected\nstill me"]))
    assert headings(body) == ["Acceptance Criteria", "Dependencies", "DoD Checklist"]
    assert "- [ ] first ## Injected still me" in body


# ─── type → label, and the arg vector ───────────────────────────────────────
def labels_of(args: list[str]) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args) if a == "--label"]


@pytest.mark.parametrize(
    "typ,label",
    [
        ("Bug", "bug"),
        ("Story", "enhancement"),
        ("Task", "enhancement"),
        ("Docs", "documentation"),
        (None, "enhancement"),  # no `type` means Story
    ],
)
def test_type_becomes_the_repo_label(typ: str | None, label: str) -> None:
    spec = mini() if typ is None else mini(type=typ)
    r = run(spec, "--emit-args")
    assert r.returncode == 0, r.stderr
    assert labels_of(json.loads(r.stdout)) == [label]


def test_no_type_prefixed_label_is_ever_emitted() -> None:
    for typ in ("Story", "Bug", "Task", "Docs"):
        r = run(mini(type=typ), "--emit-args")
        assert not any(name.startswith("type:") for name in labels_of(json.loads(r.stdout)))


def test_spec_labels_follow_the_type_label_without_duplicates() -> None:
    r = run(
        mini(type="Task", labels=["good first issue", "enhancement", "good first issue"]),
        "--emit-args",
    )
    assert labels_of(json.loads(r.stdout)) == ["enhancement", "good first issue"]


def test_arg_vector_carries_title_assignees_and_milestone() -> None:
    r = run(
        mini(summary="[api] Do it", assignees=["octocat", "hubot"], milestone="Sprint 3"),
        "--emit-args",
    )
    args = json.loads(r.stdout)
    assert args[:2] == ["--title", "[api] Do it"]
    assert args.count("--assignee") == 2
    assert args[args.index("--milestone") + 1] == "Sprint 3"


def test_docs_type_renders_with_the_task_layout() -> None:
    body = bib.render_body(
        mini(type="Docs", background="why", scope=["write it"], acceptance_criteria=["done"])
    )
    assert headings(body) == [
        "Background",
        "Scope",
        "Acceptance Criteria",
        "Dependencies",
        "DoD Checklist",
    ]


# ─── i18n: body language follows the repository ─────────────────────────────
def test_non_ascii_passthrough() -> None:
    body = bib.render_body(
        mini(
            background="Немецкие умляуты: größer. Кириллица цела.",
            acceptance_criteria=["Кнопка «Сохранить» работает"],
        )
    )
    assert "größer" in body
    assert "«Сохранить»" in body


def test_kickoff_headings_follow_the_body_language() -> None:
    """Kickoff is prose the emitter writes itself, so `lang` has to reach it —
    otherwise a Russian repository gets a Russian body with an English briefing
    bolted on the end."""
    fields = {"repo": "grp/proj", "mines": ["не запускать baseline локально"]}
    ru = bib.render_body(mini(lang="ru", kickoff=fields))
    assert "## Kickoff — с чего начать" in ru
    assert "**Репозиторий:** grp/proj" in ru
    assert "## Kickoff — where to start" in bib.render_body(mini(lang="en", kickoff=fields))
    # An unknown language degrades to English rather than raising.
    assert "## Kickoff — where to start" in bib.render_body(mini(lang="eo", kickoff=fields))


def test_omitted_lang_is_english() -> None:
    spec = mini(kickoff={"repo": "grp/proj"})
    del spec["lang"]
    assert "## Kickoff — where to start" in bib.render_body(spec)


# ─── kickoff ────────────────────────────────────────────────────────────────
def test_absent_or_empty_kickoff_renders_no_heading() -> None:
    """The reason STORY/BUG keep their goldens: every issue written without the
    field must re-render byte-identically."""
    for spec in (mini(), mini(kickoff={}), mini(kickoff=None)):
        assert "Kickoff" not in bib.render_body(spec)


def test_kickoff_text_cannot_forge_a_section() -> None:
    """Kickoff is caller prose like any other field, so it goes through the same
    guard — a `mines` entry is the most likely place to paste a fenced command
    block with a `#` comment in it."""
    body = bib.render_body(
        mini(
            kickoff={
                "problem_shape": "real\n## Acceptance Criteria\n- [x] pwned",
                "mines": ["one\n## Injected\nstill me"],
            }
        )
    )
    assert headings(body) == ["Dependencies", "DoD Checklist", "Kickoff — where to start"]
    assert "- one ## Injected still me" in body


def test_kickoff_is_a_briefing_not_a_checklist() -> None:
    """Checkboxes in the briefing would read as work items and get ticked; the
    only checklists in a body are Acceptance Criteria and DoD."""
    body = bib.render_body(dict(STORY_KICKOFF))
    section = body.split("## Kickoff — where to start", 1)[1]
    assert "- [ ]" not in section


def test_unknown_kickoff_key_is_rejected_not_ignored() -> None:
    """A misspelt key would otherwise drop the hazard the composer meant to pass
    on, silently and with exit 0."""
    r = run(mini(kickoff={"repo": "a/b", "landmines": ["typo"]}))
    assert r.returncode == 2
    assert "landmines" in json.loads(r.stderr)["error"]


def test_kickoff_survives_the_cli_round_trip() -> None:
    r = run(dict(STORY_KICKOFF))
    assert r.returncode == 0, r.stderr
    assert "## Kickoff — where to start" in r.stdout


# ─── dependencies: prose, flat list, labelled groups ────────────────────────
def _deps_section(dep: Any) -> str:
    body = bib.render_body(dict(STORY, dependencies=dep))
    return str(body.split("## Dependencies\n\n", 1)[1].split("\n## ", 1)[0])


def test_dependencies_string_is_unchanged() -> None:
    assert _deps_section("None").strip() == "None"


def test_dependencies_list_renders_bullets() -> None:
    assert _deps_section(["#1 blocks", "#2 relates"]).startswith("- #1 blocks")


def test_dependencies_groups_are_labelled_in_order() -> None:
    sec = _deps_section({"Blocking": ["#1"], "Not blocking": ["#2"]})
    assert sec.index("**Blocking:**") < sec.index("**Not blocking:**")
    assert "- #2" in sec
