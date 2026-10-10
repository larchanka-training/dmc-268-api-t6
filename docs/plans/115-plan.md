# План #115: AuthScope и installation token cache

## Цель и границы

По [#115](https://github.com/larchanka-training/dmc-268-api-t6/issues/115)
актуализировать два абзаца `docs/SYSTEM_DESIGN.md` и правило AuthScope в
`.agents/rules/backend.md`. База: `92250a1fb3b65f88a2ea111d33613f9a4af1d1d2`.
В issue указан §6.9, но целевые абзацы сейчас находятся в **§7.3**.
Изменения только документальные; runtime/OpenAPI, зависимости, тестовый код,
соседние изменения #108/#122 и область #107 не затрагиваются. Обязательные
комментарии о scope опубликованы в PR #108/#122; перед правкой сверить их diff.

## Доказательства из кода

| Контракт | Файлы → символы |
| --- | --- |
| Три места создания кэша | `app/webhook_worker.py` → `run_forever`; `app/worker.py` → `review_worker`, `github_adapters` |
| Условная композиция review-worker | `review_worker`: кэш создаётся при отсутствии внедрённого `vcs_provider` и настроенном GitHub App |
| Process-local cache, запас 60 с | `app/modules/integrations/webhooks/infrastructure/github_installation_tree_provider.py` → `InMemoryInstallationAccessTokenCache.get`, `_INSTALLATION_TOKEN_SAFETY_SKEW_SECONDS` |
| Run/cancel | `app/modules/reviews/infrastructure/run_repository.py` → `SqlAlchemyRunRepository`, `SqlAlchemyCancelRunUnitOfWork`; `authorized_run` также защищает предикат |
| Rerun | `app/modules/reviews/infrastructure/rerun_store.py` → `SqlAlchemyRerunStore`, `SqlAlchemyRerunUnitOfWork` |
| Список PR | `app/modules/reviews/infrastructure/pull_request_queries.py` → `SqlAlchemyPullRequestQueries` |
| Настройки repository | `app/modules/repositories/infrastructure/repository_settings.py` → `SqlAlchemyRepositorySettingsStore`, `SqlAlchemyRepositorySettingsUnitOfWork` |
| Явный внутренний обход | `app/worker.py` → `process_review_run`, `github_adapters`; `app/modules/reviews/infrastructure/run_processing_unit_of_work.py` → `SqlAlchemyRunProcessingUnitOfWork.repository` |

Все семь конструкторов отвергают `scope is None and not allow_unscoped`
с `ValueError`. Действующие проверки — `tests/test_sqlalchemy_run_repository.py`
и `tests/test_rerun_scope.py`; новые runtime-тесты для правки текста не нужны.

## Зеркала

По `.agents/README.md` backend — per-repo, UI-копия не требуется. Общие skills,
agents, git-workflow и PR template не меняются; `.claude/**` — symlink,
`CLAUDE.md` импортирует AGENTS. AGENTS дублирует backend §1, §2 и транзакционное
правило, но не абзац AuthScope §3: AGENTS не менять, если дублируемые пункты
остаются прежними. При их изменении синхронизировать обе стороны SYNC-маркера.

## Порядок и проверка

1. В двух абзацах §7.3 перечислить семь адаптеров и явный `allow_unscoped=True`
   для доверенных внутренних вызовов; указать три места создания кэша через
   относительные ссылки на файлы и символы без номеров строк. Сохранить
   process-local семантику, запас 60 с и существующий статус Redis/#62.
2. Согласовать правило backend §3 и проверить зеркала. Контрольная точка:
   оба AC подтверждены кодом, diff ограничен согласованной областью.
3. Проверить через `rg` полный список `scope: AuthScope | None` и созданий кэша,
   ссылки и diff; выполнить все четыре uv-гейта (команды в `115-todo.md`).
   Записать фактические результаты, включая skips integration-тестов.

Работы последовательные; подробные критерии — в `115-todo.md`. Перед реализацией
пользователь утверждает план. Затем Standards/Spec review, rebase на main,
повторная сверка с кодом и PR на русском: `What / Why / How to verify / Refs`,
Refs #115/#106, без автозакрытия и Development-panel link. Approve на текущем
head, разрешённые треды и проверка AC/закрытие техлидом, результат в #106 и
статус доски 12 — последующие требования DoD, не объявлять их выполненными заранее.
