# Backend architecture

## Назначение и границы

Этот документ фиксирует границы Clean Architecture, организацию процессов,
правила зависимостей и работу с PostgreSQL. Поля, типы, FK, ограничения и индексы
определены в SQLAlchemy-моделях; дублировать полную схему таблиц в Markdown не нужно.

Артефакты задачи:

- ORM: **SQLAlchemy 2**, типизированный Declarative (`Mapped`, `mapped_column`).
- Миграции: **Alembic**, [первичная ревизия](../alembic/versions/20260913_0001_initial_schema.py).
- Схема: [реестр моделей](../app/bootstrap/db_metadata.py) и ссылки на модули ниже.
- ERD: [Mermaid-исходник](erd/erd.mmd) и [SVG](erd/erd.svg).

[SYSTEM_DESIGN.md](SYSTEM_DESIGN.md) — источник правды о процессах, входных точках,
очередях, потоках данных, retry/lease, безопасности и развёртывании. Здесь описано,
как эти решения отражаются в исходном коде.

### Что реализовано, а что является планом

В текущем каркасе реализованы healthcheck FastAPI, ORM-модели, общая DB-инфраструктура,
Unit of Work, конфигурация Alembic, миграции схемы и тесты. Дерево слоёв и пять
entrypoints ниже — целевая организация приложения: use cases, конкретные repositories,
LLM/VCS/payment gateways и consumers ещё предстоит реализовать. Весь код пока живёт в
одном пакете `app/`; разнесение по сервисам `services/<name>/` (Р-12) реализуется в
PR #10. Текущий Compose поднимает backend, worker, PostgreSQL, RabbitMQ и Redis, а не весь
целевой runtime; worker и очередь описаны в разделе «Worker и очередь (#34)».

## Принципы

- Зависимости направлены внутрь: transport и infrastructure зависят от
  application/domain, но не наоборот.
- Use case не знает о FastAPI, AMQP, SQLAlchemy или SDK провайдера.
- Каждый процесс владеет своей точкой входа, а бизнес-логика сгруппирована по
  модулям, а не по общему техническому слою.
- Внешние зависимости представлены интерфейсами application/domain. Реализации для GitHub, LLM, Stripe, RabbitMQ, Redis и S3
  можно заменить без переписывания use case.

## Слои Clean Architecture

Выбранный стиль — **Clean Architecture**. Бизнес-правила и use cases не зависят от
FastAPI, SQLAlchemy, RabbitMQ или SDK внешних сервисов. Каждый бизнес-модуль делится
на `domain`, `application` и `infrastructure`; entrypoint остаётся отдельным
транспортным слоем.

Зависимости направлены только внутрь: `entrypoints → application → domain` и
`infrastructure → application/domain`. Поэтому use case получает нужную зависимость
как интерфейс, а конкретная реализация выбирается при запуске приложения.

```text
entrypoint / delivery mechanism
  FastAPI route | GitHub webhook | RabbitMQ consumer | scheduled reconciler
                         │
                         ▼
                   application use case
                         │
                         ▼
                 domain interfaces
                         │
                         ▼
infrastructure implementations
  SQLAlchemy | GitHub client | LLM Gateway | RabbitMQ | Redis | S3 | Stripe
```

| Слой | Содержимое | Зависимости |
| --- | --- | --- |
| `<module>/domain` | value objects, правила, доменные ошибки, интерфейсы модуля | только stdlib / небольшие абстракции |
| `<module>/application` | use cases, DTO команд и результатов | domain своего модуля и явно нужные публичные контракты других модулей |
| `<module>/infrastructure` | repositories и реализации внешних зависимостей конкретного модуля | application/domain модуля, внешние библиотеки |
| `entrypoints` | FastAPI routers, webhook endpoint, consumers, scheduler, dependency wiring | application-модули и bootstrap |

HTTP DTO и сообщения RabbitMQ — транспортные модели Pydantic. ORM-модели — детали
persistence-слоя. Ни те, ни другие не пересекают границу application.

Формулировка задания `Router → Service → Repository → LLM Gateway` отражает набор
компонентов, но не обязательную линейную цепочку. В выбранной архитектуре Service
(use case) отдельно обращается к Repository и к LLM Gateway. Репозиторий отвечает
за БД и не вызывает LLM; orchestration остаётся в application.

## Entrypoints

Согласно Р-12 из [SYSTEM_DESIGN.md](SYSTEM_DESIGN.md) (утверждён в редакции PR #10),
backend — один monorepo и пять независимо собираемых сервисов: `services/portal-api`,
`services/auth-api`, `services/webhook-api`, `services/worker`, `services/publisher`.
Каждый собирается своим Dockerfile и запускается отдельным контейнером; PostgreSQL,
RabbitMQ и Redis — общая инфраструктура на переходном этапе. Отдельного Event Collector
нет: `usage_events` пишет worker (LLM Gateway), метрики строятся по ним. Схему применяет
одноразовый job `migrator`, а не шестой сервис.

Import path един для всех сервисов: процесс стартует из `services/<name>/app/main.py` —
HTTP-сервисы командой `uvicorn app.main:app`, consumers `worker` и `publisher` командой
`python -m app.main`; `app.main` только импортирует тонкий entrypoint
`app.entrypoints.<entry>` (`portal`, `auth`, `webhook`, `worker`, `publisher`). Entrypoint
декодирует транспортный контракт, получает use case из container и преобразует результат
обратно в HTTP/AMQP. Бизнес-решения в entrypoint не допускаются.

Эта раскладка реализуется в PR #10. До его слияния код — один пакет `app/`
(структура ниже), а `docker-compose.yml` поднимает только `backend` и PostgreSQL.

Сервисы собираются и выпускаются независимо, но это не полностью автономные
микросервисы: на переходном этапе они разделяют схему БД и миграции. Бизнес-модуль не
равен контейнеру. Модели принадлежат модулям; чужие таблицы не изменяются напрямую из
use cases — доступ идёт через публичные интерфейсы. Между сервисами передаются
версионированные сообщения с идентификаторами, не ORM-объекты и не Python-вызовы.

## Интерфейсы и инфраструктурные реализации

Application зависит от узких интерфейсов, сгруппированных по задаче, а не от одного
«god repository».

| Интерфейс | Владелец | Назначение |
| --- | --- | --- |
| `RunRepository`, `ReviewRepository` | `reviews` | состояние прогона и результаты review |
| `RepositoryReader` | `repositories` | настройки репозитория для use cases |
| `VcsProvider` | использующий модуль | минимальные операции конкретного VCS-сценария |
| `ContextProvider` | `reviews` | собирает ограниченный и трассируемый `ContextPayload` для конкретного `head_sha` |
| `LlmGateway` | `reviews` | structured результат анализа неизменяемого контекста |
| `PaymentGateway` | `billing` | создание и подтверждение оплаты |
| `UnitOfWork` | общий контракт; расширение в использующем модуле | граница транзакции и доступ к интерфейсам repositories |
| `Clock`, `IdGenerator` | `common` | детерминированные тесты и технические значения |

`LlmGateway` принимает неизменяемый контекст, prompt/rule snapshots и выбранный engine;
возвращает typed findings, usage и нормализованную ошибку. Он не публикует комментарии,
не меняет состояние run и не знает о GitHub.

`ContextProvider` также не знает о публикации или состоянии Run. В MVP container
подставляет `DeterministicContextProvider`: diff, ограниченное окружение, целые файлы
по бюджету и AST/imports (L0–L4 из SYSTEM_DESIGN). `RagContextProvider` — будущая
реализация того же порта; он добавит retrieval-кандидаты перед общим
`BudgetAllocator`, но вернёт тот же `ContextPayload`. Поэтому RAG не требует новой
ORM-модели, миграции или изменения use case в рамках MVP.

## LLM Gateway (#33)

Порт — `ReviewModel.draft_review(*, context: ReviewContext)` в
`modules/reviews/application/execute_review.py` и `ConventionsModel.draft_conventions`
в `conventions.py`: use case передаёт неизменяемый контекст, а не отрендеренную строку.
Контракты вызова, общие для application и шлюза, — `modules/reviews/application/llm.py`:
`RunCallContext` (run, attempt, engine, дедлайн попытки, версии промпта и правил —
их передаёт воркер при claim, #34), `LlmCallFailed` с `error_code` из PIPELINE_SPEC §6,
порты `UsageLedger` и `LlmCallTrace`. Подгонка L1-контекста под токен-бюджет —
чистая функция `application/prompt_budget.py`.

| Файл | Что делает |
| --- | --- |
| `infrastructure/llm/settings.py` | профили моделей из env (`LLM_*`), политика PIPELINE_SPEC §3, §4.5, §5.1 |
| `infrastructure/llm/transport.py` | один адаптер OpenAI-совместимого `/chat/completions`, ротация ключей, классы HTTP-сбоев |
| `infrastructure/llm/gateway.py` | `LlmGateway`: дедлайн, переполнение, бюджет до каждого вызова; retry, repair, fallback; ≤ 4 вызовов на попытку |
| `infrastructure/llm/answers.py` | путь разбора ответа: `review-output.schema.json`, затем `parse_review_output`; тексты ошибок для repair |
| `infrastructure/llm/models.py` | `GatewayReviewModel`, `GatewayConventionsModel` — реализации портов |
| `infrastructure/llm_call_trace.py`, `analytics/infrastructure/usage_ledger.py` | запись `llm.call` в `run_actions` и `usage_events`, каждая — своей короткой транзакцией после вызова |
| `bootstrap/llm_gateway.py` | сборка шлюза для worker и точка входа `review_case` без БД (eval #30, живой прогон) |

**Структурный вывод: `response_format` с JSON Schema (`strict: true`).** Выбран потому,
что форма ответа уже задана одним файлом `review/schemas/review-output.schema.json`, и
он соответствует требованиям строгого режима (все ключи обязательны, `additionalProperties:
false`, nullable — `[..., "null"]`, PIPELINE_SPEC §9): шлюз отправляет его как есть, без
корневых `$`-аннотаций, и второй копии схемы не появляется. Tool calling дал бы ту же
схему, но с обёрткой `tool_calls` и лишним рендером аргументов; Instructor — новая
зависимость, которая дублирует наш repair-цикл (PIPELINE_SPEC §5.1 требует ровно один
repair и затем fallback, а не собственные ретраи библиотеки). Путь «JSON по промпту»
(`LLM_STRUCTURED_OUTPUT=prompt_json`, только с `LLM_ALLOW_PROMPT_JSON=1`) — для локальных
и self-hosted моделей в dev и eval: `response_format` не отправляется, ответ проходит тот
же путь разбора. Любой путь заканчивается JSON Schema и Pydantic: семантику, которую
схема не выражает, провайдер не проверяет.

**Подсчёт токенов.** Точные токенизаторы моделей за роутером офлайн недоступны, поэтому
до отправки шлюз оценивает промпт консервативно (`символы / 3`); фактические
`prompt_tokens` провайдера пишутся в `usage_events`, а HTTP 400 о длине контекста
относится к тому же классу `llm_context_overflow`.

## Целевая модульная структура кода

Код организован **сначала по бизнес-модулю**, а уже внутри модуля — по слоям Clean
Architecture. Это не позволяет превратить `common` в свалку и сохраняет use case,
его интерфейс и реализацию рядом друг с другом.

Дерево описывает пакет `app/` до PR #10. После разнесения по Р-12 у каждого сервиса свой
`services/<name>/app/`, и в его `entrypoints/` остаётся только entrypoint этого сервиса.

```text
app/
  bootstrap/
    config.py                  # env / settings
    container.py               # composition root и DI wiring
    db_metadata.py             # реализован: сбор ORM metadata всех модулей
    logging.py
  common/
    application/
      unit_of_work.py           # реализован: интерфейс управления транзакцией
    domain/
      errors.py                # только базовые технические ошибки
      ids.py                   # UUID/value types, не доменные сущности
      events.py                # межмодульные event contracts
    infrastructure/
      db/                      # Base, session factory и DB helpers; не модели модулей
      messaging/               # RabbitMQ connection и consumer/publisher helpers
      storage/                 # Redis и S3 infrastructure implementations
  modules/
    workspaces/
      domain/
      application/
      infrastructure/          # Workspace model и repository
    reviews/
      domain/                  # Run, Finding, Comment, Review interfaces, ContextProvider
      application/             # create/execute/publish/cancel/rerun, context orchestration
      infrastructure/          # review models, repos, LLM gateway, deterministic context provider
    repositories/
      domain/
      application/
      infrastructure/
    integrations/
      github/                  # общая provider infrastructure: gateway, parser, tokens
      webhooks/
        domain/
        application/
        infrastructure/
    billing/
      domain/
      application/
      infrastructure/          # Payment/CreditLedger models, repos, Stripe implementation
    analytics/
      domain/
      application/
      infrastructure/
  entrypoints/
    portal/                    # portal-api: FastAPI routes и response schemas
    auth/                      # auth-api: GitHub OAuth, JWT и сессии
    webhook/                   # webhook-api: GitHub webhook FastAPI app
    worker/                    # worker: review.run AMQP consumer
    publisher/                 # publisher: review.publish AMQP consumer
```

`bootstrap/container.py` — единственное место, где создаются engine, session factory,
GitHub client, LLM gateway и RabbitMQ publisher. Entrypoint запрашивает у container
готовый use case и не собирает зависимости сам. Тесты application-слоя подменяют интерфейсы
fakes; интеграционные тесты проверяют реальные инфраструктурные реализации отдельно.

Engine и сетевые клиенты живут в течение lifespan процесса и закрываются при shutdown
(`await engine.dispose()` и закрытие клиентов). Session и Unit of Work создаются
заново для каждой короткой транзакции, не переиспользуются между запросами, сообщениями
или параллельными asyncio tasks. `bootstrap/db_metadata.py` собирает только описания
таблиц для Alembic и не открывает соединение с БД.

### Что допустимо выносить в `common`

В `common` разрешён только код, который одновременно не содержит бизнес-правил и нужен
минимум двум модулям: настройки, логирование, соединения с инфраструктурой, базовые
идентификаторы, общие ошибки и стабильные event contracts. Нельзя выносить туда
`Run`, `Repository`, `Finding`, review-правила, SQLAlchemy repositories или use cases
«на будущее». Если код используется только одним модулем, он остаётся в нём.

Интерфейс принадлежит модулю, который его **использует**: например `LlmGateway` определён в
`modules/reviews/domain/ports.py`, а реализация лежит в
`modules/reviews/infrastructure/llm_gateway.py`. GitHub implementation живёт отдельно, потому
что его используют reviews, repositories и webhooks, но каждый модуль определяет свой
узкий интерфейс, а не зависит от полного GitHub SDK.

## Транзакции и выполнение review

Application управляет транзакцией через [UnitOfWork](../app/common/application/unit_of_work.py).
[SqlAlchemyUnitOfWork](../app/common/infrastructure/db/unit_of_work.py) создаёт одну
`AsyncSession`, требует явного `commit()` и при выходе откатывает незавершённую
транзакцию и закрывает session, в том числе при исключении.

Конкретный UoW модуля предоставляет application интерфейсы repositories; инфраструктурная
сборка передаёт всем этим repositories одну session. Репозитории могут делать `flush`,
но не `commit`. Application не обращается к `uow.session` — это свойство предназначено
только для инфраструктурной сборки и её тестов. Пока конкретных repositories нет,
реализована общая транзакционная основа, а не полный UoW каждого use case.

План выполнения worker:

1. Короткая транзакция: атомарно захватить доступный Run, проверить state/lease,
   записать владельца и зафиксировать переход. Проверка и изменение не разделяются
   на независимые операции; используются блокировка или условный UPDATE.
2. Вне DB-транзакции: получить контекст и вызвать LLM через gateway. Не удерживать
   транзакцию и блокировки на время сетевых вызовов.
3. Новая короткая транзакция: проверить актуальность lease/state/cancellation,
   сохранить результаты и usage, изменить состояние Run, сделать commit.
4. Только после commit отправить следующее сообщение и подтвердить обработанное.
   Повторная доставка должна быть безопасной. Разрыв между commit и публикацией
   восстанавливается reconciler по состоянию БД согласно SYSTEM_DESIGN; одна
   PostgreSQL-транзакция сама по себе не гарантирует доставку в RabbitMQ.

Вызовы GitHub и Stripe также не включаются в длительную DB-транзакцию. Их retries,
idempotency и reconciliation — часть соответствующего use case, не repository.

### Worker и очередь (#34)

До разнесения по сервисам (#16) worker является отдельным процессом из того же образа,
что `backend`. Запуск одной командой:

```bash
uv run python -m app.worker        # локально
docker compose up -d worker        # сервис worker в docker-compose.yml
```

Переменные: `DATABASE_URL` и `RABBITMQ_URL` обязательны; `GITHUB_APP_ID` и
`GITHUB_APP_PRIVATE_KEY` необязательны: без них процесс стартует, пишет предупреждение,
а sweep «2 мин без CI» и публикация в GitHub (ревью и check-run) выключены. `WORKER_ID`
(по умолчанию `hostname:pid`) пишется в `runs.worker_id`, `PORTAL_URL` даёт ссылку на
прогон в check-run.

При старте worker объявляет топологию SD §7.1 целиком
([amqp.py](../app/modules/reviews/infrastructure/amqp.py)) и запускает три задачи:

| Задача | Что делает | Use case |
|---|---|---|
| consumer `review.run.fast` (prefetch 1) | RunGuard, claim по lease, попытка под watchdog, retry и DLQ | [HandleReviewRun](../app/modules/reviews/application/handle_review_run.py) |
| consumer `review.publish` (prefetch 1) | T14-T16: ревью в GitHub, check-run | [PublishRunReview](../app/modules/reviews/application/publish_run_review.py) |
| лидер-цикл (`pg_advisory_lock`, 30 с) | sweep «2 мин без CI» и повторная отправка outbox (`message_published_at IS NULL`, сигналы отмены T6) | `SweepNoCi`, `TryEnqueueWebhookRun.replay_pending_publications` |

Consumer `review.run.deep` не запускается, очередь только объявляется. Реконсилер
(T12, T13, T17, T18) работает лидер-циклом процесса API (`app/main.py`, раз в 5 минут,
[reconciler.py](../app/bootstrap/reconciler.py)); без `RABBITMQ_URL` он выключен.

Транзакции попытки: claim (`queued` → `running`, `attempt + 1`, lease 5 мин,
`NOTIFY run_updated`) идёт одной короткой транзакцией; diff, конвенции и вызов модели идут
вне транзакций; heartbeat раз в 60 с продлевает lease только до дедлайна попытки
(8 мин для fast); T8 (`review.postprocess`, находки, `publishing`) идёт одной транзакцией,
после commit публикуется `review.publish/v1` с publisher confirms; ack входящего сообщения
отправляется после commit. Каждая смена `state` отправляет `NOTIFY run_updated` в той же транзакции.

Процесс API (`app/main.py`) тоже публикует в RabbitMQ: `POST /api/runs/{id}/rerun`
(новый Run с `trigger = rerun`, AMQP priority 9) и сигнал закрытия check-run T6 при
`POST /api/runs/{id}/cancel` для Run с `attempt ≥ 1`. Подключение ленивое
(`LazyAmqpPublisher`): API стартует и без доступного брокера, а неотправленное
сообщение подбирают replay в лидер-цикле worker (сигналы T6) или реконсилер (T18).
Статусы из worker доходят до `/api/stream` через PostgreSQL: API слушает канал
`run_updated` (`LISTEN` на отдельном autocommit-соединении,
[run_update_listener.py](../app/bootstrap/run_update_listener.py)) и передаёт события в
хаб, который хранит для медленного подписчика последний статус каждого Run.

## PostgreSQL и владение моделями

PostgreSQL 17, драйвер `psycopg` v3. Runtime использует `AsyncSession`, Alembic —
синхронное соединение того же драйвера. URL: `postgresql+psycopg://...`.
SQLModel не используется: ORM отделена от transport DTO и доменных контрактов.

| Модуль / код схемы | Модели |
| --- | --- |
| [workspaces](../app/modules/workspaces/infrastructure/models.py) | Workspace |
| [repositories](../app/modules/repositories/infrastructure/models.py) | ProviderInstallation, Repository, RuleVersion, RepoConventions |
| [reviews](../app/modules/reviews/infrastructure/models.py) | PromptVersion, CodeChange, Run, ContextPayload, Finding, Comment, RunAction |
| [webhooks](../app/modules/integrations/webhooks/infrastructure/models.py) | WebhookEvent |
| [billing](../app/modules/billing/infrastructure/models.py) | Payment, CreditLedger |
| [analytics](../app/modules/analytics/infrastructure/models.py) | UsageEvent |

Названия из задания сопоставляются так: `MergeRequest → CodeChange`,
`ReviewJob → Run`. Это согласованные переименования, а не отсутствующие сущности.
`CodeChange` хранит обновляемое состояние PR/MR; `Run` фиксирует SHA, engine и ссылки
на версии prompt/rules конкретного запуска. `ContextPayload` появляется при сборке
контекста: связь с Run — `0..1`, а не обязательная строка сразу после webhook.
Finding содержит результат анализа, включая `side` (`LEFT`/`RIGHT`), `severity`,
`category` и необязательный `suggestion`; `side`, `severity` и `category` хранятся
как PostgreSQL enum. Comment — сведения о публикации Finding. `RunAction` хранит
запрос инструмента и либо малый JSON-ответ inline в `response`, либо ссылку
`response_ref` на внешний payload; DB check constraint запрещает заполнять оба
поля одновременно. `UsageEvent` хранит выбранную модель и индексируется по
`(run_id, created_at)`, что позволяет выбрать последнее потребление конкретного
прогона. Поля SHA в Finding не дублируются.

ERD показывает связи и ключевые поля; он отражает актуальные поля состояния Run,
enum Finding, inline response для RunAction и модель UsageEvent. Полный состав
колонок и ограничения следует смотреть в коде. Используются UUID, TIMESTAMPTZ,
JSONB, точный Numeric для денежных значений, native ENUM и частичные unique indexes.
Общая metadata нужна для FK между модулями и единой истории миграций; она не даёт
application права обходить интерфейсы.

## Миграции и проверка

[env.py](../alembic/env.py) собирает `target_metadata` через bootstrap. Ревизии Alembic
содержат фиксированные `op.create_table`, индексы и PostgreSQL enum types; они не
импортируют runtime-модели и не используют `Base.metadata.create_all/drop_all`.
После начальной схемы добавлены отдельные ревизии: успешный статус запуска `succeeded`, enum для `Finding.severity`, `Finding.category` и
`Finding.side`, индексы для списка runs и последнего UsageEvent, а также JSONB
`RunAction.response` с check constraint для гибридного хранения ответа. Будущие
изменения схемы оформляются новой ревизией, а не правкой уже применённой.

Миграции запускаются один раз отдельным deployment-шагом до старта процессов, не
в lifespan каждого приложения. Из корня backend-проекта:

```bash
uv sync --dev
DATABASE_URL='postgresql+psycopg://user:password@localhost:5432/backend' uv run alembic upgrade head
uv run pytest -m 'not integration'
TEST_DATABASE_URL='postgresql+psycopg://user:password@localhost:5432/backend_test' uv run pytest -m integration
```

Для нового изменения: `uv run alembic revision --autogenerate -m "description"`
с настроенным `DATABASE_URL`; результат обязательно проверить вручную, особенно
ENUM, rename, partial indexes и data migrations. Перед применением к рабочей БД
проверить совместимость версий приложения и подготовить backup/rollback-план.

[Интеграционные тесты](../tests/test_initial_migration.py) создают случайную отдельную
схему в тестовой БД и удаляют только её. Проверяют два цикла upgrade/downgrade,
совпадение миграции с ORM metadata, удаление ENUM при downgrade и commit/rollback UoW.
Пользователь тестовой БД должен иметь право CREATE SCHEMA. В Alembic передаётся
готовое соединение: переменная `DATABASE_URL` приложения не может перенаправить тест
в другую БД. Без `TEST_DATABASE_URL` интеграционные тесты пропускаются.
