# CONTEXT_AND_VERIFICATION_SPEC — контекст, верификация и VCS Write-Back

Версия базовой спецификации: **1.2, утверждена**. Дата: **2026-10-14**.
Задача: [api#107](https://github.com/larchanka-training/dmc-268-api-t6/issues/107);
версия 1.2 — [api#140](https://github.com/larchanka-training/dmc-268-api-t6/issues/140).

Основание — предоставленный техлидом документ `107-rules-and-ignore-decision.md`
от 2026-10-09, разделы 1–12. Формат документов, приоритет требований, ignore и снимок политики
определены в [RULES_FORMAT_SPEC](RULES_FORMAT_SPEC.md). Эти документы составляют
одну спецификацию изменения.

**Базовые версии: утверждены.** Спецификации для
[#107](https://github.com/larchanka-training/dmc-268-api-t6/issues/107):
v1.0 — [PR #108](https://github.com/larchanka-training/dmc-268-api-t6/pull/108),
v1.1 (`unsupported_path`) — [PR #135](https://github.com/larchanka-training/dmc-268-api-t6/pull/135),
v1.2 (RAG v1, #140) — [PR #157](https://github.com/larchanka-training/dmc-268-api-t6/pull/157).
Утверждение спецификации не означает выполнения реализации, миграций или проверок.

**Редакция 2026-10-18 — [#175](https://github.com/larchanka-training/dmc-268-api-t6/issues/175),
[PR #205](https://github.com/larchanka-training/dmc-268-api-t6/pull/205).**
Базовые версии утверждены указанными PR; новые уточнения этой редакции утверждены
в PR #205 и не приписываются утверждению #108/#135/#157.
Версия 1.2 вносит RAG v1 по
[ADR 0001](adr/0001-graph-rag-v1.md) в §1, §2.1, §3.1–3.2, §3.4–3.5, §4.1, §5, §8.1, §12;
согласование остаточных расхождений — #175.

Слова **ДОЛЖЕН**, **НЕЛЬЗЯ** и **МОЖЕТ** задают требования к будущей реализации.
Числа и контракты ниже — согласованные проектные значения, а не
результаты замеров. Существующие документы описывают также переходную реализацию;
точные изменения их положений перечислены в §12.

## 1. Область и инварианты

Спецификация определяет детерминированный контекст L0–L4, происхождение данных,
маппинг координат, обязательную проверку кандидатов и воспроизводимую публикацию.
Первая реализация использует GitHub и якоря `RIGHT`; контракт `LEFT` и полный GitLab
ingress/diff проектируется сразу, внедряется отдельными задачами. Публикация MR discussions
на GitLab уже есть на `main` (#97, [SD §8.4](SYSTEM_DESIGN.md#84-координаты-gitlab-и-надёжная-публикация-107), адаптер
[`gitlab_review_publication.py`](../app/modules/reviews/infrastructure/gitlab_review_publication.py),
PIPELINE_SPEC §5.2, таблица «Сбои GitLab»). Embedding-модель, векторный
поиск, исполнение проекта и автоматическое применение suggestions в эту работу не
входят. RAG v1 по [ADR 0001](adr/0001-graph-rag-v1.md) — граф символов с
указателями без embeddings и без текста кода, построенный по разрешённому дереву
default branch репозитория с включённым тумблером RAG, — только выбирает
кандидатов; их текст и рёбра подтверждаются на ревизиях run по §3.5. Векторный
поиск (v2) — только после гейта eval #146.

1. Политика, документы, дефолты, профили моделей, версии промптов, порог severity и inline cap
   `max_comments` неизменяемы в run. `severity_threshold` и `max_comments` читаются из Repository при создании run,
   сохраняются в привязках и policy snapshot (RULES_FORMAT_SPEC §7), retry их
   переиспользует; изменение настройки действует на новые runs/rerun. Закрепление
   уже реализовано и для legacy-пути (#212): поля хранятся в Run и snapshot,
   без изменения policy_digest, schema_version=1 и conventions key. Историческая
   NULL-пара без backfill сохраняет live fallback к Repository при каждой обработке
   и retry, даже если snapshot уже существует (PIPELINE_SPEC §9). Положение порога
   после verifier и FinalReviewResult остаются целевым профилем #107/#130.
2. Ignore применяется ко всем источникам всех LLM-вызовов, включая конвенции,
   AST, verifier, repair и fallback. Кэш не даёт разрешения на отправку источника.
3. Карта diff доказывает допустимость якоря; manifest — видимость фрагмента;
   verifier оценивает смысл. Эти проверки не заменяют друг друга.
4. Кандидат публикуется только после `supported`; verifier не исправляет его текст,
   координаты, severity или suggestion и не создаёт новых кандидатов.
5. Итоговые findings, summary и payload сохраняются до `publishing`. Повтор
   публикации не вызывает модель и не пересобирает смысл результата.
6. Неполнота проверки не означает отсутствия дефектов. Статус `clean` возможен
   только при завершённом анализе разрешённого scope по правилам §8.
7. Сетевой вызов VCS/LLM не находится внутри DB-транзакции. Запись usage после
   ответа выполняется отдельной короткой транзакцией по существующему исключению
   для usage ledger; сохранение результата выполняет use case, не repository.

## 2. Снимок и последовательность run

### 2.1. Раздельные ревизии

| Поле | Значение |
| --- | --- |
| `policy_snapshot_id` | `snapshot_id` записи `run_policy_snapshots` из RULES_FORMAT_SPEC |
| `policy_base_sha` | Закреплённый `Run.base_sha`, откуда читаются документы и общие конвенции |
| `head_sha` | Новая сторона проверяемого изменения |
| `diff_old_sha` | Подтверждённая провайдером старая сторона именно сохранённого patch; может отличаться от `policy_base_sha` |
| `diff_snapshot_id` | Идентификатор неизменяемого агрегата карты diff и метаданных версии VCS |
| `provider_diff_ref` | Нормализованные и исходные метаданные версии diff для адаптера (§9) |
| `index_commit_sha` | `indexed_commit_sha` публикации индекса RAG, по которой выбраны кандидаты (§3.5). Только выбирает кандидатов и никогда не является ревизией блока; в policy snapshot run не закрепляется; блоки RAG входят в checkpoint кандидатов/manifest принятого reviewer response (§7.4), и только retry до этого checkpoint может использовать более новую публикацию |

Снимки файлов сохраняют существующий ключ `(run_id, filename)`. Старый общий
ключ `(code_change, head_sha)` не является ключом снимка run. Разные runs с одним
head могут иметь разные политику, версии промптов и результат. Повтор попытки
использует сохранённые снимки. Rerun создаёт новый run и собственный снимок.

### 2.2. Порядок операций

```text
claim run
  → использовать версии, закреплённые при enqueue, загрузить документы policy_base_sha
  → получить согласованный снимок VCS и перечень изменений
  → изолировать unsupported_path/identity_unresolved
  → review-ignore/generated → доказанное binary/empty_file → остальные пропуски (§9.2)
  → сохранить VCS-only DiffMap и отдельно immutable классификацию файлов run (§9.2), включая будущий skipped
  → проверить оба триггера §2.3: неполный inventory/неизвестные relevant counters ИЛИ >3 000 counted allowed строк → backend summary-only/partial → сохранить результат/план → publishing
  → полный достоверный пустой inventory: skipped/no_changes; только E без T/incomplete: skipped/all_changes_ignored; LLM calls = 0
  → полный достоверный inventory, хотя бы один T, остальные только E/T, incomplete нет: backend none → сохранить результат/план → publishing
  → нет анализируемого текста, есть incomplete (E/T могут быть), inventory/counters полны, counted allowed ≤3 000: backend partial, summaryOnly=false → сохранить результат/план → publishing
  → иначе анализ доступного текста; T/incomplete означают partial
  → получить общие конвенции только из base либо зафиксировать отсутствие источников
  → собрать L1 → сигнатуры L4 → context.rag → L2 → L3 в бюджете с резервом L0
  → pre-send policy guard → manifest → review LLM
  → формат → координаты → доказательства → детерминированные фильтры
  → если кандидаты остались: manifest → обязательный verifier
  → применить закреплённый порог severity → сортировка → inline max_comments
  → сохранить FinalReviewResult и публикационный план
  → publishing → сверить head/diff → восстановить или выполнить VCS write-back
```

Получение документов, файлов и модельные вызовы выполняются между короткими
транзакциями. Запись снимка использует compare-and-set по run/lease: второй worker
не заменяет уже закреплённые значения. Если worker погиб до полного снимка, retry
дочитывает те же закреплённые SHA/версии; частичный снимок не разрешает LLM-вызов.
Недоступный закреплённый объект не заменяется текущей веткой.

**Каноническая классификация и исходы.** Приоритет по каждой записи inventory:
валидация пути/идентичности (включая обе стороны rename/copy) → маски и built-in
generated → доказанное binary/empty_file → остальные пропуски (§9.2).
Маска/generated побеждает binary/too_large только при достоверной идентичности;
совпадение безопасного alias не отменяет unsupported_path/identity_unresolved.
Отсутствие patch само по себе не доказывает ни binary, ни too_large.

Доказанные исключения E — маски, generated, binary и доказанный `empty_file` (§9.2). Технические пропуски T —
`unsupported_path`, `identity_unresolved`, `too_large`, `unsupported_change`
(submodule/gitlink и доказанный чистый rename/mode-only без поддерживаемого анализа).
Missing/truncated/malformed patch обычного файла — неполнота, не E и не T,
для неё нет доказательства, позволяющего выбрать none. Критерии и хранение — §9.2.

Успешные маршруты выбираются сверху вниз; ошибки policy/целостности по-прежнему
дают failed. Сначала проверяются **оба** триггера §2.3.
Relevant counters — additions/deletions вне E/T, включая incomplete; их сумма —
counted allowed, даже если patch отсутствует. Raw counters E/T остаются диагностикой
и сами по себе не вызывают summary-only. При полном inventory только E/T
считаемый scope пуст и равен 0, даже если raw counters T неизвестны.

Анализируемый текст — разрешённые hunks из сохранённого DiffMap вне E/T,
включая сохранившиеся hunks truncated, которые поступают в L1. Truncated-only
с такими hunks идёт в обычное ревью с partial и может дать blocking; это
не отменяет ограничений координат incomplete (§9.3). Missing/malformed/truncated
без hunks не дают анализируемого текста и при выполнении условий таблицы
идут в backend partial. При malformed parser error текущий DiffMap не сохраняет
hunks; raw malformed patch не становится L1. Оба триггера §2.3 проверяются раньше.

| Состав согласованного inventory | Исход целевого профиля #107 |
| --- | --- |
| Inventory неполный/недостоверный либо неизвестны relevant counters допущенных изменений | Backend summary-only/partial, summaryOnly=true, 0 LLM, причина diff_inventory_incomplete; none/skipped не выводятся из неизвестности |
| Полный inventory, известные counted allowed >3 000 (в том числе missing-patch-only 4 000 строк) | Backend summary-only/partial, summaryOnly=true, 0 LLM, причина diff_too_large, даже если нет анализируемого текста |
| Достоверно пуст, предыдущие триггеры не сработали | skipped/no_changes, verdict=null, 0 LLM |
| Только E, T/incomplete нет, inventory полный | skipped/all_changes_ignored, verdict=null, 0 LLM; check-run skipped |
| Хотя бы один T, остальные только E/T, inventory полный и достоверный, incomplete нет | Backend FinalReviewResult: coverage=none, findings=[], verdict=null, 0 LLM; publishing → succeeded, check-run neutral; summaryOnly=false |
| Нет анализируемого текста, есть incomplete (возможно E/T); inventory полный, relevant counters известны, counted allowed ≤3 000 | Backend FinalReviewResult: coverage=partial, findings=[], verdict=null, summaryOnly=false, 0 LLM; publishing → succeeded/neutral; это не none и не skipped |
| Есть анализируемый текст и T/incomplete (E могут быть), counted allowed ≤3 000 | Ревью доступного текста с partial |
| Есть анализируемый текст, T/incomplete нет, inventory/counters полны, counted allowed ≤3 000 (E могут быть) | Обычное ревью, возможен complete/clean; доказанные binary/empty_file в excluded_files сами по себе не снижают coverage |

Доказанный empty-only/empty+ignored и binary-only/binary+ignored — строка «Только E»; too_large-only,
unsupported-only, неразрешённый rename+ignored и доказанный submodule/mode-only —
ветка none при выполнении всех её условий. E и T не считаются проверенными;
причины E включаются в excluded_files, T — отдельно в coverage.reasons.
Классификация сохраняется **до любого выхода**, включая skipped, отдельно от
VCS-only DiffMap/digest. Все backend-ветки обходят conventions, context, reviewer
и verifier, сохраняют закреплённые severity_threshold/max_comments и
below_severity_threshold_count=0; summary сообщает фактическое отсутствие анализа.
Для no-text partial известный reviewed_changed_lines=0, неизвестные исходные
статистики — null. Backend-результаты идут в общий путь сохранения результата/плана
и публикации; skipped завершается без публикации.

Выбор доказанного binary как E сохраняет текстовый scope: сервис не анализирует
бинарное содержимое. Только при подтверждённом E картинка рядом с кодом сама по
себе не делает результат неполным; bounded evidence, удаление без blob и лимит
чтения оговорены в §9.2. Отсутствие бинарного дефекта не утверждается.
Too_large означает неподдержанный размер; для текста остаётся непроверенный scope.
В #213 пустое добавление расширяет E только при полном proof `empty_file` (§9.2).
Проверенный код + доказанный empty_file может иметь complete/clean; empty-only
при полном достоверном inventory — skipped/all_changes_ignored, 0 LLM. Это
доказательство отсутствия изменённого текста, не отсутствия package/import-дефектов.
Неподтверждённое пустое добавление остаётся missing_patch/incomplete.

Решения #213 по остальным случаям сохраняют блокировку clean рядом с кодом:
доказанный pure rename/mode-only — unsupported_change/T; без no-text proof —
missing_patch/incomplete, raw status и нулевые counters не доказывают тип.
Удалённый binary без независимого proof остаётся missing_patch/incomplete;
base/old blob не читается, расширение не доказательство. Blob >1 048 576 байт
без предварительного независимого binary proof становится too_large до чтения:
это известный проектный компромисс подтверждения binary при blob cap 1 MiB,
а не универсальная публичная причина всех too_large. Prefix/range read и новый
size-origin столбец не вводятся. Для этих четырёх случаев дополнительных
tree/blob/compare-запросов нет; независимо доказанный binary остаётся E.
При пустых/info findings mixed run имеет partial/null; фактические supported
critical/high либо medium/low сохраняют blocking/attention при partial.

Канонические тексты причин #213 для summary/body/check-run (без завершающей
точки; оформление сводки может добавить счётчик, разделитель и точку):

| Случай | Полный текст причины |
| --- | --- |
| Доказанный pure rename, unsupported_change | Семантика переноса файла (включая импорты) не анализируется |
| Rename без no-text proof, missing_patch | Отсутствующий patch не подтверждает неизменность содержимого переноса |
| Доказанный mode-only, unsupported_change | Изменения прав доступа/типа файла не анализируются |
| Mode-change metadata без mode-only proof, missing_patch | Отсутствующий patch не подтверждает изменение только прав доступа/типа файла |
| Удаление без независимого binary proof | Удалённое содержимое недоступно для подтверждения binary |
| too_large без сохранённого точного источника лимита | Превышен лимит размера, содержимое не проверено |
| Квалифицированный кандидат empty-added без принятого ordinary-file proof | Нет подтверждения обычного пустого файла |
| Фактически отправленный changed patch опущен или усечён, review_input_incomplete | модель получила не весь patch |
| Технический drop unknown_path | не удалось сопоставить замечание с доступным diff |

Общие тексты причин (точные значения `REASON_TEXT`; специализированные случаи
выше имеют приоритет, счётчики и завершающая точка добавляются при оформлении):

| Код | Общий текст причины |
| --- | --- |
| classification_unavailable | нет достоверного сохранённого покрытия |
| diff_inventory_incomplete | список изменённых файлов неполон |
| missing_patch | Отсутствует подтверждённый patch |
| unsupported_change | семантика изменения метаданных не анализируется |
| too_large | Превышен лимит размера, содержимое не проверено |
| unsupported_path | неподдерживаемый путь |
| identity_unresolved | не установлена идентичность файла |
| patch_truncated | patch неполон |
| patch_malformed | patch повреждён |
| patch_unsupported | формат patch не поддерживается |
| summary_only | краткий обзор без проверки всех строк |
| review_input_incomplete | модель получила не весь patch |
| unknown_path | не удалось сопоставить замечание с доступным diff |

Метки оформления сводки и списка причин (не коды причин):

| Условие | Точная метка |
| --- | --- |
| coverage=partial | Проверка неполная |
| coverage=none | Текстовые изменения не проверены |
| Остаток причин после пяти отображённых, n — их количество | Ещё причин: {n} |
| Строчная метка verdict при partial без supported риска | проверка неполная |
| summary.done_well; reviewed_files — проверенные текстовые файлы, excluded_files — сумма exclusions | Проанализировано файлов с текстовыми изменениями: {reviewed_files}; исключено файлов: {excluded_files}. |
| Число проверенных текстовых файлов в partial/none overview | **Проверено файлов с текстовыми изменениями:** {reviewed_files} |
| summary.problem при наличии findings; n — число оставшихся findings | Подтверждено замечаний: {n} |
| Строка coverage в partial/none overview; summary_problem включает точку | **Coverage:** {status} — {summary_problem} |

Текст «Нет подтверждения обычного пустого файла» выбирается только по
доверенным backend-фактам квалифицированного кандидата: поддерживаемый
канонический путь, status=added, `previous_filename is None` (логический
`old_path=null`), additions=deletions=changes=0, точный Git empty-blob SHA и
size=None/0. Исходный patch равен None или пустой строке; при reload допустима
также точная сохранённая пустая обёртка `diff --git` / `---` / `+++` того же пути
без иных строк. При этом принятого ordinary-file proof нет. Один added, пустые
counters либо raw metadata_only недостаточны. Предыдущий путь, неподдерживаемый
путь, неизвестные/ненулевые counters, неверный/неизвестный SHA, конфликт размера
или иной patch не получают empty-текст: действует соответствующая общая причина,
для обычного added без patch — «Отсутствует подтверждённый patch».
У GitLab в этом потоке нет независимого tree-доказательства обычного файла:
пустое добавление остаётся `missing_patch`/partial, даже при empty SHA и size=0.
Квалификация переносится из durable фактов, не из model JSON. Новые сохранённые
coverage с более чем 20 причинами могут содержать внутренний optional
`reason_variant_counts`: не более шести фиксированных семантических ключей и
числа, без путей. Это сохраняет канонический текст за окном первых 20
диагностических путей без раскрытия новых имён. Restore проверяет суммы по
reason_counts и доверенную snapshot capacity; legacy без агрегата не получает
выдуманной квалификации за окном. Поле не добавляется в публичный DTO/LLM schema.
Отсутствие proof не утверждает, что файл действительно обычный и пустой.
Для исключения E=empty_file нужен независимый полный nontruncated tree на
закреплённом head: уникальное точное совпадение path+SHA, type=blob,
mode=100644/100755 и size=None/0; предел ответа — 1 500 000 сырых байт.
Для too_large снимок не хранит источник лимита (blob bytes, patch bytes или
строки), поэтому публичный текст нейтрален и не приписывает всем случаям binary/1 MiB.



### 2.3. Summary-only и исходный diff

Порог — **более 3 000 добавленных и удалённых строк разрешённых изменений**.
Ровно 3 000 не включает summary-only. Используются счётчики согласованной версии
VCS; отсутствие patch не считается нулём изменений. Если невозможно подтвердить
полноту списка файлов или счётчиков допущенных изменений, включается `summary_only` с причиной
`diff_inventory_incomplete`, без притворного построчного покрытия.

Все E и T из §2.2 исключаются **только из порогового счётчика** строк,
допускаемых к LLM. Это не превращает T в ignored и не снимает partial/none.
Исходные counters VCS сохраняются в диагностике; неизвестные — `null`, не 0.
Счётчик порога не подменяет общий VCS total_lines или метрики coverage.
Too_large с 20 001 изменённой строкой + 50 анализируемых строк даёт обычное
ревью 50 строк с partial; только too_large при полном inventory даёт none по §2.2.
Это целевой контракт #107: legacy main ещё считает too_large в пороге
(`get_run_diff._changed_line_count`), его внедрение требует изменения кода.

Ignore применяется до подсчёта: 4 000 исключённых строк + 50 разрешённых дают
обычное ревью этих 50 строк. Backend сохраняет исходный доступный `patch` для UI,
а `review_patch=null` для исключённых файлов и summary-only. Это не требование
очищать UI-снимок. Роль `code` не может восстановить такой patch для LLM из БД.

Целевой summary-only v1 формируется backend по разрешённой файловой статистике,
**без LLM-вызовов**, L1–L4, кандидатов и inline-комментариев. В текущем #213
zero-LLM lifecycle ещё не активирован: вызовы conventions/review возможны,
но все findings summary-only отбрасываются как insufficient_context, без inline
и blocking/attention из них; сохраняется partial/null. Это уточнение старого
описания «сводка по списку файлов»: сводка не утверждает дефекты по названиям файлов.
Итог `succeeded`, `coverage.status=partial`, `verdict=null`, check-run `neutral`:
«Построчное AI-ревью не выполнено: превышен лимит diff» (либо неполный список).

## 3. Архитектура расширенного контекста

### 3.1. Порты и происхождение

В `reviews/application` находятся контракты `ContextProvider`, `DiffMap`,
`ContextManifest`, `IgnorePolicy`, `BudgetAllocator`, `CandidateFilter`,
`VerifyCandidates`, `BuildFinalResult`. Инфраструктура реализует чтение VCS,
tree-sitter, кэши, LLM gateway, persistence и публикацию. Router только декодирует
транспорт и вызывает use case.

Распределение реализации и границы передачи данных:

| Владелец | Ответственность | Результат для других участников |
| --- | --- | --- |
| Инженер 3 — **createunix** | Порты чтения VCS и их GitHub-реализация; DiffMap; загрузка `.review/rules.md` и корневого `AGENTS.md`, проверка raw bytes/UTF-8/лимитов, нормализация, разбор только `review-ignore`; таблица `run_policy_snapshots` и все её миграции, связанное хранение закреплённых версий run и staging `run_policy_documents`; AST и отбор контекста | Неизменяемые policy/diff snapshots, полные документы и контекст с provenance |
| Инженер 4 — [#120](https://github.com/larchanka-training/dmc-268-api-t6/issues/120) | Контракт и значения `prompt_bindings`/`execution_profile` (промпты, модели и профили); безопасный prompt envelope; бюджет после окончательного render; pre-send guard и manifest каждого LLM-вызова; review/verifier и проверка `evidence_refs`/`requirement_refs` | Данные для закрепления версий при создании run; проверенные кандидаты и данные верификации/покрытия для финализации результата |
| Инженер 5 | Адаптеры записи VCS и Publisher: отображение допустимых якорей в API, сохранённый публикационный план, отправка и восстановление после ошибок | Подтверждённые remote identities/receipts и состояние публикации |

Версии промптов, моделей и профилей закрепляются при создании run до обращения
к VCS. Загрузчик атомарно создаёт полный snapshot, используя уже закреплённые
значения. Инженер 3 владеет также миграциями полей `prompt_bindings` и
`execution_profile`; инженер 4 передаёт их контракт и значения, не создаёт
отдельных миграций этой таблицы и не дополняет готовый snapshot через `UPDATE`.

Передача инженеру 4 содержит `policy_snapshot_id`, `policy_digest`,
`policy_base_sha`, версии профилей и обе записи документов с `path`, `presence`,
`blob_oid`, `raw_sha256`, `text_sha256`, полным нормализованным `text` и координатами
его строк. Дополнительно передаются разобранные ignore-маски и `ignore_digest`.
Это проекция закреплённого snapshot из RULES_FORMAT_SPEC §7, а не повторная загрузка
активных файлов. Backend не извлекает из Markdown семантический список требований:
инженер 4 использует полные документы, а атрибуцию проверяет по точным строкам,
реально показанным в manifest соответствующего вызова.

Версия общего профиля лимитов закрепляется при создании run; загрузчик использует
закреплённую версию, а инженер 3 проверяет ограничения доступных ему исходных данных.
Проверка итогового policy/token budget и допуска всех источников после render
принадлежит инженеру 4: успешная загрузка
документов сама по себе не разрешает LLM-вызов. Общие контракты и порядок передачи
не меняют runtime-последовательность §2.2.

Каждый блок несёт `source_role`, `repository_id`, `revision`, `path`,
`blob_sha/content_sha256`, `origin_paths`, диапазоны строк и причину включения.
`source_role` различает `policy_document`, `service_defaults`, `conventions`,
`pr_metadata`, `diff`, `source_excerpt`, `ast_excerpt`, `retrieved_excerpt`,
`candidate`, `previous_output`. `retrieved_excerpt` — фрагмент неизменённого кода,
выбранный по графу символов RAG v1 и прочитанный на ревизии стороны PR (§3.5).
Документ из `head_sha` в diff имеет роль `diff`, даже если его путь `.review/rules.md`.
Только два действующих снимка документов из base имеют исключение из ignore.

Запрет rename/copy распространяется на сущность файла во всём run: если
`generated/client.py → src/client.py` исключён по исходному пути, `src/client.py`
не возвращается ни как L3, ни как imported definition. Проверяются оба пути,
провайдерская история изменения и локальный deny-set идентичностей файлов.
Для `unsupported_path` на любой стороне rename/copy тот же запрет охватывает весь
`DiffFile`, новый файл и известные aliases: валидная половина пути не разрешает
обойти проверку ignore второй половины. Новая загрузка через L3/L4 запрет не снимает.
Если происхождение copy неизвестно провайдеру, backend не обещает обнаружить
произвольное копирование текста; гарантия относится к известным источникам.

### 3.2. Уровни L0–L4

| Уровень | Содержимое | Ревизия и ограничения |
| --- | --- | --- |
| L0 | PR metadata, раздельные документы политики, дефолты, конвенции, сведения о покрытии | Документы/общие конвенции из base; metadata текущего run недоверенны |
| L1 | Разрешённые hunks с номерами обеих сторон | Только сохранённый DiffMap; список файлов сам по себе не доказательство кода |
| L2 | Окна ±30 строк вокруг hunks, объединённые в пределах файла/стороны | RIGHT из head; LEFT только из подтверждённого diff_old_sha |
| L3 | Полный разрешённый файл | Source: ≤1 500 строк и ≤12 000 токенов; иначе L2 с окном до ±80 |
| L4 | Затронутые символы, сигнатуры и docstrings однозначно разрешённых импортов | Python, TypeScript/TSX, JavaScript/JSX, Go; одна степень импортов, без выполнения кода |
| RAG v1 | Неизменённый код по подтверждённому обратному ребру графа и битые ссылки на удалённый экспорт (§3.5) | Индекс default branch только выбирает кандидатов; RIGHT из head, LEFT из diff_old; один обратный шаг сверх L4 |

Тесты и конфиги получают L3 только если разрешённые изменения содержат лишь такие
файлы. L1 имеет преимущество перед дополнениями. Уровни могут комбинироваться:
manifest содержит реальный набор, `levels_present`, а не вывод о видимости из
одного максимального `level_used`.

Для L2 по AST выбирается минимальный объемлющий function/method/class/type. Если целый
символ не укладывается в лимит фрагмента, остаётся обычное окно; нельзя маркировать
усечённую функцию как показанную целиком. L3 заменяет дублирующие RIGHT-окна L2,
но не удаляет происхождение L1 и старые строки patch. Фрагменты разных ревизий
никогда не склеиваются в один диапазон.

### 3.3. AST v1

| Язык и расширения | Grammar | Извлекаемые объявления |
| --- | --- | --- |
| Python: `.py`, `.pyi` | [tree-sitter-python](https://github.com/tree-sitter/tree-sitter-python) | Функции, методы, классы, переменные |
| TypeScript/TSX: `.ts`, `.tsx` | [tree-sitter-typescript](https://github.com/tree-sitter/tree-sitter-typescript), отдельные TypeScript/TSX grammars | Функции, именованные arrow/function bindings, методы, классы, переменные, типы и интерфейсы |
| JavaScript/JSX: `.js`, `.jsx`, `.mjs`, `.cjs` | [tree-sitter-javascript](https://github.com/tree-sitter/tree-sitter-javascript), включая JSX | Функции, именованные arrow/function bindings, методы, классы, переменные, прямые ES exports |
| Go: `.go` | [tree-sitter-go](https://github.com/tree-sitter/tree-sitter-go) | Функции, методы с receiver, объявления типов, struct/interface, package-level const/var |

Go struct/interface и TypeScript interface имеют `kind=type`; const — `kind=variable`
с `declaration_kind=const`. Языковая форма сохраняется в необязательном
`declaration_kind`, не меняя общий набор `kind`. Для JS/Go `docstring_ranges`
означает непосредственно примыкающие doc comments. Точные версии runtime, grammar,
queries и resolver закрепляются в `ast_profile_version` и lockfile при реализации.
Другие языки сохраняют L1–L3; отсутствие parser не отменяет базовые проверки.
Здесь зависимости не добавляются.

В пределах run разбираются AST изменённых разрешённых файлов, затем непосредственно импортированные
ими определения. Дерево для RIGHT берётся на `head_sha`; для LEFT — на
`diff_old_sha`. Импорт из LEFT не разрешается по head. AST — средство выбора
контекста, а не type checker и не доказательство поведения во время выполнения.

Проектный контракт символа:

```text
SymbolRef {
  symbol_id, language, kind: function|method|class|variable|type,
  name: string|null, path, revision, blob_sha,
  declaration_range, signature_ranges[], docstring_ranges[],
  enclosing_symbol_id?, declaration_kind?, exported, parser_profile_version
}
ImportRef {
  importer_symbol_id?, module, names[], resolved_symbol_ids[],
  resolution: resolved|external|ambiguous|dynamic|excluded|unsupported|missing,
  resolution_profile_version
}
```

Затронутый символ пересекается с добавленными/удалёнными строками соответствующей
стороны. Для вставки между символами записывается module-level change, а не
произвольно соседняя функция. Импорт выбирается, если его имя непосредственно
использовано в изменённом символе, аннотации, декораторе или списке базовых классов.
Имя атрибута, keyword-аргумента или метода Python не считается использованием имени:
`repository.save()` и `f(save=1)` не используют импорт `save`.
Выдаются сигнатура и docstring, а тело импортированного определения — только
как дополнительный точный `source_excerpt` в общем бюджете. Сигнатура не
подтверждает утверждений о непоказанном теле.

Резолвер v1:

- Python: явные относительные импорты внутри дерева пакетов; абсолютный модуль
  ищется в корне и `src/` только при единственном кандидате `.py`, `.pyi` либо
  `__init__.py`. Одновременные runtime/stub варианты отмечаются отдельно; stub
  не считается телом runtime. Несколько независимых кандидатов → `ambiguous`.
- TypeScript/TSX: относительные статические imports; точные пути и кандидаты
  `.ts`, `.tsx`, `.d.ts`, `index.ts`, `index.tsx` выбираются только при
  однозначности. Path aliases, package exports, dynamic import и цепочки barrel
  re-export в v1 не разворачиваются, причина `unsupported`/`dynamic` сохраняется.
  Extension substitution по `tsconfig` не моделируется: import с JS-расширением
  при возможном TS/stub кандидате → `unsupported`, а не выбор `.js` по совпадению
  имени ([TypeScript resolution](https://www.typescriptlang.org/docs/handbook/modules/reference.html#file-extension-substitution)).
- JavaScript/JSX: только относительные статические ES imports. Путь с расширением
  читается точно; для пути без расширения рассматриваются `.js`, `.jsx`, `.mjs`
  и соответствующие `index.*`; несколько кандидатов → `ambiguous`. Разрешаются
  named/default imports и `namespace.Name` только к прямому export объявления
  или локального binding в этом файле; анонимный default export сохраняет
  координаты объявления и `name=null`. Side-effect import не выбирает
  символ. CommonJS `require`/`module.exports`/`exports`, `.cjs` как цель импорта,
  re-export, package imports/exports, aliases и смешанное TS↔JS разрешение —
  `unsupported`, `import()` — `dynamic`. `.cjs` разбирается для локальных символов
  и L2, но синтаксический ES export в нём не объявляется runtime export.
- Go: только literal import path внутри одного локального module, заданного
  `module` в корневом `go.mod` той же ревизии. Этот разрешённый политикой файл
  читается как данные; `go.work`, `replace`, вложенная граница `go.mod` или
  неподтверждённая полнота дерева → `unsupported`. Стандартная библиотека и
  сторонние модули → `external`; пути не загружаются из сети или module cache.
  В выражении `pkg.Name` выбирается однозначное экспортируемое package-level
  объявление. Явный alias берётся из import, имя по умолчанию — из `package`
  целевого пакета, **не** из последнего сегмента пути. `_` не выбирает символ,
  dot import → `unsupported`. Вывод типа receiver, поиск методов через значения
  и неявный поиск объявлений соседних файлов исходного пакета не выполняются.
- Внешние зависимости не скачиваются. Проектные конфиги не исполняются, package
  manager, `npm` и `go list` не запускаются; symlink и submodule не обходят границу
  репозитория. Неподтверждённое связывание имени, включая локальное затенение
  import binding, → `ambiguous`, а не предположение о целевом символе.
- Повторный импорт того же symbol/blob использует один блок. Глубина — один
  переход от изменённого файла; циклы и рекурсивное раскрытие запрещены.

Для разрешения Go-пакета нужна полная inventory его непосредственных non-test
`.go` файлов; это не рекурсивный разбор AST. Файлы `*_test.go`, `_*.go` и `.*.go`
не входят в целевой пакет; изменённые тесты разбираются как обычные изменённые
файлы. Все прочитанные для разрешения файлы, включая `go.mod` и не давшие выбранного определения, входят
в общий лимит **20 импортированных/служебных файлов**, с теми же blob/CPU limits.
Неполная inventory или исчерпание лимита → `unsupported` с причиной `ast_limit`;
исключённый член пакета не читается и даёт `excluded` для всего разрешения. Decode/parse failure члена
пакета также запрещает `resolved`; сохраняются причина и `unsupported`.
Уникальность имени нельзя выводить по оставшемуся подмножеству. Различные package
names или несколько подходящих объявлений → `ambiguous`.

Build target в v1 не задаётся: наличие у любого non-test члена пакета
`//go:build`, legacy `// +build`, известного GOOS/GOARCH суффикса или `import "C"`
даёт `unsupported` для разрешения пакета целиком. Списки распознаваемых суффиксов
закреплены resolver profile; неизвестная форма, влияющая на состав пакета, не
считается доказательством полноты. Изменённые условные/cgo файлы сохраняют локальный
AST и L1–L3 с явной неопределённостью build context. Семантика имён пакетов и
ограничений сборки: [Go imports](https://go.dev/ref/spec#Import_declarations),
[Go build constraints](https://pkg.go.dev/cmd/go#hdr-Build_constraints).

Tree-sitter задаёт нулевые byte/row координаты. Backend преобразует их в
однобазовые строки; exclusive end на колонке 0 следующей строки не включает её.
Исходный текст UTF-8 и LF/CRLF сохраняет соответствие строк, Unicode-нормализация
не выполняется. Правило координат основано на
[Tree-sitter Basic Parsing](https://tree-sitter.github.io/tree-sitter/using-parsers/2-basic-parsing.html).

### 3.4. Лимиты и fallback

| Ограничение AST/context v1 | Значение | Исход превышения |
| --- | --- | --- |
| Blob для разбора/окна | 256 KiB, строгий UTF-8 | L1 остаётся, `source_too_large`/`source_decode_failed` |
| AST parsing | ≤1 с CPU на файл, ≤5 с суммарно на попытку | Остановка parser worker, L1/L2 по строкам, `ast_timeout` |
| Память parser worker | 128 MiB | L1/L2, `ast_resource_limit` |
| Файлы AST/resolver | ≤50 изменённых + ≤20 импортированных/служебных | Включая все просмотренные Go package members и `go.mod`; остальные `ast_limit` |
| Импортированные символы | ≤10 на изменённый файл, ≤100 на run | Оставшиеся записываются как `ast_limit` |
| Фрагмент L2/L4 | ≤200 строк и ≤2 000 токенов | Уменьшение по явным границам/отказ от расширения |
| L4 всего | ≤8 000 входных токенов | Остальные символы не передаются |
| Явный контекст тела импорта | Не более одной функции/метода, в тех же лимитах | Только сигнатура/docstring, отмечена неполнота |
| Слот RAG v1 | ≤4 000 входных токенов; ≤20 чтений блобов, ≤10 файлов-кандидатов, ≤3 мест на файл за run, включая чтения и разбор для подтверждения рёбер | Остальные кандидаты не передаются, `rag_limit` |
| Кандидат RAG на ревизии PR | Файл или символ присутствует и читается, ребро подтверждено резолвером v1 | `rag_candidate_missing`, `rag_edge_unconfirmed`, ошибка чтения — `source_read_failed` (§3.5) |

Парсер работает в отдельном процессе-worker без исполнения репозитория. Как
построено (#168): процесс стартует с минимальным окружением (`PYTHONPATH`,
`PYTHONDONTWRITEBYTECODE`, без секретов сервиса), stderr уходит в DEVNULL;
`RLIMIT_AS` — текущий размер после загрузки грамматик плюс 128 MiB (без `/proc`
лимит не ставится); мягкий `RLIMIT_CPU` на запрос — накопленное CPU + 1 с + 20 с
запаса, страховка на случай, если родитель не остановит worker; `RLIMIT_CORE` 0.
Лимит 1 с CPU на файл проверяет сам worker, но идущий разбор из Python не прервать,
поэтому родитель ждёт ответ по wall clock (1 с + 2 с запаса, +15 с на старт
процесса) и убивает worker без ответа; 5 с на попытку родитель считает по CPU, о
котором отчитывается worker. Сетевой изоляции ОС нет: «без сети» — свойство кода
парсера, который читает исходники только из pipe и сеть не открывает (решение
техлида, #168).
Ошибка синтаксиса или `ERROR`/`MISSING`-узел, пересекающий символ, запрещает считать
его извлечение полным; доступные точные строки можно передать как L2. Отказ
обязательного L1/VCS-снимка не маскируется под AST fallback. Ошибка дополнительных
уровней не делает run `failed`, но фиксируется в coverage; verifier при нехватке
доказательств обязан иметь возможность вернуть `insufficient_context`.

### 3.5. Кандидаты графа символов (RAG v1)

Решение и обоснование — [ADR 0001](adr/0001-graph-rag-v1.md); здесь — нормативные
правила для контекста run. Шаг выполняется только при включённом тумблере RAG
репозитория. Тумблер включён, но индекс не готов (`empty`, `building`, `failed`), —
run продолжается без RAG, trace `rag: not_ready` с причиной, у `failed` — и с его
`error_code`; ожидания индекса нет. Пустой или отставший индекс ревью само ставит в
очередь и сборки не ждёт, если пробуждение ещё не запрошено, сборка не держит живой lease
и у цели индекса нет терминального отказа (ADR §6.1): `stale` — на обновление, `empty` —
на full, не чаще раза на целевой commit и раза в 24 ч, в том числе после TTL-очистки
([SD §10](SYSTEM_DESIGN.md#review-full-throttle)); `building` и `failed` ревью не трогает.
Если в закреплённой версии промпта run нет секции retrieved, RAG тоже пропускается:
`rag: not_ready`, причина `prompt_version`. Готовый, но отставший от HEAD default
branch индекс используется.

Канонический смысл сохранённых строк, включая archive/`export-ignore`/`export-subst`,
export-name join через `facts.exports`, Go directory/member paths, модульные рёбра
и неразрешённые записи импортов, задан в
[SD §7.4: контракт строк индекса](SYSTEM_DESIGN.md#index-row-contract).
Индекс хранит указатели (путь, диапазон, `blob_sha`, `indexed_commit_sha`), строки
символов (`defines`) и рёбра `imports`/`uses_imported`. Рёбра существуют только для
`resolved` по §3.3. Receiver, методы через значения, barrel re-export, package exports,
path aliases, dynamic imports и неявные связи Go не обходятся; явный alias импорта
поддерживается. Имя атрибута Python не является чтением имени: `repository.save()` не
даёт ребра к импортированному `save`. Кандидаты — файлы с ребром `imports` или
`uses_imported` к изменённому символу или модулю, на один обратный шаг сверх L4, без
рекурсии.

Правило ревизий:

1. Ревизия индекса (`index_commit_sha`, §2.1) только выбирает кандидатов. Текст
   ДОЛЖЕН читаться на ревизии стороны PR: `head_sha` для RIGHT, `diff_old_sha` для
   LEFT. Координаты default branch НЕЛЬЗЯ переносить в блок.
2. Каждое использованное ребро ДОЛЖНО заново разрешаться резолвером §3.3 на этой
   ревизии; диапазон и сигнатура заново извлекаются из её blob. Неподтверждённое
   ребро — omission `rag_edge_unconfirmed`.
3. Кандидат, отсутствующий на ревизии PR, — omission `rag_candidate_missing`;
   ошибка чтения его blob — существующая причина `source_read_failed`. Слишком
   большой, бинарный или не UTF-8 blob, язык без парсера и ошибка разбора ребро не
   подтверждают (`rag_edge_unconfirmed`); исчерпанный лимит чтений — `rag_limit`.
4. Битая ссылка определяется по обеим ревизиям. Исходный набор — экспортируемые
   символы, которые есть в извлечении LEFT (`diff_old_sha`) изменённых файлов и
   отсутствуют в извлечении RIGHT (`head_sha`), включая все экспорты удалённого,
   переименованного или перемещённого файла (старый путь DiffMap). Файл с неполным
   извлечением RIGHT (ERROR/MISSING, timeout, лимит; §3.4) удалённых экспортов не
   даёт; извлечение LEFT читается в бюджете чтений слота RAG, извлечение RIGHT —
   то, что уже загрузил L4. Чистое переименование тоже источник, но PR из одних
   перемещений сборка завершает до слота (`no_reviewable_files`).
   Импортёры `(старый путь модуля, имя)` берутся из графа. Импортёр — кандидат
   `rag_broken_reference`, не omission, если на `diff_old_sha` его импорт
   разрешается (`resolved`) в модуль, экспортирующий имя, а на `head_sha` тот же
   спецификатор с тем же именем даёт любой иной исход: `missing`, `external`
   (кандидата-файла нет), `unsupported`/`ambiguous` или модуль без этого имени.
   Импорт, сломанный ещё до PR, кандидатом не становится. Импорт, который остался,
   но на `head_sha` больше не связывает имя, читаемое объявлением, тоже битый; у Go
   без явного alias имя пакета читается из его файлов, у пакета, который резолвер
   отказался читать, — из `package` первого файла, где clause есть. Только если
   clause не даёт ни один файл, имя берётся из последнего сегмента пути, и импорт с
   другим `package` даёт тот же исход — известное ограничение v1. База разрешается
   с путём модуля Go из `head_sha`: PR, переименовавший модуль, импортёров не
   обвиняет. Исход, который
   дало прерванное чтение модуля (сбой чтения или разбора, ignore), и отсутствие
   модуля при неполном дереве `head_sha` поломку не доказывают: такой импортёр без
   другого подтверждённого ребра — `rag_edge_unconfirmed`, а если разрешение
   остановил лимит чтений — `rag_limit`. Без
   `diff_old_sha` (или при сбое чтения его дерева) имена удалённых экспортов
   неизвестны: целями становятся модули удалённых и переименованных файлов
   (`(старый путь, "")`), их импортёры не читаются и отбрасываются с путём по
   `old_revision_unknown` (при сбое дерева — `source_read_failed`); изменённый файл,
   который мог удалить экспорты, учитывается по той же причине один раз со своим
   путём. Место битой ссылки даёт блоки с одной ревизией каждый: импорт и
   использование на `head_sha` (RIGHT, по правилам блока ниже) и сигнатуру каждого
   удалённого определения, которое оно читало, на `diff_old_sha` (LEFT,
   `signature`). Склейка ревизий запрещена (§3.2). Если импорта на `head_sha` уже нет,
   кандидата нет.
5. Набор кандидатов не входит в policy snapshot run, но блоки RAG входят в
   checkpoint кандидатов/manifest принятого reviewer response (§7.4): retry при
   наличии checkpoint переиспользует их, retry до него может пересобрать их по более
   новой публикации индекса. Показанное фиксирует manifest каждого вызова (§5).
   Возобновления с checkpoint в коде ещё нет: повторная попытка выбирает RAG заново
   (§7.4).

Якорь finding остаётся строкой RIGHT файла DiffMap; §6.2 не меняется. Блок
`retrieved_excerpt` можно указать в `evidence_refs`; LEFT-блок `diff_old_sha`
служит только доказательством. Manifest verifier повторно показывает те же блоки
`retrieved_excerpt`, на которые ссылаются кандидаты (§5.2).

Ignore применяется к кандидатам повторно: закреплённый ignore run проверяется до
чтения блоба (RULES_FORMAT_SPEC §5); исключённый кандидат не читается и
учитывается только счётчиком. Чтения и разбор для подтверждения входят в лимиты
слота RAG (§3.4), а не в лимиты L4 и не в per-run лимиты резолвера §3.4
(20 импортированных файлов, 10/100 символов): у индексации и подтверждения свои
профили лимитов. Версия профиля лимитов индексации хранится с индексом, и её смена
вызывает full; лимиты слота RAG в версию индекса не входят. Блок
кандидата — типизированная недоверенная секция envelope с ролью
`retrieved_excerpt` (§3.1, §5.1); новый блок контекста требует новой версии
промпта. В коде это секция `<retrieved_excerpts>` промпта snapshot-пути сразу после
`<context_excerpts>`: по элементу `<excerpt>` на блок (`block_id`, уровень `RAG`,
путь, ревизия, сторона, представление) с пронумерованными исходными строками, как у
L2–L4. Показ секции следует закреплённой версии промпта, а не слоту: у версии с
секцией пустой слот и сбой сборки дают пустую секцию, поэтому оба плеча A/B идут одной
версией. Набор версий с секцией retrieved пуст, поэтому ни один run её пока не
показывает.

Отбор идёт внутри сборки контекста, после L4 и до L2/L3 (§4.1); сборка, которая
завершилась раньше (урезанный L1, недоступное дерево `head_sha`, summary-only и
пропуски), слот не заполняет. Цели графа — удалённые экспорты битых ссылок
(первыми), экспортируемые имена изменённых символов и изменённые на уровне модуля
файлы; граф знает файл по пути default branch, то есть по старому пути DiffMap.
За run не больше 200 целей, их читает одна транзакция REPEATABLE READ READ ONLY;
цели сверх 200 — `rag_limit` без пути, по одному на цель.
Если это чтение находит индекс непригодным, кандидатов и счётчиков нет, а trace
пишет `rag: not_ready` со свежим статусом. Файлы PR кандидатами не становятся.
Кандидаты подтверждаются по одному в порядке приоритета слота (§4.1). Резолвер
разрешает только привязки, которые по пути могут вести к изменённому файлу, и не
тратит чтений на другие модули; исключение — `from pkg import name` Python, которое
может связать подмодуль: оно действует для ребра любого вида, потому что индексатор
даёт такому импорту ребро `imports` к подмодулю. Подмодуль подтверждается по имени,
которое связывает импорт: объявление читает привязку, не перекрывая её, а `__init__.py`
пакета не связывает это имя сам — верхнеуровневым определением, присваиванием или
импортом не из этого подмодуля (`from . import store`, `from pkg import store` и
`import pkg.store as store` в `__init__.py` связывают сам подмодуль). По ребру `imports`
член подмодуля разрешаться не обязан; по ребру `uses_imported` привязка разрешается в
тот же экспорт изменённого файла. Пакет, исключённый ignore или не прочитанный, ребро
не подтверждает, остановка лимитом чтений — `rag_limit`. Ребро любого вида не
подтверждают изменённый модуль с ошибкой разбора (даже там, где разрешение его не
читает), модуль, который резолвер отказался читать (ошибка разбора в файле его пакета,
файл пакета, который нельзя прочитать), и импорт `*` или dot import Go: их
использование резолвер только предполагает. Ребро `imports` к пакету Go, хоть один
файл которого исключён ignore или не читается, не подтверждается
(`rag_edge_unconfirmed`), а если чтение файла остановил лимит — `rag_limit`.
Известные ограничения v1:
перекрытие имени видно только по верхнеуровневым конструкциям, которые извлекает
экстрактор, — импорты и определения внутри `try:` или `if:`, цели-кортежи
присваивания, `from .x import *` и модульный `__getattr__` не видны; пакет
пространства имён Python (без `__init__.py`) не обходится, и `from pkg import store` в
нём не подтверждается — как в правиле резолвера v1 (один кандидат `.py`, `.pyi` или
`__init__.py`); резолвер §3.3 (L4, индексатор и база битой ссылки) разрешает такой
импорт в подмодуль и тогда, когда `__init__.py` импортирует это имя сам, — исправление
в follow-up резолвера #130.

Чтение слота — один `blob_sha` за run через кэш блобов: попадание или промах,
удачное или нет; тот же blob считается один раз. Текст и факты, которые L4 уже
загрузил в этом run, переиспользуются и чтением RAG не считаются; файл базы с тем же
blob, что на `head_sha`, читается и разбирается один раз для обеих сторон. Чтения
идут по одному, в порядке приоритета. Извлечения LEFT для битых ссылок берут не
больше 10 файлов-источников из 20 чтений (`MAX_RAG_BASE_READS`): источник сверх
них — `rag_limit` с путём; источник, чьё извлечение недоступно, учитывается один раз
с путём: `rag_limit` при исчерпании чтений, иначе `source_read_failed` — при сбое
чтения, для бинарного, слишком большого или не UTF-8 файла и при ошибке разбора. Его
импортёры не проверяются. Файл, одно из имён которого запрещает ignore,
источником не становится: дерево и blob базы ради него не читаются, путь не
называется; извлечение, которое всё же упёрлось в ignore, учитывается только
счётчиком `excluded`, без пути.

Лимит 10 файлов считает только кандидатов, у которых есть места для слота;
фактическая граница — 20 чтений. Подтверждённый кандидат, все места которого уже
показывает L4, блока не даёт и остаётся подтверждённым. Место — самое внутреннее
объявление, которое читает привязку; у файла не больше трёх мест, остальные —
`rag_limit` с путём и их числом.
Слот — `min(4 000, остаток бюджета после L1 и L4)`; места входят в порядке
приоритета, не поместившееся место пропускается (`rag_limit` с путём, суммарно по
пути), следующее ещё может войти, а кандидат остаётся подтверждённым. Место битой
ссылки входит вместе со своими определениями LEFT или не входит; определение
показывается один раз, и в лимит 10 файлов определения LEFT не входят. Место, текст
которого или текст его определения LEFT не совпадает строка в строку с его
диапазонами, в слот не входит: omission `rag_candidate_missing` с путём места,
суммарно по пути, а кандидат остаётся подтверждённым (рендер выдержки отказал бы
такому блоку терминально). Сбой отбора, кроме ошибки политики, отмены и дедлайна,
оставляет слот пустым; L1–L4 и L2/L3 не меняются.

Если промпт с секцией retrieved не помещается в бюджет вызова, fit снимает выдержки
целиком в порядке L3 → L2 → RAG → L4 и только после них режет файлы diff. RAG
снимается по местам, начиная с последнего вошедшего: место уходит вместе со своими
определениями LEFT, кроме определения, которое читает оставшееся место. Fit verifier
(§5.2) так же ставит процитированные места RAG после L4 и перед L2/L3.

Блок слота (`RetrievedBlock`, [SD §9](SYSTEM_DESIGN.md#сборка-и-бюджет)) —
`block_id` `RAG:<путь>:<a>-<b>,…`, уровень `RAG`, `SourceRef` с ролью
`retrieved_excerpt`, ревизией, blob и стороной PR, путём, диапазонами и причиной
выбора (`rag_reverse_edge` или `rag_broken_reference`), представление, текст,
токены, а также вид ребра и `index_commit_sha` как метаданные выбора. RIGHT
(`head_sha`) — строки импорта всех привязок, которыми место подтверждено, и всё
объявление места (`exact_lines`), если его извлечение полное и оно не больше 200 строк
и 2 000 токенов (лимит фрагмента, §3.4), иначе строки импорта и сигнатура
(`signature`). LEFT (`diff_old_sha`) — сигнатура удалённого определения
(`signature`). Блоки идут в список `retrieved`, а не в блоки файлов diff, и входят
в `tokens_used`, `levels_present` и `tokens_by_role`. Место битой ссылки хранит
источники определений LEFT, которые оно читало (`definitions`).

Этот `block_id` остаётся в payload (trace `context.build` id блоков не пишет).
Промпт, manifest и evidence snapshot-пути называют блок
`RAG:<revision[:12]>:<sha256(block_id)[:16]>`: по ревизии его стороны
(`diff_old_sha` у LEFT, `head_sha` у RIGHT) и дайджесту id стадии, без пути, поэтому
длинный путь не выводит id за 128 символов `block_id` evidence. От
`index_commit_sha` id не зависит: повторная попытка с более новой публикацией
индекса показывает то же место под тем же id.

## 4. Бюджет, конвенции и кэш

### 4.1. Распределение контекста

Общий вход каждого вызова с учётом system message, схемы и служебного framing:
fast ≤60 000 токенов, deep ≤150 000, дополнительно ограничен окном конкретной
модели минус максимум выхода. Применяется более строгая граница. Профиль
токенизатора/оценщика и запас framing закрепляются; при неподдерживаемом точном
tokenizer используется консервативная оценка gateway, а не подсчёт одних diff-строк.

Документы политики не усекаются и занимают не более 12 000 токенов суммарно
в rendered форме по RULES_FORMAT_SPEC. Обязательные system/defaults/policy
должны вмещаться также в verifier и разрешённый fallback; иначе явная
`policy_prompt_budget_exceeded` до первого вызова. PR body ограничен 4 000
токенами с явным маркером усечения; конвенции — 4 000, дерево/списки — 2 000.
Сообщения коммитов (#82) входят в L0 и общий input budget: первые 20 в API-order,
каждое ≤500 Unicode-символов, всего ≤10 000 до XML escaping; после escaping
учитывается фактический token-size с framing. Они остаются недоверенными
метаданными намерения автора, не evidence и не общим входом conventions.
Сокращённые метаданные не служат доказательством отсутствующего текста.

Приоритет файла:

```text
weight(type) × ln(additions + deletions + 1) × rule_multiplier
weight: source/other=1.0, test=0.6, recognized_config=0.4
rule_multiplier: 1.5 если хотя бы одно структурированное правило применимо,
                 иначе 1.0; совпадения не перемножаются
tie-break: canonical path в порядке Unicode code points
```

Применимость для бонуса — совпал хотя бы один `include` и ни один `exclude`
в одном правиле. Сохраняется отдельная существующая семантика этого matcher,
включая предел brace expansion 256; ошибка/переполнение маски правила не даёт
бонуса. Текст Markdown не превращается в маски. Бонус не меняет иерархию требований
и не отключает дефолты для путей без совпадения.

Отбор: L1 → сигнатуры L4 → RAG → L2 → L3 по приоритету. Слот RAG (§3.5) получает
остаток после L1 и L4, не больше 4 000 токенов; при нехватке он вытесняет только
L2 и L3, L1 и L4 не вытесняются. Внутри слота: битая ссылка > вызывающие
изменённого экспортируемого символа > импортёры изменённого модуля; остальное —
`rag_limit`. Если L1 не помещается, сохраняются
целые hunks, затем явный префикс строк последнего hunk с исходными координатами;
усечение маркируется и отражается в manifest. DiffMap остаётся полной доступной
картой и не переписывается под усечённый prompt. Исключённые файлы заменяются
общим счётчиком во всех модельных `repo_tree`/`changed_files`/`omitted_files`;
разрешённые, но не вместившиеся пути можно перечислять как непроверенные.

### 4.2. Общие конвенции

На промахе кэша выбирается до 10 разрешённых base-файлов, суммарно ≤128 KiB,
не более 300 начальных строк каждого. Для воспроизводимости v1 использует
лексикографический порядок canonical path после фильтра source/ignore, без
зависимости от PR. Состав языка определяется по отфильтрованному base-дереву,
не по dominant stack текущего PR. Это ограниченная выборка, не описание всего repo.

В общий вызов НЕЛЬЗЯ включать title/body PR, изменённые пути, head-tree или код head.
Правила, AGENTS, defaults, base-tree и исходники имеют раздельные роли. Выводы
не становятся новыми полномочиями. Рекомендации текущему PR формирует review;
отдельный PR-specific conventions call в v1 не выполняется и общий кэш не загрязняет.

Ключ:

```text
SHA256(canonical_json({
  namespace: "repo-conventions-v2", repository_id, policy_base_sha,
  rendered_input_digest, shown_base_tree_digest,
  source_entries: [{path, blob_sha, shown_ranges, excerpt_sha256}],
  policy_digest, ignore_digest, matcher_version,
  defaults_checksum, conventions_prompt_checksum,
  model_profile_version, output_schema_version,
  selector_version, truncator_version, tokenizer_profile_version
}))
```

`rendered_input_digest` охватывает окончательные сообщения целиком, включая
порядок блоков. Сохраняются manifest происхождения и фактическая модель/маршрут.
Одинаковые SHA выбранных файлов без показанного дерева недостаточны. Старые
записи без base-only provenance получают namespace legacy и дают cache miss;
пустой provenance не дорисовывается. Результат конвенций неизменяем для данного
ключа: при гонке принимается первая успешно сохранённая валидная запись.

Как построено (#130, отклонение по решению техлида 25.10.2026, TL-7 в
`docs/plans/130-snapshot-path-plan.md`). Snapshot-путь пишет в
`repo_conventions.policy_key` SHA-256 от канонического JSON: namespace
`repo-conventions-v2`, ignore key попытки (`ignore_digest`, а если rename/copy
скрыли пути без совпадения маски, — digest от него и этих путей; `ignore_digest`
уже покрывает версии matcher, встроенных исключений и лимитов), `text_sha256`
rules.md и AGENTS.md, `defaults_checksum`, id и checksum закреплённого conventions
prompt и `counter_version`. repository_id, `agents_md_sha` и `prompt_version_id`
остаются отдельными колонками уникального ключа (`NULLS NOT DISTINCT`). В digest не
входят `policy_base_sha`, review- и verifier-prompts, остальной execution profile,
версии selector/truncator (таких констант нет), rendered input, показанное дерево
и source entries: следующий PR репозитория переиспользует запись, а промах дают
смена политики, дефолтов, AGENTS.md, conventions prompt или counter. Отступление
от запрета изменённых путей выше: до 100 изменённых путей PR идут в
`<changed_files>` вызова (остальные — строкой-счётчиком), но в ключ не входят.
Legacy-путь ключует по ignore key попытки (NULL у run без снимка), поэтому его
записи snapshot-путь не переиспользует.

Если после отбора нет исходников для вывода конвенций, вызов не выполняется:
фиксируется `conventions_not_applicable`, действуют явные документы и дефолты.
Если вход есть и кэша нет, генерация выполняется в общем бюджете §7. Сбой
не подменяется пустыми конвенциями: используется доступное восстановление,
затем failed/retry по классу ошибки. Уже сохранённые конвенции переиспользуются
на retry при том же ключе. Это сохраняет существующее поведение ошибок стадии
и не вводит скрытой деградации ценой обязательной проверки кандидатов.

### 4.3. Кэши файлов и AST

| Объект | Ключ | Хранение |
| --- | --- | --- |
| Raw blob | `(repository_id, blob_sha)` | Существующий кэш, TTL 7 дней |
| AST факты | `(repository_id, blob_sha, language, grammar_version, query_version)` | Redis, TTL 7 дней; без run-specific разрешения |
| Разрешённые импорты | `(repository_id, revision_tree_sha, importer_blob_sha, resolver_version)` | TTL ≤7 дней; повторный policy guard обязателен |
| Дерево | `(repository_id, revision_sha)` | TTL 1 час; фильтрация перед каждым использованием |
| Конвенции | Полный ключ §4.2 | PG + Redis, TTL не заменяет проверку provenance |

Кэш собранного контекста v1 не вводится. Если появится, ключ включает policy,
DiffMap, точные источники, budgets и версии сборки, а guard остаётся обязательным.
Запрещено переносить результаты LLM между разными head/run только по findings hash.
Закэшированное разрешение хранит весь список источников, от которых зависит
однозначность, включая Go package inventory и `go.mod`: guard и лимиты применяются
к каждому из них, а не только к файлу выбранного символа. Результат по неполной
inventory нельзя переиспользовать как полное разрешение.

## 5. Manifest фактически отправленного контекста

### 5.1. Контракт

Manifest строится **после** последнего сокращения и экранирования сообщений,
до отправки провайдеру. Каждый фактический review/conventions/verifier/repair/
fallback имеет отдельный `call_id` и manifest, даже если использует те же блоки.
Как построено (#130): повтор транспорта (timeout, недоступность, 429) — не новый
вызов в этом смысле: он переиспользует `call_id` и строку manifest своего вызова,
хотя слот вызова расходует (§7.3).

```text
ContextManifest {
  version: 1, manifest_id, run_id, attempt, call_id,
  stage: conventions|review|verification, recovery_kind: none|retry|repair|fallback,
  parent_call_id?, policy_snapshot_id, diff_snapshot_id?,
  prompt_checksum, schema_version, model_profile_version,
  rendered_messages_sha256, input_tokens_estimate,
  blocks: [ManifestBlock], omissions: [Omission], created_at
}
ManifestBlock {
  block_id, source_role, source_kind, source_id, repository_id?, revision?, blob_sha?,
  path?, origin_paths[], side?: LEFT|RIGHT,
  content_sha256, rendered_content_sha256,
  shown_ranges: [{start_line, end_line}],
  representation: exact_lines|signature|metadata|policy|derived,
  source_ref, selection_reason, truncated
}
Omission {source_id?, reason, count?, allowed_path?}
```

Manifest хранит номера **исходных** строк, не номера строк XML/JSON prompt.
`signature` с разрывами сохраняет несколько диапазонов, не весь declaration range.
У строк patch каждая сторона имеет свой блок/проекцию; context line может быть
доступна на обеих сторонах с разными номерами. Синтезированная строка вроде
`[20 lines omitted]` не получает source range. Конвенции и metadata не являются
`exact_lines` кода, даже если содержат похожий текст.

Блок кандидата графа RAG v1 (§3.5): `source_role = retrieved_excerpt`,
`source_kind = source_code`; `revision` и `side` — ревизия стороны PR
(`head_sha` для RIGHT, `diff_old_sha` для LEFT), никогда не ревизия индекса;
`selection_reason` — `rag_reverse_edge` или `rag_broken_reference`;
`index_commit_sha` и вид ребра — метаданные выбора извлечённого блока рядом с его
`source_ref`, а не внутри него. Omissions RAG —
`rag_candidate_missing`, `rag_edge_unconfirmed`, `rag_limit`, `old_revision_unknown`
и существующая `source_read_failed`; исключённые ignore кандидаты передаются только
счётчиком `excluded`, без пути.

В коде блоки RAG попадают в manifest вызова, только если его промпт показывает секцию
retrieved (§3.5), и ровно те, что она показывает после fit: `shown_ranges` — все
диапазоны источника, `truncated = false` (блок показывается или снимается только
целиком), токены — отрендеренная выдержка закреплённым счётчиком, а не оценка слота,
`repository_id` — репозиторий источника, `index_commit_sha` и `edge_kind` — рядом.
`tokens_by_role` записи `context.manifest` (PIPELINE_SPEC §2) получает роль
`retrieved_excerpt` без изменения контракта.

### 5.2. Guard и связь с ответом

До каждого вызова backend проверяет все блоки по типизированному происхождению:
совпадение snapshot, разрешённые path/origin_paths, правильную ревизию, deny-set
rename/copy и `unsupported_path`, лимиты, hashes и отсутствие источника без provenance.
Содержимое и автоматически добавленные имена unsupported-файлов не допускаются
ни в один вызов, включая conventions/verifier/repair/fallback и кэшированные
источники; в metadata/omissions передаются только причина и количество.
Блок `retrieved_excerpt` с ревизией индекса RAG вместо ревизии своей стороны PR
также является нарушением (§3.5).
Нарушение →
`policy_context_violation`, `failed` без отправки. Поиск regex по готовому prompt
не заменяет эту проверку.

**Текущее состояние.** `verify_pre_send_guard` в
`app/modules/reviews/application/prompt_budget.py` проверяет только policy/total
бюджет токенов. Отдельная `verify_provenance_pre_send_guard` вызывается перед
каждым боевым вызовом review, conventions, repair и fallback и выдаёт
`policy_context_violation` при нарушении manifest: проверяет роль/kind, путь
(если указан) и применимые ревизии блоков. Кодовый блок (роль `diff`,
`source_excerpt`, `ast_excerpt`, `retrieved_excerpt` или kind `source_code`) с
ревизией не на стороне LEFT должен нести `head_sha` PR: без `head_sha` или с другой
ревизией — нарушение, как у LEFT без `diff_old_sha`. То же правило, но со своими
исходами, а не `policy_context_violation`, держат атрибуция evidence ответа (кандидат
отбрасывается с `revision_mismatch`) и проверка ссылок в решениях verifier (ответ
verifier недействителен). Блок `retrieved_excerpt` без
`repository_id` — нарушение и без snapshot. Проверки ignore deny-set,
policy-документов и репозитория кодового блока (его `repository_id` должен совпасть с
репозиторием snapshot) требуют policy snapshot. После #130 F1 worker передаёт snapshot
только для run с закреплённым `review.system:v4`, выполняя guard в адаптере после
единственного fit; legacy остаётся без snapshot и verifier. После F2 seed активирует
v4 для новых runs; закреплённый v3 или ниже сохраняет legacy-путь. Полный guard выше остаётся целевым контрактом
#107; наличие функций не означает внедрения всего нового lifecycle (§12).

Raw response записывается с `call_id`, `manifest_id`, content digest и outcome.
Принятый результат хранит `accepted_call_id`. Ссылки из repair/fallback разрешаются
только по его собственному manifest; ссылка на блок родительского вызова
допустима лишь если блок повторно присутствует в фактическом входе текущего.
Предыдущий невалидный ответ имеет роль `previous_output` и не является кодовым
доказательством. Он не может добавлять новые источники или команды.

Verifier (§7.1) повторно получает только те блоки RAG, на которые ссылаются
кандидаты, процитированное место — с определениями LEFT, которые оно читало (§3.5).
В коде они идут секцией `<retrieved_excerpts>` после процитированных выдержек L2–L4;
без процитированного блока RAG секции у verifier нет (пустая секция — правило промпта
ревью для плеч A/B). Блоки RAG входят в бюджет verifier: после собственных строк
кандидатов fit берёт выдержки в порядке L4 → места RAG с их определениями →
процитированные напрямую определения, чья группа места не вместилась, по одному →
L2/L3, затем остальные строки diff. Manifest verifier перечисляет ровно показанные блоки:
непроцитированного (кроме определений, которые прочитало процитированное место) или не
вместившегося блока RAG нет ни в prompt, ни в manifest.

Нужные для доказательств точные excerpts сохраняются в PG как deduplicated
`context_evidence_blobs`, а manifest ссылается на них. Полный сериализованный
prompt хранить не требуется; ссылки на истекающий Redis blob недостаточно для
аудита. Excerpts, manifests, снимки, ответы и решения доступны только workspace,
удаляются каскадом вместе с ним. В обычных логах остаются IDs/digests/счётчики;
контент и секреты не выводятся. Это расширяет прежнее «только summary ContextPayload».

## 6. Кандидаты и детерминированные проходы

### 6.1. Версии ответа и evidence

Первое внедрение вводит **ReviewOutput v2**: сохраняет координаты
`path/start_line/line` без `side`, внутренне `RIGHT`, и добавляет обязательное
`evidence_refs` к finding. Остальные поля и предел **10 кандидатов** сохраняются;
атрибуция меняется по RULES_FORMAT_SPEC; полный состав и ограничения остальных
полей — SYSTEM_DESIGN §5 (`ReviewFinding`). Старый обязательный текстовый custom-prefix
не является контрактом v2. Версия схемы и prompt закрепляются вместе.

```json
{
  "path": "app/service.py",
  "start_line": 12,
  "line": 15,
  "evidence_refs": [
    {"block_id": "review-code-7", "start_line": 12, "end_line": 15}
  ]
}
```

Пример показывает только новые/координатные поля finding. `evidence_refs` содержит
1–8 ссылок. SHA, source roles, provider IDs и revision модель не генерирует:
backend разрешает их через manifest. Номера однобазовые, границы включены,
каждая строка ссылки должна быть показана. Каждая ссылка указывает на точный
код/сигнатуру, не на candidate, previous_output или модельные конвенции.
Ссылка на сигнатуру доказывает только её видимость, не утверждение о теле.

В последующем **v3** добавляется явный `side` без изменения смысла evidence refs;
для `LEFT` suggestion обязательно `null`. Это требует одновременного изменения
API, хранения, hash/dedup и eval. Старые v1 findings не получают выдуманные evidence
или отметку verified; исторические runs остаются legacy.

### 6.2. Проходы и причины отказа

| Проход | Вход | Проверка | Выход и коды отказа |
| --- | --- | --- | --- |
| 1. Формат | Raw reviewer response, закреплённая schema v2/v3 | JSON, типы, обязательные поля, enum, max 10, диапазоны | Валидный ReviewOutput либо восстановление по §7; `llm_invalid_output` после исчерпания восстановления, не пустой успешный review |
| 2. Координаты | Candidates, доверенный inventory/DiffMap, IgnorePolicy, manifest принятого review-call | Сначала достоверная идентичность (обе стороны rename/copy), затем ignore до проверки существования/стороны/видимости каждой строки; структурный deleted-file и предварительный LEFT-отказ (§9.3) | `inline_eligible` / `general_only` либо drop: `unknown_path`, `ignored_file`, `wrong_side`, `left_anchor_unsupported`, `line_absent`, `cross_hunk_range`, `range_gap`, `anchor_not_visible`, `incomplete_diff`, `unsupported_provider_anchor`; evidence-dependent LEFT-отказ только предварительный до прохода 3 |
| 3. Доказательства | Candidates с допустимыми координатами и отдельно кандидаты с предварительным evidence-dependent LEFT-отказом; refs и manifest review-call | Все refs показаны, provenance/источники разрешены, ревизии совпадают; лишь затем финализировать LEFT-причину (§9.3) | Допущенные candidates; `evidence_not_shown`, `evidence_source_disallowed`, `revision_mismatch` — drop; предварительный LEFT не даёт eligibility, после валидных refs финализируется `left_anchor_unsupported` либо map-derived `wrong_side`/другая координатная причина |
| 4. Политика/дубли | Допущенные candidates, IgnorePolicy, закреплённый lint-filter и dedup keys | Defense guard разрешённых источников для evidence/general, чистый lint, точная дедупликация; порог severity здесь не применяется | Сохранённый набор; drop `lint_pattern`, `duplicate`; guard: изолируемый запрещённый evidence/general → `evidence_source_disallowed`/partial, неизолируемый источник либо нарушенная целостность → failed/`snapshot_integrity_error`; не переименовывает evidence-причины прохода 3 и не заменяет anchor-ignore прохода 2; pre-send guard не является проходом по ответу |
| 5. Verifier | Замороженные candidates после прохода 4, policy/defaults, код и отдельный manifest | Обязательный смысловой batch по §7 | `supported` идёт дальше; `contradicted`, `insufficient_context` сохраняются без публикации; ошибки всего вызова — §8.4 |
| 6. Порог и размещение | Только supported, закреплённый `severity_threshold`, `max_comments` | Порог после verifier (§7), затем сортировка и закреплённый inline cap | Drop `severity_threshold` с отдельным счётчиком; остальные — final findings с disposition inline/general; сверх cap — general, не drop |
| 6b. Межпрогонный дедуп | Supported-кандидаты после Pass 5, реестр опубликованных комментариев PR (`comments` с `github_comment_id`), карта тредов `thread_map`, исходы `resolution_outcomes` | Межпрогонная дедупликация кандидатов внутри `findings_post_processor.py:process()` (Cross-Run Dedup по [`INCREMENTAL_REVIEW_AND_LIMITS_SPEC.md`](INCREMENTAL_REVIEW_AND_LIMITS_SPEC.md)); выполняется строго после восстановления атрибуции `_repair_attribution` и до сортировки и среза cap `Repository.max_comments`; подавленные дубли не сжигают inline cap | Повторные находки сохраняют `drop_reason IS NULL`, в полном объёме учитываются в severity counts, вердикте и check-run (`suppressed_count`), связываются с существующим `Comment` (`linked_comment_id`) и помечаются `publication_action = 'suppressed_duplicate'`; отсеянные проходами 2–6 получают `publication_action = 'dropped'`; не попадают в срез inline cap и не рендерятся в теле ревью; новые получают `publication_action = 'publish_inline'` (или `publish_body`); потребитель — `store_review_output.py` |
| 7. Публикация | FinalReviewResult и immutable publication plan (сформированный после прохода 6b), текущая идентичность VCS | Проверка допустимого адаптера, свежести head/diff; отправка review batch на GitHub (дискуссий на GitLab); чистка тредов publisher после перехода T14 | Новые inline-находки публикуются review batch; чистка устаревших/решённых тредов мутацией `resolveReviewThread` по квоте до 30; явный отказ публикации всего batch по §9–10: `superseded`, `pr_closed`, `publication_payload_too_large`, provider-specific ошибки, `publication_outcome_unknown`; без угадывания координат |

`lint_pattern` и `duplicate` сохраняют действующие имена legacy-кодов;
переименование/миграция причин не вводится. `ignored_file` для якоря выдаёт проход 2
по доказанной inventory/DiffMap идентичности **до видимости** только для масок
ignore и built-in generated: их текст закономерно отсутствует в manifest, это
не anchor_not_visible. Для достоверно идентифицированного удаления под такой
маской ignored_file побеждает структурный left_anchor_unsupported и сам по
себе не снижает coverage. Правило не распространяется на binary-якорь:
он даёт line_absent/partial, хотя доказанный binary сам по себе относится к E. Выдуманный
путь, даже совпавший с маской, не доказывает исключение: unknown_path либо
соответствующая ошибка идентичности/неполноты сохраняется. Проход 4 оставляет
защитный guard для evidence/general, не разрешая запрещённый источник.
Guard прохода 4 при надёжно изолируемом запрещённом источнике конкретного
кандидата даёт drop evidence_source_disallowed/partial без general/verifier
eligibility. Если источник нельзя надёжно изолировать либо нельзя подтвердить
целостность snapshot/manifest и их bindings, весь run завершается failed с
snapshot_integrity_error, без публикации кандидатов. Новый код не вводится.
Проверка источников evidence в проходе 3 даёт `evidence_source_disallowed`,
а не второе `ignored_file`. Pre-send guard выполняется до каждого вызова (§5)
и останавливает отправку с `policy_context_violation`; это не drop по ответу модели.
`llm_invalid_output` — код reviewer, `verification_invalid_output` — verifier (§8.4).

Проход 3 проверяет и атрибуцию `rule_name` к `.review/rules.md` или `AGENTS.md`:
документ должен быть показан review-вызову с содержимым. Если документ отсутствует,
пуст или состоит только из пробельных символов, кандидат получает
`evidence_source_disallowed` и отбрасывается до verifier; `rule_name` не очищается
(RULES_FORMAT_SPEC §8.2). Отсутствующий документ не даёт блока в manifest, пустой
даёт блок без показанных строк, а документ из одних пробельных символов показан
строками, поэтому его отсекает статус snapshot.

Ошибочная форма ссылки (не integer/нет поля) — ошибка схемы всего ответа;
правильно сформированная ссылка на непоказанный блок — drop отдельного кандидата.
Ответ verifier, напротив, целиком невалиден при неизвестных ID/refs: нельзя
принимать удобное подмножество его решений.

`general_only` допустим только при **существующем и целиком показанном** диапазоне
RIGHT того же файла/ревизии, не имеющем inline-якоря (например, L2 за пределами hunk
или диапазон через несколько hunks). Несуществующая строка не превращается
в общий комментарий. Валидные evidence из другого файла не исправляют ложный якорь.
Для general публикации suggestion не выводится как применимая правка; исходный
кандидат с suggestion сохраняется в диагностике, без изменения его текста моделью.
Удаления можно анализировать в L1, но deletion-only finding без реального RIGHT
якоря не выражается v2: причина `left_anchor_unsupported`, coverage partial; не
придумывается новая строка. Сквозная v3 снимает это ограничение.

Точная дедупликация использует path, revision, side, start/end, category и
нормализованный текст утверждения; одинаковый якорь с разными дефектами не считается
дублем. Нормализация ограничена переводами строк и крайними пробелами, не меняет
смысл/код. Семантическое объединение моделью и перенос suggestion запрещены.
Текущий lint-filter версионируется и закрепляется; исключение чистого форматирования
не должно скрывать correctness/security проблему с похожими словами.
Межпрогонная дедупликация (Cross-Run Dedup, проход 6b) между разными Run одного PR выполняется по
спецификации [`INCREMENTAL_REVIEW_AND_LIMITS_SPEC.md`](INCREMENTAL_REVIEW_AND_LIMITS_SPEC.md) (Ч.1):
в отличие от внутрипрогонной проверки, ключ `FindingFingerprint` не зависит от абсолютных
номеров строк и использует `anchor_code_hash`, а сопоставление строится по квитанциям
опубликованных комментариев (`comments` с `github_comment_id`). Проход выполняется строго до
заморозки плана публикации и вычисления `findings_hash`. Подавление повторов является фильтром плана
публикации, а не причиной отсева (drop reason): совпавшие находки сохраняют `drop_reason IS NULL`,
в полном объёме учитываются в severity counts, вердикте PR и статусе Check Run, связываются с ранее
опубликованным комментарием (`linked_comment_id`), не сжигают слоты cap `Repository.max_comments` и исключаются из массива
inline-комментариев отправляемого review batch без создания повторного комментария на VCS.

Для каждого кандидата backend создаёт UUID `candidate_id`, хранит исходный digest,
accepted_call_id и все решения фильтров. ID не вычисляется только по координате.
После фильтров ID и содержимое заморожены. Ограничение количества inline применяется
после verifier: поддержанные находки сверх cap идут в тело ревью, не исчезают
из final findings или severity counts.

## 7. Обязательный LLM-проверяющий

Отдельный verifier выбран для проверки смысла, применимости правила и suggestion:
schema/DiffMap/manifest доказывают только форму, координаты и видимость. Рассмотрены
публикация сразу после детерминированных фильтров (не проверяет смысл), self-check
в том же reviewer response (нет отдельного прохода) и необязательный verifier
только для части findings (одинаковые кандидаты получали бы разные гарантии).
Цена решения — дополнительный LLM-вызов, задержка, cost/token reserve и риск
потерять верный finding из-за ошибочного отказа. Резерв §7.3 и независимый oracle
§11 ограничивают и измеряют эту цену; качество verifier не считается доказанным
самим фактом вызова. Решение также записано в SYSTEM_DESIGN §1.

### 7.1. Профиль и вход

Для непустого набора после §6 выполняется один отдельный batch-проход. Профиль
`verification-v1`: отдельный prompt key `review.verifier`, strict JSON/schema,
`temperature=0`, tools выключены, максимум выхода **8 000 токенов**, только
решения по переданным ID. Согласованная исходная пара совпадает с текущей
конфигурацией pipeline: `mistral-small-4` и fallback `mistral-small-3.2-24b`.
Фактические provider/model IDs, маршруты, параметры и schema capabilities
фиксируются в snapshot; активная настройка между попытками их не подменяет.
При реализации пригодность профиля подтверждается отдельно; этот документ
не заявляет качества модели или успешного live-вызова.

Проверяющему передаются кандидаты, полные документы политики, дефолты, релевантные
конвенции с низким доверием, якорные строки и доказательства. Сначала резервируется
место для всех ID/кандидатов и обязательных документов, затем добавляются фрагменты.
Ему МОЖНО показать дополнительный разрешённый код L2–L4 из того же снимка для
подтверждения или опровержения, но нельзя заменить непоказанные reviewer evidence
задним числом. Manifest verifier фиксирует точный набор. Нехватка дополнительных
данных → `insufficient_context`; невозможность вместить обязательный envelope →
`verification_context_overflow`, `failed`, без пропуска части ID.

### 7.2. Ответ

```json
{
  "decisions": [
    {
      "candidate_id": "7d033785-0bf9-4d63-8db3-31d7a947290e",
      "status": "supported",
      "reason": "Показанный вызов использует вход пользователя без проверки, требуемой правилом.",
      "evidence_refs": [
        {"block_id": "verify-code-3", "start_line": 12, "end_line": 15}
      ],
      "requirement_refs": [
        {"block_id": "verify-policy-rules", "start_line": 8, "end_line": 10, "check_id": null}
      ]
    }
  ]
}
```

Все ключи обязательны, дополнительные запрещены; `decisions` содержит ровно
столько элементов, сколько входных кандидатов, 1–10. `candidate_id` — ровно один
из входных ID, без повторов. `reason` — 1–600 символов, короткое объяснение оценки,
без цепочки рассуждений. `status` — `supported|contradicted|insufficient_context`.

`evidence_refs`: 1–8 для supported/contradicted, 0–8 для insufficient. Ссылки
разрешаются **только** по manifest принятого verifier-call, с теми же проверками
всех строк, источника и revision. Недостаток контекста описывает недостающие данные,
не ссылается на несуществующие фрагменты.

`requirement_refs`: 0–8, формат `{block_id,start_line,end_line,check_id}`. Поля строк
обязательны, указывают на shown policy/defaults; `check_id` непустой только для
service-defaults и должен существовать в закреплённом manifest набора. При
`rule_name != null` для supported/contradicted нужна хотя бы одна ссылка на этот
источник/ID. Хеш документа и origin backend берёт из manifest. Отсутствие основания
для заявленной атрибуции означает insufficient, а не выдуманную ссылку.

Supported означает, что контекст подтверждает утверждение, применимость требования
по иерархии и, при наличии, соответствие suggestion указанному диапазону. Если
suggestion неверен, verifier отклоняет кандидата; не исправляет его скрытно.
Contradicted означает опровержение дефекта или применимости требования. Недостаток
доказательств отличается от опровержения. Техническая валидация refs не доказывает
истинность решения verifier.

### 7.3. Четыре вызова и резерв

Сохраняются максимум **4 фактических generation-вызова на попытку**, **12 на run**,
максимум 3 доменные попытки. Лимит общий для conventions/review/verifier и всех
repair/fallback. Он не является разрешением расходовать все четыре на reviewer.

| Состояние конвенций | Штатные вызовы | Общий резерв восстановления |
| --- | --- | --- |
| Cache miss | Conventions → review → verifier, по одному | 1 вызов для любой стадии |
| Cache hit / нет исходников конвенций | Review → verifier, по одному | 2 вызова в общих пределах восстановления |
| После reviewer нет кандидатов | Только уже состоявшиеся вызовы | Verifier не вызывается; новый поиск не запускается |

До любого дополнительного вызова сохраняется по одному слоту для каждой ещё
не начатой обязательной стадии: reviewer и verifier до завершения конвенций,
verifier после начала review. Резерв verifier освобождается только после
детерминированного получения пустого набора. Невалидный reviewer без свободного
восстановления завершает попытку по исходному классу ошибки; слот verifier
не расходуется на repair reviewer. Неиспользованные слоты не запускают новый поиск.

Например, `C1 → R1 invalid → R2 repair → V1` укладывается в четыре; после
`C1 → C2 repair → R1 → V1 invalid` восстановление verifier уже невозможно.
Это явный failed, а не разрешение на пятый вызов или публикацию без проверки.
На cache hit `R1 → R2 repair → V1 → V2 repair` допустим.

Как построено (#130, решение техлида TL-2 в `docs/plans/130-snapshot-path-plan.md`;
только snapshot-путь, legacy-путь не изменён): задача держит слоты следующих
стадий — `reserved_calls` ревью 1 (verifier), конвенций 2 (ревью и verifier),
verifier 0. Пример с repair выше верен без fallback: при настроенном fallback
последний свободный вызов стадии достаётся ему, и цепочка cache miss —
`C1 → R1 invalid → R2 fallback → V1`; предпочтение repair — follow-up.
Bounded repair передаёт прежний ответ как данные в `<previous_model_output>`, не
длиннее 24 000 символов (с маркером усечения), и ошибки валидатора в
`<validation_errors>`.

Восстановление в пределах оставшихся слотов: невалидный ответ → не более одного
repair той же модели с ошибками схемы и исходным envelope, затем один fallback,
если остался слот. Timeout/5xx → повтор primary либо fallback по действующему
профилю gateway; 429 учитывает Retry-After в пределах дедлайна, 402 сразу fallback,
без повторения того же маршрута. Максимум каждой стадии дополнительно ограничен
общим бюджетом, резервом downstream и политикой PIPELINE_SPEC §5.1; эта таблица
не обещает полную цепочку восстановления каждому вызову.

Когда verifier не может восстановиться, последняя ошибка сохраняет свой класс:
невалидный ответ → `verification_invalid_output`, failed без retry; временная
недоступность → ограниченный retry run с `verification_unavailable`, после третьей
попытки failed. Нельзя превращать terminal invalid output в retryable unavailable
только потому, что закончились слоты. Новая попытка использует тот же snapshot,
общие cost/call limits и уже сохранённые checkpoints.

Нельзя расширять бюджет внутренними повторными генерациями SDK. Transport retry,
который мог начать generation, учитывается консервативно как вызов; ротация ключа
после доказанного отказа авторизации следует существующему контракту gateway.

### 7.4. Стоимость, дедлайн и checkpoint

Лимиты всего run по всем попыткам: fast **$0.50**, deep **$3**. Перед каждым вызовом
считается `spent + upper_bound(next_call) + reserved_remaining_calls`. Верхняя
оценка использует максимальный выход, модель/маршрут, размер будущего envelope и
при необходимости курс валют по существующему gateway. Для ещё неизвестного
набора кандидатов резерв одного обязательного verifier call берётся по максимально
разрешённому envelope, включая 10 кандидатов. На возможный repair заранее
гарантированного денежного резерва нет: перед ним проводится новая admission-проверка.
После формирования набора резерв можно уменьшить. Неизвестная
цена/непригодный курс — явная ошибка существующего ledger, не нулевой резерв.

При недостатке стоимости сначала сокращаются необязательные расширения контекста;
если обязательные оставшиеся стадии не помещаются — `verification_budget_exceeded`.
Непроверенные кандидаты не публикуются и в deep; прежнее «опубликовать, что успели»
разрешено только для уже подтверждённого и сохранённого FinalReviewResult.

Дедлайн fast — 8 мин от claim; лимит одного LLM-вызова — 90 с. До reviewer должно
оставаться время для его вызова, одного verifier call и 10 с локального завершения;
до verifier — для одного call и 10 с. Каждый recovery дополнительно требует своё
окно и сохранение downstream резерва. Если этих окон нет,
`verification_deadline_exceeded` без публикации. Генерация конвенций не расходует
резерв review/verifier. Для будущего deep verifier также использует короткий
профиль 90 с; sandbox обязан передать кандидаты не позднее чем за 100 с
до собственного дедлайна либо выполнить
существующий переход deep → fast с новым дедлайном и сохранением общих лимитов.

Как построено (#130, только snapshot-путь; legacy-путь резерва времени не держит):
вызов ревью оставляет verifier 100 с — его вызов 90 с и 10 с локального завершения;
вызов конвенций оставляет вызов ревью с таймаутом движка и те же 100 с; verifier
оставляет себе 10 с завершения. Денежного резерва verifier пока нет: это follow-up
(TL-6 в `docs/plans/130-snapshot-path-plan.md`). На deep промах кэша конвенций
отклоняется до вызова (#331).

После принятого reviewer response сохраняются кандидаты/manifest checkpoint;
retry при наличии checkpoint продолжает с verifier, не меняет кандидатов.
Успешные verifier decisions и FinalReviewResult сохраняются атомарно до outbox
публикации. Сбой после этого не вызывает повторную смысловую оценку. Crash до
сохранения успешного ответа может потребовать новый вызов в остатке лимитов;
воспроизводимость снимка не означает детерминизм модели.

В коде checkpoint кандидатов — сохранённый manifest каждого вызова snapshot-пути
(`context_manifests`); возобновления с verifier ещё нет. Повторная попытка до
принятого ответа заново выбирает RAG (§3.5): более новая публикация индекса даёт
новый `index_commit_sha` в manifest её вызовов, а то же место остаётся под тем же
`block_id`. Verifier принятой попытки показывает те же блоки RAG с тем же
`index_commit_sha`, что её review (§5.2).

## 8. Итоговый результат, coverage и хранение

### 8.1. Единый результат

```text
FinalReviewResult {
  version: 1, result_id, run_id, policy_snapshot_id, diff_snapshot_id,
  accepted_review_call_id: string|null, accepted_verifier_call_id: string|null,
  findings: [{candidate_id, finding, disposition: inline|general}],
  coverage: {
    status: complete|partial|none,
    scope: allowed_changes, allowed_files, excluded_files,
    allowed_changed_lines, reviewed_changed_lines,
    omitted_changed_lines, reasons[], uncertain_candidates
  },
  verdict: blocking|attention|clean|null,
  severity_counts, severity_threshold, max_comments, below_severity_threshold_count,
  summary: {problem, done_well, effort},
  summary_text, result_digest, publication_plan_id, created_at
}
```

В целевом FinalReviewResult оба accepted-call ключа обязательны и nullable.
Реально выполненный принятый вызов имеет свой настоящий ID; невыполненный —
явный JSON null, ключ не опускается и ID не выдумывается. Backend summary-only,
none и no-text partial имеют оба ID null; ordinary review без кандидатов для
verifier имеет реальный accepted_review_call_id и accepted_verifier_call_id=null.
То же представление используется в review.finalize и во входе digest (§10.1).

`severity_threshold` и `max_comments` копируются из snapshot run и входят в
`result_digest` вместе с findings, coverage и счётчиком отсечённых кандидатов.
Идентичность публикации включает этот digest; `policy_digest` и conventions
cache от этих настроек не зависят (RFS §7.1). `below_severity_threshold_count`
— число supported candidates с `drop_reason=severity_threshold` после прохода 6;
целое ≥0, включая 0 при backend-only результате. Эти записи остаются в diagnostics,
но не входят в final findings/severity_counts/verdict. Summary, review body и
check-run берут число из того же `result_id`; строка выводится только при N > 0.

`complete` означает полноту по разрешённым изменениям и выбранным возможностям
сервиса, не доказательство отсутствия любых дефектов. Числа по исключённым файлам (включая `empty_file`)
не включаются в reviewed. Каждая изменённая строка считается один раз по
`file_id/side/line`; L1 контекстные строки и повторный показ не увеличивают число.
Для `summary_only`, неполного списка VCS или недоступного счётчика число имеет
значение `null`, а не 0. Для no-text partial из §2.2 reviewed_changed_lines
достоверно равно 0; недоступные исходные статистики остаются null. UI явно обозначает «неизвестно».

Для текущего #213 complete также требует полного changed patch в фактическом
входе провайдера после единственного adapter fit_review_context: omitted/prefix-truncated файл даёт
review_input_incomplete/partial и не входит в fully reviewed count. Технический
drop unknown_path даёт partial до формирования итогового summary/verdict; benign
lint/duplicate/severity drops сами по себе этого не делают (§8.3).
Остальные технические drops reflection (`line_absent`, `cross_hunk_range`,
`anchor_not_visible`), `evidence_not_shown` и verifier `insufficient_context`
остаются за lifecycle #107/#130; #213 не реализует их общее понижение coverage.
Следующий абзац задаёт целевой профиль, а не расширяет реализованную границу #213.

Partial устанавливается при summary-only, пропущенном разрешённом diff, усечении,
T (§2.2) при анализе других изменений, техническом drop по
недостоверным координатам/evidence, `insufficient_context`,
unsupported LEFT или недоступном расширении AST/импортов, необходимом для
оценки конкретного кандидата.
Обычное отсутствие AST для языка само по себе не обещает анализа типов и не
считается ошибкой; если конкретное основание недоступно, причина указывается.
Так же RAG (§3.5): отсутствие RAG (тумблер выключен или `rag: not_ready`) само по
себе не делает полноту partial. Причины RAG — `rag_candidate_missing`,
`rag_edge_unconfirmed`, `rag_limit`, `old_revision_unknown` и существующая
`source_read_failed` — записываются в `coverage.reasons`, как `ast_*`, и тоже не делают
её partial сами по себе; исключённые ignore кандидаты учитываются только счётчиком
`excluded`. Правило `insufficient_context` выше не меняется.
Доказанный drop `ignored_file` сам по себе не снижает coverage: исключённый
якорь под маской ignore/built-in generated не становится anchor_not_visible
из-за отсутствия текста в manifest; это правило не охватывает binary-якорь
(line_absent/partial). Любая независимая неполнота файла/кандидата по-прежнему даёт partial; разрешённый якорь
с запрещённым evidence даёт evidence_source_disallowed, а не ignored_file.
Дедупликация, чистый lint-drop, severity threshold и доказанное contradicted сами по себе не делают
полноту partial. Полнота зависит от всего pipeline, не только числа findings.

Окончательная классификация `file_omissions[file_id].reason` сохраняется по §9.2
и проецируется в coverage.reasons. T не считаются проверенными или доказанно
исключёнными, не входят в excluded_files. Binary и доказанный empty_file относятся к E, учитываются
в excluded_files и сами по себе не снижают полноту. При строгом условии none
из §2.2 известное число проверенных строк — 0; неизвестные counts остаются null.
Координатные drops `left_anchor_unsupported` и `wrong_side` оба дают partial;
выбор кода не превращает непроверенного кандидата в clean (§9.3).

### 8.2. Вердикт и публичный текст

| Условие окончательного набора | `verdict` |
| --- | --- |
| Есть supported critical/high после порога | `blocking`, даже если coverage partial |
| Иначе есть supported medium/low после порога | `attention`, даже если coverage partial |
| Только info или пусто, coverage complete | `clean` |
| Только info или пусто, coverage partial/none | `null` |
| skipped/failed/cancelled или ещё не succeeded | API `null` |

Не вводится новый enum вердикта: отдельный `coverage.status` и причины позволяют
UI отличить неполноту от незавершённого run. В списках и detail используется
сохранённый итог; старый пересчёт `empty findings → clean` для v1 результата запрещён.

Backend строит summary детерминированно только из supported findings после порога, scope и
coverage. `problem` — число/категории подтверждённых замечаний либо нейтральное
«Подтверждённых замечаний нет»; `done_well` — факт объёма выполненного анализа,
без неподтверждённой похвалы коду; `effort` = large для critical, medium для high,
small для medium/low, none для info/пусто, при partial не трактуется как готовность
к merge. UI получает `doneWell` через существующий camelCase mapping.
`summary_text` дополнительно называет неполноту и число исключённых изменений;
все T из §2.2 показываются отдельно по причинам и числу файлов,
без исходных неподдерживаемых имён. Безопасное отображение имени доступно только
в диагностике по §9.1. Компактная сводка ограничена 4 000 Unicode-символов.
Этот предел не ограничивает полное тело review: после сводки оно может содержать
все supported general findings. При `below_severity_threshold_count > 0`
summary_text дополнительно содержит строку «ниже порога severity: N» из этого
же result_id. Complete с пустым набором после порога может иметь clean при N > 0:
это отсутствие публикуемых замечаний, а не утверждение об отсутствии отсечённых.

Примеры публичной сводки:

- Полный scope без находок: «Подтверждённых замечаний нет. Проанализированы все
  50 разрешённых изменённых строк; 2 файла исключены политикой».
- Все кандидаты insufficient: «Подтверждённых замечаний нет. Проверка неполная:
  для 2 кандидатов недостаточно контекста». Сами неподтверждённые дефекты не пересказываются.
- Partial с supported high: «Подтверждено 1 замечание высокой серьёзности.
  Проверка неполная: 1 разрешённый файл не вошёл в контекст».
- Неподдерживаемые пути: «Проверка неполная: пропущен 1 файл с неподдерживаемым
  путём; остальные изменения проанализированы». При `none`: «Нет доступных для анализа изменений:
  2 файла с неподдерживаемыми путями, 1 файл исключён политикой».
  Для too_large и остальных T приводятся отдельные счётчики и причины; binary/empty_file — исключения.
  Заголовок none — «AI-ревью: нет доступных для анализа изменений» (PIPELINE_SPEC §7),
  он отличается от failed «AI-ревью не выполнено».

Для блокировок #213 используются полные тексты и квалификаторы §2.2.
Имена и списки bounded; неподдерживаемые имена не выводятся, ignored — counts-only.
Если classification недостоверна, fallback не угадывает разрешённые имена.
Нулевые строки не подменяют отсутствие proof/поддержки. Для coverage none тело и
check-run используют «нет доступных для анализа изменений».

Оригинальное модельное summary хранится только как raw diagnostic output.
Никакой fallback к нему в API, теле ревью или check-run не допускается. Все три
поверхности используют один `result_id` и согласованные counts/text. Check-run
сохраняет PIPELINE_SPEC §7: `neutral` для succeeded (включая clean и partial) и
failed, `skipped` для исключённого scope, `cancelled` для отменённого run.
`failure` не используется; blocking — вердикт ревью, а не conclusion check-run.
`reviewEvent=REQUEST_CHANGES` применяется только при blocking и соответствующей
настройке репозитория; в остальных случаях `COMMENT`.

### 8.3. Хранилище и миграция

#213 расширяет только существующий CHECK `ck_code_change_diffs_omission_reason`
миграцией `20261026_0043_empty_file_omission_reason.py`: допускается `empty_file`,
без новой таблицы/колонки mode. Downgrade сначала переписывает empty_file в
missing_patch, затем возвращает старый CHECK. Marker сохраняет уже проверенное
mode-proof; при reload повторно проверяются status/path/SHA/counters/patch
и отсутствие предыдущего пути (`previous_filename is None`).
Legacy missing_patch нельзя повышать по одному empty SHA: противоречащий size
не хранится в снимке и не должен забываться при retry.

В #213 ограниченная внутренняя coverage хранится в существующих snapshots и JSON
`context.build`/`review.postprocess`, с привязкой к head, raw DiffMap digest и
snapshot digest; списки ограничены 20 безопасными элементами при точных counts.
Точный fully reviewed count опирается на полный доверенный набор changed patches,
а не на первые 20 элементов диагностики: повторный пропуск уже непроверенного
файла не уменьшает count снова. Этот внутренний набор не сериализуется;
диагностические списки остаются bounded, ignored имена не раскрываются.

Внутренний `checked_paths` #170 — пути с сохранённым review_patch != null
(пустая строка тоже входит). Этот набор обозначает доступные текстовые входы,
а не доказывает fully reviewed coverage #213: её точный счётчик берётся из
доверенного покрытия после fit. При partial путь доступного входа может быть
перечислен, хотя fully reviewed count равен 0; coverage остаётся partial, а
при пустых/info findings verdict=null. Supported blocking/attention сохраняются.
Для списка путей overview сначала проверяется канонический поддерживаемый путь.
При classification_unavailable либо наличии ignored exclusions все имена
консервативно скрываются; иначе packer #170 выводит целые пути в бюджете 4 000
Unicode-символов и строку «… и ещё N файл/файла/файлов». Это отдельный бюджет:
ограничение 20 элементов относится к coverage diagnostics, а не к body path list.
Перепаковка через run_url/overflow footer сохраняет backend summary, coverage и
severity threshold; она не возвращает raw model summary и не делает partial clean.

Закреплённый policy_base_sha (= Run.base_sha) не заменяется живым base; фактический diff_old_sha
хранится отдельно и не подменяется policy_base_sha. Контекст и restore используют
одну достоверную durable identity; восстановление — application-решение над
реальными сохранёнными входами, без фиктивных provider locator/meta.
Выбирается новейшая context.build с явной coverage classification; последующие
prompt-only записи (их писали только попытки до #168 ч.6) её не заслоняют, невалидная
новая classification не
возвращает старый clean. Referenced response body принадлежит тому же run.

Итог учитывает точный контекст после ignore и единственного fit_review_context
в GatewayReviewModel перед отправкой провайдеру. Omitted или prefix-truncated
разрешённый changed patch даёт
review_input_incomplete/partial; такой файл исключается из fully reviewed count.
Потеря только вспомогательного контекста не означает пропуск changed patch.
Потеря changed patch на внутренней границе модели учитывается, даже если
внешний контекст не изменился. Эти доверенные факты сохраняются через обе
ветки возврата reflection до финализации и публикации без расширения
LLM ReviewOutput. Технический drop unknown_path
также понижает coverage до partial; lint/duplicate/severity drops сами по себе
этого не делают. Остальные reflection coordinate drops, evidence_not_shown и
verifier insufficient_context остаются за lifecycle #107/#130; их общее влияние
на coverage не объявляется реализованным в #213.

После #130 F1 worker собирает Reflection Pass, а после F2 seed активирует
`review.system:v4` для новых runs. Snapshot-путь выбирается только по
закреплённому v4; runs с v3 или ниже, созданные до F2 seed либо с pins до 0039,
сохраняют legacy-путь без verifier. Адаптер ревью
выполняет fit/guard/manifest один раз: snapshot-ответ `ReviewReply` несёт
фактический fitted context и manifest принятого вызова, legacy — mapping.
В обоих ответах факт потери changed patch передаётся доверенным внутренним
carrier, сохраняется после unwrap и обеих веток Reflection Pass; JSON/schema
ответа модели не расширяются. Остальные технические drops из целевого lifecycle
#107/#130 не объявляются реализованными этим стыком.
Coverage понижается до summary/effort/verdict/body и сохранения
postprocess outcome. Supported blocking/attention сохраняются при partial;
пустые/info findings дают null. Complete допускается только без причин неполноты;
оба читателя сохранённого результата/coverage проверяют этот инвариант.

Старый action без достоверной classification использует unavailable/partial
fallback: counts-only, без имён потенциально ignored файлов. Валидные ignored
классификации также раскрывают только counts. Inline legacy без coverage
получает unavailable/partial, не complete/clean. Прямой legacy-вызов
FindingsPostProcessor.process с coverage=None также получает
classification_unavailable/partial до summary/body, без clean по отсутствию данных.
Повреждённый сохранённый JSON даёт warning с run_id без raw JSON, путей или
секретов; ожидаемое отсутствие записи само по себе не предупреждение. Оба
читателя используют общий валидатор status/count invariants; restore считает
snapshot digest один раз. StoreReviewOutput явно отклоняет отсутствующий backend
summary до записи/commit/очереди, независимо от Python -O.
Для none тело review и check-run
одинаково сообщают «нет доступных для анализа изменений».

В публичных detail/latest-run для всех состояний кроме SUCCEEDED summary=None и
saved_result=None; fallback не синтезируется заранее, latest-run batch читает
результаты только succeeded. Это gate публичного caller, не общего loader:
внутренний PUBLISHING recovery может читать доверенный finalized result для
сохранения исходных event/body/verdict. Существующие nullable поля API не меняются.
Для исторического succeeded без достоверного результата API может показать
fallback/null, тогда как ранее опубликованное GitHub-тело остаётся прежним;
историческая перепубликация не выполняется.

Нового публичного coverage DTO, LLM-полей, result_id или таблиц результата нет.
Полный #107, общий run-owned file_omissions #130/#168 и zero-LLM lifecycle
summary-only/no-text partial остаются целевыми, а не активированными #213.
Zero-LLM lifecycle none активен на snapshot path с #130 F5; legacy (≤ v3) для того же
inventory по-прежнему вызывает модель.
Текущий summary-only может вызывать модель, но все его findings отбрасываются
как insufficient_context: нет inline, blocking/attention или дефектов из них;
coverage partial, verdict null. Обещание zero-LLM относится к будущему lifecycle.


Имена ниже — целевые таблицы/поля для будущих миграций, не существующая схема:

| Данные | Контракт |
| --- | --- |
| `run_policy_snapshots` | По RULES_FORMAT_SPEC; один immutable snapshot на run. Инженер 3 владеет таблицей и всеми её миграциями, включая `prompt_bindings`/`execution_profile`, связанным хранением закреплённых версий run и staging `run_policy_documents`; инженер 4 предоставляет контракт и значения без последующего изменения готового snapshot |
| `run_diff_maps` | `run_id UNIQUE`, идентичности ревизий, provider refs, version, digest, hunks |
| `context_manifests` | Один manifest на `call_id`, FK run/snapshot и digests |
| `context_evidence_blobs` | Workspace-scoped dedup по content hash, точные показанные excerpts |
| `review_candidates` | Исходный finding v2/v3, digest, ID, accepted call, решения фильтров |
| `verification_results` | Принятый call, ровно одно решение на candidate, resolved refs |
| `final_review_results` | `run_id UNIQUE`, FinalReviewResult и digest, immutable после publishing |
| `publication_plans/items` | Сохранённые provider payloads, item keys, remote receipts, состояние доставки (§10) |

Кандидаты и diagnostic drops не смешиваются с опубликованными findings. Поля
existing `findings` сохраняют итоговую проекцию для API; `side=RIGHT` v2,
`line_start=start_line ?? line`, `line_end=line` только для диапазона. Перед
включением v3 side проходит через все storage/API/eval/hash ключи.

Существующие terminal runs помечаются `verification_mode=legacy`, API `coverage=null`
в compatibility-проекции («неизвестно»), без заявлений о выполненном verifier.
Новый FinalReviewResult всегда содержит coverage; четвёртый enum `unknown` не вводится.
Начатые legacy
runs завершаются закреплённым legacy pipeline; новые запускаются целиком новым
профилем. Смешивать старый output с новым verified result нельзя. Активирование
профиля только после миграций хранения и обновления API/UI. Очередь v1 может
по-прежнему передавать run_id: версия поведения берётся из закреплённого run,
а не из текущего process config.

### 8.4. Ошибки

| `error_code` / reason | Исход | Retry |
| --- | --- | --- |
| `policy_*` | По RULES_FORMAT_SPEC; не превращать в пустую политику | Только временное VCS-чтение |
| `policy_context_violation`, `policy_snapshot_integrity_error`, `snapshot_integrity_error` | failed до отправки/публикации; последний код относится к diff/контексту, первые два — к политике | Нет |
| `verification_context_overflow` | failed, обязательный envelope не помещается | Нет |
| `verification_invalid_output` | failed после доступного восстановления в общем бюджете | Нет |
| `verification_unavailable` | queued, затем failed после лимита попыток | Да, существующие 30 с/2 мин; rate limit учитывает Retry-After |
| `verification_budget_exceeded`, `verification_deadline_exceeded` | failed без непроверенной публикации | Нет |
| `all_changes_ignored`, `no_changes` | skipped, verdict null | Не применяется |
| T (§2.2): `unsupported_path`, `identity_unresolved`, `too_large`, `unsupported_change` | none либо partial по §2.2/§8.1, не failed и не ignored | Не применяется |
| `binary` | Доказанное исключение, исход по §2.2 | Не применяется |
| `llm_invalid_output` | failed после восстановления reviewer в общем бюджете (§7) | Нет после исчерпания восстановления |
| `rag_candidate_missing`, `rag_edge_unconfirmed`, `rag_limit`, `old_revision_unknown`, `source_read_failed` (сбой чтения кандидата RAG; у базы `diff_old_sha` — любая её недоступность: сбой чтения дерева или blob, бинарный, слишком большой или не UTF-8 файл, ошибка разбора); `rag: not_ready` | Причины coverage/omission либо trace; сами по себе не partial и не error_code failed (§3.5, §8.1) | Без дополнительных вызовов; ревью продолжается без кандидата/RAG |
| `ast_*`, `insufficient_context` | Coverage reason при недоступном необходимом основании, сами по себе не error_code failed | Без дополнительных вызовов сверх резерва |

Ошибки конкретного provider call сохраняют исходный код/статус в trace; публичное
сообщение не содержит токенов, полного provider response или исключённого кода.

## 9. Карта diff и адаптеры VCS

Распределение реализации: **инженер 3 (createunix)** отвечает за чтение VCS,
его GitHub-адаптер и DiffMap (§§9.1–9.3); **инженер 5** — за адаптеры записи,
VCS Publisher и восстановление публикации (§§9.4–10). Чтение metadata, файлов,
закреплённых blobs и версий diff относится к общей стороне чтения. Publisher
использует её контракт для повторной сверки head/diff; чтение собственных remote
публикаций для восстановления receipts является частью публикационного адаптера.
Первый небольшой PR инженера 3 закрепляет общий контракт карты, якорей и provenance
и реализацию чтения из GitHub. После его слияния разработка продолжается
параллельно по §12; распределение ответственности не меняет интерфейсы ниже.

`DiffMap/v1` — неизменяемая карта конкретной версии diff. Её используют сборщик
контекста, валидатор координат и VCS-адаптер. Номера строк исходных файлов начинаются
с 1; смещения байтов, номера строк текста patch и устаревающая GitHub `position`
не являются координатами finding. Достоверность утверждения и допустимость
inline-якоря проверяются независимо.

### 9.1. Идентичность снимка и файлов

Нормативный состав `DiffMap/v1`:

| Поле | Тип и инвариант |
| --- | --- |
| `schema_version`, `parser_version` | Версии карты и алгоритма разбора; обязательны |
| `diff_snapshot_id`, `digest` | Идентификатор сохранённого снимка и SHA-256 канонического содержимого карты |
| `provider`, `repository_id`, `code_change_number` | Идентичность ресурса VCS; имя репозитория не заменяет стабильный ID |
| `policy_base_sha` | Закреплённая ревизия документов политики; не определяет старую сторону diff |
| `head_sha`, `diff_old_sha` | Новая и фактическая старая ревизии именно этого diff; неизвестная старая ревизия обозначается `null`, а не подменяется `policy_base_sha` |
| `old_revision_provenance` | Метаданные провайдера, по которым установлена старая ревизия, либо причина `unresolved` |
| `provider_diff_ref` | Для GitHub: target SHA, head SHA, merge-base SHA, digest списка файлов/patches. Для GitLab: `version_id`, `base_commit_sha`, `start_commit_sha`, `head_commit_sha`, digest patches |
| `file_list_complete`, `incomplete_reasons` | Полнота пагинации и причины недоступности; отсутствие файла в неполном списке не доказывает отсутствие изменения |
| `files` | Список `DiffFile`, включая записи пропущенных файлов с причинами |

`DiffFile` содержит `file_id` (уникален внутри снимка), `old_path`, `new_path`,
`change_type` (`added`, `modified`, `deleted`, `renamed`, `copied`, `mode_changed`,
`unsupported`), известные `old_blob_sha/new_blob_sha`, исходные поля путей провайдера,
`patch_digest`, `patch_state`, `omission_reason`, `hunks`. Логически отсутствующий
путь равен `null`: `old_path` у добавления, `new_path` у удаления. У записи с
`omission_reason=unsupported_path` отвергнутый канонический путь стороны также
равен `null`; причина и `change_type` отличают это от логического отсутствия.
Исходные API-поля провайдера сохраняются отдельно: GitLab может требовать оба строковых пути даже
для добавления/удаления. `/dev/null` — маркер patch, не путь репозитория.

Необязательный `VcsFile.git_mode` — факт свежего адаптера, используемый только
для proof §9.2; он не создаёт искусственный hunk/строку и не смешивает policy
или итоговую классификацию с VCS digest. Tree path сопоставляется только точно
и регистрозависимо после валидации идентичности, без trim/Unicode-normalization,
percent-decoding или basename matching.

`file_id` вычисляется из идентичности снимка, типа изменения и точной пары путей,
а не из одного имени. В частности, копирование не объединяет источник и копию.
`renamed/copied` требует достоверных метаданных старого пути; их отсутствие не
исправляется сравнением basename или содержимого. В таком случае запись получает
`identity_unresolved`, её содержимое не передаётся модели, поскольку нельзя
надёжно применить ignore к обоим путям.

Пути — точные регистрозависимые UTF-8 пути относительно корня с `/`, не более
1 024 байт UTF-8 и 128 сегментов, по RULES_FORMAT_SPEC. Запрещены
абсолютные пути, NUL, обратный слеш, пустые сегменты, `.` и `..`; неподдерживаемые
кодировки и управляющие символы дают `unsupported_path`. Пробелы и Unicode
не обрезаются и не нормализуются; percent-encoding не декодируется как часть имени.
JSON escaping и Git quoting обрабатывает адаптер ровно один раз. Префиксы `a/` и
`b/` снимаются только при разборе соответствующих заголовков patch, никогда
с произвольного API-пути. URL строится кодированием сегментов, без конкатенации
непроверенных строк. Finding обязан точно совпасть с допустимым путём стороны:
`RIGHT → new_path`, `LEFT → old_path`. Адаптер переводит `file_id` в API-путь;
LLM не выбирает URL, SHA или `provider_diff_ref`.

Нарушение любого из требований к пути даёт `omission_reason=unsupported_path`
для всего `DiffFile`: содержимое, автоматические метаданные с его именами и
inline-якоря недоступны для ревью; `review_patch=null`. Исходные поля провайдера
остаются только в диагностической записи; некорректные байты нельзя заменять
символами или «исправлять» до вымышленного канонического пути. Для идентичности
пропущенной записи используется исходная пара полей без потерь вместе с
идентичностью снимка и типом изменения. Диагностическое представление имени
безопасно экранируется и не используется в URL, командах или как LLM-источник.

Локально изолированная запись не завершает run: остальные файлы проходят обычное
ревью с coverage по §8.1. Невозможность изолировать файл или доказать целостность
снимка остаётся `snapshot_integrity_error`; попытка отправить запрещённый
источник — `policy_context_violation`. Неполнота inventory обрабатывается по §2.3.
Ошибки действующих policy-документов и ignore-политики по RULES_FORMAT_SPEC §9
не превращаются в файловые пропуски.

GitHub показывает PR через three-dot comparison. Старый источник устанавливается
по `merge_base_commit.sha` сравнения закреплённых target/head и увязывается с тем
же сохранённым diff. Политика при этом читается из `policy_base_sha`. Список PR
files должен быть полностью прочитан; метаданные PR сверяются до и после загрузки.
Compare API используется для merge-base, но его ограниченный список files не
заменяет список PR files. Старый контекст доступен только при подтверждённой
идентичности старой ревизии. См. [GitHub: сравнение веток](https://docs.github.com/en/pull-requests/reference/branches#three-dot-and-two-dot-git-diff-comparisons)
и [Compare commits](https://docs.github.com/en/rest/commits/commits#compare-two-commits).

GitLab использует SHA из одной выбранной версии MR: `base_commit_sha` — merge-base,
`start_commit_sha` — target HEAD при создании версии, `head_commit_sha` — source HEAD.
`diff_old_sha = base_commit_sha`; `start_commit_sha` не используется вместо него
для загрузки старого содержимого. Незаполненные refs/ещё не собранная версия требуют
повторного чтения в пределах сетевого бюджета, а не выведения SHA из имён веток.
См. [GitLab diff versions](https://docs.gitlab.com/api/merge_requests/#retrieve-merge-request-diff-versions).

### 9.2. Hunks и проход по строкам

`DiffHunk` содержит `hunk_id`, `old_start`, `old_count`, `new_start`, `new_count`,
`complete`, упорядоченные `rows`. У `DiffRow`: `row_id`, `kind`
(`context`, `added`, `removed`), `old_line: int|null`, `new_line: int|null`,
`text`, `old_cursor`, `new_cursor`. Курсоры нужны адаптеру GitLab; они не
представляют существующую строку на отсутствующей стороне. При хранении большого
текста допустима ссылка на неизменяемый patch вместо копии `text`.

Для заголовка `@@ -A[,B] +C[,D] @@` значения отсутствующих B/D равны 1;
явный 0 означает пустой диапазон. Курсоры начинаются с A/C. До обработки каждой
строки сохраняется пара курсоров:

| Префикс | `kind` | `old_line` / `new_line` | Изменение курсоров |
| --- | --- | --- | --- |
| пробел | `context` | оба текущих номера | оба +1 |
| `-` | `removed` | old / `null` | old +1 |
| `+` | `added` | `null` / new | new +1 |
| `\ No newline at end of file` | метаданные, не `DiffRow` | нет | нет |

Содержимое делится по LF, а не по произвольным Unicode-разделителям. Удаление
CR допустимо лишь как часть явно распознанного CRLF framing; одиночный CR в исходнике
не теряется. Для каждой стороны количество потреблённых строк обязано совпасть
с count. Перерасход, недостача, неизвестный префикс, конфликтующие/перекрывающиеся
hunks, невозможная нулевая координата существующей строки или combined diff
`@@@` дают `malformed_patch`/`unsupported_patch`.

`patch_state` различает `complete`, `missing`, `truncated`, `malformed`, `binary`,
`unsupported` и `metadata_only`. Нет patch — нет искусственного hunk. Несовпадение
числа добавлений/удалений с достоверной статистикой, флаг провайдера `too_large`,
незавершённая пагинация и потерянная часть hunk отражаются в диагностике.
`collapsed` допускает получение patch этой же версии, если это поддерживается
провайдером; `too_large` не считается автоматически восстановимым.
**Окончательная классификация файла run (цель #107, редакция #175).**
Единое поле причины — `file_omissions[file_id].reason` в сохранённом снимке run;
запись также содержит `class=excluded|technical|incomplete`. Она immutable после
сборки и вычисляется по порядку §2.2. Общий run-owned projection остаётся целью #130/#168; ограниченная внутренняя
проекция #213 использует существующие snapshots/actions и валидированный marker
`empty_file` (§8.3), не выдаёт себя за эту будущую модель.
`DiffFile.omission_reason` остаётся фактом VCS (`unsupported_path` или
`identity_unresolved`), а patch_state и исходные flags не смешиваются с ignore.
Изменение политики не меняет VCS DiffMap/digest. Coverage/UI и ветка none читают
окончательную классификацию run, а не независимо трактуют flags.

| Причина классификации | Критерий и класс |
| --- | --- |
| `unsupported_path`, `identity_unresolved` | Недопустимый путь или недоказанная идентичность rename/copy; technical, до масок |
| `ignored_file`, `generated` | Доказанное совпадение маски/built-in generated на достоверных путях; excluded, прежде binary/too_large |
| `binary` | Достоверный явный факт адаптера, NUL в доступном patch либо bounded blob evidence: NUL в первых 8 KiB или magic-префикс PNG/JPEG/GIF/PDF/ZIP/ELF (`is_binary_content`); excluded. Отсутствие patch и расширение файла не доказательство |
| `empty_file` | Полный proof добавленного пустого обычного файла ниже; excluded. Отсутствие patch/empty SHA по отдельности не доказательство |
| `unsupported_change` | Достоверный gitlink/submodule либо доказанный чистый rename/mode-only без текстовых изменений и без поддерживаемого анализа; technical, если ранее не доказано исключение. Один raw `patch_state=metadata_only` не доказывает этот класс |
| `too_large` | >20 000 additions+deletions, >20 000 LF-строк patch (включая context/заголовки), >1 048 576 UTF-8 байт patch или >1 048 576 сырых байт blob / достоверного blob size; либо явный provider too_large / остановка чтения на лимите; technical |
| `missing_patch` / `truncated_patch` / `malformed_patch` | Нет полного достоверного patch обычного файла и нет доказательства предыдущих причин; incomplete, не разрешает none |

`empty_file` — доказанное исключение E только для `added` с поддерживаемым
каноническим точным путём, без предыдущего пути (`previous_filename is None`, логический `old_path=null`),
исходным patch=None/пустой строкой (при reload также точная сохранённая пустая
обёртка `diff --git` / `---` / `+++` того же пути без иных строк), additions=deletions=changes=0,
Git empty blob SHA `e69de29bb2d1d6434b8b29ae775ad8c2e48c5391`, без противоречащего
размера и с независимым подтверждением обычного файла: type=`blob`, mode=`100644`
либо `100755`. Один SHA не исключает symlink/gitlink. GitHub-адаптер делает не более
одного необязательного recursive tree-запроса на fetch PR при наличии кандидатов,
строго на закреплённый `head_sha`: полный nontruncated ответ и уникальное точное
совпадение path+SHA, size отсутствует либо 0. Нет запроса без кандидатов; уже
исключённым generated не нужно это доказательство. Ответ с identity encoding
читается потоково с пределом **1 500 000 байт** (`MAX_EMPTY_FILE_TREE_BYTES`),
не 1.5 MiB; для доказанных кандидатов дополнительных blob-байт нет. Ошибка,
превышение предела, truncation, duplicate path, неверные path/SHA/type/mode,
symlink/gitlink или конфликт размера не доказывают E: `missing_patch`/incomplete,
без failed/ignored. Отмена и дедлайн продолжают прерывать попытку. Ветка/base tree
не заменяют head; обычный bounded blob fallback не доказывает регулярный mode.

`__init__.py`/`.gitkeep` не получают исключение по имени. Без полного proof они
остаются missing_patch/incomplete. Raw `patch_state=metadata_only` при changes=0
только диагностика, не proof empty_file или чистого rename/mode-only/T.
Validated empty_file хранится в текущем omission_reason после миграции §8.3;
reload проверяет durable поля, включая `previous_filename is None`, не повышает
legacy missing_patch по SHA. Целевой
`file_omissions` остаётся отдельной будущей run-классификацией. Решения и
пользовательские причины для rename/mode-only/deleted/oversized binary — §2.2;
для них proof не расширяется и новых запросов нет.

Blob сверх лимита чтения/известного size становится `too_large` до blob-based
binary-проверки, если binary не доказан независимо уже доступными данными.
GitHub для removed не даёт нового blob (`github_vcs.py`); удалённая картинка без
независимого binary evidence — `missing_patch`, incomplete. Base-blob fetch и
классификация по расширению не предусмотрены.

Ровно 20 000 строк или 1 048 576 байт по соответствующему пределу допускаются;
число строк patch включает его заголовки и context по LF. Флаг провайдера с его
исходным именем и известные counters/bytes сохраняются в диагностике.
`source_too_large` — другой лимит: blob >256 KiB для L2–L4 (§3.4). Он отменяет
расширение контекста, но оставляет допустимый L1, не классифицирует всё изменение
как too_large и не вычитает его строки из порога. Недоступный необходимый контекст
может дать insufficient_context/partial по §8.1.

Для неполного файла v1 не строит inline-якоря; независимые доказательства могут
позволить проверенному замечанию попасть в общий текст. Бинарные файлы, submodules
и изменение только режима не получают вымышленных номеров строк. Дополнительные
фрагменты полного файла не расширяют карту допустимых inline-мест.

### 9.3. Якорь finding, диапазоны и suggestion

Первое внедрение сохраняет LLM-контракт `path`, `start_line`, `line` без `side`;
внутренняя сторона — `RIGHT`. Карта хранит обе стороны уже сейчас. `LEFT` включается
отдельной версией схемы и сквозной миграцией API, хеша, дедупликации, хранения,
восстановления, публикации и eval. Удаления доступны для анализа и доказательств,
но до этой миграции не дают inline на выдуманной строке RIGHT.

Целевой `Anchor/v1` (обе стороны в карте): `file_id`, `side`, `start_line|null`, `line`, `hunk_id`.
`start_line=null` означает одну строку; иначе `1 ≤ start_line < line`.
Принимается один файл, одна сторона и один полный hunk. Для каждого номера
включительного диапазона требуется реальная строка этой стороны; проверка
только концов недостаточна. Сначала разрешается достоверная inventory/DiffMap
идентичность; ignore на ней проверяется до видимости и даёт ignored_file (§6.2).
Неизвестный путь не становится доказанным excluded по совпадению с маской.
Для дальнейшего допуска все строки якоря должны быть разрешены политикой
и присутствовать в manifest принятого ответа; ссылки доказательств отдельно
проверяются по manifest своего модельного вызова. Представление контекстной
строки в patch может содержать оба номера, но источник старого полного файла
не возникает из этого автоматически.

Неверный диапазон нельзя свести к последней строке, переместить к ближайшей строке
или перенести на другую сторону. Неизвестный путь — не разрешение на произвольный
общий комментарий. Кандидат без допустимого inline-якоря может пройти обязательную
смысловую проверку для общего текста только при валидных разрешённых доказательствах.
Дополнительные ограничения `general_only` заданы в §6.2. Причины сохраняются: `unknown_path`, `wrong_side`, `line_absent`, `cross_hunk_range`,
`range_gap`, `anchor_not_visible`, `ignored_file`, `incomplete_diff`,
`unsupported_provider_anchor`, `left_anchor_unsupported`.
В v2 модель не заявляет сторону, поэтому коды определяются наблюдаемыми данными:

- `left_anchor_unsupported`: достоверная структурная идентичность удалённого файла
  позволяет выбрать причину в проходе 2. Иначе, если реального RIGHT-якоря нет,
  проход 2 сохраняет только предварительный координатный отказ. В проходе 3
  refs должны пройти manifest, provenance, source-policy и revision validation;
  лишь после этого подтверждённые evidence удалённых строк того же файла могут
  финализировать left_anchor_unsupported. Непроверенные refs не доказывают удаление.
- `wrong_side`: при отсутствии предыдущего условия запрошенная координата
  существует в DiffMap только на LEFT; в v3 также заявленный side не совпадает
  с реальной стороной строки. Если строки нет ни на одной стороне — line_absent;
  если нет пути — unknown_path.

При невалидных/непоказанных refs окончательный отказ — существующая evidence-причина
прохода 3, а не доказательство LEFT. Если refs валидны, но не подтверждают удаление
без RIGHT-якоря, остаётся map-derived wrong_side либо другая применимая координатная
причина. Отложенный отказ не даёт inline/general eligibility и не допускает
кандидата к verifier; это не ремонт координат. При совпадении условий с валидным
подтверждением удаления побеждает `left_anchor_unsupported`. Оба кода дают partial
как технический drop по координатам (§8.1). Это целевой профиль: main пока
возвращает wrong_side для удалённого файла (diff_anchor.resolve_anchor);
он не считается доказательством корректного применения v2-правила #107.

`suggestion` — готовая замена ровно исходного RIGHT-диапазона, включая обе границы,
а не unified diff. Пустая строка означает удаление диапазона и отличается от `null`.
Для LEFT обязательно `null`. Для общего текста нет применяемой suggestion:
проверенную замену можно показать только как обычный код с явным указанием исходной
ревизии и диапазона. При утрате якоря сервис не сохраняет применяемый блок для
другого диапазона. Возможность корректно представить замену, включая fenced code
внутри неё и переводы строк, проверяется адаптером; если это невозможно, публикуется
подтверждённое замечание без применяемого блока с причиной `suggestion_unrenderable`.
Write-back публикует ревью/комментарии; автоматическое применение исправлений,
push, merge и запуск команд в его полномочия не входят.

### 9.4. GitHub

Основной endpoint — `POST /repos/{owner}/{repo}/pulls/{pull_number}/reviews`:
один сохранённый review body, event и массив comments. `commit_id` всегда равен
закреплённому `head_sha`. Путь берётся из поля `filename` провайдера (новое имя
для rename; сохранённое имя удалённого файла для удаления), а не напрямую
из side-specific пути модели. У каждого comments-элемента — `path`, `body`, `line`,
`side`; диапазон дополнительно содержит `start_line` и `start_side=side`.
Одиночная строка не содержит start-полей. `position` не используется.
Контекстные строки GitHub адресуются RIGHT; будущий LEFT-адаптер адресует удаления.
LEFT-контекст остаётся доказательством, но не переносится молча на RIGHT.
См. [Review comments](https://docs.github.com/en/rest/pulls/comments#create-a-review-comment-for-a-pull-request)
и [Create review](https://docs.github.com/en/rest/pulls/reviews#create-a-review-for-a-pull-request).

Для следующих примеров файл называется `a`, пути не менялись, patch равен:

```diff
@@ -10,3 +10,4 @@
 before()
-old_value()
+new_value()
+validate()
 after()
```

Строки: context old10/new10; removed old11; added new11; added new12;
context old12/new13. SHA ниже — условные идентификаторы одного снимка, а не
существующего PR/MR. Примеры показывают shape запроса; marker добавляет backend.

```json
{
  "commit_id": "3333333333333333333333333333333333333333",
  "event": "COMMENT",
  "body": "Проверены доступные изменения; найден подтверждённый дефект.",
  "comments": [
    {
      "path": "a",
      "start_line": 11,
      "start_side": "RIGHT",
      "line": 12,
      "side": "RIGHT",
      "body": "**Валидация выполняется после изменения**\n\nПеренесите проверку перед изменением.\n\n```suggestion\nvalidate()\nnew_value()\n```"
    }
  ]
}
```

После отдельного включения LEFT удаление old11 представляется comment
`{"path":"a","line":11,"side":"LEFT","body":"…"}` без start-полей
и без suggestion. Поле `side` не подставляется как RIGHT для всего массива.

### 9.5. GitLab: целевой контракт, отдельная реализация

На `main` (#97) реализована публикация MR discussions по более узкому профилю SD §8.4
(маркеры `findings_hash`, `new_line` на новой стороне; без `line_code`/`line_range`/`old_line`
из целевого §9.5); приём вебхуков, diff ingress и LEFT — вне scope #97. Ниже — целевой
профиль координат и payload для расширения адаптера и тестов.

Endpoint — `POST /projects/{id}/merge_requests/{iid}/discussions`.
В payload обязательны `body` и для inline `position`: `position_type="text"`,
оба исходных пути, `base_sha/start_sha/head_sha` одной закреплённой версии.
Добавление использует только `new_line`, удаление только `old_line`, context — оба
действительных номера. `line_range` задаёт `start/end`, каждый с `line_code`, `type`
и существующими номерами соответствующей строки; `type="new"` для added,
`type="old"` для остальных. Верхнеуровневые `old_line/new_line` у position
адресуют конец диапазона. См. [Discussions API](https://docs.gitlab.com/api/discussions/#create-a-thread-in-the-merge-request-diff).

Для `line_code` адаптер хранит отдельную вычисленную строку:
`SHA1(UTF8(provider_file_path)) + "_" + old_cursor + "_" + new_cursor`.
`provider_file_path` — непустой исходный `new_path`, иначе `old_path`.
Здесь используются **оба курсора до инкремента**, не nullable `old_line/new_line`;
подстановка 0 для добавленной/удалённой стороны неверна. Поэтому для added new11
из примера выше курсоры равны 12/11, для added new12 — 12/12. Эти детали сверены
с официальными [GitLab Parser](https://gitlab.com/gitlab-org/gitlab/-/blob/master/lib/gitlab/diff/parser.rb),
[Line](https://gitlab.com/gitlab-org/gitlab/-/blob/master/lib/gitlab/diff/line.rb),
[File](https://gitlab.com/gitlab-org/gitlab/-/blob/master/lib/gitlab/diff/file.rb)
и [diff_line_code](https://gitlab.com/gitlab-org/gitlab/-/blob/master/lib/gitlab/git.rb)
на 2026-10-09; реализация адаптера должна закрепить совместимый профиль GitLab.
Курсор равен 0 только когда это следует из реального заголовка пустой стороны.

Для пути `a` SHA1 равен `86f7e437faa5a7fce15d1ddcb9eaeaea377667b8`.
Пример диапазона новых строк 11–12 из того же patch:

```json
{
  "body": "**Валидация выполняется после изменения**\n\nПеренесите проверку перед изменением.",
  "position": {
    "position_type": "text",
    "base_sha": "1111111111111111111111111111111111111111",
    "start_sha": "2222222222222222222222222222222222222222",
    "head_sha": "3333333333333333333333333333333333333333",
    "old_path": "a",
    "new_path": "a",
    "new_line": 12,
    "line_range": {
      "start": {
        "line_code": "86f7e437faa5a7fce15d1ddcb9eaeaea377667b8_12_11",
        "type": "new",
        "new_line": 11
      },
      "end": {
        "line_code": "86f7e437faa5a7fce15d1ddcb9eaeaea377667b8_12_12",
        "type": "new",
        "new_line": 12
      }
    }
  }
}
```

Пример отдельной удалённой строки old11 (без suggestion и `line_range`):

```json
{
  "body": "Удаление нарушает показанный контракт вызывающего кода.",
  "position": {
    "position_type": "text",
    "base_sha": "1111111111111111111111111111111111111111",
    "start_sha": "2222222222222222222222222222222222222222",
    "head_sha": "3333333333333333333333333333333333333333",
    "old_path": "a",
    "new_path": "a",
    "old_line": 11
  }
}
```

Этот пример — контракт LEFT/v3, не разрешение публиковать LEFT из v2.
Одиночный context old12/new13 использует ту же position без `line_range`,
с `old_line=12,new_line=13`; одиночное удаление old11 — только `old_line=11`.
При rename оба пути остаются своими, SHA1 для line_code использует новый API-путь;
при delete сохраняются исходные строковые API-пути, даже если нормализованный
`new_path=null`. Ни `policy_base_sha`, ни фиктивное одинаковое значение old/new
не заменяют правильные метаданные.

GitLab suggestion сериализуется отдельно от line_range: якорь на конечной новой
строке `E`, целевая замена `[S,E]` — блок `suggestion:-(E-S)+0`. Для `[11,12]`
это `suggestion:-1+0`. В первом GitLab-профиле допускается максимум 100 строк
выше конечного якоря; более длинный диапазон сохраняется как комментарий без
применяемой замены. Это консервативный лимит нашего адаптера; реальные ограничения
и синтаксис приведены в [GitLab suggestions](https://docs.gitlab.com/user/project/merge_requests/reviews/suggestions/#multi-line-suggestions).
Сервис не вызывает API применения suggestion.

## 10. Write-back: неизменяемая публикация и восстановление

### 10.1. Payload и идентичность

Во всех формулах используется canonical JSON `policy-json/v1` из RULES_FORMAT_SPEC.
`final_result_digest` покрывает версию результата, run/snapshot bindings, принятые
call IDs (оба обязательных ключа, явный null для невыполненного вызова),
окончательные findings с disposition, coverage, verdict, severity_counts,
severity_threshold, max_comments, below_severity_threshold_count, объект summary
и отдельное поле summary_text. Непрозрачный immutable result_id — идентификатор
хранения, исключённый из digest. Сам digest, служебное время,
`publication_plan_id` и delivery receipts также в его вход не входят:
циклическое хеширование исключено. `disposition` результата задаёт исходное
планируемое размещение. При допустимом координатном fallback фактическое размещение
читается из сохранённого варианта publication plan; API/VCS/check-run используют
один выбранный вариант и те же final findings, counts и summary.

Перед `publishing` сохраняются окончательный результат проверки, итоговая сводка,
решения по всем кандидатам, версии renderer/адаптера, snapshot/digests и точный
канонический payload каждого VCS-запроса. Изменение сортировки, side, summary,
event, inline/body размещения или suggestion меняет payload digest. Retry не
перезапускает проверяющего и не восстанавливает body из текущей версии шаблона.

Перед первым POST выбранного варианта publisher проверяет все его готовые payload,
включая служебные markers, по лимитам закреплённого профиля VCS-адаптера. Профиль
явно задаёт предел и единицу измерения для каждого ограничиваемого поля/запроса;
его версия сохраняется в publication plan. Это относится и к сохранённому
body-only варианту при fallback. Превышение даёт `failed/publication_payload_too_large`
до отправки этого варианта. Нельзя молча усекать текст, терять supported findings
или дробить сохранённый план. Исключение `suggestion_unrenderable` из §9.3 позволяет
при подготовке убрать только применяемый блок, сохранив подтверждённое замечание.

Ключ публикации:

```text
publication_key = SHA256(canonical_json({
  schema: "publication/v2", provider, repository_id, code_change_number,
  run_id, head_sha, diff_snapshot_id, final_result_digest
}))
operation_key = SHA256(canonical_json({publication_key, ordinal, payload_digest}))
```

`payload_digest` охватывает все значимые отправляемые поля, включая body/event,
координаты и SHA, но исключает только генерируемый служебный marker, чтобы избежать
рекурсивного хеша. Ключ операции и payload связаны уникальным ограничением хранения.
Формат backend-маркера в body (цель #107; на `main` GitHub/GitLab публикация #97 использует
legacy `findings_hash` markers, см. SD §8.4 и PIPELINE_SPEC §5.2):

```text
<!-- ai-review:v2 run=<uuid> head=<full-sha> op=<operation-key> payload=<sha256> -->
```

Сервис добавляет marker после безопасного rendering пользовательского текста;
вход модели не может создать дополнительный служебный marker. Совпадение признаётся
только у объекта ожидаемого bot/app, внутри нужного PR/MR, с совпадающими head,
ключом и точным payload (после оговорённого удаления служебного marker).
Строка marker сама по себе не является доказательством успеха. Пагинация reviews,
comments/discussions обязательна; ошибка чтения или лимит обхода означает
`lookup_incomplete`, а не «публикации нет».

Новый run, включая rerun того же head и пустые findings, получает новый ключ.
Повтор доставки одного run восстанавливает его собственную операцию. `findings_hash`
может остаться диагностическим полем, но не определяет идентичность публикации.

### 10.2. Выполнение и гонки

1. Короткая транзакция захватывает lease публикации и фиксирует подготовленные
   операции. Перед каждым новым POST проверяются run/cancel, открытость PR/MR,
   текущий head и идентичность diff у VCS; затем выполняется поиск уже отправленной
   операции. Все HTTP-вызовы — вне DB-транзакции.
2. При смене head или версии diff новый POST запрещён, run получает
   `cancelled/superseded`. Пересчёт координат для нового head не является retry.
3. После подтверждённого ответа сохраняются review/discussion/note IDs и точное
   соответствие finding → comment по сохранённым marker/координатам и body.
   Порядок и равное число элементов в ответе недостаточны для восстановления.
4. После POST повторно читается актуальность VCS и в короткой транзакции
   завершается run. Push между GET и POST возможен: удалённая публикация остаётся
   привязана к исходному head, её IDs сохраняются, а run отмечается cancelled,
   если устарел или отменён. Новый head не объявляется проверенным. Автоматическое
   удаление опубликованного комментария для сокрытия гонки не выполняется.
5. Разрешение устаревших и исправленных замечаний (VCS Write-back). В соответствии
   со спецификацией [`INCREMENTAL_REVIEW_AND_LIMITS_SPEC.md`](INCREMENTAL_REVIEW_AND_LIMITS_SPEC.md)
   (Ч.1), после подтверждения успешности публикации review batch на GitHub, атомарного перехода
   T14 в `succeeded` (вместе с сохранением квитанций comments в единой транзакции) и обновления
   Check Run в статус `completed` (`neutral` / `success`) publisher выполняет
   фазу разрешения тредов (VCS Thread Clean-up) как best-effort шаг. Чистка запускается ТОЛЬКО если
   `run.status == 'succeeded'`; если публикация review batch не состоялась или T14 завершился ошибкой,
   фаза чистки не запускается. Набор состояний Run (Р-14 / D5) не изменяется.
   Замечания, дефекты которых были устранены автором в новой ревизии (при подтверждённом
   полном анализе участка и отсутствии любых кандидатов с `drop_reason IS NOT NULL`, кроме `contradicted`)
   или файлы которых были удалены, переводятся в статус `RESOLVED_BY_BOT` с вызовом GraphQL-мутации
   `resolveReviewThread`. Эта операция изменяет состояние жизненного цикла треда (`isResolved`), не изменяя
   исходный текст комментария (`body`), payload digest и delivery receipts, сохраняя гарантии неизменяемости публикации.
   Кандидаты на закрытие сохраняются в метаданных публикации (`run_review_pointers`) вне `findings_hash`.
   Вызовы GraphQL выполняются строго вне транзакций БД со своим таймаутом и проверкой актуальности PR
   (`provider_origin_active`, DB checkpoint и `_ensure_current_head`); мутации квотируются (до 30 на run)
   с соблюдением вторичных лимитов GitHub (интервал ≥ 1.2 с, общий бюджет фазы чистки 90 с); остаток свыше 30
   переходит на обработку в последующие Run.
   При поступлении нового коммита во время фазы чистки мутации немедленно прекращаются, Run сохраняет статус
   `succeeded`, а оставшиеся треды переносятся на следующий Run.
   При ошибках GraphQL или нехватке прав треды остаются открытыми, инкрементируется метрика
   `review_thread_cleanup_forbidden_total`, Run не прерывается и сохраняет статус `succeeded`.

Lease/уникальный ключ предотвращают обычную конкуренцию workers, но не дают
атомарности с HTTP. После истечения lease с незавершённым запросом новый worker
сначала восстанавливает его исход; одновременно выдавать новый POST нельзя.
GitHub v1 сохраняет один review batch на run. GitLab требует нескольких операций:
стабильный ordinal на finding, затем общая сводка; частично опубликованные
операции сохраняются по отдельности. При устаревании MR оставшиеся POST прекращаются;
уже созданные discussion IDs не теряются и не публикуются заново.

### 10.3. Ошибки, timeout и fallback

| Исход | Действие |
| --- | --- |
| Готовый payload превышает лимит закреплённого профиля адаптера | `failed/publication_payload_too_large` до первого POST выбранного варианта; без усечения findings или пересборки плана |
| Подтверждённый успех POST, последующее чтение IDs или запись БД не удалась | Повторить восстановление по своему marker; не создавать новое ревью |
| Timeout/reset после возможной отправки, неоднозначный 5xx | `outcome_unknown`; искать операцию по marker и payload с полной пагинацией, не делать слепой POST |
| Запрос достоверно не отправлен | Допустим ограниченный повтор того же payload после проверки актуальности |
| 429 или документированная rate-limit ошибка, включая соответствующий 403 | Соблюсти `Retry-After`/rate-limit metadata и общий дедлайн; не применять координатный fallback |
| 401 | Одно обновление токена и повтор при подтверждённом отказе, затем access failure |
| 403 без rate-limit признаков, 404 доступа/ресурса | Access/resource failure; не объявлять старый commit без соответствующего доказательства |
| Явная ошибка недопустимых path/line/side/range/position при неизменном diff | `coordinates_rejected`; один сохранённый вариант body-only после подтверждённого отказа исходной операции |
| Недопустимый commit/diff refs либо текущий head изменился | Сверить VCS; при подтверждении устаревания `cancelled/superseded`, без body-only обхода |
| 400/422 по body/event/schema, spam/abuse, неизвестная validation error | Невосстановимая ошибка payload либо отдельная rate-limit классификация; не считать её ошибкой координат |

GitHub документирует 422 и для validation, и для spam; одного HTTP-кода недостаточно
для классификации. Классификатор использует endpoint, структурированные error fields,
коды и сохранённые ограничения, а не только поиск слова `commit` в свободном тексте.
Неизвестная ошибка не должна расширять полномочия публикации.

Внутрипроцессный бюджет повторов сохраняет 2, 8 и 30 секунд. `Retry-After ≤60 s`
может заменить паузу, только если укладывается в общий дедлайн; большее значение
планируется отложенно, без раннего POST. Эти паузы применяются к безопасному повтору
или чтению состояния, но не разрешают дублировать запрос с неизвестным исходом.
После неоднозначной отправки отсутствие marker в одном или нескольких чтениях
не доказывает, что сервер не принял запрос. Если исход нельзя восстановить в
бюджете, фиксируется `failed/publication_outcome_unknown`; журнал допускает
последующее read-only reconciliation, но автоматическая повторная отправка
блокируется. Без серверного idempotency primitive нельзя обещать exactly-once.

Координатный fallback — заранее подготовленный и сохранённый вариант представления
тех же `supported` findings в общем тексте. У него собственные payload digest/key;
связь с исходной операцией хранится. Переход допускается лишь после явного
неуспешного ответа, без сомнений о возможном side effect, и при актуальном снимке.
Неизвестный исход, смена head, ошибка verifier или запрет ignore не допускают fallback.
Общая сводка не меняет выводы; меняется только размещение подтверждённых замечаний.
Применяемые suggestions при fallback убираются, а координаты сохраняются в тексте
как ссылки на исходный снимок. Для GitLab fallback уже подтверждённого отдельного
finding не повторяет остальные опубликованные discussion.

### 10.4. Совместимость с существующими публикациями

Текущий marker `<!-- ai-review findings_hash=… -->` не содержит run/head/payload.
Новые runs не используют его для дедупликации. Старые завершённые runs продолжают
показывать сохранённые remote IDs; им не приписывается новый verifier verdict.
Для старого незавершённого `publishing` сначала восстанавливается исходный
сохранённый payload, затем проверяются связанные remote IDs. При наличии только
legacy-marker нужны одновременно ожидаемый bot, commit SHA, полный body/event,
полный набор координат/текстов комментариев и однозначная принадлежность run.
Совпадения findings_hash недостаточно. Неоднозначность даёт
`legacy_publication_unresolved`, без повторной публикации и без присвоения чужого
review. Подтверждённый legacy-object связывается с run во внутреннем журнале;
менять его публичный body ради нового marker не требуется.

### 10.5. Изменения текущей реализации

- `vcs_diff.parse_file_patch` проверяет счётчики hunks, но `DiffLine` хранит один
  номер и теряет старый номер context, hunk identity и связь с ревизиями/rename.
  `VcsFile.previous_filename` уже доступен; при преобразовании `ChangedFile`
  происхождение теряется. `HttpGitHubVcsProvider` уже пагинирует files, сверяет
  количество и повторно читает PR; это следует сохранить.
- `FindingsPostProcessor` на publish path проверяет якоря через DiffMap +
  `resolve_anchor` (не `parse_unified_diff` / `hunk_lines`); при сведении диапазона
  к одной строке сбрасывает `start_line` и применяемый `suggestion`. Manifest
  фактического ввода и LEFT — вне #171.
- `github_review_publication` жёстко выставляет RIGHT, не отправляет `start_side`,
  трактует почти любой 422 как coordinates и ищет marker только на первой странице.
  Проверка актуального head перед POST уже есть; её нужно расширить до версии diff
  и дополнять восстановлением неоднозначного исхода. В рамках #171 восстановление
  размещения при redelivery: свежий POST считается атомарным (201 — все inline
  этой отправки опубликованы, тела и координаты не сверяются), а для review,
  найденного по marker, inline восстанавливаются сопоставлением тела и координат
  (`path`/`line`/`start_line`, каждый комментарий используется один раз).
- `PublishRunReview` после coordinates rejection один раз переносит unposted (GitLab)
  или все inline (GitHub) в body, сохраняя `findings_hash`; второй coordinates →
  `*_publish_failed` (#171). Новый журнал должен хранить отдельные неизменяемые
  варианты. `complete_publication` хранит размещение по `finding.id` и объединяет
  fallback с фактом провайдера (`posted_inline_finding_ids`); при прочих случаях
  `run_lifecycle_store.complete_publication` связывает comments по порядку и равному
  количеству — нужен явный mapping подтверждённых remote объектов.
- GitLab publication на `main` — [#97](https://github.com/larchanka-training/dmc-268-api-t6/issues/97)
  (`gitlab_review_publication.py`, SD §8.4). Целевой #171 заполняет `position` по §9.5 из DiffMap,
  сверяет head перед каждым POST, классифицирует 400/422 с position как coordinates. Ключи §10 /
  отдельные receipts операций и LEFT — не реализованы.

## 11. Приёмочные сценарии и оценка качества

Таблица объединяет целевые требования #107 и ограниченное расширение #213.
Актуальные committed regression references и граница реализованного покрытия —
[TEST_PLAN §2.7, inventory #213](TEST_PLAN.md#27-матрица-приёмки-107-политика-контекст-верификация-и-write-back); таблица сама по себе не является отчётом о запуске.

| Сценарий | Ожидаемое поведение |
| --- | --- |
| Конвенция уровня 4 против SEC1/COR1 | Не отменяет дефолт; явное base rules/AGENTS исключение применяется по RFS §2 |
| Порог/max_comments меняются между retry и rerun | Retry сохраняет значения; rerun закрепляет новые, conventions key не меняется только от настроек публикации |
| Complete; supported medium отсекается порогом critical | FinalReviewResult содержит below_severity_threshold_count=1, final findings пусты, verdict clean; summary_text/check-run выводят один и тот же N |
| Commit messages на границах 20 × 500 символов, включая escaping | Фактический rendered input учитывается в общем бюджете, метаданные не становятся evidence |
| Правила меняются в head | Действует base policy; head-документ — только проверяемый diff |
| 4 000 ignored + 50 allowed строк | Обычный контекст для 50 строк, без summary-only |
| Все файлы доказанно E, без T (§2.2), полный inventory | skipped, ноль conventions/review/verifier calls |
| Один путь unsupported, остальные изменения доступны | Файл отсутствует во всех LLM-источниках и inline; остальные проверяются, coverage partial; при пустых findings или только info verdict null |
| Полный inventory: хотя бы один T, остальные только E/T (§2.2) | Каноническая ветка none: 0 LLM, пустые findings, verdict null; publishing → succeeded/neutral, без summaryOnly |
| Доказанный binary-only; доказанный binary + код | E только по bounded evidence §9.2: skipped в первом случае; во втором возможны complete/clean |
| Too_large-only; too_large >20 000 строк + 50 строк кода; маска assets/** + слишком большой assets/a.txt | Соответственно none; обычное ревью 50 строк с partial; skipped/ignored, так как маска побеждает too_large |
| Identity_unresolved, submodule либо доказанный pure rename/mode-only, отдельно и с ignored, incomplete нет | T с каноническими исходами none/partial; отсутствие строк не превращается в clean |
| Неполный inventory либо неизвестные relevant counters; отдельно missing-patch-only 4 000 known строк | Сначала summary-only/partial, summaryOnly=true, 0 LLM; none/skipped и no-text partial неприменимы |
| Только incomplete без hunks (missing/malformed/truncated patch), а также I+E, I+T и I+E+T; полный inventory, known counters 0/3 000, текста нет | Backend partial, summaryOnly=false, findings=[], verdict=null, 0 LLM включая conventions/verifier; pins и below_severity_threshold_count=0 сохранены, reviewed_changed_lines=0, неизвестная статистика null; publishing → succeeded/neutral |
| Truncated-only с сохранившимися разрешёнными hunks вне E/T, также с E/T; полный inventory, known counted allowed ≤3 000 | Hunks поступают в L1: обычное ревью с partial, blocking возможен; ограничения координат incomplete сохраняются. Оба триггера summary-only проверяются раньше (§2.2/§2.3); без hunks — backend partial по предыдущей строке. |
| Доказанный empty_file (`100644`/`100755`) + проверенный код; empty-only | Complete/clean без supported warning/error; соответственно skipped/all_changes_ignored, 0 LLM |
| Пустое added без полного proof; удалённая картинка без blob/независимого binary evidence | missing_patch/incomplete; рядом с кодом partial/null с причиной; metadata_only не E/T |
| Tree proof: pinned head, точный unique path+SHA, size None/0, type blob, nontruncated; ≤1 500 000 / >1 500 000 байт | Допустимый proof только в пределах; максимум один запрос при кандидатах, ноль без них; cap/failure/truncation остаются incomplete, не failed |
| Added с previous_filename/old path, даже при empty SHA/regular mode; empty SHA без mode; symlink/gitlink; duplicate path, неверный SHA/тип, конфликт размера/счётчиков/patch, неверный revision | Не empty_file, не clean; отсутствие proof объясняется в summary |
| Snapshot store/reload и retry; legacy missing_patch с empty SHA; downgrade CHECK | Валидированный marker сохраняет результат только после повторной проверки отсутствия previous path; legacy не повышается; downgrade переписывает empty_file→missing_patch |
| Добавленный/изменённый небольшой PNG с magic; oversized blob без независимого binary evidence | Первый доказанно binary/E; второй too_large/T до blob-based проверки, отдельно none, рядом с кодом partial |
| Чистый rename/mode-only и проверенный код | T/partial/null при пустых/info findings с полными текстами §2.2; без proof missing_patch/incomplete с соответствующим полным текстом; supported blocking/attention сохраняются |
| Rename/copy с одним unsupported-путём; новый файл запрошен через import | Весь DiffFile и новый файловый/AST-контекст запрещены; валидная половина не обходит ignore |
| Неподдерживаемое имя содержит управляющие символы или некорректный UTF-8 | Только безопасная диагностика; без исправленного канонического имени, утечки через metadata/omissions/summary или URL-якоря |
| `generated/a.py` переименован в `src/a.py`, импортирован другим файлом | Diff, L2/L3/L4 нового пути не отправляются; UI может хранить исходный patch |
| Base продвинулся после fork PR | Политика из policy_base_sha; старый код из diff_old_sha |
| Достоверный ignored якорь отсутствует в manifest; отдельно неизвестный путь совпал с ignore-маской | В проходе 2 ignored_file до видимости, сам по себе без partial; неизвестный путь — unknown_path/ошибка идентичности, маска не доказывает E |
| Разрешённый якорь с ignored evidence; отдельно разрешённый якорь не показан | evidence_source_disallowed в проходе 3; anchor_not_visible в проходе 2; оба технических drop дают partial, guard не допускает запрещённый source |
| Deleted-file identity; отдельно LEFT-only координата без подтверждённого deletion evidence | Структурный left_anchor_unsupported в проходе 2; во втором случае после валидных refs остаётся wrong_side; оба partial, без verifier/general |
| LEFT-only координата и валидное показанное deletion evidence без RIGHT; отдельно refs непоказаны/запрещены/не той ревизии | После прохода 3 left_anchor_unsupported побеждает wrong_side; невалидные refs дают соответствующую evidence-причину, не доказательство LEFT; partial, без допуска к verifier |
| В полном diff строка есть, в окончательном prompt её нет | Finding не допускается по одной DiffMap; evidence drop и partial |
| Диапазон через пропущенную строку/другой hunk | Нет inline; general только при всех реально показанных строках и supported |
| Импорт неоднозначен, исключён или parser упал | Нет угадывания определения; fallback L1–L3, причина неполноты |
| Результат verifier содержит лишний/пропущенный ID или ref предыдущего call | Весь ответ невалиден; восстановление в общем бюджете, затем failed |
| Verifier contradicted | Кандидат только в диагностике; его утверждение отсутствует в summary |
| Все кандидаты insufficient | Partial, verdict null, без заявления «проблем нет» |
| Verifier недоступен/бюджет исчерпан | Нет обхода подтверждения, после допустимого восстановления failed |
| Два runs с одинаковыми findings, разным head/summary | Разные публикационные identities и remote results |
| Timeout после успешного POST | Сначала recovery по своему маркеру/receipt, без слепого повторного POST |
| Повтор review.publish | Тот же сохранённый payload и решения, ноль новых LLM calls |
| Старые conventions без provenance | Cache miss; старое head-влияние не переносится |

Детерминированные сценарии покрывают границы snapshot/ignore, hunk mapping,
диапазоны, manifests, лимиты, guards, state transitions и publication recovery.
Отдельный корпус содержит инъекции в rules/AGENTS/PR/code/conventions/candidate/
repair-output, попытки изменить роли/адресата/формат, навязать пустой ответ,
выдумать дефект или перенести команду через кэш. В тот же корпус включаются
допустимые исключения и цитируемые примеры атак: их нельзя автоматически запрещать
по ключевым словам. Техническая защита и эмпирическая устойчивость модели
оцениваются раздельно.

Существующий raw-review benchmark [TEST_PLAN §3](TEST_PLAN.md#3-методология-llm-eval)
сохраняется. К нему добавляется отдельная оценка production pipeline и verifier,
без подмены первоначальных raw метрик:

- Raw и final Precision/Recall, Critical Recall и ручной Hallucination Rate —
  раздельно, с одним и тем же ground truth и однозначным matching.
- `verifier_false_rejection_rate`: реальные корректные кандидаты, отклонённые как
  contradicted, / все корректные кандидаты, переданные verifier. Insufficient
  показывается отдельно и также входит в потери final recall.
- `verifier_unsupported_acceptance_rate`: ошибочные кандидаты, получившие supported,
  / все ошибочные кандидаты на входе verifier.
- Доля insufficient, invalid verifier responses, failed runs и partial coverage;
  число и стоимость вызовов, p50/p95 длительности по каждой фазе.

Числитель/знаменатель и размер выборки обязательны; нулевой знаменатель → null.
Истину подтверждает независимая ручная разметка (спорные случаи — второй эксперт),
а не тот же verifier. В корпус добавляются как истинные кандидаты, так и
правдоподобные галлюцинации и пограничные текстовые исключения. Потеря хотя бы
одного размеченного critical из-за verifier разбирается вручную до включения
профиля. Продуктовые ориентиры текущего TEST_PLAN сохраняются; новые численные
quality gates не объявляются достигнутыми без baseline и согласования техлида.

## 12. Согласование контрактов и порядок внедрения

| Существующее положение | Целевое изменение этой спецификации |
| --- | --- |
| SYSTEM_DESIGN §9: L0, L2–L4 без полного provenance | §2–5: раздельные ревизии, AST profile, guard, manifest каждого вызова |
| SYSTEM_DESIGN Р-15/§9–11: `(code_change, head_sha)`, summary-only очищает patch | §2: снимки `(run_id, filename)`, UI patch сохраняется; очищается review_patch |
| SYSTEM_DESIGN §10: conventions cache только по AGENTS/prompt | §4.2: весь показанный base-input, policy, tree, selectors и версии |
| SYSTEM_DESIGN §10: ContextPayload только summary | §5: manifest и точные evidence excerpts в PG для аудита |
| SYSTEM_DESIGN §13: deep публикует сделанное по исчерпании бюджета | §7: публикация только завершённого supported final result |
| PIPELINE_SPEC §1: нет running → skipped для ignore | §2.2: добавить переход/уведомление all_changes_ignored |
| PIPELINE_SPEC §4–6: общий budget/recovery без verifier | §7–8: slots/cost/time reserve, новые ошибки, checkpoints |
| PIPELINE_SPEC §9–11 / review-output schema | v2 evidence_refs и новая attribution, coverage-aware verdict; v3 side отдельно |
| TEST_PLAN §3.4: после #133 есть схемы ReviewOutput v2/verifier, исторический prompt `review.verifier.v1.md` (#133) и `review.verifier.v2.md` (#219, текущий pin по умолчанию), unit-сценарии; #130 F1 подключает Reflection Pass в worker только для закреплённого v4, после F2 seed активирует v4 для новых runs, v3 или ниже сохраняет legacy | §7/§11: v4 активирован F2, полный lifecycle и quality baseline остаются целевыми; raw eval и независимый ручной oracle сохраняются |
| API RunDetail/RunSession/latestRun и UI | Добавить coverage, resultVersion/verificationMode; null verdict при partial без значимых supported findings |
| Legacy review/postprocess/lint-filter.md, порог severity | В #107 перенести severity threshold за verifier (§6.2), сохранить код lint_pattern; legacy-порядок PS §9 отдельно |
| Postprocess/lint-filter, prompt fixtures и review README | Версии evidence/атрибуции и фильтров; не восстанавливать raw summary |
| VCS publisher | §9–10: DiffMap + `resolve_anchor` на publish path, run-specific identities, сохранённый payload, адресные ошибки и recovery |
| SYSTEM_DESIGN Р-13, §9: RAG вне MVP; исторический §1 v1.0 этой спецификации (заменён v1.2): без RAG и индексации репозитория | Версия 1.2, [ADR 0001](adr/0001-graph-rag-v1.md): RAG v1 — граф символов с указателями; §2.1 `index_commit_sha`, §3.1 `retrieved_excerpt`, §3.5 правило ревизий и подтверждение рёбер, §4.1 слот бюджета, §5 manifest и guard |
| SYSTEM_DESIGN §10/§13: код клиента только в кэше блобов до 7 дней | Индекс — производные метаданные без текста и сигнатур, 30 дней с `last_used_at` (его сдвигают чтение при ревью и постановка full, не чаще раза в час; incremental не продлевает TTL); по событиям сразу нечитаем, физически удаляется сборкой мусора, которая идёт каждый интервал и продолжается на следующих тиках лидера, пока не вычистит всё; цель — в пределах часа ([ADR 0001 §4](adr/0001-graph-rag-v1.md#4-хранение-очистка-и-изоляция)) |
| PIPELINE_SPEC §2, §6 | Действие `context.rag`; `rag_*` — причины coverage, RAG не задаёт `error_code` |
| TEST_PLAN | §3.5: функциональные сценарии RAG, корпус со снимком репозитория, A/B-метрика вклада и гейт v2 до замера #146 |

Целевые положения [SYSTEM_DESIGN](SYSTEM_DESIGN.md), [PIPELINE_SPEC](PIPELINE_SPEC.md)
и [TEST_PLAN](TEST_PLAN.md) синхронизируются с этой таблицей в том же
документационном изменении #107; строки RAG v1 — в документационном изменении #140. Исторические результаты и сведения о текущем
коде сохраняются с явным обозначением legacy/версии. Таблица также задаёт
последующие изменения schemas, кода, prompts и UI; они здесь не реализуются.
До их внедрения новый pipeline не считается реализованным. Поведение legacy
не переносится в новые контракты по умолчанию.

Порядок разработки и слияния PR:

1. Инженер 3 делает небольшой PR с общими контрактами `DiffMap/v1`, якорей,
   provenance и портами чтения VCS с реализацией для GitHub. Этот PR должен быть
   слит первым, чтобы следующие изменения опирались на один принятый контракт.
2. После его слияния инженер 3 реализует загрузку/снимки правил, ignore, AST и
   контекст; инженер 5 параллельно реализует публикацию и её восстановление.
   Инженер 4 реализует безопасный envelope, окончательные guard/manifests,
   атрибуцию и обязательный verifier по контракту передачи из §3.1. Незавершённая
   часть не подменяется заглушкой в production-пути публикации.
3. Интеграция связывает policy snapshots/versions, base-only conventions,
   provenance, контекст, manifests и технические фильтры RIGHT с verifier,
   final result, API/UI coverage и write-back. Новый pipeline включается только
   после полной реализации и приёмки всей цепочки по §11. Первые отдельные PR
   не дают права заявить соответствие полному pipeline.
4. Сквозные LEFT и полный GitLab ingress — отдельные задачи; публикация discussions #97 уже на `main`.

Это порядок поставки компонентов, а не порядок обработки run: в runtime
загрузка политики и применение ignore предшествуют контексту и любому LLM-вызову
по §2.2; публикация требует завершённого проверенного результата.

Согласование содержания обеих спецификаций, включая новые лимиты, профиль и
контракты, зафиксировано в PR #108/#135/#157 (v1.0/v1.1/v1.2).
Это не означает выполнения реализации или проверок; последующие изменения
проходят review собственного PR. Закрытие #107 этим документом не заявляется.
