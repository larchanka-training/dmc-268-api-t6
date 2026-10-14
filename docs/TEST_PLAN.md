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
| End-to-end | GitHub App, webhook, очереди, API, UI, публикация и изоляция арендаторов. | Автоматического полного e2e нет ни в api, ни в ui. api: HTTP-smoke вебхука (подпись HMAC, дубль delivery; p95 задержки ACK только печатается, порога нет) — обязательный job `Webhook container smoke` в PR CI на собранном образе с PostgreSQL: `uv run --locked python scripts/webhook_smoke.py --url http://127.0.0.1:8000/webhooks/github --count 50`; локально `uv run --env-file .env python scripts/webhook_smoke.py` с тем же `GITHUB_WEBHOOK_SECRET`, что у API, и только против одноразового стенда, не staging и не prod. Полный путь, включая UI, — вручную на staging (VPS): тестовые PR и установки staging GitHub App, без реальных секретов в фикстурах или логах. Sandbox isolation — фаза 3. |

API baseline gates локально и в обязательном CI: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy .`, `uv run pytest`. UI gates: `pnpm lint`, `pnpm check-types`, `pnpm format:check`, `pnpm test`, `pnpm build`. Для integration сервисы должны быть доступны; отсутствие `TEST_DATABASE_URL` даёт skip локально, но не считается успехом обязательного integration job. Staging smoke измеряет p95 ACK < 500 мс и p95 чистого времени DiffEngine ≤ 40 с на определённом наборе PR; окно, объём выборки и измерение записываются с результатом.

**Локальный стенд.** PostgreSQL и RabbitMQ для integration берутся из `docker-compose.yml`; рецепт совпадает с [README, Tests](../README.md#tests): `docker compose up -d --wait postgres rabbitmq`, затем отдельный vhost `test` — `docker compose exec rabbitmq rabbitmqctl add_vhost test` и `docker compose exec rabbitmq rabbitmqctl set_permissions -p test app '.*' '.*' '.*'`, затем `TEST_DATABASE_URL=postgresql+psycopg://app:app@localhost:5432/app TEST_RABBITMQ_URL=amqp://app:app@localhost:5672/test uv run pytest -m integration -rs`. `TEST_DATABASE_URL` нужен каждому integration-тесту: тест создаёт случайную схему, мигрирует и удаляет только её. `TEST_RABBITMQ_URL` нужен тестам worker, rerun и cancel (`tests/test_worker_queue_postgres.py`, `tests/test_rerun_run_postgres.py`, `tests/test_cancel_run_postgres.py`): они удаляют и заново объявляют топологию очередей, поэтому им нужен свой vhost, не `/`, где потребляет compose `worker`. Без переменной соответствующие тесты локально skipped, а не падают. Только при `GITHUB_ACTIONS=true` эти три файла берут `RABBITMQ_URL`, если `TEST_RABBITMQ_URL` не задан: так их запускает job `Python lint / type / test`, где PostgreSQL и RabbitMQ — service containers. Его шаг «Integration suite (zero skips)» запускает `uv run pytest -m integration -q -rs` и падает, если integration-тестов нет или хоть один skipped.

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

`POST /api/runs/{id}/rerun` (T3) создаёт новый `queued` Run на текущем SHA с `trigger=rerun` без проверки лейбла/CI; активный Run либо закрытый PR дают 409, нет авторизации — 401, чужой Workspace — 404. Клиент UI проверяет callback failure/state mismatch, один общий refresh при конкурентных 401 и ровно один retry исходных запросов, logout при неудачном refresh, восстановление сессии через refresh+me. Контракт Finding→ReviewComment проверяет однострочный и диапазонный D6-якорь, severity/category и состояния Run.

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
| 4 000 ignored + 50 allowed строк; 3 000/3 001 allowed; все ignored; неполная пагинация files | Обычное ревью 50 и 3 000 строк; summary-only для 3 001/неполного inventory без LLM; all ignored → skipped. UI patch сохраняется; `review_patch=null` не восстанавливается из БД как code для модели. |
| Неподдерживаемый путь рядом с обычным файлом: backslash, управляющий символ, недекодируемое имя, превышение 1 024 UTF-8 байт или 128 сегментов | `unsupported_path` изолирует только проблемный источник, без «исправления» имени. Остальной допущенный файл проверяется; coverage partial, при пустых findings или только info verdict null. Содержимое и автоматически добавленное имя пропущенного файла не попадают в модель, включая metadata, conventions, AST, verifier, repair и fallback; сводка сообщает пропуск, диагностическое имя выводится безопасно и с ограничением длины. Неизвестный счётчик пропущенного файла остаётся null и сам по себе не включает summary-only. |
| Полный inventory: только unsupported пути либо unsupported + достоверно ignored; отдельно все ignored и неполный inventory | В первых двух случаях 0 LLM-вызовов, пустые findings, coverage none с причиной unsupported_path, verdict null; сохранённый backend-результат проходит публикацию и даёт succeeded/neutral. Это не all_changes_ignored и само по себе не summary-only. Все достоверно ignored без unsupported → skipped; неполный inventory сохраняет summary-only/partial. |
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

## 3. Методология LLM eval

### 3.1. Корпус и вход оценки

Корпус содержит 24 независимых PR-кейса (допустимо 20–30): четыре класса безопасности и по пять кейсов ресурсов, логики, синтаксиса и чистых изменений; не менее пяти licensed real PR (хотя бы один на класс), Python и TypeScript/React, не менее пяти отдельных `critical` ground truths. Слот SEC-05 исключён после остановки автоматическим фильтром и не входит в знаменатель replay/live оценки. Отображение класса в основную категорию: безопасность → `security`, ресурсы/память → `performance` (либо `correctness` при конкретном сбое), логика → `correctness`, синтаксический overhead → `readability`, чистое изменение → без findings. Исключения записываются в metadata и README.

Каждый кейс имеет JSON metadata/ground truth, исходное состояние файла (pre-image), применимый unified `.patch` и источник; raw responses хранятся отдельно от кейса, в `test-prs-dataset/responses/`. Для real PR обязательны URL PR/commit, revision, название и URL лицензии, attribution и ручная проверка права на конкретный excerpt. Ground truth использует `path`, `start_line`, `line`, `severity`, `category` из Finding; `start_line: null` означает одну строку, иначе `< line`. Ожидаемый `verdict` выводится из severity по [PIPELINE_SPEC §11](PIPELINE_SPEC.md#11-вердикт-и-бейджи-d3): `critical/high → blocking`, иначе `medium/low → attention`, иначе `clean`. Case schema собственная; [ReviewOutput schema](../review/schemas/review-output.schema.json) применяется только к ответу модели. `git apply --check` выполняется на копии pre-image, truth якорится в добавленных строках новой стороны; answer-hint комментарии запрещены.

Оценивать **сырой** ответ модели до post-processing, hunk-фильтра, severity threshold, production verifier, cap inline и публикации: иначе качество промпта скрывается правилами выпуска. Replay читает committed raw response без сети и ключа, с model ID, prompt version/SHA и run metadata. Для существующего legacy harness валиден только ответ, для которого `uv run python review/scripts/validate_findings.py <response.json>` завершился кодом 0 **и** напечатал `OK ReviewOutput`; exit 1/2 и `RepoConventionsDraft` невалидны. Это не проверка целевого v2 с evidence/manifests. Live использует PromptBuilder и `review/prompts/`, выбранную модель и внешние credentials, сохраняет raw output и ту же provenance для replay. Live запускается вручную (§2, `eval-live.yml`); закоммиченный baseline — ответы и `manifest.json` в `test-prs-dataset/responses/`, его модель и provenance описаны в [README корпуса](../test-prs-dataset/README.md); историческая съёмка 2026-10-13 использовала одиночные revision snapshots (результат §3.2.1); полная замена всех 24 ответов с проверенными PR prefixes сохранена как исторический baseline §3.2.2; текущий post-rebase baseline после merged #149 — §3.2.3. LLM-as-a-judge для оценки этого raw benchmark не используется. Обязательный production verifier по #107 — часть продукта, качество которого оценивается отдельно (§3.4).

### 3.2. Однозначное сопоставление и счётчики

Для каждой валидной находки и эталона одного кейса ребро допустимо, когда совпадают **`path` и `category`**, оба конечных якоря `line` находятся на добавленных строках новой стороны diff и `abs(pred.line - truth.line) ≤ 2`. Находки вне добавленных строк не отбрасываются: они остаются FP и влияют на derived verdict из сырого ответа. Для диапазона `line` — конечный якорь, `start_line` проверяется как корректный диапазон, но не расширяет окно. Сначала ищется максимальное число пар один-к-одному; при равенстве выбирается минимальная суммарная дистанция строк, затем лексикографический порядок `(truth index, prediction index)` для воспроизводимости. Один prediction закрывает не более одного truth. Корпус не должен содержать две разные проблемы с неразличимыми `path/category/line ±2`.

Каждая пара = TP; непарная prediction = FP (включая дубликат и любую находку на чистом кейсе); непарный truth = FN. Неверная категория/путь/якорь даёт FP и FN. Severity пары сохраняется в отчёте как есть, не нормализуется: `critical` truth, совпавший с `high` prediction, считается найденным для Critical Recall, но severity mismatch виден отдельно. Невалидный raw response остаётся в знаменателе validity: его predictions не интерпретируются, все truths кейса дают FN, FP=0; такой кейс не может дать verdict agreement.

Суммировать TP/FP/FN **по всему корпусу** (micro), не усреднять проценты кейсов. `Precision = TP/(TP+FP)`, `Recall = TP/(TP+FN)`. `Critical Recall = matched critical truths / all critical truths`. По каждой категории truth показывать TP/FN, по категории prediction — FP; per-category Precision = `TP_c/(TP_c+FP_c)`, Recall = `TP_c/(TP_c+FN_c)`. Если знаменатель ноль, метрика `null`/«не определена», а не 0 или 100; счётчики всегда выводятся. `Validity = valid raw responses / all cases`; пустой корпус делает validity неопределённой. `Verdict agreement = cases with derived predicted verdict == expected_verdict / all cases`; невалидный ответ считается несогласием. Предсказанный verdict выводится из **сырого** набора findings по тому же severity-правилу, без поля модели.

### 3.2.1. Зафиксированный raw baseline #82 (2026-10-13)

Это историческое измерение; исходные raw bytes/digests доступны на
[`76df6d4`](https://github.com/larchanka-training/dmc-268-api-t6/tree/76df6d4d0f431ce4c02e6708410c000c66f6135a/test-prs-dataset/responses).
Baseline §3.2.2 заменяет весь response set одной новой съёмкой без drift. Новый
sidecar содержит проверенные API-order prefixes при совпадающих PR head/revision:
CLEAN-03/LOG-03/SEC-04 по 1 сообщению, RES-03/SYN-04 по 20 и uncertainty marker.
Strict schema и canonical/active binding проверяются до calls/staging; все
canonical case.json identity inputs и shaping modules входят в static provenance.
Результаты ниже относятся только к capture 2026-10-13 и не измеряют новые
prefixes; новая полная съёмка описана в §3.2.2.

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

| Метрика | Текущий измеренный raw baseline |
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

### 3.3. Ручной оракул семантики

Автоматическая пара по пути, категории и строке не доказывает, что модель поняла корневую причину. Куратор вручную отмечает для каждой находки groundedness (есть ли утверждаемый код/тип/API в переданном контексте), совпадение причины с truth и тон; отдельно проверяет корректность `suggestion` на новой версии. Галлюцинация — утверждение о несуществующем коде, типе, библиотеке или поведении; спорные случаи проходят вторую ручную оценку. `Hallucination Rate = число вручную подтверждённых галлюцинаций / число находок в валидных raw responses`; нулевой знаменатель — `null`. Отчёт хранит ручные решения и ссылки на контекст, не выдаёт автоматическое path/line совпадение за семантическую истину. Оракул качества не использует LLM judge; production verifier не заменяет независимую ручную разметку.

### 3.4. Независимая оценка production pipeline и verifier (#107)

Новый versioned harness сохраняет для каждого кейса policy/diff snapshots, фактически показанные reviewer/verifier manifests и excerpts, raw v2 output, deterministic drops, все входные candidate IDs, решения verifier и final result. Pin-ы включают schema, prompts, defaults, selector/AST/matcher/model/recovery profiles; provenance вызовов и usage позволяют воспроизвести replay без сети. Сырые исторические v1 ответы остаются прежним baseline: evidence для них не дорисовываются, новый результат не выдаётся за пересчёт старого измерения.

Сравнение raw и final выполняется на одном и том же зафиксированном ground truth, с отдельными TP/FP/FN, Precision/Recall, Critical Recall, validity и ручным Hallucination Rate для каждой стадии. Для RIGHT v2 применяется сопоставление §3.2; оно остаётся метрикой исходного корпуса с truth на добавленных строках, а не ограничением допустимости production-якоря на context line. Расширенные кейсы с допустимыми контекстными/общими находками получают отдельную versioned методику matching по показанным source ranges. Перед будущим v3 matching обязательно сравнивает `side`: LEFT ищется на удалённых строках `diff_old_sha`, RIGHT — на новой стороне; также различаются соответствующие old/new paths и revision. Находки разных сторон/ревизий не сопоставляются через одинаковый номер строки; историческое окно ±2 и его результаты не меняются задним числом.

Для final-метрик в знаменателе остаются все эталоны запланированного evaluation scope. Невалидный ответ, failed run, insufficient, deterministic drop и отсутствие анализа из-за summary-only не удаляют трудные кейсы: неполученные истины остаются FN; их причины показываются отдельно. Доля исключённого политикой scope и результаты сценариев all-ignored/summary-only публикуются отдельно от качества модели, с заранее объявленным составом выборки. `verdict=null` при partial — корректное поведение продукта; его нельзя превратить в clean ради verdict agreement или исключить без учёта из отчёта. Raw verdict agreement §3.2 сохраняется; для final отдельно показываются agreement на определённых verdict и доля null по полной выборке.

Для изолированной оценки verifier к входу после deterministic filters добавляется зафиксированный набор настоящих корректных кандидатов, правдоподобных ложных утверждений и случаев недостаточного контекста. Истинность кандидата, применимость policy и suggestion размечает человек по ground truth; отдельно отмечает достаточность точного показанного verifier-контекста до сопоставления с ответом проверяемой модели. Спорные случаи подтверждает второй эксперт. Недостаточный контекст не делает истинный дефект ложным и не исключает его из анализа потерь recall. Один и тот же verifier не генерирует ground truth и не судит собственное качество.

| Метрика | Числитель / знаменатель |
| --- | --- |
| `verifier_false_rejection_rate` | Реальные корректные кандидаты с `contradicted` / все корректные кандидаты, переданные verifier. |
| `verifier_unsupported_acceptance_rate` | Размеченные ошибочные кандидаты с `supported` / все ошибочные кандидаты на входе verifier. |
| Insufficient | Число `insufficient_context` / все входные кандидаты, дополнительно по ручным меткам истинности и достаточности входа; корректные кандидаты с insufficient учитываются в потере final recall. |
| Invalid verifier output | Невалидные фактические ответы / все verifier generation-вызовы; отдельно терминально неуспешные batches и причины. Невалидный batch не даёт частично принятых решений. |
| Failed / partial / null | Соответствующие runs/results / все запланированные runs; причины, исходные counts и coverage scope сохраняются. |
| Ресурсы | Количество/стоимость вызовов, p50/p95 времени каждой стадии и run; recovery, cache hit/miss, модель и размер выборки раздельно. |

Каждая доля сопровождается числителем, знаменателем и размером выборки; нулевой знаменатель — null. Дополнительно нужен разбор потерь raw → deterministic → verifier → final, чтобы улучшение Precision не скрывало потерю Recall. Потеря хотя бы одного размеченного critical из-за verifier разбирается вручную до включения профиля. Продуктовые ориентиры §5 сохраняются; численные quality gates нового verifier не объявляются достигнутыми без baseline и согласования техлида.

Adversarial eval использует пары одинакового дефекта/контекста с инъекцией и без неё и отдельные допустимые текстовые исключения. Измеряются навязанные ложные findings, подавленные реальные дефекты и нарушения законных исключений reviewer/verifier; отдельно проверяется перенос через conventions/cache/repair. Schema validity, отсутствие запрещённых tools и детерминированный guard оцениваются независимо от смысловой устойчивости модели. Результат корпуса не заявляет полной защиты от произвольного Prompt Injection.

### 3.5. Вклад графа RAG v1 и гейт v2 (#146)

Решение — [ADR 0001](adr/0001-graph-rag-v1.md#12-замер-вклада-и-гейт-v2), правила
контекста — [CONTEXT_AND_VERIFICATION_SPEC §3.5](CONTEXT_AND_VERIFICATION_SPEC.md#35-кандидаты-графа-символов-rag-v1).
Метрика и порог ниже объявлены до замера #146 и после него не меняются.

Функциональные сценарии (unit/contract и integration, добавляются вместе с #141–#145):

| Сценарий | Ожидаемый результат |
| --- | --- |
| Fencing: задача скачала архив, затем выключили тумблер или удалили App/репозиторий | Очистка повышает поколение; устаревшая задача не пишет ни одной строки и не повторяется; публикация после этого невозможна |
| Очистка по `installation_repositories.removed`, `installation.deleted`, выключению тумблера и TTL 30 дней | Все строки индекса репозитория удалены явным DELETE по `repository_id`, статус `empty`; строки других репозиториев не затронуты |
| Incremental после force push, смены default branch, >500 файлов, смены ignore или версии экстрактора | Full вместо incremental; `indexed_commit_sha = C` только при полностью проиндексированном дереве на `C` |
| Тумблер включён, индекс `empty`/`building`/`failed` | Ревью без RAG и без ожидания, trace `rag: not_ready` с причиной |
| Ребро не подтверждается резолвером на ревизии PR; кандидат отсутствует на ней | Omission `rag_edge_unconfirmed` / `rag_candidate_missing`, блока нет |
| Удалённый экспорт всё ещё импортируется на `head_sha` | Кандидат `rag_broken_reference`: два блока, RIGHT на `head_sha` и LEFT на `diff_old_sha`, без склейки ревизий |
| Блок с ревизией индекса вместо ревизии PR | `policy_context_violation` до отправки |
| Превышение лимита индексации, небезопасные entries архива | `failed` с `limit_exceeded:<лимит>`, без retry; entries с `..`, абсолютным путём, symlink, hardlink, устройством, FIFO не индексируются; ревью без RAG |
| Инъекция внутри извлечённого кода | Блок передаётся типизированной недоверенной секцией `retrieved_excerpt` и не меняет роли, schema или адресата; влияние на ответ модели измеряется в корпусе ниже и по §3.4 |

Корпус для замера — не меньше 24 кейсов, каждый со снимком репозитория на ревизии
кейса (§4): 10 дефектов, видимых только через обратное ребро (3 из них — удалённые
или переименованные экспорты); 6 контрольных, где достаточно L4; 4 глубины 2
(ожидаемые промахи v1, задел для v2); 2 с инъекцией внутри извлечённого кода; 2
без дефектов. Разметка и сопоставление — по §3.2–3.3.

A/B на одном корпусе: одна версия промпта и одна модель; плечо A без RAG, плечо B
с RAG; offline replay с замороженным снимком индекса; 3 прогона на плечо. Метрика
вклада графа — ΔRecall (B − A) на подмножестве кейсов с обратным ребром.

Гейт открытия v2 (#147) — должны выполниться все условия:

| Условие | Порог |
| --- | --- |
| ΔRecall на подмножестве обратных рёбер | ≥ +15 п. п. |
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
