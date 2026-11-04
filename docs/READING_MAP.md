# Карта чтения больших спек

Какой раздел открыть под задачу, чтобы не читать спеку целиком. Карта покрывает
[SYSTEM_DESIGN.md](SYSTEM_DESIGN.md) (SD), [PIPELINE_SPEC.md](PIPELINE_SPEC.md) (PIPELINE) и
[CONTEXT_AND_VERIFICATION_SPEC.md](CONTEXT_AND_VERIFICATION_SPEC.md) (CVS). Правило чтения —
блок Context discipline в [AGENTS.md](../AGENTS.md).

## Как читать

1. Найти задачу в таблице «Задача → разделы» или раздел в таблице файла.
2. Получить номера строк: `grep -n '^## ' docs/<файл>`, для подраздела — `grep -n '^### ' docs/<файл>`.
3. Прочитать только этот раздел по диапазону строк: `sed -n` или offset/limit своего инструмента.

В разделах с пометкой «ориентир» нет `###`. Строку находит `grep -nF '<ориентир>' docs/<файл>`,
читать от неё до следующего ориентира или до конца раздела.

Номеров строк и размеров в карте нет: они быстро устаревают. PR, который меняет заголовок `##`
или `###` из карты, добавляет новый `##` или убирает ориентир, обновляет карту в том же PR.

## Задача → разделы

| Задача | Что открыть |
| --- | --- |
| Добавить или изменить `error_code` | [PIPELINE §6](PIPELINE_SPEC.md#6-каталог-error_code), [PIPELINE §5](PIPELINE_SPEC.md#5-сбои-и-деградация) |
| Новая фаза или `tool` в `run_actions`, правка трейса | [PIPELINE §2](PIPELINE_SPEC.md#2-фазы-внутри-running-и-трейс-run_actions), [SD §6.2](SYSTEM_DESIGN.md#62-прогон-быстрый-путь) |
| Новое состояние или переход Run | [PIPELINE §1](PIPELINE_SPEC.md#1-состояния-и-переходы), [SD §6.4](SYSTEM_DESIGN.md#64-состояния-run) |
| Таймаут, retry, DLQ | [PIPELINE §3](PIPELINE_SPEC.md#3-таймауты), [PIPELINE §4](PIPELINE_SPEC.md#4-retry-и-backoff), [SD §6.8](SYSTEM_DESIGN.md#68-retry-и-dlq) |
| Лимит вызовов LLM и стоимости | [PIPELINE §4.5](PIPELINE_SPEC.md#45-лимиты-вызовов-и-стоимости), [CVS §7.4](CONTEXT_AND_VERIFICATION_SPEC.md#74-стоимость-дедлайн-и-checkpoint) |
| Запуск ревью: вебхук, лейбл, sweep | [PIPELINE §8](PIPELINE_SPEC.md#8-триггер-р-10-d2), [SD §6.1](SYSTEM_DESIGN.md#61-триггер-р-10-два-события-в-любом-порядке), [SD §8.2](SYSTEM_DESIGN.md#82-github-события-и-права) |
| Очередь: сообщение, consumer, топология | [SD §7](SYSTEM_DESIGN.md#7-очередь-rabbitmq), [PIPELINE §12](PIPELINE_SPEC.md#12-сообщения-очереди) |
| Вызов GitHub API: токен, rate limit, сбои | [SD §8.3](SYSTEM_DESIGN.md#83-правила-работы-с-api), [PIPELINE §5.2](PIPELINE_SPEC.md#52-сбои-github) |
| Новый метод порта или новый VCS-провайдер | [SD §8.1](SYSTEM_DESIGN.md#81-порт-vcsprovider-р-11), [CVS §9](CONTEXT_AND_VERIFICATION_SPEC.md#9-карта-diff-и-адаптеры-vcs) |
| GitLab или Bitbucket | [SD §8.4](SYSTEM_DESIGN.md#84-координаты-gitlab-и-надёжная-публикация-107), [SD §8.5](SYSTEM_DESIGN.md#85-bitbucket-cloud-333), [CVS §9.5](CONTEXT_AND_VERIFICATION_SPEC.md#95-gitlab-целевой-контракт-отдельная-реализация), [PIPELINE §8.4](PIPELINE_SPEC.md#84-gitlab-mr-intake-169), [Bitbucket Cloud path (#333)](PIPELINE_SPEC.md#bitbucket-cloud-path-333) |
| Публикация ревью: идемпотентность, гонки, восстановление | [CVS §10](CONTEXT_AND_VERIFICATION_SPEC.md#10-write-back-неизменяемая-публикация-и-восстановление), [CVS §9.4](CONTEXT_AND_VERIFICATION_SPEC.md#94-github), [SD §8.4](SYSTEM_DESIGN.md#84-координаты-gitlab-и-надёжная-публикация-107) |
| Якорь находки, suggestion, карта diff | [CVS §9.3](CONTEXT_AND_VERIFICATION_SPEC.md#93-якорь-finding-диапазоны-и-suggestion), [CVS §9.2](CONTEXT_AND_VERIFICATION_SPEC.md#92-hunks-и-проход-по-строкам), [PIPELINE §10](PIPELINE_SPEC.md#10-якорь-находки-d6) |
| Контекст ревью: уровни L0–L4, AST, бюджет | [CVS §3.2](CONTEXT_AND_VERIFICATION_SPEC.md#32-уровни-l0l4), [CVS §3.3](CONTEXT_AND_VERIFICATION_SPEC.md#33-ast-v1), [CVS §4](CONTEXT_AND_VERIFICATION_SPEC.md#4-бюджет-конвенции-и-кэш), [SD §9](SYSTEM_DESIGN.md#9-сборщик-контекста-4-уровня) |
| RAG: граф символов и индекс | [CVS §3.5](CONTEXT_AND_VERIFICATION_SPEC.md#35-кандидаты-графа-символов-rag-v1), [SD §7.4](SYSTEM_DESIGN.md#74-очередь-indexrepo-142), [PIPELINE §4.6](PIPELINE_SPEC.md#46-очередь-indexrepo-142), [SD §11](SYSTEM_DESIGN.md#11-данные-согласование-с-erd-роли-6) |
| Схема ответа LLM, `ReviewOutput` | [PIPELINE §9](PIPELINE_SPEC.md#9-выход-llm-reviewoutput), [CVS §6.1](CONTEXT_AND_VERIFICATION_SPEC.md#61-версии-ответа-и-evidence) |
| Verifier, детерминированные проходы, причины отказа | [CVS §7](CONTEXT_AND_VERIFICATION_SPEC.md#7-обязательный-llm-проверяющий), [CVS §6.2](CONTEXT_AND_VERIFICATION_SPEC.md#62-проходы-и-причины-отказа), [PIPELINE §2](PIPELINE_SPEC.md#2-фазы-внутри-running-и-трейс-run_actions) |
| Итоговый результат, вердикт, coverage, summary-only | [CVS §8](CONTEXT_AND_VERIFICATION_SPEC.md#8-итоговый-результат-coverage-и-хранение), [CVS §2.3](CONTEXT_AND_VERIFICATION_SPEC.md#23-summary-only-и-исходный-diff), [PIPELINE §11](PIPELINE_SPEC.md#11-вердикт-и-бейджи-d3) |
| Кэши | [SD §10](SYSTEM_DESIGN.md#10-стратегия-кэширования-контекстов), [CVS §4.3](CONTEXT_AND_VERIFICATION_SPEC.md#43-кэши-файлов-и-ast) |
| Endpoint, DTO, контракт с UI | [SD §12](SYSTEM_DESIGN.md#12-контракт-api--ui) |
| Таблица, поле, миграция | [SD §11](SYSTEM_DESIGN.md#11-данные-согласование-с-erd-роли-6), [CVS §8.3](CONTEXT_AND_VERIFICATION_SPEC.md#83-хранилище-и-миграция) |
| Вход и сессия | [SD §6.5](SYSTEM_DESIGN.md#65-вход-и-сессия-d4) |
| GitHub App: установка и переустановка | [SD §6.9](SYSTEM_DESIGN.md#69-подключение-репозиториев-установка-github-app-d10) |
| Выбор модели LLM, OQ-2, аудит #178 | [SYSTEM_DESIGN_LLM_LOG.md](SYSTEM_DESIGN_LLM_LOG.md) |

## SYSTEM_DESIGN.md

| Раздел | Когда открывать |
| --- | --- |
| [1. Решения](SYSTEM_DESIGN.md#1-решения) | Какие решения (Р-N) уже приняты и почему, прежде чем предлагать другое |
| [2. Границы ответственности](SYSTEM_DESIGN.md#2-границы-ответственности) | В каком слое (backend, frontend, LLM) место новой логике |
| [3. C4 — уровень 1: контекст](SYSTEM_DESIGN.md#3-c4--уровень-1-контекст) | Внешние системы и пользователи: общий обзор |
| [4. C4 — уровень 2: контейнеры](SYSTEM_DESIGN.md#4-c4--уровень-2-контейнеры) | Какие контейнеры есть и как они связаны |
| [5. C4 — уровень 3: компоненты AI Worker](SYSTEM_DESIGN.md#5-c4--уровень-3-компоненты-ai-worker) | Компоненты AI Worker перед правкой пайплайна |
| [6. Потоки данных](SYSTEM_DESIGN.md#6-потоки-данных) | Как сценарий идёт от события до результата; открывать подраздел нужного сценария |
| ↳ [6.1 Триггер (Р-10): два события в любом порядке](SYSTEM_DESIGN.md#61-триггер-р-10-два-события-в-любом-порядке) | Запуск ревью по двум событиям в любом порядке |
| ↳ [6.2 Прогон: быстрый путь](SYSTEM_DESIGN.md#62-прогон-быстрый-путь) | Обычный прогон целиком |
| ↳ [6.3 Схлопывание (Р-2): пуш во время прогона](SYSTEM_DESIGN.md#63-схлопывание-р-2-пуш-во-время-прогона) | Пуш во время активного прогона |
| ↳ [6.4 Состояния Run](SYSTEM_DESIGN.md#64-состояния-run) | Диаграмма состояний Run; переходы подробно — PIPELINE §1 |
| ↳ [6.5 Вход и сессия (D4)](SYSTEM_DESIGN.md#65-вход-и-сессия-d4) | Логин и сессия |
| ↳ [6.6 Чтение в UI и SSE](SYSTEM_DESIGN.md#66-чтение-в-ui-и-sse) | Как UI читает Run, SSE |
| ↳ [6.7 Перезапуск](SYSTEM_DESIGN.md#67-перезапуск) | Перезапуск прогона |
| ↳ [6.8 Retry и DLQ](SYSTEM_DESIGN.md#68-retry-и-dlq) | Retry и DLQ на уровне потока; правила — PIPELINE §4 |
| ↳ [6.9 Подключение репозиториев: установка GitHub App (D10)](SYSTEM_DESIGN.md#69-подключение-репозиториев-установка-github-app-d10) | Установка, переустановка и удаление GitHub App, подключение репозиториев |
| [7. Очередь: RabbitMQ](SYSTEM_DESIGN.md#7-очередь-rabbitmq) | Очередь RabbitMQ; открывать подраздел по типу правки |
| ↳ [7.1 Топология](SYSTEM_DESIGN.md#71-топология) | Exchange, очереди, routing key, потребители |
| ↳ [7.2 Форматы сообщений](SYSTEM_DESIGN.md#72-форматы-сообщений) | Что кладём в сообщение и что воркер читает сам |
| ↳ [7.3 Реализация (#34): отступления](SYSTEM_DESIGN.md#73-реализация-34-отступления) | Как worker устроен сейчас и чем отличается от дизайна |
| ↳ [7.4 Очередь index.repo (#142)](SYSTEM_DESIGN.md#74-очередь-indexrepo-142) | Индексатор графа RAG: очередь и контракт строк индекса |
| [8. Взаимодействие с VCS](SYSTEM_DESIGN.md#8-взаимодействие-с-vcs) | Работа с VCS; открывать подраздел по порту или провайдеру |
| ↳ [8.1 Порт VcsProvider (Р-11)](SYSTEM_DESIGN.md#81-порт-vcsprovider-р-11) | Метод порта `VcsProvider`, контракт адаптера |
| ↳ [8.2 GitHub: события и права](SYSTEM_DESIGN.md#82-github-события-и-права) | Новое событие вебхука, права GitHub App |
| ↳ [8.3 Правила работы с API](SYSTEM_DESIGN.md#83-правила-работы-с-api) | Ответ на вебхук, идемпотентность, токены, rate limit |
| ↳ [8.4 Координаты, GitLab и надёжная публикация (#107)](SYSTEM_DESIGN.md#84-координаты-gitlab-и-надёжная-публикация-107) | GitLab, координаты, надёжная публикация |
| ↳ [8.5 Bitbucket Cloud (#333)](SYSTEM_DESIGN.md#85-bitbucket-cloud-333) | Bitbucket Cloud |
| [9. Сборщик контекста: 4 уровня](SYSTEM_DESIGN.md#9-сборщик-контекста-4-уровня) | Обзор сборки контекста; контракт подробно — CVS §3 и §4 |
| ↳ [Граница MVP и будущего RAG](SYSTEM_DESIGN.md#граница-mvp-и-будущего-rag) | Что вошло в MVP и что отложено до RAG |
| ↳ [L0 — политика, метаданные и конвенции](SYSTEM_DESIGN.md#l0--политика-метаданные-и-конвенции) | `.review/rules.md`, `AGENTS.md`, метаданные |
| ↳ [L1 — Diff разрешённых изменений](SYSTEM_DESIGN.md#l1--diff-разрешённых-изменений) | Diff и порог summary-only |
| ↳ [L2 — Surrounding (по умолчанию для всех source-файлов)](SYSTEM_DESIGN.md#l2--surrounding-по-умолчанию-для-всех-source-файлов) | Окружение изменённых строк |
| ↳ [L3 — Whole File (по бюджету, топ-K файлов)](SYSTEM_DESIGN.md#l3--whole-file-по-бюджету-топ-k-файлов) | Файл целиком по бюджету |
| ↳ [L4 — AST / Imports (source-файлы языков с парсером)](SYSTEM_DESIGN.md#l4--ast--imports-source-файлы-языков-с-парсером) | AST и импорты |
| ↳ [Сборка и бюджет](SYSTEM_DESIGN.md#сборка-и-бюджет) | Сборка контекста и распределение бюджета |
| [10. Стратегия кэширования контекстов](SYSTEM_DESIGN.md#10-стратегия-кэширования-контекстов) | Ключи, TTL и инвалидация кэшей |
| [11. Данные (согласование с ERD роли 6)](SYSTEM_DESIGN.md#11-данные-согласование-с-erd-роли-6) | Поля сущностей, индекс графа; `###` нет — по ориентирам |
| ↳ ориентир `Текущий ограниченный контракт #213` | Внутренняя coverage в snapshots и JSON |
| ↳ ориентир `Сущность (роль 6)` | Требуемые поля сущностей и таблицы `code_index_*`: новое поле, миграция |
| ↳ ориентир `Индекс графа (#141` | Жизненный цикл индекса графа RAG: изоляция, видимость строк, очистка |
| ↳ ориентир `Статус индекса, который` | Статус индекса для API и ревью |
| [12. Контракт API ↔ UI](SYSTEM_DESIGN.md#12-контракт-api--ui) | Endpoint, DTO, контракт с UI |
| [13. Нефункциональные требования](SYSTEM_DESIGN.md#13-нефункциональные-требования) | Целевые p50 и p95, другие нефункциональные требования |
| [14. Развёртывание v1](SYSTEM_DESIGN.md#14-развёртывание-v1) | Что разворачивается; скрипты и стенды — `docs/INFRASTRUCTURE.md`, `docs/CICD.md` |
| [15. Открытые вопросы для команды](SYSTEM_DESIGN.md#15-открытые-вопросы-для-команды) | Статус открытого вопроса; `###` нет — по ориентирам. Журнал выбора LLM вынесен в [SYSTEM_DESIGN_LLM_LOG.md](SYSTEM_DESIGN_LLM_LOG.md) |
| ↳ ориентир `Кто решает` | Таблица вопросов OQ-N |
| ↳ ориентир `Граница исторических свидетельств` | Что считать историческим свидетельством: прогоны, цены, cache |
| ↳ ориентир `Строгий D7 и оговорка` | Строгий результат D7 и оговорка #46 |
| ↳ ориентир `**Production` | Источник `commit_messages` в production (#82) |
| [16. Решения по переходному коду и легаси-артефактам (#54)](SYSTEM_DESIGN.md#16-решения-по-переходному-коду-и-легаси-артефактам-54) | Можно ли убрать переходный код или легаси-артефакт |
| [17. Проверка DoD](SYSTEM_DESIGN.md#17-проверка-dod) | Чек-лист самого документа; для работы над задачей обычно не нужен |

## PIPELINE_SPEC.md

| Раздел | Когда открывать |
| --- | --- |
| [1. Состояния и переходы](PIPELINE_SPEC.md#1-состояния-и-переходы) | Новое состояние или переход, происхождение Run |
| [2. Фазы внутри running и трейс run_actions](PIPELINE_SPEC.md#2-фазы-внутри-running-и-трейс-run_actions) | Фазы и `tool` в `run_actions`, тексты причин; `###` нет — по ориентирам |
| ↳ ориентир `Что делает` | Таблица фаз: `tool`, состояние, исполнитель, checkpoint |
| ↳ ориентир `Порядок попытки на` | Порядок шагов попытки на `main` |
| ↳ ориентир `Канон после #168` | Канонический порядок фаз после #168 |
| ↳ ориентир `JSONB, только метаданные` | Содержимое `request` и `response` по каждому `tool` |
| ↳ ориентир `Канонические тексты причин` | Тексты причин #213 для summary, body и check-run |
| ↳ ориентир `Общие тексты причин` | Значения `REASON_TEXT` |
| ↳ ориентир `Метки оформления сводки` | Метки сводки и списка причин |
| ↳ ориентир `**Размер` | Предел размера `response` |
| [3. Таймауты](PIPELINE_SPEC.md#3-таймауты) | Значения таймаутов и исход при превышении |
| [4. Retry и backoff](PIPELINE_SPEC.md#4-retry-и-backoff) | Повторы и лимиты; открывать подраздел по теме |
| ↳ [4.1 Доменный счётчик и AMQP x-attempt (D13 [дефолт])](PIPELINE_SPEC.md#41-доменный-счётчик-и-amqp-x-attempt-d13-дефолт) | `runs.attempt` и `x-attempt`: кто считает попытки |
| ↳ [4.2 Задержки](PIPELINE_SPEC.md#42-задержки) | Задержки и очереди retry по ситуациям |
| ↳ [4.3 Возврат из retry-очереди — требование к топологии SD §7.1 (реализует #34)](PIPELINE_SPEC.md#43-возврат-из-retry-очереди--требование-к-топологии-sd-71-реализует-34) | Возврат сообщения из retry-очереди: настройки очередей |
| ↳ [4.4 DLQ](PIPELINE_SPEC.md#44-dlq) | Что попадает в DLQ |
| ↳ [4.5 Лимиты вызовов и стоимости](PIPELINE_SPEC.md#45-лимиты-вызовов-и-стоимости) | Лимиты generation-вызовов и стоимости Run |
| ↳ [4.6 Очередь index.repo (#142)](PIPELINE_SPEC.md#46-очередь-indexrepo-142) | Retry и исходы заданий индекса `index.repo` |
| [5. Сбои и деградация](PIPELINE_SPEC.md#5-сбои-и-деградация) | Поведение при сбое; открывать подраздел по источнику |
| ↳ [5.1 Классы сбоев LLM](PIPELINE_SPEC.md#51-классы-сбоев-llm) | Сбои LLM и fallback-модель |
| ↳ [5.2 Сбои GitHub](PIPELINE_SPEC.md#52-сбои-github) | Сбои GitHub: итог Run, `error_code`, что видят автор PR и UI |
| ↳ [5.3 Деградация deep → fast (SD §13)](PIPELINE_SPEC.md#53-деградация-deep--fast-sd-13) | Деградация deep → fast |
| [6. Каталог error_code](PIPELINE_SPEC.md#6-каталог-error_code) | Каталог `error_code` |
| ↳ [6.1 HTTP-отказы rerun](PIPELINE_SPEC.md#61-http-отказы-rerun) | Почему `POST /api/runs/{id}/rerun` отвечает 409 |
| [7. Check-run бота (SD §8.3)](PIPELINE_SPEC.md#7-check-run-бота-sd-83) | Check-run бота, статус Bitbucket |
| [8. Триггер (Р-10, D2)](PIPELINE_SPEC.md#8-триггер-р-10-d2) | Условия запуска ревью, повторное ревью после пуша, sweep, GitLab intake |
| [9. Выход LLM: ReviewOutput](PIPELINE_SPEC.md#9-выход-llm-reviewoutput) | Схема ответа LLM `ReviewOutput` |
| [10. Якорь находки (D6)](PIPELINE_SPEC.md#10-якорь-находки-d6) | Координаты якоря находки |
| [11. Вердикт и бейджи (D3)](PIPELINE_SPEC.md#11-вердикт-и-бейджи-d3) | Вердикт и бейджи |
| [12. Сообщения очереди](PIPELINE_SPEC.md#12-сообщения-очереди) | Формат сообщений очереди |
| [13. Термин куратора → канон](PIPELINE_SPEC.md#13-термин-куратора--канон) | Термин куратора и его каноническое имя |
| [14. Решения](PIPELINE_SPEC.md#14-решения) | Принятые решения по пайплайну и их источники |
| [15. Открытые вопросы](PIPELINE_SPEC.md#15-открытые-вопросы) | Открытые вопросы пайплайна |
| [16. Проверки](PIPELINE_SPEC.md#16-проверки) | Команды и ожидаемые результаты проверок контрактов |
| [Bitbucket Cloud path (#333)](PIPELINE_SPEC.md#bitbucket-cloud-path-333) | Вебхук и привязка репозитория в Bitbucket Cloud |

## CONTEXT_AND_VERIFICATION_SPEC.md

| Раздел | Когда открывать |
| --- | --- |
| [1. Область и инварианты](CONTEXT_AND_VERIFICATION_SPEC.md#1-область-и-инварианты) | Область спецификации и инварианты; читать первым при работе с этой спекой |
| [2. Снимок и последовательность run](CONTEXT_AND_VERIFICATION_SPEC.md#2-снимок-и-последовательность-run) | Снимок и порядок run; открывать подраздел по теме |
| ↳ [2.1. Раздельные ревизии](CONTEXT_AND_VERIFICATION_SPEC.md#21-раздельные-ревизии) | Раздельные ревизии снимка: какое поле что значит |
| ↳ [2.2. Порядок операций](CONTEXT_AND_VERIFICATION_SPEC.md#22-порядок-операций) | Порядок операций от claim run до конца |
| ↳ [2.3. Summary-only и исходный diff](CONTEXT_AND_VERIFICATION_SPEC.md#23-summary-only-и-исходный-diff) | Порог summary-only и исходный diff |
| [3. Архитектура расширенного контекста](CONTEXT_AND_VERIFICATION_SPEC.md#3-архитектура-расширенного-контекста) | Расширенный контекст; открывать подраздел по теме |
| ↳ [3.1. Порты и происхождение](CONTEXT_AND_VERIFICATION_SPEC.md#31-порты-и-происхождение) | Порты `ContextProvider` и `DiffMap`, происхождение данных |
| ↳ [3.2. Уровни L0–L4](CONTEXT_AND_VERIFICATION_SPEC.md#32-уровни-l0l4) | Что входит в уровни L0–L4 |
| ↳ [3.3. AST v1](CONTEXT_AND_VERIFICATION_SPEC.md#33-ast-v1) | AST v1: языки, grammar, извлекаемые объявления |
| ↳ [3.4. Лимиты и fallback](CONTEXT_AND_VERIFICATION_SPEC.md#34-лимиты-и-fallback) | Лимиты AST и контекста, исход превышения |
| ↳ [3.5. Кандидаты графа символов (RAG v1)](CONTEXT_AND_VERIFICATION_SPEC.md#35-кандидаты-графа-символов-rag-v1) | Кандидаты графа символов (RAG v1) |
| [4. Бюджет, конвенции и кэш](CONTEXT_AND_VERIFICATION_SPEC.md#4-бюджет-конвенции-и-кэш) | Бюджет контекста, конвенции и кэши; открывать подраздел по теме |
| ↳ [4.1. Распределение контекста](CONTEXT_AND_VERIFICATION_SPEC.md#41-распределение-контекста) | Лимит токенов на вызов и распределение контекста |
| ↳ [4.2. Общие конвенции](CONTEXT_AND_VERIFICATION_SPEC.md#42-общие-конвенции) | Как отбираются файлы для общих конвенций |
| ↳ [4.3. Кэши файлов и AST](CONTEXT_AND_VERIFICATION_SPEC.md#43-кэши-файлов-и-ast) | Ключи и хранение кэшей файлов и AST |
| [5. Manifest фактически отправленного контекста](CONTEXT_AND_VERIFICATION_SPEC.md#5-manifest-фактически-отправленного-контекста) | Manifest отправленного контекста и guard перед вызовом |
| [6. Кандидаты и детерминированные проходы](CONTEXT_AND_VERIFICATION_SPEC.md#6-кандидаты-и-детерминированные-проходы) | Кандидаты и детерминированные проходы; открывать подраздел по теме |
| ↳ [6.1. Версии ответа и evidence](CONTEXT_AND_VERIFICATION_SPEC.md#61-версии-ответа-и-evidence) | `ReviewOutput` v2, версии ответа, evidence |
| ↳ [6.2. Проходы и причины отказа](CONTEXT_AND_VERIFICATION_SPEC.md#62-проходы-и-причины-отказа) | Проходы, их проверки и коды отказа |
| [7. Обязательный LLM-проверяющий](CONTEXT_AND_VERIFICATION_SPEC.md#7-обязательный-llm-проверяющий) | LLM-проверяющий; открывать подраздел по теме |
| ↳ [7.1. Профиль и вход](CONTEXT_AND_VERIFICATION_SPEC.md#71-профиль-и-вход) | Профиль `verification-v1` и вход проверки |
| ↳ [7.2. Ответ](CONTEXT_AND_VERIFICATION_SPEC.md#72-ответ) | Формат ответа проверяющего |
| ↳ [7.3. Четыре вызова и резерв](CONTEXT_AND_VERIFICATION_SPEC.md#73-четыре-вызова-и-резерв) | Лимит generation-вызовов и резерв |
| ↳ [7.4. Стоимость, дедлайн и checkpoint](CONTEXT_AND_VERIFICATION_SPEC.md#74-стоимость-дедлайн-и-checkpoint) | Лимит стоимости run, дедлайн, checkpoint |
| [8. Итоговый результат, coverage и хранение](CONTEXT_AND_VERIFICATION_SPEC.md#8-итоговый-результат-coverage-и-хранение) | Итоговый результат; открывать подраздел по теме |
| ↳ [8.1. Единый результат](CONTEXT_AND_VERIFICATION_SPEC.md#81-единый-результат) | Структура `FinalReviewResult` |
| ↳ [8.2. Вердикт и публичный текст](CONTEXT_AND_VERIFICATION_SPEC.md#82-вердикт-и-публичный-текст) | Условия вердикта и публичный текст |
| ↳ [8.3. Хранилище и миграция](CONTEXT_AND_VERIFICATION_SPEC.md#83-хранилище-и-миграция) | Хранение результата и миграция |
| ↳ [8.4. Ошибки](CONTEXT_AND_VERIFICATION_SPEC.md#84-ошибки) | Коды ошибок, исход и retry |
| [9. Карта diff и адаптеры VCS](CONTEXT_AND_VERIFICATION_SPEC.md#9-карта-diff-и-адаптеры-vcs) | Карта diff и адаптеры VCS; открывать подраздел по теме |
| ↳ [9.1. Идентичность снимка и файлов](CONTEXT_AND_VERIFICATION_SPEC.md#91-идентичность-снимка-и-файлов) | Состав `DiffMap/v1`, идентичность снимка и файлов |
| ↳ [9.2. Hunks и проход по строкам](CONTEXT_AND_VERIFICATION_SPEC.md#92-hunks-и-проход-по-строкам) | Hunks и проход по строкам |
| ↳ [9.3. Якорь finding, диапазоны и suggestion](CONTEXT_AND_VERIFICATION_SPEC.md#93-якорь-finding-диапазоны-и-suggestion) | Якорь finding, диапазоны, suggestion |
| ↳ [9.4. GitHub](CONTEXT_AND_VERIFICATION_SPEC.md#94-github) | Публикация на GitHub |
| ↳ [9.5. GitLab: целевой контракт, отдельная реализация](CONTEXT_AND_VERIFICATION_SPEC.md#95-gitlab-целевой-контракт-отдельная-реализация) | GitLab: целевой контракт |
| [10. Write-back: неизменяемая публикация и восстановление](CONTEXT_AND_VERIFICATION_SPEC.md#10-write-back-неизменяемая-публикация-и-восстановление) | Публикация; открывать подраздел по теме |
| ↳ [10.1. Payload и идентичность](CONTEXT_AND_VERIFICATION_SPEC.md#101-payload-и-идентичность) | Canonical JSON и идентичность публикации |
| ↳ [10.2. Выполнение и гонки](CONTEXT_AND_VERIFICATION_SPEC.md#102-выполнение-и-гонки) | Lease публикации, проверки перед POST, гонки |
| ↳ [10.3. Ошибки, timeout и fallback](CONTEXT_AND_VERIFICATION_SPEC.md#103-ошибки-timeout-и-fallback) | Исход ошибки или timeout, действие, fallback |
| ↳ [10.4. Совместимость с существующими публикациями](CONTEXT_AND_VERIFICATION_SPEC.md#104-совместимость-с-существующими-публикациями) | Старый marker публикаций и совместимость с ним |
| ↳ [10.5. Изменения текущей реализации](CONTEXT_AND_VERIFICATION_SPEC.md#105-изменения-текущей-реализации) | Какой текущий код меняется ради этой спеки |
| [11. Приёмочные сценарии и оценка качества](CONTEXT_AND_VERIFICATION_SPEC.md#11-приёмочные-сценарии-и-оценка-качества) | Приёмочные сценарии и оценка качества |
| [12. Согласование контрактов и порядок внедрения](CONTEXT_AND_VERIFICATION_SPEC.md#12-согласование-контрактов-и-порядок-внедрения) | Что спека меняет в существующих контрактах, порядок внедрения |

## Остальные файлы

- Больше 80 КБ на 04.11.2026, карты нет, сразу `grep -n '^## '`: [CICD.md](CICD.md),
  [SECRETS.md](SECRETS.md), [RULES_FORMAT_SPEC.md](RULES_FORMAT_SPEC.md),
  [INCREMENTAL_REVIEW_AND_LIMITS_SPEC.md](INCREMENTAL_REVIEW_AND_LIMITS_SPEC.md),
  [TEST_PLAN.md](TEST_PLAN.md), [RULES_MD_TEST_REPORT.md](test-plan/RULES_MD_TEST_REPORT.md).
- Журнал выбора LLM и аудит #178, вынесенные из SD §15:
  [SYSTEM_DESIGN_LLM_LOG.md](SYSTEM_DESIGN_LLM_LOG.md).
- Каталоги и baselines, вынесенные из TEST_PLAN: [acceptance-107.md](test-plan/acceptance-107.md),
  [rules-md-scenarios-163.md](test-plan/rules-md-scenarios-163.md),
  [bitbucket-acceptance-333.md](test-plan/bitbucket-acceptance-333.md),
  [eval-baselines.md](test-plan/eval-baselines.md), [rag-scenarios-146.md](test-plan/rag-scenarios-146.md).
