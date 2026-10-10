# План #111: ссылка на прогон в check-run staging

## Цель и исходное состояние

[Issue #111](https://github.com/larchanka-training/dmc-268-api-t6/issues/111)
требует доставить `PORTAL_URL` в review-worker, описать источник настройки и
подтвердить ссылку `{PORTAL_URL}/runs/{run_id}` живым sandbox-прогоном.
Связанный трекер: [#106, часть 5](https://github.com/larchanka-training/dmc-268-api-t6/issues/106).

План подготовлен 10.10.2026 на основании удалённого `main`
`92250a1fb3b65f88a2ea111d33613f9a4af1d1d2`. Текущий checkout — чистая ветка
`fix/109-superseded-ci-run`, commit `eb2c02a`, открытый PR #121. Её изменения
не должны попасть в PR #111.

Подтверждённая цепочка сбоя:

- `WorkerSettings.from_environment` уже читает `PORTAL_URL`.
- `run_url_factory` уже формирует `/runs/{run_id}` и удаляет завершающий `/`
  у базового адреса; без настройки возвращает `None`.
- `check_run_view` уже добавляет ссылку в summary завершившегося успешного
  или неуспешного прогона. Правка Python-кода для исправления доставки не нужна.
- Workflow «Bundle application secrets» не содержит `PORTAL_URL` ни в `env`,
  ни в `names`; `WORKER_ENV_KEYS` в `env-file.sh` также его не допускает.
- По чтению GitHub variables настройка отсутствует и в repository, и в
  Environment `staging`; одного изменения workflow недостаточно.
- `APP_DOMAIN` repository сейчас равен `dmc268-t6.axyi.ru`; правильный UI-origin
  staging — `https://staging-ui.dmc268-t6.axyi.ru`.

## Решения

1. Источник — публичная variable `PORTAL_URL` в GitHub Environment `staging`.
   Workflow читает `${{ vars.PORTAL_URL }}`. Такое явное значение подходит и
   курсовому VPS, и Terraform-хосту с отдельно размещённым UI. Repository variable
   может служить общим значением; Environment перекрывает её обычным механизмом
   GitHub. Не выводить адрес UI из API health URL.
2. Передать значение существующим `APP_SECRETS_B64`-бандлом и allowlist строго
   в `worker.env`. Compose уже подключает этот файл только review-worker.
   Новые файлы env, настройки сервисов и зависимости не нужны.
3. Сохранить нынешнюю семантику незаданного/пустого значения: ключ отсутствует
   в env-файле, worker продолжает запускаться без ссылки. Для staging оператор
   задаёт адрес UI явно; документация описывает результат отсутствия настройки.
4. Не расширять задачу до изменения GitHub `details_url`: AC требует ссылку
   конкретного прогона в summary. Текущее API check-run её уже поддерживает.
5. Регрессию проверять выполнением настоящего bundle-step и deploy-скриптов
   через существующий fake Docker CLI, с буквальным URL в ожидаемом результате.
   Не заменять проверку реальной доставки только поиском строки в YAML.

## Изоляция и владение файлами

Перед development координатор выбирает отдельный checkout или чистую ветку
`fix/111-worker-portal-url` от актуального `origin/main`. Безопасный простой
вариант при отсутствии других писателей: сохранить два plan-файла вне checkout,
обновить `origin/main`, создать новую ветку от него и вернуть plan-файлы.
Текущая ветка #109 и её commits остаются на месте. Если checkout занят, создать
отдельный worktree через инструменты приложения и передать developer его путь.
Не начинать ветку #111 от HEAD текущей ветки.

Список открытых PR проверен: #108, #121, #122, #123, #124. Они не владеют
`.github/workflows/ci-cd.yml`, `deploy/scripts/env-file.sh`,
`tests/test_deploy_staging_services.py`, `docs/SECRETS.md`, `docs/CICD.md`
или plan-файлами #111. Перед правкой и публикацией проверить владение повторно.
Если появится пересечение, сначала оставить требуемый правилами комментарий
в соответствующем PR; не менять принадлежащие ему файлы молча.

## Задачи разработки

Подробные acceptance criteria и отметки выполнения — в [111-todo.md](111-todo.md).

1. **Доставка конфигурации, RED → GREEN** (3 файла): failing-регрессия
   workflow → bundle → `worker.env`; затем `vars.PORTAL_URL`, `names` и
   `WORKER_ENV_KEYS`. Проверить отсутствие настройки и распределение по ролям.
2. **Документы оператора** (2 файла): добавить `PORTAL_URL` в SECRETS.md и
   CICD.md, указать Environment, адрес UI, путь доставки, поведение при пустом
   значении, сохранение при rollback и рецепт живой проверки.
3. **Гейты, review и публикация**: четыре обязательных uv-гейта,
   независимые Standards/Spec reviews; после устранения findings Conventional
   Commit, push и PR с `What` / `Why` / `How to verify` / `Refs #111` и
   `Refs #106`, без автозакрытия. Новых зависимостей нет.

После задачи 1 — focused-тесты и review; после задачи 2 — полный checkpoint
и свежий review. Publisher работает только после нулевых findings.

## Операционная проверка AC

Отдельный шаг, обязательный для полной приёмки issue:

1. После одобрения плана задать публичную variable Environment `staging`
   `PORTAL_URL=https://staging-ui.dmc268-t6.axyi.ru` и прочитать значение обратно.
   Не печатать секреты и не извлекать их из GitHub.
2. Изменённый workflow выкатывается штатно после approve на текущем head,
   разрешения тредов и merge в защищённый `main` ответственным участником.
   PR CI не выкатывает staging; branch policy Environment допускает только main.
   Не обходить политику ради получения evidence.
3. После зелёного `Deploy staging` проверить фактический `PORTAL_URL` в
   review-worker. Получение доступа к хосту может требовать оператора: GitHub
   secrets недоступны для чтения, а SSH-доступ пока не подтверждён.
4. Запустить один обычный оплачиваемый review в подключённом sandbox
   `axyi/dmc268-t6-sandbox` с GitHub App и дождаться завершения. Существующий
   старый check-run не считается проверкой новой конфигурации.
5. Получить check-run через GitHub API; проверить, что `output.summary`
   содержит буквально `https://staging-ui.dmc268-t6.axyi.ru/runs/{external_id}`,
   где `external_id` — UUID проверяемого Run. Открыть ссылку и проверить страницу
   этого Run; при необходимости требуется авторизованная сессия портала.
6. Приложить ссылку `html_url` check-run, SHA/digest staging, Run UUID и
   результат проверки к PR/приёмке. Статус #106 и доски 12 обновляет техлид
   по проверенному результату; issue закрывает техлид.

Публикация PR завершает delivery кода, но не подтверждает второй AC issue.
До выката и sandbox-evidence живую проверку помечать pending. Если доступа нет,
передать конкретные команды и ожидаемый URL оператору, явно указав отсутствие
evidence. Полную задачу не объявлять завершённой лишь по unit-тестам.

## Проверки и ограничения

- Focused: `uv run pytest tests/test_deploy_staging_services.py`.
- Обязательные: `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy .`, `uv run pytest`.
- Запуск тестов до GREEN обязателен: новая проверка должна воспроизвести
  отсутствие передачи `PORTAL_URL` на baseline.
- Требуемые интеграционные тесты могут skip без `TEST_DATABASE_URL`; записать
  количество skipped и не представлять их как успешную живую проверку.
- Никаких миграций, новых пакетов, изменений Python бизнес-логики или API/UI
  контракта не требуется.
- На момент планирования GitHub read-доступ проверен; доступ записи variable,
  SSH, sandbox/порталу и остаток LLM-кредитов не проверены. Сама live-проверка
  имеет внешний dependency — штатный merge и deployment.

## Утверждение

В соответствии с явно вызванным agent-loop реализация начинается после
утверждения этого плана пользователем. Утверждение охватывает описанную ветку,
реализацию, тесты, документацию, настройку публичной variable и публикацию PR.
Merge, выдача approve и обход deployment policy в этот план не входят.
