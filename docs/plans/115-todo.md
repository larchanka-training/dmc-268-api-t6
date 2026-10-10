# Задачи #115

## 1. SYSTEM_DESIGN (S, 1 файл)

**Описание:** Обновить два абзаца §7.3 по символам из `115-plan.md`.

**Критерии приёмки:**

- [x] Все семь scope-адаптеров перечислены; без scope — `ValueError`, обход
      только явным `allow_unscoped=True` для доверенных внутренних вызовов.
- [x] Указаны три места создания кэша через файлы/символы без номеров строк;
      условная композиция `review_worker`, process-local семантика, 60 с и #62 сохранены.
- [x] Diff ограничен двумя абзацами, изменения #108/#122 и область #107 сохранены.

**Проверка:** Сверить через `rg` и чтение конструкторов/функций; проверить ссылки
и diff. **Зависимости:** Одобрение плана, сверка diff PR #108/#122 (scope-комментарии
уже опубликованы). **Файл:** `docs/SYSTEM_DESIGN.md`.

## 2. Правило backend (S, 1 файл)

**Описание:** Согласовать fail-closed правило backend §3 с SYSTEM_DESIGN.

**Критерии приёмки:**

- [x] Правило охватывает все адаптеры с `scope: AuthScope | None`, содержит
      семь актуальных примеров и явный внутренний обход.
- [x] Формулировки согласованы с SYSTEM_DESIGN и кодом.
- [x] Зеркала проверены по README/SYNC; backend per-repo, AGENTS не меняется
      без изменения дублируемых пунктов.

**Проверка:** Сопоставить текст, код, sync map и итоговый diff.
**Зависимости:** Задача 1. **Файл:** `.agents/rules/backend.md`.

## Контрольная точка после задач 1–2

- [x] Оба AC #115 подтверждены; ссылки/символы корректны, diff в согласованных
      границах, без runtime/OpenAPI изменений и искусственных тестов документации.

## 3. Проверки и ревью (S, проверки + статус задач)

**Описание:** Выполнить гейты и передать diff на Standards/Spec review.

**Критерии приёмки:**

- [x] Четыре гейта зелёные; фактические результаты и skips записаны.
- [x] Standards/Spec review завершены, замечания исправлены и повторно проверены.
- [x] Rebase на main выполнен; русский Draft PR подготовлен по процессу из плана.

**Проверка:**

- [x] `uv run ruff check .`
- [x] `uv run ruff format --check .`
- [x] `uv run mypy .`
- [x] `uv run pytest`
- [x] `git diff --check`, финальное чтение diff и проверка ссылок/символов.

**Зависимости:** Задачи 1–2. **Файл:** `docs/plans/115-todo.md` (статус/результаты).

## Фактические результаты

- База реализации: `92250a1fb3b65f88a2ea111d33613f9a4af1d1d2`.
- Задачи 1–2: изменены только два абзаца SYSTEM_DESIGN §7.3 и правило backend §3;
  runtime, OpenAPI, зависимости и тестовый код не менялись.
- Полный поиск `scope: AuthScope | None` в `app/`: семь конструкторов адаптеров,
  `authorized_run` и фабрика `ReviewsApiResources.run_repository`.
  Все семь конструкторов проверены: `scope is None and not allow_unscoped` → `ValueError`.
- Поиск созданий кэша и AST-сверка подтвердили ровно три функции: `run_forever`,
  `review_worker`, `github_adapters`. Условие композиции `review_worker`,
  process-local семантика и `_INSTALLATION_TOKEN_SAFETY_SKEW_SECONDS = 60` сверены с кодом.
- Относительные Python-ссылки в обоих документах существуют, символы подтверждены;
  `git diff --check` завершился успешно, diff прочитан.
- PR #108 добавляет вводный текст §7.3 и меняет другие разделы, целевые два абзаца
  не затрагивает; PR #122 меняет только абзац Access §9.5. Их scope сохранён.
- Зеркала: backend — per-repo по `.agents/README.md`; изменён только AuthScope §3,
  дублируемые в AGENTS пункты §1/§2 и transaction rule не менялись.
  `CLAUDE.md` импортирует AGENTS, `.claude/skills` и `.claude/agents` — symlink.
- `uv run ruff check .`: **All checks passed!**
- `uv run ruff format --check .`: **346 files already formatted**.
- `uv run mypy .`: **Success: no issues found in 343 source files**.
- `uv run pytest`: **2257 passed, 128 skipped, 2 warnings in 135.63s** (2385 collected).
  128 skips — opt-in интеграционные тесты PostgreSQL/RabbitMQ: `TEST_DATABASE_URL`
  и `TEST_RABBITMQ_URL` не заданы. Два предупреждения — deprecation из
  `starlette.testclient`/`anyio`, не ошибки проверок.
- Независимые Standards/Spec review завершены: **0 findings** по обоим направлениям.
- Перед публикацией выполнены `git fetch origin main` и
  `git rebase --autostash origin/main`: ветка уже актуальна, база осталась
  `92250a1fb3b65f88a2ea111d33613f9a4af1d1d2`, изменения восстановлены без конфликтов.
- Для публикации подготовлен русский Draft PR: `What / Why / How to verify / Refs`,
  Refs #115/#106 и Before merge checklist. Reviewer не выбран; approve на текущем
  head, разрешение тредов, merge и приёмка техлидом остаются последующими шагами.
  Issue остаётся In Progress с assignee `ndovnar`; новых зависимостей нет.
