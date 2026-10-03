<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/create-issue/tests/golden/issue-46.md -->

## Background

PR #40 (#33) смёржен; OQ-2 в SD §15 закрыт по каталогу EUrouter, а strict structured output должен подтвердить ручной workflow `LLM live run`. Первый прогон после мержа (run 37153425606, артефакт `llm-live-run`) не подтвердил D7 по двум внешним причинам: `mistral-small-4` — HTTP 402 «Insufficient balance» (на курсовом аккаунте EUrouter за ключом `AI_DMC268_T6` нет кредитов; маршрутизация прошла, запрос со strict-схемой роутер принял); `gpt-4.1-mini` — HTTP 400 «No providers available for model with given preferences» (у модели единственный провайдер на EUrouter — Microsoft Foundry; причина не разведена: endpoint отфильтрован для аккаунта либо провайдер не принимает `response_format: json_schema, strict: true`). Шлюз отработал как в тестах (fallback, нормализация ошибок, трейс). Разбор — комментарий в #33 от 03.10.

## Scope

- Кредиты: вопрос пополнения курсового аккаунта EUrouter поднимает техлид у куратора; после пополнения — повторный `gh workflow run llm-live-run.yml` на `main` с дефолтными моделями. Если оба прогона зелёные — записать ссылку на run в SD §15, задача закрывается без правок кода.
- Если 400 на `gpt-4.1-mini` остаётся: развести причину (прогон кандидатов через input `model` того же workflow; при необходимости — один прогон primary с `LLM_STRUCTURED_OUTPUT=prompt_json` локально у того, у кого есть ключ, чтобы отделить «провайдер недоступен» от «strict отвергнут»).
- Переназначение primary (и fallback, если потребуется) по D7: strict structured output подтверждён прогоном (`calls == [primary]`, `validate_findings.py` OK), контекст ≥ 60k, прогон fast ≤ $0.50 в худшем случае (3 попытки × 4 вызова по ценам самого дорогого endpoint). Предпочтение — модели с ≥ 2 провайдерами на EUrouter и разными вендорами для primary/fallback. Кандидаты из каталога на 03.10 (контекст ≥ 60k, `response_format`, ≤ $0.015 за максимальный вызов): `qwen3-coder-30b-a3b` (4 провайдера), `gpt-oss-120b` (10), `qwen3-235b-a22b-instruct` (6), `deepseek-v4-flash-0731` (6), `gemma-4` (4).
- Запись результата одним PR: таблица D7 и строка OQ-2 в `docs/SYSTEM_DESIGN.md` §15 (со ссылкой на зелёный run), дефолты `KNOWN_MODELS` в `app/modules/reviews/infrastructure/llm/settings.py`, `.env.example`, первая фраза `docs/PIPELINE_SPEC.md` §5.1, дефолты input'ов `model`/`fallback_model` в `.github/workflows/llm-live-run.yml`; результат по поддержке ключевых слов схемы — в PIPELINE §9 (там это закреплено за #33).
- Вне scope: классификация HTTP 402 как не-ретраибельного класса (сейчас `llm_unavailable`, три попытки прогона на биллинговой ошибке) — отдельная задача; strict-схема конвенций на EUrouter (известный пробел в SD §15) — отдельная задача; проброс ключа в staging — #35.

## Acceptance Criteria

- [ ] Workflow `LLM live run` на `main` зелёный для обоих прогонов: в `calls` только `primary`, `validate_findings.py` — `OK ReviewOutput N items`; ссылка на run записана в SD §15
- [ ] Если пара моделей изменилась: SD §15 (таблица D7: strict, контекст, цены трёх требований с датой каталога), `KNOWN_MODELS`, `.env.example`, PIPELINE §5.1 и дефолты workflow обновлены в одном PR; `uv run pytest` зелёный, тесты конфигурации (`test_eurouter_is_selected_by_configuration_only` и соседние) проходят с новыми дефолтами
- [ ] Худший случай стоимости fast для новой пары пересчитан по ценам самого дорогого endpoint каждой модели и ≤ $0.50; валюта endpoint'ов (USD/EUR) учтена
- [ ] Причина 400 на `gpt-4.1-mini` записана в SD §15 одной фразой (провайдер недоступен / strict отвергнут / пропало после пополнения) — чтобы выбор не пересматривали вслепую
- [ ] В #33 оставлен комментарий со ссылкой на зелёный run и итоговой парой моделей

## Dependencies

**Блокирует:**

- Кредиты на курсовом аккаунте EUrouter за `AI_DMC268_T6` — пополнить может только курс; эскалация у куратора за техлидом (axyi). Без кредитов любой прогон даёт HTTP 402

**Связано, не блокирует:**

- #33 — задача-источник: OQ-2 и workflow `LLM live run` сделаны в PR #40, итог прогона — в комментарии там же
- #30 — live-режим eval берёт модель из `KNOWN_MODELS`/`LLM_MODEL`; смена primary меняет baseline
- #35 — проброс `LLM_*` в staging; значения дефолтов должны совпасть с итогом этой задачи
- Follow-up: HTTP 402 → не-ретраибельный класс `error_code` (§6); strict-схема конвенций на EUrouter

## DoD Checklist

- [ ] Tests pass
- [ ] Linters clean
- [ ] Docs updated
- [ ] PR reviewed and approved
- [ ] Issue status updated

## References

- Run первого прогона: https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/37153425606
- Разбор прогона: https://github.com/larchanka-training/dmc-268-api-t6/issues/33#issuecomment-5973444214
- Каталог EUrouter: `GET https://api.eurouter.ai/api/v1/models`, детали endpoint'ов: `/api/v1/models/<vendor>/<model>/endpoints` (без ключа)
- Документация EUrouter: routing (`/docs/concepts/routing`), ошибки (`/docs/guides/error-handling`: 400 — «fix the request, do not retry unchanged», 402 — «add credits»)
- `docs/SYSTEM_DESIGN.md` §15 (OQ-2, таблица D7), `docs/PIPELINE_SPEC.md` §5.1, §9, §14
- `.github/workflows/llm-live-run.yml`, `app/bootstrap/llm_gateway.py` (CLI и форма JSON), `app/modules/reviews/infrastructure/llm/settings.py` (`KNOWN_MODELS`)

## Kickoff — с чего начать

**Репозиторий:** larchanka-training/dmc-268-api-t6

**Ветка:** fix/<n>-oq2-live-run — `main` защищён, через PR; если итог «кредиты пополнены, оба прогона зелёные» — PR только с правкой SD §15

**Форма проблемы:** Workflow и шлюз работают; не хватает внешнего подтверждения D7. Два независимых блокера: нулевой баланс аккаунта (402 на любой модели) и маршрут `gpt-4.1-mini` с единственным провайдером, который отвергает запрос до биллинга (400). Пока не понятно, отвергает он сам запрос со strict-схемой или просто недоступен аккаунту — это и нужно развести, а затем либо подтвердить текущую пару, либо выбрать другую по тем же трём требованиям D7.

**Что прочитать первым:**

- docs/SYSTEM_DESIGN.md §15 — что именно обещано по OQ-2 и какие три требования D7 проверяются
- .github/workflows/llm-live-run.yml — что считается зелёным прогоном (`calls == ["primary"]`, валидатор) и как подставить другую модель через input
- app/bootstrap/llm_gateway.py — форма JSON в артефакте (`calls[].error.{class,http_status,message}`), по ней читается причина отказа
- app/modules/reviews/infrastructure/llm/settings.py — `KNOWN_MODELS` (окно, цены, structured_output) и правила наследования fallback-профиля
- комментарий в #33 от 03.10 — артефакт первого прогона и список кандидатов

**Перед тем как писать код:**

- `gh workflow run llm-live-run.yml --repo larchanka-training/dmc-268-api-t6 -f model=<кандидат>` и `gh run download <id>` — артефакт покажет `http_status` и текст ошибки по каждому вызову; это дешевле любого кода
- `curl -s https://api.eurouter.ai/api/v1/models/<vendor>/<model>/endpoints | jq '.data.endpoints[] | {provider_name, supported_parameters, pricing}'` — число провайдеров, валюта и поддержка `response_format` до выбора
- `uv run pytest tests/test_llm_gateway.py -q` — после смены дефолтов `KNOWN_MODELS`

**Мины:**

- `workflow_dispatch` запускается только для файла на `main` — кандидатов проверяют на `main` через input `model`, а не с ветки
- У части endpoint'ов цены в EUR (Regolo, AKI.IO) при `currency` в каталоге; лимит $0.50 считать по самому дорогому endpoint модели, а не по первому
- Запрос шлюза всегда со strict `json_schema` (D7); `LLM_STRUCTURED_OUTPUT=prompt_json` разрешён только локально с `LLM_ALLOW_PROMPT_JSON=1` — для диагностики, не для staging
- 402 сейчас классифицируется как `llm_unavailable` и run-retryable: при нулевом балансе воркер сделает три попытки — не принимать это за «нестабильность провайдера»
- Каталог `supported_parameters` объявляет только `response_format`, отдельной возможности `structured_outputs` у EUrouter нет — наличие параметра не доказывает strict, доказывает только прогон
- Смена primary — это новая пара в SD §15, `KNOWN_MODELS` и `.env.example` одновременно; `LLM_FALLBACK_*` наследует ключи только на том же endpoint
