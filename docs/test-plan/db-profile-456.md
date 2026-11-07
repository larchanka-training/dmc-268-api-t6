# Профиль дашборда: `GET /api/metrics/summary` и `GET /api/runs` (#456)

Часть 1 задачи #456: цифры «до» на заявленном объёме. Запросы, индексы и миграции в этой части не менялись; цель карточки спринта 5 — ответ основных эндпоинтов дашборда < 100 мс. Оптимизацию и цифры «после» той же командой добавит часть 2.

Снято 07.11.2026, код приложения — `main` на `86e0001f`.

## Итог

Диапазоны трёх прогонов на 54 тыс. прогонов за год, load от 2,5 до 11:

| Сценарий | p50, мс | p95, мс | Цель 100 мс |
| --- | --- | --- | --- |
| `summary 30d` | 772–814 | 911–1 666 | не укладывается: в 8–9 раз по p50 и по p95 без всплеска прогона 2 |
| `summary 30d repo` | 110–113 | 128–213 | не укладывается |
| `runs first page` | 30–33 | 39–61 | укладывается |
| `runs by cursor` | 46–51 | 57–90 | укладывается, но растёт с глубиной страницы (см. [наблюдения](#наблюдения-для-части-2)) |

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
- SQL: те же use case (`GetMetricsSummary`, `ListRuns`) вызываются в процессе команды, `before_cursor_execute` и `after_cursor_execute` записывают каждый запрос с его параметрами. `EXPLAIN (ANALYZE, BUFFERS)` выполняется для этих же запросов с теми же параметрами, не для переписанных вручную. Соединение переиспользуется, как в пуле API: первый вызов прогревает, в таблицу идёт второй, поэтому `total_ms − sql_ms` не содержит установку соединения. Эти цифры нужны для долей и ранжирования, цифры ответа — только HTTP.
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
| `summary 30d` | 774,38 | 931,14 | 813,71 | 1665,80 | 772,30 | 911,21 |
| `summary 30d repo` | 110,77 | 127,81 | 113,05 | 213,13 | 109,60 | 130,52 |
| `runs first page` | 29,67 | 38,95 | 32,51 | 61,18 | 29,68 | 40,12 |
| `runs by cursor` | 46,43 | 56,82 | 51,11 | 90,30 | 46,05 | 57,14 |

| Прогон | Начало (UTC) | load перед замером | load после замера |
| --- | --- | --- | --- |
| 1 | 2026-11-07T01:29:03Z | 9,1 | 3,7 |
| 2 | 2026-11-07T01:34:15Z | 10,9 | 4,1 |
| 3 | 2026-11-07T01:40:32Z | 3,9 | 2,5 |

p50 трёх прогонов расходятся не больше чем на 11 %. p95 прогона 2 выше, чем у прогонов 1 и 3, в 1,5–1,8 раза при том же p50: это редкие долгие ответы, причина не установлена, load перед этим замером был самым высоким. Поэтому при сверке сначала смотрите на p50, а p95 — на нескольких прогонах.

Цифры зависят от нагрузки на машину. Тот же HTTP-замер при load около 22 (ранний прогон той же команды) дал p50 и p95: `summary 30d` 2 142 и 2 470, `summary 30d repo` 263 и 305, `runs first page` 73 и 86, `runs by cursor` 111 и 127, то есть p50 в 2,4–2,8 раза выше, чем в прогоне 3. Новый прогон сопоставим с таблицей только при близкой нагрузке, поэтому часть 2 снимает «до» и «после» подряд на одной машине и приводит обе пары цифр.

## Куда уходит время

Один тёплый вызов в процессе команды, в каждом прогоне; `sql` — сумма времени запросов на клиенте, `total` — весь вызов use case, в миллисекундах.

| Вызов | Запросов | 1: `sql` | 1: `total` | 2: `sql` | 2: `total` | 3: `sql` | 3: `total` |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `summary 30d` | 9 | 697,01 | 838,14 | 653,70 | 787,01 | 572,86 | 686,48 |
| `summary 30d repo` | 9 | 88,81 | 120,17 | 83,49 | 120,92 | 85,41 | 128,65 |
| `runs first page` | 1 | 24,92 | 33,42 | 23,27 | 32,43 | 15,92 | 22,29 |
| `runs by cursor` | 1 | 36,57 | 42,92 | 30,19 | 40,17 | 30,78 | 40,60 |
| `runs limit=10` | 1 | 18,77 | 27,58 | 14,70 | 23,58 | 15,12 | 19,97 |
| `runs limit=100` | 1 | 23,41 | 38,20 | 32,79 | 43,90 | 21,30 | 32,37 |

Запросы `summary 30d` от самого долгого, прогон 3, файл `app/modules/reviews/infrastructure/metrics_summary.py`, для `load_review_results` — `saved_review_results.py`. Время измерено на клиенте и включает передачу запроса и строк результата, поэтому больше `Execution Time` в плане:

| # | мс | Источник |
| --- | --- | --- |
| 1 | 178,95 | `_terminal_heads`: последний терминальный прогон PR и его находки |
| 2 | 116,11 | `load_review_results`: сохранённые результаты |
| 3 | 103,14 | `_usage_and_duration`: расход коррелированными подзапросами |
| 4 | 61,38 | `_days`: ряд по дням |
| 5 | 61,05 | `_finding_counts`: находки по серьёзности и категории |
| 6 | 32,39 | `_days`: ряд по дням |
| 7 | 13,60 | `_status_counts`: прогоны по статусам |
| 8 | 3,87 | `load_review_results`: запасное чтение диффов без сохранённого результата |
| 9 | 2,35 | `_workspaces`: расход Workspace за сегодня |

## Наблюдения для части 2

Ничего из этого здесь не исправлялось. Значения — из планов прогона 3 ниже.

1. **`_terminal_heads` читает все находки истории.** `Seq Scan on findings` читает 127 375 опубликованных строк и хеширует их, хотя нужны находки только 2 395 последних прогонов окна. Индекс на `findings` есть только по `run_id`.
2. **JIT занимает около 43 % времени выполнения.** Завышенная оценка стоимости из-за подзапросов на строку включает JIT: в `_terminal_heads` 88,7 мс из 207,6 мс, в `_usage_and_duration` 47,0 мс из 109,7 мс. Настройка JIT и снижение оценки — разные пути к этому эффекту, выбор за частью 2.
3. **`_usage_and_duration` суммирует расход тремя коррелированными подзапросами на каждый прогон окна:** `loops=3614` у каждого. Это `_event_sums` из Kickoff, тот же приём в списке — `_usage_totals`; замена одним агрегатом по `usage_events` — кандидат для части 2, выигрыш пока не измерен.
4. **`load_review_results` передаёт около 2 000 идентификаторов через `IN`**, и план выбирает `Parallel Seq Scan on run_actions` по всей таблице: фильтр отбрасывает 119 562 строки на процесс. Отсюда же строки SQL и плана на десятки килобайт.
5. **Вне выполнения SQL use case сводки тратит 114–141 мс** (`total − sql` у `summary 30d`): сборка запросов SQLAlchemy, разбор строк и около 2 000 сохранённых результатов, расчёт ответа; по отдельности эти части не измерялись. Это уже больше цели в 100 мс, поэтому для `summary 30d` одной оптимизации SQL не хватит, нужно менять и этот код. Кэширование ответов вне scope задачи; если при неизменном контракте цель недостижима, отчёт части 2 называет причину и объём, на котором она достигается, а решение за техлидом.
6. **Предикат курсора не использует индекс.** `or_(created_at < X, and_(created_at = X, id < Y))` в `list_runs` не становится `Index Cond`: `Index Scan Backward using ix_runs_created_id` идёт от новейшей строки и отбрасывает фильтром 27 437 строк на странице из середины истории. У плана `Buffers: shared hit=20511` против 1753 у первой страницы и выполнение 24,7 мс против 10,2 мс. Стоимость страницы растёт с глубиной.
7. **`_summary_only_projection` читает `context_payloads` целиком.** `EXISTS` по `summary ->> 'status'` становится хешированным подпланом с `Seq Scan on context_payloads` на 52 532 строки: 7,8 мс из 10,2 мс выполнения первой страницы и 9,8 мс в `_terminal_heads`.

## Блокировка при создании индексов

Решение техлида (07.11.2026, хвост #401): индексы части 2 создаются обычным `CREATE INDEX`, без `CONCURRENTLY`, как во всех 21 ревизии с `create_index`. На время построения запись в таблицу блокируется, миграция идёт в сервисе `bootstrap` при деплое. Поэтому часть 2 измеряет длительность каждого своего индекса на этом объёме и записывает её здесь; индекс, который строится дольше 1 с, переводится на `CONCURRENTLY`.

Примерные цифры на данных прогона 3, по одному пробному индексу на таблицу. Состав индексов этим не определяется, в репозиторий они не входят:

| Индекс | `CREATE INDEX`, мс |
| --- | --- |
| `runs (code_change_id, finished_at DESC, id DESC)` | 75 |
| `findings (run_id) WHERE published AND drop_reason IS NULL` | 86 |
| `usage_events (run_id) INCLUDE (tokens_in, tokens_out, cost_usd)` | 122 |
| `run_actions (run_id) WHERE tool = 'review.postprocess'` | 67 |
| `context_payloads (run_id) WHERE summary ->> 'status' = 'summary_only'` | 19 |

## Планы `EXPLAIN (ANALYZE, BUFFERS)`

Прогон 3: три самых дорогих запроса `summary 30d` и запрос списка на обеих страницах. Строки длиннее 500 символов обрезаны с указанием числа символов; планы всех сценариев и текст SQL пишет `--explain-out`.

### `summary 30d`: `_terminal_heads`

```text
Hash Left Join  (cost=25417.18..137779.25 rows=8888 width=94) (actual time=135.687..181.947 rows=6214 loops=1)
  Hash Cond: (runs.id = findings.run_id)
  Buffers: shared hit=27648 read=11210
  ->  Unique  (cost=11720.08..123754.08 rows=3387 width=86) (actual time=69.168..112.999 rows=2395 loops=1)
        Buffers: shared hit=26943 read=1152
        ->  Nested Loop  (cost=11720.08..123745.61 rows=3387 width=86) (actual time=69.166..112.269 rows=3611 loops=1)
              Buffers: shared hit=26943 read=1152
              ->  Gather Merge  (cost=11719.93..12239.02 rows=4457 width=101) (actual time=58.558..74.283 rows=4512 loops=1)
                    Workers Planned: 2
                    Workers Launched: 2
                    Buffers: shared hit=12637
                    ->  Sort  (cost=10719.90..10724.55 rows=1857 width=101) (actual time=37.260..37.467 rows=1504 loops=3)
                          Sort Key: runs.code_change_id, runs.finished_at DESC NULLS LAST, runs.id DESC
                          Sort Method: quicksort  Memory: 223kB
                          Buffers: shared hit=12637
                          Worker 0:  Sort Method: quicksort  Memory: 225kB
                          Worker 1:  Sort Method: quicksort  Memory: 226kB
                          ->  Hash Join  (cost=7044.39..10619.08 rows=1857 width=101) (actual time=35.864..36.515 rows=1504 loops=3)
                                Hash Cond: (code_changes.id = runs.code_change_id)
                                Buffers: shared hit=12597
                                ->  Parallel Seq Scan on code_changes  (cost=0.00..3480.08 rows=15208 width=32) (actual time=26.142..29.367 rows=12167 loops=3)
                                      Buffers: shared hit=3328
                                ->  Hash  (cost=6988.67..6988.67 rows=4457 width=85) (actual time=5.255..5.256 rows=4512 loops=3)
                                      Buckets: 8192  Batches: 1  Memory Usage: 593kB
                                      Buffers: shared hit=9209
                                      ->  Index Scan using ix_runs_created_id on runs  (cost=0.29..6988.67 rows=4457 width=85) (actual time=0.057..3.935 rows=4512 loops=3)
                                            Index Cond: ((created_at >= '2026-10-08 01:45:09.161137+00'::timestamp with time zone) AND (created_at <= '2026-11-07 01:45:09.161137+00'::timestamp with time zone))
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
                            ->  Nested Loop  (cost=0.00..3.46 rows=1 width=0) (actual time=0.006..0.006 rows=1 loops=25)
                                  Buffers: shared hit=65
                                  ->  Nested Loop  (cost=0.00..2.10 rows=1 width=16) (actual time=0.004..0.004 rows=1 loops=25)
                                        Join Filter: (provider_installations.workspace_id = github_user_workspace_access.workspace_id)
                                        Rows Removed by Join Filter: 0
                                        Buffers: shared hit=45
                                        ->  Seq Scan on provider_installations  (cost=0.00..1.05 rows=1 width=32) (actual time=0.001..0.001 rows=1 loops=25)
                                              Filter: ((workspace_id = ANY ('{858fd0e5-ca21-40d3-a635-8257d2805b71,c1c34d37-8d54-41c5-8cc4-7f110348200e}'::uuid[])) AND (id = repositories.provider_installation_id) AND ((provider)::text = 'github'::text))
                                              Rows Removed by Filter: 1
                                              Buffers: shared hit=25
                                        ->  Seq Scan on github_user_workspace_access  (cost=0.00..1.02 rows=2 width=16) (actual time=0.001..0.001 rows=2 loops=20)
                                              Filter: (github_user_id = '42'::bigint)
                                              Buffers: shared hit=20
                                  ->  Seq Scan on github_user_repository_access  (cost=0.00..1.35 rows=1 width=16) (actual time=0.001..0.001 rows=1 loops=20)
                                        Filter: ((github_user_id = '42'::bigint) AND (provider_installation_id = repositories.provider_installation_id) AND (repository_external_id = repositories.external_id))
                                        Rows Removed by Filter: 10
                                        Buffers: shared hit=20
                          SubPlan 5
                            ->  Result  (cost=0.00..2.13 rows=1 width=0) (actual time=0.004..0.005 rows=0 loops=5)
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
                ->  Aggregate  (cost=24.52..24.53 rows=1 width=1) (actual time=0.007..0.007 rows=1 loops=3611)
                      Buffers: shared hit=13154 read=1152
                      ->  Index Scan using uq_code_change_diffs_run_filename on code_change_diffs  (cost=0.42..24.51 rows=5 width=1) (actual time=0.005..0.006 rows=5 loops=3611)
                            Index Cond: (run_id = runs.id)
                            Buffers: shared hit=13154 read=1152
              SubPlan 3
                ->  Seq Scan on context_payloads  (cost=0.00..1951.31 rows=5 width=16) (actual time=0.157..9.806 rows=952 loops=1)
                      Filter: ((NOT (summary ? 'reviewable_lines'::text)) AND ((summary ->> 'status'::text) = 'summary_only'::text))
                      Rows Removed by Filter: 51580
                      Buffers: shared hit=1032
  ->  Hash  (cost=12574.99..12574.99 rows=89769 width=20) (actual time=65.777..65.778 rows=127375 loops=1)
        Buckets: 131072  Batches: 1  Memory Usage: 7493kB
        Buffers: shared hit=695 read=10057
        ->  Seq Scan on findings  (cost=0.00..12574.99 rows=89769 width=20) (actual time=0.048..38.833 rows=127375 loops=1)
              Filter: ((published IS TRUE) AND (drop_reason IS NULL))
              Rows Removed by Filter: 54924
              Buffers: shared hit=695 read=10057
Planning:
  Buffers: shared hit=943 read=38
Planning Time: 8.079 ms
JIT:
  Functions: 139
  Options: Inlining false, Optimization false, Expressions true, Deforming true
  Timing: Generation 10.009 ms (Deform 5.173 ms), Inlining 0.000 ms, Optimization 3.882 ms, Emission 74.856 ms, Total 88.747 ms
Execution Time: 207.556 ms
```

### `summary 30d`: `_usage_and_duration`

```text
Aggregate  (cost=175959.16..175959.17 rows=1 width=136) (actual time=103.876..103.884 rows=1 loops=1)
  Buffers: shared hit=49825
  ->  Nested Loop  (cost=7022.25..11195.50 rows=3387 width=36) (actual time=53.394..57.210 rows=3614 loops=1)
        Buffers: shared hit=6517
        ->  Hash Join  (cost=7022.10..10942.17 rows=4457 width=52) (actual time=53.283..55.258 rows=4517 loops=1)
              Hash Cond: (code_changes.id = runs.code_change_id)
              Buffers: shared hit=6397
              ->  Seq Scan on code_changes  (cost=0.00..3693.00 rows=36500 width=32) (actual time=41.718..46.915 rows=36500 loops=1)
                    Buffers: shared hit=3328
              ->  Hash  (cost=6966.39..6966.39 rows=4457 width=52) (actual time=3.765..3.765 rows=4517 loops=1)
                    Buckets: 8192  Batches: 1  Memory Usage: 453kB
                    Buffers: shared hit=3069
                    ->  Index Scan using ix_runs_created_id on runs  (cost=0.29..6966.39 rows=4457 width=52) (actual time=0.037..2.626 rows=4517 loops=1)
                          Index Cond: ((created_at >= '2026-10-08 01:45:09.161137+00'::timestamp with time zone) AND (created_at <= '2026-11-07 01:45:09.161137+00'::timestamp with time zone))
                          Buffers: shared hit=3069
        ->  Memoize  (cost=0.15..5.76 rows=1 width=16) (actual time=0.000..0.000 rows=1 loops=4517)
              Cache Key: code_changes.repository_id
              Cache Mode: logical
              Hits: 4492  Misses: 25  Evictions: 0  Overflows: 0  Memory Usage: 3kB
              Buffers: shared hit=120
              ->  Index Scan using uq_repositories_owner on repositories  (cost=0.14..5.75 rows=1 width=16) (actual time=0.010..0.010 rows=1 loops=25)
                    Index Cond: (id = code_changes.repository_id)
                    Filter: (EXISTS(SubPlan 4) OR EXISTS(SubPlan 5))
                    Rows Removed by Filter: 0
                    Buffers: shared hit=120
                    SubPlan 4
                      ->  Nested Loop  (cost=0.00..3.46 rows=1 width=0) (actual time=0.005..0.005 rows=1 loops=25)
                            Buffers: shared hit=65
                            ->  Nested Loop  (cost=0.00..2.10 rows=1 width=16) (actual time=0.003..0.003 rows=1 loops=25)
                                  Join Filter: (provider_installations.workspace_id = github_user_workspace_access.workspace_id)
                                  Rows Removed by Join Filter: 0
                                  Buffers: shared hit=45
                                  ->  Seq Scan on provider_installations  (cost=0.00..1.05 rows=1 width=32) (actual time=0.001..0.001 rows=1 loops=25)
                                        Filter: ((workspace_id = ANY ('{858fd0e5-ca21-40d3-a635-8257d2805b71,c1c34d37-8d54-41c5-8cc4-7f110348200e}'::uuid[])) AND (id = repositories.provider_installation_id) AND ((provider)::text = 'github'::text))
                                        Rows Removed by Filter: 1
                                        Buffers: shared hit=25
                                  ->  Seq Scan on github_user_workspace_access  (cost=0.00..1.02 rows=2 width=16) (actual time=0.001..0.001 rows=2 loops=20)
                                        Filter: (github_user_id = '42'::bigint)
                                        Buffers: shared hit=20
                            ->  Seq Scan on github_user_repository_access  (cost=0.00..1.35 rows=1 width=16) (actual time=0.001..0.001 rows=1 loops=20)
                                  Filter: ((github_user_id = '42'::bigint) AND (provider_installation_id = repositories.provider_installation_id) AND (repository_external_id = repositories.external_id))
                                  Rows Removed by Filter: 10
                                  Buffers: shared hit=20
                    SubPlan 5
                      ->  Result  (cost=0.00..2.13 rows=1 width=0) (actual time=0.004..0.004 rows=0 loops=5)
                            One-Time Filter: (repositories.installation_available IS TRUE)
                            Buffers: shared hit=5
                            ->  Nested Loop  (cost=0.00..2.13 rows=1 width=0) (actual time=0.003..0.003 rows=0 loops=5)
                                  Join Filter: (github_user_workspace_access_1.workspace_id = bitbucket_repository_identities.workspace_id)
                                  Buffers: shared hit=5
                                  ->  Nested Loop  (cost=0.00..2.11 rows=1 width=64) (actual time=0.002..0.003 rows=0 loops=5)
                                        Join Filter: (github_user_workspace_access_1.workspace_id = bitbucket_connections.workspace_id)
                                        Buffers: shared hit=5
                                        ->  Nested Loop  (cost=0.00..2.10 rows=1 width=48) (actual time=0.002..0.002 rows=0 loops=5)
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
    ->  Aggregate  (cost=16.20..16.21 rows=1 width=8) (actual time=0.004..0.005 rows=1 loops=3614)
          Buffers: shared hit=14436
          ->  Bitmap Heap Scan on usage_events  (cost=4.44..16.19 rows=3 width=4) (actual time=0.003..0.003 rows=3 loops=3614)
                Recheck Cond: (run_id = runs.id)
                Heap Blocks: exact=3594
                Buffers: shared hit=14436
                ->  Bitmap Index Scan on ix_usage_events_run_created  (cost=0.00..4.44 rows=3 width=0) (actual time=0.002..0.002 rows=3 loops=3614)
                      Index Cond: (run_id = runs.id)
                      Buffers: shared hit=10842
  SubPlan 2
    ->  Aggregate  (cost=16.20..16.21 rows=1 width=8) (actual time=0.003..0.003 rows=1 loops=3614)
          Buffers: shared hit=14436
          ->  Bitmap Heap Scan on usage_events usage_events_1  (cost=4.44..16.19 rows=3 width=4) (actual time=0.002..0.002 rows=3 loops=3614)
                Recheck Cond: (run_id = runs.id)
                Heap Blocks: exact=3594
                Buffers: shared hit=14436
                ->  Bitmap Index Scan on ix_usage_events_run_created  (cost=0.00..4.44 rows=3 width=0) (actual time=0.001..0.001 rows=3 loops=3614)
                      Index Cond: (run_id = runs.id)
                      Buffers: shared hit=10842
  SubPlan 3
    ->  Aggregate  (cost=16.20..16.21 rows=1 width=32) (actual time=0.004..0.004 rows=1 loops=3614)
          Buffers: shared hit=14436
          ->  Bitmap Heap Scan on usage_events usage_events_2  (cost=4.44..16.19 rows=3 width=6) (actual time=0.002..0.002 rows=3 loops=3614)
                Recheck Cond: (run_id = runs.id)
                Heap Blocks: exact=3594
                Buffers: shared hit=14436
                ->  Bitmap Index Scan on ix_usage_events_run_created  (cost=0.00..4.44 rows=3 width=0) (actual time=0.001..0.001 rows=3 loops=3614)
                      Index Cond: (run_id = runs.id)
                      Buffers: shared hit=10842
Planning:
  Buffers: shared hit=127 read=4
Planning Time: 2.235 ms
JIT:
  Functions: 91
  Options: Inlining false, Optimization false, Expressions true, Deforming true
  Timing: Generation 5.173 ms (Deform 2.492 ms), Inlining 0.000 ms, Optimization 1.513 ms, Emission 40.315 ms, Total 47.001 ms
Execution Time: 109.650 ms
```

### `summary 30d`: `load_review_results`

```text
Unique  (cost=16487.20..16707.21 rows=1785 width=192) (actual time=38.488..48.418 rows=1998 loops=1)
  Buffers: shared hit=233 read=12669
  ->  Merge Left Join  (cost=16487.20..16702.68 rows=1811 width=192) (actual time=38.486..47.834 rows=1998 loops=1)
        Merge Cond: (run_actions.run_id = run_action_responses.run_id)
        Join Filter: (((run_action_responses.id)::character varying)::text = run_actions.response_ref)
        Buffers: shared hit=233 read=12669
        ->  Gather Merge  (cost=16487.19..16698.12 rows=1811 width=192) (actual time=38.457..47.536 rows=1998 loops=1)
              Workers Planned: 2
              Workers Launched: 2
              Buffers: shared hit=233 read=12669
              ->  Sort  (cost=15487.17..15489.06 rows=755 width=192) (actual time=17.292..17.357 rows=666 loops=3)
                    Sort Key: run_actions.run_id, run_actions.index DESC
                    Sort Method: quicksort  Memory: 731kB
                    Buffers: shared hit=233 read=12669
                    Worker 0:  Sort Method: quicksort  Memory: 661kB
                    Worker 1:  Sort Method: quicksort  Memory: 663kB
                    ->  Parallel Seq Scan on run_actions  (cost=5.09..15451.08 rows=755 width=192) (actual time=14.478..16.330 rows=666 loops=3)
                          Filter: (((tool)::text = 'review.postprocess'::text) AND (run_id = ANY ('{87ddf900-176d-4744-93f9-005554629d85,387b6e0b-d9a4-43f1-ac6d-bb384c960d5a,25249498-f6f4-4a29-9b7b-e23aa1dd8bd5,976c64c3-ea17-4ef4-9aa3-c62757d63671,4e85e981-41ca-4c97-b385-f9796c46967d,a3a922e5-ac4e-466c-9f3c-33b3bc6318df,21c4f9ee-9fff-4be9-bdc1-ab4365421440,01e749cf-a0f9-4c7c-962c-df62190ef63f,c4016d08-7a88-4227-bb05-6e0ff21fb797,4bd0246f-f251-48b8-8751-12d06af67f40, [75011 chars cut]
                          Rows Removed by Filter: 119562
                          Buffers: shared hit=147 read=12669
        ->  Sort  (cost=0.01..0.02 rows=1 width=64) (actual time=0.025..0.026 rows=0 loops=1)
              Sort Key: run_action_responses.run_id
              Sort Method: quicksort  Memory: 25kB
              ->  Seq Scan on run_action_responses  (cost=0.00..0.00 rows=1 width=64) (actual time=0.008..0.009 rows=0 loops=1)
Planning:
  Buffers: shared hit=145 read=8
Planning Time: 2.758 ms
Execution Time: 48.855 ms
```

### `runs first page`: `list_runs`

```text
Limit  (cost=1.02..2296.66 rows=21 width=3187) (actual time=8.515..9.527 rows=21 loops=1)
  Buffers: shared hit=1753
  ->  Nested Loop Left Join  (cost=1.02..4548112.62 rows=41605 width=3187) (actual time=8.514..9.523 rows=21 loops=1)
        Join Filter: (runs.origin_provider_installation_id = provider_installations.id)
        Rows Removed by Join Filter: 8
        Buffers: shared hit=1753
        ->  Nested Loop  (cost=1.02..409651.87 rows=41605 width=2880) (actual time=0.110..0.455 rows=21 loops=1)
              Join Filter: (code_changes.repository_id = repositories.id)
              Rows Removed by Join Filter: 190
              Buffers: shared hit=226
              ->  Nested Loop  (cost=1.02..394624.68 rows=41605 width=2864) (actual time=0.094..0.402 rows=21 loops=1)
                    Buffers: shared hit=225
                    ->  Nested Loop Semi Join  (cost=0.73..371221.93 rows=41605 width=2133) (actual time=0.081..0.344 rows=21 loops=1)
                          Buffers: shared hit=162
                          ->  Index Scan Backward using ix_runs_created_id on runs  (cost=0.29..24263.55 rows=54744 width=2117) (actual time=0.016..0.035 rows=26 loops=1)
                                Buffers: shared hit=22
                          ->  Nested Loop  (cost=0.44..6.33 rows=1 width=16) (actual time=0.011..0.011 rows=1 loops=26)
                                Buffers: shared hit=140
                                ->  Index Scan using code_changes_pkey on code_changes code_changes_1  (cost=0.29..0.56 rows=1 width=32) (actual time=0.005..0.005 rows=1 loops=26)
                                      Index Cond: (id = runs.code_change_id)
                                      Buffers: shared hit=78
                                ->  Memoize  (cost=0.15..5.76 rows=1 width=16) (actual time=0.006..0.006 rows=1 loops=26)
                                      Cache Key: code_changes_1.repository_id
                                      Cache Mode: logical
                                      Hits: 13  Misses: 13  Evictions: 0  Overflows: 0  Memory Usage: 2kB
                                      Buffers: shared hit=62
                                      ->  Index Scan using uq_repositories_owner on repositories repositories_1  (cost=0.14..5.75 rows=1 width=16) (actual time=0.010..0.010 rows=1 loops=13)
                                            Index Cond: (id = code_changes_1.repository_id)
                                            Filter: (EXISTS(SubPlan 9) OR EXISTS(SubPlan 10))
                                            Rows Removed by Filter: 0
                                            Buffers: shared hit=62
                                            SubPlan 9
                                              ->  Nested Loop  (cost=0.00..3.46 rows=1 width=0) (actual time=0.006..0.006 rows=1 loops=13)
                                                    Buffers: shared hit=33
                                                    ->  Nested Loop  (cost=0.00..2.10 rows=1 width=16) (actual time=0.004..0.004 rows=1 loops=13)
                                                          Join Filter: (provider_installations_1.workspace_id = github_user_workspace_access.workspace_id)
                                                          Rows Removed by Join Filter: 0
                                                          Buffers: shared hit=23
                                                          ->  Seq Scan on provider_installations provider_installations_1  (cost=0.00..1.05 rows=1 width=32) (actual time=0.002..0.002 rows=1 loops=13)
                                                                Filter: ((workspace_id = ANY ('{858fd0e5-ca21-40d3-a635-8257d2805b71,c1c34d37-8d54-41c5-8cc4-7f110348200e}'::uuid[])) AND (id = repositories_1.provider_installation_id) AND ((provider)::text = 'github'::text))
                                                                Rows Removed by Filter: 1
                                                                Buffers: shared hit=13
                                                          ->  Seq Scan on github_user_workspace_access  (cost=0.00..1.02 rows=2 width=16) (actual time=0.001..0.001 rows=1 loops=10)
                                                                Filter: (github_user_id = '42'::bigint)
                                                                Buffers: shared hit=10
                                                    ->  Seq Scan on github_user_repository_access  (cost=0.00..1.35 rows=1 width=16) (actual time=0.002..0.002 rows=1 loops=10)
                                                          Filter: ((github_user_id = '42'::bigint) AND (provider_installation_id = repositories_1.provider_installation_id) AND (repository_external_id = repositories_1.external_id))
                                                          Rows Removed by Filter: 9
                                                          Buffers: shared hit=10
                                            SubPlan 10
                                              ->  Result  (cost=0.00..2.13 rows=1 width=0) (actual time=0.004..0.004 rows=0 loops=3)
                                                    One-Time Filter: (repositories_1.installation_available IS TRUE)
                                                    Buffers: shared hit=3
                                                    ->  Nested Loop  (cost=0.00..2.13 rows=1 width=0) (actual time=0.003..0.004 rows=0 loops=3)
                                                          Join Filter: (github_user_workspace_access_1.workspace_id = bitbucket_repository_identities.workspace_id)
                                                          Buffers: shared hit=3
                                                          ->  Nested Loop  (cost=0.00..2.11 rows=1 width=64) (actual time=0.003..0.003 rows=0 loops=3)
                                                                Join Filter: (github_user_workspace_access_1.workspace_id = bitbucket_connections.workspace_id)
                                                                Buffers: shared hit=3
                                                                ->  Nested Loop  (cost=0.00..2.10 rows=1 width=48) (actual time=0.003..0.003 rows=0 loops=3)
                                                                      Join Filter: (provider_installations_2.workspace_id = github_user_workspace_access_1.workspace_id)
                                                                      Buffers: shared hit=3
                                                                      ->  Seq Scan on provider_installations provider_installations_2  (cost=0.00..1.05 rows=1 width=32) (actual time=0.002..0.002 rows=0 loops=3)
                                                                            Filter: ((deleted_at IS NULL) AND (workspace_id = ANY ('{858fd0e5-ca21-40d3-a635-8257d2805b71,c1c34d37-8d54-41c5-8cc4-7f110348200e}'::uuid[])) AND (id = repositories_1.provider_installation_id) AND ((provider)::text = 'bitbucket'::text))
                                                                            Rows Removed by Filter: 3
                                                                            Buffers: shared hit=3
                                                                      ->  Seq Scan on github_user_workspace_access github_user_workspace_access_1  (cost=0.00..1.02 rows=2 width=16) (never executed)
                                                                            Filter: (github_user_id = '42'::bigint)
                                                                ->  Seq Scan on bitbucket_connections  (cost=0.00..0.00 rows=1 width=32) (never executed)
                                                                      Filter: ((revoked_at IS NULL) AND (provider_installation_id = repositories_1.provider_installation_id))
                                                          ->  Seq Scan on bitbucket_repository_identities  (cost=0.00..0.00 rows=1 width=32) (never executed)
                                                                Filter: ((provider_installation_id = repositories_1.provider_installation_id) AND (repository_id = repositories_1.id))
                    ->  Index Scan using code_changes_pkey on code_changes  (cost=0.29..0.56 rows=1 width=747) (actual time=0.002..0.002 rows=1 loops=21)
                          Index Cond: (id = runs.code_change_id)
                          Buffers: shared hit=63
              ->  Materialize  (cost=0.00..1.38 rows=25 width=32) (actual time=0.000..0.001 rows=10 loops=21)
                    Buffers: shared hit=1
                    ->  Seq Scan on repositories  (cost=0.00..1.25 rows=25 width=32) (actual time=0.002..0.004 rows=19 loops=1)
                          Buffers: shared hit=1
        ->  Materialize  (cost=0.00..1.04 rows=3 width=23) (actual time=0.001..0.001 rows=1 loops=21)
              Buffers: shared hit=1
              ->  Seq Scan on provider_installations  (cost=0.00..1.03 rows=3 width=23) (actual time=0.001..0.001 rows=2 loops=1)
                    Buffers: shared hit=1
        SubPlan 1
          ->  Limit  (cost=5.78..9.39 rows=1 width=40) (actual time=0.009..0.010 rows=1 loops=21)
                Buffers: shared hit=103
                ->  Incremental Sort  (cost=5.78..16.61 rows=3 width=40) (actual time=0.009..0.009 rows=1 loops=21)
                      Sort Key: usage_events.created_at DESC, usage_events.id DESC
                      Presorted Key: usage_events.created_at
                      Full-sort Groups: 21  Sort Methods: top-N heapsort, quicksort  Average Memory: 25kB  Peak Memory: 25kB
                      Buffers: shared hit=103
                      ->  Index Scan Backward using ix_usage_events_run_created on usage_events  (cost=0.42..16.47 rows=3 width=40) (actual time=0.004..0.006 rows=3 loops=21)
                            Index Cond: (run_id = runs.id)
                            Buffers: shared hit=100
        SubPlan 2
          ->  Aggregate  (cost=8.56..8.57 rows=1 width=8) (actual time=0.009..0.009 rows=1 loops=21)
                Buffers: shared hit=64
                ->  Index Only Scan using run_actions_run_id_index_key on run_actions  (cost=0.42..8.54 rows=7 width=0) (actual time=0.007..0.008 rows=6 loops=21)
                      Index Cond: (run_id = runs.id)
                      Heap Fetches: 0
                      Buffers: shared hit=64
        SubPlan 3
          ->  Aggregate  (cost=24.52..24.53 rows=1 width=1) (actual time=0.006..0.006 rows=1 loops=21)
                Buffers: shared hit=81
                ->  Index Scan using uq_code_change_diffs_run_filename on code_change_diffs  (cost=0.42..24.51 rows=5 width=1) (actual time=0.004..0.006 rows=4 loops=21)
                      Index Cond: (run_id = runs.id)
                      Buffers: shared hit=81
        SubPlan 5
          ->  Seq Scan on context_payloads  (cost=0.00..1951.31 rows=5 width=16) (actual time=0.027..7.812 rows=952 loops=1)
                Filter: ((NOT (summary ? 'reviewable_lines'::text)) AND ((summary ->> 'status'::text) = 'summary_only'::text))
                Rows Removed by Filter: 51580
                Buffers: shared hit=1032
        SubPlan 6
          ->  Aggregate  (cost=16.20..16.21 rows=1 width=8) (actual time=0.005..0.005 rows=1 loops=21)
                Buffers: shared hit=82
                ->  Bitmap Heap Scan on usage_events usage_events_1  (cost=4.44..16.19 rows=3 width=4) (actual time=0.003..0.003 rows=3 loops=21)
                      Recheck Cond: (run_id = runs.id)
                      Heap Blocks: exact=19
                      Buffers: shared hit=82
                      ->  Bitmap Index Scan on ix_usage_events_run_created  (cost=0.00..4.44 rows=3 width=0) (actual time=0.002..0.002 rows=3 loops=21)
                            Index Cond: (run_id = runs.id)
                            Buffers: shared hit=63
        SubPlan 7
          ->  Aggregate  (cost=16.20..16.21 rows=1 width=8) (actual time=0.003..0.003 rows=1 loops=21)
                Buffers: shared hit=82
                ->  Bitmap Heap Scan on usage_events usage_events_2  (cost=4.44..16.19 rows=3 width=4) (actual time=0.002..0.002 rows=3 loops=21)
                      Recheck Cond: (run_id = runs.id)
                      Heap Blocks: exact=19
                      Buffers: shared hit=82
                      ->  Bitmap Index Scan on ix_usage_events_run_created  (cost=0.00..4.44 rows=3 width=0) (actual time=0.001..0.001 rows=3 loops=21)
                            Index Cond: (run_id = runs.id)
                            Buffers: shared hit=63
        SubPlan 8
          ->  Aggregate  (cost=16.20..16.21 rows=1 width=32) (actual time=0.005..0.005 rows=1 loops=21)
                Buffers: shared hit=82
                ->  Bitmap Heap Scan on usage_events usage_events_3  (cost=4.44..16.19 rows=3 width=6) (actual time=0.002..0.002 rows=3 loops=21)
                      Recheck Cond: (run_id = runs.id)
                      Heap Blocks: exact=19
                      Buffers: shared hit=82
                      ->  Bitmap Index Scan on ix_usage_events_run_created  (cost=0.00..4.44 rows=3 width=0) (actual time=0.001..0.001 rows=3 loops=21)
                            Index Cond: (run_id = runs.id)
                            Buffers: shared hit=63
Planning:
  Buffers: shared hit=256 read=4
Planning Time: 4.140 ms
Execution Time: 10.153 ms
```

### `runs by cursor`: `list_runs`

```text
Limit  (cost=1.02..2319.45 rows=21 width=3187) (actual time=23.255..24.207 rows=21 loops=1)
  Buffers: shared hit=20511
  ->  Nested Loop Left Join  (cost=1.02..2288731.19 rows=20731 width=3187) (actual time=23.253..24.203 rows=21 loops=1)
        Join Filter: (runs.origin_provider_installation_id = provider_installations.id)
        Rows Removed by Join Filter: 8
        Buffers: shared hit=20511
        ->  Nested Loop  (cost=1.02..226612.41 rows=20731 width=2880) (actual time=14.896..15.237 rows=21 loops=1)
              Join Filter: (code_changes.repository_id = repositories.id)
              Rows Removed by Join Filter: 186
              Buffers: shared hit=18973
              ->  Nested Loop  (cost=1.02..219123.94 rows=20731 width=2864) (actual time=14.879..15.183 rows=21 loops=1)
                    Join Filter: (code_changes.id = runs.code_change_id)
                    Buffers: shared hit=18972
                    ->  Nested Loop Semi Join  (cost=0.73..204561.53 rows=20731 width=2133) (actual time=14.864..15.122 rows=21 loops=1)
                          Buffers: shared hit=18909
                          ->  Index Scan Backward using ix_runs_created_id on runs  (cost=0.29..24674.13 rows=27278 width=2117) (actual time=14.742..14.765 rows=27 loops=1)
                                Filter: ((created_at < '2026-05-08 08:19:58.682607+00'::timestamp with time zone) OR ((created_at = '2026-05-08 08:19:58.682607+00'::timestamp with time zone) AND (id < '10088395-1279-4f92-94bb-bddde09c7029'::uuid)))
                                Rows Removed by Filter: 27437
                                Buffers: shared hit=18733
                          ->  Nested Loop  (cost=0.44..6.58 rows=1 width=16) (actual time=0.012..0.012 rows=1 loops=27)
                                Buffers: shared hit=176
                                ->  Index Scan using code_changes_pkey on code_changes code_changes_1  (cost=0.29..0.82 rows=1 width=32) (actual time=0.004..0.004 rows=1 loops=27)
                                      Index Cond: (id = runs.code_change_id)
                                      Buffers: shared hit=81
                                ->  Memoize  (cost=0.15..5.76 rows=1 width=16) (actual time=0.008..0.008 rows=1 loops=27)
                                      Cache Key: code_changes_1.repository_id
                                      Cache Mode: logical
                                      Hits: 7  Misses: 20  Evictions: 0  Overflows: 0  Memory Usage: 3kB
                                      Buffers: shared hit=95
                                      ->  Index Scan using uq_repositories_owner on repositories repositories_1  (cost=0.14..5.75 rows=1 width=16) (actual time=0.009..0.009 rows=1 loops=20)
                                            Index Cond: (id = code_changes_1.repository_id)
                                            Filter: (EXISTS(SubPlan 9) OR EXISTS(SubPlan 10))
                                            Rows Removed by Filter: 0
                                            Buffers: shared hit=95
                                            SubPlan 9
                                              ->  Nested Loop  (cost=0.00..3.46 rows=1 width=0) (actual time=0.005..0.005 rows=1 loops=20)
                                                    Buffers: shared hit=50
                                                    ->  Nested Loop  (cost=0.00..2.10 rows=1 width=16) (actual time=0.003..0.003 rows=1 loops=20)
                                                          Join Filter: (provider_installations_1.workspace_id = github_user_workspace_access.workspace_id)
                                                          Rows Removed by Join Filter: 0
                                                          Buffers: shared hit=35
                                                          ->  Seq Scan on provider_installations provider_installations_1  (cost=0.00..1.05 rows=1 width=32) (actual time=0.001..0.001 rows=1 loops=20)
                                                                Filter: ((workspace_id = ANY ('{858fd0e5-ca21-40d3-a635-8257d2805b71,c1c34d37-8d54-41c5-8cc4-7f110348200e}'::uuid[])) AND (id = repositories_1.provider_installation_id) AND ((provider)::text = 'github'::text))
                                                                Rows Removed by Filter: 1
                                                                Buffers: shared hit=20
                                                          ->  Seq Scan on github_user_workspace_access  (cost=0.00..1.02 rows=2 width=16) (actual time=0.001..0.001 rows=1 loops=15)
                                                                Filter: (github_user_id = '42'::bigint)
                                                                Buffers: shared hit=15
                                                    ->  Seq Scan on github_user_repository_access  (cost=0.00..1.35 rows=1 width=16) (actual time=0.001..0.001 rows=1 loops=15)
                                                          Filter: ((github_user_id = '42'::bigint) AND (provider_installation_id = repositories_1.provider_installation_id) AND (repository_external_id = repositories_1.external_id))
                                                          Rows Removed by Filter: 10
                                                          Buffers: shared hit=15
                                            SubPlan 10
                                              ->  Result  (cost=0.00..2.13 rows=1 width=0) (actual time=0.003..0.003 rows=0 loops=5)
                                                    One-Time Filter: (repositories_1.installation_available IS TRUE)
                                                    Buffers: shared hit=5
                                                    ->  Nested Loop  (cost=0.00..2.13 rows=1 width=0) (actual time=0.003..0.003 rows=0 loops=5)
                                                          Join Filter: (github_user_workspace_access_1.workspace_id = bitbucket_repository_identities.workspace_id)
                                                          Buffers: shared hit=5
                                                          ->  Nested Loop  (cost=0.00..2.11 rows=1 width=64) (actual time=0.002..0.002 rows=0 loops=5)
                                                                Join Filter: (github_user_workspace_access_1.workspace_id = bitbucket_connections.workspace_id)
                                                                Buffers: shared hit=5
                                                                ->  Nested Loop  (cost=0.00..2.10 rows=1 width=48) (actual time=0.002..0.002 rows=0 loops=5)
                                                                      Join Filter: (provider_installations_2.workspace_id = github_user_workspace_access_1.workspace_id)
                                                                      Buffers: shared hit=5
                                                                      ->  Seq Scan on provider_installations provider_installations_2  (cost=0.00..1.05 rows=1 width=32) (actual time=0.001..0.001 rows=0 loops=5)
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
                    ->  Seq Scan on repositories  (cost=0.00..1.25 rows=25 width=32) (actual time=0.002..0.005 rows=20 loops=1)
                          Buffers: shared hit=1
        ->  Materialize  (cost=0.00..1.04 rows=3 width=23) (actual time=0.001..0.001 rows=1 loops=21)
              Buffers: shared hit=1
              ->  Seq Scan on provider_installations  (cost=0.00..1.03 rows=3 width=23) (actual time=0.003..0.004 rows=2 loops=1)
                    Buffers: shared hit=1
        SubPlan 1
          ->  Limit  (cost=5.78..9.39 rows=1 width=40) (actual time=0.008..0.008 rows=1 loops=21)
                Buffers: shared hit=105
                ->  Incremental Sort  (cost=5.78..16.61 rows=3 width=40) (actual time=0.007..0.007 rows=1 loops=21)
                      Sort Key: usage_events.created_at DESC, usage_events.id DESC
                      Presorted Key: usage_events.created_at
                      Full-sort Groups: 21  Sort Method: top-N heapsort  Average Memory: 25kB  Peak Memory: 25kB
                      Buffers: shared hit=105
                      ->  Index Scan Backward using ix_usage_events_run_created on usage_events  (cost=0.42..16.47 rows=3 width=40) (actual time=0.004..0.005 rows=3 loops=21)
                            Index Cond: (run_id = runs.id)
                            Buffers: shared hit=105
        SubPlan 2
          ->  Aggregate  (cost=8.56..8.57 rows=1 width=8) (actual time=0.006..0.006 rows=1 loops=21)
                Buffers: shared hit=64
                ->  Index Only Scan using run_actions_run_id_index_key on run_actions  (cost=0.42..8.54 rows=7 width=0) (actual time=0.004..0.005 rows=7 loops=21)
                      Index Cond: (run_id = runs.id)
                      Heap Fetches: 0
                      Buffers: shared hit=64
        SubPlan 3
          ->  Aggregate  (cost=24.52..24.53 rows=1 width=1) (actual time=0.007..0.007 rows=1 loops=21)
                Buffers: shared hit=84
                ->  Index Scan using uq_code_change_diffs_run_filename on code_change_diffs  (cost=0.42..24.51 rows=5 width=1) (actual time=0.004..0.006 rows=5 loops=21)
                      Index Cond: (run_id = runs.id)
                      Buffers: shared hit=84
        SubPlan 5
          ->  Seq Scan on context_payloads  (cost=0.00..1951.31 rows=5 width=16) (actual time=0.026..7.847 rows=952 loops=1)
                Filter: ((NOT (summary ? 'reviewable_lines'::text)) AND ((summary ->> 'status'::text) = 'summary_only'::text))
                Rows Removed by Filter: 51580
                Buffers: shared hit=1032
        SubPlan 6
          ->  Aggregate  (cost=16.20..16.21 rows=1 width=8) (actual time=0.005..0.005 rows=1 loops=21)
                Buffers: shared hit=84
                ->  Bitmap Heap Scan on usage_events usage_events_1  (cost=4.44..16.19 rows=3 width=4) (actual time=0.003..0.003 rows=3 loops=21)
                      Recheck Cond: (run_id = runs.id)
                      Heap Blocks: exact=21
                      Buffers: shared hit=84
                      ->  Bitmap Index Scan on ix_usage_events_run_created  (cost=0.00..4.44 rows=3 width=0) (actual time=0.002..0.002 rows=3 loops=21)
                            Index Cond: (run_id = runs.id)
                            Buffers: shared hit=63
        SubPlan 7
          ->  Aggregate  (cost=16.20..16.21 rows=1 width=8) (actual time=0.003..0.003 rows=1 loops=21)
                Buffers: shared hit=84
                ->  Bitmap Heap Scan on usage_events usage_events_2  (cost=4.44..16.19 rows=3 width=4) (actual time=0.002..0.002 rows=3 loops=21)
                      Recheck Cond: (run_id = runs.id)
                      Heap Blocks: exact=21
                      Buffers: shared hit=84
                      ->  Bitmap Index Scan on ix_usage_events_run_created  (cost=0.00..4.44 rows=3 width=0) (actual time=0.001..0.001 rows=3 loops=21)
                            Index Cond: (run_id = runs.id)
                            Buffers: shared hit=63
        SubPlan 8
          ->  Aggregate  (cost=16.20..16.21 rows=1 width=32) (actual time=0.004..0.004 rows=1 loops=21)
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
Planning Time: 2.968 ms
Execution Time: 24.730 ms
```
