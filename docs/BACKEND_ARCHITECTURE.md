# Backend architecture

## Назначение и границы

Этот документ фиксирует границы Clean Architecture, организацию процессов,
правила зависимостей и работу с PostgreSQL. Поля, типы, FK, ограничения и индексы
определены в SQLAlchemy-моделях; дублировать полную схему таблиц в Markdown не нужно.

Артефакты задачи:

- ORM: **SQLAlchemy 2**, типизированный Declarative (`Mapped`, `mapped_column`).
- Миграции: **Alembic**, [первичная ревизия](alembic/versions/20260913_0001_initial_schema.py).
- Схема: [реестр моделей](app/bootstrap/db_metadata.py) и ссылки на модули ниже.
- ERD: [Mermaid-исходник](docs/erd/erd.mmd) и [SVG](docs/erd/erd.svg).

[SYSTEM_DESIGN.md](dos/SYSTEM_DESIGN.md) — источник правды о процессах, входных точках,
очередях, потоках данных, retry/lease, безопасности и развёртывании. Здесь описано,
как эти решения отражаются в исходном коде.

### Что реализовано, а что является планом

В текущем каркасе реализованы healthcheck FastAPI, ORM-модели, общая DB-инфраструктура,
Unit of Work, конфигурация Alembic, начальная миграция и тесты. Дерево слоёв и пять
entrypoints ниже — целевая организация приложения: use cases, конкретные repositories,
LLM/VCS/payment gateways и consumers ещё предстоит реализовать. Текущий Compose
поднимает backend и PostgreSQL, а не весь целевой runtime.

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

Согласно Р-12 из [SYSTEM_DESIGN.md](../SYSTEM_DESIGN.md), image имеет пять entrypoint:
`api`, `webhook`, `worker`, `publisher` и `collector`. Каждый располагается в
`app/entrypoints/<name>/` и остаётся тонкой точкой входа: декодирует транспортный
контракт, получает use case из container и преобразует результат обратно в HTTP/AMQP.
Бизнес-решения в entrypoint не допускаются.

Развёртывание: один репозиторий, один Docker image и несколько процессов с разными
командами запуска. На старте три контейнера приложения: `api` (+ `collector`),
`webhook`, `worker` (+ `publisher`). RabbitMQ и PostgreSQL — общая инфраструктура.
При раздельном запуске `publisher`/`collector` встроенные consumers нужно отключить
в исходных процессах через конфигурацию; это не требует изменения бизнес-логики.

Это модульный многопроцессный backend, а не полностью автономные микросервисы:
процессы разделяют версию кода, схему БД и миграции. Бизнес-модуль не равен контейнеру.
Модели принадлежат модулям; чужие таблицы не изменяются напрямую из use cases —
доступ идёт через публичные интерфейсы. Между процессами передаются версионированные
сообщения с идентификаторами, не ORM-объекты и не Python-вызовы.

## Интерфейсы и инфраструктурные реализации

Application зависит от узких интерфейсов, сгруппированных по задаче, а не от одного
«god repository».

| Интерфейс | Владелец | Назначение |
| --- | --- | --- |
| `RunRepository`, `ReviewRepository` | `reviews` | состояние прогона и результаты review |
| `RepositoryReader` | `repositories` | настройки репозитория для use cases |
| `VcsProvider` | использующий модуль | минимальные операции конкретного VCS-сценария |
| `LlmGateway` | `reviews` | structured результат анализа контекста |
| `PaymentGateway` | `billing` | создание и подтверждение оплаты |
| `UnitOfWork` | общий контракт; расширение в использующем модуле | граница транзакции и доступ к интерфейсам repositories |
| `Clock`, `IdGenerator` | `common` | детерминированные тесты и технические значения |

`LlmGateway` принимает неизменяемый контекст, prompt/rule snapshots и выбранный engine;
возвращает typed findings, usage и нормализованную ошибку. Он не публикует комментарии,
не меняет состояние run и не знает о GitHub.

## Модульная структура кода

Код организован **сначала по бизнес-модулю**, а уже внутри модуля — по слоям Clean
Architecture. Это не позволяет превратить `common` в свалку и сохраняет use case,
его интерфейс и реализацию рядом друг с другом.

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
      domain/                  # Run, Finding, Comment, Review interfaces
      application/             # create/execute/publish/cancel/rerun
      infrastructure/          # review models, repos, LLM gateway
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
    api/                       # FastAPI routes и response schemas
    webhook/                   # GitHub webhook FastAPI app
    worker/                    # review.run AMQP consumer
    publisher/                 # review.publish AMQP consumer
    collector/                 # events.* AMQP consumer
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

Application управляет транзакцией через [UnitOfWork](app/common/application/unit_of_work.py).
[SqlAlchemyUnitOfWork](app/common/infrastructure/db/unit_of_work.py) создаёт одну
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

## PostgreSQL и владение моделями

PostgreSQL 17, драйвер `psycopg` v3. Runtime использует `AsyncSession`, Alembic —
синхронное соединение того же драйвера. URL: `postgresql+psycopg://...`.
SQLModel не используется: ORM отделена от transport DTO и доменных контрактов.

| Модуль / код схемы | Модели |
| --- | --- |
| [workspaces](app/modules/workspaces/infrastructure/models.py) | Workspace |
| [repositories](app/modules/repositories/infrastructure/models.py) | ProviderInstallation, Repository, RuleVersion, RepoConventions |
| [reviews](app/modules/reviews/infrastructure/models.py) | PromptVersion, CodeChange, Run, ContextPayload, Finding, Comment, RunAction |
| [webhooks](app/modules/integrations/webhooks/infrastructure/models.py) | WebhookEvent |
| [billing](app/modules/billing/infrastructure/models.py) | Payment, CreditLedger |
| [analytics](app/modules/analytics/infrastructure/models.py) | UsageEvent |

Названия из задания сопоставляются так: `MergeRequest → CodeChange`,
`ReviewJob → Run`. Это согласованные переименования, а не отсутствующие сущности.
`CodeChange` хранит обновляемое состояние PR/MR; `Run` фиксирует SHA, engine и ссылки
на версии prompt/rules конкретного запуска. `ContextPayload` появляется при сборке
контекста: связь с Run — `0..1`, а не обязательная строка сразу после webhook.
Finding содержит результат анализа (включая category и необязательный suggestion),
Comment — сведения о его публикации. Поля SHA в Finding не дублируются.

ERD показывает связи и ключевые поля; полный состав колонок и ограничения следует
смотреть в коде. Используются UUID, TIMESTAMPTZ, JSONB, точный Numeric для денежных
значений, native ENUM и частичные unique indexes. Общая metadata нужна для FK между
модулями и единой истории миграций; она не даёт application права обходить интерфейсы.

## Миграции и проверка

[env.py](alembic/env.py) собирает `target_metadata` через bootstrap. Ревизии Alembic
содержат фиксированные `op.create_table`, индексы и PostgreSQL enum types; они не
импортируют runtime-модели и не используют `Base.metadata.create_all/drop_all`.
Будущие изменения схемы оформляются новой ревизией, а не правкой уже применённой.

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

[Интеграционные тесты](tests/test_initial_migration.py) создают случайную отдельную
схему в тестовой БД и удаляют только её. Проверяют два цикла upgrade/downgrade,
совпадение миграции с ORM metadata, удаление ENUM при downgrade и commit/rollback UoW.
Пользователь тестовой БД должен иметь право CREATE SCHEMA. В Alembic передаётся
готовое соединение: переменная `DATABASE_URL` приложения не может перенаправить тест
в другую БД. Без `TEST_DATABASE_URL` интеграционные тесты пропускаются.
