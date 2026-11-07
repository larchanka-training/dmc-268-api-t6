# Профиль дашборда: `GET /api/metrics/summary` и `GET /api/runs` (#456)

Часть 1 задачи #456: цифры «до» на заявленном объёме. Запросы, индексы и миграции в этой части не менялись; цель карточки спринта 5 — ответ основных эндпоинтов дашборда < 100 мс. Оптимизацию и цифры «после» той же командой добавит часть 2.

Снято 07.11.2026, код приложения — `main` на `86e0001f`.

## Итог

При load 5–10 (прогоны 2 и 3 ниже) и 54 тыс. прогонов за год:

| Сценарий | p95, мс | Цель 100 мс |
| --- | --- | --- |
| `summary 30d` | 1 019–1 309 | не укладывается, примерно в 10 раз |
| `summary 30d repo` | 139–161 | не укладывается |
| `runs first page` | 41–46 | укладывается |
| `runs by cursor` | 63–71 | укладывается, но растёт с глубиной страницы (см. [наблюдения](#наблюдения-для-части-2)) |

N+1 на уровне приложения нет: список — один SQL-запрос при `limit` 10, 20 и 100, сводка — девять (восемь, если у всех прогонов есть сохранённый результат), и их число не зависит от числа прогонов. Время уходит внутри запросов и в разборе их результатов.

## Эндпоинты и сценарии

| Сценарий | Запрос | Откуда параметры |
| --- | --- | --- |
| `summary 30d` | `GET /api/metrics/summary?range=30d` | scope задачи |
| `summary 30d repo` | то же и `repo=<репозиторий>` | самый загруженный из видимых пользователю |
| `runs first page` | `GET /api/runs?limit=20` | `RUN_LIST_PAGE_SIZE = 20` в UI, а не значение API по умолчанию 50 |
| `runs by cursor` | то же и `cursor=<середина истории>` | курсор строится `RunCursor.encode` по прогону из середины видимой истории |

## Объём данных

SD §13 задаёт только пропускную способность — 100 PR/день; историю в один год выбрал техлид при планировании части 1: прогоны и находки хранятся до удаления Workspace, очистка #457 их не затрагивает. Часть 2 судит о цели на этом же объёме.

Команда печатает фактические числа, ниже они из её вывода:

| Таблица | Строк |
| --- | --- |
| `code_changes` (PR) | 36 500 |
| `runs` | 54 744 |
| `findings` | 182 299 |
| `usage_events` | 157 596 |
| `run_actions` | 360 684 |
| `code_change_diffs` | 262 660 |

В окне 30 дней: 4 517 прогонов, из них 3 847 успешных, у 3 769 есть сохранённый результат `review.postprocess`, остальные идут по запасному чтению. Дайджест строк `runs` одинаков во всех трёх прогонах: `a0a0762d99ad15396e0dc77d94e18bb3` (`--seed 456`, идентификаторы и состояния не зависят от времени запуска, `created_at` отсчитывается от момента загрузки).

Заполняются все таблицы, которые читают эндпоинты, а не только три из критерия: на пустых `code_change_diffs`, `context_payloads` и `run_actions` подзапросы списка и чтение результатов ничего бы не стоили. Сохранённые результаты собраны функциями приложения (`RunCoverage.to_json`, `severity_counts`, `coverage_verdict`), поэтому чтение идёт рабочим путём; интеграционный тест проверяет их через `read_saved_review_result`.

Допущения генератора (константы в начале `scripts/dashboard_profile.py`):

| Параметр | Значение |
| --- | --- |
| Workspace и репозитории | 3 и 25; измеряемому пользователю видны 2 и 20, то есть около 80 % данных; репозиторий PR выбирается равномерно |
| Прогонов на PR | в среднем 1,5 |
| Состояния прогонов | `succeeded` 85 %, `cancelled` 6 %, `failed` 5 %, `skipped` 4 %; ещё до 5 активных на конце истории (SD §13) |
| Summary-only | 2 % успешных, без сохранённого результата |
| Находки успешного прогона | от 0 до 8 равномерно, 70 % опубликовано |
| На не-`skipped` прогон | 3 `usage_events`, 6 `run_actions` (плюс `review.postprocess` у успешного), 5 файлов в `code_change_diffs`, 1 `context_payloads` |

Распределения — оценка: со стендом они не сверялись, замер по условию задачи только локальный. Только GitHub, без Bitbucket и GitLab; `run_action_responses` пуста; тела ответов инструментов короче настоящих, патчи фиксированной длины.

## Метод замера

- Задержка: настоящий HTTP на loopback к `uvicorn app.main:app` (как `CMD` в `Dockerfile`, один процесс) с RS256-токеном; брокера нет, поэтому реконсилер выключен. Подключение к БД — обычный пул приложения.
- Клиент последовательный, по одному запросу. Сначала 5 прогревочных запросов на сценарий, затем по 200 замеров; сценарии чередуются, поэтому шум машины достаётся всем поровну. p50 и p95 — nearest rank, как в `scripts/webhook_ack_load.py`. Ответ не 200 или неполная страница останавливают прогон.
- SQL: те же use case (`GetMetricsSummary`, `ListRuns`) вызываются в процессе команды, `before_cursor_execute` и `after_cursor_execute` записывают каждый запрос с его параметрами. `EXPLAIN (ANALYZE, BUFFERS)` выполняется для этих же запросов с теми же параметрами, не для переписанных вручную. Здесь каждый вызов открывает новое соединение, поэтому времена запросов выше, чем по HTTP: они нужны для долей и ранжирования, а не как цифры ответа.
- PostgreSQL: отдельный контейнер `postgres:17-alpine` (17.11) с настройками по умолчанию: `shared_buffers=128MB`, `work_mem=4MB`, `jit=on`, `max_parallel_workers_per_gather=2`. В `deploy/` и `docker-compose.yml` эти параметры не заданы, поэтому стенд, развёрнутый из репозитория, получает те же значения. После загрузки — `VACUUM (ANALYZE)`.
- Машина: AMD Ryzen 7 PRO 4750U (16 потоков), 62 ГБ, API и БД на ней же; её делят другие сессии, поэтому нагрузка указана у каждого прогона.

## Команда

```bash
docker run -d --name dash-profile -e POSTGRES_PASSWORD=profile \
  -p 127.0.0.1:55456:5432 postgres:17-alpine
until docker exec dash-profile pg_isready -h 127.0.0.1 -U postgres -q; do sleep 1; done

uv run python -m scripts.dashboard_profile \
  --server-url postgresql+psycopg://postgres:profile@127.0.0.1:55456/postgres \
  --explain-out docs/plans/456-explain.md

docker rm -fv dash-profile
```

Команда работает только с адресом loopback, создаёт базу `dashboard_profile_<hex>` и удаляет её в конце, в том числе при сбое. Занимает 5–15 минут в зависимости от нагрузки и до 1 ГБ на диске вместе с WAL. Запускать из корня репозитория: `python scripts/…` не импортирует `app`. Флаг `-v` у `docker rm` нужен, иначе остаётся анонимный том. `docs/plans/` не попадает в git.

## Результаты

Три прогона команды, каждый на новом контейнере, по 200 замеров на сценарий; значения в миллисекундах.

| Сценарий | 1: p50 | 1: p95 | 2: p50 | 2: p95 | 3: p50 | 3: p95 |
| --- | --- | --- | --- | --- | --- | --- |
| `summary 30d` | 2142,13 | 2470,41 | 946,74 | 1309,27 | 829,11 | 1018,57 |
| `summary 30d repo` | 262,96 | 305,34 | 126,42 | 160,93 | 112,28 | 139,47 |
| `runs first page` | 72,65 | 86,29 | 34,86 | 46,32 | 30,48 | 41,17 |
| `runs by cursor` | 111,10 | 127,46 | 52,99 | 71,38 | 48,33 | 62,82 |

| Прогон | Начало (UTC) | load перед замером | load после замера |
| --- | --- | --- | --- |
| 1 | 2026-11-07T00:48:17Z | 22,4 | 22,6 |
| 2 | 2026-11-07T01:00:34Z | 9,6 | 5,9 |
| 3 | 2026-11-07T01:06:38Z | 9,3 | 5,2 |

Разброс определяется нагрузкой на машину: при load около 22 цифры в 2–2,6 раза выше, чем при load 5–10, а между прогонами 2 и 3 расхождение p95 не больше 30 %. Новый прогон сопоставим с этой таблицей только при близкой нагрузке, поэтому часть 2 снимает «до» и «после» подряд на одной машине и приводит обе пары цифр.

## Куда уходит время

Один тёплый вызов в процессе команды, прогон 3; `sql_ms` — сумма времени выполнения запросов, `total_ms` — весь вызов use case, включая новое соединение.

| Вызов | Запросов | `sql_ms` | `total_ms` |
| --- | --- | --- | --- |
| `summary 30d` | 9 | 921,94 | 1094,54 |
| `summary 30d repo` | 9 | 137,90 | 186,63 |
| `runs first page` | 1 | 70,42 | 93,43 |
| `runs by cursor` | 1 | 82,50 | 107,18 |
| `runs limit=10` | 1 | 44,19 | 63,09 |
| `runs limit=100` | 1 | 50,60 | 75,87 |

Запросы `summary 30d` от самого долгого, файл `app/modules/reviews/infrastructure/metrics_summary.py`, для `load_review_results` — `saved_review_results.py`:

| # | мс | Источник |
| --- | --- | --- |
| 1 | 307,23 | `_terminal_heads`: последний терминальный прогон PR и его находки |
| 2 | 180,75 | `_usage_and_duration`: расход коррелированными подзапросами |
| 3 | 146,51 | `load_review_results`: сохранённые результаты |
| 4 | 95,48 | `_days`: ряд по дням |
| 5 | 90,67 | `_finding_counts`: находки по серьёзности и категории |
| 6 | 49,98 | `_days`: ряд по дням |
| 7 | 42,60 | `_status_counts`: прогоны по статусам |
| 8 | 4,56 | `load_review_results`: запасное чтение диффов без сохранённого результата |
| 9 | 4,17 | `_workspaces`: расход Workspace за сегодня |

## Наблюдения для части 2

Ничего из этого здесь не исправлялось. Значения — из планов прогона 3 ниже.

1. **`_terminal_heads` читает все находки истории.** `Seq Scan on findings` пропускает 127 375 опубликованных строк и хеширует их, хотя нужны находки только 2 395 последних прогонов окна. Индекс на `findings` есть только по `run_id`.
2. **JIT дороже самих вычислений.** Завышенная оценка стоимости из-за подзапросов на строку включает JIT: в `_terminal_heads` 120 мс из 259 мс выполнения, в `_usage_and_duration` около 65 мс из 142 мс. Настройка JIT и снижение оценки — разные пути к этому эффекту, выбор за частью 2.
3. **`_usage_and_duration` суммирует расход тремя коррелированными подзапросами на каждый прогон окна.** Это `_event_sums` из Kickoff, тот же приём в списке — `_usage_totals`; замена одним агрегатом по `usage_events` — кандидат для части 2, выигрыш пока не измерен.
4. **`load_review_results` передаёт около 2 000 идентификаторов через `IN`**, и план выбирает `Parallel Seq Scan on run_actions` по всей таблице: фильтр отбрасывает почти 120 000 строк на процесс. Отсюда же строки SQL и плана на десятки килобайт.
5. **Вне SQL уходит около 160–175 мс при load 5–10** (`total_ms − sql_ms` у `summary 30d` в прогонах 2 и 3; оценка сверху, потому что включает новое соединение): разбор строк и около 2 000 сохранённых результатов, сборка ответа. Это само по себе больше цели в 100 мс, поэтому одной оптимизации SQL для `summary 30d` не хватит. Кэширование ответов вне scope задачи; если при неизменном контракте цель недостижима, отчёт части 2 называет причину и объём, на котором она достигается, а решение за техлидом.
6. **Предикат курсора не использует индекс.** `or_(created_at < X, and_(created_at = X, id < Y))` в `list_runs` не становится `Index Cond`: `Index Scan Backward using ix_runs_created_id` идёт от новейшей строки и отбрасывает фильтром 27 437 строк на странице из середины истории: `Buffers: shared hit=20511` против 1 753 у первой страницы. Стоимость страницы растёт с глубиной.
7. **`_summary_only_projection` читает `context_payloads` целиком.** `EXISTS` по `summary ->> 'status'` превращается в хешированный подплан с `Seq Scan on context_payloads` на 52 532 строки: около 9 мс из 12 мс выполнения первой страницы и 12 мс в `_terminal_heads`.

## Блокировка при создании индексов

Часть 1 индексов не создаёт. Решение про `CREATE INDEX` без `CONCURRENTLY` (хвост #401: он блокирует запись в `runs`) записывает здесь часть 2 вместе с новой ревизией Alembic.

## Планы `EXPLAIN (ANALYZE, BUFFERS)`

Прогон 3. Строки длиннее 500 символов обрезаны с указанием числа символов; полные планы всех сценариев и текст SQL пишет `--explain-out`. План `runs first page` той же формы, что `runs by cursor`, но индекс по `runs` читается без отброшенных фильтром строк.

### `summary 30d`: `_terminal_heads`

```text
Hash Left Join  (cost=25380.24..137113.83 rows=8898 width=94) (actual time=172.952..226.332 rows=6214 loops=1)
  Hash Cond: (runs.id = findings.run_id)
  Buffers: shared hit=28238 read=10620
  ->  Unique  (cost=11684.73..123091.18 rows=3368 width=86) (actual time=91.451..141.880 rows=2395 loops=1)
        Buffers: shared hit=27657 read=438
        ->  Nested Loop  (cost=11684.73..123082.76 rows=3368 width=86) (actual time=91.449..140.897 rows=3611 loops=1)
              Buffers: shared hit=27657 read=438
              ->  Gather Merge  (cost=11684.58..12200.64 rows=4431 width=101) (actual time=78.576..97.068 rows=4512 loops=1)
                    Workers Planned: 2
                    Workers Launched: 2
                    Buffers: shared hit=12637
                    ->  Sort  (cost=10684.56..10689.17 rows=1846 width=101) (actual time=50.626..50.885 rows=1504 loops=3)
                          Sort Key: runs.code_change_id, runs.finished_at DESC NULLS LAST, runs.id DESC
                          Sort Method: quicksort  Memory: 25kB
                          Buffers: shared hit=12637
                          Worker 0:  Sort Method: quicksort  Memory: 405kB
                          Worker 1:  Sort Method: quicksort  Memory: 268kB
                          ->  Hash Join  (cost=7009.83..10584.41 rows=1846 width=101) (actual time=48.579..49.517 rows=1504 loops=3)
                                Hash Cond: (code_changes.id = runs.code_change_id)
                                Buffers: shared hit=12597
                                ->  Parallel Seq Scan on code_changes  (cost=0.00..3480.08 rows=15208 width=32) (actual time=35.674..39.891 rows=12167 loops=3)
                                      Buffers: shared hit=3328
                                ->  Hash  (cost=6954.44..6954.44 rows=4431 width=85) (actual time=6.914..6.915 rows=4512 loops=3)
                                      Buckets: 8192  Batches: 1  Memory Usage: 593kB
                                      Buffers: shared hit=9209
                                      ->  Index Scan using ix_runs_created_id on runs  (cost=0.29..6954.44 rows=4431 width=85) (actual time=0.071..5.110 rows=4512 loops=3)
                                            Index Cond: ((created_at >= '2026-10-08 01:11:56.462726+00'::timestamp with time zone) AND (created_at <= '2026-11-07 01:11:56.462726+00'::timestamp with time zone))
                                            Filter: (state = ANY ('{succeeded,failed,cancelled,skipped}'::run_state[]))
                                            Rows Removed by Filter: 5
                                            Buffers: shared hit=9209
              ->  Memoize  (cost=0.15..5.76 rows=1 width=16) (actual time=0.000..0.000 rows=1 loops=4512)
                    Cache Key: code_changes.repository_id
                    Cache Mode: logical
                    Hits: 4487  Misses: 25  Evictions: 0  Overflows: 0  Memory Usage: 3kB
                    Buffers: shared hit=120
                    ->  Index Scan using uq_repositories_owner on repositories  (cost=0.14..5.75 rows=1 width=16) (actual time=0.012..0.012 rows=1 loops=25)
                          Index Cond: (id = code_changes.repository_id)
                          Filter: (EXISTS(SubPlan 4) OR EXISTS(SubPlan 5))
                          Rows Removed by Filter: 0
                          Buffers: shared hit=120
                          SubPlan 4
                            ->  Nested Loop  (cost=0.00..3.46 rows=1 width=0) (actual time=0.007..0.007 rows=1 loops=25)
                                  Buffers: shared hit=65
                                  ->  Nested Loop  (cost=0.00..2.10 rows=1 width=16) (actual time=0.004..0.004 rows=1 loops=25)
                                        Join Filter: (provider_installations.workspace_id = github_user_workspace_access.workspace_id)
                                        Rows Removed by Join Filter: 0
                                        Buffers: shared hit=45
                                        ->  Seq Scan on provider_installations  (cost=0.00..1.05 rows=1 width=32) (actual time=0.002..0.002 rows=1 loops=25)
                                              Filter: ((workspace_id = ANY ('{858fd0e5-ca21-40d3-a635-8257d2805b71,c1c34d37-8d54-41c5-8cc4-7f110348200e}'::uuid[])) AND (id = repositories.provider_installation_id) AND ((provider)::text = 'github'::text))
                                              Rows Removed by Filter: 1
                                              Buffers: shared hit=25
                                        ->  Seq Scan on github_user_workspace_access  (cost=0.00..1.02 rows=2 width=16) (actual time=0.001..0.001 rows=2 loops=20)
                                              Filter: (github_user_id = '42'::bigint)
                                              Buffers: shared hit=20
                                  ->  Seq Scan on github_user_repository_access  (cost=0.00..1.35 rows=1 width=16) (actual time=0.002..0.002 rows=1 loops=20)
                                        Filter: ((github_user_id = '42'::bigint) AND (provider_installation_id = repositories.provider_installation_id) AND (repository_external_id = repositories.external_id))
                                        Rows Removed by Filter: 10
                                        Buffers: shared hit=20
                          SubPlan 5
                            ->  Result  (cost=0.00..2.13 rows=1 width=0) (actual time=0.005..0.005 rows=0 loops=5)
                                  One-Time Filter: (repositories.installation_available IS TRUE)
                                  Buffers: shared hit=5
                                  ->  Nested Loop  (cost=0.00..2.13 rows=1 width=0) (actual time=0.004..0.004 rows=0 loops=5)
                                        Join Filter: (github_user_workspace_access_1.workspace_id = bitbucket_repository_identities.workspace_id)
                                        Buffers: shared hit=5
                                        ->  Nested Loop  (cost=0.00..2.11 rows=1 width=64) (actual time=0.004..0.004 rows=0 loops=5)
                                              Join Filter: (github_user_workspace_access_1.workspace_id = bitbucket_connections.workspace_id)
                                              Buffers: shared hit=5
                                              ->  Nested Loop  (cost=0.00..2.10 rows=1 width=48) (actual time=0.003..0.003 rows=0 loops=5)
                                                    Join Filter: (provider_installations_1.workspace_id = github_user_workspace_access_1.workspace_id)
                                                    Buffers: shared hit=5
                                                    ->  Seq Scan on provider_installations provider_installations_1  (cost=0.00..1.05 rows=1 width=32) (actual time=0.003..0.003 rows=0 loops=5)
                                                          Filter: ((deleted_at IS NULL) AND (workspace_id = ANY ('{858fd0e5-ca21-40d3-a635-8257d2805b71,c1c34d37-8d54-41c5-8cc4-7f110348200e}'::uuid[])) AND (id = repositories.provider_installation_id) AND ((provider)::text = 'bitbucket'::text))
                                                          Rows Removed by Filter: 3
                                                          Buffers: shared hit=5
                                                    ->  Seq Scan on github_user_workspace_access github_user_workspace_access_1  (cost=0.00..1.02 rows=2 width=16) (never executed)
                                                          Filter: (github_user_id = '42'::bigint)
                                              ->  Seq Scan on bitbucket_connections  (cost=0.00..0.00 rows=1 width=32) (never executed)
                                                    Filter: ((revoked_at IS NULL) AND (provider_installation_id = repositories.provider_installation_id))
                                        ->  Seq Scan on bitbucket_repository_identities  (cost=0.00..0.00 rows=1 width=32) (never executed)
                                              Filter: ((provider_installation_id = repositories.provider_installation_id) AND (repository_id = repositories.id))
              SubPlan 1
                ->  Aggregate  (cost=24.52..24.53 rows=1 width=1) (actual time=0.007..0.007 rows=1 loops=3611)
                      Buffers: shared hit=13868 read=438
                      ->  Index Scan using uq_code_change_diffs_run_filename on code_change_diffs  (cost=0.42..24.51 rows=5 width=1) (actual time=0.005..0.007 rows=5 loops=3611)
                            Index Cond: (run_id = runs.id)
                            Buffers: shared hit=13868 read=438
              SubPlan 3
                ->  Seq Scan on context_payloads  (cost=0.00..1951.31 rows=5 width=16) (actual time=0.187..12.011 rows=952 loops=1)
                      Filter: ((NOT (summary ? 'reviewable_lines'::text)) AND ((summary ->> 'status'::text) = 'summary_only'::text))
                      Rows Removed by Filter: 51580
                      Buffers: shared hit=1032
  ->  Hash  (cost=12574.99..12574.99 rows=89642 width=20) (actual time=80.642..80.643 rows=127375 loops=1)
        Buckets: 131072  Batches: 1  Memory Usage: 7493kB
        Buffers: shared hit=570 read=10182
        ->  Seq Scan on findings  (cost=0.00..12574.99 rows=89642 width=20) (actual time=0.087..48.647 rows=127375 loops=1)
              Filter: ((published IS TRUE) AND (drop_reason IS NULL))
              Rows Removed by Filter: 54924
              Buffers: shared hit=570 read=10182
Planning:
  Buffers: shared hit=981
Planning Time: 10.300 ms
JIT:
  Functions: 139
  Options: Inlining false, Optimization false, Expressions true, Deforming true
  Timing: Generation 12.489 ms (Deform 6.500 ms), Inlining 0.000 ms, Optimization 4.811 ms, Emission 102.742 ms, Total 120.042 ms
Execution Time: 258.715 ms
```

### `summary 30d`: `_usage_and_duration`

```text
Aggregate  (cost=174999.56..174999.57 rows=1 width=136) (actual time=134.755..134.763 rows=1 loops=1)
  Buffers: shared hit=49825
  ->  Nested Loop  (cost=6987.82..11160.17 rows=3368 width=36) (actual time=72.116..77.151 rows=3614 loops=1)
        Buffers: shared hit=6517
        ->  Hash Join  (cost=6987.67..10907.48 rows=4431 width=52) (actual time=71.992..74.597 rows=4517 loops=1)
              Hash Cond: (code_changes.id = runs.code_change_id)
              Buffers: shared hit=6397
              ->  Seq Scan on code_changes  (cost=0.00..3693.00 rows=36500 width=32) (actual time=57.905..64.418 rows=36500 loops=1)
                    Buffers: shared hit=3328
              ->  Hash  (cost=6932.29..6932.29 rows=4431 width=52) (actual time=4.379..4.379 rows=4517 loops=1)
                    Buckets: 8192  Batches: 1  Memory Usage: 453kB
                    Buffers: shared hit=3069
                    ->  Index Scan using ix_runs_created_id on runs  (cost=0.29..6932.29 rows=4431 width=52) (actual time=0.073..2.945 rows=4517 loops=1)
                          Index Cond: ((created_at >= '2026-10-08 01:11:56.462726+00'::timestamp with time zone) AND (created_at <= '2026-11-07 01:11:56.462726+00'::timestamp with time zone))
                          Buffers: shared hit=3069
        ->  Memoize  (cost=0.15..5.76 rows=1 width=16) (actual time=0.000..0.000 rows=1 loops=4517)
              Cache Key: code_changes.repository_id
              Cache Mode: logical
              Hits: 4492  Misses: 25  Evictions: 0  Overflows: 0  Memory Usage: 3kB
              Buffers: shared hit=120
              ->  Index Scan using uq_repositories_owner on repositories  (cost=0.14..5.75 rows=1 width=16) (actual time=0.013..0.013 rows=1 loops=25)
                    Index Cond: (id = code_changes.repository_id)
                    Filter: (EXISTS(SubPlan 4) OR EXISTS(SubPlan 5))
                    Rows Removed by Filter: 0
                    Buffers: shared hit=120
                    SubPlan 4
                      ->  Nested Loop  (cost=0.00..3.46 rows=1 width=0) (actual time=0.007..0.007 rows=1 loops=25)
                            Buffers: shared hit=65
                            ->  Nested Loop  (cost=0.00..2.10 rows=1 width=16) (actual time=0.005..0.005 rows=1 loops=25)
                                  Join Filter: (provider_installations.workspace_id = github_user_workspace_access.workspace_id)
                                  Rows Removed by Join Filter: 0
                                  Buffers: shared hit=45
                                  ->  Seq Scan on provider_installations  (cost=0.00..1.05 rows=1 width=32) (actual time=0.002..0.002 rows=1 loops=25)
                                        Filter: ((workspace_id = ANY ('{858fd0e5-ca21-40d3-a635-8257d2805b71,c1c34d37-8d54-41c5-8cc4-7f110348200e}'::uuid[])) AND (id = repositories.provider_installation_id) AND ((provider)::text = 'github'::text))
                                        Rows Removed by Filter: 1
                                        Buffers: shared hit=25
                                  ->  Seq Scan on github_user_workspace_access  (cost=0.00..1.02 rows=2 width=16) (actual time=0.001..0.001 rows=2 loops=20)
                                        Filter: (github_user_id = '42'::bigint)
                                        Buffers: shared hit=20
                            ->  Seq Scan on github_user_repository_access  (cost=0.00..1.35 rows=1 width=16) (actual time=0.002..0.002 rows=1 loops=20)
                                  Filter: ((github_user_id = '42'::bigint) AND (provider_installation_id = repositories.provider_installation_id) AND (repository_external_id = repositories.external_id))
                                  Rows Removed by Filter: 10
                                  Buffers: shared hit=20
                    SubPlan 5
                      ->  Result  (cost=0.00..2.13 rows=1 width=0) (actual time=0.005..0.005 rows=0 loops=5)
                            One-Time Filter: (repositories.installation_available IS TRUE)
                            Buffers: shared hit=5
                            ->  Nested Loop  (cost=0.00..2.13 rows=1 width=0) (actual time=0.003..0.004 rows=0 loops=5)
                                  Join Filter: (github_user_workspace_access_1.workspace_id = bitbucket_repository_identities.workspace_id)
                                  Buffers: shared hit=5
                                  ->  Nested Loop  (cost=0.00..2.11 rows=1 width=64) (actual time=0.003..0.003 rows=0 loops=5)
                                        Join Filter: (github_user_workspace_access_1.workspace_id = bitbucket_connections.workspace_id)
                                        Buffers: shared hit=5
                                        ->  Nested Loop  (cost=0.00..2.10 rows=1 width=48) (actual time=0.003..0.003 rows=0 loops=5)
                                              Join Filter: (provider_installations_1.workspace_id = github_user_workspace_access_1.workspace_id)
                                              Buffers: shared hit=5
                                              ->  Seq Scan on provider_installations provider_installations_1  (cost=0.00..1.05 rows=1 width=32) (actual time=0.002..0.002 rows=0 loops=5)
                                                    Filter: ((deleted_at IS NULL) AND (workspace_id = ANY ('{858fd0e5-ca21-40d3-a635-8257d2805b71,c1c34d37-8d54-41c5-8cc4-7f110348200e}'::uuid[])) AND (id = repositories.provider_installation_id) AND ((provider)::text = 'bitbucket'::text))
                                                    Rows Removed by Filter: 3
                                                    Buffers: shared hit=5
                                              ->  Seq Scan on github_user_workspace_access github_user_workspace_access_1  (cost=0.00..1.02 rows=2 width=16) (never executed)
                                                    Filter: (github_user_id = '42'::bigint)
                                        ->  Seq Scan on bitbucket_connections  (cost=0.00..0.00 rows=1 width=32) (never executed)
                                              Filter: ((revoked_at IS NULL) AND (provider_installation_id = repositories.provider_installation_id))
                                  ->  Seq Scan on bitbucket_repository_identities  (cost=0.00..0.00 rows=1 width=32) (never executed)
                                        Filter: ((provider_installation_id = repositories.provider_installation_id) AND (repository_id = repositories.id))
  SubPlan 1
    ->  Aggregate  (cost=16.20..16.21 rows=1 width=8) (actual time=0.006..0.006 rows=1 loops=3614)
          Buffers: shared hit=14436
          ->  Bitmap Heap Scan on usage_events  (cost=4.44..16.19 rows=3 width=4) (actual time=0.004..0.004 rows=3 loops=3614)
                Recheck Cond: (run_id = runs.id)
                Heap Blocks: exact=3594
                Buffers: shared hit=14436
                ->  Bitmap Index Scan on ix_usage_events_run_created  (cost=0.00..4.44 rows=3 width=0) (actual time=0.003..0.003 rows=3 loops=3614)
                      Index Cond: (run_id = runs.id)
                      Buffers: shared hit=10842
  SubPlan 2
    ->  Aggregate  (cost=16.20..16.21 rows=1 width=8) (actual time=0.004..0.004 rows=1 loops=3614)
          Buffers: shared hit=14436
          ->  Bitmap Heap Scan on usage_events usage_events_1  (cost=4.44..16.19 rows=3 width=4) (actual time=0.002..0.003 rows=3 loops=3614)
                Recheck Cond: (run_id = runs.id)
                Heap Blocks: exact=3594
                Buffers: shared hit=14436
                ->  Bitmap Index Scan on ix_usage_events_run_created  (cost=0.00..4.44 rows=3 width=0) (actual time=0.002..0.002 rows=3 loops=3614)
                      Index Cond: (run_id = runs.id)
                      Buffers: shared hit=10842
  SubPlan 3
    ->  Aggregate  (cost=16.20..16.21 rows=1 width=32) (actual time=0.004..0.004 rows=1 loops=3614)
          Buffers: shared hit=14436
          ->  Bitmap Heap Scan on usage_events usage_events_2  (cost=4.44..16.19 rows=3 width=6) (actual time=0.002..0.003 rows=3 loops=3614)
                Recheck Cond: (run_id = runs.id)
                Heap Blocks: exact=3594
                Buffers: shared hit=14436
                ->  Bitmap Index Scan on ix_usage_events_run_created  (cost=0.00..4.44 rows=3 width=0) (actual time=0.002..0.002 rows=3 loops=3614)
                      Index Cond: (run_id = runs.id)
                      Buffers: shared hit=10842
Planning:
  Buffers: shared hit=131
Planning Time: 2.722 ms
JIT:
  Functions: 91
  Options: Inlining false, Optimization false, Expressions true, Deforming true
  Timing: Generation 6.859 ms (Deform 3.314 ms), Inlining 0.000 ms, Optimization 1.867 ms, Emission 56.183 ms, Total 64.908 ms
Execution Time: 142.478 ms
```

### `summary 30d`: `load_review_results`

```text
Unique  (cost=16487.04..16706.19 rows=1777 width=189) (actual time=44.050..56.102 rows=1998 loops=1)
  Buffers: shared hit=233 read=12669
  ->  Merge Left Join  (cost=16487.04..16701.68 rows=1804 width=189) (actual time=44.048..55.380 rows=1998 loops=1)
        Merge Cond: (run_actions.run_id = run_action_responses.run_id)
        Join Filter: (((run_action_responses.id)::character varying)::text = run_actions.response_ref)
        Buffers: shared hit=233 read=12669
        ->  Gather Merge  (cost=16487.03..16697.14 rows=1804 width=189) (actual time=44.013..55.020 rows=1998 loops=1)
              Workers Planned: 2
              Workers Launched: 2
              Buffers: shared hit=233 read=12669
              ->  Sort  (cost=15487.01..15488.89 rows=752 width=189) (actual time=23.423..23.513 rows=666 loops=3)
                    Sort Key: run_actions.run_id, run_actions.index DESC
                    Sort Method: quicksort  Memory: 782kB
                    Buffers: shared hit=233 read=12669
                    Worker 0:  Sort Method: quicksort  Memory: 526kB
                    Worker 1:  Sort Method: quicksort  Memory: 749kB
                    ->  Parallel Seq Scan on run_actions  (cost=5.09..15451.08 rows=752 width=189) (actual time=19.689..22.138 rows=666 loops=3)
                          Filter: (((tool)::text = 'review.postprocess'::text) AND (run_id = ANY ('{87ddf900-176d-4744-93f9-005554629d85,387b6e0b-d9a4-43f1-ac6d-bb384c960d5a,25249498-f6f4-4a29-9b7b-e23aa1dd8bd5,976c64c3-ea17-4ef4-9aa3-c62757d63671,4e85e981-41ca-4c97-b385-f9796c46967d,a3a922e5-ac4e-466c-9f3c-33b3bc6318df,21c4f9ee-9fff-4be9-bdc1-ab4365421440,01e749cf-a0f9-4c7c-962c-df62190ef63f,c4016d08-7a88-4227-bb05-6e0ff21fb797,4bd0246f-f251-48b8-8751-12d06af67f40, [75011 chars cut]
                          Rows Removed by Filter: 119562
                          Buffers: shared hit=147 read=12669
        ->  Sort  (cost=0.01..0.02 rows=1 width=64) (actual time=0.031..0.032 rows=0 loops=1)
              Sort Key: run_action_responses.run_id
              Sort Method: quicksort  Memory: 25kB
              ->  Seq Scan on run_action_responses  (cost=0.00..0.00 rows=1 width=64) (actual time=0.012..0.012 rows=0 loops=1)
Planning:
  Buffers: shared hit=153
Planning Time: 3.045 ms
Execution Time: 56.662 ms
```

### `runs by cursor`: `list_runs`

```text
Limit  (cost=1.02..2319.23 rows=21 width=3187) (actual time=29.732..30.877 rows=21 loops=1)
  Buffers: shared hit=20511
  ->  Nested Loop Left Join  (cost=1.02..2300001.51 rows=20835 width=3187) (actual time=29.731..30.871 rows=21 loops=1)
        Join Filter: (runs.origin_provider_installation_id = provider_installations.id)
        Rows Removed by Join Filter: 8
        Buffers: shared hit=20511
        ->  Nested Loop  (cost=1.02..227553.08 rows=20835 width=2880) (actual time=19.665..20.069 rows=21 loops=1)
              Join Filter: (code_changes.repository_id = repositories.id)
              Rows Removed by Join Filter: 186
              Buffers: shared hit=18973
              ->  Nested Loop  (cost=1.02..220026.99 rows=20835 width=2864) (actual time=19.647..20.008 rows=21 loops=1)
                    Join Filter: (code_changes.id = runs.code_change_id)
                    Buffers: shared hit=18972
                    ->  Nested Loop Semi Join  (cost=0.73..205391.53 rows=20835 width=2133) (actual time=19.629..19.936 rows=21 loops=1)
                          Buffers: shared hit=18909
                          ->  Index Scan Backward using ix_runs_created_id on runs  (cost=0.29..24670.78 rows=27415 width=2117) (actual time=19.511..19.538 rows=27 loops=1)
                                Filter: ((created_at < '2026-05-08 07:46:05.178651+00'::timestamp with time zone) OR ((created_at = '2026-05-08 07:46:05.178651+00'::timestamp with time zone) AND (id < '10088395-1279-4f92-94bb-bddde09c7029'::uuid)))
                                Rows Removed by Filter: 27437
                                Buffers: shared hit=18733
                          ->  Nested Loop  (cost=0.44..6.58 rows=1 width=16) (actual time=0.014..0.014 rows=1 loops=27)
                                Buffers: shared hit=176
                                ->  Index Scan using code_changes_pkey on code_changes code_changes_1  (cost=0.29..0.82 rows=1 width=32) (actual time=0.004..0.004 rows=1 loops=27)
                                      Index Cond: (id = runs.code_change_id)
                                      Buffers: shared hit=81
                                ->  Memoize  (cost=0.15..5.76 rows=1 width=16) (actual time=0.009..0.009 rows=1 loops=27)
                                      Cache Key: code_changes_1.repository_id
                                      Cache Mode: logical
                                      Hits: 7  Misses: 20  Evictions: 0  Overflows: 0  Memory Usage: 3kB
                                      Buffers: shared hit=95
                                      ->  Index Scan using uq_repositories_owner on repositories repositories_1  (cost=0.14..5.75 rows=1 width=16) (actual time=0.010..0.010 rows=1 loops=20)
                                            Index Cond: (id = code_changes_1.repository_id)
                                            Filter: (EXISTS(SubPlan 9) OR EXISTS(SubPlan 10))
                                            Rows Removed by Filter: 0
                                            Buffers: shared hit=95
                                            SubPlan 9
                                              ->  Nested Loop  (cost=0.00..3.46 rows=1 width=0) (actual time=0.006..0.006 rows=1 loops=20)
                                                    Buffers: shared hit=50
                                                    ->  Nested Loop  (cost=0.00..2.10 rows=1 width=16) (actual time=0.004..0.004 rows=1 loops=20)
                                                          Join Filter: (provider_installations_1.workspace_id = github_user_workspace_access.workspace_id)
                                                          Rows Removed by Join Filter: 0
                                                          Buffers: shared hit=35
                                                          ->  Seq Scan on provider_installations provider_installations_1  (cost=0.00..1.05 rows=1 width=32) (actual time=0.002..0.002 rows=1 loops=20)
                                                                Filter: ((workspace_id = ANY ('{858fd0e5-ca21-40d3-a635-8257d2805b71,c1c34d37-8d54-41c5-8cc4-7f110348200e}'::uuid[])) AND (id = repositories_1.provider_installation_id) AND ((provider)::text = 'github'::text))
                                                                Rows Removed by Filter: 1
                                                                Buffers: shared hit=20
                                                          ->  Seq Scan on github_user_workspace_access  (cost=0.00..1.02 rows=2 width=16) (actual time=0.001..0.001 rows=1 loops=15)
                                                                Filter: (github_user_id = '42'::bigint)
                                                                Buffers: shared hit=15
                                                    ->  Seq Scan on github_user_repository_access  (cost=0.00..1.35 rows=1 width=16) (actual time=0.002..0.002 rows=1 loops=15)
                                                          Filter: ((github_user_id = '42'::bigint) AND (provider_installation_id = repositories_1.provider_installation_id) AND (repository_external_id = repositories_1.external_id))
                                                          Rows Removed by Filter: 10
                                                          Buffers: shared hit=15
                                            SubPlan 10
                                              ->  Result  (cost=0.00..2.13 rows=1 width=0) (actual time=0.004..0.004 rows=0 loops=5)
                                                    One-Time Filter: (repositories_1.installation_available IS TRUE)
                                                    Buffers: shared hit=5
                                                    ->  Nested Loop  (cost=0.00..2.13 rows=1 width=0) (actual time=0.004..0.004 rows=0 loops=5)
                                                          Join Filter: (github_user_workspace_access_1.workspace_id = bitbucket_repository_identities.workspace_id)
                                                          Buffers: shared hit=5
                                                          ->  Nested Loop  (cost=0.00..2.11 rows=1 width=64) (actual time=0.003..0.003 rows=0 loops=5)
                                                                Join Filter: (github_user_workspace_access_1.workspace_id = bitbucket_connections.workspace_id)
                                                                Buffers: shared hit=5
                                                                ->  Nested Loop  (cost=0.00..2.10 rows=1 width=48) (actual time=0.003..0.003 rows=0 loops=5)
                                                                      Join Filter: (provider_installations_2.workspace_id = github_user_workspace_access_1.workspace_id)
                                                                      Buffers: shared hit=5
                                                                      ->  Seq Scan on provider_installations provider_installations_2  (cost=0.00..1.05 rows=1 width=32) (actual time=0.002..0.002 rows=0 loops=5)
                                                                            Filter: ((deleted_at IS NULL) AND (workspace_id = ANY ('{858fd0e5-ca21-40d3-a635-8257d2805b71,c1c34d37-8d54-41c5-8cc4-7f110348200e}'::uuid[])) AND (id = repositories_1.provider_installation_id) AND ((provider)::text = 'bitbucket'::text))
                                                                            Rows Removed by Filter: 3
                                                                            Buffers: shared hit=5
                                                                      ->  Seq Scan on github_user_workspace_access github_user_workspace_access_1  (cost=0.00..1.02 rows=2 width=16) (never executed)
                                                                            Filter: (github_user_id = '42'::bigint)
                                                                ->  Seq Scan on bitbucket_connections  (cost=0.00..0.00 rows=1 width=32) (never executed)
                                                                      Filter: ((revoked_at IS NULL) AND (provider_installation_id = repositories_1.provider_installation_id))
                                                          ->  Seq Scan on bitbucket_repository_identities  (cost=0.00..0.00 rows=1 width=32) (never executed)
                                                                Filter: ((provider_installation_id = repositories_1.provider_installation_id) AND (repository_id = repositories_1.id))
                    ->  Index Scan using code_changes_pkey on code_changes  (cost=0.29..0.69 rows=1 width=747) (actual time=0.002..0.002 rows=1 loops=21)
                          Index Cond: (id = code_changes_1.id)
                          Buffers: shared hit=63
              ->  Materialize  (cost=0.00..1.38 rows=25 width=32) (actual time=0.001..0.001 rows=10 loops=21)
                    Buffers: shared hit=1
                    ->  Seq Scan on repositories  (cost=0.00..1.25 rows=25 width=32) (actual time=0.003..0.006 rows=20 loops=1)
                          Buffers: shared hit=1
        ->  Materialize  (cost=0.00..1.04 rows=3 width=23) (actual time=0.001..0.001 rows=1 loops=21)
              Buffers: shared hit=1
              ->  Seq Scan on provider_installations  (cost=0.00..1.03 rows=3 width=23) (actual time=0.004..0.004 rows=2 loops=1)
                    Buffers: shared hit=1
        SubPlan 1
          ->  Limit  (cost=5.78..9.39 rows=1 width=40) (actual time=0.008..0.008 rows=1 loops=21)
                Buffers: shared hit=105
                ->  Incremental Sort  (cost=5.78..16.61 rows=3 width=40) (actual time=0.008..0.008 rows=1 loops=21)
                      Sort Key: usage_events.created_at DESC, usage_events.id DESC
                      Presorted Key: usage_events.created_at
                      Full-sort Groups: 21  Sort Method: top-N heapsort  Average Memory: 25kB  Peak Memory: 25kB
                      Buffers: shared hit=105
                      ->  Index Scan Backward using ix_usage_events_run_created on usage_events  (cost=0.42..16.47 rows=3 width=40) (actual time=0.004..0.005 rows=3 loops=21)
                            Index Cond: (run_id = runs.id)
                            Buffers: shared hit=105
        SubPlan 2
          ->  Aggregate  (cost=8.56..8.57 rows=1 width=8) (actual time=0.007..0.007 rows=1 loops=21)
                Buffers: shared hit=64
                ->  Index Only Scan using run_actions_run_id_index_key on run_actions  (cost=0.42..8.54 rows=7 width=0) (actual time=0.005..0.006 rows=7 loops=21)
                      Index Cond: (run_id = runs.id)
                      Heap Fetches: 0
                      Buffers: shared hit=64
        SubPlan 3
          ->  Aggregate  (cost=24.52..24.53 rows=1 width=1) (actual time=0.009..0.009 rows=1 loops=21)
                Buffers: shared hit=84
                ->  Index Scan using uq_code_change_diffs_run_filename on code_change_diffs  (cost=0.42..24.51 rows=5 width=1) (actual time=0.006..0.008 rows=5 loops=21)
                      Index Cond: (run_id = runs.id)
                      Buffers: shared hit=84
        SubPlan 5
          ->  Seq Scan on context_payloads  (cost=0.00..1951.31 rows=5 width=16) (actual time=0.027..9.439 rows=952 loops=1)
                Filter: ((NOT (summary ? 'reviewable_lines'::text)) AND ((summary ->> 'status'::text) = 'summary_only'::text))
                Rows Removed by Filter: 51580
                Buffers: shared hit=1032
        SubPlan 6
          ->  Aggregate  (cost=16.20..16.21 rows=1 width=8) (actual time=0.006..0.006 rows=1 loops=21)
                Buffers: shared hit=84
                ->  Bitmap Heap Scan on usage_events usage_events_1  (cost=4.44..16.19 rows=3 width=4) (actual time=0.004..0.004 rows=3 loops=21)
                      Recheck Cond: (run_id = runs.id)
                      Heap Blocks: exact=21
                      Buffers: shared hit=84
                      ->  Bitmap Index Scan on ix_usage_events_run_created  (cost=0.00..4.44 rows=3 width=0) (actual time=0.002..0.002 rows=3 loops=21)
                            Index Cond: (run_id = runs.id)
                            Buffers: shared hit=63
        SubPlan 7
          ->  Aggregate  (cost=16.20..16.21 rows=1 width=8) (actual time=0.004..0.004 rows=1 loops=21)
                Buffers: shared hit=84
                ->  Bitmap Heap Scan on usage_events usage_events_2  (cost=4.44..16.19 rows=3 width=4) (actual time=0.002..0.003 rows=3 loops=21)
                      Recheck Cond: (run_id = runs.id)
                      Heap Blocks: exact=21
                      Buffers: shared hit=84
                      ->  Bitmap Index Scan on ix_usage_events_run_created  (cost=0.00..4.44 rows=3 width=0) (actual time=0.002..0.002 rows=3 loops=21)
                            Index Cond: (run_id = runs.id)
                            Buffers: shared hit=63
        SubPlan 8
          ->  Aggregate  (cost=16.20..16.21 rows=1 width=32) (actual time=0.005..0.005 rows=1 loops=21)
                Buffers: shared hit=84
                ->  Bitmap Heap Scan on usage_events usage_events_3  (cost=4.44..16.19 rows=3 width=6) (actual time=0.002..0.002 rows=3 loops=21)
                      Recheck Cond: (run_id = runs.id)
                      Heap Blocks: exact=21
                      Buffers: shared hit=84
                      ->  Bitmap Index Scan on ix_usage_events_run_created  (cost=0.00..4.44 rows=3 width=0) (actual time=0.001..0.001 rows=3 loops=21)
                            Index Cond: (run_id = runs.id)
                            Buffers: shared hit=63
Planning:
  Buffers: shared hit=82
Planning Time: 3.271 ms
Execution Time: 31.543 ms
```
