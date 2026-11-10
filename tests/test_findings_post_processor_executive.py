"""Executive summary block at the start of the review body (#316)."""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import pytest
from markdown_it import MarkdownIt

from app.modules.reviews.application.findings_post_processor import (
    FindingsPostProcessor,
    ProcessedReviewOutput,
    finalize_stored_review_body,
    format_body_only_overflow_footer,
)
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.review_ignore import (
    IgnoreMatcher,
    apply_ignore,
    parse_review_ignore,
)
from app.modules.reviews.application.run_coverage import (
    CoverageStatus,
    RunCoverage,
    project_run_coverage,
    restore_run_coverage,
    snapshot_vcs_file,
)
from app.modules.reviews.application.store_review_output import hashed_findings
from app.modules.reviews.application.verdict import (
    findings_hash,
    publish_message_id,
)
from tests.findings_post_processor_fakes import (
    _body_only_accounted_in_review_body,
    _diff_map_from_vcs,
    finding,
    output,
)

_EXECUTIVE_PATCH = "@@ -1 +1 @@\n-old\n+new"
_IGNORE_PRIVATE = "```review-ignore\nprivate/**\n```\n"
_INCOMPLETE = "проверка неполная"
_NOTHING = "нет доступных для анализа изменений"


def _text_change(path: str = "app/example.py") -> DiffSnapshot:
    """A reviewed text change; ``replace`` turns it into one kind of omission."""
    return DiffSnapshot(
        path,
        _EXECUTIVE_PATCH,
        "d" * 40,
        additions=1,
        deletions=1,
        changes=2,
        review_patch=_EXECUTIVE_PATCH,
    )


def _truncated_change(path: str = "src/cut.py") -> DiffSnapshot:
    return replace(_text_change(path), additions=2, changes=3)


def _binary_change() -> DiffSnapshot:
    return DiffSnapshot("assets/logo.png", None, "d" * 40, omission_reason="binary")


def _too_large_change(path: str = "src/huge.py") -> DiffSnapshot:
    return DiffSnapshot(path, None, "d" * 40, omission_reason="too_large")


def _unresolved_rename(path: str = "src/moved.py") -> DiffSnapshot:
    """A rename with a patch, sent by the provider without its previous name."""
    return replace(_text_change(path), status="renamed", previous_filename=None)


def _process_snapshots(
    snapshots: list[DiffSnapshot],
    *findings: dict[str, object],
    rules: str = "",
    summary_only: bool = False,
    change_summary: tuple[str, ...] = (),
) -> ProcessedReviewOutput:
    """Process as a run does: coverage projected under the policy, saved and read back."""
    diff_map = _diff_map_from_vcs(*(snapshot_vcs_file(item) for item in snapshots))
    ignore = apply_ignore(IgnoreMatcher(parse_review_ignore(rules)), diff_map)
    saved = project_run_coverage(snapshots, diff_map, ignore=ignore).to_json()
    coverage = restore_run_coverage(saved, snapshots=snapshots, diff_map=diff_map)
    assert coverage is not None
    return FindingsPostProcessor.from_default_patterns().process(
        output(*findings),
        diff_map=diff_map,
        checked_paths=frozenset(
            item.filename for item in snapshots if item.review_patch is not None
        ),
        rule_names=frozenset(),
        coverage=coverage,
        summary_only=summary_only,
        change_summary=change_summary,
    )


def _what_to_check(result: ProcessedReviewOutput) -> list[str]:
    head = result.review_body[: result.review_body.index("\n\n## Review summary")]
    _, marker, lines = head.partition("**What to check:**\n")
    return lines.splitlines() if marker else []


@pytest.mark.parametrize(
    ("snapshots", "findings", "summary_only", "verdict", "risk"),
    [
        ([_text_change()], [finding(severity="high")], False, "blocking", "High"),
        ([_text_change()], [finding(severity="medium")], False, "attention", "Medium"),
        ([_text_change()], [], False, "clean", "Low"),
        ([_text_change(), _truncated_change()], [], False, _INCOMPLETE, "Medium"),
        ([replace(_text_change(), summary_only=True)], [], True, _INCOMPLETE, "Medium"),
        ([_text_change(), _binary_change()], [], False, "clean", "Low"),
        ([_binary_change()], [], False, _NOTHING, "Low"),
        ([_too_large_change()], [], False, _NOTHING, "Medium"),
        ([_text_change()], [finding(path="not/in/diff.py")], False, _INCOMPLETE, "Medium"),
        ([_text_change(), _text_change("src\\moved.py")], [], False, _INCOMPLETE, "Medium"),
        ([_text_change(), _unresolved_rename()], [], False, _INCOMPLETE, "Medium"),
    ],
)
def test_executive_risk_score_table(
    snapshots: list[DiffSnapshot],
    findings: list[dict[str, object]],
    summary_only: bool,
    verdict: str,
    risk: str,
) -> None:
    result = _process_snapshots(snapshots, *findings, summary_only=summary_only)

    assert result.review_body.startswith(
        f"## Executive summary\n\n**Verdict:** {verdict} · **Risk Score:** {risk}\n"
    )
    assert result.review_body.count("**Verdict:**") == 1


def test_executive_risk_is_medium_when_the_classification_is_unavailable() -> None:
    result = FindingsPostProcessor.from_default_patterns().process(
        output(),
        diff_map=_diff_map_from_vcs(snapshot_vcs_file(_text_change())),
        checked_paths=frozenset({"app/example.py"}),
        rule_names=frozenset(),
    )

    assert result.review_body.startswith(
        f"## Executive summary\n\n**Verdict:** {_INCOMPLETE} · **Risk Score:** Medium\n"
    )


def test_executive_block_without_a_checklist_opens_the_unchanged_review_summary() -> None:
    result = _process_snapshots([_text_change()])

    assert result.review_body == "\n".join(
        (
            "## Executive summary",
            "",
            "**Verdict:** clean · **Risk Score:** Low",
            "",
            "## Review summary",
            "",
            "**Risk summary:** critical: 0, high: 0, medium: 0, low: 0, info: 0",
            "**Checked files:**",
            "- app/example.py",
            "",
            "**Main issue:** Подтверждённых замечаний нет.",
            "",
            "**Done well:** Проанализировано файлов с текстовыми изменениями: 1; "
            "исключено файлов: 0.",
            "",
            "**Effort:** none",
        )
    )


def test_executive_checklist_names_unreviewed_files_by_reason() -> None:
    result = _process_snapshots(
        [
            _text_change(),
            _truncated_change(),
            replace(_text_change("src/broken.py"), patch="@@ -1 +1 @@\n?invalid"),
            DiffSnapshot("src/run.sh", None, "d" * 40, "changed"),
            _too_large_change(),
            replace(_text_change("src/partial.py"), review_patch="@@ -1 +1 @@\n-old\n+partial"),
            _text_change("src\\bad.py"),
            _unresolved_rename(),
            _binary_change(),
        ]
    )

    assert _what_to_check(result) == [
        "- Не проверено — не установлена идентичность файла (1)",
        "- Не проверено — Отсутствует подтверждённый patch (1): `src/run.sh`",
        "- Не проверено — patch повреждён (1): `src/broken.py`",
        "- Не проверено — patch неполон (1): `src/cut.py`",
        "- Не проверено — модель получила не весь patch (1): `src/partial.py`",
        "- Не проверено — Превышен лимит размера, содержимое не проверено (1): `src/huge.py`",
        "- Не проверено — неподдерживаемый путь (1)",
    ]


@pytest.mark.parametrize(
    ("file_count", "tail"),
    [
        (5, ""),
        # The saved coverage keeps 20 items of the 25: the count stays exact.
        (25, " и ещё 20"),
    ],
)
def test_executive_checklist_folds_a_long_list(file_count: int, tail: str) -> None:
    snapshots = [_truncated_change(f"c{n:02}.py") for n in range(file_count)]

    assert _what_to_check(_process_snapshots([_text_change(), *snapshots])) == [
        f"- Не проверено — patch неполон ({file_count}): "
        f"`c00.py`, `c01.py`, `c02.py`, `c03.py`, `c04.py`{tail}"
    ]


def test_executive_checklist_asks_to_confirm_critical_and_high_findings() -> None:
    result = _process_snapshots(
        [_text_change()],
        finding(severity="high", title="Race"),
        finding(severity="critical", title="Leak"),
        finding(severity="medium", title="Weak check"),
    )

    assert _what_to_check(result) == [
        "- Подтвердите находки critical/high (2): **Leak** (`app/example.py:12`), "
        "**Race** (`app/example.py:12`)",
    ]


def test_executive_checklist_names_changed_migrations_ci_and_manifests() -> None:
    paths = (
        "alembic/versions/0040_x.py",
        "backend/db/migrate/1_add.rb",
        "migrations/0001.sql",
        ".github/workflows/ci.yml",
        ".gitlab-ci.yml",
        "web/package.json",
        "pyproject.toml",
        "requirements-dev.txt",
        # Lookalikes.
        "docs/migrations.md",
        ".github/README.md",
        "ci.yml",
        "src/package.json.md",
    )

    assert _what_to_check(_process_snapshots([_text_change(path) for path in paths])) == [
        "- Изменённые миграции (3): `alembic/versions/0040_x.py`, `backend/db/migrate/1_add.rb`, "
        "`migrations/0001.sql`",
        "- Изменённые CI-workflows (2): `.github/workflows/ci.yml`, `.gitlab-ci.yml`",
        "- Изменённые манифесты зависимостей (3): `pyproject.toml`, `requirements-dev.txt`, "
        "`web/package.json`",
    ]


@pytest.mark.parametrize(
    ("old_path", "new_path", "line"),
    [
        (
            "alembic/versions/001.py",
            "README.md",
            "- Изменённые миграции (1): `alembic/versions/001.py`",
        ),
        (
            ".github/workflows/ci.yml",
            "docs/ci.yml",
            "- Изменённые CI-workflows (1): `.github/workflows/ci.yml`",
        ),
        (
            "docs/notes.md",
            "migrations/0002.sql",
            "- Изменённые миграции (1): `migrations/0002.sql`",
        ),
    ],
)
def test_executive_checklist_names_a_renamed_file_by_the_path_that_matched(
    old_path: str, new_path: str, line: str
) -> None:
    renamed = replace(_text_change(new_path), status="renamed", previous_filename=old_path)

    assert _what_to_check(_process_snapshots([_text_change(), renamed])) == [line]


@pytest.mark.parametrize(
    ("snapshots", "line"),
    [
        (
            [*(_text_change(f"src\\moved{n:02}.py") for n in range(20)), _truncated_change()],
            "- Не проверено — patch неполон (1): `src/cut.py`",
        ),
        (
            [
                *(_text_change(f"src\\moved{n:02}.py") for n in range(20)),
                *(_truncated_change(f"src/cut{n}.py") for n in range(3)),
            ],
            "- Не проверено — patch неполон (3): `src/cut0.py`, `src/cut1.py`, `src/cut2.py`",
        ),
        (
            [
                *(_truncated_change(f"src/cut{n:02}.py") for n in range(25)),
                _too_large_change("z/huge.py"),
            ],
            "- Не проверено — Превышен лимит размера, содержимое не проверено (1): `z/huge.py`",
        ),
    ],
)
def test_executive_checklist_keeps_a_name_for_every_reason_past_twenty_saved_reasons(
    snapshots: list[DiffSnapshot], line: str
) -> None:
    # Saved coverage keeps 20 reasons; the checklist names files from that window.
    assert line in _what_to_check(_process_snapshots([_text_change(), *snapshots]))


def test_executive_checklist_only_counts_sensitive_files_when_the_policy_hides_names() -> None:
    migration = "alembic/versions/1.py"
    result = _process_snapshots(
        [_text_change(), _text_change(migration), _text_change("private/2.py")],
        rules=_IGNORE_PRIVATE,
    )

    assert _what_to_check(result) == ["- Изменённые миграции (1)"]
    assert migration not in result.review_body


def test_executive_checklist_only_counts_sensitive_files_when_the_policy_is_unknown() -> None:
    migration = "alembic/versions/1.py"
    unknown_policy = FindingsPostProcessor.from_default_patterns().process(
        output(),
        diff_map=_diff_map_from_vcs(snapshot_vcs_file(_text_change(migration))),
        checked_paths=frozenset({migration}),
        rule_names=frozenset(),
    )

    assert _what_to_check(unknown_policy) == [
        "- Не проверено — нет достоверного сохранённого покрытия (1)",
        "- Изменённые миграции (1)",
    ]
    assert migration not in unknown_policy.review_body


def test_executive_ignored_file_neither_raises_the_risk_nor_shows_its_name() -> None:
    result = _process_snapshots(
        [_text_change(), _truncated_change("private/cut.py")], rules=_IGNORE_PRIVATE
    )

    assert result.review_body.startswith(
        "## Executive summary\n\n**Verdict:** clean · **Risk Score:** Low\n"
    )
    assert "private/cut.py" not in result.review_body


@pytest.mark.parametrize(
    ("snapshot", "reason"),
    [
        (_text_change("src\\moved.py"), "неподдерживаемый путь"),
        (_unresolved_rename(), "не установлена идентичность файла"),
        (_unresolved_rename("alembic/versions/moved.py"), "не установлена идентичность файла"),
    ],
)
def test_executive_unusable_names_appear_only_as_a_number(
    snapshot: DiffSnapshot, reason: str
) -> None:
    result = _process_snapshots([_text_change(), snapshot])

    assert _what_to_check(result) == [f"- Не проверено — {reason} (1)"]
    assert "moved.py" not in result.review_body


@pytest.mark.parametrize(
    ("snapshot", "reason", "severity"),
    [
        (_text_change("src\\moved.py"), "неподдерживаемый путь", "high"),
        (_unresolved_rename(), "не установлена идентичность файла", "critical"),
    ],
)
def test_executive_finding_on_an_unusable_name_stays_counted_without_the_name(
    snapshot: DiffSnapshot, reason: str, severity: str
) -> None:
    result = _process_snapshots(
        [_text_change(), snapshot],
        finding(
            path=snapshot.filename,
            severity=severity,
            title="Leak in moved.py",
            body="The token stays in moved.py.",
        ),
    )

    assert "moved.py" not in result.review_body
    assert result.review_body.startswith(
        "## Executive summary\n\n**Verdict:** blocking · **Risk Score:** High\n"
    )
    assert _what_to_check(result) == [
        f"- Не проверено — {reason} (1)",
        f"- Подтвердите находки critical/high (1): **Находка** (имя файла скрыто, {severity})",
    ]
    assert result.review_body.endswith(f"- **Находка** (имя файла скрыто, {severity})")
    assert [item.finding.path for item in result.body_only] == [snapshot.filename]
    assert f"{severity}: 1," in result.review_body


def test_executive_text_of_another_finding_does_not_repeat_an_unusable_name() -> None:
    result = _process_snapshots(
        [_text_change(), _unresolved_rename()],
        finding(
            severity="high",
            title="Moved from src/moved.py",
            body="Compare with ./src/moved.py.",
        ),
    )

    assert "src/moved.py" not in result.review_body
    assert _what_to_check(result)[1] == (
        "- Подтвердите находки critical/high (1): "
        "**Moved from имя файла скрыто** (`app/example.py:12`)"
    )
    assert result.review_body.endswith(
        "- **Moved from имя файла скрыто** (`app/example.py:12`): Compare with имя файла скрыто."
    )


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        ("Fix utils.py now", "Fix имя файла скрыто now"),
        ("See `utils.py`.", "See `имя файла скрыто`."),
        ("See utils.py.", "See имя файла скрыто."),
        ("See utils.py:3", "See имя файла скрыто:3"),
        ("See ./utils.py", "See имя файла скрыто"),
        ("See /utils.py", "See имя файла скрыто"),
        ("Compare app/utils.py", "Compare app/utils.py"),
        ("Compare app\\utils.py", "Compare app\\utils.py"),
        ("Compare old-utils.py", "Compare old-utils.py"),
        ("Compare old.utils.py", "Compare old.utils.py"),
        ("Compare utils.py.bak", "Compare utils.py.bak"),
        ("Compare utils.py-old", "Compare utils.py-old"),
        ("Compare utils.py/tests", "Compare utils.py/tests"),
    ],
)
def test_executive_text_loses_an_unusable_name_only_where_it_is_a_whole_path(
    text: str, shown: str
) -> None:
    result = _process_snapshots(
        [_text_change(), _unresolved_rename("utils.py")],
        finding(severity="high", title="Note", body=text),
    )

    assert result.review_body.endswith(f"- **Note** (`app/example.py:12`): {shown}")


def test_executive_store_time_repack_keeps_an_unusable_name_out_of_the_body() -> None:
    hidden = "src/moved.py"
    findings = (
        finding(path=hidden, title="Note on moved.py", confidence=0.4),
        *(
            finding(title=f"Note {n} on {hidden}", confidence=0.4, body=f"{hidden} {'x' * 600}")
            for n in range(9)
        ),
    )
    # Checked files fill the budget, so the footer has no room and the body is packed again.
    fillers = [_text_change(f"src/file{n:02}.py") for n in range(90)]
    snapshots = [_text_change(), _unresolved_rename(hidden), *fillers]
    processed = _process_snapshots(snapshots, *findings)
    run_url = "https://example.test/runs/1"
    footer = format_body_only_overflow_footer(processed.omitted_body_only_count, run_url)
    assert len(processed.review_body) + len(footer) > 4000

    final = finalize_stored_review_body(
        output(*findings),
        processed,
        checked_paths=frozenset(item.filename for item in snapshots),
        severity_threshold="all",
        below_threshold=0,
        run_url=run_url,
    )

    assert "moved.py" not in final.review_body
    assert "\n- **Находка** (имя файла скрыто, low)\n" in final.review_body
    assert len(final.review_body) <= 4000


def test_executive_block_stays_within_its_limit_with_every_kind_of_line() -> None:
    names = [f"{'d' * 180}{n}" for n in range(6)]
    snapshots = [
        _text_change(),
        *(_truncated_change(f"src/{name}.py") for name in names),
        *(_too_large_change(f"big/{name}.py") for name in names),
        *(DiffSnapshot(f"gone/{name}.py", None, "d" * 40, "changed") for name in names),
        *(_text_change(f"alembic/versions/{name}.py") for name in names),
        *(_text_change(f".github/workflows/{name}.yml") for name in names),
        *(_text_change(f"{name}/pyproject.toml") for name in names),
    ]
    findings = (finding(severity="high", title="T" * 70 + str(n)) for n in range(6))

    result = _process_snapshots(snapshots, *findings)

    assert len(result.executive_block) <= 1500
    assert len(_what_to_check(result)) == 7
    assert "- Не проверено — patch неполон (6)" in result.executive_block
    assert "- Подтвердите находки critical/high (6)" in result.executive_block


def test_executive_block_keeps_a_backtick_in_a_name_inside_its_code_span() -> None:
    result = _process_snapshots([_text_change(), _truncated_change("src/a`b.py")])

    assert _what_to_check(result) == ["- Не проверено — patch неполон (1): `` src/a`b.py ``"]


def test_executive_block_survives_the_store_time_repack_within_the_body_budget() -> None:
    findings = tuple(finding(title=f"Note {n}", confidence=0.4, body="x" * 600) for n in range(10))
    snapshots = [_text_change(), *(_truncated_change(f"src/cut{n}.py") for n in range(8))]
    processed = _process_snapshots(snapshots, *findings)
    assert processed.omitted_body_only_count > 0

    final = finalize_stored_review_body(
        output(*findings),
        processed,
        checked_paths=frozenset({"app/example.py"}),
        severity_threshold="all",
        below_threshold=0,
        run_url="https://example.test/runs/1",
    )

    assert final.review_body.startswith(
        "## Executive summary\n\n"
        "**Verdict:** attention · **Risk Score:** Medium\n\n"
        "**What to check:**\n"
        "- Не проверено — patch неполон (8): `src/cut0.py`, `src/cut1.py`, `src/cut2.py`, "
        "`src/cut3.py`, `src/cut4.py` и ещё 3\n\n"
        "## Review summary\n"
    )
    assert final.review_body.count("**Verdict:**") == 1
    assert len(final.review_body) <= 4000
    assert _body_only_accounted_in_review_body(final.review_body) == 10


@pytest.mark.parametrize(
    ("snapshots", "change_summary"),
    [
        ([_text_change(), _truncated_change(), _text_change("pyproject.toml")], ()),
        ([_text_change()], ("Adds a purge job for stale runs.",)),
    ],
    ids=["checklist", "change_summary"],
)
def test_executive_block_changes_neither_the_findings_hash_nor_the_publish_message(
    snapshots: list[DiffSnapshot], change_summary: tuple[str, ...]
) -> None:
    findings = (finding(severity="high"),)
    plain = _process_snapshots([_text_change()], *findings)
    other = _process_snapshots(snapshots, *findings, change_summary=change_summary)
    assert plain.review_body != other.review_body

    plain_hash = findings_hash(hashed_findings(plain))
    assert findings_hash(hashed_findings(other)) == plain_hash
    run_id = UUID(int=7)
    assert publish_message_id(run_id, "c" * 40, plain_hash) == publish_message_id(
        run_id, "c" * 40, findings_hash(hashed_findings(other))
    )


def test_executive_block_renders_the_same_bytes_for_the_same_files_in_any_order() -> None:
    # The stored body is compared on republish.
    findings = (finding(severity="high"),)
    busy = [_text_change(), _truncated_change(), _text_change("pyproject.toml")]
    rendered = _process_snapshots(busy, *findings)

    assert rendered.review_body == _process_snapshots(busy, *findings).review_body
    assert (
        _process_snapshots(list(reversed(busy)), *findings).executive_block
        == rendered.executive_block
    )


_SIGNED = "**What changed** (автоописание, не замечание): "


@pytest.mark.parametrize(
    ("snapshots", "verdict", "checklist"),
    [
        ([_text_change()], "clean · **Risk Score:** Low", ""),
        (
            [_text_change(), _truncated_change()],
            f"{_INCOMPLETE} · **Risk Score:** Medium",
            "\n\n**What to check:**\n- Не проверено — patch неполон (1): `src/cut.py`",
        ),
    ],
    ids=["no-checklist", "checklist"],
)
def test_executive_change_summary_is_signed_between_the_verdict_and_the_checklist(
    snapshots: list[DiffSnapshot], verdict: str, checklist: str
) -> None:
    result = _process_snapshots(
        snapshots, change_summary=("Adds a purge job.", " Schedules it once a day. ")
    )

    assert result.executive_block == (
        f"## Executive summary\n\n**Verdict:** {verdict}\n\n"
        f"{_SIGNED}Adds a purge job. Schedules it once a day.{checklist}"
    )
    assert result.review_body.startswith(f"{result.executive_block}\n\n## Review summary\n")
    assert result.change_summary_shown == 2


def test_executive_change_summary_does_not_repeat_an_unusable_name() -> None:
    result = _process_snapshots(
        [_text_change(), _unresolved_rename()],
        change_summary=("Moves src/moved.py next to ./src/moved.py.",),
    )

    assert "moved.py" not in result.review_body
    assert f"{_SIGNED}Moves имя файла скрыто next to имя файла скрыто." in result.executive_block


def test_executive_change_summary_keeps_its_sentences_while_names_of_the_checklist_fold() -> None:
    cut = [f"src/{'d' * 108}{n}.py" for n in range(6)]
    huge = [f"big/{'d' * 108}{n}.py" for n in range(6)]
    snapshots = [
        _text_change(),
        *(_truncated_change(name) for name in cut),
        *(_too_large_change(name) for name in huge),
    ]
    sentences = tuple(f"{'S' * 198}{n}." for n in range(3))
    # Without the text the checklist keeps five names a line (1 417 characters); with it,
    # five names make 2 068 and three 1 588, so one name a line is left (1 108).
    assert len(_what_to_check(_process_snapshots(snapshots))[0].split(", ")) == 5

    result = _process_snapshots(snapshots, change_summary=sentences)

    assert f"{_SIGNED}{' '.join(sentences)}\n\n" in result.executive_block
    assert _what_to_check(result) == [
        f"- Не проверено — patch неполон (6): `{cut[0]}` и ещё 5",
        "- Не проверено — Превышен лимит размера, содержимое не проверено (6): "
        f"`{huge[0]}` и ещё 5",
    ]
    assert result.change_summary_shown == 3


def test_executive_change_summary_drops_its_last_sentences_once_no_name_is_left() -> None:
    diff_map = _diff_map_from_vcs(
        *(
            snapshot_vcs_file(_text_change(path))
            for path in (
                "app/example.py",
                "alembic/versions/0001_init.py",
                ".github/workflows/ci.yml",
                "pyproject.toml",
            )
        )
    )
    every_reason = (
        "classification_unavailable",
        "diff_inventory_incomplete",
        "identity_unresolved",
        "missing_patch",
        "patch_malformed",
        "patch_truncated",
        "patch_unsupported",
        "review_input_incomplete",
        "summary_only",
        "too_large",
        "unknown_path",
        "unsupported_change",
        "unsupported_path",
    )
    coverage = RunCoverage(
        CoverageStatus.PARTIAL, 1, stored_reason_counts=tuple((name, 1) for name in every_reason)
    )
    sentences = tuple(f"{'S' * 198}{n}." for n in range(3))

    result = FindingsPostProcessor.from_default_patterns().process(
        output(finding(severity="high")),
        diff_map=diff_map,
        checked_paths=frozenset({"app/example.py"}),
        rule_names=frozenset(),
        coverage=coverage,
        change_summary=sentences,
    )

    # Every line at no name is 980 characters with the signature: two sentences fit, not three.
    assert len(result.executive_block) <= 1500
    assert f"{_SIGNED}{sentences[0]} {sentences[1]}\n\n" in result.executive_block
    assert sentences[2] not in result.executive_block
    assert len(_what_to_check(result)) == 17
    assert result.change_summary_shown == 2


def test_executive_change_summary_without_room_for_one_sentence_is_left_out() -> None:
    snapshots = [_text_change(), _truncated_change()]
    plain = _process_snapshots(snapshots)

    result = _process_snapshots(snapshots, change_summary=("S" * 1500,))

    assert result.executive_block == plain.executive_block
    assert result.change_summary_shown == 0


@pytest.mark.parametrize(
    ("sentence", "shown"),
    [
        ("Pings @octo-org/admins about it.", "Pings @\u200bocto-org/admins about it."),
        ("Mails ops@evil.test now.", "Mails ops@\u200bevil.test now."),
        (
            "Links [the docs](https://evil.test/x).",
            "Links [the docs]\u200b(https:\u200b//evil.test/x).",
        ),
        (
            "Adds ![x](http://t.test/p.png) to it.",
            "Adds ![x]\u200b(http:\u200b//t.test/p.png) to it.",
        ),
        ("Adds <img src=x> here.", "Adds <\u200bimg src=x> here."),
        ("See <https://evil.test> now.", "See <\u200bhttps:\u200b//evil.test> now."),
        ("Adds &#64;octo here.", "Adds &\u200b#\u200b64;octo here."),
        ("Moves it to WWW.evil.test now.", "Moves it to WWW\u200b.evil.test now."),
        ("Moves it to _www.evil.test_ now.", "Moves it to _www\u200b.evil.test_ now."),
        ("Moves it to (www.evil.test) now.", "Moves it to (www\u200b.evil.test) now."),
        ("Moves it to *www.evil.test* now.", "Moves it to *www\u200b.evil.test* now."),
        ("Fixes #12 and octo/other#3.", "Fixes #\u200b12 and octo/other#\u200b3."),
        ("Closes GH-12 today.", "Closes GH-\u200b12 today."),
        (
            "Merges !7 with ~bug, %v1 and $9.",
            "Merges !\u200b7 with ~\u200bbug, %\u200bv1 and $\u200b9.",
        ),
        (
            "Moves purge_stale_runs to app/jobs.py, done!",
            "Moves purge_stale_runs to app/jobs.py, done!",
        ),
    ],
    ids=[
        "mention",
        "email",
        "link",
        "image",
        "html",
        "autolink",
        "entity",
        "www",
        "www-after-underscore",
        "www-after-parenthesis",
        "www-after-asterisk",
        "issue-references",
        "gh-reference",
        "gitlab-references",
        "plain",
    ],
)
def test_executive_change_summary_shows_no_mention_link_image_or_markup(
    sentence: str, shown: str
) -> None:
    result = _process_snapshots([_text_change()], change_summary=(sentence,))

    assert result.executive_block.endswith(f"{_SIGNED}{shown}")
    rendered = MarkdownIt("commonmark").render(result.executive_block)
    assert "<a " not in rendered
    assert "<img" not in rendered
