# SYSTEM_DESIGN — AI Code Reviewer (команда 6)

| | |
|---|---|
| Статус | **утверждён командой** — PR #36 (#20) |
| Владелец | техлид (роль 1) |
| Связанные документы | `BACKEND_ARCHITECTURE.md` (роль 6, ERD), [`PIPELINE_SPEC.md`](PIPELINE_SPEC.md) (жизненный цикл Run, retry, сбои), [`contracts/openapi.yaml`](../contracts/openapi.yaml) (HTTP API), [`FRONTEND_ARCHITECTURE.md`](https://github.com/larchanka-training/dmc-268-ui-t6/blob/main/docs/FRONTEND_ARCHITECTURE.md) (роль 5, Zod-контракты), [`TEST_PLAN.md`](https://github.com/larchanka-training/dmc-268-ui-t6/blob/main/docs/TEST_PLAN.md) (роль 2, quality gates), инфраструктура (роль 3) |
| Нумерация решений | `Р-1…Р-15`; `Р-1…Р-9` — общие с [`TEST_PLAN.md`](https://github.com/larchanka-training/dmc-268-ui-t6/blob/main/docs/TEST_PLAN.md), не менять |

**Продукт.** GitHub App, который ревьюит pull request с лейблом `ai-review`. После зелёного CI бот публикует одно ревью с inline-комментариями прямо в PR. Web UI показывает прогоны, трейс действий агента, метрики и расход.

**Стек (зафиксирован).** Backend: Python 3.13, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL 17, RabbitMQ, Redis, uv, ruff, mypy strict; объектного хранилища в MVP нет (S3 — после MVP, §10). Frontend: React 19, Vite 8, TypeScript 6, pnpm, Zustand + TanStack Query, Zod, Vitest 5. Инфра: Docker Compose; staging — курсовой VPS за edge-прокси Caddy (Terraform / Hetzner — альтернативная цель, [CICD.md](CICD.md) §8).

---

## 1. Решения

| # | Решение | Причина |
|---|---|---|
| Р-1 | Транспорт задач — **RabbitMQ** (durable, persistent, ack после фиксации состояния). **Источник истины — PostgreSQL**: брокер доставляет указатель на задачу, состояние живёт в БД | Микросервисы → общая БД как очередь становится антипаттерном; RabbitMQ даёт ack/DLQ/приоритеты; отмена и схлопывание не выражаются в брокере — поэтому состояние в БД |
| Р-2 | Один активный прогон на PR: ключ `(installation_id, repo_id, pr_number)`; новый пуш отменяет предыдущий прогон, побеждает последний `head_sha` | Иначе 10 пушей = 10 ревью и 10× цена. Разные PR, ветки, репозитории — параллельны |
| Р-3 | Два движка за одним контрактом `ReviewOutput` (§5): **DiffEngine** (без диска, ≤ 40 с) и **SandboxEngine** (контейнер, ≤ 10 мин). v1 = DiffEngine | Движок будет меняться; всё ниже контракта об этом не знает |
| Р-4 | Сандбокс: `--network=none`, без токенов внутри, клон монтирует контроллер снаружи | Промпт-инъекция в диффе — штатная ситуация |
| Р-5 | Публикация **одним** `POST /pulls/{n}/reviews`; повтор безопасен по `findings_hash`; строки вне диффа — в тело ревью | N комментариев = N уведомлений и N вызовов к rate limit; 422 от GitHub на строку вне диффа |
| Р-6 | Правила и промпты — неизменяемые версии; прогон ссылается на `rule_version_id`, `prompt_version_id` | Dry-run, откат, объяснимость — бесплатно |
| Р-7 | Арендатор = **Workspace** (роль 6), к которому привязана `ProviderInstallation`; права на репозитории — из провайдера, не из своей таблицы ролей | Своя модель ролей разъедется с GitHub |
| Р-8 | `usage_events` (токены, деньги, модель) — с первого вызова LLM, только вставка | Восстановить задним числом нельзя; основа для `CreditLedger` |
| Р-9 | Бот односторонний: публикует, на комментарии не отвечает; **обязательно** игнорирует собственные события | Скорость запуска; защита от цикла «бот → CI → бот» |
| Р-10 | Триггер — **конъюнкция двух событий в любом порядке**: на PR стоит лейбл `ai-review` ∧ CI успешен для текущего `head_sha`. Бота нельзя запросить ревьюером: GitHub отвечает 201 с пустым `requested_reviewers`, событие `review_requested` не приходит ([#37](https://github.com/larchanka-training/dmc-268-api-t6/issues/37#issuecomment-5874776355)). **После пуша — авто-повтор**, пока PR открыт и стоит лейбл (наш флаг `ai_review_labeled`): бот лейбл не снимает, флаг снимают `unlabeled` от человека и закрытие PR (решения техлида по #20, OQ-1, и по #37). Условие «CI успешен» и sweep «2 мин без CI» — §6.1, [PIPELINE_SPEC](PIPELINE_SPEC.md) §8 | Решение мита; события независимы, порядок не гарантирован. Лейбл ставит только участник с правом triage и выше: внешний автор PR не запустит ревью и не потратит бюджет LLM |
| Р-11 | Провайдер VCS — за портом `VcsProvider`; v1 реализует только GitHub | ТЗ упоминает GitLab, роль 6 — Bitbucket; порт дешёвый, реализации — нет |
| Р-12 | Один monorepo и пять независимо собираемых сервисов (`portal-api`, `auth-api`, `webhook-api`, `worker`, `publisher`) с собственными зависимостями; у роли `webhook-api` второй процесс — `webhook-worker` (тот же образ, `python -m app.webhook_worker`, §4) | Изоляция релизов и зависимостей без потери единого lock-файла и локального окружения |
| Р-13 | **RAG не входит в MVP.** Worker получает контекст через порт `ContextProvider`: в MVP — детерминированный сборщик L1–L4, позднее — `RagContextProvider`, возвращающий тот же `ContextPayload` | В MVP нет затрат и операционных рисков embeddings/vector DB, но RAG подключается без изменения LLM, post-processing и публикации |
| Р-14 | Успешный прогон — статус **`succeeded`** везде: домен (`RunState.SUCCEEDED`), PG enum `run_state`, API, Zod фронтенда. Полный набор: `queued\|running\|publishing\|succeeded\|failed\|cancelled\|skipped` (§6.4); `completed` — только события и статусы GitHub (`check_suite`, `check_run`, `workflow_run`) | Одно имя от БД до UI без маппинга; код и миграция уже используют `succeeded` и `publishing` |
| Р-15 | Снимок диффа для `GET /api/runs/{id}/diff` хранится в **PostgreSQL**: патчи по файлам с ключом `(code_change, head_sha)`; дифф > 3 000 строк → хранится только список файлов (API: `patch: null`, `RunSession.summaryOnly: true`); удаляется каскадом вместе с Workspace. Объектное хранилище для снимка не используется (в MVP его нет, §10) | UI получает дифф прогона без GitHub и object storage; объём ограничен порогом сводки; данные клиента удаляются вместе с арендатором |

---

## 2. Границы ответственности

| Слой | Отвечает | Не отвечает |
|---|---|---|
| **Frontend** (`dmc-268-ui-t6`) | Экраны: обзор + лента прогонов, карточка прогона с диффом и инлайн-комментариями, инспектор трейса (`RunSession → RunAction`), репозитории и правила, метрики. Клиентское состояние (Zustand), серверный кэш (TanStack Query) | Не считает метрики, не парсит дифф вручную (сырой unified-diff `RawFileDiff` разбирает библиотека — `react-diff-view` / `gitdiff-parser` — за адаптером), не знает про провайдеров |
| **Backend** (`dmc-268-api-t6`) | Приём вебхуков, триггер, очередь, состояние прогонов, сборка контекста, вызов LLM, постобработка находок, публикация, метрики, авторизация, кэш | Не хранит код клиентов сверх сроков §10 (кэш блобов — 7 дней, снимки диффов и контексты — до удаления Workspace); не принимает решений о качестве кода — это LLM |
| **LLM-сервисы** | Анализ контекста → находки в фиксированной схеме; промежуточный шаг «конвенции репозитория» (роль 7) | Не ходят в GitHub, не имеют токенов, не решают, что публиковать (фильтрует постобработка) |

---

## 3. C4 — уровень 1: контекст

> Нотация C4, рендер через `flowchart`: родной `C4Context`/`C4Container` в Mermaid накладывает подписи рёбер друг на друга.

```mermaid
flowchart LR
  dev["<b>Разработчик</b><br/><i>[Person]</i><br/>открывает PR, ставит лейбл ai-review,<br/>читает замечания в GitHub"]
  op["<b>Оператор / тимлид</b><br/><i>[Person]</i><br/>включает бота на репозиториях,<br/>правит правила, смотрит прогоны и расход"]
  sys["<b>AI Code Reviewer</b><br/><i>[Software System]</i><br/>контекст → LLM → одно ревью в PR"]
  gh["<b>GitHub</b><br/><i>[External System]</i><br/>PR, вебхуки, check-runs,<br/>REST v3 / GraphQL v4"]
  llm["<b>LLM Provider</b><br/><i>[External System]</i><br/>Anthropic / OpenAI / self-hosted<br/>за LLM Gateway"]

  dev -->|"открывает PR,<br/>ставит лейбл ai-review"| gh
  gh -->|"вебхуки<br/>HTTPS + HMAC"| sys
  sys -->|"дифф, файлы, публикация ревью,<br/>check-run · REST / GraphQL"| gh
  sys -->|"контекст → находки<br/>HTTPS"| llm
  op -->|"правила, прогоны, метрики<br/>Web UI · вход через GitHub App"| sys
  gh -.->|"ревью бота в PR"| dev

  classDef person fill:#08427b,color:#fff,stroke:#052e56
  classDef system fill:#1168bd,color:#fff,stroke:#0b4884
  classDef ext fill:#8a8a8a,color:#fff,stroke:#5f5f5f
  class dev,op person
  class sys system
  class gh,llm ext
```

---

## 4. C4 — уровень 2: контейнеры

Диаграмма фиксирует целевую границу системы; развёртывание сервисов, включая одноразовый
`migrator`, — в §14.

```mermaid
flowchart TB
  dev["<b>Разработчик</b><br/><i>[Person]</i>"]
  op["<b>Оператор</b><br/><i>[Person]</i>"]
  gh["<b>GitHub</b><br/><i>[External System]</i>"]
  llm["<b>LLM Provider</b><br/><i>[External System]</i>"]

  subgraph sys["AI Code Reviewer"]
    direction TB
    ui["<b>Web UI</b><br/><i>[Container: React 19 + Vite 8 + TS 6]</i><br/>прогоны, дифф, инспектор трейса,<br/>правила, метрики"]
    portal["<b>Portal API</b><br/><i>[Container: FastAPI BFF]</i><br/>REST + SSE, подписка, биллинг,<br/>настройки и ручной перезапуск"]
    auth["<b>Auth API</b><br/><i>[Container: FastAPI]</i><br/>вход через GitHub App,<br/>JWT и refresh, Workspace (Р-7)"]
    hook["<b>Webhook API</b><br/><i>[Container: FastAPI]</i><br/>GitHub HMAC, квитанция в webhook_events<br/>(идемпотентность по delivery_id), ack < 500 мс"]
    ww["<b>webhook-worker</b><br/><i>[Container: Python]</i><br/>разбор квитанций раз в 30 с: проекция PR<br/>и лейбла ai-review, подключение репозиториев,<br/>схлопывание Р-2, триггер Р-10 (#52)"]
    worker["<b>AI Worker</b><br/><i>[Container: Python + aio-pika]</i><br/>сборщик контекста (4 уровня) → LLM Gateway<br/>→ постобработка → трейс"]
    pub["<b>GitHub Publisher</b><br/><i>[Container: Python + aio-pika]</i><br/>валидация координат, одно ревью и check-run"]
    sandbox["<b>Sandbox Runner</b><br/><i>[Container: Docker, --network=none]</i><br/>SandboxEngine · фаза 3"]
    mq[("<b>RabbitMQ</b><br/><i>[AMQP 0-9-1]</i><br/>reviews (direct), retry и DLX")]
    pg[("<b>PostgreSQL 17</b><br/><i>[SQLAlchemy 2 + Alembic]</i><br/>источник истины")]
    redis[("<b>Redis</b><br/>токены, блобы, AST, дерево репо")]
  end

  dev -->|"PR, лейбл ai-review"| gh
  op -->|HTTPS| ui
  ui -->|"REST + SSE<br/>Zod-контракты"| portal
  ui -->|"вход, refresh<br/>/api/auth"| auth
  gh -->|"webhooks<br/>HTTPS + HMAC"| hook

  hook -->|"webhook_events<br/>(квитанция)"| pg
  ww -->|"claim квитанций, code_changes,<br/>repositories, отмена runs"| pg
  ww -->|"GET /pulls/{n}, дерево, лейбл ai-review,<br/>check-suites и status (#52) · REST"| gh
  ww -.->|"review.run (#52)"| mq
  portal -->|"чтение, конфигурация"| pg
  portal -->|"review.run<br/>(rerun)"| mq
  portal -->|"блобы для /files"| redis
  auth -->|"user authorization,<br/>/user/installations (Р-7)"| gh
  auth -->|"пользователи,<br/>refresh-токены"| pg

  mq -->|"review.run.fast / .deep<br/>prefetch=1"| worker
  worker -->|"diff, blobs, tree · REST"| gh
  worker -->|"промпт → находки"| llm
  worker -->|"блобы, AST"| redis
  worker -->|"context_payloads, findings,<br/>run_actions, usage_events"| pg
  worker -.->|"docker API · фаза 3"| sandbox
  worker -->|"review.publish"| mq

  mq -->|review.publish| pub
  pub -->|"POST /pulls/{n}/reviews,<br/>check-run · REST"| gh
  pub -->|"comments, run.state"| pg
  pub -->|"installation-токен"| redis

  classDef person fill:#08427b,color:#fff,stroke:#052e56
  classDef ext fill:#8a8a8a,color:#fff,stroke:#5f5f5f
  classDef cont fill:#438dd5,color:#fff,stroke:#2e6295
  classDef store fill:#2f6db3,color:#fff,stroke:#1f4f85
  class dev,op person
  class gh,llm ext
  class ui,portal,auth,hook,ww,worker,pub,sandbox cont
  class mq,pg,redis store
```

**Один monorepo, пять сервисов** (Р-12): `services/portal-api`, `services/auth-api`,
`services/webhook-api`, `services/worker`, `services/publisher`. Каждый собирается своим
Dockerfile и запускается отдельным контейнером; у роли `webhook-api` второй процесс из того же
образа — `webhook-worker` (`python -m app.webhook_worker`, ниже). PostgreSQL, RabbitMQ и Redis —
общая инфраструктура на переходном этапе. Объектного хранилища в MVP нет: всё, что §10 раньше
относил к S3, лежит в PostgreSQL (D1 по #20). Отдельного Event Collector нет:
`usage_events` пишет worker (LLM Gateway), метрики §12 строятся по ним.

Import path един для всех сервисов: процесс стартует из `services/<name>/app/main.py` —
HTTP-сервисы командой `uvicorn app.main:app`, consumers `worker` и `publisher` командой
`python -m app.main`; `app.main` только импортирует тонкий entrypoint
`app.entrypoints.<entry>` (`portal`, `auth`, `webhook`, `worker`, `publisher`).

Workspace-пакет `database-migrator` запускается Compose-сервисом `migrator` и не
входит в runtime-набор: это одноразовый job из profile `tools`, который применяется
до запуска сервисов и обращается только к PostgreSQL.

Диаграмма и раскладка выше — целевые. До разделения на сервисы (#16; скорее всего после
этого спринта, отдельным рефакторингом) `portal-api`, `auth-api` и `webhook-api` —
логические роли: их маршруты живут в одном FastAPI-приложении `app/main.py`, а `worker`
(в MVP — вместе с consumer `review.publish`, §7.1) запускается отдельным процессом из
того же образа. Отдельным процессом из того же образа запускается и `webhook-worker`
(`python -m app.webhook_worker`, Compose profile `webhooks`): он разбирает квитанции,
которые маршрут `webhook-api` записал в `webhook_events`, и требует PostgreSQL и GitHub App
(ID, приватный ключ, логин бота) — [WEBHOOK_WORKER.md](WEBHOOK_WORKER.md).

---

## 5. C4 — уровень 3: компоненты AI Worker

```mermaid
flowchart LR
  MQ[(RabbitMQ<br/>review.run.*)] --> C[Consumer<br/>prefetch=1, manual ack]
  C --> G[RunGuard<br/>state == queued?<br/>cancel_requested?]
  G -->|skip| ACK[ack без работы]
  G --> CC

  subgraph CC[ContextProvider → ContextPayload]
    direction TB
    M[MetaLoader<br/>PR, ветка, AGENTS.md,<br/>конвенции репо] --> D[DiffFetcher<br/>L1 unified diff → FileDiff]
    D --> F[FileFetcher<br/>блобы по sha, кэш Redis]
    F --> S[SurroundingExtractor<br/>L2 ±N строк / границы функции]
    F --> W[WholeFileLoader<br/>L3 при size ≤ лимита]
    F --> A[ASTIndexer<br/>L4 tree-sitter: импорты, символы]
    S & W & A --> B[BudgetAllocator<br/>приоритеты файлов, лимит токенов]
    R[RagContextProvider<br/>после MVP: retrieval-кандидаты] -. до BudgetAllocator .-> B
  end

  B --> P[PromptBuilder<br/>prompt_version + rule_version<br/>+ конвенции]
  P --> L[LLM Gateway<br/>OpenAI-совместимый адаптер, ротация ключей,<br/>ретраи, fallback-модель, prompt cache,<br/>usage_events, llm.call]
  L --> PP[FindingsPostProcessor<br/>lint-фильтр ×2, порог confidence,<br/>дедуп, hunk-валидация, лимит N]
  PP --> T[TraceRecorder<br/>RunAction: tool, request, response, ms]
  T --> OUT[(review.publish)]
  T --> PG[(PostgreSQL)]
  G -.checkpoints: после каждого уровня,<br/>перед каждым LLM-вызовом.-> PP
```

Контракт между движками и всем остальным — **`ReviewOutput`**: `{findings: [≤ 10], summary: {problem, done_well, effort}}`. Форму задаёт [`review/schemas/review-output.schema.json`](../review/schemas/review-output.schema.json) (JSON Schema draft 2020-12, единственный источник формы); рантайм-модель — Pydantic `ReviewOutput` (`app/modules/reviews/application/review_output.py`), совпадение проверяет `tests/test_review_output_schema.py`. Все ключи обязательны, nullable-поля приходят со значением `null`, лишних ключей нет. Семантика, которую схема не выражает, правила `suggestion` и strict structured output провайдеров — [PIPELINE_SPEC](PIPELINE_SPEC.md) §9. Поля находки — для обзора, авторитетна схема:

```python
class ReviewFinding:          # элемент ReviewOutput.findings
    path: str                 # якорь (D6): path / start_line / line
    start_line: int | None    # null — одна строка; иначе start_line < line
    line: int                 # строка новой версии файла
    severity: Literal["critical", "high", "medium", "low", "info"]
    category: Literal["security", "correctness", "performance", "readability"]
    title: str                # ≤ 80 символов, одна строка
    body: str                 # markdown, ≤ 1200 символов
    suggestion: str | None    # готовая замена строк start_line..line → ```suggestion
    confidence: float         # 0..1
    rule_name: str | None     # имя пользовательского правила → префикс атрибуции (роль 7)
```

`side` и SHA в выходе нет: сторону (`RIGHT`) ставит постпроцессор, SHA берётся из `Run.head_sha`. Маппинг якоря в БД, API и GitHub — PIPELINE_SPEC §10.

**LLM Gateway** (#33) — один адаптер OpenAI-совместимого API: EUrouter и self-hosted (LM Studio, Ollama, vLLM) различаются только конфигурацией — base URL, модель, список ключей (`docs/SECRETS.md`). Внутри шлюза: **ротация ключей** — несколько ключей на провайдера, при 401/403/429 запрос уходит со следующим ключом, и это не считается вызовом; **fallback-модель** — вторая модель со strict structured output (D7), вызывается один раз на попытку после исчерпания повторов основной модели. Повторы, repair-вызов, лимиты вызовов и стоимости, дедлайн попытки и нормализация ошибок в `error_code` — PIPELINE_SPEC §3, §4.5, §5.1, §6. Шлюз подгоняет L1-контекст под `min(окно модели, лимит §13) − резерв на ответ` (обрезка файла с трейлером, остальное — в `<omitted_files>`), пишет `usage_events` и по записи `llm.call` на каждый вызов провайдера через порты вне транзакции. Раскладка кода и выбор механизма структурного вывода — `BACKEND_ARCHITECTURE.md`, раздел «LLM Gateway».

---

## 6. Потоки данных

### 6.1 Триггер (Р-10): два события в любом порядке

```mermaid
sequenceDiagram
  autonumber
  participant GH as GitHub
  participant WH as Webhook API
  participant PG as PostgreSQL
  participant WW as webhook-worker
  participant MQ as RabbitMQ

  GH->>WH: pull_request.labeled (label = ai-review)
  WH->>WH: HMAC ok? (иначе 401) заголовки и JSON? (иначе 400)
  WH->>PG: webhook_events INSERT … ON CONFLICT (delivery_id) DO NOTHING
  WH-->>GH: 202 pending / duplicate (< 500 мс)
  WW->>PG: claim квитанции (цикл раз в 30 с)
  WW->>GH: GET /pulls/{n} под advisory lock — текущие лейблы
  WW->>PG: code_changes: ai_review_labeled = true
  WW->>PG: try_enqueue(pr): ci_ok(head_sha)? — нет, ждём

  GH->>WH: check_suite.completed (head_sha)
  WH->>PG: webhook_events INSERT (квитанция)
  WH-->>GH: 202
  WW->>PG: code_changes: ci_status[head_sha] (кэш событий)
  WW->>GH: try_enqueue: REST check-suites и combined status для head_sha (свой suite не в счёт)
  WW->>PG: ai_review_labeled ∧ CI зелёный ∧ нет активного run → runs INSERT (queued)
  WW->>MQ: publish review.run {run_id, head_sha, engine}
  Note over WW,MQ: ci_status, try_enqueue и publish из доставки подключает #52
```

Ответ 202 подтверждает только приём: HMAC и одна вставка квитанции в `webhook_events`, без REST-вызовов; проекцию события и `try_enqueue` выполняет `webhook-worker` после ответа ([WEBHOOK_WORKER.md](WEBHOOK_WORKER.md); `try_enqueue` из доставки — #52). `try_enqueue` — одна функция, вызывается из обоих обработчиков и из sweep; условие проверяется по состоянию, а не по тому, какое событие пришло последним. «CI зелёный» (дефолт по #20): все check suites для `head_sha`, **кроме suite самого App** (`app.id`), завершены с `success` / `neutral` / `skipped`, а combined status коммита — `success` или статусов нет; проверяется REST-запросами check-suites и status внутри `try_enqueue`. Свой suite исключён: GitHub создаёт его для App с `checks: write`, а завершает его только наш check-run (§8.3) — иначе условие ждало бы само себя. Если у репозитория нет CI (`wait_for_ci = auto` и ни одного чужого check suite или статуса для `head_sha` за 2 минуты) — прогон стартует по одному лейблу. Это правило реализует sweep раз в 30 с в leader-цикле `worker` (лидер через `pg_advisory_lock`, дефолт по #20): он вызывает тот же `try_enqueue`; реконсилер (раз в 5 мин, §6.4) его не заменяет. После пуша — авто-повтор, пока стоит лейбл `ai-review` (флаг `ai_review_labeled`, Р-10). Полное условие, `wait_for_ci` и жизненный цикл флага — [PIPELINE_SPEC](PIPELINE_SPEC.md) §8.

### 6.2 Прогон: быстрый путь

```mermaid
sequenceDiagram
  autonumber
  participant MQ as RabbitMQ
  participant W as AI Worker
  participant GH as GitHub
  participant R as Redis
  participant LLM as LLM Provider
  participant PG as PostgreSQL
  participant P as Publisher

  MQ->>W: review.run (run_id)
  W->>PG: run.state queued → running (lease, worker_id)
  W->>GH: GET /pulls/{n} + files (patch на файл)
  Note over W: дифф больше 3 000 строк → summary-only до ContextProvider, без L1–L4 (§9)
  W->>GH: GET AGENTS.md (base_sha), дерево репо (head_sha)
  loop файлы по приоритету, пока есть бюджет
    W->>R: blob(repo, sha)? AST(sha)?
    R-->>W: hit / miss
    W->>GH: GET /git/blobs/{sha} (при miss)
    W->>R: put blob, put AST
  end
  W->>PG: context_payloads (summary, полный контекст в MVP не хранится)
  W->>LLM: system(промпт vN + правила + конвенции) + контекст
  LLM-->>W: ReviewOutput (JSON)
  W->>PG: usage_events, run_actions, findings
  W->>PG: cancel_requested? head_sha актуален?
  W->>MQ: review.publish {run_id, findings_hash}
  W->>MQ: ack review.run
  MQ->>P: review.publish
  P->>PG: head_sha == code_changes.head_sha? findings_hash не опубликован?
  P->>GH: POST /pulls/{n}/reviews (одно ревью) + check-run completed
  P->>PG: comments (github ids), run.state → succeeded
  P->>MQ: ack review.publish
```

### 6.3 Схлопывание (Р-2): пуш во время прогона

```mermaid
sequenceDiagram
  autonumber
  participant GH as GitHub
  participant WH as Webhook API
  participant PG as PostgreSQL
  participant WW as webhook-worker
  participant W as AI Worker
  participant P as Publisher

  Note over W: выполняет run#1 (sha_1)
  GH->>WH: pull_request.synchronize (head = sha_2)
  WH->>PG: webhook_events INSERT (квитанция)
  WH-->>GH: 202
  WW->>PG: claim квитанции (цикл раз в 30 с)
  WW->>GH: GET /pulls/{n} под advisory lock — текущий head_sha
  WW->>PG: code_changes.head_sha = sha_2, сброс ci_status
  WW->>PG: runs: run#1.cancel_requested = true
  W->>PG: checkpoint: cancel_requested? → да
  W->>PG: run#1.state → cancelled
  W-->>W: ack review.run (run#1), без публикации
  Note over P: если run#1 уже в review.publish — Publisher сверяет sha_1 ≠ head_sha и не постит
  GH->>WH: check_suite.completed (sha_2, success)
  WH->>PG: webhook_events INSERT (квитанция)
  WH-->>GH: 202
  WW->>PG: try_enqueue → run#2 (sha_2)
  Note over WW: try_enqueue из доставки подключает #52
```

Сообщение run#1 в RabbitMQ удалить нельзя — поэтому решение всегда принимается по состоянию в БД (`RunGuard`), а брокер только доставляет.

### 6.4 Состояния Run

```mermaid
stateDiagram-v2
  [*] --> queued: try_enqueue
  queued --> running: worker claim (lease)
  queued --> cancelled: новый head_sha
  running --> cancelled: cancel_requested на checkpoint
  running --> publishing: findings готовы
  running --> failed: сбой без retry или attempt ≥ 3
  running --> queued: исключение, attempt < 3 (retry с задержкой)
  publishing --> succeeded: ревью опубликовано
  publishing --> cancelled: head_sha устарел
  publishing --> failed: GitHub 5xx после повторов, 403
  queued --> skipped: правило отбора не прошло
  succeeded --> [*]
  failed --> [*]
  cancelled --> [*]
  skipped --> [*]
```

Реконсилер (в `portal-api`, раз в 5 мин, лидер через `pg_advisory_lock`): `running` с истёкшим `lease_until` → `queued` + повторная публикация сообщения (при `attempt ≥ 3` — `failed`, `lease_expired`); `queued` старше 10 мин без сообщения → повторная публикация.

Источник по переходам (триггеры, guard'ы и побочные эффекты T1–T18), lease (5 мин, heartbeat 60 с), таймаутам и retry — [PIPELINE_SPEC](PIPELINE_SPEC.md) §1, §3, §4; при расхождении по жизненному циклу прав он.

### 6.5 Вход и сессия (D4)

```mermaid
sequenceDiagram
  autonumber
  participant B as Web UI
  participant GH as GitHub
  participant A as Auth API
  participant PG as PostgreSQL
  participant P as Portal API

  B->>B: state → sessionStorage
  B->>GH: authorize (client_id App, state), без scopes
  GH-->>B: redirect /auth/callback (code, state)
  B->>B: state совпадает?
  B->>A: POST /api/auth/github/callback {code}
  A->>GH: code → токен пользователя, GET /user, GET /user/installations
  A->>PG: пользователь · refresh-токен · Workspace доступных установок (Р-7)
  A-->>B: {accessToken, expiresIn, user} + Set-Cookie refresh_token
  B->>P: GET /api/runs (Authorization: Bearer)
  P->>P: подпись публичным ключом auth-api, exp, iss/aud, Workspace из claims
  B->>A: POST /api/auth/refresh (cookie) при загрузке и по истечении access
  A-->>B: новый accessToken + ротация cookie
  B->>A: GET /api/auth/me → Me
  B->>A: POST /api/auth/logout → refresh отозван, cookie стёрта
```

Сроки токенов, cookie, claims и граница Workspace — §12; пути и схемы — `contracts/openapi.yaml`.

### 6.6 Чтение в UI и SSE

```mermaid
sequenceDiagram
  autonumber
  participant B as Web UI
  participant P as Portal API
  participant PG as PostgreSQL
  participant W as AI Worker

  P->>PG: LISTEN run_updated
  B->>P: GET /api/stream (fetch, Authorization: Bearer)
  B->>P: GET /api/runs (status, repo, cursor)
  P->>PG: SELECT runs по Workspace из JWT
  P-->>B: RunListPage
  W->>PG: UPDATE runs.state + NOTIFY run_updated (одна транзакция)
  PG-->>P: уведомление после commit
  P-->>B: event run.updated {runId, status}
  B->>P: GET /api/runs/{id} → RunDetail
```

Каждая смена статуса Run шлёт `NOTIFY run_updated` в своей транзакции (D12 — дефолт по #20; payload и отправители — PIPELINE_SPEC §1): уведомление приходит только после commit, и UI не видит незафиксированных состояний. Поток читается через `fetch` с Bearer — `EventSource` заголовки не передаёт. Мост LISTEN/NOTIFY → SSE — #34.

### 6.7 Перезапуск

```mermaid
sequenceDiagram
  autonumber
  participant B as Web UI
  participant P as Portal API
  participant PG as PostgreSQL
  participant MQ as RabbitMQ

  B->>P: POST /api/runs/{id}/rerun
  P->>PG: PR открыт? есть активный Run по PR?
  alt PR закрыт или активный Run есть
    P-->>B: 409
  else
    P->>PG: runs INSERT (queued, trigger = rerun, текущий head_sha) + NOTIFY
    P->>MQ: review.run/v1 → review.run.{engine}, priority 9, после commit
    P-->>B: 202 RunSession (queued)
  end
```

Флаг `ai_review_labeled` и CI не проверяются; дальше — как §6.2 (PIPELINE_SPEC T3).

### 6.8 Retry и DLQ

```mermaid
sequenceDiagram
  autonumber
  participant MQ as RabbitMQ review.run.*
  participant W as AI Worker
  participant PG as PostgreSQL
  participant RQ as reviews.retry
  participant DLQ as reviews.dlq

  MQ->>W: review.run
  W->>PG: claim: attempt + 1, lease 5 мин
  Note over W: сбой, класс — PIPELINE_SPEC §5
  alt класс с retry, attempt < 3
    W->>PG: queued, available_at = now + задержка
    W->>RQ: копия в retry.{задержка}.{engine}
    W->>MQ: ack после confirm копии
    RQ->>MQ: TTL истёк → reviews, ключ review.run.{engine}
  else класс с retry, attempt ≥ 3
    W->>PG: failed, error_code
    W->>DLQ: nack(requeue=false) через reviews.dlx
  else класс без retry
    W->>PG: failed, error_code
    W->>MQ: ack, без DLQ
  end
```

Счётчик один — `runs.attempt` (инкремент при claim), `x-death` — только диагностика. Задержки 30 с → 2 мин (при rate limit — не меньше 2 мин); после 3 попыток — `failed` и копия в `reviews.dlq`; классы без retry (невалидный вывод модели, переполнение контекста, бюджет, дедлайн) ведут в `failed` сразу, без DLQ. Автор PR видит check-run `neutral` «AI-ревью не выполнено», ревью нет. Политика целиком — PIPELINE_SPEC §4–§7.

### 6.9 Подключение репозиториев: установка GitHub App (D10)

```mermaid
sequenceDiagram
  autonumber
  participant B as Web UI
  participant GH as GitHub
  participant WH as Webhook API
  participant PG as PostgreSQL
  participant WW as webhook-worker

  B->>GH: «Подключить» → страница установки App
  GH->>GH: пользователь выбирает аккаунт и репозитории
  GH->>WH: installation.created / installation_repositories.added
  WH->>PG: webhook_events INSERT … ON CONFLICT (delivery_id) DO NOTHING
  WH-->>GH: 202 pending / duplicate
  WW->>PG: claim квитанции (цикл раз в 30 с), установка связана с Workspace?
  alt установка не связана с Workspace
    WW->>PG: квитанция отложена, повтор через 5 мин
  else связана
    WW->>GH: дерево репозитория для языков · REST, вне транзакции
    WW->>GH: POST labels ai-review, 422 — лейбл уже есть · REST, вне транзакции
    WW->>PG: repositories upsert + начальная версия правил, одна транзакция
  end
  GH->>WH: installation_repositories.removed / installation.deleted
  WH->>PG: webhook_events INSERT (квитанция)
  WH-->>GH: 202
  WW->>PG: repositories отключены, без REST
```

Репозиторий подключается только установкой App: `POST /api/repos` нет (D10), кнопка «Подключить» в UI — ссылка на установку App, список обновляют вебхуки (§8.2). Новую установку с Workspace связывает #11; способ — при `installation.created` или при входе через `GET /user/installations` — выбирается в PR по #11. На `main` доставка для установки без Workspace подтверждается ответом 202, а `webhook-worker` откладывает её квитанцию и повторяет разбор раз в 5 мин, пока установку не свяжут с Workspace. При подключении репозитория App создаёт в нём лейбл `ai-review` — триггер Р-10: `webhook-worker` вызывает `POST /repos/{owner}/{repo}/labels` вне транзакции; эндпоинт относится к правам Issues, поэтому у App есть `issues: write` (добавлено 03.10.2026 по #37). Ответ 422 (лейбл уже есть) — успех; другая ошибка логируется и подключение не отменяет [дефолт]. Лейбл, удалённый мейнтейнером, вернётся только при повторном подключении репозитория (OQ-8).

---

## 7. Очередь: RabbitMQ

### 7.1 Топология

| Exchange | Тип | Routing key | Очередь | Потребитель | Свойства |
|---|---|---|---|---|---|
| `reviews` | direct | `review.run.fast` | `review.run.fast` | AI Worker (fast pool) | durable, `x-max-priority=10`, DLX → `reviews.dlx` |
| `reviews` | direct | `review.run.deep` | `review.run.deep` | AI Worker (deep pool) | то же; отдельный пул — чтобы сандбокс не блокировал быстрые |
| `reviews` | direct | `review.publish` | `review.publish` | GitHub Publisher (в MVP — consumer в процессе worker) | durable, DLX |
| `reviews.retry` | direct | `retry.{30s,2m,10m}.{fast,deep}` | с тем же именем | — | `x-message-ttl`, `x-dead-letter-exchange=reviews`, `x-dead-letter-routing-key=review.run.{engine}` (отложенный повтор без плагина; PIPELINE_SPEC §4.3) |
| `reviews.dlx` | fanout | — | `reviews.dlq` | оператор | хранение 7 дней |

Параметры: сообщения `delivery_mode=2`, publisher confirms включены, `prefetch_count=1` на run-очередях (задачи длинные и неравные), ack **только после** фиксации состояния в PostgreSQL, `consumer_timeout=45min` (попытка в `running` — не дольше 28 мин с учётом lease и реконсилера, PIPELINE_SPEC §3). Retry, lease и таймауты — PIPELINE_SPEC §3–§4.

Пока отдельного сервиса `publisher` нет, очередь `review.publish` потребляет отдельный consumer в процессе worker: сообщение `review.publish/v1`, переходы и идемпотентность по `findings_hash` те же (PIPELINE_SPEC §1).

### 7.2 Форматы сообщений

Сообщение — **указатель**, не данные: без диффов, без payload'ов. Всё, что нужно воркеру, он читает из БД и GitHub по идентификаторам. Так сообщение остаётся < 1 КБ, а состояние — единым.

Форма сообщений — JSON Schema, пример — строгая JSON-фикстура; фикстуры и отказ на мутациях проверяет `tests/test_contract_schemas.py`.

| Сообщение | Схема · фикстура | Кто → кому | Поля |
|---|---|---|---|
| `review.run/v1` | [`contracts/schemas/review.run.v1.schema.json`](../contracts/schemas/review.run.v1.schema.json) · [`contracts/examples/review.run.v1.json`](../contracts/examples/review.run.v1.json) | webhook-worker (#52), Portal API (rerun, реконсилер), worker (sweep) → AI Worker | `schema`, `message_id` (= `run_id`, ключ идемпотентности), `run_id`, `workspace_id`, `installation_id`, `repo {id, provider, external_id, full_name}`, `pr {number, head_sha, base_sha, base_ref}`, `engine`, `rule_version_id`, `prompt_version_id`, `trigger`, `attempt`, `requested_at` |
| `review.publish/v1` | [`contracts/schemas/review.publish.v1.schema.json`](../contracts/schemas/review.publish.v1.schema.json) · [`contracts/examples/review.publish.v1.json`](../contracts/examples/review.publish.v1.json) | AI Worker, реконсилер → GitHub Publisher (в MVP — consumer в worker) | `schema`, `message_id`, `run_id`, `head_sha`, `findings_hash` (идемпотентность Р-5), `review_event` |

ID — UUID без префиксов, как в БД; `findings_hash` — 64 hex; приоритет (rerun — 9) — свойство AMQP, а не поле. Отличия от прежних примеров этого раздела — PIPELINE_SPEC §12.

Правила: заголовок `schema` версионируется, потребитель отвергает в DLQ незнакомую мажорную версию и сообщение, не прошедшее схему; `message_id` = детерминированный id из БД, повторная доставка безопасна; попытки считает один счётчик `runs.attempt` (инкремент при claim), `x-death` — только диагностика; после 3 попыток — `run.state = failed` с `error_code` и копия в `reviews.dlq` (PIPELINE_SPEC §4).

### 7.3 Реализация (#34): отступления

- `consumer_timeout=45min` не задаётся аргументом очереди: RabbitMQ 4 отклоняет `x-consumer-timeout` для classic-очереди (`PRECONDITION_FAILED`). Это настройка брокера (`consumer_timeout` в `rabbitmq.conf`, по умолчанию 30 мин). Попытка в `running` занимает не больше 18 мин (PIPELINE_SPEC §3), поэтому дефолта хватает; значение 45 мин задаётся конфигурацией брокера при деплое (#35).
- `budget_paused` (PIPELINE_SPEC §5.3): `workspaces.daily_budget_usd = 0` означает «лимит не задан», проверка остатка идёт только при положительном лимите. Остаток считается по `usage_events.cost_usd` за текущие сутки UTC.
- `rule_not_matched`: правила отбора PR в схеме пока нет. RunGuard спрашивает порт `RunSelectionRule`, текущая реализация пропускает любой PR; условия отбора подключаются к этому порту отдельной задачей.
- Идемпотентность публикации (Р-5): в тело ревью добавляется скрытый маркер `<!-- ai-review findings_hash=... -->`. Перед `POST /pulls/{n}/reviews` publisher ищет ревью с этим маркером и при находке не публикует повторно.
- Check-run называется `AI Review`, ссылка на прогон строится из `PORTAL_URL` (`{PORTAL_URL}/runs/{run_id}`); без `PORTAL_URL` ссылки нет.
- Без `GITHUB_APP_ID` и `GITHUB_APP_PRIVATE_KEY` worker стартует, но diff получить не может: доставленный `review.run/v1` завершает Run как `failed` / `github_forbidden` без retry.
- Rerun (T3) пишет Run с `message_published_at = null` и снимает пометку после confirm, но в replay лидер-цикла worker не попадает: выборка outbox из #11 рассчитана на Run с `trigger = webhook` и лейблом `ai-review`. Неотправленный rerun переотправляет реконсилер (T18, через 10 мин).
- Ответ `review.postprocess` дополнительно хранит `summary` из `ReviewOutput`: `llm.review_output` больше 1 МиБ усекается (D1), а run detail берёт сводку из маленького `review.postprocess`.
- `POST /api/runs/{id}/rerun` отвечает 422, если у репозитория нет активной версии правил или промпта: это не конфликт T3 (409 только для активного Run или закрытого PR).
- `GET /api/runs/{id}` отдаёт `RunDetail`; `author`, `headRef` и `baseRef` в `PullRequestRef` заполняются только в нём, список `GET /api/runs` отдаёт `RunSession` без них. Zod-схема UI допускает оба ответа: с ui#59 (`4369e02`) эти поля `nullable().optional()`; снимок `tests/fixtures/ui_zod_contracts.json` ещё снят с ui `68c85e0` (до ui#59), его перегенерация — PIPELINE_SPEC §16.
- До подключения LLM Gateway (#33) worker собирает заглушку `ReviewModel`: вызов модели завершается `llm_unavailable`, Run проходит retry и уходит в `failed`.
- Installation-токены кэшируются в памяти процесса (`InMemoryInstallationAccessTokenCache`: `app/webhook_worker.py:97`, `app/worker.py:368`): каждый процесс выпускает свой токен и держит его до `expires_at` из ответа GitHub минус 60 с. Цель — Redis `token:{installation_id}` (§8.3, §10); перенос в Redis — бэклог, #62.

---

## 8. Взаимодействие с VCS

### 8.1 Порт `VcsProvider` (Р-11)

```python
class VcsProvider(Protocol):
    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> WebhookEvent: ...
    async def get_pull_request(self, repo: RepoRef, number: int) -> PullRequest: ...
    async def get_diff(self, repo: RepoRef, number: int) -> list[RawFilePatch]: ...   # unified diff на файл
    async def get_blob(self, repo: RepoRef, blob_sha: str) -> bytes: ...
    async def get_tree(self, repo: RepoRef, ref: str) -> list[TreeEntry]: ...
    async def get_ci_status(self, repo: RepoRef, sha: str) -> CiStatus: ...
    async def upsert_check_run(self, repo: RepoRef, sha: str, status: CheckRun) -> str: ...
    async def post_review(self, repo: RepoRef, number: int, review: ReviewPayload) -> str: ...
    async def list_feedback(self, repo: RepoRef, number: int) -> list[FeedbackSignal]: ...   # после MVP
```

v1 — `GitHubProvider`. `GitLabProvider` (MR `changes`, `discussions`, `pipeline` events, bot-user token) и `BitbucketProvider` — по портy, без реализации.

### 8.2 GitHub: события и права

| Событие | Действия | Что делаем |
|---|---|---|
| `pull_request` | `opened`, `reopened`, `synchronize`, `edited` | upsert `code_changes`; `synchronize` → сброс `ci_status`, `cancel_requested` активного Run |
| `pull_request` | `labeled` / `unlabeled` | `ai_review_labeled = true/false`, только если `label.name == "ai-review"`; события самого бота отбрасывает Р-9 (§8.3); на `reopened` флаг берётся из `pull_request.labels` (PIPELINE_SPEC §8.2) |
| `pull_request` | `closed` | отмена активного Run |
| `check_suite`, `workflow_run` | `completed` | `ci_status[head_sha]` (кэш) → `try_enqueue`; «зелёный» = все suites для sha, **кроме suite самого App**, завершены с `success` / `neutral` / `skipped` — проверяется REST-запросом в `try_enqueue` (§6.1) |
| `status` | — | для репозиториев со сторонним CI через commit status; combined status `success` или пусто — часть условия «CI зелёный» |
| `pull_request_review_thread` | `resolved`, `unresolved` | **после MVP**: `feedback_signals` |
| `installation`, `installation_repositories` | `created`, `deleted`, `added`, `removed` | синхронизация `repositories`; при подключении — создание лейбла `ai-review` (§6.9) |

Права App (repository): `pull_requests: write`, `checks: write`, `contents: read`, `metadata: read`, `issues: write` (создание лейбла `ai-review`, §6.9), `actions: read` (событие `workflow_run`), `statuses: read` (событие `status` и combined status в PIPELINE_SPEC §8.1). Бот **не** имеет `contents: write`. Прав организации и аккаунта нет. События `labeled` / `unlabeled` приходят в подписке `pull_request`, поэтому триггер Р-10 новых прав не требует; лейбл `ai-review` App создаёт сам при подключении репозитория (§6.9), `POST /repos/{owner}/{repo}/labels` требует `issues: write`, а не `pull_requests: write` ([GitHub Docs](https://docs.github.com/en/rest/authentication/permissions-required-for-github-apps), OQ-8). Регистрация App и полный список настроек — #37.

### 8.3 Правила работы с API

| Правило | Как |
|---|---|
| Ответ на вебхук < 500 мс | Проверка HMAC (`X-Hub-Signature-256`, `hmac.compare_digest`), заголовков и JSON, одна вставка квитанции в `webhook_events` — всё; никакой работы в обработчике: REST-вызовы и проекция — в `webhook-worker` после ответа (§6.1) |
| Идемпотентность | `webhook_events.delivery_id UNIQUE` (`X-GitHub-Delivery`); GitHub **не** ретраит доставки сам — приёмник обязан быть доступен |
| Игнор собственных событий (Р-9) | Маршрут по `sender` не фильтрует: квитанция сохраняется, ответ — 202. `webhook-worker` пропускает событие лейбла `ai-review` (`labeled` / `unlabeled`), если `sender.type == "Bot"` ∧ `sender.login` совпадает с `GITHUB_APP_BOT_LOGIN` без учёта регистра, и помечает квитанцию разобранной (`projected_at`). В событиях бота `sender` — пользователь `<slug>[bot]`, и его id не равен App ID (staging: App ID `5111033`, `dmc268-t6-reviewer[bot]` — `335108304`, #37). App ID остаётся в `iss` App JWT и в исключении своего check suite по `app.id` (PIPELINE_SPEC §8.1) |
| Installation-токен | живёт 1 ч; Redis `token:{installation_id}`, TTL 50 мин (сейчас — в памяти процесса, отступление §7.3); private key App — только в env `webhook-worker`/`worker`/`publisher` |
| Rate limit | 5000 req/ч на installation; `X-RateLimit-Remaining` в метрики; `403/429` + `Retry-After` — повторы по PIPELINE_SPEC §5.2; вторичные лимиты — не более 1 мутации/сек |
| Дифф | `GET /pulls/{n}/files` (patch на файл, ≤ 3000 файлов, patch пустой у бинарных и > 20 000 строк → файл помечается `too_large`) |
| Файлы | `GET /git/blobs/{sha}` по sha из `files[].sha` — кэшируется на 7 дней (содержимое неизменяемо по sha, §10); никогда `contents` по пути с ref |
| Дерево | `GET /git/trees/{head_sha}?recursive=1` (лимит 100 000 записей → для монорепо только затронутые директории) |
| Публикация (Р-5) | один `POST /pulls/{n}/reviews`: `commit_id = head_sha`, `event`, `body`, `comments[{path, line, side: "RIGHT", start_line?, body}]`; ≤ 10 inline по умолчанию, остальное — в `body`; строка вне диффа → в `body` (иначе 422); `suggestion` только если строка в диффе |
| Check-run | `in_progress` при первом claim, `completed` с `conclusion: neutral` + summary для `succeeded` и `failed`; у `cancelled` и `skipped` — свои conclusion (PIPELINE_SPEC §7); `failure` никогда — бот не блокирует merge (настройка репозитория может изменить) |
| Обратная связь | **после MVP** (`FeedbackSignal`): `pull_request_review_thread.resolved` — вебхук; реакции — poll `GET /pulls/comments/{id}/reactions` раз в час по комментариям бота за 7 дней |

---

## 9. Сборщик контекста: 4 уровня

Цель: дать модели ровно столько, чтобы не галлюцинировать про код вне диффа (TC-06 в тест-плане), и не больше бюджета. Уровни **накапливаются**: файл получает L1 всегда (кроме раннего пути `summary-only` для диффа > 3 000 строк — см. L1), дальше — по приоритету и бюджету. Для каждого файла в `context_payloads` записывается `level_used` — инспектор показывает, что модель видела.

### Граница MVP и будущего RAG

Worker зависит от порта `ContextProvider`, который по PR и снимку `head_sha` возвращает неизменяемый `ContextPayload`. В **MVP** его единственная реализация — `DeterministicContextProvider`: описанные ниже L0–L4, фильтры файлов и `BudgetAllocator`. Это не «весь репозиторий в prompt»: source-файлы отбираются по приоритету, размеру и токен-бюджету; generated/binary/too-large файлы исключаются.

**RAG не реализуется в MVP:** нет embedding-модели, vector DB, фоновой индексации всего репозитория и отдельного ingestion worker. После MVP `RagContextProvider` сможет добавить кандидаты контекста **после Diff/AST-анализа и до `BudgetAllocator`**. Кандидаты проходят те же allowlist путей, лимиты размера и токенов, записываются в trace с причиной выбора и в итоге дают тот же `ContextPayload`. Поэтому LLM Gateway, постобработка, хранение результатов и Publisher от способа retrieval не зависят.

### L0 — метаданные (всегда)

```python
class PrMeta(BaseModel):
    title: str; body: str | None; author: str; branch: str; base_ref: str
    labels: list[str]; files_changed: int; additions: int; deletions: int
    is_draft: bool; is_fork: bool

class RepoConventions(BaseModel):         # шаг «конвенции репозитория» (роль 7)
    agents_md: str | None                 # AGENTS.md проверяемого репо, ≤ 8k токенов
    key_patterns: list[str]               # выведены LLM один раз на base_sha, кэш
    recommendations: list[str]
    languages: dict[str, int]             # {"python": 62, "typescript": 38} — % по дереву
```

### L1 — Diff (всегда, все файлы)

**Исключение — дифф > 3 000 строк.** «Всегда, все файлы» действует до этого порога. Больше — ранний путь `summary-only` **до** `ContextProvider`: L1–L4 не собираются, построчного ревью и inline-комментариев нет, публикуется одна сводка по списку файлов (§13); снимок диффа хранит только список файлов (Р-15): `GET /api/runs/{id}/diff` отдаёт `[{ filename, patch: null }]`, `RunSession` несёт `summaryOnly: true`, UI показывает список файлов и «дифф слишком большой».

Модели ниже — формат для LLM. В UI по проводу уходит `RawFileDiff` `{ filename, patch }` (§12, `patch` = `raw_patch`); Zod `FileDiff`/`DiffLine` фронтенда (роль 5) — клиентская модель, которую UI строит из `patch` библиотекой за адаптером.

```python
class DiffLine(BaseModel):
    type: Literal["context", "added", "removed"]
    old_line: int | None; new_line: int | None; content: str

class Hunk(BaseModel):
    header: str                           # "@@ -12,7 +12,9 @@ def foo"
    old_start: int; old_lines: int; new_start: int; new_lines: int
    lines: list[DiffLine]

class FileDiff(BaseModel):
    path: str; old_path: str | None
    status: Literal["added", "modified", "removed", "renamed"]
    language: str | None                  # по расширению
    blob_sha: str | None                  # новая версия (None для removed)
    hunks: list[Hunk]
    raw_patch: str                        # unified diff файла — из него же собирается `patch` в API (§12)
    is_binary: bool; is_generated: bool; is_too_large: bool
    tokens_est: int
```

`is_generated`: lock-файлы, `dist/`, `*.min.js`, `*.pb.go`, `__snapshots__`, миграции-снапшоты, локали — **исключаются до вызова модели** (TC-07). Пользовательские правила добавляют свои глобы.

### L2 — Surrounding (по умолчанию для всех source-файлов)

```python
class LineRange(BaseModel):
    start: int; end: int                  # строки новой версии
    lines: list[str]
    reason: Literal["hunk_window", "enclosing_symbol"]

class Surrounding(BaseModel):
    path: str
    ranges: list[LineRange]               # окна ±N вокруг ханков, пересечения слиты
    window: int                           # N, по умолчанию 30
```

Если для языка есть AST (L4) — окно расширяется до **границ объемлющего символа** (функция/класс/метод), а не по числу строк: модель видит функцию целиком.

### L3 — Whole File (по бюджету, топ-K файлов)

```python
class WholeFile(BaseModel):
    path: str; blob_sha: str; language: str | None
    content: str; loc: int; tokens_est: int
    truncated: bool                       # > лимита → усечено по границам символов, не по строкам
```

Условия: файл — source, `loc ≤ 1500` и `tokens_est ≤ 12 000`; иначе L2 с `window = 80`. Тесты и конфиги — L3 только если это единственные изменённые файлы.

### L4 — AST / Imports (source-файлы языков с парсером)

Парсер: **tree-sitter** (`tree-sitter-python`, `tree-sitter-typescript`); остальные языки — только L1–L3. Индексируем не весь репозиторий, а **изменённые файлы + файлы, откуда импортированы затронутые символы** (одна степень). Дерево репозитория для резолва импортов (`resolved_path`) берётся на `head_sha`, а не на `base_sha`: иначе файлы, добавленные или перемещённые в PR, не резолвятся.

```python
class ImportRef(BaseModel):
    module: str; names: list[str]
    resolved_path: str | None             # по дереву репо; None → внешняя зависимость
    is_external: bool

class Symbol(BaseModel):
    name: str; kind: Literal["function", "method", "class", "variable", "type"]
    path: str; start_line: int; end_line: int
    signature: str                        # "def foo(a: int, *, b: str = '') -> Result"
    docstring: str | None

class SymbolContext(BaseModel):
    path: str
    imports: list[ImportRef]
    changed_symbols: list[Symbol]         # символы, чьи диапазоны пересекают ханки
    referenced_symbols: list[Symbol]      # определения того, что вызывается из изменённого кода (из других файлов)
    exported_symbols: list[str]           # что этот файл отдаёт наружу — для оценки радиуса поражения
```

Именно `referenced_symbols` закрывает TC-06: метод родительского класса вне диффа попадает в контекст сигнатурой и докстрингой, а не полным файлом.

### Сборка и бюджет

```python
class FileContext(BaseModel):
    diff: FileDiff
    surrounding: Surrounding | None
    whole_file: WholeFile | None
    symbols: SymbolContext | None
    level_used: Literal[1, 2, 3, 4]
    priority: float                       # для инспектора: почему этот файл получил больше

class ContextPayload(BaseModel):          # сущность роли 6
    run_id: str
    pr: PrMeta
    conventions: RepoConventions
    files: list[FileContext]
    omitted_files: list[str]              # не влезли в бюджет — перечислены модели явно
    budget: dict                          # {"limit": 60000, "used": 48210, "engine": "fast"}
```

Алгоритм `BudgetAllocator` (детерминированный):

1. Приоритет файла = `source(1.0) | test(0.6) | config(0.4)` × `log(изменённых строк + 1)` × `1.5 если путь попал под пользовательское правило`.
2. L1 всем (кроме generated/binary/too_large). Если L1 сам не влезает — младшие по приоритету файлы уходят в `omitted_files`, модели сообщается их список.
3. L4 `changed_symbols` + `referenced_symbols` всем source-файлам с парсером (дёшево: сигнатуры).
4. L2 всем source-файлам по приоритету.
5. L3 — сверху вниз по приоритету, пока `used ≤ limit`.
6. `level_used` фиксируется; `ContextPayload` → PostgreSQL (summary без содержимого); полный payload в MVP не хранится (S3 — после MVP, §10).

Глубокий путь (фаза 3) отличается только тем, что шаги 3–5 выполняет агент в сандбоксе инструментами `read_file` / `grep` / `list_symbols` по клону, а не воркер по API; контракт `ContextPayload` тот же. Он также не требует RAG.

---

## 10. Стратегия кэширования контекстов

| Что | Ключ | Где | TTL / инвалидация | Зачем |
|---|---|---|---|---|
| Installation-токен | `installation_id` | Redis (сейчас — в памяти процесса, отступление §7.3) | 50 мин | лимит 1 ч у GitHub |
| Блоб файла | `(repo_id, blob_sha)` | Redis ≤ 256 КБ, иначе PostgreSQL (`cached_file_blobs`) | 7 дней; содержимое неизменяемо по sha | один и тот же файл в серии пушей |
| AST / `SymbolContext` файла | `(blob_sha, parser_version)` | Redis | 7 дней | парсинг дороже сети |
| Дерево репозитория | `(repo_id, head_sha)` | Redis | 1 ч | резолв импортов |
| `RepoConventions` | `(repo_id, sha AGENTS.md, prompt_version)` | PostgreSQL + Redis | пока не изменился AGENTS.md в default-ветке (вебхук `push`) | это LLM-вызов, самый дорогой кэш |
| Снимок диффа PR (Р-15) | `(code_change, head_sha)` | PostgreSQL: патчи по файлам; дифф > 3 000 строк — только список файлов (`patch: null`) | до удаления Workspace (каскад) | `GET /api/runs/{id}/diff`, dry-run, повтор, инспектор |
| `ContextPayload` | `run_id` | PG: только summary (полный — после MVP, S3) | до удаления Workspace | инспектор, отладка |
| Результат прогона | `(repo_id, pr, diff_hash, rule_version, prompt_version)` | PostgreSQL | — | совпал → прогон не запускается, переиспользуем |
| Промпт у провайдера | стабильный префикс: system + правила + конвенции **в начале** промпта, дифф — в конце | prompt caching провайдера | 5 мин – 1 ч | ~70 % входа PR в серии пушей — общий префикс |

Что **не** кэшируем: код клиента дольше 7 дней (снимки диффов и `ContextPayload` — хранение прогона, а не кэш: до удаления Workspace); ответы LLM для разных `head_sha`; ничего в сандбоксе.

Тела ответов инструментов больше 64 КБ лежат в отдельной таблице PostgreSQL, на строку которой указывает `RunAction.response_ref` (PIPELINE_SPEC §2; таблица и миграция — #34); payload вебхуков — JSONB в `webhook_events` вместо `payload_s3_ref` (миграция — #11). Это строки PostgreSQL: они хранятся до удаления Workspace и удаляются каскадом вместе со снимками диффов, summary `ContextPayload`, `RunAction` и `WebhookEvent`.

**Объектное хранилище (S3) — после MVP** (D1 по #20): MinIO community архивирован, его образы удалены с Docker Hub 11.09.2026. Когда S3 вернётся (полные `ContextPayload`, крупные тела), use case удаления Workspace должен удалять его объекты по ссылкам до каскада — S3 каскадов не знает.

---

## 11. Данные (согласование с ERD роли 6)

Сущности — из `BACKEND_ARCHITECTURE.md`; здесь только поля, которые требует этот дизайн.

| Сущность (роль 6) | Требуемые поля |
|---|---|
| `Workspace` | арендатор (Р-7); дневной бюджет |
| `ProviderInstallation` | `provider`, `external_id`, `metadata` (JSON); токены App в БД не сохраняются (installation-токен — только кэш Redis, §8.3; сейчас — в памяти процесса, отступление §7.3); шифрование в MVP не заявлено — OQ-7 |
| `Repository` | `enabled`, `default_engine`, `wait_for_ci: auto\|always\|never`, `review_event`, `max_comments` |
| `CodeChange` (PR) | `number`, `head_sha`, `base_sha`, `ai_review_labeled` (стоит лейбл `ai-review`, Р-10), `ci_status` (jsonb по sha), `state` |
| `Run` | `head_sha`, `state` (§6.4), `engine`, `rule_version_id`, `prompt_version_id`, `attempt`, `available_at`, `lease_until`, `cancel_requested`, `worker_id`, `trigger`, `error_code`, `error_message` (каталог — PIPELINE_SPEC §6); `summaryOnly` в API не хранится, а выводится из снимка диффа (Р-15: только список файлов) |
| `ContextPayload` | §9; summary в jsonb; полный payload в MVP не хранится, `s3_ref` — после MVP (§10) |
| `Finding` | поля `ReviewFinding` (§5); якорь канона `path` / `start_line` / `line` хранится как `file_path`, `line_start`, `line_end`, `side` (`RIGHT` ставит постпроцессор) — маппинг PIPELINE_SPEC §10; + `run_id`, `published: bool`, `inline_comment`, `drop_reason`; SHA не хранится — берётся из `Run.head_sha` |
| `Comment` | `finding_id`, `github_review_id`, `github_comment_id`, `findings_hash` |
| `CreditLedger` | потребитель `usage_events`, не источник |
| **Добавить:** `WebhookEvent` | `delivery_id UNIQUE`, `event`, `action`, `payload` (JSONB в PostgreSQL; до миграции #11 — колонка `payload_s3_ref`), `received_at` |
| **Добавить:** `RuleVersion`, `PromptVersion` | неизменяемые (Р-6) |
| **Добавить:** `UsageEvent` | `run_id`, `model`, `tokens_in/out`, `cache_read_tokens`, `cost_usd` — только вставка (Р-8) |
| **Добавить:** `RunAction` | `run_id`, `index`, `tool` (каталог — PIPELINE_SPEC §2), `request jsonb`, `response jsonb?` (≤ 64 КБ) или `response_ref` (> 64 КБ — id строки отдельной таблицы PG, PIPELINE_SPEC §2), `started_at`, `duration_ms` — Zod `RunAction` фронта |
| **Добавить:** снимок диффа (Р-15) | ключ `(code_change, head_sha)`; по файлу — `filename`, `patch` (дифф > 3 000 строк — только `filename`, в API `patch: null`); каскад от Workspace |
| **После MVP:** `FeedbackSignal` | `finding_id`, `kind: resolved\|line_changed\|reaction`, `value`, `at` |

В MVP всё хранится в PostgreSQL (D1): payload вебхуков — JSONB; полный `ContextPayload` не хранится, только summary; ответы инструментов до 64 КБ — в `run_actions.response`, больше — в отдельной таблице по `response_ref`. Данные хранятся до удаления Workspace и удаляются вместе с ним (§10).

---

## 12. Контракт API ↔ UI

Zod-схемы фронта (роль 5) и бэкенд описывают одни и те же DTO. `RunSession` — API-представление сущности `Run` (§11; в задании — `ReviewJob`): одна сущность, не отдельная таблица.

**Источник истины — [`contracts/openapi.yaml`](../contracts/openapi.yaml)** (OpenAPI 3.1): пути, параметры, схемы, коды ответа и ошибки. `x-service` на операции называет сервис, `x-status: planned` + `x-issue` помечают ещё не реализованное. Совпадение с FastAPI и ответами проверяет `tests/test_openapi_contract.py`, с Zod — `tests/test_ui_zod_contracts.py`. Ниже — только сводка.

Провод — **camelCase** (Ф-10): `defaultEngine`, `waitForCi`, `maxComments`, `reviewEvent`; snake_case — только в БД (и во внутренних сообщениях очереди, §7.2). Префикс `/api` без версии.

| Группа | Операции | Сервис | Главное |
|---|---|---|---|
| Прогоны | `GET /api/runs`, `GET /api/runs/{id}`, `…/diff`, `…/files`, `…/comments`, `…/actions`, `…/actions/{index}/response` | `portal-api` | список — `RunListPage {items, nextCursor}`, фильтр `status` — семь состояний Р-14; run detail — `RunDetail` = `RunSession` + `verdict`, `summary`, `severityCounts`, `findings` (с `suggestion`, `confidence`), `budget` (D3, PIPELINE_SPEC §11); `/diff` — сырой unified-diff на файл из снимка Р-15, summary-only → `patch: null`; `/files` старше TTL кэша блобов (§10) → `404`/`410`; тело действия > 64 КБ — через `…/actions/{index}/response` |
| Действия с прогоном | `POST /api/runs/{id}/rerun`, `POST /api/runs/{id}/cancel` | `portal-api` | rerun — новый Run `queued`, `202`; PR закрыт или есть активный Run → `409` (§6.7); cancel идемпотентен |
| Поток | `GET /api/stream` | `portal-api` | SSE `run.updated {runId, status}`, один поток на вкладку, `fetch` с Bearer; между процессами — PG `LISTEN/NOTIFY` (D12 — дефолт по #20, §6.6) |
| Репозитории | `GET /api/repos`, `GET/PATCH /api/repos/{id}`, `GET /api/repos/{id}/pulls` | `portal-api` | подключение — установкой App (§6.9): `POST /api/repos` и PATCH коллекции нет (D10); `GET /api/repos` → `Repository[]`; PATCH — `enabled`, `defaultEngine`, `waitForCi` (`auto\|always\|never`), `maxComments` (1..10), `reviewEvent` (`COMMENT\|REQUEST_CHANGES`); pulls → `{items: PullRequestSummary[], nextCursor}` с `latestRun {id, status, verdict}` (D11 — дефолт по #20) |
| Правила, метрики | `GET/POST /api/repos/{id}/rules`, `POST /api/rules/{id}/preview`, `GET /api/metrics/summary` | `portal-api` | после MVP (`x-note` в спеке) |
| Вход и сессия | `POST /api/auth/github/callback`, `POST /api/auth/refresh`, `GET /api/auth/me`, `POST /api/auth/logout` | `auth-api` | ниже; поток — §6.5 |

**Контракт авторизации** (D4 — решение техлида по #20; хранение access в памяти, подпись в `auth-api` с проверкой публичным ключом и граница Workspace — дефолт по #20):

- **Вход** — GitHub App user authorization, без OAuth scopes. `state` генерирует SPA и хранит в `sessionStorage`; GitHub возвращает на SPA `/auth/callback`, SPA сверяет `state` и отправляет `POST /api/auth/github/callback {code}` → `AuthSession {accessToken, tokenType: "Bearer", expiresIn, user}` + cookie refresh.
- **Access** — JWT на 15 мин в заголовке `Authorization: Bearer` на всех `/api/*`, кроме callback, refresh и logout; SPA держит его в памяти, не в `localStorage`.
- **Refresh** — ротируемый, 30 дней, cookie `refresh_token`: `HttpOnly; Secure; SameSite=Strict; Path=/api/auth`. `POST /api/auth/refresh` выдаёт новый access и новую cookie, `POST /api/auth/logout` отзывает refresh и стирает cookie. При загрузке SPA восстанавливает сессию: refresh, затем `GET /api/auth/me` (`Me` = `User` + `workspaces [{id, name, installationId}]`).
- **Claims** — `sub` (пользователь), id доступных Workspace, `iat` / `exp`, `iss` / `aud`. Подписывает `auth-api`; `portal-api` проверяет подпись публичным ключом `auth-api`, `exp`, `iss` и `aud`.
- **Граница Workspace (Р-7)** — `auth-api` при входе получает установки пользователя (`GET /user/installations`) и кладёт в токен только их Workspace; `portal-api` фильтрует каждый `/api/*` по этому claim. Пустой список Workspace — валидный claim (решение техлида 28.09.2026): `auth-api` выдаёт токен с `workspaces: []`, `portal-api` отвечает пустыми списками, UI показывает «Подключить»; 401 — только если claim Workspace нет или он битый. Своей таблицы ролей нет.
- **SSE** — `fetch`-стрим с тем же Bearer (`EventSource` заголовки не передаёт).

---

## 13. Нефункциональные требования

| Область | Требование | Источник |
|---|---|---|
| Ack вебхука | p95 < 500 мс (внутренняя цель 200 мс) | TEST_PLAN 2.2 |
| DiffEngine | p95 ≤ 40 с чистого времени движка | TEST_PLAN QG |
| Время до ревью (fast) | p50 ≤ 90 с, p95 ≤ 4 мин от выполнения триггера (очередь + движок + публикация) | этот документ |
| SandboxEngine | жёсткий таймаут 10 мин, затем тот же Run продолжается как fast (действие `engine.fallback` в трейсе) — это не `failed` | Р-3, PIPELINE_SPEC §5.3 |
| Лимиты контекста | fast: 60 000 входных токенов; deep: 150 000; один файл L3 ≤ 12 000; ≤ 50 файлов с контекстом, остальные в `omitted_files`; дифф > 3 000 строк → ранний путь `summary-only` до `ContextProvider`: без построчного ревью, одна сводка «PR слишком большой» по списку файлов | §9 |
| Выход | ≤ 10 inline-комментариев (настройка репозитория), тело ревью ≤ 4 000 символов, ответ модели — `ReviewOutput` по `review/schemas/review-output.schema.json` (§5) | Р-5 |
| Пропускная способность v1 | 100 PR/день, 5 параллельных прогонов; масштаб — реплики `worker` | |
| Надёжность | ни одна задача не теряется: persistent-сообщения + состояние в PG + реконсилер 5 мин; приёмник вебхуков — отдельный процесс (GitHub не ретраит) | |
| Схлопывание | 100 % устаревших прогонов отменены до публикации | TEST_PLAN QG |
| Стоимость | лимит на прогон: fast $0.50, deep $3 — сумма `usage_events` за все попытки плюс оценка следующего вызова, проверка до каждого вызова; превышение: deep — прервать и опубликовать, что успели, fast — `failed` (`budget_exceeded`); дневной бюджет на Workspace → деградация в fast, затем пауза (`skipped`). Лимиты вызовов, токенов и стоимости применяются по PIPELINE_SPEC §4.5, §5 | Р-8, PIPELINE_SPEC |
| Качество | Precision ≥ 85 %, Critical Recall ≥ 75 %, Hallucination < 3 % на golden dataset | TEST_PLAN QG |
| Безопасность | HMAC на вебхуках; секреты только через env из CI (роль 3); токены GitHub и LLM не попадают в сандбокс; сандбокс `--network=none`, non-root, read-only rootfs; PG/RabbitMQ/Redis — только внутренняя сеть | TEST_PLAN 2.2, 2.4, 2.6 |
| Данные клиента | блобы ≤ 7 дней в кэше; диффы, контексты, тела ответов инструментов и payload вебхуков — до удаления Workspace; ни один прогон не логирует содержимое файлов в stdout | |
| Наблюдаемость | структурированные логи (JSON) с `run_id` во всех контейнерах; метрики: глубина очередей, длительность по этапам, `X-RateLimit-Remaining`, стоимость; self-hosted стек — отдельная задача | |

**Риск схлопывания** (решение техлида 04.10.2026, #56): цель «100 % устаревших прогонов отменены до публикации» остаётся, но пока не выполняется гарантированно. Publisher считает head актуальным, если `head_sha` прогона совпадает с `code_changes.head_sha` в PostgreSQL (`app/modules/reviews/application/publish_run_review.py:161-165`, `head_current`). Новый head попадает в PostgreSQL только через `webhook-worker` (§6.3): до одного цикла разбора квитанций (~30 с при пустой очереди, `app/webhook_worker.py:101-103`) плюс обработка после ответа 202. В этом окне ревью старого sha ещё может быть опубликовано. Закрыть окно должна #52.

---

## 14. Развёртывание v1

```mermaid
flowchart TB
  subgraph vps["staging · курсовой VPS · Docker Compose"]
    caddy[caddy · TLS · сеть dmc268-edge] --> portal[portal-api]
    caddy --> auth[auth-api]
    caddy --> hook[webhook-api]
    caddy --> ui[Web UI · nginx :8080]
    portal --> pg[(postgres 17)]
    hook --> pg
    ww[webhook-worker] --> pg
    ww -.->|"review.run (#52)"| mq[(rabbitmq)]
    portal --> mq
    worker[worker] --> mq
    publisher[publisher] --> mq
    worker --> pg
    publisher --> pg
    worker --> redis[(redis)]
    portal --> redis
    publisher --> redis
    auth --> pg
    migrator[migrator · one-shot] -.->|Alembic| pg
  end
  gh[GitHub] -->|webhooks| caddy
  op[Оператор · браузер] -->|HTTPS| caddy
  auth -->|"user authorization,<br/>/user/installations"| gh
  ww -->|REST| gh
  worker -->|REST| gh
  publisher -->|REST| gh
  worker -->|HTTPS| llm[LLM Provider]
```

Внешние порты — только у edge-прокси Caddy: 443 (80 — редирект на HTTPS). HTTP-сервисы и Web UI подключаются к docker-сети `dmc268-edge` по alias, без host-портов; Web UI — nginx-контейнер из репозитория UI (alias `ui-staging`, порт 8080). Маршруты edge-прокси текущего staging и альтернативная цель выката (Hetzner VM из Terraform роли 3, без edge-прокси) — [CICD.md](CICD.md) §8. Диаграмма — целевая раскладка Р-12, объектного хранилища в ней нет (§10). До разделения на сервисы (#16; скорее всего после этого спринта, отдельным рефакторингом) маршруты `portal-api`, `auth-api` и `webhook-api` живут в одном приложении `api`, а `worker` — отдельный процесс из того же образа (§4); сегодня на staging (`deploy/compose/staging.yml`) работают `api`, разовый `bootstrap` (роль `migrator`), `postgres` 17, `rabbitmq` 4 и `redis` 8 — последние три только во внутренней сети, без `ports:`; `worker` и `webhook-worker` (разбор квитанций вебхуков) добавляются следующим PR по #35. Edge-прокси знает только upstream `api-staging`: туда идут хост API и `/api/*` хоста UI, включая `/api/auth/*`. Маршрут на `auth-api` и переименование `api` → `portal-api` добавляются в CICD.md §8 вместе с разделением. Сандбокс (фаза 3) — отдельная VM с Docker-сокетом, недоступным из `portal-api`, `auth-api` и `webhook-api`.

**Один origin** (решение техлида по #20, 28.09): браузер обращается к `/api/*` на хосте UI — edge-прокси или nginx UI проксирует эти пути в API (для SSE без буферизации), поэтому CORS не нужен, а refresh-cookie `Path=/api/auth` (§12) работает в пределах одного origin. Хост API остаётся для healthcheck и вебхуков GitHub. Настроено в #35 маршрутом `handle /api/*` edge-прокси (`deploy/edge/Caddyfile`); Caddy отдаёт `text/event-stream` без буферизации.

---

## 15. Открытые вопросы для команды

| # | Вопрос | Предложение | Кто решает |
|---|---|---|---|
| OQ-1 | После первого ревью GitHub снимает бота из requested reviewers. Повторный пуш: ревьюим автоматически или ждём повторного назначения? | **закрыт**, вопрос потерял смысл: бота нельзя запросить ревьюером, триггер — лейбл `ai-review`, бот его не снимает ([#37](https://github.com/larchanka-training/dmc-268-api-t6/issues/37#issuecomment-5874776355), Р-10). После пуша — автоматически, пока PR открыт и лейбл стоит (решение техлида по #20). Определение «CI зелёный» и sweep «2 мин без CI» — дефолт по #20 (§6.1, PIPELINE_SPEC §8) | продукт / мит |
| OQ-2 | Модель для DiffEngine и размер бюджета | **открыт до [#46](https://github.com/larchanka-training/dmc-268-api-t6/issues/46)**: модели выбраны по каталогу EUrouter (#33, 29.09.2026): основная — `gpt-4.1-mini`, fallback — `mistral-small-4`; проверка по требованиям D7 и расчёт — таблица ниже. Strict `json_schema` не подтверждён: единственный прогон workflow `LLM live run` ([run 37153425606](https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/37153425606), 03.10.2026, `main` @ `05d991f`) упал до ответа модели — `mistral-small-4` HTTP 402 «Insufficient balance», `gpt-4.1-mini` HTTP 400 «No providers available»; подтверждение или переназначение моделей — #46. Бюджет — лимиты §13 без изменений; дизайн модель-агностичен: модель меняется конфигурацией шлюза (`LLM_MODEL`, `LLM_FALLBACK_MODEL`) | исполнитель #33; подтверждение — #46 |
| OQ-3 | `review_event` по умолчанию: `COMMENT` или `REQUEST_CHANGES` при critical? | **закрыт** решением по #20: `COMMENT` по умолчанию, поле `reviewEvent` у репозитория; `REQUEST_CHANGES` — только при `reviewEvent = REQUEST_CHANGES` и вердикте `blocking` (PIPELINE_SPEC §11) | продукт |
| OQ-4 | Раскладка `.agents/` vs `docs/agents/` | **закрыт** решением роли 7 в [dmc-268-ui-t6#32](https://github.com/larchanka-training/dmc-268-ui-t6/issues/32): `.agents/` — источник истины, `.claude/{skills,agents}` — симлинки на него | роль 7 + техлид |
| OQ-5 | Event Collector как отдельный процесс — с какого порога | **закрыт Р-12**: отдельного collector нет, `usage_events` пишет worker | техлид |
| OQ-6 | Стековые PR (B на основе A): пуш в A меняет дифф B, событие приходит только по A | не решаем в v1, фиксируем как известный пробел | — |
| OQ-7 | Шифрование `ProviderInstallation` at rest | в MVP не заявлено: токены App не сохраняются, в `metadata` — только JSON-описание установки; вернуться, если в `metadata` появятся секреты | техлид + роль 6 |
| OQ-8 | Кто создаёт лейбл `ai-review` в подключённом репозитории | **закрыт** решением техлида: лейбл создаёт App при подключении репозитория (`installation.created`, `installation_repositories.added`, §6.9), ответ 422 на существующий лейбл — успех. `POST /repos/{owner}/{repo}/labels` требует `issues: write`, а не `pull_requests: write`: право добавлено App 03.10.2026 по #37 (§8.2, [GitHub Docs](https://docs.github.com/en/rest/authentication/permissions-required-for-github-apps)). Ограничение: лейбл, удалённый мейнтейнером, вернётся только при повторном подключении репозитория | техлид; реализация — #11 |

**Закрытие OQ-2: проверка по D7.** Значения — каталог EUrouter (`GET https://api.eurouter.ai/api/v1/models`) на 29.09.2026; цены в USD за 1 млн токенов, они же — дефолты `KNOWN_MODELS` шлюза и переопределяются env без релиза.

| Требование D7 | `gpt-4.1-mini` (основная) | `mistral-small-4` (fallback) |
|---|---|---|
| Strict structured output | `response_format` в `supported_parameters` — параметр принимается. Шлюз шлёт `json_schema` со `strict: true` и схемой `review-output.schema.json` (без `$`-аннотаций); что маршрут EUrouter принимает её целиком, подтверждает workflow `LLM live run` — первый прогон после мержа, ссылка на run добавляется сюда | то же |
| Контекст ≥ 60 000 | 1 047 576 | 262 144 |
| Цена вход / выход / чтение кэша | $0.44 / $1.76 / $0.11 | $0.165 / $0.66 / $0.0165 — маршрут Mistral AI; у EUrouter ещё два эндпоинта: Regolo €0.50 / €2.10, AKI.IO €0.20 / €0.60 |
| Максимум одного вызова fast (52 000 вход + 8 000 выход) | $0.037 | $0.014 (Regolo — около €0.043) |
| Прогон fast ≤ $0.50 | худший случай — 3 попытки × (3 вызова основной + 1 fallback) = $0.37 (вызов конвенций входит в те же 4 вызова попытки); типичный прогон — один вызов ≈ 20 000 / 2 000 токенов ≈ $0.012 | — |

Выбор: основная и fallback — разные вендоры (OpenAI, Mistral), так что сбой одного не выбивает обе; обе с EU-резидентностью данных через EUrouter. `gpt-4.1-mini` — не reasoning-модель; `mistral-small-4` — гибридная, reasoning опционален (`reasoning.mandatory = false`), шлюз его не включает, поэтому ответ укладывается в таймаут 90 с. Даже по самому дорогому эндпоинту fallback худший случай прогона fast остаётся ниже $0.50, а лимит шлюз в любом случае проверяет до каждого вызова по `usage_events` (PIPELINE_SPEC §4.5).

Строгость `json_schema` на маршруте EUrouter подтверждается ручным workflow `LLM live run` (`.github/workflows/llm-live-run.yml`, `workflow_dispatch`, ключ — секрет организации `AI_DMC268_T6`): два прогона — основная с fallback и fallback как основная — и `validate_findings.py` на ответе; итог (провайдер, фактическая модель, вызовы, токены, стоимость, время) — в summary run. `workflow_dispatch` запускается только для файла на `main`, поэтому первый прогон — сразу после мержа #33; ссылка на него и проверка, что `usage.cost` приходит в USD, добавляются в эту строку. Если маршрут отвергнет схему (HTTP 400 на `response_format`), это hotfix по OQ-2: другая пара моделей или tool calling. Известные пробелы: схему конвенций (`RepoConventionsDraft`) workflow не проверяет — её strict-путь подтвердится вместе с подключением шлюза в воркер (#34); `review.conventions.v1` не описывает маркеры `[N more … omitted]`, поэтому при обрезанном дереве или > 100 изменённых путях модель может выдать запись на строку-маркер и уйти в repair — это учесть в следующей версии промпта конвенций.

---

## 16. Проверка DoD

- [x] Компоненты, потоки данных, границы backend / frontend / LLM — §2, §3–§5
- [x] Правила взаимодействия с VCS (GitHub, порт для GitLab) — §8
- [x] Форматы очереди задач (RabbitMQ) — §7
- [x] Стратегия кэширования контекстов — §10
- [x] Сборщик контекста: структуры данных на 4 уровнях — §9
- [x] Диаграммы C4 (контекст, контейнеры, компоненты) и потоков — §3–§6, §14
- [x] Нефункциональные требования: latency, лимиты контекста — §13
- [x] Утверждено командой — PR-ревью (PR #36, #20)
