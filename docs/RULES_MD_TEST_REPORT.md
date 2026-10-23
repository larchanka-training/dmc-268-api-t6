# Отчёт: тестирование `.review/rules.md` (#163)

> Отчёт закоммичен в репозиторий: артефакты `eval-live` хранятся только 14 дней, а результаты этапов должны пережить их. Каталог сценариев и покрытие строк RFS §11 — [TEST_PLAN §2.8](TEST_PLAN.md#28-каталог-сценариев-reviewrulesmd-163).

## Этап 1 — сквозной E2E

**Что проверяется.** `tests/test_rules_md_e2e_postgres.py`: подписанный webhook `pull_request labeled` проходит через ASGI-приложение, настоящий receiver проецирует его, и приложение само создаёт Run и публикует его в RabbitMQ; настоящий `run_worker` загружает политику закреплённой base-ревизии (`policy.load`), анализирует diff с fake LLM и публикует ревью в fake GitHub. Тест не вставляет Run. Ожидаемые значения — литералы [RULES_FORMAT_SPEC](RULES_FORMAT_SPEC.md) §3, §4, §9 и §11.

**Окружение.**

| Параметр | Значение |
| --- | --- |
| Дата | 2026-10-23 |
| База | `main` `22c621ab` |
| PR | этап 1 по #163 |
| Локально | PostgreSQL 17.11 и RabbitMQ 4.3.6, своя БД и отдельный vhost |
| CI | обязательный job `Python lint / type / test` (service containers `postgres:17-alpine`, `rabbitmq:4-management-alpine`), шаг «Integration suite (zero skips)», маркер `integration` |
| Изоляция | одна мигрированная схема на модуль; перед каждым сценарием все таблицы, кроме `alembic_version`, очищаются (`TRUNCATE … RESTART IDENTITY CASCADE`) и заполняются тем же seed с фиксированными id, топология очередей пересоздаётся |
| Время | 23 теста; 87–98 с при load average 15–19 (машину делили параллельные сессии); первая версия файла (19 тестов, схема мигрировалась в каждом сценарии) — 70 с на свободной машине. Подготовка сценария: миграция 1,35–1,49 с против очистки и seed 0,53–0,69 с |

**Конфигурация → итог.** Все 23 теста пройдены, пропусков нет.

| Сценарий | Конфигурация `.review/rules.md` в base | Итог |
| --- | --- | --- |
| missing | нет записи `.review` | `succeeded`, `presence=missing`, оба изменённых файла доходят до модели, одно ревью на head |
| valid | секции Markdown и блок `review-ignore` с `generated/**` | `succeeded`, `presence=present`, ни путь, ни содержимое `generated/client.py` не попадают ни в один вызов LLM, одно ревью |
| empty | 0 байт | `succeeded`, `presence=present`, `raw_sha256 = e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`, `byte_count = 0`, `policy_digest` отличается от missing |
| at-limit | ровно 65 536 байт | `succeeded`, `presence=present`, `byte_count = 65536` |
| all-ignored | блок с `app/**` и `generated/**` | `skipped`, `all_changes_ignored`, 0 LLM, ревью нет |
| decode | невалидный UTF-8 | `failed`, `policy_document_decode_error`, `attempt = 1`, 0 LLM |
| control | символ ESC (U+001B) | `failed`, `policy_document_control_character`, `attempt = 1`, 0 LLM |
| duplicate | два блока `review-ignore` | `failed`, `policy_ignore_block_duplicate`, `attempt = 1`, 0 LLM |
| nested | блок внутри элемента списка | `failed`, `policy_ignore_block_nested`, `attempt = 1`, 0 LLM |
| unclosed | fence без закрытия | `failed`, `policy_ignore_block_unclosed`, `attempt = 1`, 0 LLM |
| info | `review-ignore extra` | `failed`, `policy_ignore_info_invalid`, `attempt = 1`, 0 LLM |
| pattern | маска `a?.py` | `failed`, `policy_ignore_pattern_invalid`, `attempt = 1`, 0 LLM |
| too-large | 65 537 байт | `failed`, `policy_document_too_large`, `attempt = 1`, 0 LLM |
| limit | 257 масок | `failed`, `policy_ignore_limit_exceeded`, `attempt = 1`, 0 LLM |
| symlink | rules.md — symlink (mode `120000`) | `failed`, `policy_document_type_unsupported`, `attempt = 1`, 0 LLM |
| root-404 | 404 на дерево base, `.review` нет | `failed`, `policy_revision_unavailable`, `attempt = 1`, 0 LLM |
| root-denied | 403 без rate limit на дерево base | `failed`, `policy_access_denied`, `attempt = 1`, 0 LLM |
| tree-404 | 404 на поддерево `.review` | `failed`, `policy_revision_unavailable`, `attempt = 1`, 0 LLM |
| blob-404 | 404 на blob rules.md | `failed`, `policy_revision_unavailable`, `attempt = 1`, 0 LLM |
| denied | 403 без rate limit на поддерево `.review` | `failed`, `policy_access_denied` (не `cancelled`), `attempt = 1`, 0 LLM |
| truncated | листинг base с `truncated: true`, `.review` нет | `failed`, `policy_revision_unavailable`, `attempt = 1`, 0 LLM |
| head-added | `.review` нет; PR добавляет rules.md с `**/*` | `succeeded`, `presence=missing`, не `skipped` |
| head-changed | текст без масок; PR меняет его на `**/*` | `succeeded`, `raw_sha256` — от байтов base, не `skipped` |

В графе «Итог» записан фактический код прогона. Тест для 404 допускает обе строки §9 (`policy_revision_unavailable` или `policy_access_denied`), а для truncated — `policy_revision_unavailable` или `policy_fetch_transient`.

**Мутационная проверка.** Каждая мутация вносилась в код приложения, затем прогонялся весь файл и правка откатывалась (`git checkout`). Все мутации прогонялись на окончательной версии теста.

| # | Мутация | Результат | Покраснел тест |
| --- | --- | --- | --- |
| M1 | `github_policy_source.py`: 404 на поддерево политики → документ missing | красный | tree-404 |
| M2 | `policy_documents.py`: файл в 0 байт считается missing | красный | empty: равны сами `policy_digest` empty и missing |
| M3 | `review_context_stage.py:329-350`: снят только первый `RunSkipped("all_changes_ignored")` | зелёный, эквивалентная мутация: второй guard `review_context_stage.py:372-373` (по статусу из `review_context.py:386-387`) пропускает тот же run; по PIPELINE_SPEC оба пути заканчиваются на `context.build` | — |
| M3b | сняты оба места `RunSkipped("all_changes_ignored")` | красный | all-ignored: `failed` вместо `skipped` |
| M4 | `policy_snapshot_unit_of_work.py`: снимок закрепляет `head_sha` вместо `base_sha` | красный | head-added, head-changed, truncated, root-404, root-denied |
| M5 | `run_failures.py`: `policy_document_decode_error` становится повторяемым | красный | decode: `attempt = 3` |
| M6 | `github_policy_source.py`: 404 на корневое дерево base → документ missing | красный | root-404 |

**Что не проверяется и почему.**

- Текст rules.md в prompt модели — ни наличие, ни отсутствие. На `main` текст до модели не доходит (`app/modules/reviews/infrastructure/review_prompt_repository.py:73-83`, `app/worker.py:277-280`) — это открытый остаток #130 (AC9). Утверждение об отсутствии закрепило бы дефект, утверждение о наличии сегодня падает.
- Точный код усечённого дерева: код даёт терминальный `policy_revision_unavailable`, а RFS §9 относит неполную загрузку к повторяемому `policy_fetch_transient`. До решения #247 ч.1 тест допускает оба кода и не закрепляет число попыток.
- Коды бюджета проверяются только unit-тестами: `policy_match_budget_exceeded` — `tests/test_review_ignore.py::test_exhausted_budget_stops_the_run`, `policy_prompt_budget_exceeded` на `main` недостижим (остаток #130). Когда текст rules.md попадёт в prompt, сценарий at-limit (65 536 байт) может упереться в бюджет 12 000 токенов (RFS §3.3) раньше байтового лимита.
- Текст и conclusion check-run не проверяются: тест только ждёт завершённого check-run, прежде чем читать результат.

## Этап 2 — envelope, инъекции и A/B

В работе, PR …

## Этап 3 — нагрузка

В работе, PR …

## Расхождения со спецификацией

- #247 ч.1 — усечённое дерево: `app/modules/reviews/infrastructure/github_policy_source.py:98-104` даёт терминальный `policy_revision_unavailable`, [RFS §9](RULES_FORMAT_SPEC.md#9-ошибки-и-наблюдаемость) (`docs/RULES_FORMAT_SPEC.md:942`) — повторяемый `policy_fetch_transient`.
- #247 ч.2 — диагностика §9; ч.3 — атрибуция §8.2.
- #168 ч.4 — `policy_path_invalid` против `unsupported_path`; ч.6 — размер blob, вторичный 403, дата в `Retry-After`.
- Остаток #130 — `policy_prompt_budget_exceeded` недостижим; строка RFS §11 про `unsupported_path` и `**/*` (на `main` модель вызывается и публикуется clean).
