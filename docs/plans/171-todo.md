# Список задач: DiffMap в публикации и GitLab §9.5 (#171)

См. [171-plan.md](171-plan.md) (решения D-1–D-5). Baseline `origin/main` `1d05fbe`; ветка
`fix/171-publication-diffmap`. Каждая задача — один TDD-срез, если не указано иное.

**Общая проверка** (после задач с кодом):

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run pytest
```

Для интеграции с БД — `TEST_DATABASE_URL`.

**Перед стартом:** создать ветку от `main`, не переносить WIP #170.

---

## Task 1: RED — GitLab context position (Ч.1)

**Описание:** Тест в `tests/test_gitlab_review_publication.py`: находка на context-строке (old+new) — POST
`discussions` с `old_line` и `new_line`, без лишнего `line_range`.

**Критерии:**

- [x] Тест падает на текущем `_position` (только `new_line`).
- [x] MockTransport фиксирует JSON position.

**Проверка:** `uv run pytest tests/test_gitlab_review_publication.py -k context -q` → RED.

**Файлы:** `tests/test_gitlab_review_publication.py`.

**Зависимости:** нет.

---

## Task 2: GREEN — context + rename paths (Ч.1)

**Описание:** Реализовать сборку position для context и rename (разные `old_path`/`new_path` из координат,
не из одного `finding.path`).

**Критерии:**

- [x] Task 1 GREEN.
- [x] Отдельный тест rename с `old_path != new_path`.

**Проверка:** `tests/test_gitlab_review_publication.py`.

**Файлы:** `gitlab_review_publication.py`, модуль координат (D-1).

**Зависимости:** Task 1.

---

## Task 3: RED — line_range для диапазона [S,E] (Ч.1)

**Описание:** Тест: `PublishedFinding` с `start_line=S`, `line=E` → position с `line_range.start/end`,
`line_code`, `new_line=E`.

**Критерии:**

- [x] RED на main.
- [x] line_code соответствует §9.5 для фикстурного hunk.

**Проверка:** `tests/test_gitlab_review_publication.py`.

**Зависимости:** Task 2 (или параллельно после D-1 каркаса).

---

## Task 4: GREEN — line_range + курсоры DiffRow (Ч.1 / D-1)

**Описание:** Проброс курсоров из DiffMap; при отсутствии — минимальное расширение `DiffRow`/`build_diff_map`
+ тесты в `test_diff_map.py`.

**Критерии:**

- [x] Task 3 GREEN.
- [x] `uv run pytest tests/test_diff_map.py -q` зелёный.

**Файлы:** `diff_map.py`, координаты, `gitlab_review_publication.py`.

**Зависимости:** Task 3.

---

## Task 5: RED/GREEN — suggestion `-N+0` (Ч.1)

**Описание:** Для диапазона new [11,12] тело содержит `suggestion:-1+0`; для >100 строк — без applicable
suggestion (только prose).

**Критерии:**

- [x] Тест на `-1+0`.
- [x] Тест на длинный диапазон без suggestion-блока.

**Файлы:** `review_output.py` (`comment_body` или GitLab-вариант), `gitlab_review_publication.py`, тесты.

**Зависимости:** Task 4.

---

## Task 6: Тест сериализатора `deleted` (Ч.1, fixture only)

**Описание:** Unit-тест: position с только `old_line` для removed row — **без** assert на живой POST v2 LEFT;
комментарий в тесте ссылается на issue #171 / §9.5 оговорку.

**Критерии:**

- [x] Сериализация JSON совпадает с примером CONTEXT §9.5.
- [ ] В PR-body зафиксировано: не публикация LEFT из v2. *(placeholder в Task 20 What)*

**Файлы:** тест координат / `test_gitlab_review_publication.py`.

**Зависимости:** D-1.

---

## Task 7: RED — added position (реgression, Ч.1)

**Описание:** Убедиться, что существующий `test_posts_new_side_discussions_then_one_summary` и added-only
position остаются зелёными после рефакторинга `_position`.

**Критерии:**

- [x] Не регрессировать happy path added.

**Зависимости:** Tasks 2–5.

---

## Task 8: Документ §9.5 / PR note про deleted (Ч.1)

**Описание:** При необходимости уточнить в PR «What» ограничение deleted; не менять §9.5 без согласования
ndovnar, если меняется смысл.

**Критерии:**

- [ ] PR текст отражает fixture-only deleted.

**Зависимости:** Task 6.

---

## Task 9: RED — unanchored line → body (GitHub + GitLab) (Ч.2)

**Описание:** В `tests/test_review_publisher.py` (или post-processor): строка вне DiffMap → `body_only` /
не уходит inline у обоих маршрутов.

**Критерии:**

- [x] RED до замены `hunk_lines`.
- [x] GREEN: `test_unanchored_line_routes_to_body_for_github_and_gitlab`.

**Файлы:** тесты.

**Зависимости:** нет (можно параллельно с Ч.1 после ветки).

---

## Task 10: GREEN — bucket через `resolve_anchor` (Ч.2)

**Описание:** Заменить `hunk_lines`/`_new_hunk_lines` для publish path на DiffMap + `resolve_anchor` /
единый helper (D-2).

**Критерии:**

- [x] Task 9 GREEN.
- [x] `parse_unified_diff` не используется для проверки строк findings при публикации (grep / тест).

**Файлы:** `findings_post_processor.py`, `run_repository.py`, worker/store при необходимости.

**Зависимости:** Task 9.

---

## Task 11: DiffMap на publish path (Ч.2)

**Описание:** Прокинуть snapshot DiffMap из run context в post-processor / publication assembly.

**Критерии:**

- [x] Integration/unit: publish submission использует те же адреса, что DiffMap.

**Файлы:** `run_repository.py`, `store_review_output.py`, `worker.py`.

**Зависимости:** Task 10.

---

## Task 12: CONTEXT §12 / отступление (Ч.2)

**Описание:** Обновить строку «VCS publisher» в §12 на DiffMap/v1 + resolve_anchor **или** записать принятое
отступление (если останется второй parse — только с явным текстом).

**Критерии:**

- [x] Документ согласован с кодом Task 10–11.

**Файлы:** `docs/CONTEXT_AND_VERIFICATION_SPEC.md`.

**Зависимости:** Task 11.

---

## Task 13: RED — partial fallback не снимает inline с опубликованных (Ч.3)

**Описание:** Тест: два inline, первый POST ok, второй 400 coordinates → complete_publication: первый
`inline_comment=True`, второй в теле.

**Критерии:**

- [x] RED на `moved_to_body` глобальном сбросе.

**Файлы:** `tests/test_publish_run_review.py`, возможно `test_sqlalchemy_run_repository.py`.

**Зависимости:** Tasks 1–2 желательно (GitLab path), но логика в application layer.

---

## Task 14: GREEN — per-finding placement (Ч.3)

**Описание:** D-3: `complete_publication` и fallback в `publish_run_review` помечают только unposted.

**Критерии:**

- [x] Task 13 GREEN.
- [x] Check-run inline/body counts совпадают (`run_lifecycle_store` / `check_runs`).

**Файлы:** `publish_run_review.py`, `run_lifecycle_store.py`.

**Зависимости:** Task 13.

---

## Task 15: RED/GREEN — повторная доставка после fallback (Ч.3)

**Описание:** Второй `execute` publish не ставит `moved_to_body=False` поверх уже inline findings.

**Критерии:**

- [x] Тест redelivery.

**Файлы:** `tests/test_publish_run_review.py`.

**Зависимости:** Task 14.

---

## Task 16: Счётчики check-run (Ч.3)

**Описание:** Убедиться, что `inline_count` / body count в check-run и UI payload отражают per-finding flags.

**Критерии:**

- [x] Тест или расширение существующего на counts.

**Зависимости:** Task 14.

---

## Task 17: RED — stale head между двумя POST (Ч.4)

**Описание:** Mock: после первого POST versions меняются `[HEAD]` → `[NEW_HEAD, HEAD]` → `stale_commit`, третий
POST не выполняется.

**Критерии:**

- [x] RED (сейчас цикл продолжается).

**Файлы:** `tests/test_gitlab_review_publication.py`.

**Зависимости:** нет.

---

## Task 18: GREEN — checkpoint перед каждым POST (Ч.4)

**Описание:** `_require_latest_head` (или эквивалент) перед каждым `_create` в цикле findings.

**Критерии:**

- [x] Task 17 GREEN.

**Файлы:** `gitlab_review_publication.py`.

**Зависимости:** Task 17.

---

## Task 19: Конечные исходы 404/409/422/429 и pagination (Ч.4)

**Описание:** `_classify` + `publish_run_review`: не бесконечный retry; PIPELINE_SPEC §5.2 строки для GitLab.

**Критерии:**

- [x] Тесты на classify для каждого статуса.
- [x] >20 страниц discussions → failed, не retryable loop.

**Файлы:** `gitlab_review_publication.py`, `publish_run_review.py`, `docs/PIPELINE_SPEC.md`.

**Зависимости:** Task 18.

---

## Task 20: Финальные гейты и PR

**Описание:** Полный pytest с `TEST_DATABASE_URL`, format/lint/mypy; PR body What/Why/How/Refs #171.

**Критерии:**

- [x] Задачи 13–19 отмечены выполненными (Tasks 1–12, 17–19 — см. выше; Task 6/8 — PR-note placeholders).
- [x] `uv run ruff check .` — green.
- [x] `uv run ruff format --check .` — green (после `ruff format`).
- [x] `uv run mypy .` — green (515 files).
- [x] `uv run pytest` — **4040 passed**, **854 skipped** (`TEST_DATABASE_URL` не задан — postgres/integration skips ожидаемы).
- [ ] Полный pytest с `TEST_DATABASE_URL` (postgres/integration без skip).
- [ ] PR body What/Why/How/Refs #171.
- [ ] Approving review по процессу курса.

**Зависимости:** все.

---

## Порядок выполнения (рекомендуемый)

1. Tasks 1–7 (GitLab position) — можно начать без DiffMap bucket, но line_code потребует Task 4 / DiffRow.
2. Tasks 9–12 параллельно или сразу после Task 4 (общий модуль координат).
3. Tasks 13–16 после стабилизации publish assembly.
4. Tasks 17–19 GitLab lifecycle.
5. Task 20.
