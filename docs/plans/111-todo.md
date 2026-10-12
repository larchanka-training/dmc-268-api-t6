# Задачи #111: worker PORTAL_URL

План: [111-plan.md](111-plan.md). Baseline `origin/main`:
`92250a1fb3b65f88a2ea111d33613f9a4af1d1d2` (10.10.2026).

## Перед development

- [x] Пользователь утвердил план agent-loop («делай»).
- [x] Изолированная ветка `fix/111-worker-portal-url` создана от актуального
  `origin/main`; commits PR #121 не включены.
- [x] Developer получил пути plan/todo и рабочего checkout.
- [x] Повторно проверено владение файлами открытых PR.

Evidence 10.10.2026: `HEAD=92250a1fb3b65f88a2ea111d33613f9a4af1d1d2`,
ветка `fix/111-worker-portal-url`; `gh pr list --state open --json number,title,files`
вернул #108, #121, #122, #123, #124, без пересечения с файлами Task 1.

## Task 1: PORTAL_URL доставляется только review-worker

**Описание:** включить публичную Environment variable в существующий bundle
и разрешить её запись в `worker.env` без изменения Compose/Python.

**Acceptance criteria:**

- [x] `.github/workflows/ci-cd.yml` читает `${{ vars.PORTAL_URL }}` и включает
  `PORTAL_URL` в `names`; `env-file.sh` разрешает ключ в `WORKER_ENV_KEYS`.
- [x] Тест выполняет реальный bundle-step и host deploy, получает буквальный
  `PORTAL_URL=https://staging-ui.example.test` в `worker.env`; значение не
  попадает в `app.env`, `api.env` и `webhook-worker.env`.
- [x] Незаданное и пустое значение не создают ключ в env-файлах; существующие
  проверки ролей, masking, mode 0600 и rollback остаются зелёными.

**Verification:**

- [x] RED: зафиксированы failing tests на baseline до исправления.
- [x] GREEN: `uv run pytest tests/test_deploy_staging_services.py`.
- [x] Review Standards/Spec: ноль findings после исправлений.

Независимые reports Task 1 (10.10.2026): Standards — **0 findings**, доставка и
тесты соответствуют существующим паттернам; Spec — **0 findings**, реальная
цепочка workflow → bundle → allowlist → worker проверена, live AC явно pending.

RED 10.10.2026: `uv run pytest tests/test_deploy_staging_services.py -k portal_url_from_ci`
на baseline после добавления теста, до правки workflow/allowlist: **1 failed,
42 deselected**. Настоящий bundle-step и deploy завершились успешно, но
`worker.env` содержал `{}` вместо `{"PORTAL_URL": "https://staging-ui.example.test"}`.
После минимальной правки тот же тест: **1 passed, 42 deselected**.
GREEN: `uv run pytest tests/test_deploy_staging_services.py` — **45 passed in 16.27s**.
Новый URL включён также в существующие проверки masking, mode 0600 и ручного/
автоматического rollback; отсутствие и пустое значение проверяются отдельно.

Гейты checkpoint Task 1 (10.10.2026): `uv run ruff check .` — All checks passed;
`uv run ruff format --check .` — 346 files already formatted;
`uv run mypy .` — no issues in 343 source files;
`uv run pytest` — **2260 passed, 128 skipped, 2 warnings in 72.49s**.
128 skipped — PostgreSQL/broker integration tests без `TEST_DATABASE_URL`/
`TEST_RABBITMQ_URL`; это не evidence live AC. Два предупреждения — существующие
deprecations Starlette/httpx и anyio BlockingPortal. После Task 2 гейты выполняются
повторно для окончательного checkpoint.

**Dependencies:** утверждение и изоляция.

**Files likely touched:**

- `.github/workflows/ci-cd.yml`
- `deploy/scripts/env-file.sh`
- `tests/test_deploy_staging_services.py`

**Estimated scope:** Medium, 3 файла.

## Task 2: оператор знает источник и адрес PORTAL_URL

**Описание:** описать настройку и её проверку в существующих документах выката.

**Acceptance criteria:**

- [x] SECRETS.md определяет публичную variable `PORTAL_URL` Environment
  `staging`, правильный UI-origin и получателя `worker.env` → review-worker;
  опциональность runtime и необходимость значения для staging-ссылки явны.
- [x] CICD.md описывает настройку до штатного выката, путь workflow/bundle/
  allowlist, отсутствие ключа при пустом значении и сохранение env при rollback.
- [x] Документы дают воспроизводимый live-check summary `/runs/{run_id}`
  со ссылкой на свежий check-run и не считают существующий корневой details_url
  достаточным evidence.

**Verification:**

- [x] Сверка двух документов с фактическим именем, источником и получателем.
- [x] Все четыре uv-гейта выполнены; их реальные результаты записаны.
- [x] Свежий независимый Standards/Spec review: ноль findings.

Финальные независимые reports (10.10.2026): Standards — **0 findings**,
включая документированные пути, Compose и rollback; Spec — **0 implementation
findings**, внешний live AC явно pending.

Evidence Task 2 (10.10.2026): SECRETS.md и CICD.md сверены с
`vars.PORTAL_URL`, `names`, `WORKER_ENV_KEYS`, Compose `worker.env` и
`run_url_factory`; Bash syntax check прошли 3 новых блока команд, jq-проверка
summary приняла fixture с совпадающими Run UUID/head/URL. Эти проверки не
выполняли выкат или live review. Владение docs повторно проверено по открытым
PR #108, #121, #122, #123, #124: пересечений нет.

Финальные гейты после Task 2: `uv run ruff check .` — All checks passed;
`uv run ruff format --check .` — 346 files already formatted;
`uv run mypy .` — no issues in 343 source files;
`uv run pytest` — **2260 passed, 128 skipped, 2 warnings in 97.27s**.
Skipped: 128 PostgreSQL/broker integration tests без `TEST_DATABASE_URL`/
`TEST_RABBITMQ_URL`. Warnings: существующие deprecations Starlette/httpx и
anyio BlockingPortal. Live AC остаётся pending до штатного merge/deployment.

**Dependencies:** Task 1.

**Files likely touched:**

- `docs/SECRETS.md`
- `docs/CICD.md`

**Estimated scope:** Small, 2 файла.

## Checkpoint: delivery кода и PR

- [x] Все implementation tasks завершены и review reports без findings.
- [x] `uv run ruff check .` зелёный.
- [x] `uv run ruff format --check .` зелёный.
- [x] `uv run mypy .` зелёный.
- [x] `uv run pytest` зелёный; skipped integrations отдельно перечислены.
- [x] Ветка актуальна относительно main; повторное review требуется, если
  rebase меняет проверенный diff.
- [ ] Publisher проверил status, staged explicit new paths / `git add -u`,
  создал Conventional Commit с `(#111)` без `--no-verify`, push и draft PR
  (пользователь выбрал «Черновик PR»).
- [ ] PR содержит What / Why / How to verify / Refs, `Refs #111`, `Refs #106`,
  результаты проверок и pending-статус live AC, без closing keyword.
- [ ] PR URL возвращён координатору и прикреплён к этому чату.

Evidence перед публикацией 10.10.2026: `git fetch origin main` подтвердил
`HEAD=origin/main=92250a1fb3b65f88a2ea111d33613f9a4af1d1d2`; rebase не нужен.
Повторная проверка открытых PR #108, #121, #122, #123, #124 не обнаружила
пересечений с семью файлами этого PR. Пункты commit/push/PR отмечаются только
по фактическому результату публикации; live AC остаётся pending.

## Операционная часть: полный AC issue (внешняя зависимость)

Эти пункты не подменять успехом unit-тестов. PR публикуется перед защищённым
merge/deployment; пока этой зависимости нет, live acceptance остаётся pending.

- [x] Environment `staging` variable задана:
  `PORTAL_URL=https://staging-ui.dmc268-t6.axyi.ru`; значение прочитано обратно.
- [ ] Ответственный участник получил approve на текущем head, разрешены треды,
  выполнен штатный merge в main и зелёный `Deploy staging`.
- [ ] Проверен фактический `PORTAL_URL` review-worker после выката.
- [ ] Новый sandbox review завершён с настоящим GitHub App/LLM на staging.
- [ ] Summary check-run содержит точный UI URL `/runs/{external_id}`;
  открытая страница относится к этому Run UUID.
- [ ] Приложены check-run URL, Run UUID, SHA/digest staging и результат проверки.
- [ ] Техлид отразил приёмку в #106 и на доске 12; issue закрывает техлид.

**Dependencies:** разрешённые доступы к Environment, staging, sandbox и порталу;
доступные кредиты LLM; штатный reviewed merge/deployment. При отсутствии доступа
или deployment записать причину и handoff, оставив соответствующие чекбоксы пустыми.

Evidence 10.10.2026 (координатор): публичная variable Environment `staging`
задана через `gh variable set`; чтение обратно подтвердило буквальное значение
`PORTAL_URL=https://staging-ui.dmc268-t6.axyi.ru`. Выкат и live sandbox AC ещё pending.

## Коррекционный раунд PR #125 — 12.10.2026

Review baseline `f5a1e56`; исторические evidence выше не заменяют проверки
текущего head. Пользователь прямо поручил исправить review через agent-loop;
дополнительное утверждение не требуется.

- [x] Координатор выполнил rebase: `HEAD=a8259ba` поверх `origin/main=46a9077`.
- [x] Владение документацией согласовано комментариями в PR #126
  (`issuecomment-6121802405`) и PR #136 (`issuecomment-6121802800`).

### Correction Task 1: P3-1 — unit-регрессии env → URL

**Acceptance criteria:**

- [x] Новый `tests/test_worker_portal_url.py` проверяет буквальный
  `https://ui.test/runs/<фиксированный UUID>` для URL без завершающего слеша,
  с одним и несколькими слешами; `None` и `""` дают `None`.
- [x] `WorkerSettings.from_environment` получает литеральный `PORTAL_URL`
  из mapping с обязательными `DATABASE_URL`/`RABBITMQ_URL`; отсутствие ключа
  и пустое значение дают `settings.portal_url is None`.
- [x] Изолированное mutation-RED ловит все три замеченные мутации:
  `/runs/` → `/run/`, удаление `rstrip("/")`, `PORTAL_URL` → `PORTAL_URI`.
  В рабочем checkout production `app/worker.py` остаётся неизменённым.

**Verification:** `uv run pytest tests/test_worker_portal_url.py` зелёный
на немутированном коде; RED-команды, failures и GREEN-результат записаны ниже
по факту. Никаких сетевых вызовов или интеграционных сервисов не требуется.

**Dependencies:** rebase. **Scope:** Small, один новый тестовый файл.

### Correction Task 2: P3-2 — checklist и compose-комментарий

**Acceptance criteria:**

- [x] `deploy/compose/staging.yml`: комментарий `worker.env` включает
  «LLM gateway and portal links» без изменения сервисной конфигурации.
- [x] `docs/SECRETS.md` §2 п.3: variables checklist перечисляет
  опциональный `PORTAL_URL` для ссылки на Run.

**Verification:** сверка с таблицей env-получателей, workflow `vars.PORTAL_URL`
и существующим URL-контрактом; `uv run pytest tests/test_deploy_staging_services.py`.

**Dependencies:** согласование владения выполнено; независима от Task 1.
**Scope:** Small, два файла.

### Checkpoint: актуальный head и существующий PR

- [x] Focused tests и mutation-RED/GREEN записаны по фактическим результатам.
- [x] `uv run ruff check .`.
- [x] `uv run ruff format --check .`.
- [x] `uv run mypy .`.
- [x] `uv run pytest`; актуальные counts/skips/warnings записаны отдельно.
- [x] Свежие Standards/Spec reviews без findings; correction tasks выполнены.
- [ ] Publisher staged только изменения PR #125, сохранил чужие untracked
  планы, создал Conventional Commit с `(#111)` без `--no-verify` и push.
- [ ] В What названы `docs/plans/111-plan.md` и `docs/plans/111-todo.md`;
  How to verify/counts и rebase-checkbox обновлены на текущий head.
- [ ] Review получил ответ с SHA исправления; resolving оставлен reviewer.
- [x] Live sandbox AC явно pending до approve/merge/Deploy staging;
  handoff требует `succeeded` Run, check-run URL, Run UUID, ссылку из summary
  и SHA/digest staging. Merge и исправление дрейфа PIPELINE_SPEC не выполнялись.

Evidence текущего раунда дописывается после выполнения; история 10.10.2026
не переписывается.

Evidence Correction Tasks 1–2 (12.10.2026): новый focused-набор содержит
**8 passed in 0.52s**, `tests/test_deploy_staging_services.py` —
**45 passed in 22.14s**. Проверены публичные seams `run_url_factory` и
`WorkerSettings.from_environment` на литералах без сетевых вызовов и сервисов.
Комментарий Compose и variables checklist сверены с таблицей `worker.env`,
`vars.PORTAL_URL` workflow и `run_url_factory`; конфигурация сервисов не менялась.

Изолированное RED: `uv run python /tmp/pr125-mutation-check.py run-path trailing-slash`
копирует `app/` и новый тест в отдельные `/tmp/pr125-mutation-*` и запускает там
`uv run --project <checkout> python -m pytest -c <copy>/pyproject.toml
<copy>/tests/test_worker_portal_url.py`. `/runs/` → `/run/`: **3 failed**;
удаление `rstrip("/")`: **2 failed, 1 passed**. После этого немутированный первый
slice дал **3 passed**. Следующий slice закрепил пустые значения и чтение env:
`uv run python /tmp/pr125-mutation-check.py portal-key` с `PORTAL_URL` →
`PORTAL_URI` дал **1 failed, 7 passed**; немутированный набор — **8 passed**.
Все failures — assertion failures ожидаемых URL/`settings.portal_url`, а не
ошибки импорта или инфраструктуры. `git diff -- app/worker.py` пустой.

Логи: `/tmp/pr125-mutation-run-path.log`,
`/tmp/pr125-mutation-trailing-slash.log`, `/tmp/pr125-mutation-portal-key.log`,
`/tmp/pr125-url-slice-green.log`, `/tmp/pr125-worker-portal-url.log`,
`/tmp/pr125-deploy-staging-services.log`.

Гейты lint/format/typecheck: Ruff — **All checks passed**;
format — **354 files already formatted**; mypy — **no issues in 351 source files**.
Formatter исправил перенос decorator нового теста перед финальными гейтами.
Логи: `/tmp/pr125-ruff-check.log`, `/tmp/pr125-ruff-format.log`,
`/tmp/pr125-mypy-final.log`. Команды используют
`UV_CACHE_DIR=/tmp/pr125-uv-cache`: стандартный uv cache недоступен sandbox.
`TEST_DATABASE_URL` и `TEST_RABBITMQ_URL` отсутствуют (проверены boolean-значения,
`/tmp/pr125-integration-env.log`); контейнеры других PR не использовались.

Первый полный pytest в sandbox: **2 failed, 2305 passed, 145 skipped,
2 warnings, 3 errors in 82.12s**. Пять случаев
`tests/test_webhook_smoke_script.py` не смогли bind localhost HTTP server:
`PermissionError: [Errno 1] Operation not permitted`; результат не считается
зелёным gate. Лог сохранён в `/tmp/pr125-pytest-sandbox.log`. Повторный полный
pytest выполнен с разрешением localhost sockets, без изменения кода,
без `TEST_DATABASE_URL`/`TEST_RABBITMQ_URL`; его фактический результат ниже.

Независимые correction reviews (12.10.2026), результаты переданы координатором:
**Standards — 0 findings; Spec — 0 findings** на текущих тестах и документации.

Финальный полный gate `uv run pytest`: **2310 passed, 145 skipped,
2 warnings in 89.51s**, exit 0; `/tmp/pr125-pytest.log`.
145 skips — PostgreSQL/broker integrations без `TEST_DATABASE_URL` и
`TEST_RABBITMQ_URL`; предупреждения — существующие deprecations
Starlette/httpx и anyio BlockingPortal. `git diff --check` прошёл;
`git diff --exit-code -- app/worker.py` подтвердил отсутствие production-правок.
Чужие untracked планы #113/#117 сохранены; commit/push не выполнял developer.
Live sandbox AC остаётся pending после штатного reviewed merge/Deploy staging.
