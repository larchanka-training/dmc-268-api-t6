# Backend architecture

## Назначение и границы

Этот документ фиксирует границы Clean Architecture, организацию процессов,
правила зависимостей и работу с PostgreSQL. Поля, типы, FK, ограничения и индексы
определены в SQLAlchemy-моделях; дублировать полную схему таблиц в Markdown не нужно.

Артефакты задачи:

- ORM: **SQLAlchemy 2**, типизированный Declarative (`Mapped`, `mapped_column`).
- Миграции: **Alembic**, [первичная ревизия](../migrations/versions/20260913_0001_initial_schema.py).
- Схема: [реестр metadata](../packages/database/src/database/metadata.py) и ссылки на модули ниже.
- ERD: [Mermaid-исходник](erd/erd.mmd) и [SVG](erd/erd.svg).

[SYSTEM_DESIGN.md](SYSTEM_DESIGN.md) — источник правды о процессах, входных точках,
очередях, потоках данных, retry/lease, безопасности и развёртывании. Здесь описано,
как эти решения отражаются в исходном коде.

### Что реализовано, а что является планом

В текущем каркасе реализованы healthcheck FastAPI, ORM-модели, общая DB-инфраструктура,
Unit of Work, конфигурация Alembic, начальная миграция и тесты. HTTP-каркас находится
в `services/portal-api`, persistence — в `packages/database`, а миграции — в `migrations/`.
Пять сервисов теперь имеют отдельные uv-пакеты и Docker-образы; AMQP
consumers, use cases, конкретные repositories и LLM/VCS/payment gateways ещё предстоит
реализовать. Подробное устройство репозитория — в
разделе «Monorepo и границы сервисов» ниже.

## Принципы

- Зависимости направлены внутрь: transport и infrastructure зависят от
  application/domain, но не наоборот.
- Use case не знает о FastAPI, AMQP, SQLAlchemy или SDK провайдера.
- Каждый сервис владеет своей точкой входа и полным набором внутренних слоёв;
  бизнес-правила не смешиваются с техническими адаптерами.
- Внешние зависимости представлены интерфейсами application/domain. Реализации для GitHub, LLM, Stripe, RabbitMQ, Redis и S3
  можно заменить без переписывания use case.

## Слои Clean Architecture

Выбранный стиль — **Clean Architecture**. Бизнес-правила и use cases не зависят от
FastAPI, SQLAlchemy, RabbitMQ или SDK внешних сервисов. Каждый сервис имеет слои
`domain`, `application` и `infrastructure`; entrypoint остаётся отдельным транспортным
слоем.

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
| `domain` | value objects, правила, доменные ошибки, порты | только stdlib / небольшие абстракции |
| `application` | use cases, DTO команд и результатов, application ports | domain и явно нужные публичные контракты |
| `infrastructure` | persistence, messaging и реализации внешних портов | application/domain, внешние библиотеки |
| `entrypoints` | FastAPI routers, webhook endpoint, consumers, scheduler, dependency wiring | application и bootstrap |

HTTP DTO и сообщения RabbitMQ — транспортные модели Pydantic. ORM-модели — детали
persistence-слоя. Ни те, ни другие не пересекают границу application.

Формулировка задания `Router → Service → Repository → LLM Gateway` отражает набор
компонентов, но не обязательную линейную цепочку. В выбранной архитектуре Service
(use case) отдельно обращается к Repository и к LLM Gateway. Репозиторий отвечает
за БД и не вызывает LLM; orchestration остаётся в application.

## Monorepo и границы сервисов

```text
.
├── services/
│   ├── portal-api/          # frontend BFF
│   ├── auth-api/            # OAuth, JWT и сессии
│   ├── webhook-api/         # GitHub webhook HTTP boundary
│   ├── worker/              # consumer review.run
│   └── publisher/           # consumer review.publish
├── packages/
│   ├── contracts/           # versioned HTTP and AMQP contracts
│   └── database/            # SQLAlchemy models and PostgreSQL primitives
├── migrations/              # database-migrator: revisions, tests and image
├── infra/
│   ├── docker/
│   └── deploy/
├── alembic.ini
├── docker-compose.yml
├── Makefile
├── pyproject.toml           # uv workspace root
└── uv.lock                  # one locked dependency graph
```

| Компонент | Граница ответственности | Точка входа |
| --- | --- | --- |
| `portal-api` | BFF личного кабинета: подписка, биллинг, настройки, данные прогона и ручной rerun | FastAPI / REST / SSE |
| `auth-api` | GitHub OAuth, JWT и сессии | FastAPI |
| `webhook-api` | проверка GitHub HMAC, идемпотентность и постановка review-задач | FastAPI |
| `worker` | получение контекста, вызов LLM и сохранение findings | AMQP `review.run.*` |
| `publisher` | публикация review и check-run в GitHub | AMQP `review.publish` |
| `database-migrator` | применение и проверка схемы PostgreSQL | Compose service `migrator`, profile `tools` |

Первые пять компонентов — независимо собираемые микросервисы. Каждый имеет собственные
`pyproject.toml`, Dockerfile, тесты и `app/entrypoints/<name>/`. Entrypoint остаётся
тонкой границей: декодирует HTTP/AMQP контракт, получает use case из container и
преобразует результат обратно в транспортный формат. Бизнес-решения в entrypoint не
допускаются. `database-migrator` не является шестым микросервисом и не работает
постоянно.

### Зависимости и данные

- Сервис использует пакет только через явную зависимость в своём `pyproject.toml`.
- `packages/contracts` содержит только версионированные DTO и схемы сообщений; без
  ORM-моделей, repositories и use cases.
- `packages/database` — узкий технический пакет общей PostgreSQL-схемы. Он
  импортируется как `database`, а не `reviewer_database`; бизнес-правила в нём не живут.
- Workspace-пакет `database-migrator` в `migrations/` — единственный потребитель
  Alembic. Он импортирует
  metadata непосредственно из `packages/database`, а не из `portal-api` или другого
  сервиса.
- Сервисы передают между собой идентификаторы и версионированные сообщения RabbitMQ,
  а не ORM-сущности и не импорты чужого `app`.

Сейчас схема и её история миграций общие. Когда сервис получит собственное хранилище,
к нему переносятся только принадлежащие ему модели и новые миграции — первоначальную
ревизию нельзя механически разрезать.

## Интерфейсы и инфраструктурные реализации

Application зависит от узких интерфейсов, сгруппированных по задаче, а не от одного
«god repository».

| Интерфейс | Владелец | Назначение |
| --- | --- | --- |
| `RunRepository`, `ReviewRepository` | `reviews` | состояние прогона и результаты review |
| `RepositoryReader` | `repositories` | настройки репозитория для use cases |
| `VcsProvider` | использующий use case | минимальные операции конкретного VCS-сценария |
| `LlmGateway` | `reviews` | structured результат анализа контекста |
| `PaymentGateway` | `billing` | создание и подтверждение оплаты |
| `UnitOfWork` | application | граница транзакции и доступ к интерфейсам repositories |
| `Clock`, `IdGenerator` | application | детерминированные тесты и технические значения |

`LlmGateway` принимает неизменяемый контекст, prompt/rule snapshots и выбранный engine;
возвращает typed findings, usage и нормализованную ошибку. Он не публикует комментарии,
не меняет состояние run и не знает о GitHub.

## Структура Clean Architecture внутри сервиса

Каждый микросервис — самостоятельное Clean Architecture-приложение. Папки
`modules/` и `common/` не используются: они отражали прежний модульный монолит и
размывали границы сервисов. Зависимости всегда направлены внутрь:
`entrypoints → application → domain`; `infrastructure` реализует порты внутреннего
слоя, но не задаёт бизнес-правила.

```text
services/<service>/app/
  bootstrap/
    config.py                  # env / settings
    container.py               # composition root и DI wiring
    logging.py
  domain/
    entities/                  # entities, value objects, domain errors
    ports/                     # domain-owned abstractions
  application/
    use_cases/                 # orchestration, commands, queries and DTOs
    ports/
      unit_of_work.py          # реализован: интерфейс управления транзакцией
  infrastructure/
    messaging/                 # AMQP adapters
    providers/                 # GitHub, LLM, Stripe and storage adapters
  entrypoints/
    <service>/                 # service-specific FastAPI or AMQP boundary
```

Общие PostgreSQL-адаптеры находятся в отдельном workspace-пакете, а не в
`app/common` и не в коде API:

```text
packages/database/src/database/
  models/                      # SQLAlchemy models общей схемы
  base.py                      # declarative base
  session.py                   # session factory
  unit_of_work.py              # SQLAlchemy adapter
```

`bootstrap/container.py` — единственное место, где создаются engine, session factory,
GitHub client, LLM gateway и RabbitMQ publisher. Entrypoint запрашивает у container
готовый use case и не собирает зависимости сам. Тесты application-слоя подменяют интерфейсы
fakes; интеграционные тесты проверяют реальные инфраструктурные реализации отдельно.

Engine и сетевые клиенты живут в течение lifespan процесса и закрываются при shutdown
(`await engine.dispose()` и закрытие клиентов). Session и Unit of Work создаются
заново для каждой короткой транзакции, не переиспользуются между запросами, сообщениями
или параллельными asyncio tasks. `database.metadata` собирает только описания
таблиц для Alembic и не открывает соединение с БД.

### Границы кода и контрактов

Интерфейс принадлежит application или domain-коду сервиса, который его использует:
например, `LlmGateway` определяется в `application/ports`, а реализация живёт в
`infrastructure/providers`. Публичные HTTP DTO и сообщения RabbitMQ оформляются как
версионированные контракты в `packages/contracts`; они не содержат ORM-моделей,
repositories или use cases. `packages/database` — намеренно узкое исключение:
технический пакет общей PostgreSQL-схемы, импортируемый как `database` (не
`reviewer_database`) и явно объявленный в зависимостях потребляющего сервиса. Не
следует создавать новый общий технический слой между сервисами: код остаётся
локальным, пока не станет стабильным публичным контрактом.

## Транзакции и выполнение review

Application управляет транзакцией через [UnitOfWork](../services/portal-api/app/application/ports/unit_of_work.py).
[SqlAlchemyUnitOfWork](../packages/database/src/database/unit_of_work.py) создаёт одну
`AsyncSession`, требует явного `commit()` и при выходе откатывает незавершённую
транзакцию и закрывает session, в том числе при исключении.

Конкретный UoW предоставляет application интерфейсы repositories; инфраструктурная
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

| Группа ORM-моделей | Модели |
| --- | --- |
| [workspaces](../packages/database/src/database/models/workspaces.py) | Workspace |
| [repositories](../packages/database/src/database/models/repositories.py) | ProviderInstallation, Repository, RuleVersion, RepoConventions |
| [reviews](../packages/database/src/database/models/reviews.py) | PromptVersion, CodeChange, Run, ContextPayload, Finding, Comment, RunAction |
| [webhooks](../packages/database/src/database/models/webhooks.py) | WebhookEvent |
| [billing](../packages/database/src/database/models/billing.py) | Payment, CreditLedger |
| [analytics](../packages/database/src/database/models/analytics.py) | UsageEvent |

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
группами таблиц и единой истории миграций; она не даёт application права обходить
интерфейсы.

## Миграции и проверка

[env.py](../migrations/env.py) собирает `target_metadata` из `database.metadata`, без импорта
какого-либо сервиса. Ревизии Alembic
содержат фиксированные `op.create_table`, индексы и PostgreSQL enum types; они не
импортируют runtime-модели и не используют `Base.metadata.create_all/drop_all`.
Будущие изменения схемы оформляются новой ревизией, а не правкой уже применённой.

Миграции запускаются один раз отдельным deployment-шагом до старта процессов, не
в lifespan каждого приложения. Из корня backend-проекта:

```bash
uv sync --all-packages
make migrate
DATABASE_URL='postgresql+psycopg://user:password@localhost:5432/backend' uv run --package database-migrator alembic -c alembic.ini upgrade head
uv run --package portal-api pytest services/portal-api/tests
TEST_DATABASE_URL='postgresql+psycopg://user:password@localhost:5432/backend_test' uv run --package database-migrator pytest migrations/tests -m integration
```

Для нового изменения: `uv run --package database-migrator alembic -c alembic.ini revision --autogenerate -m "description"`
с настроенным `DATABASE_URL`; результат обязательно проверить вручную, особенно
ENUM, rename, partial indexes и data migrations. Перед применением к рабочей БД
проверить совместимость версий приложения и подготовить backup/rollback-план.

[Интеграционные тесты](../migrations/tests/test_initial_migration.py) создают случайную отдельную
схему в тестовой БД и удаляют только её. Проверяют два цикла upgrade/downgrade,
совпадение миграции с ORM metadata, удаление ENUM при downgrade и commit/rollback UoW.
Пользователь тестовой БД должен иметь право CREATE SCHEMA. В Alembic передаётся
готовое соединение: переменная `DATABASE_URL` приложения не может перенаправить тест
в другую БД. Без `TEST_DATABASE_URL` интеграционные тесты пропускаются.
