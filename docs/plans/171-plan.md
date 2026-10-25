# План: DiffMap в публикации и контракт GitLab §9.5 (#171)

## Статус и базовая линия

Issue: https://github.com/larchanka-training/dmc-268-api-t6/issues/171  
Рекомендуемая ветка: `fix/171-publication-diffmap` (от `origin/main`; в issue также указано `fix/-publication-diffmap`)  
Чеклист задач: [171-todo.md](171-todo.md).

**Baseline pytest** (до правок продукта, kickoff issue):

```sh
uv run pytest tests/test_gitlab_review_publication.py tests/test_publish_run_review.py tests/test_diff_map.py -q
```

Результат на текущем дереве: **PASS** — 80 тестов.

**Конфликт с параллельной работой:** на машине может быть активна ветка `fix/170-overview-budget-and-docs` с WIP в
`findings_post_processor.py`, `run_repository.py`, `run_lifecycle_store.py`. Ветку #171 создавать от `main`, не
смешивать с #170; при мерже — разрешить конфликт в post-processor осознанно (общий файл с #170).

## Обзор

Четыре среза по AC issue (латентные дефекты до приёма GitLab #169):

| Срез | Приоритет | Суть |
| --- | --- | --- |
| Ч.1 | P2 | `position` GitLab по CONTEXT §9.5: context, rename, диапазон, suggestion `-N+0`; `deleted` — только тест сериализатора (комментарий issue / #205: не публикация LEFT из v2) |
| Ч.2 | — | GitHub и GitLab берут адресуемость из DiffMap + `resolve_anchor`, а не из `parse_unified_diff` в `run_repository._new_hunk_lines` |
| Ч.3 | P3 | Размещение (`inline_comment`) по каждой находке после частичного GitLab fallback; повторная доставка не сбрасывает флаги |
| Ч.4 | P3 | Checkpoint головы MR перед каждым POST (§10.2); конечные исходы для 404/409/422/429 и лимита страниц discussions |

Вне scope: #169 (живой GitLab MR), #170 (бюджет overview), приём LEFT/v3 из v2.

## Текущее поведение (на `main`)

### Ч.1 — `_position` GitLab

```120:129:app/modules/reviews/infrastructure/gitlab_review_publication.py
def _position(version: dict[str, str], finding: PublishedFinding) -> dict[str, Any]:
    return {
        ...
        "old_path": finding.path,
        "new_path": finding.path,
        "new_line": finding.line,
    }
```

- Нет `old_line` для context; rename не различает `old_path` / `new_path`.
- Диапазон `[S,E]` не даёт `line_range` с `line_code`; якорь только на `E`.
- `comment_body` всегда оборачивает suggestion в обычный ` ```suggestion` блок без `-N+0` (§9.5).

### Ч.2 — третий разбор diff для публикации

Цепочка: `findings_post_processor` проверяет `hunk_lines[path]` → `run_repository._new_hunk_lines` →
`prompt_builder.parse_unified_diff`. `resolve_anchor` используется только в тестах (`test_diff_anchor.py`).

### Ч.3 — глобальный `moved_to_body`

`publish_run_review.py:269-277` при coordinates-fallback переносит **все** оставшиеся inline в тело одним флагом
`moved=True`. `complete_publication` (`run_lifecycle_store.py:431-433`) при `moved_to_body` снимает
`inline_comment` со **всех** inline-находок, включая уже опубликованные на MR.

### Ч.4 — голова MR и классификация ошибок

- `_require_latest_head` вызывается до цикла (`:183`) и после 400 (`_require_current_head`), но **не** перед каждым POST.
- `_classify`: всё кроме 400 coordinates / 401 / 403 → `retryable`; после 3 пауз — `gitlab_publish_failed` без
  детализации в PIPELINE_SPEC §5.2.
- `_discussions`: после 20 страниц — `retryable` «list is short» (бесконечные ретраи).

GitHub-эталон: `github_review_publication.py` вызывает `checkpoint()` перед каждым HTTP-шагом и ловит stale head.

## Архитектурные решения

### D-1 — Модель координат для публикаторов (Ч.1–Ч.2)

Ввести узкий application-слой (например `publication_coordinates.py` или расширение `diff_anchor`):

- Вход: `DiffMap`, `PublishedFinding` (path, start_line, line).
- Выход: либо **reject → body** (единый reason для GitHub/GitLab), либо **GitLabPosition** / **GitHub line** payload.

`line_code` по §9.5: `SHA1(UTF8(provider_file_path)) + "_" + old_cursor + "_" + new_cursor` с курсорами DiffRow
**до инкремента** (§9.2). Если в `DiffRow` нет полей курсора — минимальное расширение `DiffMap`/`build_diff_map`
согласовать с владельцем #130 (#168); до этого — RED-тесты с фикстурой row, где курсоры заданы явно.

`deleted` в AC: unit-тест сериализации `old_line` без POST на LEFT; в PR явно указать ограничение v2/RIGHT.

### D-2 — Замена `hunk_lines` на DiffMap (Ч.2)

- При сборке контекста публикации (worker / `run_repository` / store) передавать `DiffMap` snapshot run (уже есть
  на этапе context) в post-processor **или** вычислять bucket inline/body через `resolve_anchor` в одном месте.
- `_new_hunk_lines` + `parse_unified_diff` для **публикации** убрать; допустимо оставить parse для других путей
  (prompt, diff viewer), если не участвуют в bucket findings для publish.
- Тест AC: строка, которую DiffMap не адресует → body_only у обоих провайдеров (`test_review_publisher.py`).

### D-3 — Per-finding placement после partial fallback (Ч.3)

- `PublishError.coordinates` с `unposted` уже несёт хвост; дополнительно возвращать **posted** ids или помечать
  findings при успешном POST до 400.
- `complete_publication`: принимать `moved_finding_ids` / per-row `inline_comment` вместо булева `moved_to_body`,
  либо `moved_to_body` только для findings из `unposted`.
- Повторная доставка: не перезаписывать placement, если finding уже в БД с корректным флагом (тест redelivery).

### D-4 — GitLab lifecycle (Ч.4)

- Перед **каждым** `_create` / POST discussion: GET versions + `_require_latest_head` (как GitHub checkpoint).
- Расширить `_classify` / маппинг в `publish_run_review`: 404/409/422/429 → конечные коды; превышение страниц
  discussions → не-retryable `gitlab_publish_failed` с описанием.
- Документировать строки в `docs/PIPELINE_SPEC.md` §5.2 (и при необходимости SD §8.4).

### D-5 — Многострочный suggestion GitLab (Ч.1)

По §9.5: для диапазона `[S,E]` на new side — `suggestion:-(E-S)+0` в теле; если диапазон > 100 строк — комментарий
без применяемой замены (консервативный лимит адаптера). Зафиксировать в тесте `test_gitlab_review_publication.py`.

## Нарезка PR (опционально)

1. **PR-A (Ч.2 + часть Ч.3):** DiffMap/`resolve_anchor` в bucketing + per-finding placement — меньше риска для GitLab payload.
2. **PR-B (Ч.1 + Ч.4):** GitLab position, suggestion, stale между POST, исходы API.
3. Или один PR с Refs #171, если объём укладывается в review.

## Критерии приёмки (cross-ref)

| AC | План |
| --- | --- |
| Ч.1 | Задачи 1–8 |
| Ч.2 | Задачи 9–12 |
| Ч.3 | Задачи 13–16 |
| Ч.4 | Задачи 17–20 |

DoD: полные гейты backend (`AGENTS.md`), документация §5.2 / §12 при изменении контракта публикации.

## Риски

- **Курсоры line_code:** без полей в DiffRow реализация §9.5 неполная — эскалация к #130/#168 или локальное
  расширение с тестами на известных hunks.
- **Маркер inline зависит от текста comment** (`_finding_id` от `comment_body`): смена suggestion меняет identity —
  не ломать в #171 без отдельного решения.
- **HTTP вне транзакции:** сохранить; placement пишется только после успешного publish batch.

## Ссылки

- CONTEXT_AND_VERIFICATION_SPEC §9.2, §9.5, §10.2, §12
- PIPELINE_SPEC §5.2 (добавить GitLab исходы)
- #97, #130/#134, #169, #170, комментарий issue про `deleted` / #205
