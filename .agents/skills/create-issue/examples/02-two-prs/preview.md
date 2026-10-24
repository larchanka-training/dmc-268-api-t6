<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/create-issue/examples/02-two-prs/preview.md -->

Batch `sarif-export`: 1 issue, in creation order.

| # | Key | Repo | Type | Title | Parent | Blocked by | Milestone | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `sarif-export` | larchanka-training/dmc-268-api-t6 | Task | [api] Выгружать находки прогона в SARIF | — | — | — | Todo |

# 1. `sarif-export` — [api] Выгружать находки прогона в SARIF

## Background

Находки прогона видны только в комментариях к PR и в нашем ui. SARIF позволяет отдать их стандартным инструментам и показать во вкладке Security репозитория.

## Scope

- Вне scope: показ SARIF в ui и загрузка в другие хранилища отчётов
- Ч.1 (отдельный PR): Эндпоинт `GET /api/runs/{id}/findings.sarif` отдаёт находки прогона в SARIF 2.1.0
  - Граница PR: только чтение: сериализация, эндпоинт, тесты и описание в OpenAPI; в GitHub ничего не отправляется
  - Готово, когда: `uv run pytest tests -k sarif` — зелёный; ответ на прогон с находками проходит проверку схемой SARIF 2.1.0 в тесте
- Ч.2 (отдельный PR): По завершении прогона SARIF загружается в GitHub code scanning репозитория
  - Граница PR: загрузка после коммита транзакции и обработка её ошибок; эндпоинт Ч.1 не меняется
  - Готово, когда: `uv run pytest tests -k sarif_upload` — зелёный: тест с фейковым GitHub видит один POST `code-scanning/sarifs` с `commit_sha` головы PR

## Acceptance Criteria

- [ ] Ч.1: `GET /api/runs/{id}/findings.sarif` возвращает SARIF 2.1.0 с одним `result` на находку (файл, строка, severity, правило)
- [ ] Ч.1: Чужой или несуществующий прогон — 404, как у `GET /api/runs/{id}`
- [ ] Ч.2: После завершения прогона в GitHub уходит ровно один POST `code-scanning/sarifs` с `commit_sha` и `ref` PR
- [ ] Ч.2: Ошибка загрузки не меняет статус прогона и пишется в лог с `run_id`

## Dependencies

Нет

## DoD Checklist

- [ ] Gates green (AGENTS.md → Commands)
- [ ] Docs updated
- [ ] PR approved, all review threads resolved
- [ ] Issue status updated on board 12

## Kickoff — с чего начать

**Репозиторий:** larchanka-training/dmc-268-api-t6

**Ветка:** feat/<n>-sarif-export — `main` защищён, по PR на часть

**Форма проблемы:** Находки хранятся как комментарии прогона; формата выгрузки нет, а публикация в GitHub знает только комментарии к PR.

**Что прочитать первым:**

- app/modules/reviews/application/get_run_comments.py — откуда берутся находки прогона
- app/modules/reviews/application/publish_run_review.py — где прогон публикует результат в GitHub (Ч.2)
- app/main.py — роуты `/runs`, рядом встаёт новый

**Перед тем как писать код:**

- uv run pytest tests -k run_comments — зелёная база до правок

**Мины:**

- AGENTS.md: транзакция БД не охватывает вызов GitHub — загрузка SARIF идёт после коммита
- code scanning принимает поле `sarif` только сжатым gzip и в base64 — сырой JSON вернёт 400
