# SYSTEM_DESIGN §15: журнал выбора LLM (OQ-2) и аудит #178

Вынесено из [SYSTEM_DESIGN §15](SYSTEM_DESIGN.md#15-открытые-вопросы-для-команды) в #380: свидетельства выбора модели (#46), пробы кандидатов, живые прогоны и аудит формата #178. Действующие решения, строгий D7, курс и граница цены — в SYSTEM_DESIGN §15; raw baselines — в [TEST_PLAN §3.2.1–3.2.5](test-plan/eval-baselines.md#321-зафиксированный-raw-baseline-82-2026-10-13).

## OQ-2: свидетельства выбора пары (#46)

`gpt-4.1-mini` (выбор #33) из пары убран: провайдер недоступен аккаунту — HTTP 400 «No providers available for model 'gpt-4.1-mini' with given preferences» остаётся и при наличии кредитов, и без `response_format` ([run 37298451583](https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/37298451583)), то есть это не отказ strict-режима; причина неизвестна: правил маршрутизации на организации и ключе нет (подтвердил куратор, 05.10.2026), а preferences шлюз не передаёт. Strict `json_schema` у обеих моделей подтверждён на обслуживших маршрутах (Mistral AI; OVHcloud и Scaleway — таблица ниже) workflow `LLM live run` на ветке `fix/46-oq2-live-run` @ `c3811b7`: в [run 37304578603](https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/37304578603) оба шага — `calls == [primary]`. Из трёх прогонов с дефолтами ([37304499334](https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/37304499334), [37304578603](https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/37304578603), [37304646906](https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/37304646906)) зелёный по обоим шагам — 1 из 3. Первый ответ валиден у `mistral-small-4` в 2 из 3 на ветке и в 4 из 7 по всем прогонам #46, где она основная; у `mistral-small-3.2-24b` — в 2 из 3 на ветке и в 2 из 4 всего; каждый repair дал валидный ответ. Причина repair в артефактах ветки не сохраняется: в пробах с выгрузкой сырого ответа это была только семантика PIPELINE_SPEC §9 — `start_line == line` у `mistral-small-4`, `summary.problem` из двух предложений у `mistral-small-3.2-24b`; в run 37304646906 итоговые находки содержат `start_line: null`, что согласуется с `start_line == line`.

## Строгий D7: прогон после мержа #46

После мержа #46 [run 37342619900](https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/37342619900) на `main` дал `calls == [primary]` в обоих review-шагах; `validate_findings.py` подтвердил `OK ReviewOutput 1 items` и `OK ReviewOutput 3 items`. Этот run ещё не содержал шагов конвенций.

## Аудит формата первого ответа (#178, 20.10.2026)

**Аудит формата первого ответа (#178, 20.10.2026).** Офлайн-разбор неизменённых
ответов baseline 15.10 подтверждает 17 невалидных кейсов: 15 кейсов / 33 находки
без обязательного префикса атрибуции, 9 кейсов / 22 диапазона `start_line == line`
(обратных диапазонов нет) и два кейса с нарушением порядка severity/confidence
(RES-03, RES-05). Причины пересекаются; полная матрица 17 кейсов и суммы по
`rule_name` — [README корпуса](../test-prs-dataset/README.md#first-answer-format-audit-and-current-gateway-acceptance-178).
Имена правил существуют в default backend/frontend наборах, но это само по себе
не доказывает применимость правила или семантическую истинность находки.

Для исторической съёмки 15.10 strict raw-first validity — **7/24**: независимый
`review/scripts/validate_findings.py` проверяет первый сырой ответ до любой
нормализации, repair/fallback, lint-фильтрации, anchoring и verifier; знаменатель
включает все 24 активных кейса. TP/FP/FN **2/2/17**, critical recall **2/5** и
verdict agreement **5/24** относятся к тем же историческим ответам. `warnings=[]`
выше — свидетельство на дереве старой съёмки. После #177 pre-refresh replay на
candidate `47f9add` предупреждал `Static input path set changed; refresh the complete baseline`.
Этот аудит не менял raw bytes или digests; новая полная съёмка описана ниже.

Текущий production-путь уже отличается от строгой сырой метрики:
`llm/answers.py::validate_review_answer` после JSON/schema-проверки превращает
равный диапазон в `start_line: null` и сортирует findings по severity, затем
confidence, до typed validation. `ReviewFinding` проверяет диапазоны и заголовки,
`ReviewOutput` — порядок; legacy-префикс атрибуции они не проверяют. Офлайн
пропуск тех же ответов через gateway validator даёт **24/24 normalized
acceptance**, что не заменяет **7/24 raw-first validity**. Downstream
`FindingsPostProcessor._repair_attribution` сбрасывает неверную legacy-атрибуцию,
а не делает платный repair-вызов. Поэтому 17 сырых ошибок не доказывают 17
production repair-вызовов; экономия стоимости и задержки этим аудитом не измерена.

## Закрытие OQ-2: проверка по D7

**Закрытие OQ-2: проверка по D7.** Значения — каталог EUrouter (`GET https://api.eurouter.ai/api/v1/models` и `/endpoints`) на 05.10.2026; цены — за 1 млн токенов, по эндпоинтам и в их валюте.

| Требование D7 | `mistral-small-4` (основная) | `mistral-small-3.2-24b` (fallback) |
|---|---|---|
| Strict structured output | шлюз шлёт `json_schema` со `strict: true` и схемой `review-output.schema.json` (без `$`-аннотаций); принял маршрут Mistral AI (`response.provider = mistral`) — workflow `LLM live run`, [run 37304578603](https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/37304578603). Regolo и AKI.IO вызовов не обслуживали — не проверены | приняли маршруты Scaleway ([run 37304578603](https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/37304578603)) и OVHcloud ([run 37304499334](https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/37304499334)); GreenPT не проверен |
| Контекст ≥ 60 000 | 262 144 | 128 000 у Scaleway и GreenPT, 131 072 у OVHcloud; в `KNOWN_MODELS` — 128 000 |
| Цена вход / выход / чтение кэша | Mistral AI $0.165 / $0.66 / $0.0165; Regolo €0.50 / €2.10; AKI.IO €0.20 / €0.60. Нижняя граница `KNOWN_MODELS` для Regolo при 1.1225: $0.56125 / $2.35725 / $0.56125; неизвестная цена кэша считается полной ценой входа | OVHcloud €0.09 / €0.28; Scaleway €0.15 / €0.35; GreenPT €0.20 / €0.40. Нижняя граница `KNOWN_MODELS` для GreenPT при 1.1225: $0.2245 / $0.449 / $0.2245; неизвестная цена кэша считается полной ценой входа |
| Максимум одного вызова fast (52 000 вход + 8 000 выход) | Mistral AI $0.0139; Regolo €0.0428 = $0.048043 при 1.1225, $0.047953 при 1.1204 и $0.051360 при 1.20; AKI.IO €0.0152 = $0.0171 при 1.1225 | OVHcloud €0.0069 = $0.0078; Scaleway €0.0106 = $0.0119; GreenPT €0.0136 = $0.015266 при 1.1225, $0.015237 при 1.1204 и $0.016320 при 1.20 |
| Лимит прогона fast $0.50 | 4 вызова общие для конвенций и ревью в каждой попытке; за 3 попытки возможны 12 основных вызовов. 12 максимальных вызовов Regolo стоят €0.5136 ≈ $0.5765 при 1.1225; порог $0.50 — около 0.9735 USD/EUR, поэтому они не могут все пройти предварительную проверку лимита | Иллюстрация 9 основных Regolo + 3 fallback GreenPT: €0.426 ≈ $0.4782 при 1.1225; эта смесь пересекает $0.50 при курсе около 1.1737 USD/EUR. Это не гарантированный состав вызовов |

Курс 1.1225 USD/EUR в таблице — историческая оценка EUrouter для сравнения цен #46, не курс рантайма. Число 1.1204 за 05.10.2026 — также исторический пример [справочного курса ЕЦБ](https://www.lb.lt/en/daily-euro-foreign-exchange-reference-rates-published-by-the-european-central-bank), а не конфигурация staging.

Остальные кандидаты отклонены по прогонам-пробам на временной ветке `chore/46-live-run-probe` (не мержится): `qwen3-coder-30b-a3b` — валидный ответ без находок (пропускает обе high из `findings.sample.json`); `deepseek-v4-flash-0731` — нарушения PIPELINE_SPEC §9 (`start_line == line`, порядок находок), repair тоже невалиден; `gemma-4` — таймаут 90 с на первом вызове; `minimax-m3` — HTTP 400 на `json_schema`; `qwen3.6-35b` — лучшие находки, но около 3 000 reasoning-токенов и 77–92 с на вызов (таймауты), а оба способа отключить reasoning в теле запроса (`reasoning.enabled`, `chat_template_kwargs.enable_thinking`) игнорируются.

## Ручной workflow `LLM live run` (#46, #53)

До #53 ручной workflow `LLM live run` (`.github/workflows/llm-live-run.yml`, `workflow_dispatch`, секрет организации `AI_DMC268_T6`) проверял два review-сценария: основная модель с fallback и fallback как основная. В #53 добавлены два сценария `RepoConventionsDraft`; для каждого из четырёх шагов summary теперь показывает провайдера, фактическую модель, последовательность вызовов, курс ЕЦБ по каждому вызову, токены, стоимость в USD, время, результат валидатора и буквальный D7. D7 подтверждается только при успешной валидации первого ответа и `calls == [primary]`; repair, retry или fallback его не подтверждают. Живой результат для conventions получен на ветке PR #65 (задача #53) до мержа: [run 37586973849](https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/37586973849) (`feat/53-llm-eval-gateway-project` @ `44f797a` — голова ветки до rebase, 07.10.2026; шлюз, промпты, валидатор и workflow совпадают с `main` после мержа) дал в обоих шагах conventions `calls == [primary]` и `OK RepoConventionsDraft 3 items`: `mistral-small-4` на маршруте Mistral AI (`mistral/mistral-small-4`), `mistral-small-3.2-24b` на Scaleway (`scaleway/mistral-small-3.2-24b`), то есть буквальный D7 в 1 из 1 у каждой модели. Сам run красный из-за review-шага «fallback как основная»: `llm_invalid_output` после `[primary, repair]` (`done_well` — семантика, не отказ схемы); review-шаг основной модели дал `calls == [primary]` и `OK ReviewOutput 1 items`.

Пара подтверждена review-прогонами #46 и [run 37342619900](https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/37342619900) на `main` (оба review-ответа валидны при `calls == [primary]`); ни один из них не проверял conventions.

Отдельный живой conventions-прогон на обеих моделях — run 37586973849 выше; в шагах conventions semantic repair не понадобился. Тот же вызов `RepoConventionsDraft` через шлюз worker делает перед ревью, когда для репозитория, ревизии `AGENTS.md` и версии промпта нет кэша конвенций (`app/modules/reviews/application/process_run.py`, поиск и промах кэша — `app/modules/reviews/application/conventions.py:203-221`), но последовательность вызовов staging-ревью не видна из GitHub (только в трейсе run на staging), поэтому как свидетельство D7 здесь не учитывается. `review.conventions.v2.md` описывает маркеры `[N more … omitted]`; локальные тесты рендера это подтверждают, но живую strict-проверку не заменяют.
