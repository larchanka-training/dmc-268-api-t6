<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/create-issue/examples/03-ui-api/preview.md -->

Batch `runs-pr-filter`: 2 issues, in creation order.

| # | Key | Repo | Type | Title | Parent | Blocked by | Milestone | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `api-pr-filter` | larchanka-training/dmc-268-api-t6 | Task | [api] Фильтровать список прогонов по номеру PR | — | — | — | Todo |
| 2 | `ui-pr-filter` | larchanka-training/dmc-268-ui-t6 | Story | [ui] Добавить фильтр списка прогонов по номеру PR | @api-pr-filter | @api-pr-filter | — | Todo |

# 1. `api-pr-filter` — [api] Фильтровать список прогонов по номеру PR

## Background

Прогоны одного PR приходится искать глазами в общем списке: `GET /api/runs` не умеет отбирать прогоны по номеру PR.

## Scope

- Параметр `pull_number` в `GET /api/runs`: только прогоны PR с этим номером; вместе с `repo` — PR этого репозитория
- Вне scope: поле фильтра в ui — это sub-issue в ui

## Acceptance Criteria

- [ ] `GET /api/runs?pull_number=N` возвращает только прогоны PR с номером N; пагинация через `cursor` работает как без фильтра
- [ ] `pull_number` меньше 1 — ответ 422
- [ ] Параметр описан в OpenAPI; тесты use case и роута зелёные

## Dependencies

**Блокирует:**

- sub-issue в ui — поле «PR #» в фильтре списка прогонов ждёт этот параметр

## DoD Checklist

- [ ] Gates green (AGENTS.md → Commands)
- [ ] Docs updated
- [ ] PR approved, all review threads resolved
- [ ] Issue status updated on board 12

## Kickoff — с чего начать

**Репозиторий:** larchanka-training/dmc-268-api-t6

**Ветка:** feat/<n>-runs-pr-filter — `main` защищён, через PR

**Форма проблемы:** Роут `GET /runs` фильтрует по статусу и репозиторию; номера PR среди параметров нет, ни в роуте, ни в use case.

**Что прочитать первым:**

- app/main.py — роут `GET /runs` и его параметры
- app/modules/reviews/application/list_runs.py — use case и запрос к репозиторию прогонов

**Перед тем как писать код:**

- uv run pytest tests -k list_runs — зелёная база до правок

**Мины:**

- номер PR не уникален между репозиториями — без `repo` фильтр отдаёт прогоны PR с этим номером из всех видимых репозиториев
- клиент передаёт курсор следующей страницы вместе с теми же фильтрами — нужен тест на вторую страницу с `pull_number`

# 2. `ui-pr-filter` — [ui] Добавить фильтр списка прогонов по номеру PR

## User Story

**AS** участник команды, который разбирает прогоны
**I WANT** отфильтровать список прогонов по номеру PR
**SO THAT** сразу видеть все прогоны своего PR

## Scope

- Поле «PR #» в фильтре над списком прогонов; значение уходит параметром `pull_number` в `GET /api/runs` и хранится в адресе страницы
- Вне scope: правки api — они в родительском issue

## Acceptance Criteria

- [ ] Номер в поле «PR #» оставляет в списке только прогоны этого PR
- [ ] Номер хранится в адресе страницы: после перезагрузки список отфильтрован так же
- [ ] Тесты фильтра и запроса списка проверяют параметр `pull_number`

## Dependencies

**Блокирующие:**

- родительский issue в api — параметр `pull_number` в `GET /api/runs` должен быть на staging

## DoD Checklist

- [ ] Gates green (AGENTS.md → Commands)
- [ ] Docs updated
- [ ] PR approved, all review threads resolved
- [ ] Issue status updated on board 12

## Kickoff — с чего начать

**Репозиторий:** larchanka-training/dmc-268-ui-t6

**Ветка:** feat/<n>-runs-pr-filter — `main` защищён, через PR

**Форма проблемы:** Фильтр списка прогонов знает только репозиторий (`repo` в адресе страницы); номера PR нет ни в фильтре, ни в схеме запроса.

**Что прочитать первым:**

- src/features/run-list-filter/model/runListFilter.ts — фильтр и его параметры в адресе страницы
- src/entities/run/model/schemas.ts — `RunListQuerySchema`, по ней собирается запрос списка

**Перед тем как писать код:**

- pnpm test src/features/run-list-filter src/entities/run — зелёная база до правок

**Мины:**

- `RunListQuerySchema` — `z.object`, он молча отбрасывает незнакомые ключи: без `pull_number` в схеме параметр не уйдёт в запрос
- пока параметра нет на staging, api отвечает без фильтра — ручная проверка до мержа api даёт ложное «работает»
