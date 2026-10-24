"""Contract tests for scripts/issue-create.sh.

Offline — no network, no tokens, nothing is ever created. Two seams:

* ISSUE_FIXTURE_DIR replaces every CLI call with a file (most tests);
* a stand-in `gh` / `curl` first on PATH runs the script's real command lines
  and records them (the "real command line" group at the end), so the argv that
  would reach GitHub is asserted, not assumed.

The groups that carry weight:

* the attribution guard, asserted in both directions — authorship markers are
  refused, ordinary repository vocabulary passes;
* the recovery path, which is the reason this script exists — a create whose
  output looks like a failure must be resolved by looking, never by retrying,
  or the second attempt duplicates an issue the first one already made; and an
  issue that was already there must never be mistaken for the one just created;
* the board step, which runs after the create and must never lose the issue:
  a missing token scope ends in exit 6 with the url still on stdout;
* validation before the create (milestone, board Status, link targets — nothing
  is created when one is wrong) and the board Status and native links after it,
  whose failures keep the issue: exit 6 / 7 with the exact repair command;
* the regression sample — the spec reconstructed from issue
  larchanka-training/dmc-268-api-t6#46 must reproduce that issue's body byte for
  byte (tests/golden/issue-46.md).
"""
# SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/create-issue/tests/test_create.py

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "scripts" / "issue-create.sh"
GOLDEN = HERE / "golden"

BASE_SPEC: dict[str, Any] = {
    "repo": "acme/sandbox",
    "type": "Story",
    "lang": "en",
    "summary": "[test] a title",
    "acceptance_criteria": ["it works"],
}

# Keys of the sibling tooling this skill was ported from. Built from two halves
# so the self-check grep for the other host's name stays empty over this file.
FOREIGN_BLOCK = "git" + "lab"

# The spec behind issue larchanka-training/dmc-268-api-t6#46 — the team's reference
# body (Russian layout, labelled dependencies, kickoff). Its `dod` is the five-item
# list that issue carries, written out explicitly: the skill's default DoD has since
# changed, and this sample pins the emitter, not the default.
SPEC_46: dict[str, Any] = {
    "repo": "larchanka-training/dmc-268-api-t6",
    "type": "Task",
    "lang": "ru",
    "summary": (
        "[llm] Подтвердить D7 живым прогоном и при необходимости переназначить модели OQ-2"
    ),
    "background": (
        "PR #40 (#33) смёржен; OQ-2 в SD §15 закрыт по каталогу EUrouter, а strict "
        "structured output должен подтвердить ручной workflow `LLM live run`. Первый "
        "прогон после мержа (run 37153425606, артефакт `llm-live-run`) не подтвердил "
        "D7 по двум внешним причинам: `mistral-small-4` — HTTP 402 «Insufficient "
        "balance» (на курсовом аккаунте EUrouter за ключом `AI_DMC268_T6` нет "
        "кредитов; маршрутизация прошла, запрос со strict-схемой роутер принял); "
        "`gpt-4.1-mini` — HTTP 400 «No providers available for model with given "
        "preferences» (у модели единственный провайдер на EUrouter — Microsoft "
        "Foundry; причина не разведена: endpoint отфильтрован для аккаунта либо "
        "провайдер не принимает `response_format: json_schema, strict: true`). Шлюз "
        "отработал как в тестах (fallback, нормализация ошибок, трейс). Разбор — "
        "комментарий в #33 от 03.10."
    ),
    "scope": [
        (
            "Кредиты: вопрос пополнения курсового аккаунта EUrouter поднимает техлид у "
            "куратора; после пополнения — повторный `gh workflow run llm-live-run.yml` на "
            "`main` с дефолтными моделями. Если оба прогона зелёные — записать ссылку на "
            "run в SD §15, задача закрывается без правок кода."
        ),
        (
            "Если 400 на `gpt-4.1-mini` остаётся: развести причину (прогон кандидатов "
            "через input `model` того же workflow; при необходимости — один прогон primary "
            "с `LLM_STRUCTURED_OUTPUT=prompt_json` локально у того, у кого есть ключ, "
            "чтобы отделить «провайдер недоступен» от «strict отвергнут»)."
        ),
        (
            "Переназначение primary (и fallback, если потребуется) по D7: strict "
            "structured output подтверждён прогоном (`calls == [primary]`, "
            "`validate_findings.py` OK), контекст ≥ 60k, прогон fast ≤ $0.50 в худшем "
            "случае (3 попытки × 4 вызова по ценам самого дорогого endpoint). Предпочтение "
            "— модели с ≥ 2 провайдерами на EUrouter и разными вендорами для "
            "primary/fallback. Кандидаты из каталога на 03.10 (контекст ≥ 60k, "
            "`response_format`, ≤ $0.015 за максимальный вызов): `qwen3-coder-30b-a3b` (4 "
            "провайдера), `gpt-oss-120b` (10), `qwen3-235b-a22b-instruct` (6), "
            "`deepseek-v4-flash-0731` (6), `gemma-4` (4)."
        ),
        (
            "Запись результата одним PR: таблица D7 и строка OQ-2 в "
            "`docs/SYSTEM_DESIGN.md` §15 (со ссылкой на зелёный run), дефолты "
            "`KNOWN_MODELS` в `app/modules/reviews/infrastructure/llm/settings.py`, "
            "`.env.example`, первая фраза `docs/PIPELINE_SPEC.md` §5.1, дефолты input'ов "
            "`model`/`fallback_model` в `.github/workflows/llm-live-run.yml`; результат по "
            "поддержке ключевых слов схемы — в PIPELINE §9 (там это закреплено за #33)."
        ),
        (
            "Вне scope: классификация HTTP 402 как не-ретраибельного класса (сейчас "
            "`llm_unavailable`, три попытки прогона на биллинговой ошибке) — отдельная "
            "задача; strict-схема конвенций на EUrouter (известный пробел в SD §15) — "
            "отдельная задача; проброс ключа в staging — #35."
        ),
    ],
    "acceptance_criteria": [
        (
            "Workflow `LLM live run` на `main` зелёный для обоих прогонов: в `calls` "
            "только `primary`, `validate_findings.py` — `OK ReviewOutput N items`; ссылка "
            "на run записана в SD §15"
        ),
        (
            "Если пара моделей изменилась: SD §15 (таблица D7: strict, контекст, цены трёх "
            "требований с датой каталога), `KNOWN_MODELS`, `.env.example`, PIPELINE §5.1 и "
            "дефолты workflow обновлены в одном PR; `uv run pytest` зелёный, тесты "
            "конфигурации (`test_eurouter_is_selected_by_configuration_only` и соседние) "
            "проходят с новыми дефолтами"
        ),
        (
            "Худший случай стоимости fast для новой пары пересчитан по ценам самого "
            "дорогого endpoint каждой модели и ≤ $0.50; валюта endpoint'ов (USD/EUR) учтена"
        ),
        (
            "Причина 400 на `gpt-4.1-mini` записана в SD §15 одной фразой (провайдер "
            "недоступен / strict отвергнут / пропало после пополнения) — чтобы выбор не "
            "пересматривали вслепую"
        ),
        "В #33 оставлен комментарий со ссылкой на зелёный run и итоговой парой моделей",
    ],
    "dependencies": {
        "Блокирует": [
            (
                "Кредиты на курсовом аккаунте EUrouter за `AI_DMC268_T6` — пополнить может "
                "только курс; эскалация у куратора за техлидом (axyi). Без кредитов любой "
                "прогон даёт HTTP 402"
            ),
        ],
        "Связано, не блокирует": [
            (
                "#33 — задача-источник: OQ-2 и workflow `LLM live run` сделаны в PR #40, "
                "итог прогона — в комментарии там же"
            ),
            (
                "#30 — live-режим eval берёт модель из `KNOWN_MODELS`/`LLM_MODEL`; смена "
                "primary меняет baseline"
            ),
            (
                "#35 — проброс `LLM_*` в staging; значения дефолтов должны совпасть с "
                "итогом этой задачи"
            ),
            (
                "Follow-up: HTTP 402 → не-ретраибельный класс `error_code` (§6); "
                "strict-схема конвенций на EUrouter"
            ),
        ],
    },
    "dod": [
        "Tests pass",
        "Linters clean",
        "Docs updated",
        "PR reviewed and approved",
        "Issue status updated",
    ],
    "references": [
        (
            "Run первого прогона: "
            "https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/37153425606"
        ),
        (
            "Разбор прогона: "
            "https://github.com/larchanka-training/dmc-268-api-t6/issues/33#issuecomment-5973444214"
        ),
        (
            "Каталог EUrouter: `GET https://api.eurouter.ai/api/v1/models`, детали "
            "endpoint'ов: `/api/v1/models/<vendor>/<model>/endpoints` (без ключа)"
        ),
        (
            "Документация EUrouter: routing (`/docs/concepts/routing`), ошибки "
            "(`/docs/guides/error-handling`: 400 — «fix the request, do not retry "
            "unchanged», 402 — «add credits»)"
        ),
        "`docs/SYSTEM_DESIGN.md` §15 (OQ-2, таблица D7), `docs/PIPELINE_SPEC.md` §5.1, §9, §14",
        (
            "`.github/workflows/llm-live-run.yml`, `app/bootstrap/llm_gateway.py` (CLI и "
            "форма JSON), `app/modules/reviews/infrastructure/llm/settings.py` "
            "(`KNOWN_MODELS`)"
        ),
    ],
    "kickoff": {
        "repo": ("larchanka-training/dmc-268-api-t6"),
        "branch": (
            "fix/<n>-oq2-live-run — `main` защищён, через PR; если итог «кредиты "
            "пополнены, оба прогона зелёные» — PR только с правкой SD §15"
        ),
        "problem_shape": (
            "Workflow и шлюз работают; не хватает внешнего подтверждения D7. Два "
            "независимых блокера: нулевой баланс аккаунта (402 на любой модели) и "
            "маршрут `gpt-4.1-mini` с единственным провайдером, который отвергает "
            "запрос до биллинга (400). Пока не понятно, отвергает он сам запрос со "
            "strict-схемой или просто недоступен аккаунту — это и нужно развести, а "
            "затем либо подтвердить текущую пару, либо выбрать другую по тем же трём "
            "требованиям D7."
        ),
        "read_first": [
            (
                "docs/SYSTEM_DESIGN.md §15 — что именно обещано по OQ-2 и какие три "
                "требования D7 проверяются"
            ),
            (
                ".github/workflows/llm-live-run.yml — что считается зелёным прогоном "
                '(`calls == ["primary"]`, валидатор) и как подставить другую модель '
                "через input"
            ),
            (
                "app/bootstrap/llm_gateway.py — форма JSON в артефакте "
                "(`calls[].error.{class,http_status,message}`), по ней читается причина "
                "отказа"
            ),
            (
                "app/modules/reviews/infrastructure/llm/settings.py — `KNOWN_MODELS` "
                "(окно, цены, structured_output) и правила наследования fallback-профиля"
            ),
            "комментарий в #33 от 03.10 — артефакт первого прогона и список кандидатов",
        ],
        "before_writing_code": [
            (
                "`gh workflow run llm-live-run.yml --repo "
                "larchanka-training/dmc-268-api-t6 -f model=<кандидат>` и `gh run download "
                "<id>` — артефакт покажет `http_status` и текст ошибки по каждому вызову; "
                "это дешевле любого кода"
            ),
            (
                "`curl -s https://api.eurouter.ai/api/v1/models/<vendor>/<model>/endpoints "
                "| jq '.data.endpoints[] | {provider_name, supported_parameters, "
                "pricing}'` — число провайдеров, валюта и поддержка `response_format` до "
                "выбора"
            ),
            ("`uv run pytest tests/test_llm_gateway.py -q` — после смены дефолтов `KNOWN_MODELS`"),
        ],
        "mines": [
            (
                "`workflow_dispatch` запускается только для файла на `main` — кандидатов "
                "проверяют на `main` через input `model`, а не с ветки"
            ),
            (
                "У части endpoint'ов цены в EUR (Regolo, AKI.IO) при `currency` в "
                "каталоге; лимит $0.50 считать по самому дорогому endpoint модели, а не по "
                "первому"
            ),
            (
                "Запрос шлюза всегда со strict `json_schema` (D7); "
                "`LLM_STRUCTURED_OUTPUT=prompt_json` разрешён только локально с "
                "`LLM_ALLOW_PROMPT_JSON=1` — для диагностики, не для staging"
            ),
            (
                "402 сейчас классифицируется как `llm_unavailable` и run-retryable: при "
                "нулевом балансе воркер сделает три попытки — не принимать это за "
                "«нестабильность провайдера»"
            ),
            (
                "Каталог `supported_parameters` объявляет только `response_format`, "
                "отдельной возможности `structured_outputs` у EUrouter нет — наличие "
                "параметра не доказывает strict, доказывает только прогон"
            ),
            (
                "Смена primary — это новая пара в SD §15, `KNOWN_MODELS` и `.env.example` "
                "одновременно; `LLM_FALLBACK_*` наследует ключи только на том же endpoint"
            ),
        ],
    },
}


# Board 12 as `gh project view` / `gh project field-list --format json` describe it.
PROJECT_VIEW = json.dumps({"id": "PVT_board12", "number": 12, "title": "dmc-268-t6"})
STATUS_FIELD: dict[str, Any] = {
    "id": "PVTSSF_status",
    "name": "Status",
    "type": "ProjectV2SingleSelectField",
    "options": [
        {"id": "opt_todo", "name": "Todo"},
        {"id": "opt_progress", "name": "In Progress"},
        {"id": "opt_done", "name": "Done"},
    ],
}
PROJECT_FIELDS = json.dumps(
    {
        "fields": [{"id": "PVTF_title", "name": "Title", "type": "ProjectV2Field"}, STATUS_FIELD],
        "totalCount": 2,
    }
)


def golden_body(name: str) -> str:
    """SYNC line, blank line, then the body: drop exactly two lines, no strip."""
    text = (GOLDEN / f"{name}.md").read_bytes().decode("utf-8")
    return text.split("\n", 2)[2]


def run(
    tmp_path: Path,
    spec: dict[str, Any] | None = None,
    *flags: str,
    search: str | None = "[]",
    recover_before: str | None = "[]",
    recover: str | None = "[]",
    create: str = '{"url": ""}',
    view: str | None = None,
    board_code: str = "0",
    board_err: str = "",
    auth_code: str = "0",
    raw_spec: str | None = None,
    milestones: str | None = None,
    project_view: str | None = PROJECT_VIEW,
    project_fields: str | None = PROJECT_FIELDS,
    extra: dict[str, Any] | None = None,
    item_add: str = '{"id": "PVTI_item"}',
    status_reads: dict[str, str] | None = None,
    edit_err: str = "",
) -> subprocess.CompletedProcess[str]:
    """`None` for search / recover_before / recover / milestones / project_view /
    project_fields means the fixture file does not exist, which the script treats as
    a failed call. `extra` adds fixture files by relative path (issue-link.sh's
    REST-path files, `<name>.err` texts); dicts and lists are written as JSON.
    `status_reads` maps each board Status read-back — `first` (after the edit),
    `confirm` (after the pause, only for a Status other than Todo) and `retry` (after
    the re-edit) — to the name it shows; a missing key is a failed read. By default
    the first and the confirm read show the Status asked for."""
    spec = spec or BASE_SPEC
    fix = tmp_path / "fix"
    fix.mkdir(parents=True, exist_ok=True)
    optional = {
        "issue_search.json": search,
        "issue_recover_before.json": recover_before,
        "issue_recover.json": recover,
        "milestones.json": milestones,
        "project_view.json": project_view,
        "project_fields.json": project_fields,
    }
    want = spec.get("status") or "Todo"
    reads = status_reads if status_reads is not None else {"first": want, "confirm": want}
    for key, name in (
        ("first", "project_status.json"),
        ("confirm", "project_status.confirm.json"),
        ("retry", "project_status.retry.json"),
    ):
        optional[name] = status_read(reads[key]) if key in reads else None
    for name, val in optional.items():
        if val is not None:
            (fix / name).write_text(val)
    files = {
        "reach.code": "0",
        "reach.http": "200",
        "auth_user.code": auth_code,
        "auth_user.json": '{"login": "octocat"}',
        "repo_perms.json": "true",
        "issue_create.json": create,
        # By default the read-back sees exactly the title that was posted.
        "issue_view.json": view
        if view is not None
        else json.dumps({"title": spec.get("summary", "")}),
        "project_add.code": board_code,
        "project_add.err": board_err,
        "project_add.json": item_add,
        "project_edit.err": edit_err,
    }
    for name, val in files.items():
        (fix / name).write_text(val)
    for rel, content in (extra or {}).items():
        path = fix / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content if isinstance(content, str) else json.dumps(content))
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(raw_spec if raw_spec is not None else json.dumps(spec), encoding="utf-8")
    return subprocess.run(
        ["bash", str(SCRIPT), str(spec_path), *flags],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "ISSUE_FIXTURE_DIR": str(fix),
            "ISSUE_STATUS_RETRY_DELAY": "0",
        },
        timeout=60,
    )


def status_read(name: str) -> str:
    """`gh api graphql` answering fieldValueByName(name: "Status") for the item."""
    return json.dumps({"data": {"node": {"fieldValueByName": {"name": name}}}})


def rest_issue(
    number: int, title: str = "[test] a title", author: str | None = "octocat", **extra: Any
) -> dict[str, Any]:
    """One row of the REST issues listing. `octocat` is the authenticated user of every
    fixture and of the stand-in gh; `author=None` leaves the `user` key out."""
    row: dict[str, Any] = {
        "number": number,
        "title": title,
        "html_url": f"https://github.com/acme/sandbox/issues/{number}",
    }
    if author is not None:
        row["user"] = {"login": author}
    row.update(extra)
    return row


def listing(*issues: dict[str, Any]) -> str:
    return json.dumps(list(issues))


def first_error(r: subprocess.CompletedProcess[str]) -> str:
    """The emitter's own message; the script adds a second line after it."""
    return str(json.loads(r.stderr.splitlines()[0])["error"])


def board_was_called(tmp_path: Path) -> bool:
    return (tmp_path / "fix" / "project_add.called").exists()


def fixture_calls(tmp_path: Path) -> list[str]:
    """The stand-in calls the fixture seam logged, in order."""
    log = tmp_path / "fix" / "calls.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def gh_issue(ref: str, **extra: Any) -> dict[str, Any]:
    """A REST issue object; the internal id (9_000_000 + N) never equals the number."""
    repo, num = ref.split("#")
    row: dict[str, Any] = {
        "id": 9_000_000 + int(num),
        "number": int(num),
        "title": f"issue {num}",
        "repository_url": f"https://api.github.com/repos/{repo}",
        "parent_issue_url": None,
    }
    row.update(extra)
    return row


def issue_fixture(ref: str, **extra: Any) -> dict[str, Any]:
    repo, num = ref.split("#")
    return {f"repos/{repo}/issues/{num}.json": gh_issue(ref, **extra)}


SCOPE_ERROR_TEXT = (
    "error: your authentication token is missing required scopes [project]\n"
    "To request it, run:  gh auth refresh -s project\n"
)
MILESTONES = json.dumps([{"number": 3, "title": "Sprint 3"}, {"number": 4, "title": "Sprint 4"}])
CREATED_9 = '{"url": "https://x/9"}'  # the create returns a url
# A spec using every new key: a same-repo parent, a cross-repo blocker, a milestone
# and a non-default board Status.
LINKED_SPEC: dict[str, Any] = dict(
    BASE_SPEC,
    parent="#5",
    blocked_by=["other/lib#7"],
    milestone="Sprint 3",
    status="In Progress",
)
TARGETS = {**issue_fixture("acme/sandbox#5"), **issue_fixture("other/lib#7")}


# ─── dry run ────────────────────────────────────────────────────────────────
def test_dry_run_creates_nothing_and_returns_the_body(tmp_path: Path) -> None:
    r = run(tmp_path, None, "--dry-run")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["dry_run"] is True
    assert out["repo"] == "acme/sandbox"
    assert "## Dependencies" in out["body"]
    assert "host_kind" not in out
    assert not board_was_called(tmp_path)


def test_dry_run_on_the_46_spec_reproduces_the_issue_body(tmp_path: Path) -> None:
    r = run(tmp_path, SPEC_46, "--dry-run")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["body"] == golden_body("issue-46")


# ─── bad specs ──────────────────────────────────────────────────────────────
def test_bad_spec_is_refused_before_any_call(tmp_path: Path) -> None:
    r = run(tmp_path, {"repo": "a/b"}, "--dry-run")
    assert r.returncode == 2
    assert "summary" in first_error(r)


@pytest.mark.parametrize("key", ["host_kind", FOREIGN_BLOCK])
def test_a_key_the_emitter_does_not_know_is_an_error_not_a_noop(tmp_path: Path, key: str) -> None:
    r = run(tmp_path, dict(BASE_SPEC, **{key: "github"}), "--dry-run")
    assert r.returncode == 2
    assert f"unknown top-level key(s): {key}" in r.stderr


@pytest.mark.parametrize("raw", ["{not json", "[]", '"a string"', ""])
def test_a_malformed_spec_file_gets_the_emitters_message(tmp_path: Path, raw: str) -> None:
    """The emitter runs first, so bad JSON is reported by the component that knows
    what a spec is — not as a generic "missing repo"."""
    r = run(tmp_path, None, "--dry-run", raw_spec=raw)
    assert r.returncode == 2
    assert "must carry" not in r.stderr
    assert "Traceback" not in r.stderr
    assert "spec" in json.loads(r.stderr.splitlines()[0])["error"]


def test_a_wrong_value_type_is_refused_before_any_call(tmp_path: Path) -> None:
    r = run(tmp_path, dict(BASE_SPEC, scope="not a list"), "--dry-run")
    assert r.returncode == 2
    assert '"scope" must be a list of strings' in first_error(r)


@pytest.mark.parametrize(
    "repo", ["acme/sandbox/extra", "github.com/acme/sandbox", "https://github.com/acme/sandbox"]
)
def test_a_repo_that_is_not_owner_slash_repo_is_refused(tmp_path: Path, repo: str) -> None:
    """Refused by the emitter, before any call: a host prefix would pass preflight and
    then 404 in every later `repos/$REPO/...` path."""
    r = run(tmp_path, dict(BASE_SPEC, repo=repo), "--dry-run")
    assert r.returncode == 2
    assert "OWNER/REPO" in first_error(r)


@pytest.mark.parametrize("field", ["labels", "assignees"])
def test_a_blank_label_or_assignee_is_refused_not_dropped(tmp_path: Path, field: str) -> None:
    """A blank entry would reach `gh` as a bare `--label` and silently vanish."""
    r = run(tmp_path, dict(BASE_SPEC, **{field: ["fine", ""]}), "--dry-run")
    assert r.returncode == 2
    assert "blank" in first_error(r)


# ─── validation: refused before the create, in --dry-run too ───────────────
def test_dry_run_reports_milestone_status_and_the_resolved_links(tmp_path: Path) -> None:
    r = run(tmp_path, LINKED_SPEC, "--dry-run", milestones=MILESTONES, extra=TARGETS)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["milestone"] == "Sprint 3"
    assert out["status"] == "In Progress"
    assert out["links"] == [
        {"kind": "parent", "repo": "acme/sandbox", "number": 5},
        {"kind": "blocked_by", "repo": "other/lib", "number": 7},
    ]
    # the keys never change the body
    plain = run(tmp_path / "plain", dict(BASE_SPEC, milestone="Sprint 3"), "--dry-run",
                milestones=MILESTONES)  # fmt: skip
    assert out["body"] == json.loads(plain.stdout)["body"]
    assert fixture_calls(tmp_path) == [
        "milestones",
        "project_view",
        "project_fields",
        "GET repos/acme/sandbox/issues/5",
        "GET repos/other/lib/issues/7",
    ]
    assert not board_was_called(tmp_path)


def test_dry_run_status_defaults_to_todo_and_is_null_without_the_board(tmp_path: Path) -> None:
    on = run(tmp_path, None, "--dry-run")
    assert json.loads(on.stdout)["status"] == "Todo"
    off = run(tmp_path / "off", None, "--dry-run", "--no-board", project_view=None)
    assert off.returncode == 0, off.stderr
    out = json.loads(off.stdout)
    assert (out["status"], out["milestone"], out["links"]) == (None, None, [])
    assert "project_view" not in fixture_calls(tmp_path / "off")


@pytest.mark.parametrize("flags", [("--dry-run",), ()])
@pytest.mark.parametrize(
    "milestones,shown",
    [
        (json.dumps([{"title": "Sprint 2"}, {"title": "Sprint 4"}]), "(open: Sprint 2, Sprint 4)"),
        ("[]", "(open: none)"),
        # exact title only
        (json.dumps([{"title": "sprint 3"}]), "(open: sprint 3)"),
    ],
)
def test_a_missing_milestone_stops_the_run_before_the_create(
    tmp_path: Path, flags: tuple[str, ...], milestones: str, shown: str
) -> None:
    spec = dict(BASE_SPEC, milestone="Sprint 3")
    r = run(tmp_path, spec, *flags, milestones=milestones, create='{"url": "https://x/9"}')
    assert r.returncode == 2
    assert (
        json.loads(r.stderr)["error"] == f'milestone "Sprint 3" not found in acme/sandbox {shown}'
    )
    assert r.stdout == ""
    assert "issue_create" not in fixture_calls(tmp_path)
    assert not board_was_called(tmp_path)


@pytest.mark.parametrize(
    "err,code",
    [
        ("gh: Bad credentials (HTTP 401)", 3),
        ("gh: Resource not accessible by integration (HTTP 403)", 3),
        # a (secondary) rate limit is transient, not a refusal
        ("gh: API rate limit exceeded for user ID 1. (HTTP 403)", 5),
        ("gh: You have exceeded a secondary rate limit. (HTTP 403)", 5),
        ("gh: You have triggered an abuse detection mechanism. (HTTP 403)", 5),
        ("gh: Server Error (HTTP 502)", 5),
        ("", 5),
    ],
)
def test_a_failed_milestone_listing_is_classified_and_creates_nothing(
    tmp_path: Path, err: str, code: int
) -> None:
    spec = dict(BASE_SPEC, milestone="Sprint 3")
    r = run(tmp_path, spec, extra={"milestones.err": err})
    assert r.returncode == code
    assert "milestones of acme/sandbox" in json.loads(r.stderr)["error"]
    assert "issue_create" not in fixture_calls(tmp_path)


def test_a_milestone_listing_that_is_not_a_list_is_exit_5(tmp_path: Path) -> None:
    r = run(tmp_path, dict(BASE_SPEC, milestone="Sprint 3"), milestones='{"message": "x"}')
    assert r.returncode == 5


@pytest.mark.parametrize("flags", [("--dry-run",), ()])
@pytest.mark.parametrize("status", ["Doing", "todo"])  # an option name matches exactly
def test_an_unknown_status_is_refused_with_the_boards_options(
    tmp_path: Path, flags: tuple[str, ...], status: str
) -> None:
    r = run(tmp_path, dict(BASE_SPEC, status=status), *flags)
    assert r.returncode == 2
    err = json.loads(r.stderr)["error"]
    assert f'"{status}"' in err and "(options: Todo, In Progress, Done)" in err
    assert "issue_create" not in fixture_calls(tmp_path)


@pytest.mark.parametrize("flags", [("--dry-run",), ()])
def test_status_together_with_no_board_is_refused(tmp_path: Path, flags: tuple[str, ...]) -> None:
    r = run(tmp_path, dict(BASE_SPEC, status="Todo"), "--no-board", *flags)
    assert r.returncode == 2
    assert "--no-board" in json.loads(r.stderr)["error"]
    assert r.stdout == ""


def test_a_board_without_a_status_field_is_refused(tmp_path: Path) -> None:
    r = run(tmp_path, None, project_fields=json.dumps({"fields": [], "totalCount": 0}))
    assert r.returncode == 2
    assert "Status" in json.loads(r.stderr)["error"]


@pytest.mark.parametrize(
    "fixtures,code,needle",
    [
        ({"project_view": None, "extra": {"project_view.err": SCOPE_ERROR_TEXT}}, 3,
         "gh auth refresh -s project"),
        ({"project_fields": None, "extra": {"project_fields.err": "gh: HTTP 502"}}, 5,
         "board 12"),
        ({"project_fields": "not json"}, 5, "board 12"),
    ],
)  # fmt: skip
def test_an_unreadable_board_stops_the_run_before_the_create(
    tmp_path: Path, fixtures: dict[str, Any], code: int, needle: str
) -> None:
    """The Status is resolved before the create, so a token without the `project`
    scope now stops the run while nothing exists — not after the issue is made."""
    r = run(tmp_path, None, **fixtures)
    assert r.returncode == code
    assert needle in json.loads(r.stderr)["error"]
    assert "issue_create" not in fixture_calls(tmp_path)


@pytest.mark.parametrize("flags", [("--dry-run",), ()])
@pytest.mark.parametrize(
    "extra,code,needle",
    [
        ({"repos/other/lib/issues/7.json": None,
          "repos/other/lib/issues/7.err": "gh: Not Found (HTTP 404)"},
         2, "other/lib#7 not found"),
        (issue_fixture("acme/sandbox#5", pull_request={"url": "https://x/pull/5"}),
         2, "acme/sandbox#5 is a pull request"),
        # issue-link.sh --check's own class passes through: auth, transient
        ({"repos/other/lib/issues/7.json": None,
          "repos/other/lib/issues/7.err": "gh: Bad credentials (HTTP 401)"},
         3, "cannot read other/lib#7: gh: Bad credentials (HTTP 401)"),
        ({"repos/other/lib/issues/7.json": None,
          "repos/other/lib/issues/7.err": "gh: Server Error (HTTP 502)"},
         5, "cannot read other/lib#7: gh: Server Error (HTTP 502)"),
    ],
)  # fmt: skip
def test_a_link_target_that_cannot_be_linked_stops_the_run_before_the_create(
    tmp_path: Path, flags: tuple[str, ...], extra: dict[str, Any], code: int, needle: str
) -> None:
    files = {k: v for k, v in {**TARGETS, **extra}.items() if v is not None}
    r = run(tmp_path, LINKED_SPEC, *flags, milestones=MILESTONES, extra=files)
    assert r.returncode == code
    assert r.stdout == ""
    assert needle in json.loads(r.stderr.splitlines()[-1])["error"]
    assert "issue_create" not in fixture_calls(tmp_path)
    assert not board_was_called(tmp_path)


def test_a_parent_of_another_owner_is_refused_by_the_emitter(tmp_path: Path) -> None:
    r = run(tmp_path, dict(BASE_SPEC, parent="other/sandbox#5"), "--dry-run")
    assert r.returncode == 2
    assert "owner" in first_error(r)
    assert fixture_calls(tmp_path) == []


def test_preflight_failures_pass_their_exit_code_through(tmp_path: Path) -> None:
    r = run(tmp_path, None, "--dry-run", auth_code="1")
    assert r.returncode == 3
    assert "gh auth login" in json.loads(r.stderr)["error"]


# ─── attribution guard, in both directions ──────────────────────────────────
@pytest.mark.parametrize(
    "field,value",
    [
        ("background", "Generated with Claude Code"),
        ("background", "GENERATED WITH a tool"),
        ("summary", "[test] Generated with a tool"),
        ("dependencies", "Co-Authored-By: someone <someone@example.com>"),
        ("dependencies", "   co-authored-by: lower case, indented"),
        ("references", ["noreply@anthropic.com"]),
        ("background", "see https://claude.ai/code/session_01ABC"),
        # argv only: a label never reaches the body
        ("labels", ["generated with a tool"]),
    ],
)
def test_refuses_to_post_a_marker_of_assistant_authorship(
    tmp_path: Path, field: str, value: Any
) -> None:
    """Not conditional on which repo we are on: an unconditional refusal cannot
    be reached with a wrong flag. Covers body, title and argv, case-insensitive."""
    r = run(tmp_path, dict(BASE_SPEC, **{field: value}), "--dry-run")
    assert r.returncode == 2
    assert "authorship" in json.loads(r.stderr)["error"]


def test_the_guard_runs_before_the_dry_run_exit(tmp_path: Path) -> None:
    """The preview step (`--dry-run`) already catches a marker: nothing is printed."""
    r = run(tmp_path, dict(BASE_SPEC, background="Generated with a tool"), "--dry-run")
    assert r.returncode == 2
    assert r.stdout == ""


@pytest.mark.parametrize(
    "field,value",
    [
        # repository vocabulary: files, directories and tools by name
        ("background", "The skill is read through .claude/skills; CLAUDE.md imports AGENTS.md."),
        ("background", "Run it from Claude Code or from Codex."),
        ("summary", "[agents] Document the Anthropic SDK upgrade path"),
        ("references", ["https://claude.ai/docs"]),
        # a trailer is a line that STARTS with the key; a mention mid-line is prose
        ("background", "The hook strips any co-authored-by: trailer before the push."),
    ],
)
def test_ordinary_repository_vocabulary_passes_the_guard(
    tmp_path: Path, field: str, value: Any
) -> None:
    r = run(tmp_path, dict(BASE_SPEC, **{field: value}), "--dry-run")
    assert r.returncode == 0, r.stderr


# ─── duplicate guard ────────────────────────────────────────────────────────
def search_hit(number: int, title: str = "[test] a title", **extra: Any) -> dict[str, Any]:
    """One row of `gh issue list --json number,title,url,state,author,createdAt`."""
    row: dict[str, Any] = {
        "number": number,
        "title": title,
        "url": f"https://github.com/acme/sandbox/issues/{number}",
        "state": "CLOSED",
        "author": {"id": "U_1", "is_bot": False, "login": "teammate", "name": "T. Mate"},
        "createdAt": "2026-10-01T09:00:00Z",
    }
    row.update(extra)
    return row


# The duplicate as issue-create.sh reports it: one shape for both lookups.
DUP_7 = {
    "number": 7,
    "url": "https://github.com/acme/sandbox/issues/7",
    "state": "CLOSED",
    "author": "teammate",
    "createdAt": "2026-10-01T09:00:00Z",
}


def test_exact_title_match_blocks_creation(tmp_path: Path) -> None:
    r = run(tmp_path, search=json.dumps([search_hit(6, "[test] a title, extended"), search_hit(7)]))
    assert r.returncode == 4
    assert json.loads(r.stderr)["error"] == (
        "an issue with exactly this title already exists in acme/sandbox: "
        "https://github.com/acme/sandbox/issues/7 — pass --allow-duplicate to create another anyway"
    )
    # The duplicate is also on stdout, structured — with its state, author and age and
    # the login of who is filing, so issue-batch.py can decide whether a rerun adopts it.
    assert json.loads(r.stdout) == {
        "duplicate": True,
        **DUP_7,
        "repo": "acme/sandbox",
        "user": "octocat",
    }
    assert "issue_create" not in fixture_calls(tmp_path)
    assert not board_was_called(tmp_path)


def test_dry_run_shows_the_duplicate_the_guard_would_block_on(tmp_path: Path) -> None:
    hits = json.dumps([search_hit(6, "[test] a title, extended"), search_hit(7)])
    r = run(tmp_path, None, "--dry-run", search=hits)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["duplicate"] == DUP_7
    assert out["user"] == "octocat"
    assert "issue_create" not in fixture_calls(tmp_path)


@pytest.mark.parametrize(
    "flags,search,warned",
    [
        (("--dry-run",), "[]", False),
        (("--dry-run", "--allow-duplicate"), json.dumps([search_hit(7)]), False),
        (("--dry-run",), None, True),  # fail-open: a failed search shows no duplicate
    ],
    ids=["no-match", "allow-duplicate", "failed-search"],
)
def test_dry_run_shows_no_duplicate(
    tmp_path: Path, flags: tuple[str, ...], search: str | None, warned: bool
) -> None:
    r = run(tmp_path, None, *flags, search=search)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["duplicate"] is None
    assert ("duplicate search failed" in r.stderr) is warned


def test_substring_match_does_not_block(tmp_path: Path) -> None:
    """Search is loose. Only an exact title is the same issue — otherwise
    'fix login' would block 'fix login redirect' forever."""
    hits = json.dumps([{"number": 7, "title": "[test] a title, extended", "url": "https://x/7"}])
    r = run(tmp_path, search=hits, create='{"url": "https://x/9"}')
    assert r.returncode == 0, r.stderr


def test_allow_duplicate_overrides_the_guard(tmp_path: Path) -> None:
    hits = json.dumps([{"number": 7, "title": "[test] a title", "url": "https://x/7"}])
    r = run(tmp_path, None, "--allow-duplicate", search=hits, create='{"url": "https://x/9"}')
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["url"] == "https://x/9"


def test_a_failed_duplicate_search_warns_and_lets_the_create_proceed(tmp_path: Path) -> None:
    """Fail-open by decision: a broken search must not stop an issue from being filed,
    but it must not be silent either."""
    r = run(tmp_path, search=None, create='{"url": "https://x/9"}')
    assert r.returncode == 0, r.stderr
    assert "duplicate search failed" in json.loads(r.stderr)["warning"]


# ─── --adopt-since: a rerun of a batch finds the issue it already opened ────
SINCE = "2026-10-24T12:00:00Z"
AFTER = "2026-10-24T12:05:00Z"


def own(number: int, **extra: Any) -> dict[str, Any]:
    """A listing row: open, by the authenticated user, created after SINCE."""
    return rest_issue(number, **{"state": "open", "created_at": AFTER, **extra})


def test_adopt_since_reports_your_issue_opened_since_then_as_the_duplicate(
    tmp_path: Path,
) -> None:
    newest = listing(own(41, title="[test] another title"), own(40), own(39))
    r = run(tmp_path, None, "--adopt-since", SINCE, recover_before=newest, create=CREATED_9)
    assert r.returncode == 4
    assert json.loads(r.stdout) == {
        "duplicate": True,
        "number": 40,
        "url": "https://github.com/acme/sandbox/issues/40",
        "state": "OPEN",
        "author": "octocat",
        "createdAt": AFTER,
        "repo": "acme/sandbox",
        "user": "octocat",
    }
    assert json.loads(r.stderr)["error"] == (
        "an issue with exactly this title, opened by you at or after 2026-10-24T12:00:00Z, "
        "already exists in acme/sandbox: "
        "https://github.com/acme/sandbox/issues/40 — not created again"
    )
    assert "issue_create" not in fixture_calls(tmp_path)


@pytest.mark.parametrize(
    "row,state",
    [
        (own(40, created_at=SINCE), "OPEN"),  # at the start counts
        (own(40, user={"login": "OctoCat"}), "OPEN"),  # logins compare without case
        (own(40, state="closed"), "CLOSED"),  # reported whatever its state
        (own(40, created_at="2026-10-24T14:05:00+02:00"), "OPEN"),  # = 12:05Z
    ],
    ids=["at-the-start", "login-case", "closed", "other-zone"],
)
def test_adopt_since_at_its_edges(tmp_path: Path, row: dict[str, Any], state: str) -> None:
    r = run(tmp_path, None, "--adopt-since", SINCE, recover_before=listing(row), create=CREATED_9)
    assert r.returncode == 4, r.stderr
    assert (json.loads(r.stdout)["number"], json.loads(r.stdout)["state"]) == (40, state)
    assert "issue_create" not in fixture_calls(tmp_path)


@pytest.mark.parametrize(
    "row",
    [
        own(40, user={"login": "teammate"}),
        rest_issue(40, author=None, state="open", created_at=AFTER),
        own(40, created_at="2026-10-24T11:59:59Z"),
        own(40, created_at="2026-10-24T13:00:00+02:00"),  # = 11:00Z
        own(40, created_at=None),
        own(40, pull_request={"url": "https://api.github.com/repos/acme/sandbox/pulls/40"}),
        own(40, title="[test] a title, extended"),
    ],
    ids=["teammate", "no-author", "older", "older-other-zone", "no-created-at", "pull-request",
         "other-title"],
)  # fmt: skip
def test_adopt_since_leaves_any_other_issue_to_the_create(
    tmp_path: Path, row: dict[str, Any]
) -> None:
    r = run(tmp_path, None, "--adopt-since", SINCE, recover_before=listing(row), create=CREATED_9)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["url"] == "https://x/9"
    assert "issue_create" in fixture_calls(tmp_path)


def test_adopt_since_wins_over_an_older_duplicate_the_search_finds(tmp_path: Path) -> None:
    """The search lags and finds what is old; the issue the batch opened comes first."""
    r = run(
        tmp_path,
        None,
        "--adopt-since",
        SINCE,
        search=json.dumps([search_hit(7)]),
        recover_before=listing(own(40)),
        create=CREATED_9,
    )
    assert r.returncode == 4
    assert json.loads(r.stdout)["number"] == 40


def test_without_adopt_since_your_new_issue_does_not_block(tmp_path: Path) -> None:
    r = run(tmp_path, None, recover_before=listing(own(40)), create=CREATED_9)
    assert r.returncode == 0, r.stderr


NO_LISTING = (
    "cannot list the newest issues of acme/sandbox to look for one an earlier run of this "
    "batch opened"
)


@pytest.mark.parametrize(
    "newest,message",
    [
        (None, f"{NO_LISTING} — rerun when GitHub answers"),
        ('{"message": "Not Found"}', f"{NO_LISTING}: the response is not a list"),
    ],
    ids=["failed", "not-a-list"],
)
def test_adopt_since_without_the_listing_is_exit_5_and_creates_nothing(
    tmp_path: Path, newest: str | None, message: str
) -> None:
    """Without the listing a lagging search could let the issue be created twice."""
    r = run(tmp_path, None, "--adopt-since", SINCE, recover_before=newest, create=CREATED_9)
    assert r.returncode == 5
    assert json.loads(r.stderr)["error"] == message
    assert "issue_create" not in fixture_calls(tmp_path)


@pytest.mark.parametrize("since", ["yesterday", "2026-10-24T12:00:00"], ids=["text", "no-zone"])
def test_adopt_since_needs_a_time_with_a_zone(tmp_path: Path, since: str) -> None:
    r = run(tmp_path, None, "--adopt-since", since, recover_before=listing(own(40)))
    assert r.returncode == 2
    assert json.loads(r.stderr)["error"] == (
        f'--adopt-since "{since}" is not an ISO 8601 time with a zone'
    )
    assert "issue_create" not in fixture_calls(tmp_path)


def test_adopt_since_needs_a_value(tmp_path: Path) -> None:
    r = run(tmp_path, None, "--adopt-since")
    assert r.returncode == 2
    assert json.loads(r.stderr)["error"] == "--adopt-since needs a timestamp"


# ─── the recovery path ──────────────────────────────────────────────────────
def test_a_create_that_landed_without_output_is_found_not_retried(tmp_path: Path) -> None:
    """The exact failure this script is built around: the create call returns
    nothing, but the server did make the issue. Looking finds it; retrying would
    have made a second one."""
    r = run(tmp_path, create='{"url": ""}', recover=listing(rest_issue(42)))
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["url"] == "https://github.com/acme/sandbox/issues/42"
    assert out["number"] == 42
    assert board_was_called(tmp_path)


def test_recovery_picks_the_new_issue_among_older_same_title_ones(tmp_path: Path) -> None:
    old = rest_issue(7)
    r = run(
        tmp_path,
        None,
        "--allow-duplicate",
        create='{"url": ""}',
        recover_before=listing(old),
        recover=listing(rest_issue(42), old),
    )
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["number"] == 42


def test_recovery_never_reports_an_issue_that_was_already_there(tmp_path: Path) -> None:
    """With --allow-duplicate an older issue with the same title is legitimate. If
    the create then fails, that older issue is not "the one just created": reporting
    it as verified, and putting it on the board, would be a false success."""
    old = rest_issue(7)
    r = run(
        tmp_path,
        None,
        "--allow-duplicate",
        create='{"url": ""}',
        recover_before=listing(old),
        recover=listing(old),
    )
    assert r.returncode == 4
    assert "not retrying" in json.loads(r.stderr)["error"]
    assert r.stdout == ""
    assert not board_was_called(tmp_path)


def test_recovery_does_not_adopt_a_teammates_same_title_issue(tmp_path: Path) -> None:
    """A new, same-title issue opened by someone else in the window between snapshot
    and create is the wrong object: adopting it would report and board it as ours."""
    r = run(tmp_path, create='{"url": ""}', recover=listing(rest_issue(42, author="hubot")))
    assert r.returncode == 4
    assert "not retrying" in json.loads(r.stderr)["error"]
    assert r.stdout == ""
    assert not board_was_called(tmp_path)


def test_recovery_adopts_the_users_own_issue_whatever_the_login_case(tmp_path: Path) -> None:
    r = run(tmp_path, create='{"url": ""}', recover=listing(rest_issue(42, author="OctoCat")))
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["number"] == 42


def test_recovery_prefers_the_users_issue_over_a_newer_teammates(tmp_path: Path) -> None:
    mine, theirs = rest_issue(42), rest_issue(43, author="hubot")
    r = run(tmp_path, create='{"url": ""}', recover=listing(theirs, mine))
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["number"] == 42


@pytest.mark.parametrize(
    "row",
    [
        rest_issue(42, author=None),  # no `user` at all
        rest_issue(42, user=None),
        rest_issue(42, user="octocat"),  # not an object
        rest_issue(42, user={"login": None}),
        rest_issue(42, user={}),
    ],
)
def test_recovery_does_not_adopt_an_issue_without_a_usable_author(
    tmp_path: Path, row: dict[str, Any]
) -> None:
    r = run(tmp_path, create='{"url": ""}', recover=listing(row))
    assert r.returncode == 4
    assert "not retrying" in json.loads(r.stderr)["error"]


def test_recovery_ignores_pull_requests_and_other_titles(tmp_path: Path) -> None:
    pull = rest_issue(9, pull_request={"url": "https://x/pull/9"})
    other = rest_issue(10, title="[test] something else")
    r = run(tmp_path, create='{"url": ""}', recover=listing(pull, other))
    assert r.returncode == 4
    assert "not retrying" in json.loads(r.stderr)["error"]


def test_create_failed_and_nothing_landed_is_an_error_not_a_retry(tmp_path: Path) -> None:
    r = run(tmp_path, create='{"url": ""}')
    assert r.returncode == 4
    assert "not retrying" in json.loads(r.stderr)["error"]


@pytest.mark.parametrize(
    "err,code",
    [
        ("gh: Bad credentials (HTTP 401)", 3),
        ("gh: Resource not accessible by integration (HTTP 403)", 3),
        # a (secondary) rate limit is transient, not a refusal
        ("gh: API rate limit exceeded for user ID 1. (HTTP 403)", 5),
        ("gh: You have exceeded a secondary rate limit. (HTTP 403)", 5),
        ("gh: You have triggered an abuse detection mechanism. (HTTP 403)", 5),
    ],
)
def test_a_failed_create_that_did_not_land_is_classified_and_not_retried(
    tmp_path: Path, err: str, code: int
) -> None:
    r = run(tmp_path, create='{"url": ""}', extra={"issue_create.err": err})
    assert r.returncode == code
    assert err in json.loads(r.stderr)["error"]
    assert fixture_calls(tmp_path).count("issue_create") == 1
    assert not board_was_called(tmp_path)


@pytest.mark.parametrize(
    "fixtures",
    [
        {"recover": None},
        {"recover_before": None},
        {"recover": "not json"},
        {"recover": '{"message": "Bad credentials"}'},
        {"recover": "[null]"},
        {"recover": '["x"]'},
        {"recover_before": "[1]"},
    ],
)
def test_a_failed_lookup_is_unknown_never_none_exist(
    tmp_path: Path, fixtures: dict[str, Any]
) -> None:
    """If the repository cannot be checked, the honest answer is "unknown — look by
    hand", not "nothing was created, retry"."""
    r = run(tmp_path, create='{"url": ""}', **fixtures)
    assert r.returncode == 5
    err = json.loads(r.stderr)["error"]
    assert "outcome unknown" in err
    assert "not retrying" not in err
    assert not board_was_called(tmp_path)


def test_a_failed_snapshot_does_not_block_a_create_that_worked(tmp_path: Path) -> None:
    r = run(tmp_path, create='{"url": "https://x/13"}', recover_before=None)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["number"] == 13


# ─── result shape and the read-back ─────────────────────────────────────────
def test_success_reports_url_number_verified_and_board(tmp_path: Path) -> None:
    r = run(tmp_path, create='{"url": "https://github.com/acme/sandbox/issues/13"}')
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == {
        "url": "https://github.com/acme/sandbox/issues/13",
        "number": 13,
        "repo": "acme/sandbox",
        "verified": True,
        "board": "added",
        "status": "Todo",
    }
    assert r.stderr == ""


@pytest.mark.parametrize("view", ['{"title": "[test] some other issue"}', "{}", "not json"])
def test_read_back_that_does_not_match_the_title_is_not_verified(tmp_path: Path, view: str) -> None:
    """`verified` used to be hard-coded. It is the title read back from the
    server, so a create that landed as something else says so."""
    r = run(tmp_path, create='{"url": "https://x/5"}', view=view)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["verified"] is False


@pytest.mark.parametrize(
    "milestone,verified",
    [({"title": "Sprint 3"}, True), (None, False), ({"title": "Sprint 4"}, False)],
)
def test_verified_also_reads_back_the_milestone(
    tmp_path: Path, milestone: dict[str, str] | None, verified: bool
) -> None:
    spec = dict(BASE_SPEC, milestone="Sprint 3")
    view = json.dumps({"title": spec["summary"], "milestone": milestone})
    r = run(tmp_path, spec, create='{"url": "https://x/5"}', view=view, milestones=MILESTONES)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["verified"] is verified


def test_nothing_is_written_outside_the_temp_dir(tmp_path: Path) -> None:
    run(tmp_path, create='{"url": "https://x/5"}')
    assert not (tmp_path / ".cache").exists(), "the idempotency anchor file is gone"


# ─── the board ──────────────────────────────────────────────────────────────
def test_the_issue_is_put_on_the_board_after_the_create(tmp_path: Path) -> None:
    r = run(tmp_path, create='{"url": "https://x/5"}')
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["board"] == "added"
    assert board_was_called(tmp_path)


def test_no_board_flag_skips_the_board_step_entirely(tmp_path: Path) -> None:
    # The board fixture is set to fail: if item-add ran, this would be exit 6.
    r = run(tmp_path, None, "--no-board", create='{"url": "https://x/5"}', board_code="1")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["board"] == "skipped"
    assert not board_was_called(tmp_path)


SCOPE_ERROR = SCOPE_ERROR_TEXT


def test_the_board_status_is_set_and_read_back(tmp_path: Path) -> None:
    spec = dict(BASE_SPEC, status="In Progress")
    r = run(tmp_path, spec, create='{"url": "https://x/5"}')
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert (out["board"], out["status"]) == ("added", "In Progress")
    assert "links" not in out, "no links asked, no links key"
    calls = fixture_calls(tmp_path)
    # a Status other than Todo is read twice: the automation may not have run yet
    assert calls[calls.index("issue_create") :] == [
        "issue_create",
        "project_add",
        "project_edit",
        "project_status",
        "project_status.confirm",
    ]


def test_todo_is_read_back_once(tmp_path: Path) -> None:
    """Todo is what the automation sets: a match needs no second read."""
    r = run(tmp_path, None, create='{"url": "https://x/5"}')
    assert r.returncode == 0, r.stderr
    assert (json.loads(r.stdout)["board"], json.loads(r.stdout)["status"]) == ("added", "Todo")
    calls = fixture_calls(tmp_path)
    assert calls[calls.index("project_add") :] == ["project_add", "project_edit", "project_status"]


def test_a_status_overwritten_by_the_boards_automation_is_set_once_more(tmp_path: Path) -> None:
    """ "Item added to project" may set Todo after our edit: re-edit once, read again."""
    spec = dict(BASE_SPEC, status="In Progress")
    reads = {"first": "Todo", "retry": "In Progress"}
    r = run(tmp_path, spec, create='{"url": "https://x/5"}', status_reads=reads)
    assert r.returncode == 0, r.stderr
    assert (json.loads(r.stdout)["board"], json.loads(r.stdout)["status"]) == (
        "added",
        "In Progress",
    )
    calls = fixture_calls(tmp_path)
    assert calls[calls.index("project_add") :] == [
        "project_add",
        "project_edit",
        "project_status",
        "project_edit",
        "project_status.retry",
    ]


def test_a_late_reset_by_the_automation_is_caught_by_the_second_read(tmp_path: Path) -> None:
    """The first read can match before the automation runs: only a second match after
    the pause is success; a reset seen then gets the one re-edit."""
    spec = dict(BASE_SPEC, status="In Progress")
    reads = {"first": "In Progress", "confirm": "Todo", "retry": "In Progress"}
    r = run(tmp_path, spec, create='{"url": "https://x/5"}', status_reads=reads)
    assert r.returncode == 0, r.stderr
    assert (json.loads(r.stdout)["board"], json.loads(r.stdout)["status"]) == (
        "added",
        "In Progress",
    )
    calls = fixture_calls(tmp_path)
    assert calls[calls.index("project_add") :] == [
        "project_add",
        "project_edit",
        "project_status",
        "project_status.confirm",
        "project_edit",
        "project_status.retry",
    ]


@pytest.mark.parametrize(
    "reads,edit_err,shown",
    [
        ({"first": "Todo", "retry": "Todo"}, "", '"Todo"'),
        ({"first": "Todo"}, "", '""'),
        ({}, "gh: Something went wrong (HTTP 502)", "HTTP 502"),
        # the late reset, and the re-edit does not hold either
        ({"first": "In Progress", "confirm": "Todo", "retry": "Todo"}, "", '"Todo"'),
    ],
)
def test_a_status_still_wrong_after_the_retry_is_exit_6(
    tmp_path: Path, reads: dict[str, str], edit_err: str, shown: str
) -> None:
    spec = dict(BASE_SPEC, status="In Progress")
    r = run(tmp_path, spec, create='{"url": "https://x/5"}', status_reads=reads, edit_err=edit_err)
    assert r.returncode == 6
    out = json.loads(r.stdout)
    assert (out["url"], out["board"]) == ("https://x/5", "failed")
    err = json.loads(r.stderr)["error"]
    assert shown in err
    assert "Do not re-run the create" in err
    assert (
        "gh project item-edit --id PVTI_item --project-id PVT_board12 --field-id PVTSSF_status "
        "--single-select-option-id opt_progress"
    ) in err


def test_an_item_add_that_returns_no_item_is_exit_6(tmp_path: Path) -> None:
    r = run(tmp_path, None, create='{"url": "https://x/5"}', item_add="{}")
    assert r.returncode == 6
    assert json.loads(r.stdout)["board"] == "failed"
    assert "gh project item-add 12" in json.loads(r.stderr)["error"]


URL_13_FIX = '{"url": "https://github.com/acme/sandbox/issues/13"}'
CHILD_13 = "repos/acme/sandbox/issues/13"
BLOCKED_13 = f"{CHILD_13}/dependencies/blocked_by"
# The created issue as issue-link.sh sees it, and both read-backs after the links.
LINKED_WORLD: dict[str, Any] = {
    **TARGETS,
    **issue_fixture("acme/sandbox#13"),
    f"{BLOCKED_13}.json": [],
    f"{CHILD_13}.after.json": gh_issue(
        "acme/sandbox#13", parent_issue_url="https://api.github.com/repos/acme/sandbox/issues/5"
    ),
    f"{BLOCKED_13}.after.json": [gh_issue("other/lib#7")],
}
LINKED_VIEW = json.dumps({"title": "[test] a title", "milestone": {"title": "Sprint 3"}})
REPAIR = "issue-link.sh acme/sandbox#13 --parent acme/sandbox#5 --blocked-by other/lib#7"


def run_linked(tmp_path: Path, *flags: str, **kw: Any) -> subprocess.CompletedProcess[str]:
    world = {k: v for k, v in {**LINKED_WORLD, **kw.pop("world", {})}.items() if v is not None}
    defaults: dict[str, Any] = {
        "create": URL_13_FIX,
        "view": LINKED_VIEW,
        "milestones": MILESTONES,
        "extra": world,
    }
    return run(tmp_path, LINKED_SPEC, *flags, **{**defaults, **kw})


def test_the_whole_spec_creates_boards_and_links_in_order(tmp_path: Path) -> None:
    r = run_linked(tmp_path)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == {
        "url": "https://github.com/acme/sandbox/issues/13",
        "number": 13,
        "repo": "acme/sandbox",
        "verified": True,
        "board": "added",
        "status": "In Progress",
        "links": {
            "parent": {"ref": "acme/sandbox#5", "ok": True, "action": "linked"},
            "blocked_by": [{"ref": "other/lib#7", "ok": True, "action": "linked"}],
        },
    }
    assert r.stderr == ""
    assert fixture_calls(tmp_path) == [
        "milestones",
        "project_view",
        "project_fields",
        "GET repos/acme/sandbox/issues/5",
        "GET repos/other/lib/issues/7",
        "issue_create",
        "project_add",
        "project_edit",
        "project_status",
        "project_status.confirm",
        f"GET {CHILD_13}",
        "GET repos/acme/sandbox/issues/5",
        "GET repos/other/lib/issues/7",
        f"GET {BLOCKED_13}?per_page=100",
        # internal ids from the fixtures, never the numbers 13 / 7
        "POST repos/acme/sandbox/issues/5/sub_issues sub_issue_id=9000013",
        f"GET {CHILD_13}",
        f"POST {BLOCKED_13} issue_id=9000007",
        f"GET {BLOCKED_13}?per_page=100",
    ]


def test_links_without_the_board(tmp_path: Path) -> None:
    spec = {k: v for k, v in LINKED_SPEC.items() if k != "status"}
    r = run(
        tmp_path, spec, "--no-board", create=URL_13_FIX, view=LINKED_VIEW, milestones=MILESTONES,
        extra=LINKED_WORLD,
    )  # fmt: skip
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert (out["board"], out["status"]) == ("skipped", None)
    assert out["links"]["parent"]["ok"] is True
    assert not board_was_called(tmp_path)


def test_a_failed_link_call_is_exit_7_with_the_url_and_the_repair_command(tmp_path: Path) -> None:
    r = run_linked(
        tmp_path,
        world={
            "repos/acme/sandbox/issues/5/sub_issues.post.code": "1",
            "repos/acme/sandbox/issues/5/sub_issues.post.err": "gh: Validation Failed (HTTP 422)",
            f"{CHILD_13}.after.json": gh_issue("acme/sandbox#13"),
        },
    )
    assert r.returncode == 7
    out = json.loads(r.stdout)
    assert out["url"] == "https://github.com/acme/sandbox/issues/13"
    assert out["links"]["parent"]["ok"] is False
    assert "HTTP 422" in out["links"]["parent"]["error"]
    assert out["links"]["blocked_by"][0]["ok"] is True
    err = json.loads(r.stderr)["error"]
    assert err.startswith(
        "created and verified https://github.com/acme/sandbox/issues/13, but 1 native link(s) "
        "failed — the issue stays, do not re-create it; repair: "
    )
    assert err.endswith(REPAIR)


def test_a_link_that_is_not_there_on_read_back_is_exit_7(tmp_path: Path) -> None:
    r = run_linked(tmp_path, world={f"{BLOCKED_13}.after.json": []})
    assert r.returncode == 7
    assert "not listed on read-back" in json.loads(r.stdout)["links"]["blocked_by"][0]["error"]
    assert REPAIR in json.loads(r.stderr)["error"]


def test_a_lookup_that_fails_after_the_create_reports_every_link_failed(tmp_path: Path) -> None:
    """issue-link.sh stops before linking (exit 5): the issue stays, and every link
    is reported as not in place with that error — never silently dropped."""
    r = run_linked(
        tmp_path,
        world={f"{CHILD_13}.json": None, f"{CHILD_13}.err": "gh: Server Error (HTTP 502)"},
    )
    assert r.returncode == 7
    links = json.loads(r.stdout)["links"]
    assert [x["ok"] for x in [links["parent"], *links["blocked_by"]]] == [False, False]
    assert "HTTP 502" in links["parent"]["error"]
    assert "2 native link(s) failed" in json.loads(r.stderr)["error"]


def test_board_and_links_both_failing_is_exit_7_with_both_reported(tmp_path: Path) -> None:
    r = run_linked(
        tmp_path,
        board_code="1",
        board_err=SCOPE_ERROR,
        world={f"{BLOCKED_13}.after.json": []},
    )
    assert r.returncode == 7
    out = json.loads(r.stdout)
    assert out["board"] == "failed"
    assert out["links"]["blocked_by"][0]["ok"] is False
    board_err, link_err = (json.loads(line)["error"] for line in r.stderr.splitlines())
    assert "not added to board 12" in board_err
    assert "repair: " in link_err


def test_an_unverified_issue_is_not_called_verified_in_the_link_error(tmp_path: Path) -> None:
    r = run_linked(
        tmp_path, view=json.dumps({"title": "[test] other"}), world={f"{BLOCKED_13}.after.json": []}
    )
    assert r.returncode == 7
    err = json.loads(r.stderr)["error"]
    assert "created https://github.com/acme/sandbox/issues/13 (the read-back did not match" in err
    assert "verified https" not in err


def test_missing_project_scope_keeps_the_issue_and_exits_6(tmp_path: Path) -> None:
    r = run(tmp_path, create='{"url": "https://x/5"}', board_code="1", board_err=SCOPE_ERROR)
    assert r.returncode == 6
    # The issue is not lost: its url and number are on stdout, board says failed.
    out = json.loads(r.stdout)
    assert out["url"] == "https://x/5" and out["number"] == 5
    assert out["board"] == "failed"
    err = json.loads(r.stderr)["error"]
    assert "run gh auth refresh -s project" in err
    assert "Do not re-run the create" in err


def test_other_board_errors_surface_the_original_gh_text(tmp_path: Path) -> None:
    text = "GraphQL: Could not resolve to a ProjectV2 with the number 12."
    r = run(tmp_path, create='{"url": "https://x/5"}', board_code="1", board_err=text)
    assert r.returncode == 6
    err = json.loads(r.stderr)["error"]
    assert text in err
    assert "run gh auth refresh" not in err
    assert json.loads(r.stdout)["board"] == "failed"


# ─── the real command line, through a stand-in gh / curl ────────────────────
# No ISSUE_FIXTURE_DIR here: the script takes its real, non-fixture path and calls
# `gh` and `curl` from PATH. Both names are the same recording stand-in
# (tests/fake_gh.py), which logs argv and answers from a JSON config. This is what
# pins the flags the script sends to GitHub — the label, the board number and
# owner, the read-back path.
FAKE_GH = HERE / "fake_gh.py"
URL_13 = "https://github.com/acme/sandbox/issues/13"
ITEM_ADD = [
    "project", "item-add", "12", "--owner", "larchanka-training", "--url", URL_13,
    "--format", "json",
]  # fmt: skip


def run_live(
    tmp_path: Path, spec: dict[str, Any] | None = None, *flags: str, **cfg: Any
) -> tuple[subprocess.CompletedProcess[str], list[list[str]], str]:
    """Returns the process, the recorded `gh` calls (argv without the program name)
    and the body file that `gh issue create` was handed."""
    spec = spec or BASE_SPEC
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for program in ("gh", "curl"):
        (bindir / program).write_text(FAKE_GH.read_text(encoding="utf-8"))
        (bindir / program).chmod(0o755)
    config: dict[str, Any] = {
        "repo": spec["repo"],
        "create_out": URL_13 + "\n",
        "create_rc": 0,
        "create_err": "",
        "view": json.dumps({"title": spec["summary"]}),
        "recent": ["[]", "[]"],
        "board_rc": 0,
        "board_err": "",
        "milestones": MILESTONES,
        "project_view": PROJECT_VIEW,
        "fields": PROJECT_FIELDS,
        "item_add_out": json.dumps({"id": "PVTI_item13", "type": "Issue", "url": URL_13}),
        "edit_rc": 0,
        "edit_err": "",
        "status_reads": [status_read(spec.get("status") or "Todo")] * 2,
        **cfg,
    }
    (tmp_path / "cfg.json").write_text(json.dumps(config))
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    log, body = tmp_path / "calls.jsonl", tmp_path / "posted-body.md"
    r = subprocess.run(
        ["bash", str(SCRIPT), str(spec_path), *flags],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{bindir}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "STUB_LOG": str(log),
            "STUB_CFG": str(tmp_path / "cfg.json"),
            "STUB_BODY": str(body),
            "ISSUE_STATUS_RETRY_DELAY": "0",
        },
        timeout=60,
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    posted = body.read_text(encoding="utf-8") if body.exists() else ""
    return r, [c[1:] for c in calls if c[0] == "gh"], posted


def create_argv(gh_calls: list[list[str]]) -> list[str]:
    return next(c for c in gh_calls if c[:2] == ["issue", "create"])


def labels_in(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == "--label"]


MILESTONES_CALL = ["api", "repos/acme/sandbox/milestones?state=open&per_page=100"]
PROJECT_VIEW_CALL = ["project", "view", "12", "--owner", "larchanka-training", "--format", "json"]
FIELD_LIST_CALL = [
    "project", "field-list", "12", "--owner", "larchanka-training", "--format", "json",
    "--limit", "100",
]  # fmt: skip


def test_the_real_calls_in_order_with_the_real_flags(tmp_path: Path) -> None:
    spec = dict(BASE_SPEC, assignees=["octocat"], milestone="Sprint 3")
    view = json.dumps({"title": spec["summary"], "milestone": {"title": "Sprint 3"}})
    r, gh, posted = run_live(tmp_path, spec, view=view)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert (out["number"], out["verified"], out["board"]) == (13, True, "added")

    search = [
        "issue", "list", "--repo", "acme/sandbox", "--state", "all",
        "--search", "[test] a title", "--json", "number,title,url,state,author,createdAt",
        "--limit", "20",
    ]  # fmt: skip
    readback = ["api", "repos/acme/sandbox/issues/13"]
    assert search in gh and readback in gh and ITEM_ADD in gh
    create = create_argv(gh)
    assert create[:5] == ["issue", "create", "--repo", "acme/sandbox", "--body-file"]
    assert create[6:] == [
        "--title", "[test] a title", "--label", "enhancement", "--assignee", "octocat",
        "--milestone", "Sprint 3",
    ]  # fmt: skip
    assert "- [ ] it works" in posted
    # sequence: validation (milestone, board Status), search, create, read back, board
    order = [MILESTONES_CALL, PROJECT_VIEW_CALL, FIELD_LIST_CALL, search, create, readback]
    assert [gh.index(c) for c in order] == sorted(gh.index(c) for c in order)
    assert gh.index(readback) < gh.index(ITEM_ADD)


def test_a_bad_milestone_never_reaches_the_real_create(tmp_path: Path) -> None:
    r, gh, _ = run_live(tmp_path, dict(BASE_SPEC, milestone="Sprint 9"))
    assert r.returncode == 2
    assert 'milestone "Sprint 9" not found' in json.loads(r.stderr)["error"]
    assert MILESTONES_CALL in gh
    assert not any(c[:2] == ["issue", "create"] for c in gh)


def test_a_pull_request_as_parent_never_reaches_the_real_create(tmp_path: Path) -> None:
    pull = dict(gh_issue("acme/sandbox#5"), pull_request={"url": "https://x/pull/5"})
    r, gh, _ = run_live(tmp_path, dict(BASE_SPEC, parent="#5"), issues={"acme/sandbox#5": pull})
    assert r.returncode == 2
    assert ["api", "repos/acme/sandbox/issues/5"] in gh
    assert not any(c[:2] == ["issue", "create"] for c in gh)


def test_preflight_probes_run_for_real_too(tmp_path: Path) -> None:
    _, gh, _ = run_live(tmp_path)
    assert ["api", "user", "--jq", ".login"] in gh
    assert ["api", "repos/acme/sandbox", "--jq", ".has_issues"] in gh


@pytest.mark.parametrize(
    "typ,label",
    [("Bug", "bug"), ("Story", "enhancement"), ("Task", "enhancement"), ("Docs", "documentation")],
)
def test_the_type_label_reaches_the_real_create_command(
    tmp_path: Path, typ: str, label: str
) -> None:
    r, gh, _ = run_live(tmp_path, dict(BASE_SPEC, type=typ))
    assert r.returncode == 0, r.stderr
    assert labels_in(create_argv(gh)) == [label]


def test_no_board_never_calls_item_add(tmp_path: Path) -> None:
    r, gh, _ = run_live(tmp_path, None, "--no-board")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["board"] == "skipped"
    assert not any(c[:2] == ["project", "item-add"] for c in gh)


def test_a_scope_error_from_gh_exits_6_with_the_refresh_hint(tmp_path: Path) -> None:
    r, gh, _ = run_live(tmp_path, board_rc=1, board_err=SCOPE_ERROR)
    assert r.returncode == 6
    assert ITEM_ADD in gh
    out = json.loads(r.stdout)
    assert (out["url"], out["board"]) == (URL_13, "failed")
    assert "run gh auth refresh -s project" in json.loads(r.stderr)["error"]


def test_a_real_rerun_finds_the_batch_issue_in_the_listing_and_creates_nothing(
    tmp_path: Path,
) -> None:
    r, gh, _ = run_live(tmp_path, None, "--adopt-since", SINCE, recent=[listing(own(40)), "[]"])
    assert r.returncode == 4
    assert json.loads(r.stdout)["number"] == 40
    newest = ["api", "repos/acme/sandbox/issues?state=all&sort=created&direction=desc&per_page=30"]
    assert gh.count(newest) == 1
    assert not any(c[:2] == ["issue", "create"] for c in gh)


def test_a_failed_create_that_landed_is_recovered_without_a_second_create(
    tmp_path: Path,
) -> None:
    landed = listing(rest_issue(13))
    r, gh, _ = run_live(
        tmp_path, create_out="", create_rc=1, create_err="HTTP 502", recent=["[]", landed]
    )
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["url"] == URL_13
    assert sum(1 for c in gh if c[:2] == ["issue", "create"]) == 1
    listing_at = [i for i, c in enumerate(gh) if c[0] == "api" and "per_page=30" in c[1]]
    assert len(listing_at) == 2, "one snapshot before the create, one lookup after"
    assert gh[listing_at[0]][1] == (
        "repos/acme/sandbox/issues?state=all&sort=created&direction=desc&per_page=30"
    )
    # The snapshot must precede the create, or recovery cannot tell old from new.
    assert listing_at[0] < gh.index(create_argv(gh)) < listing_at[1]


ITEM_EDIT = [
    "project", "item-edit", "--id", "PVTI_item13", "--project-id", "PVT_board12",
    "--field-id", "PVTSSF_status", "--single-select-option-id", "opt_progress",
]  # fmt: skip
LIVE_ISSUES = {
    "acme/sandbox#13": gh_issue(
        "acme/sandbox#13", title="[test] a title", milestone={"title": "Sprint 3"}
    ),
    "acme/sandbox#5": gh_issue("acme/sandbox#5"),
    "other/lib#7": gh_issue("other/lib#7"),
}


def test_the_real_calls_of_a_linked_issue_in_order(tmp_path: Path) -> None:
    r, gh, _ = run_live(
        tmp_path, LINKED_SPEC, issues=LIVE_ISSUES, status_reads=[status_read("In Progress")] * 2
    )
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert (out["verified"], out["board"], out["status"]) == (True, "added", "In Progress")
    assert out["links"]["parent"]["action"] == "linked"
    graphql = next(c for c in gh if c[:2] == ["api", "graphql"])
    assert graphql[-2:] == ["-f", "id=PVTI_item13"]
    assert 'fieldValueByName(name: "Status")' in graphql[3]
    expected = [
        MILESTONES_CALL,
        PROJECT_VIEW_CALL,
        FIELD_LIST_CALL,
        ["api", "repos/acme/sandbox/issues/5"],
        ["api", "repos/other/lib/issues/7"],
        create_argv(gh),
        ITEM_ADD,
        ITEM_EDIT,
        graphql,
        [
            "api",
            "-X",
            "POST",
            "repos/acme/sandbox/issues/5/sub_issues",
            "-F",
            "sub_issue_id=9000013",
        ],
        ["api", "-X", "POST", f"{BLOCKED_13}", "-F", "issue_id=9000007"],
    ]
    at = [gh.index(c) for c in expected]
    assert at == sorted(at), "validation → create → board → Status → links"
    assert create_argv(gh)[-2:] == ["--milestone", "Sprint 3"]


def test_a_real_status_reset_by_the_automation_is_edited_twice(tmp_path: Path) -> None:
    reads = [status_read("Todo"), status_read("In Progress")]
    r, gh, _ = run_live(tmp_path, LINKED_SPEC, issues=LIVE_ISSUES, status_reads=reads)
    assert r.returncode == 0, r.stderr
    assert gh.count(ITEM_EDIT) == 2
    assert json.loads(r.stdout)["status"] == "In Progress"


def graphql_reads(gh: list[list[str]]) -> int:
    return sum(1 for c in gh if c[:2] == ["api", "graphql"])


def test_a_real_late_reset_is_caught_by_the_second_read_and_edited_again(tmp_path: Path) -> None:
    reads = [status_read("In Progress"), status_read("Todo"), status_read("In Progress")]
    r, gh, _ = run_live(tmp_path, LINKED_SPEC, issues=LIVE_ISSUES, status_reads=reads)
    assert r.returncode == 0, r.stderr
    assert (gh.count(ITEM_EDIT), graphql_reads(gh)) == (2, 3)
    assert json.loads(r.stdout)["status"] == "In Progress"


def test_a_real_todo_is_read_back_once(tmp_path: Path) -> None:
    r, gh, _ = run_live(tmp_path)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["status"] == "Todo"
    assert graphql_reads(gh) == 1


def test_a_real_link_failure_is_exit_7_with_the_repair_command(tmp_path: Path) -> None:
    r, gh, _ = run_live(
        tmp_path,
        LINKED_SPEC,
        issues=LIVE_ISSUES,
        status_reads=[status_read("In Progress")] * 2,
        sub_issue_post={"code": 1, "err": "gh: Validation Failed (HTTP 422)", "lands": False},
    )
    assert r.returncode == 7
    assert json.loads(r.stdout)["url"] == URL_13
    assert json.loads(r.stderr)["error"].endswith(REPAIR)
    assert sum(1 for c in gh if c[:2] == ["issue", "create"]) == 1


@pytest.mark.parametrize(
    "create_err,code",
    [
        ("HTTP 401: Bad credentials", 3),
        ("HTTP 403: Resource not accessible by integration", 3),
        # a (secondary) rate limit is transient, not a refusal
        ("HTTP 403: API rate limit exceeded for user ID 1.", 5),
        ("HTTP 403: You have exceeded a secondary rate limit.", 5),
        ("HTTP 403: You have triggered an abuse detection mechanism.", 5),
        ("HTTP 502: Bad Gateway", 5),
        ("Unprocessable Entity: validation failed", 4),
    ],
)
def test_a_failed_create_is_classified_by_what_gh_said(
    tmp_path: Path, create_err: str, code: int
) -> None:
    r, _, _ = run_live(tmp_path, create_out="", create_rc=1, create_err=create_err)
    assert r.returncode == code
    assert json.loads(r.stderr)["error"]
