<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/create-issue/examples/01-single/preview.md -->

Batch `copy-run-link`: 1 issue, in creation order.

| # | Key | Repo | Type | Title | Parent | Blocked by | Milestone | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `copy-run-link` | larchanka-training/dmc-268-ui-t6 | Task | [ui] Добавить кнопку «Скопировать ссылку» на страницу прогона | — | — | — | Todo |

# 1. `copy-run-link` — [ui] Добавить кнопку «Скопировать ссылку» на страницу прогона

## Background

Ссылку на прогон сейчас копируют из адресной строки браузера, чтобы отправить её в чат команды.

## Scope

- Кнопка «Скопировать ссылку» в шапке страницы прогона копирует адрес прогона в буфер обмена и показывает подтверждение
- Вне scope: ссылки на отдельные находки и любые правки api

## Acceptance Criteria

- [ ] На странице прогона кнопка «Скопировать ссылку» кладёт в буфер обмена адрес текущего прогона
- [ ] После копирования кнопка показывает подтверждение «Скопировано»
- [ ] Тест страницы прогона проверяет вызов `navigator.clipboard.writeText` с адресом прогона

## Dependencies

Нет

## DoD Checklist

- [ ] Gates green (AGENTS.md → Commands)
- [ ] Docs updated
- [ ] PR approved, all review threads resolved
- [ ] Issue status updated on board 12

## Kickoff — с чего начать

**Репозиторий:** larchanka-training/dmc-268-ui-t6

**Ветка:** feat/<n>-copy-run-link — `main` защищён, через PR

**Форма проблемы:** На странице прогона нет действия, которое отдаёт ссылку на прогон; адрес есть только в строке браузера.

**Что прочитать первым:**

- src/pages/runs/RunDetailPage.tsx — шапка страницы, куда встаёт кнопка
- src/pages/runs/RunDetailPage.test.tsx — как страница рендерится в тестах

**Перед тем как писать код:**

- pnpm test src/pages/runs — зелёная база до правок

**Мины:**

- в jsdom `navigator.clipboard` не определён — в тесте его подменяют, иначе тест падает на undefined
