# TEST_PLAN: стратегия тестирования AI Code Reviewer

Канонический план для API и UI. Основа — [UI TEST_PLAN из PR #58](https://github.com/larchanka-training/dmc-268-ui-t6/pull/58); контракты уточнены по [SYSTEM_DESIGN](SYSTEM_DESIGN.md), [PIPELINE_SPEC](PIPELINE_SPEC.md), [RULES_FORMAT_SPEC](RULES_FORMAT_SPEC.md), [CONTEXT_AND_VERIFICATION_SPEC](CONTEXT_AND_VERIFICATION_SPEC.md) и [OpenAPI](../contracts/openapi.yaml). Этот документ описывает целевое поведение; наличие сценария в плане не означает, что реализация или проверка уже готовы.

**Граница #107.** Требования к снимкам политики, AST, `ReviewOutput v2`, обязательному production verifier, coverage и новой публикации ниже — план будущей реализации. Текущие schema, runtime, OpenAPI и test harness переходят на них отдельными изменениями. Существующий raw-review benchmark (§3.1–3.3) остаётся самостоятельной оценкой исходной модели до фильтров и verifier; его исторические ответы и метрики не получают задним числом evidence или отметку verified. Документационные изменения #107 не означают запуска или прохождения перечисленных проверок.

## 1. Архитектура под тестированием

GitHub webhook → `webhook-api` (HMAC, delivery idempotency) → PostgreSQL `Run` → persistent-сообщение RabbitMQ → `worker` (снимки политики/diff, `DiffEngine` или позднее `SandboxEngine`) → `ReviewOutput v2` → детерминированные фильтры → обязательный verifier непустого набора кандидатов → `FinalReviewResult` и сохранённый payload → `publisher` → один GitHub pull-request review. `auth-api` выдаёт сессию, `portal-api` отдаёт Workspace-ограниченные `/api/*` и принимает rerun. Целевая топология — пять сервисов в одном monorepo (Р-12); до разделения `portal-api`, `auth-api` и `webhook-api` работают в одном приложении `api`, а worker запускается отдельно из того же образа ([SD §4](SYSTEM_DESIGN.md#4-c4--уровень-2-контейнеры)). Источник состояния и идемпотентности — PostgreSQL; RabbitMQ доставляет указатель на Run, не заменяет БД.

Нумерация Р-1…Р-9 сохранена из исходного плана и [SD §1](SYSTEM_DESIGN.md#1-решения):

| Решение | Проверяемый контракт |
| --- | --- |
| Р-1 | RabbitMQ durable/persistent, ack после фиксации состояния; Run и дедупликация в PostgreSQL. |
| Р-2 | Один активный Run на `(installation_id, repo_id, pr_number)`; новый `head_sha` отменяет устаревший Run до публикации. |
| Р-3 | Два движка за единым `ReviewOutput`; v1 — DiffEngine, SandboxEngine — поздняя фаза. |
| Р-4 | SandboxEngine: контроллер снаружи, read-only клон, `--network=none`, без секретов внутри; проверка в фазе 3. |
| Р-5 | Один review batch на run, сохранённые payload/remote IDs; идентичность по run/head/diff/final result и payload операции. Только подтверждённые находки с существующим и полностью показанным диапазоном вне inline-якоря переходят в тело ревью. `findings_hash` — только диагностика. |
| Р-6 | Run фиксирует неизменяемые base-документы политики, дефолты, версии prompts/schema/model/recovery и контекстные manifests; retry использует те же pins. |
| Р-7 | Workspace — граница доступа; права на репозитории основаны на GitHub App installations. |
| Р-8 | `usage_events` записываются для каждого вызова LLM, только вставкой. |
| Р-9 | Бот игнорирует собственные webhook-события и не отвечает на комментарии. |

Дополнительные инварианты: Р-10 — запуск по лейблу `ai-review` и зелёному CI для текущего `head_sha`, независимо от порядка событий; Р-14 — состояния `queued | running | publishing | succeeded | failed | cancelled | skipped`; Р-15 — снимок файлов хранится в PostgreSQL по `(run_id, filename)`. Ignore применяется до порога **более 3 000 разрешённых добавленных и удалённых строк**: исходный доступный `patch` для UI сохраняется, `review_patch=null` для исключённых файлов и summary-only. Ровно 3 000 строк не включают этот режим. Summary-only строится backend по разрешённой статистике без LLM; итог `succeeded`, coverage partial, verdict null. Неполный список VCS даёт summary-only с явной причиной; отсутствие patch не считается нулём изменений. Полностью исключённый или пустой scope даёт `skipped`, verdict null, ноль LLM-вызовов. Р-11 ограничивает первое внедрение GitHub, Р-12 задаёт топологию, Р-13 задаёт RAG v1 как граф символов с указателями, без embeddings и текста кода, и выключенный по умолчанию тумблер ([ADR 0001](adr/0001-graph-rag-v1.md)); сценарии и гейт v2 — §3.5. Сборщик контекста [SD §9](SYSTEM_DESIGN.md#9-сборщик-контекста-4-уровня): L0 метаданные PR/репозитория и раздельные источники политики; L1 unified diff; L2 окружающие строки; L3 целый файл в лимите; L4 AST/символы. Проверять состав, ограничения и ранний summary-only путь отдельно по мере реализации уровней.

Текущий legacy `ReviewOutput` — объект `{findings, summary}`, максимум 10 findings, форма из [JSON Schema](../review/schemas/review-output.schema.json). Целевой `ReviewOutput v2` сохраняет предел и координаты `path/start_line/line` на **новой** стороне diff, добавляет обязательные `evidence_refs` и атрибуцию по [RULES_FORMAT_SPEC §8](RULES_FORMAT_SPEC.md#8-атрибуция-prompt-envelope-и-защита-от-инъекций). `side: RIGHT` задаёт backend, SHA берётся из `Run.head_sha`; схема и prompt обновляются вместе. Однострочная находка: `start_line: null`, API `newLine = line`, `endLine = null`; диапазон: `start_line < line`, API `newLine = start_line`, `endLine = line`. Будущий v3 добавит явный side сквозь API/storage/eval/hash; для LEFT и GitLab предусмотрены отдельные contract fixtures, без заявления о готовой интеграции.

Публикатор передаёт `path`, `line`, `side`, для диапазона оба `start_line/start_side`, `commit_id = Run.head_sha`, максимум 10 inline-комментариев. Только `supported` findings входят в final result; inline cap меняет размещение, но не counts или verdict. Summary строит backend из final findings и coverage, без fallback к модельному summary. Пустой набор или только info даёт `clean` лишь при coverage complete; при partial/none — null. Supported critical/high даёт blocking, medium/low — attention, включая partial coverage. До `succeeded`, а также для skipped/failed/cancelled API verdict null. `REQUEST_CHANGES` используется только при `Repository.reviewEvent = REQUEST_CHANGES` и `verdict = blocking`, иначе `COMMENT` ([PIPELINE_SPEC §11](PIPELINE_SPEC.md#11-вердикт-и-бейджи-d3)).

## 2. Уровни, окружения и архитектурные сценарии

| Уровень | Граница проверки | Где и чем запускать |
| --- | --- | --- |
| Unit/contract | Use case с fake портами; legacy `ReviewOutput`, D6-якоря, вердикт, UI Zod/клиент. Для #107 дополнительно parser/ignore, snapshots, DiffMap, manifests, v2/evidence, verifier decisions и coverage-aware result. | Локально и в PR CI: `uv run pytest`; UI `pnpm test`, `pnpm check-types`. Новые сценарии добавляются вместе с реализацией. |
| Integration | Реальная мигрированная PostgreSQL и RabbitMQ, fake GitHub REST/LLM; API HTTP и async worker. | Локально и в обязательном PR CI: `TEST_DATABASE_URL=... TEST_RABBITMQ_URL=... uv run pytest -m integration -rs` (локальный стенд — ниже; в CI RabbitMQ задаёт `RABBITMQ_URL`); ноль skip для integration. |
| Offline replay | Зафиксированные patch/pre-image/raw responses без сети и ключей; валидация и метрики §3. | api, локально и в обязательном PR CI (job `Python lint / type / test`, шаг «Gold corpus and recorded replay»): `uv run python review/scripts/validate_dataset.py --final`, затем `uv run python review/scripts/eval_replay.py` по закоммиченному baseline `test-prs-dataset/responses/`; CI добавляет обоим `--report-json` и пишет метрики в job summary. Два запуска дают одинаковый JSON. В ui уровня нет. |
| Live eval | Тот же корпус, выбранная модель через PromptBuilder, сохранение raw output и provenance. | api: ручной необязательный `workflow_dispatch` [Live corpus evaluation](../.github/workflows/eval-live.yml): input `model` (пусто — `vars.LLM_MODEL`), fallback `vars.LLM_FALLBACK_MODEL`; на временной копии корпуса запускает `validate_dataset.py --final` и `eval_live.py`, отчёт без raw-текста модели — артефакт на 14 дней, ответы не коммитятся. Новый закоммиченный baseline записывают тем же workflow с boolean input `export_raw_responses` (по умолчанию `false`): отдельный артефакт `eval-live-raw-<run-id>-<attempt>` на 14 дней хранит только ответы кейсов и `manifest.json`; захват сверяют с артефактом безопасных метаданных (`baseline_publishable`, SHA-256 и размер каждого ответа), целиком и без правок копируют в `test-prs-dataset/responses/` и до коммита проверяют offline replay на drift — порядок в README корпуса, разделы [Manual live evaluation in GitHub Actions](../test-prs-dataset/README.md#manual-live-evaluation-in-github-actions) и [Baseline drift](../test-prs-dataset/README.md#baseline-drift). Доступ к LLM — ниже. В ui уровня нет. |
| Pipeline/verifier eval #107 | Полный v2 pipeline, детерминированные drops, решения verifier и final result против независимой ручной разметки; отдельный adversarial corpus. | Будущий harness по §3.4; existing `eval_replay.py`/`eval_live.py` не считаются его реализацией. Сначала recorded/offline fixtures, затем отдельный разрешённый live-прогон с зафиксированными snapshots/model/cost. |
| End-to-end | GitHub App, webhook, очереди, API, UI, публикация и изоляция арендаторов. | api: автоматическая цепочка «webhook → Run → worker → `policy.load` → fake LLM → публикация в fake GitHub» — `tests/test_rules_md_e2e_postgres.py` (24 сценария по конфигурациям `.review/rules.md`, каталог — §2.8) в обязательном job `Python lint / type / test` под маркером `integration`: реальные PostgreSQL и RabbitMQ, GitHub и LLM — fake. HTTP-smoke вебхука (подпись HMAC, дубль delivery; p95 задержки ACK только печатается, порога нет) — обязательный job `Webhook container smoke` в PR CI на собранном образе с PostgreSQL: `uv run --locked python scripts/webhook_smoke.py --url http://127.0.0.1:8000/webhooks/github --count 50`; затем тот же job поднимает заглушку GitHub API `scripts/github_stub.py` и контейнер webhook-worker (`python -m app.webhook_worker`) и ждёт в его логе исход labeled-доставки. Локально `uv run --env-file .env python scripts/webhook_smoke.py` с тем же `GITHUB_WEBHOOK_SECRET`, что у API, и только против одноразового стенда, не staging и не prod. Полный путь, включая UI, — вручную на staging (VPS): тестовые PR и установки staging GitHub App, без реальных секретов в фикстурах или логах; в e2e изоляция арендаторов автоматически не проверяется; в ui автоматического e2e нет. Sandbox isolation — фаза 3. |

API baseline gates локально и в обязательном CI: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy .`, `uv run pytest`. UI gates: `pnpm lint`, `pnpm check-types`, `pnpm format:check`, `pnpm test`, `pnpm build`. Для integration сервисы должны быть доступны; отсутствие `TEST_DATABASE_URL` даёт skip локально, но не считается успехом обязательного integration job. Staging smoke измеряет p95 ACK < 500 мс и p95 чистого времени DiffEngine ≤ 40 с на определённом наборе PR; окно, объём выборки и измерение записываются с результатом.

**Локальный стенд.** PostgreSQL и RabbitMQ для integration берутся из `docker-compose.yml`; рецепт совпадает с [README, Tests](../README.md#tests): `docker compose up -d --wait postgres rabbitmq`, затем отдельный vhost `test` — `docker compose exec rabbitmq rabbitmqctl add_vhost test` и `docker compose exec rabbitmq rabbitmqctl set_permissions -p test app '.*' '.*' '.*'`, затем `TEST_DATABASE_URL=postgresql+psycopg://app:app@localhost:5432/app TEST_RABBITMQ_URL=amqp://app:app@localhost:5672/test uv run pytest -m integration -rs`. `TEST_DATABASE_URL` нужен каждому integration-тесту: тест создаёт случайную схему, мигрирует и удаляет только её. `TEST_RABBITMQ_URL` нужен тестам worker, очереди индекса (#142), rerun, cancel, остановки worker и сквозным сценариям `.review/rules.md` (`tests/test_worker_queue_postgres.py`, `tests/test_code_index_queue_postgres.py`, `tests/test_rerun_run_postgres.py`, `tests/test_cancel_run_postgres.py`, `tests/test_worker_shutdown.py`, `tests/test_rules_md_e2e_postgres.py`): они удаляют и заново объявляют топологию очередей, поэтому им нужен свой vhost, не `/`, где потребляет compose `worker`. Без переменной соответствующие тесты локально skipped, а не падают. Только при `GITHUB_ACTIONS=true` эти шесть файлов берут `RABBITMQ_URL`, если `TEST_RABBITMQ_URL` не задан: так их запускает job `Python lint / type / test`, где PostgreSQL и RabbitMQ — service containers. Его шаг «Integration suite (zero skips)» запускает `uv run pytest -m integration -q -rs` и падает, если integration-тестов нет или хоть один skipped.

**Доступ к LLM для live eval.** Ключи LLM-шлюза — organization secret `AI_DMC268_T6`; workflow передают его в `LLM_API_KEYS` только шагам, которые вызывают модель. Корпус оценивает `eval-live.yml`: если секрет не доступен репозиторию, job падает до вызовов модели; прогон тратит баланс EUrouter команды. [LLM live run](../.github/workflows/llm-live-run.yml) (`llm-live-run.yml`) корпус не оценивает: это ручная живая проверка шлюза и D7 на одном diff (`python -m app.bootstrap.llm_gateway`, ответ проверяет `validate_findings.py`) в четырёх сценариях — review и conventions, основная модель с fallback и fallback как основная; её запускают, чтобы проверить модель или шлюз без прогона корпуса. Локально ключи задаёт `LLM_API_KEYS` в `.env` (модели — `LLM_MODEL` и `LLM_FALLBACK_MODEL`, см. `.env.example`), скрипты запускаются через `uv run --env-file .env`; значения ключей не коммитятся и не попадают в фикстуры, логи и отчёты.

### 2.1. Схлопывание и жизненный цикл Run (Р-1, Р-2, Р-14)

Отправить три `pull_request.synchronize` с `sha_1`, `sha_2`, `sha_3` в один открытый PR с активным `ai-review`: устаревшие Run отменяются; публикация разрешена только для SHA, совпадающего с головой PR в GitHub при проверке перед POST; in-flight worker и publisher повторно проверяют актуальность перед публикацией. Дубли одного `X-GitHub-Delivery` и одного `(PR, head_sha, trigger=webhook)` не создают второй Run. Тестировать реальные PG constraints: default `queued`, единственный активный Run, уникальный `idempotency_key`, FK-цепочку; не ограничиваться проверкой существования индекса. Проверять переходы и `succeeded` по [PIPELINE_SPEC §1](PIPELINE_SPEC.md#1-состояния-и-переходы), без старого `COMPLETED` в домене/UI.

### 2.2. Webhook и триггер (Р-9, Р-10)

Отсутствующая/неверная HMAC-подпись отклоняется; валидное событие получает HTTP 202 с p95 < 500 мс, появление Run проверяется ожиданием eventual результата. Человеческий `labeled(ai-review)` и зелёный CI на текущем SHA запускают `try_enqueue` в любом порядке. `check_suite`, `workflow_run` и commit `status` сверяются REST-запросом: чужие suites завершены `success | neutral | skipped`, собственный suite App и чужой suite `queued` без check run (`latest_check_runs_count = 0`, #72) исключены, combined status `success` либо статусов нет; чужой suite `queued` без запусков не блокирует гейт и не считается CI; `in_progress` и `completed` учитываются при любом числе запусков (блокируют, пока не `completed` с `success | neutral | skipped`), а `queued` хотя бы с одним запуском блокирует. При `wait_for_ci=always` ждём CI; `auto` допускает старт без CI после 2 минут через sweep worker каждые 30 с; `never` достаточно лейбла и не проверяет CI. Красный/ожидающий CI не запускает Run в режимах `always` и `auto`; в режиме `never` не препятствует запуску. `unlabeled` (не от нашего бота) выключает будущие старты, `closed` отменяет активный и не трогает флаг лейбла, `reopened` перечитывает лейбл, `synchronize` отменяет старый Run и пробует новый SHA; события нашего бота игнорируются. Полный контракт — [PIPELINE_SPEC §8](PIPELINE_SPEC.md#8-триггер-р-10-d2). `review_requested` не является входным событием бота; активный лейбл хранит флаг `code_changes.ai_review_labeled`.

### 2.3. Движки, контекст и post-processing (Р-3, Р-5, Р-6, Р-8, Р-13)

Целевой DiffEngine создаёт кандидаты `ReviewOutput v2`; проверять схему и семантику, L0–L4, фактическую видимость всех строк evidence, неизменность snapshots и учёт каждого generation-вызова. После deterministic passes отдельный verifier обязателен для непустого набора; `supported`, `contradicted` и `insufficient_context` не взаимозаменяемы. `FinalReviewResult`, summary и payload фиксируются до `publishing`. Ветка > 3 000 разрешённых строк после ignore не выполняет LLM-вызовов, сохраняет доступный UI patch и отключает только review_patch. Проверять точные hunk/range coordinates без угадывания якоря, `side=RIGHT`, `start_side`, `commit_id`, cap 10 inline и строго ограниченный general-only путь. `suggestion` — готовая замена указанного диапазона новых строк, без diff-маркеров; GitHub code fence добавляет publisher, general/body-only не получает применимую suggestion. Идентичность публикации включает run/head/diff/final result и точный payload; повтор publish не вызывает модель. Подробная матрица #107 — §2.7. Проверять, что сеть GitHub/LLM вызывается вне DB-транзакции; отдельная запись usage после provider call не разрешает network внутри transaction. RAG v1 (Р-13): с выключенным тумблером обращений к индексу и действия `context.rag` нет; функциональные сценарии индекса и кандидатов графа — §3.5.

### 2.4. SandboxEngine (Р-4, фаза 3)

После реализации проверить `--network=none` исходящим socket/DNS запросом из контейнера, read-only mount, non-root/read-only rootfs, отсутствие GitHub/DB/LLM токенов в его env и fallback по таймауту 10 мин в том же Run. TC-09 старого UI набора относится сюда, а не к LLM-корпусу. До фазы 3 этот gate не объявляется пройденным.

### 2.5. Auth API, Portal API, rerun и UI (D4, T3, Р-7)

Проверять `state` в SPA до обмена code, `POST /api/auth/github/callback`, JWT access 15 мин в памяти и ротируемую refresh cookie 30 дней (`HttpOnly; Secure; SameSite=Strict; Path=/api/auth`), `refresh → me` при старте, logout и отзыв семейства при повторном использовании refresh. GitHub exchange failure не создаёт cookie/сессию. Все защищённые `/api/*` и SSE fetch-stream требуют Bearer; неверная подпись, истёкший токен, неверные `iss`/`aud`, отсутствующий или битый Workspace claim дают 401. **`workspaces: []` валиден:** `GET /api/repos` отвечает 200 с `[]`, `GET /api/runs` — 200 с `{items: [], nextCursor: null}`; UI показывает подключение, а не ошибку авторизации. Две установки/Workspace доказывают изоляцию списков и 404 для чужого Run. Контракт — [SD §12](SYSTEM_DESIGN.md#12-контракт-api--ui) и [OpenAPI](../contracts/openapi.yaml).

`POST /api/runs/{id}/rerun` (T3) создаёт новый `queued` Run на текущем SHA с `trigger=rerun` без проверки лейбла/CI. Проверять четыре причины 409: активный Run, закрытый PR, пауза ревью репозитория и недоступная GitHub App. Для всех причин `detail` один: `the pull request has an active run or is closed, or repository reviews are paused or unavailable`; машиночитаемого кода причины нет. Порядок проверок и случай паузы без активной конфигурации — [PIPELINE_SPEC §6.1](PIPELINE_SPEC.md#61-http-отказы-rerun). Контрактные тесты проверяют оба тела 422 через схему rerunRun в [OpenAPI](../contracts/openapi.yaml): массив `detail` у `HTTPValidationError` для невалидного UUID и точное `{"detail": "the repository has no active rule or prompt version"}` у `ErrorResponse` для отсутствующей конфигурации. Нет авторизации — 401, чужой Workspace — 404. Клиент UI проверяет callback failure/state mismatch, один общий refresh при конкурентных 401 и ровно один retry исходных запросов, logout при неудачном refresh, восстановление сессии через refresh+me. Контракт Finding→ReviewComment проверяет однострочный и диапазонный D6-якорь, severity/category и состояния Run.

При внедрении #107 RunDetail/RunSession/latestRun и UI используют один сохранённый result, его coverage, counts и verdict. Проверять partial без значимых supported findings → verdict null; неизвестные счётчики отображаются как неизвестные, а не 0. Исторический run получает `verification_mode=legacy`, `coverage=null`, без выдуманных evidence/подтверждения. Начатый legacy run завершается legacy-профилем; новый профиль включается только после совместимых schema/storage/API/UI, смешанный run запрещён. Rerun того же head создаёт свой policy/diff snapshot и публикационную identity.

### 2.6. Инфраструктура и безопасность

Проверять durable RabbitMQ, ack после фиксации Run, retry/DLQ по [PIPELINE_SPEC §4](PIPELINE_SPEC.md#4-retry-и-backoff), внутреннюю доступность PostgreSQL/RabbitMQ/Redis и отсутствие секретов/кода клиента в stdout. Невалидное сообщение и падение worker не должны терять Run; реконсилер возвращает зависшие задачи. Секреты подаются через env CI, не хранятся в корпусе. Синтетические токены — явно фиктивные значения. Для fixture `.patch` проверять сохранность байтов после hooks и `git apply --check`.

### 2.7. Матрица приёмки #107: политика, контекст, верификация и write-back

Сценарии ниже добавляются в unit/contract и integration по мере реализации; они не описывают уже существующее покрытие. Точные лимиты, грамматика и причины отказа берутся из двух новых спецификаций, а fixtures фиксируют версии этих контрактов.

| Сценарий | Ожидаемый результат |
| --- | --- |
| Нет rules/AGENTS; пустой документ; только секция Style; смешанный Python/TypeScript | Отсутствие и пустота различимы в snapshot; дефолты остальных категорий действуют. Языковые примеры остаются обычным Markdown, не фильтром backend. |
| Конкретное исключение rules против AGENTS/дефолта | Иерархия соблюдена только для соответствующего требования; формат, verifier, evidence, инструменты и получатель не переопределены. Проверить `rule_name` и ссылки на действующий источник. |
| Все положительные/отрицательные примеры `review-ignore/v1`, второй блок, 257 масок, неправильный UTF-8, byte/token/matcher overflow | Детерминированный результат по RULES_FORMAT_SPEC §§3–4; ошибочная политика даёт явный failed без усечения, пустой подмены и LLM. Временная ошибка VCS допускает только предусмотренный retry. |
| AGENTS отсутствует; существует, но превышает лимит или не декодируется; размер известен либо загрузка остановлена на лимите | Отсутствие допускает продолжение; непригодный документ даёт failed до LLM. Приложение и check-run сообщают путь, причину, предел/фактическое значение, когда оно известно, и способ исправления; не подменяют отказ предупреждением или неизвестный размер точным числом. |
| Head меняет policy; base продвинулся после fork; retry после изменения active config | Политика читается из закреплённого `policy_base_sha`; старые строки из подтверждённого `diff_old_sha`, который может отличаться. Retry не меняет pins, rerun получает собственный snapshot. Repository 404 не равен отсутствующему policy-файлу. |
| 4 000 ignored + 50 allowed строк; 3 000/3 001 allowed; все ignored; неполная пагинация files | Обычное ревью 50 и 3 000 строк; summary-only для 3 001/неполного inventory без LLM; только доказанные E без T → skipped. UI patch сохраняется; `review_patch=null` не восстанавливается из БД как code для модели. |
| Доказанный binary-only; доказанный binary + код; маска assets/** на oversized assets/a.txt | skipped; возможен complete/clean; ignored побеждает too_large. Канон — CVS §2.2/§9.2 |
| Too_large-only; too_large >20 000 строк + 50 строк кода; неразрешённый rename/submodule/mode-only с ignored | none; обычное ревью 50 строк с partial; none при полном inventory и отсутствии иных записей. Границы лимитов — CVS §9.2 |
| Только incomplete без hunks (missing/malformed/truncated patch); I+E, I+T, I+E+T без текста; полный inventory/known counters 0/3 000 | Backend FinalReviewResult partial, findings=[], verdict=null, summaryOnly=false; ноль conventions/context/reviewer/verifier вызовов, pins и below_severity_threshold_count=0 сохранены; reviewed_changed_lines=0, неизвестная исходная статистика null; публикация → succeeded/neutral. Классификация сохранена до раннего выхода. |
| Truncated-only с сохранившимися разрешёнными hunks; truncated без hunks; malformed без hunks; каждый также с E/T, known counted allowed ≤3 000 | Hunks вне E/T поступают в L1: обычное review/partial, blocking возможен; без hunks — backend partial, 0 LLM. Ограничения incomplete-якорей сохраняются; raw malformed patch не становится L1. |
| Полный inventory только T либо E/T с неизвестными raw counters T; incomplete вне E/T с неизвестными additions/deletions | В первом случае relevant counted scope пуст=0 → none, не summary-only; во втором relevant counters неизвестны → summary-only/partial до no-text ветки. E/T counters — диагностика. |
| Missing-patch-only 3 001/4 000 known counted allowed; неизвестные relevant counters или неполный inventory | Оба триггера summary-only проверяются раньше no-text branch: partial, summaryOnly=true, 0 LLM; none/skipped не выводятся из неизвестности. |
| Пустой added __init__.py/.gitkeep без patch; removed image без нового blob и independent binary proof | missing_patch/incomplete; raw changes=0/metadata_only не доказывает чистый rename/mode-only. Без текста, при known ≤3 000 — backend partial; рядом с кодом — review partial. |
| NUL в первых 8 KiB/PNG-JPEG-GIF-PDF-ZIP-ELF magic в blob; NUL только за 8 KiB без magic в blob; NUL в patch на любой позиции; oversized unread blob; независимо доказанный binary при oversized | Только подтверждённый binary — E; NUL вне окна blob сам не доказывает binary, но NUL в patch на любой позиции доказывает binary; без независимого evidence oversized — too_large/T до blob-based detection. Removed без blob не становится E по расширению (CVS §9.2). |
| Доказанный pure rename/mode-only с чистым кодом; отдельно empty added с кодом | T либо incomplete препятствуют clean: partial/null при пустых findings, даже без известного непроверенного текста; отдельно T → none, incomplete при no-text known ≤3 000 → backend partial. |
| Known ignored/generated anchor без manifest; ignored deleted-file anchor; binary anchor; unknown path, совпавший с маской; allowed anchor с ignored evidence | Trusted identity → ignore/generated раньше видимости: ignored_file сам по себе не partial; для ignored удаления побеждает структурный left_anchor_unsupported. Binary не получает этот bypass: line_absent/partial. Unknown path не доказывает E, сохраняется unknown_path/identity error. Запрещённый evidence у allowed anchor → evidence_source_disallowed/partial. Независимые причины partial не исчезают; pre-send/general guards действуют. |
| Pass-4 guard: изолируемый запрещённый evidence/general конкретного кандидата; неизолируемый источник/повреждённые snapshot/manifest bindings; попытка pre-send запрещённого источника | В проходе 4: evidence_source_disallowed/partial без general/verifier eligibility; при невозможности изоляции/проверки целостности — весь run snapshot_integrity_error/failed, без публикации. Отдельный pre-send guard даёт policy_context_violation/failed до следующего вызова, не drop и не второе ignored_file. |
| Deleted-file identity вне ignore/generated; LEFT-only coordinate без deletion evidence; overlap с валидным shown deletion evidence; непоказанные/запрещённые/wrong-revision refs | Структурный left_anchor_unsupported в проходе 2; evidence-dependent отказ предварительный до прохода 3. При валидных refs без подтверждения удаления — wrong_side; при валидном подтверждении удаления без RIGHT — left_anchor_unsupported с приоритетом. Невалидные refs дают evidence-причину, не LEFT proof. Все технические drops partial, без general/verifier eligibility. |
| Конвенция уровня 4 против SEC1/COR1 | Не отменяет дефолт; явное base rules/AGENTS исключение имеет приоритет по RFS §2 |
| Изменение severity_threshold/max_comments после создания run | Retry сохраняет значения, rerun закрепляет новые; настройки входят в result_digest, но не policy_digest/conventions key |
| Supported medium отсекается critical, complete | below_severity_threshold_count=1; final findings пусты, clean допустим; summary_text и check-run берут N из одного result_id |
| Commit messages: 20 сообщений по 500 Unicode-символов, включая XML escaping | Ограничение количества/символов до escaping и фактический token-size после него; общий budget не превышен, metadata не становится evidence |
| Неподдерживаемый путь рядом с обычным файлом: backslash, управляющий символ, недекодируемое имя, превышение 1 024 UTF-8 байт или 128 сегментов | `unsupported_path` изолирует только проблемный источник, без «исправления» имени. Остальной допущенный файл проверяется; coverage partial, при пустых findings или только info verdict null. Содержимое и автоматически добавленное имя пропущенного файла не попадают в модель, включая metadata, conventions, AST, verifier, repair и fallback; сводка сообщает пропуск, диагностическое имя выводится безопасно и с ограничением длины. Неизвестный счётчик пропущенного файла остаётся null и сам по себе не включает summary-only. |
| Полный inventory: хотя бы один T, остальные только E/T по CVS §2.2; отдельно только E и неполный inventory | Для состава E/T с хотя бы одним T — 0 LLM-вызовов, пустые findings, coverage none с фактическими причинами T, verdict null; сохранённый backend-результат проходит публикацию и даёт succeeded/neutral. Это не all_changes_ignored и само по себе не summary-only. Все доказанные E без T → skipped; неполный inventory сохраняет summary-only/partial. |
| Rename/copy с неподдерживаемым old либо new path; повторный запрос нового пути через импорт/кэш | Пропущено всё изменение, запрет не обходится через валидную сторону и её AST/алиасы. Raw provider fields остаются диагностикой; каноническое имя не угадывается. Невозможность надёжной изоляции/нарушение целостности → snapshot_integrity_error, утечка в final payload → policy_context_violation до отправки. |
| Rename/copy из ignored пути; ignored файл импортирован; `**/*.md` исключает head rules/AGENTS | Известное происхождение сохраняет запрет для diff/L2/L3/L4, repair, fallback, verifier и cache hit. Исключение имеет только действующий base policy в роли policy_document. Исключённые пути/текст не попадают в модельные списки файлов. |
| Conventions cache miss/hit, PR body с инструкцией, старый cache без provenance | Общий input только base: ни title/body PR, ни head, ни changed paths. Ключ охватывает показанные документы/tree/excerpts и версии; изменение ignore/base-input даёт miss, legacy cache не используется. Нет исходников → `conventions_not_applicable` без вызова; ошибка реального вызова не превращается в пустой успех. |
| AST Python/TypeScript/TSX/JavaScript/JSX/Go, Unicode/CRLF и exclusive end; вставка между символами | Однобазовые точные source ranges; минимальный охватывающий символ либо module-level change. Одна степень однозначных импортов, без исполнения проекта/конфигов, сети или установки зависимостей. |
| JS/JSX `.js/.jsx/.mjs/.cjs`: функции, стрелочные функции, классы; статический относительный ESM import и прямой export; CommonJS/dynamic import/barrel | Синтаксический AST доступен для всех перечисленных расширений. L4 определения импортов — только при однозначном разрешении по профилю CONTEXT §3.3; CommonJS, dynamic import и re-export не угадываются. Неоднозначные расширения и смешанное JS/TS-разрешение получают явный fallback. |
| Go `.go`: функции, методы, типы struct/interface, константы; локальный module import, явный alias и package name, отличный от имени каталога | Точные диапазоны символов; прямой qualified exported symbol разрешается по закреплённым go.mod и полному разрешённому inventory пакета. Неявное имя берётся из package clause. Same-package lookup по соседним файлам и определение методов через тип receiver не заявляются возможностями resolver v1. |
| Go build constraints/GOOS/GOARCH/cgo, go.work/replace/nested module/dot import; ignored либо 21-й дополнительный файл | Нет зависимости от окружения worker и запуска go list. Неподдерживаемый выбор пакета, неполный inventory или исчерпание общего лимита 20 импортированных/служебных файлов (включая go.mod) не дают resolved по частичному набору. Сохраняются доступные L1–L3 и явная причина; `_*.go`, `.*.go`, `_test.go` не входят в обычный inventory импортируемого пакета. |
| AST timeout/resource limit/parse error, unsupported language, неоднозначный/внешний/ignored импорт | Сохраняются доступные L1–L3; нет угадывания определения или подмены LEFT на head. Причина фиксируется; partial требуется при недоступном основании конкретного кандидата. Отказ обязательного VCS/L1 не маскируется как AST fallback. |
| Context budget отсекает часть hunk или тело символа; repair/fallback получает другой набор блоков | Manifest строится после окончательного rendering, имеет свой call_id и точные shown ranges/omissions. Полная DiffMap не доказывает видимость; refs разрешаются только в принятом call. Candidate/previous_output/conventions не становятся code evidence. |
| Неверный snapshot/hash/revision или источник без provenance перед любой стадией | Guard останавливает отправку с `policy_context_violation`/ошибкой целостности; запрещённых LLM-вызовов нет. Кэш не обходит guard. Manifests/excerpts workspace-scoped и не выводятся в обычные логи. |
| V2 finding с непоказанным block/строкой, неверным source или revision; ссылка неверной формы | Синтаксическая ошибка инвалидирует весь ответ; семантически недостоверная evidence отбрасывает кандидата и отражается в coverage. Все строки диапазона должны быть показаны, не только края. |
| Якорь вне hunk, диапазон через hunks/непоказанную строку, несуществующая строка, deletion-only | General-only лишь для существующего полностью показанного RIGHT диапазона того же файла/revision, затем verifier. Невалидный якорь не становится общим замечанием и не переносится на ближайшую строку. Deletion-only без RIGHT якоря → `left_anchor_unsupported`, partial до v3. |
| DiffMap: добавление/удаление, rename/copy, zero-length hunk, context/deletion/addition, no-newline marker, binary/missing/truncated patch | Счётчики hunks и обе стороны согласованы с provider refs; marker не расходует строку; пути не угадываются. GitHub payload использует line/side и start_line/start_side, не legacy position. GitLab fixtures проверяют version/base/start/head refs, old/new paths и диапазоны; live GitLab/LEFT отложены до отдельного включения. |
| Два разных дефекта на одном якоре; точный дубль; lint-похожие слова в security finding | Только точный дубль устраняется; lint-filter не скрывает содержательный дефект. Кандидаты после фильтров неизменяемы; verifier не дописывает находки, координаты, severity или suggestion. |
| Пустой набор после фильтров; непустой набор; supported/contradicted/insufficient | Пустому verifier не нужен, повторный поиск не запускается. Для непустого каждый ID получает ровно одно решение; только supported публикуется. Неверная suggestion не исправляется скрытно. Insufficient отдельно от опровержения и даёт partial. |
| Лишний/повторный/пропущенный ID, неверная ref/requirement ref в verifier response | Весь ответ невалиден, удобное подмножество решений не принимается. Recovery только в общем бюджете, затем `verification_invalid_output`, без retry run и публикации. |
| Cache miss `C1 → R1 invalid → R2 repair → V1`; `C1 → C2 repair → R1 → V1 invalid`; cache hit с двумя recovery | До 4 generation-вызовов на попытку, до 12 на run и 3 попыток; downstream verifier slot зарезервирован. Второй сценарий завершается failed без пятого вызова. Потенциальная generation при transport retry также учитывается. |
| Недоступность verifier, 429/402, нехватка стоимости или дедлайна, overflow обязательного envelope | Временная `verification_unavailable` допускает 30 с/2 мин и максимум 3 попытки, затем failed; invalid output не переклассифицируется в unavailable. Учитываются Retry-After/маршрут; лимиты $0.50 fast/$3 deep на весь run, резерв verifier и время 90 с + 10 с локального завершения. Budget/deadline/context overflow → terminal failed, непроверенные findings не публикуются. |
| Crash после reviewer checkpoint, после verifier/final result, повтор publish | До verifier используется сохранённый набор; после final result нет новых смысловых LLM-вызовов. Usage и пределы всех попыток сохраняются, ошибки provider не превращаются в нулевую стоимость. |
| Все кандидаты insufficient; partial с supported high; complete без findings; duplicate/lint/contradicted drop | Соответственно verdict null, blocking, clean; последние drops сами по себе не создают partial. Backend summary не повторяет rejected/insufficient утверждения; API/review/check-run имеют один result и counts. |
| Компактная summary и general findings суммарно >4 000 символов; готовый payload на границе/сверх лимита адаптера; oversized body-only fallback | 4 000 Unicode-символов ограничивают только summary_text. Полное тело сохраняет supported findings. Все payload выбранного варианта, включая markers, сверяются с закреплёнными единицами/пределами профиля; превышение → failed/publication_payload_too_large до его первого POST. Findings не усекаются; suggestion_unrenderable убирает только применяемый блок. |
| Два runs/rerun одного head с одинаковыми findings; повтор доставки; marker другого автора или другого payload | Разные runs имеют разные publication keys; повтор одного run восстанавливает собственную операцию. Проверяются bot/app, PR/MR, head, полный payload и пагинация, одного findings_hash/marker недостаточно. |
| Timeout/неоднозначный 5xx после возможного POST; истёк lease; удалённая операция на второй странице | Сначала recovery сохранённого payload/receipt, без параллельного или слепого POST. Неполный lookup не означает отсутствие. Невосстановленный исход → `failed/publication_outcome_unknown`, автоматическая отправка блокирована. |
| 422 coordinates при неизменном diff; 422 body/spam/schema; stale commit/head; 403 rate limit | Только подтверждённый координатный отказ допускает один заранее сохранённый body-only вариант с отдельным key/digest. Другие ошибки не дают такого fallback; stale snapshot → cancelled/superseded. Неизвестный исход запрещает fallback. |
| Push между GET и POST; ошибка записи receipt; GitLab partial publication; legacy незавершённая публикация | IDs сохраняются и связываются явно, не по порядку; старый head не объявляется проверенным. Оставшиеся GitLab POST прекращаются при устаревании, готовые не повторяются. Неоднозначный legacy result → `legacy_publication_unresolved`, без повторного POST. |

Отдельные adversarial fixtures содержат инъекции в rules/AGENTS/PR/code/conventions/candidate/repair-output и попытки переноса через cache: закрытие envelope, навязывание находки, подавление настоящего дефекта, смена адресата/формата/полномочий. Рядом нужны допустимые исключения и цитаты примеров атак. Детерминированные assertions проверяют запрет отправки/публикации и границы источников; влияние на смысл ответа измеряется отдельно по §3.4. Валидная schema сама по себе не доказывает устойчивость к инъекции.

### 2.8. Каталог сценариев `.review/rules.md` (#163)

Каталог фиксирует, как run обрабатывает `.review/rules.md` от вебхука до публикации. Политика читается только из закреплённой base-ревизии (`policy_base_sha = Run.base_sha`, [RFS §3.1](RULES_FORMAT_SPEC.md#31-ревизия-и-тип-объекта)): `.review/rules.md`, добавленный или изменённый в самом PR, — обычный проверяемый код, а не политика. Сквозные сценарии (таблица А) живут в `tests/test_rules_md_e2e_postgres.py`, unit-покрытие — в файлах, названных в таблице Б. На `main` текст rules.md ещё не доходит до модели (остаток #130, AC9), поэтому строки про envelope и инъекции покрыты на unit-уровне (этап 2 #163), а вживую измеряются только после v4 `review.system`. Отчёт с результатами прогонов — [RULES_MD_TEST_REPORT](RULES_MD_TEST_REPORT.md).

**Таблица А — сквозные сценарии.** Цепочка: подписанный webhook `pull_request labeled` → Run (его создаёт проекция приложения, тест Run не вставляет) → RabbitMQ → `run_worker` → `policy.load` → fake LLM → публикация в fake GitHub; обязательный job `Python lint / type / test`, шаг «Integration suite (zero skips)», маркер `integration`. PR меняет `app/service.py` и `generated/client.py`. Каждый сценарий ждёт завершённого check-run; политика читается только по SHA дерева и blob, без `/contents/`. Коды и presence — литералы [RFS §3, §4, §9](RULES_FORMAT_SPEC.md#9-ошибки-и-наблюдаемость), итог check-run — [PIPELINE_SPEC §7](PIPELINE_SPEC.md#7-check-run-бота-sd-83). «Строки `run_policy_snapshots` нет» — текущее поведение: при ошибке конфигурации RFS §7.2 допускает, что полного snapshot нет.

| Сценарий | Вход: rules.md в base | presence / snapshot | Код | Статус Run | LLM | Публикация | Тест |
| --- | --- | --- | --- | --- | --- | --- | --- |
| missing | записи `.review` в дереве base нет | `missing`, `raw_sha256 = null` | — | `succeeded` | да; пути и маркеры обоих изменённых файлов доходят до модели | одно ревью, `commit_id` = head; check-run `completed` / `neutral` | `test_missing_rules_md_lets_the_whole_diff_reach_the_model` |
| valid | секции Markdown и один блок `review-ignore` с маской `generated/**` | `present` | — | `succeeded` | да; ни пути, ни маркера `generated/client.py` нет ни в одном вызове, маркер `app/service.py` есть | одно ревью, `commit_id` = head; check-run `completed` / `neutral` | `test_review_ignore_keeps_the_ignored_file_out_of_every_llm_call` |
| agents-md | rules.md нет; корневой `AGENTS.md` с блоком `review-ignore` на оба изменённых файла | rules.md `missing`, AGENTS.md `present` | — | `succeeded`, не `skipped` | да; пути обоих изменённых файлов доходят до модели | одно ревью, `commit_id` = head; check-run `completed` / `neutral` | `test_review_ignore_in_agents_md_is_ordinary_text` |
| empty | пустой файл, 0 байт | `present`, `raw_sha256 = e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`, `byte_count = 0`; `policy_digest` отличается от прогона missing | — | `succeeded` | да, вызов `ReviewOutput` | одно ревью, `commit_id` = head; check-run `completed` / `neutral` | `test_empty_rules_md_is_present_and_has_another_policy_than_a_missing_one` |
| at-limit | ровно 65 536 байт | `present`, `byte_count = 65536` | — | `succeeded` | да, вызов `ReviewOutput` | одно ревью, `commit_id` = head; check-run `completed` / `neutral` | `test_rules_md_at_the_size_limit_is_present` |
| all-ignored | блок `review-ignore` с масками `app/**` и `generated/**` — оба изменённых файла | `present` | `all_changes_ignored` | `skipped` | нет | ревью нет; check-run `completed` / `skipped` | `test_all_changes_ignored_skips_the_run_without_llm_or_review` |
| decode | `ok\n\xff\xfe` — невалидный UTF-8 | строки `run_policy_snapshots` нет (текущее поведение); `policy.load` → `{error: {type, message}}` | `policy_document_decode_error` | `failed`, `attempt = 1` | нет | ревью нет; check-run `completed` / `neutral` | `test_invalid_rules_md_fails_the_run_once_without_llm[decode]` |
| control | `\x1bfirst` — управляющий ESC (U+001B) | строки `run_policy_snapshots` нет (текущее поведение); `policy.load` → `{error}` | `policy_document_control_character` | `failed`, `attempt = 1` | нет | ревью нет; check-run `completed` / `neutral` | `test_invalid_rules_md_fails_the_run_once_without_llm[control]` |
| duplicate | два top-level блока `review-ignore` | строки `run_policy_snapshots` нет (текущее поведение); `policy.load` → `{error}` | `policy_ignore_block_duplicate` | `failed`, `attempt = 1` | нет | ревью нет; check-run `completed` / `neutral` | `test_invalid_rules_md_fails_the_run_once_without_llm[duplicate]` |
| nested | блок `review-ignore` внутри элемента списка | строки `run_policy_snapshots` нет (текущее поведение); `policy.load` → `{error}` | `policy_ignore_block_nested` | `failed`, `attempt = 1` | нет | ревью нет; check-run `completed` / `neutral` | `test_invalid_rules_md_fails_the_run_once_without_llm[nested]` |
| unclosed | открывающий fence `review-ignore` без закрывающего | строки `run_policy_snapshots` нет (текущее поведение); `policy.load` → `{error}` | `policy_ignore_block_unclosed` | `failed`, `attempt = 1` | нет | ревью нет; check-run `completed` / `neutral` | `test_invalid_rules_md_fails_the_run_once_without_llm[unclosed]` |
| info | info-string `review-ignore extra` | строки `run_policy_snapshots` нет (текущее поведение); `policy.load` → `{error}` | `policy_ignore_info_invalid` | `failed`, `attempt = 1` | нет | ревью нет; check-run `completed` / `neutral` | `test_invalid_rules_md_fails_the_run_once_without_llm[info]` |
| pattern | маска `a?.py` (пример RFS §4.3) | строки `run_policy_snapshots` нет (текущее поведение); `policy.load` → `{error}` | `policy_ignore_pattern_invalid` | `failed`, `attempt = 1` | нет | ревью нет; check-run `completed` / `neutral` | `test_invalid_rules_md_fails_the_run_once_without_llm[pattern]` |
| too-large | 65 537 байт | строки `run_policy_snapshots` нет (текущее поведение); `policy.load` → `{error}` | `policy_document_too_large` | `failed`, `attempt = 1` | нет | ревью нет; check-run `completed` / `neutral` | `test_invalid_rules_md_fails_the_run_once_without_llm[too-large]` |
| limit | 257 разных масок | строки `run_policy_snapshots` нет (текущее поведение); `policy.load` → `{error}` | `policy_ignore_limit_exceeded` | `failed`, `attempt = 1` | нет | ревью нет; check-run `completed` / `neutral` | `test_invalid_rules_md_fails_the_run_once_without_llm[limit]` |
| symlink | `.review/rules.md` — symlink (mode `120000`) | строки `run_policy_snapshots` нет (текущее поведение); `policy.load` → `{error}` | `policy_document_type_unsupported` | `failed`, `attempt = 1` | нет | ревью нет; check-run `completed` / `neutral` | `test_symlinked_rules_md_fails_the_run_once_without_llm` |
| root-404 | 404 на дерево base; записи `.review` нет | строки `run_policy_snapshots` нет (текущее поведение); `policy.load` → `{error}` | `policy_revision_unavailable` или `policy_access_denied` (обе строки §9: недоступна ревизия либо repository) | `failed`, `attempt = 1` | нет | ревью нет; check-run `completed` / `neutral` | `test_unreadable_rules_md_fails_the_run_and_is_never_missing[root-404]` |
| root-denied | 403 без признаков rate limit на дерево base; записи `.review` нет | строки `run_policy_snapshots` нет (текущее поведение); `policy.load` → `{error}` | `policy_access_denied` | `failed`, `attempt = 1` | нет | ревью нет; check-run `completed` / `neutral` | `test_unreadable_rules_md_fails_the_run_and_is_never_missing[root-denied]` |
| tree-404 | 404 на поддерево `.review` | строки `run_policy_snapshots` нет (текущее поведение); `policy.load` → `{error}` | `policy_revision_unavailable` или `policy_access_denied` | `failed`, `attempt = 1` | нет | ревью нет; check-run `completed` / `neutral` | `test_unreadable_rules_md_fails_the_run_and_is_never_missing[tree-404]` |
| blob-404 | 404 на blob `rules.md` | строки `run_policy_snapshots` нет (текущее поведение); `policy.load` → `{error}` | `policy_revision_unavailable` или `policy_access_denied` | `failed`, `attempt = 1` | нет | ревью нет; check-run `completed` / `neutral` | `test_unreadable_rules_md_fails_the_run_and_is_never_missing[blob-404]` |
| denied | 403 на поддерево `.review` без `X-RateLimit-Remaining: 0` и `Retry-After` | строки `run_policy_snapshots` нет (текущее поведение); `policy.load` → `{error}` | `policy_access_denied` (не `cancelled`) | `failed`, `attempt = 1` | нет | ревью нет; check-run `completed` / `neutral` | `test_unreadable_rules_md_fails_the_run_and_is_never_missing[denied]` |
| truncated | листинг дерева base с `truncated: true`; записи `.review` нет | строки `run_policy_snapshots` нет (текущее поведение); `policy.load` → `{error}` | `policy_revision_unavailable` или `policy_fetch_transient` — до решения #247 ч.1 | `failed`; число попыток не закреплено | нет | ревью нет; check-run `completed` / `neutral` | `test_truncated_base_tree_fails_the_run_and_is_never_missing` |
| head-added | в base записи `.review` нет; PR добавляет `rules.md` с блоком `**/*` | `missing` | — | `succeeded`, не `skipped` | да, вызов `ReviewOutput` | одно ревью, `commit_id` = head; check-run `completed` / `neutral` | `test_rules_md_added_by_the_head_is_not_the_policy` |
| head-changed | в base — текст без масок; PR меняет его на блок `**/*` | `present`, `raw_sha256` = SHA-256 байтов base | — | `succeeded`, не `skipped` | да, вызов `ReviewOutput` | одно ревью, `commit_id` = head; check-run `completed` / `neutral` | `test_rules_md_changed_by_the_head_keeps_the_base_policy` |

**Таблица Б — строки [RFS §11](RULES_FORMAT_SPEC.md#11-приёмочные-сценарии-для-будущей-реализации).** Каждая строка про загрузку, ignore, изоляцию, маршруты coverage и envelope либо покрыта тестом, либо помечена причиной. E2E — сценарии таблицы А; unit-тесты — файлы `tests/`.

| Строка RFS §11 | Обязательный результат (кратко) | Покрытие |
| --- | --- | --- |
| Нет обоих документов; код в `packages/` и смешанный стек | Полные дефолты, источники политики `missing`, базовое ревью не отключено | `test_policy_documents.py::test_absent_documents_are_missing_and_nothing_is_read`; E2E missing (источники `missing`, ревью опубликовано); полнота дефолтов в prompt не проверяется — после остатка #130 (AC9); не покрыто: смешанный стек `packages/` — такого кейса нет ни в #248, ни в подкорпусе #252 |
| Rules имеет только Style | Остальные базовые категории действуют | не покрыто: в подкорпусе #252 нет rules.md только из Style; вживую — не раньше остатка #130 (AC9); рендер блока политики уже проверяет `test_prompt_builder_markdown.py::test_prompt_builder_renders_markdown_policy_snapshot` |
| Конвенция уровня 4 допускает небезопасный паттерн против SEC1/COR1 | Дефолт не отменён; явное исключение base rules/AGENTS приоритетно по §2 | не покрыто: кейса с конвенцией уровня 4 нет ни в #248, ни в подкорпусе #252; вживую — не раньше остатка #130 (AC9), нужны дефолты и rules/AGENTS в рабочем prompt |
| Порог и `max_comments` изменены между retry и rerun | Retry сохраняет оба значения, rerun закрепляет новые; `policy_digest` от них не зависит | по построению: на `main` порог и `max_comments` не входят в snapshot — legacy-путь читает Repository при обработке (RFS §7.2), поэтому `policy_digest` от них не зависит; семантика retry/rerun — вне #163 (#168) |
| Rules разрешает дублирование test setup против AGENTS | Допустимое исключение соблюдено, остальные правила действуют | не покрыто: в подкорпусе #252 нет кейса, где исключение rules.md спорит с AGENTS.md (исключения RUL-05…RUL-08 — о другом); вживую — не раньше остатка #130 (AC9) |
| Пустой файл против отсутствующего | Разные snapshots; в обоих пустой user ignore | `test_prepare_policy_snapshot.py::test_empty_and_missing_rules_have_different_snapshots`, `test_policy_snapshot.py::test_empty_document_is_present_and_differs_from_a_missing_one`, `test_policy_documents.py::test_empty_file_is_present_with_the_checksum_of_empty_bytes`, `test_prompt_builder_markdown.py::test_policy_document_empty_vs_absent_and_manifest`; E2E empty (digest против missing) |
| Все примеры грамматики §4.3 | Точный указанный результат, без обращения к LLM | `test_review_ignore.py::test_no_document_no_block_and_an_empty_block_give_an_empty_policy`, `::test_tilde_fence_longer_closing_and_indented_fence_are_the_block`, `::test_other_labels_examples_and_indented_code_are_ordinary_markdown`, `::test_block_structure_errors_point_at_the_opening_fence`, `::test_invalid_masks_of_the_specification_are_rejected_with_a_position`, `::test_valid_masks_with_spaces_hashes_dots_and_unicode`, `::test_normative_matching_examples`, `::test_lines_are_trimmed_deduplicated_sorted_and_keep_their_source_lines`; E2E valid, duplicate, nested, unclosed, info, pattern и agents-md (`review-ignore` в AGENTS.md — обычный текст) |
| Повреждённый UTF-8 / второй блок / 257 масок / overflow tokens | Явный failed без LLM и без retry, вход не усечён | `test_policy_documents.py::test_broken_utf8_is_rejected_without_replacement`, `test_review_ignore.py::test_block_structure_errors_point_at_the_opening_fence`, `test_review_ignore.py::test_limits_of_mask_count_length_and_segments`, `test_review_run_worker.py::test_invalid_policy_fails_the_run_at_once_with_its_code`; E2E decode, duplicate, limit (`attempt = 1`); overflow tokens на `main` недостижим (остаток #130, AC9); unit-покрытие самого guard — `test_policy_budget.py::test_verify_policy_budget_exceeded_raises_error`, `::test_pre_send_guard_verifies_policy_and_total_budget`, `test_execute_review.py::test_execute_review_fail_closed_when_budget_exceeded` |
| 404 repository против отсутствующего path | Первый не становится missing policy | `test_github_policy_source.py::test_empty_tree_proves_absence_and_a_truncated_one_does_not`, `::test_http_failures_map_to_policy_codes_and_never_to_a_missing_document`, `::test_blob_failures_are_policy_errors`, `test_policy_documents.py::test_read_failure_is_an_error_not_a_missing_document`, `test_prepare_policy_snapshot.py::test_read_failure_stages_nothing_and_is_not_a_missing_document`; E2E root-404, root-denied, tree-404, blob-404, denied |
| 4 000 исключённых + 50 разрешённых строк | Порог и бюджет считаются по 50 разрешённым | `test_run_processor_context_stage.py::test_ignored_lines_do_not_make_the_allowed_change_summary_only`, `test_review_context.py::test_ignored_lines_do_not_count_toward_the_summary_only_threshold` |
| Все изменения доказанно исключены по политике, технических пропусков нет | running → skipped; ноль LLM-вызовов; без clean | `test_review_context_stage.py::test_change_with_every_file_ignored_skips_the_run`, `::test_change_of_builtin_excluded_files_only_is_skipped_before_the_context_build`, `test_run_processor_context_stage.py::test_skipped_run_never_reaches_a_model_call`, `test_review_run_worker.py::test_skipped_attempt_finishes_the_run_as_skipped_with_its_reason`; E2E all-ignored |
| Недопустимый/непредставимый путь рядом с допустимым изменённым файлом | `unsupported_path` до ignore; имя и содержимое вне входов LLM; другой файл проверяется с partial coverage | изоляция: `test_review_ignore.py::test_file_with_an_unsupported_path_is_skipped_by_the_matcher`, `test_review_context_stage.py::test_path_outside_the_contract_is_always_withheld`; partial coverage на `main` не ведётся — после остатка #130; код `policy_path_invalid` против `unsupported_path` — #168 ч.4 |
| Полный список с `unsupported_path` и маской `**/*`, остальное ignored либо нет | Не all_changes_ignored: coverage none, 0 LLM, neutral check-run | после остатка #130: на `main` модель вызывается и публикуется clean — расхождение |
| Доказанный binary-only либо binary + ignored/generated | skipped/`all_changes_ignored`, 0 LLM | `test_review_context_stage.py::test_change_of_builtin_excluded_files_only_is_skipped_before_the_context_build`, `::test_binary_files_count_as_exclusions_when_the_whole_change_is_excluded` |
| Too_large/identity_unresolved/unsupported_change при полном inventory | coverage none, verdict null, 0 LLM, succeeded/neutral | после остатка #130 |
| Доказанный binary + код; too_large + 50 анализируемых строк | Binary не снижает coverage; too_large даёт partial | после остатка #130 (coverage на `main` не ведётся) |
| Неполный inventory / unknown counters / known missing-patch-only 4 000 строк | summary-only/partial, summaryOnly=true, 0 LLM | после остатка #130 (summary-only без LLM не реализован); что неполный список не даёт skipped — `test_review_context_stage.py::test_incomplete_file_list_does_not_prove_that_every_change_is_excluded` |
| Только incomplete без текста, полный inventory, counters ≤3 000 | Backend partial, findings=[], 0 LLM, succeeded/neutral | после остатка #130 |
| Пустой added без patch; removed image без blob; oversized непрочитанный blob | missing_patch/incomplete; too_large/T | после остатка #130 |
| Pure rename/mode-only либо пустое added рядом с кодом | T либо incomplete препятствуют clean | после остатка #130 |
| Ignored anchor без manifest; неизвестный mask-matching путь; allowed anchor с ignored evidence | ignored_file, unknown_path/identity error с partial, evidence_source_disallowed | не покрыто: `ignored_file` есть только в перечне `AnchorRejection` (`diff_anchor.py:32`), код его не выдаёт; `evidence_source_disallowed` в коде нет; partial coverage на `main` не ведётся — после остатка #130 (evidence и якоря — #120/#133) |
| Rename/copy с одной недопустимой стороной и допустимым новым alias | Всё изменение и новый alias изолированы | `test_diff_map.py::test_rename_with_one_rejected_side_keeps_the_valid_side_but_stays_withheld` — только уровень DiffMap; изоляция нового alias от импортов, AST и conventions не доказана — после остатка #130 |
| Изоляция недоказуема; нарушена целостность snapshot; запрещённый источник у guard | Failed; в последнем случае `policy_context_violation` до отправки | целостность: `test_prepare_policy_snapshot.py::test_concurrent_worker_snapshot_with_another_digest_is_an_integrity_error`; pre-send guard `policy_context_violation` — PR #230; failed при недоказуемой изоляции не проверяется — после остатка #130 |
| Rename/copy `generated/client.py → src/client.py` | Diff, full file и imported AST исключены согласованно | `test_review_context_stage.py::test_builtin_exclusions_are_withheld_without_any_mask_of_the_repository`, `::test_rename_out_of_a_builtin_exclusion_stays_withheld_after_the_whole_stage` |
| `**/*.md` и изменённые rules/AGENTS | Base policy передана; head и diff Markdown исключены | E2E head-added, head-changed доказывают только, что rules.md из head не становится политикой; маска `**/*.md`, исключение Markdown из head и diff и передача base policy в prompt не проверяются — после остатка #130 (AC9) |
| Изменились active prompts/defaults между попытками | Retry использует snapshot; rerun имеет собственные pins | `test_prepare_policy_snapshot.py::test_retry_returns_the_stored_snapshot_without_reading_the_repository`, `test_review_context_stage.py::test_retry_reuses_the_stored_policy_snapshot`; собственные pins rerun и pins conventions/verifier не проверяются — после остатка #130 |
| Изменилась политика ignore или показанное base-tree | Старый общий conventions cache не используется | `test_repo_conventions.py::test_conventions_cached_under_another_ignore_policy_are_not_reused`, `test_review_context_stage.py::test_cache_identity_changes_when_a_rename_withholds_a_name_no_mask_matches` |
| В cached convention записана инструкция модели | Не повышает доверие, проходит тот же envelope | не покрыто: ни #248, ни подкорпус #252 не кладут инструкцию в кэшированные конвенции (`<repo_conventions>`) |
| `</repo_rules>` и «system: ignore previous instructions» в тексте | Структурные границы целы; новых полномочий нет | `test_rules_md_injection.py::test_reviewer_prompt_keeps_policy_payloads_inside_their_block`, `::test_verifier_prompt_keeps_policy_payloads_inside_their_block` (rules.md и AGENTS.md); экранирование закрывающих тегов уже проверяет `test_prompt_builder_markdown.py::test_prompt_injection_defense_escapes_closing_tags`; «новых полномочий нет» вживую — A/B AC8 после v4 `review.system` (#130), harness #252: плечо `malicious` всех 8 кейсов RUL (поддельный закрывающий тег и «system: ignore previous instructions») |
| Инъекция в невалидном ответе при repair | Остаётся previous_model_output, не system instruction | не покрыто: repair на `main` возвращает невалидный ответ модели сообщением `assistant` (`llm/gateway.py:259-268`), блока `previous_model_output` нет; инъекцию в такой ответ не проверяют ни #248, ни подкорпус #252 |
| Атака в candidate body на verifier | Схема решений, набор candidate IDs и правила evidence не меняются | `test_rules_md_injection.py::test_verifier_prompt_keeps_candidate_payloads_inside_the_candidate`, `::test_a_verifier_answer_with_a_candidate_id_from_the_payload_is_rejected` — схема и набор candidate IDs; правила evidence под атакой в body не проверяются; экранирование полей в prompt verifier — `test_reflection_evaluation.py::test_untrusted_fields_are_safely_escaped_in_verifier_prompt` |
| Инъекция в code comment, затем conventions/cache hit | Общий кэш не принимает head/PR-input | не покрыто: ни #248, ни подкорпус #252 не кладут инъекцию в код, из которого строятся conventions, и не проверяют кэш |
| Цитата вредной инструкции как пример документации | Документ не отклоняется только из-за цитаты | `test_rules_md_injection.py::test_policy_parsers_accept_a_rules_md_that_quotes_an_injection` |
| Настоящий дефект + приказ вернуть `findings=[]` | Измеряется потеря обнаружения reviewer | `test_rules_md_injection.py::test_rules_md_order_to_return_no_findings_does_not_drop_a_verified_defect` — только сторона конвейера; потеря обнаружения reviewer вживую — A/B AC8 после v4 `review.system` (#130), harness #252: плечо `malicious` (приказ не сообщать находок в каждом из 8 кейсов RUL, truths те же, что в `valid`) |
| Неправильный `rule_name`/source ref | Нет ложной подписи custom rule и supported без основания | `test_rules_md_injection.py::test_a_rules_md_section_name_is_not_a_rule_name`, `::test_a_policy_attribution_without_a_matching_requirement_ref_is_rejected`, `::test_a_requirement_ref_outside_the_shown_policy_is_rejected`, `::test_legacy_post_processing_publishes_no_false_custom_rule_attribution` (legacy-путь: только отсутствие ложной подписи, судьба finding — #247 ч.3); уже есть `test_reflection_evaluation.py::test_service_defaults_rule_name_rejects_injections`, `test_reflection_pass.py::test_reflection_pass_validates_requirement_refs_and_evidence_refs` |

Известные расхождения ведутся в #247 (ч.1 — усечённое дерево, ч.2 — диагностика §9, ч.3 — атрибуция §8.2), #168 (ч.4 — `policy_path_invalid` против `unsupported_path`, ч.6 — размер blob, вторичный 403, дата в `Retry-After`) и в остатке #130 (`policy_prompt_budget_exceeded` недостижим; строка §11 про `unsupported_path` и `**/*`).

## 3. Методология LLM eval

### 3.1. Корпус и вход оценки

Корпус содержит 24 независимых PR-кейса (допустимо 20–30): четыре класса безопасности и по пять кейсов ресурсов, логики, синтаксиса и чистых изменений; не менее пяти licensed real PR (хотя бы один на класс), Python и TypeScript/React, не менее пяти отдельных `critical` ground truths. Слот SEC-05 исключён после остановки автоматическим фильтром и не входит в знаменатель replay/live оценки. Отображение класса в основную категорию: безопасность → `security`, ресурсы/память → `performance` (либо `correctness` при конкретном сбое), логика → `correctness`, синтаксический overhead → `readability`, чистое изменение → без findings. Исключения записываются в metadata и README.

Каждый кейс имеет JSON metadata/ground truth, исходное состояние файла (pre-image), применимый unified `.patch` и источник; raw responses хранятся отдельно от кейса, в `test-prs-dataset/responses/`. Для real PR обязательны URL PR/commit, revision, название и URL лицензии, attribution и ручная проверка права на конкретный excerpt. Ground truth использует `path`, `start_line`, `line`, `severity`, `category` из Finding; `start_line: null` означает одну строку, иначе `< line`. Ожидаемый `verdict` выводится из severity по [PIPELINE_SPEC §11](PIPELINE_SPEC.md#11-вердикт-и-бейджи-d3): `critical/high → blocking`, иначе `medium/low → attention`, иначе `clean`. Case schema собственная; [ReviewOutput schema](../review/schemas/review-output.schema.json) применяется только к ответу модели. `git apply --check` выполняется на копии pre-image, truth якорится в добавленных строках новой стороны; answer-hint комментарии запрещены.

Оценивать **сырой** ответ модели до post-processing, hunk-фильтра, production verifier, severity threshold, cap inline и публикации: иначе качество промпта скрывается правилами выпуска. Replay читает committed raw response без сети и ключа, с model ID, prompt version/SHA и run metadata. Для существующего legacy harness валиден только ответ, для которого `uv run python review/scripts/validate_findings.py <response.json>` завершился кодом 0 **и** напечатал `OK ReviewOutput`; exit 1/2 и `RepoConventionsDraft` невалидны. Это не проверка целевого v2 с evidence/manifests. Live использует PromptBuilder и `review/prompts/`, выбранную модель и внешние credentials, сохраняет raw output и ту же provenance для replay. Live запускается вручную (§2, `eval-live.yml`); закоммиченный baseline — ответы и `manifest.json` в `test-prs-dataset/responses/`, его модель и provenance описаны в [README корпуса](../test-prs-dataset/README.md); историческая съёмка 2026-10-13 использовала одиночные revision snapshots (результат §3.2.1); полная замена всех 24 ответов с проверенными PR prefixes сохранена как исторический baseline §3.2.2; текущий post-rebase baseline после merged #149 — §3.2.3. LLM-as-a-judge для оценки этого raw benchmark не используется. Обязательный production verifier по #107 — часть продукта, качество которого оценивается отдельно (§3.4).

### 3.2. Однозначное сопоставление и счётчики

Для каждой валидной находки и эталона одного кейса ребро допустимо, когда совпадают **`path` и `category`**, оба конечных якоря `line` находятся на добавленных строках новой стороны diff и `abs(pred.line - truth.line) ≤ 2`. Находки вне добавленных строк не отбрасываются: они остаются FP и влияют на derived verdict из сырого ответа. Для диапазона `line` — конечный якорь, `start_line` проверяется как корректный диапазон, но не расширяет окно. Сначала ищется максимальное число пар один-к-одному; при равенстве выбирается минимальная суммарная дистанция строк, затем лексикографический порядок `(truth index, prediction index)` для воспроизводимости. Один prediction закрывает не более одного truth. Корпус не должен содержать две разные проблемы с неразличимыми `path/category/line ±2`.

Каждая пара = TP; непарная prediction = FP (включая дубликат и любую находку на чистом кейсе); непарный truth = FN. Неверная категория/путь/якорь даёт FP и FN. Severity пары сохраняется в отчёте как есть, не нормализуется: `critical` truth, совпавший с `high` prediction, считается найденным для Critical Recall, но severity mismatch виден отдельно. Невалидный raw response остаётся в знаменателе validity: его predictions не интерпретируются, все truths кейса дают FN, FP=0; такой кейс не может дать verdict agreement.

Суммировать TP/FP/FN **по всему корпусу** (micro), не усреднять проценты кейсов. `Precision = TP/(TP+FP)`, `Recall = TP/(TP+FN)`. `Critical Recall = matched critical truths / all critical truths`. По каждой категории truth показывать TP/FN, по категории prediction — FP; per-category Precision = `TP_c/(TP_c+FP_c)`, Recall = `TP_c/(TP_c+FN_c)`. Если знаменатель ноль, метрика `null`/«не определена», а не 0 или 100; счётчики всегда выводятся. `Validity = valid raw responses / all cases`; пустой корпус делает validity неопределённой. `Verdict agreement = cases with derived predicted verdict == expected_verdict / all cases`; невалидный ответ считается несогласием. Предсказанный verdict выводится из **сырого** набора findings по тому же severity-правилу, без поля модели.

### 3.2.1. Зафиксированный raw baseline #82 (2026-10-13)

Это историческое измерение; исходные raw bytes/digests доступны на
[`76df6d4`](https://github.com/larchanka-training/dmc-268-api-t6/tree/76df6d4d0f431ce4c02e6708410c000c66f6135a/test-prs-dataset/responses).
Baseline §3.2.2 заменил весь response set одной новой съёмкой без drift. Новый
sidecar содержит проверенные API-order prefixes при совпадающих PR head/revision:
CLEAN-03/LOG-03/SEC-04 по 1 сообщению, RES-03/SYN-04 по 20 и uncertainty marker.
Strict schema и canonical/active binding проверяются до calls/staging; все
canonical case.json identity inputs и shaping modules входят в static provenance.
Результаты ниже относятся только к capture 2026-10-13 и не измеряют новые
prefixes; новая полная съёмка описана в §3.2.2; текущий baseline — §3.2.3.

[Actions run 38468081001, attempt 1](https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/38468081001)
снял все 24 первых ответа на source SHA `80ede3cd274d745616b8a24082ff7d1d00f5b736`;
начало `2026-10-13T21:06:56.682693Z`. Engine `fast`, primary `mistral-small-4`,
configured fallback `mistral-small-3.2-24b`, configured provider `eurouter`.
Все первые вызовы — `answer` / `primary`, модель `mistral-small-4`, serving-provider
label `mistral`; все gateway statuses `accepted`, paid-metadata errors отсутствуют,
`baseline_publishable=true`. Prompt `review/prompts/review.system.v2.md` (`v2`),
SHA-256 `9177e358c23d1a7cd8987cbf667cf8b705c2afb27eaf8b2237480e4531431d75`.
Static digest `sha256-v1:9b28cf4c284ac07a03a5a35a62e3c2e48dc38264076a55d1a3d31724bacc2c17`,
corpus digest `sha256-v1:1ab8ca9a16cc50c357b39e1571c73b1e6674752737c444bd8b58d3f2aab81dfa`.

Пять real-кейсов в этой исторической съёмке получили проверенные offline snapshots сообщений точной
`source.revision` из `review/eval/pr-commit-messages.json`, а не восстановленный
полный исторический PR commit list. Все 19 synthetic-кейсов получили пустые commit
tuples; SEC-05 исключён. Patch/pre-image/truth не менялись. Все 24 raw ответа и
manifest скопированы целиком byte-for-byte; SHA-256/размер каждого ответа сверены
с safe metadata, оба manifest идентичны. Final validation: 24/24; два offline JSON
отчёта byte-identical, `warnings=[]`; отчёт после удаления validator diagnostics
byte-identical live safe report.

| Метрика | Измеренный raw baseline |
| --- | --- |
| Validity | 29.2% (7/24 первых ответов) |
| Micro TP / FP / FN | 2 / 2 / 17 |
| Precision | 50.0% (2/4 predictions) |
| Recall | 10.5% (2/19 truths) |
| Critical Recall | 40.0% (2/5 critical truths) |
| Verdict agreement | 16.7% (4/24 кейсов) |
| Severity mismatches | 1/2 matched findings: LOG-02, predicted high / truth critical |

| Категория | TP / FP / FN | Precision | Recall |
| --- | --- | --- | --- |
| security | 1 / 1 / 3 | 50.0% (1/2) | 25.0% (1/4) |
| correctness | 1 / 1 / 4 | 50.0% (1/2) | 20.0% (1/5) |
| performance | 0 / 0 / 5 | Не определена (0 predictions) | 0.0% (0/5) |
| readability | 0 / 0 / 5 | Не определена (0 predictions) | 0.0% (0/5) |

Невалидны 17/24 первых ответов: только custom-rule attribution prefix — 8;
prefix + `start_line >= line` — 4; prefix + range + severity/confidence ordering — 2;
только range — 3. Нет пустых/отсутствующих первых ответов или JSON parse failures.
Их predictions не интерпретируются; истины остаются FN, кейсы не исключаются из
знаменателя. Validity real — 0/5, synthetic — 7/19; по классам security 2/4,
resource 1/5, logic 1/5, syntax 1/5, clean 2/5. Полные case IDs, class counts,
проверка artifact hashes и сравнение с историческими Mistral/Nemotron измерениями
даны в [README корпуса](../test-prs-dataset/README.md#recorded-mistral-baseline--2026-10-13).

Исторический Mistral 2026-10-07: validity 5/24, TP/FP/FN 1/1/18, precision 1/2,
recall 1/19, critical recall 1/5, verdict agreement 2/24. Это два отдельных захвата
с разными static inputs; различия не доказывают причинное улучшение от commit
context. Ручной semantic/Hallucination Rate oracle не выполнялся; raw baseline
не устанавливает качество production verifier/final output и целевые quality gates.

Экспорт содержит configured цены USD/миллион input/cache-read/output tokens:
primary `0.56125 / 0.56125 / 2.35725`, fallback `0.2245 / 0.2245 / 0.449`;
nominal fast policy limit USD 0.50/кейс (USD 12 для 24 кейсов), 4 calls/attempt,
90s timeout. Полные настройки сохранены в manifest. Actual spend, billed-token
totals и остаток баланса в artifacts отсутствуют; они не выводятся из policy limits.

### 3.2.2. Зафиксированный PR-prefix raw baseline #82 (2026-10-14)

Исторический capture, заменённый текущим post-rebase baseline §3.2.3; original
raw bytes/manifest доступны на [revision bf09117](https://github.com/larchanka-training/dmc-268-api-t6/tree/bf0911713b3f530add49443b5b13df5720e6521e/test-prs-dataset/responses).

[Actions run 38560319651, attempt 1](https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/38560319651)
снял все 24 первых raw ответа на frozen source SHA `5173792fd2997d5401aed4b5787d536254249a26`;
начало `2026-10-14T11:58:17.004986Z`. Один согласованный платный capture,
engine `fast`, primary `mistral-small-4`, configured fallback `mistral-small-3.2-24b`,
configured provider `eurouter`, raw export включён. Пять real cases получают
проверенные API-order PR prefixes: CLEAN-03/LOG-03/SEC-04 по 1 сообщению,
RES-03/SYN-04 первые 20 плюс `[Additional commits may be omitted]`.
Все 19 synthetic histories пусты; SEC-05 исключён, corpus/truth не менялись.

Prompt v2 SHA-256 `9177e358c23d1a7cd8987cbf667cf8b705c2afb27eaf8b2237480e4531431d75`.
Static digest `sha256-v1:09c0c809ca0469f300c50c281848edded260fdbe3373d414e294bbb39c012736`,
corpus digest `sha256-v1:1ab8ca9a16cc50c357b39e1571c73b1e6674752737c444bd8b58d3f2aab81dfa`.
Все первые calls `answer`, модель `mistral-small-4`, serving-provider label `mistral`
(digest `sha256-v1:8e1af0131864f4488dc577f497afcf5c87b74b237e86f0322abf99e8c2ef018a`).
Gateway statuses `accepted`, paid-metadata errors отсутствуют,
`baseline_publishable=true`, nonpublishable list пуст. Все 25 файлов импортированы
byte-for-byte; hashes/sizes 24 ответов совпали с safe metadata, оба manifest
идентичны, SHA-256 `264dde15691f1fba3bdea64d81276c1d644c7820628e8a29e1a64860b4c94673`.
Final corpus 24/24, два replay JSON byte-identical/`warnings=[]`; redacted offline
report byte-identical live safe report. Поздние repair/fallback ответы не подставлялись.

| Метрика | Измеренный PR-prefix raw baseline |
| --- | --- |
| Validity | 33.3% (8/24) |
| Micro TP / FP / FN | 2 / 1 / 17 |
| Precision | 66.7% (2/3 predictions) |
| Recall | 10.5% (2/19 truths) |
| Critical Recall | 40.0% (2/5) |
| Verdict agreement | 16.7% (4/24) |
| Severity mismatches | 2/2: LOG-02/LOG-05, predicted high / truth critical |

Security TP/FP/FN 0/0/4; correctness 2/1/3; performance 0/0/5; readability 0/0/5.
Validity real 0/5, synthetic 8/19. Невалидные 16 первых ответов остаются в знаменателе;
их predictions не интерпретируются, truths остаются FN. Полные case IDs/class
counts приведены в [README корпуса](../test-prs-dataset/README.md#recorded-mistral-baseline--2026-10-14).
Сравнение с исторической snapshot съёмкой §3.2.1 не является controlled experiment
и не доказывает эффект commit context. Manual semantic/Hallucination Rate oracle
не выполнялся; production verifier/final-output quality оцениваются отдельно.

Configured цены/fast policy совпадают с §3.2.1 и полными настройками manifest:
nominal USD 0.50/кейс (USD 12 для 24), 4 calls/attempt, 90s timeout. Actual spend,
billed-token totals и остаток баланса отсутствуют в artifacts.

### 3.2.3. Зафиксированный post-rebase raw baseline #82 (2026-10-15)

Исторический baseline; текущая полная съёмка v3 — §3.2.4 (20.10.2026).
Original raw bytes и manifest сохранены [на pre-import revision](https://github.com/larchanka-training/dmc-268-api-t6/tree/47f9add7c4df2b7c32db0091d39296cdd2e4810c/test-prs-dataset/responses).

[Actions run 38701845865, attempt 1](https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/38701845865)
снял все 24 первых raw ответа на frozen source SHA `a1cc6229c6a6fdba65ecb640c60ca823e262a6aa`;
начало `2026-10-15T07:53:56.845426Z` (09:53:56 Europe/Warsaw). Это один дополнительно
разрешённый платный capture после rebase на main `f27d4c2ccd5854ac736ad8c0ecc1fe30d3b08140`:
merged #149 изменил pinned parser/schema ReviewOutput. Prompt, frozen API-order
PR prefixes и corpus/truth не менялись. Engine `fast`, primary `mistral-small-4`,
configured fallback `mistral-small-3.2-24b`, provider `eurouter`, raw export включён.
Real prefixes: CLEAN-03/LOG-03/SEC-04 по 1 сообщению, RES-03/SYN-04 первые 20 плюс
`[Additional commits may be omitted]`; 19 synthetic histories пусты, SEC-05 исключён.

Prompt v2 SHA-256 `9177e358c23d1a7cd8987cbf667cf8b705c2afb27eaf8b2237480e4531431d75`.
Static digest `sha256-v1:77d46f9c731dc26aded94c7ec60cb860314fe7835e6041a318d63b8ddc690449`,
corpus digest `sha256-v1:1ab8ca9a16cc50c357b39e1571c73b1e6674752737c444bd8b58d3f2aab81dfa`.
Все 24 первых calls — `answer`, primary `mistral-small-4`, serving-provider label
`mistral` (digest `sha256-v1:8e1af0131864f4488dc577f497afcf5c87b74b237e86f0322abf99e8c2ef018a`).
Gateway statuses `accepted`, paid-metadata errors отсутствуют,
`baseline_publishable=true`, nonpublishable list пуст. Все 25 файлов импортированы
вместе byte-for-byte: hashes/sizes ответов совпали с safe metadata, raw/safe
manifest идентичны, SHA-256 `c7f52e42004f2d1149b9d3dd80956eea1d3e17cee9fa93ff0e5625686c0694df`.
Final corpus 24/24; два replay JSON byte-identical, `warnings=[]`, redacted offline
report byte-identical live safe report. Repair/fallback не подменяют первый ответ.

| Метрика | Исторический raw baseline 2026-10-15 |
| --- | --- |
| Validity | 29.2% (7/24) |
| Micro TP / FP / FN | 2 / 2 / 17 |
| Precision | 50.0% (2/4 predictions) |
| Recall | 10.5% (2/19 truths) |
| Critical Recall | 40.0% (2/5) |
| Verdict agreement | 20.8% (5/24) |
| Severity mismatches | 1/2: LOG-02, predicted high / truth critical |

Security TP/FP/FN 1/1/3, precision 1/2, recall 1/4; correctness 1/1/4,
precision 1/2, recall 1/5; performance и readability по 0/0/5, precision не определена,
recall 0/5. Validity real 1/5, synthetic 6/19. Все 17 невалидных первых ответов
остаются в знаменателе: predictions не интерпретируются, truths остаются FN.
Case IDs и распределение по классам — [README корпуса](../test-prs-dataset/README.md#recorded-mistral-baseline--2026-10-15).
§3.2.1 и §3.2.2 сохраняют исторические capture/метрики/digests. Сравнение разных
captures не является controlled experiment и не доказывает качество или эффект
изменения parser/schema/commit context. Manual semantic/Hallucination Rate oracle
не выполнялся; production verifier/final-output quality оцениваются отдельно.

Effective settings совпадают с предыдущей согласованной конфигурацией (§3.2.1/§3.2.2):
nominal USD 0.50/кейс (USD 12 для 24), 4 calls/attempt, 90s timeout, полные
настройки в manifest. Configured цены/лимиты не измеряют actual spend,
billed-token totals или баланс — этих величин в artifacts нет.

### 3.2.4. Зафиксированный v3 raw baseline #178 (2026-10-20)

[Actions run 39443263258, attempt 1](https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/39443263258)
снял все 24 первых raw ответа на source `47f9add7c4df2b7c32db0091d39296cdd2e4810c`,
ветка `feat/178-first-answer-format`, начало `2026-10-20T14:53:06.772709Z`
(16:53:06 Europe/Warsaw). После публикации и remote verification candidate пользователь
разрешил «сделай 1 прогон» на предложенные nominal USD 12 / 24 кейса / прежнюю
пару моделей / raw export. Выполнен ровно один workflow без rerun.
Source включает merged #184 (#177 часть 2) и #133, main на момент capture —
`2326273e6ab7fcea89d1cf3b94b88a4be66ca5e9`. Пользователь осознанно выбрал запуск
до pending pinned-изменений #230/#229/#227; их последующий merge может дать drift,
новый платный прогон этим решением не разрешён. Required CI replay на main после
merge **ещё не выполнен**; локальный replay не закрывает эту часть AC3.

Engine `fast`, configured provider `eurouter`, primary `mistral-small-4`, fallback
`mistral-small-3.2-24b`, raw export включён. Corpus/truth и frozen PR prefixes не
менялись: 19 synthetic + 5 real, SEC-05 исключён. Prompt `review.system.v3`, SHA-256
`eb851e7fc2329eb7bdff015ec0fda945c46111ec5c884934ed6f6d6803883452`.
46 pins, static digest
`sha256-v1:387834e02e34318527ee2c48e25e2fb63743b65a6b33a5739c25c7b877d8ca3a`;
corpus digest
`sha256-v1:1ab8ca9a16cc50c357b39e1571c73b1e6674752737c444bd8b58d3f2aab81dfa`.
Все 24 first calls — answer/primary/mistral-small-4, serving-provider label mistral,
gateway accepted, paid-metadata errors false; publishable=true, nonpublishable IDs
пусты. Это проверка целостности, не порог качества и не число repair-вызовов.

Полные 25 файлов импортированы byte-for-byte после проверки safe/raw artifacts
`eval-live-39443263258-1` и `eval-live-raw-39443263258-1`. Все hashes/sizes совпали;
raw/safe manifest SHA-256
`1a6a2c6aea7bdea6026c592ef9e59c62823aefec7eac0279f125ff09d0e161a2`.
Final corpus validation 24/24, два offline report byte-identical, `warnings=[]`;
после удаления только validator diagnostics offline совпадает с live safe report
byte-for-byte. Raw-first validity измерена до нормализации/repair/fallback и verifier.

| Метрика | Исторический 2026-10-15 | Текущий 2026-10-20 |
| --- | --- | --- |
| Validity | 29.2% (7/24) | 41.7% (10/24) |
| Micro TP / FP / FN | 2 / 2 / 17 | 3 / 5 / 16 |
| Precision | 50.0% (2/4) | 37.5% (3/8) |
| Recall | 10.5% (2/19) | 15.8% (3/19) |
| Critical Recall | 40.0% (2/5) | 40.0% (2/5) |
| Verdict agreement | 20.8% (5/24) | 16.7% (4/24) |
| Severity mismatches | 1/2 matched | 2/3 matched: LOG-02 high/critical, LOG-04 high/medium |

| Category | TP | FP | FN | Precision | Recall |
| --- | --- | --- | --- | --- | --- |
| security | 1 | 0 | 3 | 100.0% (1/1) | 25.0% (1/4) |
| correctness | 2 | 5 | 3 | 28.6% (2/7) | 40.0% (2/5) |
| performance | 0 | 0 | 5 | undefined | 0.0% (0/5) |
| readability | 0 | 0 | 5 | undefined | 0.0% (0/5) |

| Case class | Validity | TP / FP / FN | Verdict agreement |
| --- | --- | --- | --- |
| security | 1/4 | 1 / 1 / 3 | 1/4 |
| resource | 2/5 | 0 / 2 / 5 | 0/5 |
| logic | 2/5 | 2 / 0 / 3 | 1/5 |
| syntax | 2/5 | 0 / 0 / 5 | 0/5 |
| clean | 3/5 | 0 / 2 / 0 | 2/5 |

Real validity 1/5, synthetic 9/19. 14 невалидных ответов остаются в знаменателе,
predictions не интерпретируются, truths остаются FN. Пересекающиеся причины:
27 attribution omissions в 12 кейсах, 14 equal ranges в четырёх, один order case;
reversed ranges, empty/missing answers, JSON parse и другие ошибки отсутствуют.
Все case IDs и непересекающиеся группы — [README корпуса](../test-prs-dataset/README.md#recorded-mistral-baseline--2026-10-20).

Результаты смешанные: validity/TP/recall выросли, FP выросли, precision и verdict
agreement снизились, critical recall прежний. Весь cohort сохранён, без
cherry-picking или повторной съёмки ради улучшения метрик. Разные captures не
являются controlled experiment; semantic improvement/Hallucination Rate,
production verifier/final-output quality и repair/latency/cost saving не измерены.
Effective settings полностью совпадают с историческим manifest (§3.2.3), включая
nominal USD 0.50/кейс (USD 12 для 24), 4 calls/attempt и 90s timeout.
Actual spend, billed tokens и баланс artifacts не сообщают. Verifier в этом legacy
eval не запускается; pins для неиспользуемого verifier не добавлены.

### 3.3. Ручной оракул семантики

Автоматическая пара по пути, категории и строке не доказывает, что модель поняла корневую причину. Куратор вручную отмечает для каждой находки groundedness (есть ли утверждаемый код/тип/API в переданном контексте), совпадение причины с truth и тон; отдельно проверяет корректность `suggestion` на новой версии. Галлюцинация — утверждение о несуществующем коде, типе, библиотеке или поведении; спорные случаи проходят вторую ручную оценку. `Hallucination Rate = число вручную подтверждённых галлюцинаций / число находок в валидных raw responses`; нулевой знаменатель — `null`. Отчёт хранит ручные решения и ссылки на контекст, не выдаёт автоматическое path/line совпадение за семантическую истину. Оракул качества не использует LLM judge; production verifier не заменяет независимую ручную разметку.

### 3.4. Независимая оценка production pipeline и verifier (#107)

Новый versioned harness сохраняет для каждого кейса policy/diff snapshots, фактически показанные reviewer/verifier manifests и excerpts, raw v2 output, deterministic drops, все входные candidate IDs, решения verifier и final result. Pin-ы включают schema, prompts, defaults, selector/AST/matcher/model/recovery profiles; provenance вызовов и usage позволяют воспроизвести replay без сети. Сырые исторические v1 ответы остаются прежним baseline: evidence для них не дорисовываются, новый результат не выдаётся за пересчёт старого измерения.

Сравнение raw и final выполняется на одном и том же зафиксированном ground truth, с отдельными TP/FP/FN, Precision/Recall, Critical Recall, validity и ручным Hallucination Rate для каждой стадии. Для RIGHT v2 применяется сопоставление §3.2; оно остаётся метрикой исходного корпуса с truth на добавленных строках, а не ограничением допустимости production-якоря на context line. Расширенные кейсы с допустимыми контекстными/общими находками получают отдельную versioned методику matching по показанным source ranges. Перед будущим v3 matching обязательно сравнивает `side`: LEFT ищется на удалённых строках `diff_old_sha`, RIGHT — на новой стороне; также различаются соответствующие old/new paths и revision. Находки разных сторон/ревизий не сопоставляются через одинаковый номер строки; историческое окно ±2 и его результаты не меняются задним числом.

Для final-метрик в знаменателе остаются все эталоны запланированного evaluation scope. Невалидный ответ, failed run, insufficient, deterministic drop и отсутствие анализа из-за summary-only не удаляют трудные кейсы: неполученные истины остаются FN; их причины показываются отдельно. Доля исключённого политикой scope и результаты сценариев all-ignored/summary-only публикуются отдельно от качества модели, с заранее объявленным составом выборки. `verdict=null` при partial — корректное поведение продукта; его нельзя превратить в clean ради verdict agreement или исключить без учёта из отчёта. Raw verdict agreement §3.2 сохраняется; для final отдельно показываются agreement на определённых verdict и доля null по полной выборке.

Для изолированной оценки verifier к входу после deterministic filters добавляется зафиксированный набор настоящих корректных кандидатов, правдоподобных ложных утверждений и случаев недостаточного контекста. Истинность кандидата, применимость policy и suggestion размечает человек по ground truth; отдельно отмечает достаточность точного показанного verifier-контекста до сопоставления с ответом проверяемой модели. Спорные случаи подтверждает второй эксперт. Недостаточный контекст не делает истинный дефект ложным и не исключает его из анализа потерь recall. Один и тот же verifier не генерирует ground truth и не судит собственное качество.

Набор сценариев верификации (`tests/test_reflection_evaluation.py`) включает:
- **Positive cases**: реальный дефект в видимом коде и соответствующее правило — verifier подтверждает статус `supported` с валидными `evidence_refs` и `requirement_refs`.
- **Negative cases (hallucinations)**: правдоподобное, но ложное утверждение, противоречащее видимому коду — verifier отклоняет с `contradicted`.
- **Inapplicable rule rejection**: находка ссылается на правило репозитория, которое не применимо к данному контексту или файлу — verifier отклоняет кандидата (`insufficient_context` / `contradicted`).
- **Gap rejection**: кандидат ссылается на строки вне показанного диапазона или контекст с разрывами — `verify()` отклоняет такой ответ verifier с `VerificationInvalidOutputError`.
- **Non-code rejection**: замечания по нерелевантным блокам фильтруются через проверку роли manifest (`VerificationInvalidOutputError`).

| Метрика | Числитель / знаменатель |
| --- | --- |
| `verifier_false_rejection_rate` | Реальные корректные кандидаты с `contradicted` / все корректные кандидаты, переданные verifier. |
| `verifier_unsupported_acceptance_rate` | Размеченные ошибочные кандидаты с `supported` / все ошибочные кандидаты на входе verifier. |
| Insufficient | Число `insufficient_context` / все входные кандидаты, дополнительно по ручным меткам истинности и достаточности входа; корректные кандидаты с insufficient учитываются в потере final recall. |
| Invalid verifier output | Невалидные фактические ответы / все verifier generation-вызовы; отдельно терминально неуспешные batches и причины. Невалидный batch не даёт частично принятых решений. |
| Failed / partial / null | Соответствующие runs/results / все запланированные runs; причины, исходные counts и coverage scope сохраняются. |
| Ресурсы | Количество/стоимость вызовов, p50/p95 времени каждой стадии и run; recovery, cache hit/miss, модель и размер выборки раздельно. |

Каждая доля сопровождается числителем, знаменателем и размером выборки; нулевой знаменатель — null. Дополнительно нужен разбор потерь raw → deterministic → verifier → final, чтобы улучшение Precision не скрывало потерю Recall. Потеря хотя бы одного размеченного critical из-за verifier разбирается вручную до включения профиля. Продуктовые ориентиры §5 сохраняются; численные quality gates нового verifier не объявляются достигнутыми без baseline и согласования техлида. В unit-тестах стаб-вызовы проверяют соблюдение контракта и формирование промпта; качество смысловых решений verifier измеряется исключительно через replay/eval на размеченном корпусе.

Adversarial eval использует пары одинакового дефекта/контекста с инъекцией и без неё и отдельные допустимые текстовые исключения. Измеряются навязанные ложные findings, подавленные реальные дефекты и нарушения законных исключений reviewer/verifier; отдельно проверяется перенос через conventions/cache/repair. Schema validity, отсутствие запрещённых tools и детерминированный guard оцениваются независимо от смысловой устойчивости модели. Результат корпуса не заявляет полной защиты от произвольного Prompt Injection.

**Защита от Prompt Injection на unit-уровне (AC 7)**:
Параметризованные тесты синтаксических инъекций (`TestPromptInjectionDefenses` в `tests/test_reflection_evaluation.py`) проверяют оба прохода (reviewer и verifier):
- Системный промпт верификатора (`review.verifier.v1.md`) содержит декларацию Source Authority and Trust с наивысшим приоритетом над пользовательскими данными (добавление аналогичной декларации в обновлённый системный промпт reviewer отложено до задачи #130 в рамках Option (a)).
- Все недоверенные поля (`rules_markdown`, `agents_md`, `candidate_id`, `path`, `title`, `body`, `suggestion`, `code_diff`) экранируются по стандарту XML-сущностей (`&lt;`, `&gt;`, `&amp;`, `&quot;`, `&apos;`).
- Атрибуты XML строго валидируются: идентификаторы `service-defaults:<ID>` ограничены регулярным выражением `\A[a-zA-Z0-9_\-]+\Z`, исключая кавычки и инъекции атрибутов.
- Тесты проверяют отсутствие неэкранированных тегов `<system>`, попыток преждевременного закрытия контейнеров (`</candidate_findings>`, `</code_context>`) и маркеров инструкций (`<|im_start|>`, `[INST]`).


### 3.5. Вклад графа RAG v1 и гейт v2 (#146)

Решение — [ADR 0001](adr/0001-graph-rag-v1.md#12-замер-вклада-и-гейт-v2), правила
контекста — [CONTEXT_AND_VERIFICATION_SPEC §3.5](CONTEXT_AND_VERIFICATION_SPEC.md#35-кандидаты-графа-символов-rag-v1).
Метрика и порог ниже объявлены до замера #146 и после него не меняются.

Функциональные сценарии (unit/contract и integration, добавляются вместе с #141–#145):

| Сценарий | Ожидаемый результат |
| --- | --- |
| Fencing: задача скачала архив, затем выключили тумблер или удалили App/репозиторий | Очистка (`FOR UPDATE`) повышает поколение; пакетная запись устаревшей задачи (`FOR SHARE`) видит другое поколение и не пишет ни одной строки, без retry; публикация после этого невозможна |
| Повторная доставка задачи того же репозитория; лимит 1 задачи на установку и 2 всего; истёкший lease или lease старого поколения | Дубль не захватывает живой lease и не пишет; лишняя задача ждёт; истёкший или старый lease перехватывается; публикация с чужим, отозванным или истёкшим lease или с номером не выше `published` отклоняется; промах compare-and-set ставит новое пробуждение, а не retry; нет строки состояния — задача прекращается без записи |
| Брошенная попытка: опубликован C1 (№1); попытка №2 для C2 пишет строки `foo` и умирает; C3 возвращает `foo` к C1 и меняет только `bar`; incremental №3 сравнивает C1 с C3. Отдельно: пакетная запись прежнего владельца после перехвата его lease; вариант с full №3 | №3 сразу после захвата, до скачивания архива, удаляет строки №2, а compare-and-set отклоняется, пока остаток есть; после публикации видимый индекс совпадает с full на C3 (строк `foo` из C2 нет); поздняя пакетная запись прежнего владельца отклонена сверкой token; при full №3 нижняя граница скрывает строки №2, сборка мусора удаляет их |
| Задача зависла дольше своего lease, перехвата не было; сборка мусора отозвала lease и удалила часть её строк; задача проснулась | Её следующая пакетная запись и compare-and-set отклонены (lease отозван или истёк по `clock_timestamp()`), публикации нет; остаток дочищает сборка мусора или следующая задача сразу после захвата, при full его скрывает нижняя граница |
| Сборка мусора против захвата сразу после истечения lease; транзакция compare-and-set, зависшая через момент истечения | Каждая порция сборки мусора перечитывает строку состояния под `FOR SHARE` и не трогает номер нового живого lease; истёкший lease отозван до удаления его строк, и compare-and-set зависшей задачи отклонён по token; неполный индекс не публикуется |
| Очистка по `installation_repositories.removed`, `installation.deleted`, выключению тумблера и TTL 30 дней (чтения при ревью и постановка full; incremental не продлевает) | В транзакции события поколение повышено, статус `empty`, `purge_reason` записан, публикация и lease сняты — ни одна строка не читается, в том числе до физического DELETE; DELETE по `repository_id` делает сборка мусора, которая идёт каждый интервал и продолжается на следующих тиках лидера, пока не вычистит всё; цель — в пределах часа; строки других репозиториев не затронуты; TTL-задача перепроверяет `last_used_at` под `FOR UPDATE`; leader-сверка не поднимает индекс после TTL |
| Мерж PR и push в default branch; другие события PR, push в другую ветку или тег, push, удаливший ветку; невалидные `default_branch` и `merge_commit_sha` в payload PR | Мерж пишет цель — merge commit, push — `after`, оба ставят маркер пробуждения; два мержа до рассылки дают одно incremental-задание до второй цели; остальные события строку и маркер не трогают; невалидное поле читается как отсутствующее и не отклоняет payload; сбой запроса индекса при push не отменяет чтение правил (`test_code_index_lifecycle_postgres.py::test_ac1_two_merges_before_the_dispatch_wake_one_incremental_build_up_to_the_second`, `::test_ac1_other_pull_request_events_leave_the_row_and_the_marker_alone`, `::test_ac7_a_push_to_the_default_branch_sets_the_target_and_the_marker`; `test_github_pr_projection.py::test_an_invalid_index_only_field_reads_none_and_never_rejects_the_payload`; `test_github_installation_dispatch.py::test_a_failed_index_request_is_logged_and_the_rules_refresh_still_runs`) |
| Сверка с HEAD: прямой push, смена default branch, потерянное пробуждение (`empty` и `ready`), HEAD равен проиндексированному; триггер между чтением HEAD и записью; медленное чтение HEAD; терминальный отказ (политика, доступ) и временный код на том же HEAD; потерянное пробуждение нового HEAD после отказа | Маркер и цель — живой HEAD, смена ветки — full с `BRANCH_CHANGED`; равный HEAD — без пробуждения; цель триггера, поставленная после чтения, не переписывается; терминальный отказ на HEAD, равном цели, — без пробуждения, на новом HEAD, ветке или версиях — с пробуждением, временный код — с пробуждением на каждом проходе; другая цель (триггер или сверка) снимает отказ, и сверка будит новый HEAD, даже если пробуждение триггера потерялось, а запись той же цели код сохраняет; сверка идёт последней, берёт остаток бюджета тика и не начинает репозиторий, если осталось меньше 2 с, а остальные задачи не голодают; после таймаута сверка снова идёт на следующем тике, а сборка мусора ждёт свой интервал (`test_code_index_lifecycle_postgres.py::test_ac2_a_changed_default_branch_is_reconciled_into_a_full_build_of_the_new_branch`, `::test_ac3_the_reconcile_wakes_a_direct_push_and_leaves_an_equal_head_alone`, `::test_ac3_the_reconcile_recovers_an_index_whose_wake_up_was_lost`, `::test_ac3_the_reconcile_rebuilds_a_policy_refusal_once_and_then_only_on_a_new_head`, `::test_ac3_a_push_after_a_policy_refusal_is_rebuilt_by_the_reconcile_when_its_wake_up_is_lost`; `test_code_index_store_postgres.py::test_reconcile_head_keeps_the_target_of_a_trigger_after_its_head_read`, `::test_reconcile_head_retries_a_terminal_refusal_only_on_a_new_head_branch_or_version`, `::test_a_new_target_clears_a_terminal_refusal_so_a_lost_wake_up_is_recovered`, `::test_a_write_of_the_same_target_keeps_a_terminal_refusal`; `test_code_index_leader_tasks.py::test_a_slow_reconcile_runs_last_on_what_is_left_and_never_starves_the_other_tasks`, `::test_after_a_timeout_only_a_task_that_continues_runs_again_on_the_next_tick`) |
| Сходимость: переустановка тем же аккаунтом (#113), повторный `added`, живой индекс при выключенном RAG; повторное включение против выключения тумблера; очистка против PATCH `ragEnabled=true` | Повторное включение — новое поколение и full; живой индекс без RAG очищается с `disabled` или `removed`; повторное включение ждёт `FOR SHARE` и индекс не воскрешает; гонка с PATCH заканчивается либо очисткой и повторным включением, либо живым индексом, но никогда живым индексом при выключенном RAG (`test_code_index_lifecycle_postgres.py::test_ac5_a_reconnected_repository_is_re_armed_and_built_in_full`, `::test_converge_racing_a_patch_that_turns_rag_on_never_leaves_a_live_index_with_rag_off`; `test_code_index_maintenance_postgres.py::test_a_rearm_waits_for_a_toggle_off_and_does_not_resurrect_the_index`, `::test_converge_purges_live_indexes_whose_rag_is_off_and_gc_leaves_no_rows`) |
| Outbox пробуждений: push между пробуждением и снятием маркера; триггер, пока задание держит lease | Compare-and-set не снимает более новый маркер, следующая рассылка будит снова, индекс доходит до нового HEAD; маркер под живым lease ждёт и уходит после публикации, второе задание — incremental (`test_code_index_lifecycle_postgres.py::test_a_push_between_a_wake_up_and_its_clear_keeps_the_marker_for_the_next_dispatch`, `::test_a_trigger_during_a_job_is_woken_once_the_job_has_published`; `test_code_index_store_postgres.py::test_clear_wake_request_clears_only_the_marker_it_read`) |
| Гонки тумблера и порядка: запрос обновления против очистки; выключение и включение во время задания; push в обратном порядке; force push; включение у репозитория не на GitHub | Запрос ждёт `FOR UPDATE` очистки, возвращает False и маркера не ставит; задание отстранено, остаются строки только нового поколения; индекс не откатывается назад; force push — full с `NOT_AHEAD`; включение не на GitHub индекса не создаёт (`test_code_index_lifecycle_postgres.py::test_an_index_request_racing_a_purge_waits_for_its_lock_and_requests_nothing`, `::test_ac8_off_and_on_during_a_job_fence_it_out_and_only_the_new_generation_remains`, `::test_ac9_a_late_push_never_moves_the_index_back`, `::test_ac9_a_force_push_builds_in_full`; `test_repository_rag_postgres.py::test_rag_on_of_a_repository_outside_github_creates_no_index`) |
| Чтение графа во время публикации или сборки мусора | Retriever видит одну публикацию целиком: `published`, поколение, нижняя граница и строки прочитаны в одной транзакции REPEATABLE READ; вытесненные строки удаляются не раньше чем через 10 мин |
| Retry run после и до checkpoint кандидатов/manifest (CONTEXT_AND_VERIFICATION_SPEC §7.4) | После checkpoint блоки RAG переиспользуются без нового чтения индекса; до него могут быть пересобраны по более новой публикации |
| Incremental после force push, смены default branch, `go.mod`, >500 файлов, смены ignore, версии встроенных исключений или экстрактора | Full вместо incremental; `indexed_commit_sha = C` только когда на `C` проиндексировано всё разрешённое дерево, кроме файлов, пропущенных по лимитам и ignore |
| Добавлен файл, который раньше не разрешался импортом другого файла | Импортёр найден по сохранённой неразрешённой записи импорта и переизвлечён |
| Индексация: rules.md доказанно отсутствует; пуст; невалидный UTF-8/тип/лимит/ignore; временно недоступен | Отсутствие/пустой файл дают пустые пользовательские маски с built-ins; ошибка не становится missing, попытка не публикует индекс нового HEAD с пустой политикой; retry по классу ошибки (#142, RFS §5.1, ADR §9) |
| Тумблер включён, индекс `empty`/`building`/`failed` или в закреплённом промпте нет секции retrieved | Ревью без RAG и без ожидания, trace `rag: not_ready` с причиной (статус индекса или `prompt_version`) |
| Ребро не подтверждается резолвером на ревизии PR; кандидат отсутствует на ней | Omission `rag_edge_unconfirmed` / `rag_candidate_missing`, блока нет |
| Экспорт есть в извлечении `diff_old_sha` и пропал на `head_sha` (удалён, переименован, файл удалён или перемещён); импорт был `resolved` на `diff_old_sha` и тем же спецификатором остаётся на `head_sha` с исходом `missing`, `external`, `unsupported`/`ambiguous` или модулем без имени | Кандидат `rag_broken_reference`: два блока, RIGHT на `head_sha` и LEFT на `diff_old_sha`, без склейки ревизий; импорт, сломанный ещё до PR, кандидатом не становится |
| Блок с ревизией индекса вместо ревизии PR | `policy_context_violation` до отправки |
| Превышение лимита индексации, небезопасные entries архива | `failed` с `limit_exceeded:<лимит>`; entry с `..`, абсолютным путём, hardlink, устройством или FIFO — `failed` с `archive_unsafe`; symlink не индексируется; без retry, публикация не используется, ревью без RAG |
| Прочий терминальный отказ задачи после retry | Последняя публикация читается (`stale`), на строке состояния `last_error_code` |
| Неожиданное исключение задачи индексации на любой попытке, где задача ещё держит lease (best effort: если снятие lease не удалось, код не пишется); отмена задачи | Lease снят, строка `ready`/`empty`, `last_error_code = internal_error`, `error_code` не задан, метрики задачи записаны; следующая публикация код сбрасывает, а любой исход без публикации (UNCHANGED, EMPTY, REWAKE) и отменённая задача — нет (известный предел). Отмена снимает lease без кода, прежний `last_error_code` остаётся |
| Отказ по квоте хранения при claim (`limit_exceeded:repository_storage`, `limit_exceeded:workspace_storage`) | `failed` с кодом квоты, `github_request_count`, `downloaded_bytes` и `build_duration_ms` равны 0, а не значениям прошлой задачи; `mark_failed` без статистики оставляет записанные |
| Инъекция внутри извлечённого кода | Блок передаётся типизированной недоверенной секцией `retrieved_excerpt` и не меняет роли, schema или адресата; влияние на ответ модели измеряется в корпусе ниже и по §3.4 |
| Тумблер RAG: репозиторий до ревизии `20261016_0033`, новый репозиторий, повторный онбординг включённого, переустановка App | `rag_enabled = false` после миграции и у нового; повторный онбординг и переустановка сохраняют `true` |
| `GET`/`PATCH /api/repos/{id}` с `ragEnabled`; чужой репозиторий; недоступная установка; `{"ragEnabled": null}` и `ragIndex` в теле | По умолчанию `ragEnabled: false` и `ragIndex.status = disabled` в detail и в списке; PATCH пишет предпочтение, включение без строки состояния даёт `empty`; чужой — 404, строка не меняется; недоступная установка — `false` и `disabled`, сохранённое предпочтение остаётся; недопустимые тела — 422 без записи |
| Статус индекса по строке `code_index_states`: нет строки, `building`, `ready` без target, `ready` с target ≠ indexed, `failed` (`limit_exceeded:symbols`), `ready` с `last_error_code`, тумблер выключен при `ready` | `empty`, `building`, `ready`, `stale`, `failed` с кодом `error_code`, `ready` с этим кодом, `disabled`; commit, время и счётчики — только у `ready`/`stale`, иначе null и 0 |
| Блок prompt в system, user или дополнительном сообщении; ответ без `usage`; расход до лимита run | Оценка входа учитывает блок в любой позиции; ровно одно событие `usage_events` этого run с `tokens_in` по оценке; вызов на границе лимита проходит, сверх неё — `budget_exceeded` без запроса; `context.build` пишет `tokens_by_role` |

Корпус для замера — не меньше 24 кейсов, каждый со снимком репозитория на ревизии
кейса (§4): 10 дефектов, видимых только через обратное ребро (3 из них — удалённые
или переименованные экспорты); 6 контрольных, где достаточно L4; 4 глубины 2
(ожидаемые промахи v1, задел для v2); 2 с инъекцией внутри извлечённого кода; 2
без дефектов. Разметка и сопоставление — по §3.2–3.3. Ground truth и якоря
находок остаются на добавленных строках RIGHT файлов diff: извлечённый код —
только evidence, ручной оракул §3.3 считает показанные блоки `retrieved_excerpt`
контекстом для groundedness. Три кейса удалённого или переименованного экспорта
имеют RIGHT-якорь (переименование либо удаление, при котором в изменённом модуле
остаются изменённые RIGHT-строки), ground truth — на этой строке. Чисто удаляющие
кейсы в recall гейта не входят: ReviewOutput v2 не выражает их якорь
(`left_anchor_unsupported`, [ADR 0001 §8.3](adr/0001-graph-rag-v1.md#83-удалённый-или-переименованный-экспорт)).

A/B на одном корпусе: одна версия промпта (при выключенном RAG она рендерит пустую
секцию retrieved) и одна модель; плечо A без RAG, плечо B с RAG; offline replay с
замороженным снимком индекса; 3 прогона на плечо. Метрика вклада графа — ΔRecall
(B − A) на подмножестве ровно из 10 кейсов обратного ребра, без кейсов глубины 2;
Recall — micro по ожидаемым находкам этих кейсов, усреднённый по 3 прогонам плеча.

Гейт открытия v2 (#147) — должны выполниться все условия:

| Условие | Порог |
| --- | --- |
| ΔRecall на подмножестве обратных рёбер | ≥ +15 п. п. |
| Кейсы, где B находит дефект не меньше чем в 2 из 3 прогонов, а A пропускает не меньше чем в 2 из 3 | ≥ 3 |
| Precision на всём корпусе | падает не больше чем на 5 п. п. |
| Critical Recall на всём корпусе | не ниже, чем в плече A |
| Hallucination Rate на всём корпусе | растёт не больше чем на 2 п. п. |
| Медиана входных токенов на run | не больше +30 % к плечу A |
| Исполненные инъекции из извлечённого кода | 0 |

Это решение о гейте, а не обязательная проверка CI: режимы §5 не меняются. Гейт не
пройден — #147 закрывается как not planned со ссылкой на отчёт #146.

## 4. Эталонные данные и исполнение

Целевой каталог — `test-prs-dataset/cases/<id>/` с metadata JSON, `diff.patch` и pre-image; raw responses и manifest версионируются отдельно. Минимальный шаблон metadata (форма в [case schema](../test-prs-dataset/schema/case.schema.json)): `id`, `class`, `language`, `source {kind, url, revision, license_name, license_url, attribution}`, `base_path`, `patch_path`, `expected_verdict`, `expected_findings [{path, start_line, line, severity, category}]`. Для synthetic source URL/license могут быть неприменимы, но attribution обязателен. Отдельный response manifest после baseline хранит `model_id`, `prompt_sha`, `prompt_version`, metadata прогона и путь raw response. В case README фиксируются замены категорий и ручной оракул.

Кейсам RAG v1 (§3.5) pre-image и patch недостаточно: каждый дополнительно хранит снимок разрешённого дерева репозитория на ревизии кейса и замороженный снимок индекса, построенный по этому дереву, чтобы обратные рёбра и replay были воспроизводимы без сети. Для real PR лицензия и attribution должны покрывать весь снимок, а не только показанный excerpt. Формат снимков закрепляет #146.

Старые UI TC-01…06 — идеи для переработки в корректные pre-image/patch и канонические verdict; их YAML и patch не считаются валидным корпусом. TC-07 становится unit-тестом фильтра generated files, TC-08 — webhook integration, TC-09 — sandbox phase 3. Старый UI каталог `tests/golden_dataset/` уже удалён ([ui#69](https://github.com/larchanka-training/dmc-268-ui-t6/pull/69)): ui `docs/TEST_PLAN.md` отсылает к этому плану, последняя версия старого плана — [ui `d8c9172`](https://github.com/larchanka-training/dmc-268-ui-t6/blob/d8c9172/docs/TEST_PLAN.md).

## 5. Критерии приёмки и отчётность

| Проверка | Цель | Режим в этом спринте |
| --- | --- | --- |
| Corpus schema и patch apply | 100% кейсов; лицензии/ground truth подтверждены вручную | Обязательный gate. |
| Validity, micro Precision/Recall, Critical Recall, per-category, verdict agreement | Числа и provenance в PR summary и воспроизводимом JSON | Отчёт; качество модели не валит required check. |
| Precision ≥ 85%, Critical Recall ≥ 75%, Hallucination Rate < 3% | Продуктовые ориентиры из [SD §13](SYSTEM_DESIGN.md#13-нефункциональные-требования) | Отчёт; ручной hallucination oracle обязателен для заявления метрики. |
| Coalescing: публикация разрешена только для актуального head/diff при проверке перед POST; webhook HMAC/idempotency, batch publication и классификация ошибок 422 | Интеграционные тесты с реальными PG/RabbitMQ и fake GitHub | Обязательные функциональные gates после реализации зависимостей. Окно между проверкой и POST — принятый остаточный риск ([SD §13](SYSTEM_DESIGN.md#13-нефункциональные-требования), решение техлида 05.10.2026). |
| #107: policy/ignore/snapshots, AST fallback, manifests/evidence, verifier reserve/recovery, coverage и publication recovery | Сценарии §2.7, без запрещённой отправки и неподтверждённых findings | Целевые функциональные gates вместе с реализацией; документация не подтверждает прохождение. GitLab/LEFT активируются отдельно со сквозными контрактами. |
| #107: raw и final quality, false rejection/unsupported acceptance, insufficient, invalid/failed/partial, cost/latency | Независимый корпус и ручной оракул §3.4, versioned provenance и все знаменатели | Отдельный отчёт нового harness, не подмена legacy benchmark; baseline и новые численные gates согласуются до включения профиля. |
| Sandbox isolation 100% | Сеть/секреты/FS из §2.4 | Фаза 3; текущий спринт не заявляет прохождение. |
| DiffEngine p95 ≤ 40 с, webhook ACK p95 < 500 мс | Измерения на указанной staging выборке | Отчёт с размером выборки и окружением. |

Существующий required CI запускает schema/apply и кодовые gates; legacy raw replay выполняется всегда: без закоммиченного response manifest job падает, а job summary называет причину. Некорректный набор или сломанный replay завершают job ошибкой, значения quality metrics только выводятся; расхождения текущих входов с manifest выводятся как warning и job не валят: SHA промпта (`Prompt SHA mismatch for …`), static digest (`Static input digest mismatch; …`, при смене набора static-файлов — `Static input path set changed; …`) и corpus digest (`Corpus input digest mismatch; …`). Integration suite в required CI работает с реальными PostgreSQL и RabbitMQ и проверяет обработку сообщений: полный путь текущего Run `queued → running → publishing → succeeded` через worker с fake GitHub и fake ReviewModel, сквозную сборку `run_worker` с LLM gateway, где GitHub и модель подменены через `httpx.MockTransport` (`test_main_composition_reviews_through_the_llm_gateway`), retry-очереди и DLQ, восстановление Run реконсилером после падения worker (смоделировано отменой consumer-задач и истечением lease), отмену устаревшего Run новым push до вызова модели и во время публикации (новый `head_sha` записывается напрямую, без webhook), топологию очередей, а также публикацию rerun и сигнала cancel (`tests/test_worker_queue_postgres.py`, `tests/test_rerun_run_postgres.py`, `tests/test_cancel_run_postgres.py`). Эти существующие тесты не доказывают прохождение новых требований §2.7. Live eval — отдельный ручной job, результаты сохраняются с model/prompt provenance. Перед выпуском сверить этот план с критериями [api#30](https://github.com/larchanka-training/dmc-268-api-t6/issues/30), включая устаревшее там `review_requested`: действующий триггер задан Р-10 и [PIPELINE_SPEC §8](PIPELINE_SPEC.md#8-триггер-р-10-d2); формулировку issue нужно согласовать с владельцем.
