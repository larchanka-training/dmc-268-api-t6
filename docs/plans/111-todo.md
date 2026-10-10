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
