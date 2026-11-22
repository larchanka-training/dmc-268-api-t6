# TEST_PLAN §2.7: матрица приёмки #107

Вынесено из [TEST_PLAN §2.7](../TEST_PLAN.md#27-матрица-приёмки-107-политика-контекст-верификация-и-write-back) в #380: матрица, инвентарь регрессий #213 и карта тестов #130 и #168. Методология и общие нормы — в TEST_PLAN.

## 2.7. Матрица приёмки #107: политика, контекст, верификация и write-back

Сценарии ниже добавляются в unit/contract и integration по мере реализации; они не описывают уже существующее покрытие. Точные лимиты, грамматика и причины отказа берутся из двух новых спецификаций, а fixtures фиксируют версии этих контрактов.

| Сценарий | Ожидаемый результат |
| --- | --- |
| Нет rules/AGENTS; пустой документ; только секция Style; смешанный Python/TypeScript | Отсутствие и пустота различимы в snapshot; дефолты остальных категорий действуют. Языковые примеры остаются обычным Markdown, не фильтром backend. |
| Конкретное исключение rules против AGENTS/дефолта | Иерархия соблюдена только для соответствующего требования; формат, verifier, evidence, инструменты и получатель не переопределены. Проверить `rule_name` и ссылки на действующий источник. |
| Все положительные/отрицательные примеры `review-ignore/v1`, второй блок, 257 масок, неправильный UTF-8, byte/token/matcher overflow | Детерминированный результат по RULES_FORMAT_SPEC §§3–4; ошибочная политика даёт явный failed без усечения, пустой подмены и LLM. Временная ошибка VCS допускает только предусмотренный retry. |
| AGENTS отсутствует; существует, но превышает лимит или не декодируется; размер известен либо загрузка остановлена на лимите | Отсутствие допускает продолжение; непригодный документ даёт failed до LLM. Приложение и check-run сообщают путь, причину, предел/фактическое значение, когда оно известно, и способ исправления; не подменяют отказ предупреждением или неизвестный размер точным числом. |
| Head меняет policy; base продвинулся после fork; retry после изменения active config | Политика читается из закреплённого `policy_base_sha`; старые строки из подтверждённого `diff_old_sha`, который может отличаться. Retry не меняет pins, rerun получает собственный snapshot. Repository 404 не равен отсутствующему policy-файлу. |
| 4 000 ignored + 50 allowed строк; 3 000/3 001 allowed; все ignored; неполная пагинация files | Обычное ревью 50 и 3 000 строк; summary-only для 3 001/неполного inventory без LLM; только доказанные E без T → skipped. UI patch сохраняется; `review_patch=null` не восстанавливается из БД как code для модели. |
| Доказанный binary-only/empty-only; доказанные binary/empty_file + код; маска assets/** на oversized assets/a.txt | skipped/all_changes_ignored при полном inventory; complete/clean при полном анализе кода без supported warning/error; ignored побеждает too_large. E = маски/generated/binary/empty_file, канон — CVS §2.2/§9.2 |
| Too_large-only; too_large >20 000 строк + 50 строк кода; неразрешённый rename/submodule/mode-only с ignored | none; обычное ревью 50 строк с partial; none при полном inventory и отсутствии иных записей. Границы лимитов — CVS §9.2 |
| Только incomplete без hunks (missing/malformed/truncated patch); I+E, I+T, I+E+T без текста; полный inventory/known counters 0/3 000 | Backend FinalReviewResult partial, findings=[], verdict=null, summaryOnly=false; ноль conventions/context/reviewer/verifier вызовов, pins и below_severity_threshold_count=0 сохранены; reviewed_changed_lines=0, неизвестная исходная статистика null; публикация → succeeded/neutral. Классификация сохранена до раннего выхода. |
| Truncated-only с сохранившимися разрешёнными hunks; truncated без hunks; malformed без hunks; каждый также с E/T, known counted allowed ≤3 000 | Hunks вне E/T поступают в L1: обычное review/partial, blocking возможен; без hunks — backend partial, 0 LLM. Ограничения incomplete-якорей сохраняются; raw malformed patch не становится L1. |
| Полный inventory только T либо E/T с неизвестными raw counters T; incomplete вне E/T с неизвестными additions/deletions | В первом случае relevant counted scope пуст=0 → none, не summary-only; во втором relevant counters неизвестны → summary-only/partial до no-text ветки. E/T counters — диагностика. |
| Missing-patch-only 3 001/4 000 known counted allowed; неизвестные relevant counters или неполный inventory | Оба триггера summary-only проверяются раньше no-text branch: partial, summaryOnly=true, 0 LLM; none/skipped не выводятся из неизвестности. |
| Added __init__.py/.gitkeep с patch=None/пустой строкой (при reload также точная сохранённая пустая обёртка) и точным empty Git blob SHA, всеми counters=0, поддерживаемым каноническим путём, без предыдущего пути (`previous_filename is None`/логический `old_path=null`), непротиворечивым size и независимым regular mode 100644/100755 | empty_file/E; рядом с проверенным кодом complete/clean, empty-only skipped до conventions, 0 LLM; нет искусственных hunks/номеров строк |
| Квалифицированный empty candidate: поддерживаемый канонический путь, added, previous_filename=None/old_path=null, точный empty SHA, все counters=0, size=None/0, patch=None/пустая строка (при reload также точная сохранённая пустая обёртка), но независимого ordinary-file proof нет | missing_patch/incomplete, рядом с кодом partial/null; summary/body/check-run: «Нет подтверждения обычного пустого файла»; у GitLab нет независимого tree-доказательства обычного файла в этом потоке, поэтому пустое добавление остаётся missing_patch/partial даже при empty SHA и size=0 |
| Added с previous_filename/old path, неподдерживаемый путь, неизвестные/ненулевые counters, конфликт patch/size либо неверный/неизвестный SHA; raw metadata_only сам по себе | Не квалифицированный empty candidate и не empty_file/E; соответствующая technical/incomplete причина, с кодом partial/null; обычный added без patch: «Отсутствует подтверждённый patch» |
| Квалифицированный empty candidate с symlink/gitlink/unknown mode, empty SHA без mode или отсутствующим/негодным tree proof | Ordinary-file proof не принят, missing_patch/incomplete; empty-текст только при сохранении всех условий квалифицированного кандидата |
| Pinned head tree: unique exact case-sensitive path+SHA, type blob, size None/0, nontruncated; 1 500 000 / 1 500 001 response bytes | Первый предел допустим, превышение fail-closed/incomplete; максимум один дополнительный streamed identity metadata request на fetch с кандидатами, ноль без них/generated-only, ноль дополнительных blob-байт для доказанных кандидатов |
| Wrong revision/path/SHA/type/mode; duplicate tree path; truncation/ошибка/cap; cancellation/deadline | Нет empty_file и clean без доказательства; ошибки optional proof не failed, cancellation/deadline распространяются; branch/base tree не заменяют head |
| Fetch→snapshot store/reload→retry; legacy missing_patch с empty SHA; downgrade `20261026_0043` | Validated marker сохраняется с повторной проверкой durable identity/SHA/status/counters/patch и `previous_filename is None`; конфликт size не забывается; legacy не повышается по SHA; реальная БД принимает новый CHECK, downgrade переписывает empty_file→missing_patch |
| NUL в первых 8 KiB/PNG-JPEG-GIF-PDF-ZIP-ELF magic в blob; NUL только за 8 KiB без magic в blob; NUL в patch на любой позиции; oversized unread blob; независимо доказанный binary при oversized | Только подтверждённый binary — E; NUL вне окна blob сам не доказывает binary, но NUL в patch на любой позиции доказывает binary; без независимого evidence oversized — too_large/T до blob-based detection. Removed без blob не становится E по расширению (CVS §9.2). |
| Доказанный pure rename/mode-only с чистым кодом; raw status/zero counters без proof | T либо missing_patch/incomplete сохраняют partial/null; используются соответствующие полные тексты ([PIPELINE_SPEC §2](../PIPELINE_SPEC.md#2-фазы-внутри-running-и-трейс-run_actions)); ноль новых tree/blob/compare запросов |
| Removed image без нового blob/independent binary proof; oversized unread blob >1 048 576 байт без независимого proof + код | missing_patch/incomplete либо too_large/T, partial/null; summary/check-run используют «Удалённое содержимое недоступно для подтверждения binary» либо «Превышен лимит размера, содержимое не проверено»; old/base/prefix/range fetch не добавляется, binary cap не повышается |
| fit_review_context опускает весь allowed changed patch либо оставляет префикс; внутреннее усечение модели при неизменном внешнем контексте; untrimmed control; обе reflection ветки | review_input_incomplete/partial до финализации; affected файл не fully reviewed; пустые/info null, supported blocking/attention сохраняются; вспомогательный context-only omission сам по себе не меняет changed-patch coverage |
| Полная durable membership после restore с более чем 20 причинами; повторное усечение уже incomplete файла; ignored контроль | Счётчик fully reviewed уменьшается только для ранее полного allowed patch; уже неполный не учитывается повторно; diagnostic lists ≤20, внутренний набор не сериализуется, ignored имена не раскрываются |
| Технический unknown_path drop, включая критический кандидат; lint/duplicate/severity controls | unknown_path даёт partial, пустой остаток не clean; benign drops сами по себе не понижают complete |
| Любой non-SUCCEEDED public detail/latest-run; trusted PUBLISHING recovery | Публичные summary и saved_result равны None без fallback; internal recovery читает сохранённый finalized outcome, не меняет event/body/verdict |
| Literal saved JSON complete с причинами; stale/malformed/bool-as-int; недоступная classification и ignored paths | Оба читателя fail closed; invalid newest record не оживляет старый clean; fallback counts-only без имён ignored |
| Сохранённый coverage none и итог без значимых findings | Body/check-run одинаково: «нет доступных для анализа изменений», neutral/null; inline legacy без coverage unavailable/partial |
| Любой retained blocker + supported critical/high либо medium/low; пустые/info findings | blocking/attention допустимы и при partial; пустые/info дают null на API/check-run/review body, без fallback к clean |
| Known ignored/generated anchor без manifest; ignored deleted-file anchor; binary anchor; unknown path, совпавший с маской; allowed anchor с ignored evidence | Trusted identity → ignore/generated раньше видимости: ignored_file сам по себе не partial; для ignored удаления `ignored_file` побеждает структурный `left_anchor_unsupported`. Binary не получает этот bypass: line_absent/partial. Unknown path не доказывает E, сохраняется unknown_path/identity error. Запрещённый evidence у allowed anchor → evidence_source_disallowed/partial. Независимые причины partial не исчезают; pre-send/general guards действуют. |
| Pass-4 guard: изолируемый запрещённый evidence/general конкретного кандидата; неизолируемый источник/повреждённые snapshot/manifest bindings; попытка pre-send запрещённого источника | В проходе 4: evidence_source_disallowed/partial без general/verifier eligibility; при невозможности изоляции/проверки целостности — весь run snapshot_integrity_error/failed, без публикации. Отдельный pre-send guard даёт policy_context_violation/failed до следующего вызова, не drop и не второе ignored_file. |
| Deleted-file identity вне ignore/generated; LEFT-only coordinate без deletion evidence; overlap с валидным shown deletion evidence; непоказанные/запрещённые/wrong-revision refs | Структурный left_anchor_unsupported в проходе 2; evidence-dependent отказ предварительный до прохода 3. При валидных refs без подтверждения удаления — wrong_side; при валидном подтверждении удаления без RIGHT — left_anchor_unsupported с приоритетом. Невалидные refs дают evidence-причину, не LEFT proof. Все технические drops partial, без general/verifier eligibility. |
| Конвенция уровня 4 против SEC1/COR1 | Не отменяет дефолт; явное base rules/AGENTS исключение имеет приоритет по RFS §2 |
| Изменение severity_threshold/max_comments после создания run | Retry сохраняет значения, rerun закрепляет новые; настройки входят в result_digest, но не policy_digest/conventions key |
| Supported medium отсекается critical, complete | below_severity_threshold_count=1; final findings пусты, clean допустим; summary_text и check-run GitHub берут N из одного result_id (GitLab Run — без check-run, #97) |
| Commit messages: 20 сообщений по 500 Unicode-символов, включая XML escaping | Ограничение количества/символов до escaping и фактический token-size после него; общий budget не превышен, metadata не становится evidence |
| Неподдерживаемый путь рядом с обычным файлом: backslash, управляющий символ, недекодируемое имя, превышение 1 024 UTF-8 байт или 128 сегментов | `unsupported_path` изолирует только проблемный источник, без «исправления» имени. Остальной допущенный файл проверяется; coverage partial, при пустых findings или только info verdict null. Содержимое и автоматически добавленное имя пропущенного файла не попадают в модель, включая metadata, conventions, AST, verifier, repair и fallback; сводка сообщает пропуск, диагностическое имя выводится безопасно и с ограничением длины. Неизвестный счётчик пропущенного файла остаётся null и сам по себе не включает summary-only. |
| Полный inventory: хотя бы один T, остальные только E/T по CVS §2.2; отдельно только E и неполный inventory | Для состава E/T с хотя бы одним T — 0 LLM-вызовов, пустые findings, coverage none с фактическими причинами T, verdict null; сохранённый backend-результат проходит публикацию и даёт succeeded/neutral. Это не all_changes_ignored и само по себе не summary-only. Все доказанные E без T → skipped; неполный inventory сохраняет summary-only/partial. |
| Rename/copy с неподдерживаемым old либо new path; повторный запрос нового пути через импорт/кэш | Пропущено всё изменение, запрет не обходится через валидную сторону и её AST/алиасы. Raw provider fields остаются диагностикой; каноническое имя не угадывается. Невозможность надёжной изоляции/нарушение целостности → snapshot_integrity_error, утечка в final payload → policy_context_violation до отправки. |
| Rename/copy из ignored пути; ignored файл импортирован; `**/*.md` исключает head rules/AGENTS | Известное происхождение сохраняет запрет для diff/L2/L3/L4, repair, fallback, verifier и cache hit. Исключение имеет только действующий base policy в роли policy_document. Исключённые пути/текст не попадают в модельные списки файлов. |
| Conventions cache miss/hit, PR body с инструкцией, старый cache без provenance | Общий input только base: ни title/body PR, ни head, ни changed paths. Ключ охватывает показанные документы/tree/excerpts и версии; изменение ignore/base-input даёт miss, legacy cache не используется. Нет исходников → `conventions_not_applicable` без вызова; ошибка реального вызова не превращается в пустой успех. |
| AST Python/TypeScript/TSX/JavaScript/JSX/Go, Unicode/CRLF и exclusive end; вставка между символами | Однобазовые точные source ranges; минимальный охватывающий символ либо module-level change. Одна степень однозначных импортов, без исполнения проекта/конфигов, сети или установки зависимостей. |
| JS/JSX `.js/.jsx/.mjs/.cjs`: функции, стрелочные функции, классы; статический относительный ESM import и прямой export; CommonJS/dynamic import/barrel | Синтаксический AST доступен для всех перечисленных расширений. L4 определения импортов — только при однозначном разрешении по профилю CONTEXT §3.3; CommonJS, dynamic import и re-export не угадываются. Неоднозначные расширения и смешанное JS/TS-разрешение получают явный fallback. |
| Go `.go`: функции, методы, типы struct/interface, константы; локальный module import, явный alias и package name, отличный от имени каталога | Точные диапазоны символов; прямой qualified exported symbol разрешается по закреплённым go.mod и полному разрешённому inventory пакета. Неявное имя берётся из package clause. Same-package lookup по соседним файлам и определение методов через тип receiver не заявляются возможностями resolver v1. |
| Go build constraints/GOOS/GOARCH/cgo, go.work/replace/nested module/dot import; ignored либо 21-й дополнительный файл | Нет зависимости от окружения worker и запуска go list. Неподдерживаемый выбор пакета, неполный inventory или исчерпание общего лимита 20 импортированных/служебных файлов (включая go.mod) не дают resolved по частичному набору. Сохраняются доступные L1–L3 и явная причина; `_*.go`, `.*.go`, `_test.go` не входят в обычный inventory импортируемого пакета. |
| AST timeout/resource limit/parse error, unsupported language, неоднозначный/внешний/ignored импорт | Сохраняются доступные L1–L3; нет угадывания определения или подмены LEFT на head. Причина фиксируется; partial требуется при недоступном основании конкретного кандидата. Отказ обязательного VCS/L1 не маскируется как AST fallback. |
| Context budget отсекает часть hunk или тело символа; repair/fallback получает другой набор блоков | Manifest строится после окончательного rendering, имеет свой call_id и точные shown ranges/omissions. Полная DiffMap не доказывает видимость; refs разрешаются только в принятом call. Candidate/previous_output/conventions не становятся code evidence. |
| Неверный snapshot/hash/revision или источник без provenance перед любой стадией | Guard останавливает отправку с `policy_context_violation`/ошибкой целостности; запрещённых LLM-вызовов нет. Кэш не обходит guard. Manifests/excerpts workspace-scoped и не выводятся в обычные логи. |
| V2 finding с непоказанным block/строкой, неверным source или revision; ссылка неверной формы | Синтаксическая ошибка инвалидирует весь ответ; семантически недостоверная evidence отбрасывает кандидата и отражается в coverage. Все строки диапазона должны быть показаны, не только края. |
| Якорь вне hunk, диапазон через hunks/непоказанную строку, несуществующая строка, deletion-only | General-only лишь для существующего полностью показанного RIGHT диапазона того же файла/revision, затем verifier. Невалидный якорь не становится общим замечанием и не переносится на ближайшую строку. Deletion-only без RIGHT якоря → `left_anchor_unsupported`, partial до v3. |
| DiffMap: добавление/удаление, rename/copy, zero-length hunk, context/deletion/addition, no-newline marker, binary/missing/truncated patch | Счётчики hunks и обе стороны согласованы с provider refs; marker не расходует строку; пути не угадываются. GitHub payload использует line/side и start_line/start_side, не legacy position. GitLab fixtures проверяют version/base/start/head refs, old/new paths и диапазоны (CONTEXT §9.5); live GitLab diff/LEFT отложены. Публикация discussions — #97, gateway-тесты в `test_gitlab_review_publication.py`. |
| Два разных дефекта на одном якоре; точный дубль; lint-похожие слова в security finding | Только точный дубль устраняется; lint-filter не скрывает содержательный дефект. Кандидаты после фильтров неизменяемы; verifier не дописывает находки, координаты, severity или suggestion. |
| Пустой набор после фильтров; непустой набор; supported/contradicted/insufficient | Пустому verifier не нужен, повторный поиск не запускается. Для непустого каждый ID получает ровно одно решение; только supported публикуется. Неверная suggestion не исправляется скрытно. Insufficient отдельно от опровержения и даёт partial. |
| Лишний/повторный/пропущенный ID, неверная ref/requirement ref в verifier response | Весь ответ невалиден, удобное подмножество решений не принимается. Recovery только в общем бюджете, затем `verification_invalid_output`, без retry run и публикации. |
| Cache miss `C1 → R1 invalid → R2 repair → V1`; `C1 → C2 repair → R1 → V1 invalid`; cache hit с двумя recovery | До 4 generation-вызовов на попытку, до 12 на run и 3 попыток; downstream verifier slot зарезервирован. Второй сценарий завершается failed без пятого вызова. Потенциальная generation при transport retry также учитывается. |
| Недоступность verifier, 429/402, нехватка стоимости или дедлайна, overflow обязательного envelope | Временная `verification_unavailable` допускает 30 с/2 мин и максимум 3 попытки, затем failed; invalid output не переклассифицируется в unavailable. Учитываются Retry-After/маршрут; лимиты $0.50 fast/$3 deep на весь run, резерв verifier и время 90 с + 10 с локального завершения. Budget/deadline/context overflow → terminal failed, непроверенные findings не публикуются. |
| Crash после reviewer checkpoint, после verifier/final result, повтор publish | До verifier используется сохранённый набор; после final result нет новых смысловых LLM-вызовов. Usage и пределы всех попыток сохраняются, ошибки provider не превращаются в нулевую стоимость. |
| Все кандидаты insufficient; partial с supported high; complete без findings; duplicate/lint/contradicted drop | Соответственно verdict null, blocking, clean; последние drops сами по себе не создают partial. Backend summary не повторяет rejected/insufficient утверждения; API/review/check-run имеют один result и counts. |
| Компактная summary и general findings суммарно >4 000 символов; готовый payload на границе/сверх лимита адаптера; oversized body-only fallback | 4 000 Unicode-символов ограничивают только summary_text. Полное тело сохраняет supported findings. Все payload выбранного варианта, включая markers, сверяются с закреплёнными единицами/пределами профиля; превышение → failed/publication_payload_too_large до его первого POST. Findings не усекаются; suggestion_unrenderable убирает только применяемый блок. |
| Два runs/rerun одного head с одинаковыми findings; повтор доставки; marker другого автора или другого payload | Разные runs имеют разные publication keys; повтор одного run восстанавливает собственную операцию. Проверяются bot/app, PR/MR, head, полный payload и пагинация, одного findings_hash/marker недостаточно. |
| Timeout/неоднозначный 5xx после возможного POST; истёк lease; удалённая операция на второй странице | Сначала recovery сохранённого payload/receipt, без параллельного или слепого POST. Неполный lookup не означает отсутствие. Невосстановленный исход → `failed/publication_outcome_unknown`, автоматическая отправка блокирована. |
| 422 coordinates при неизменном diff; 422 body/spam/schema; stale commit/head; 403 rate limit | Только подтверждённый координатный отказ допускает один заранее сохранённый body-only вариант с отдельным key/digest. Другие ошибки не дают такого fallback; stale snapshot → cancelled/superseded. Неизвестный исход запрещает fallback. |
| Push между GET и POST; ошибка записи receipt; GitLab partial publication; legacy незавершённая публикация | IDs сохраняются и связываются явно, не по порядку; старый head не объявляется проверенным. Оставшиеся GitLab POST прекращаются при устаревании, готовые не повторяются. Неоднозначный legacy result → `legacy_publication_unresolved`, без повторного POST. GitLab adapter #97 — `test_publish_run_review.py`, `test_gitlab_review_publication.py`. |

Отдельные adversarial fixtures содержат инъекции в rules/AGENTS/PR/code/conventions/candidate/repair-output и попытки переноса через cache: закрытие envelope, навязывание находки, подавление настоящего дефекта, смена адресата/формата/полномочий. Рядом нужны допустимые исключения и цитаты примеров атак. Детерминированные assertions проверяют запрет отправки/публикации и границы источников; влияние на смысл ответа измеряется отдельно по §3.4. Валидная schema сама по себе не доказывает устойчивость к инъекции.

Тексты причин #213 для summary/body/check-run, общие тексты `REASON_TEXT`, метки сводки, квалификация
empty-added (в т.ч. GitLab без tree-proof), `reason_variant_counts`, условия tree-proof E=`empty_file`
и нейтральный текст too_large — канон в [PIPELINE_SPEC §2](../PIPELINE_SPEC.md#2-фазы-внутри-running-и-трейс-run_actions); здесь не дублируются.

#213: ограниченная проекция в существующих snapshots/context/postprocess JSON
не означает активацию всех целевых backend none/no-text/summary-only маршрутов
#107/#130/#168. Для четырёх retained случаев дополнительных запросов нет; для
empty proof бюджет строго 1 500 000 metadata bytes / максимум один запрос,
blob cap остаётся 1 048 576 байт, L2–L4 source cap 256 KiB независим.
Summary-only с нулём вызовов модели — текущий жизненный цикл: стадия останавливается
до RAG, conventions, ревью и verifier и публикует `NoModelAnswer`; coverage partial,
verdict null. Сохранённый полный allowed patch ещё
не доказывает полного входа модели: omitted/prefix-truncated changed patches
после fit_review_context дают review_input_incomplete/partial и уменьшают fully
reviewed count, в том числе через обе reflection ветки. Технический unknown_path
drop также понижает coverage; benign lint/dedupe/severity drops — нет.
Остальные reflection coordinate drops, evidence_not_shown и verifier
insufficient_context остаются за lifecycle #107/#130; их общее влияние на coverage
не объявляется реализованным в #213.

После #130 F1 worker собирает Reflection Pass. F2 seed активировал
`review.system:v4` для новых runs; после #350 новые runs закрепляют общий v5.
Snapshot-путь выбирается по закреплённому v4 или v5; runs с v3 или ниже,
созданные до F2 seed либо с pins до 0039,
сохраняют legacy-путь без verifier. Адаптер ревью
выполняет fit/guard/manifest один раз: snapshot-ответ `ReviewReply` несёт
фактический fitted context и manifest принятого вызова, legacy — mapping.
В обоих ответах факт потери changed patch передаётся доверенным внутренним
carrier, сохраняется после unwrap и обеих веток Reflection Pass; JSON/schema
ответа модели не расширяются. Остальные технические drops из целевого lifecycle
#107/#130 не объявляются реализованными этим стыком.
Все поверхности строятся после этого понижения; supported risk сохраняется.

Независимая защита внешнего сравнения исходных файлов с `ReviewReply.context`
проверяется `test_outer_context_loss_survives_both_reflection_returns_without_carrier_loss`
в `tests/test_clean_without_text_sent_context.py`: контекст теряет отдельный файл,
а carrier передаёт пустой `incomplete_paths`. Пустой ответ и supported high
сохраняют partial; verdict соответственно null и blocking. Удаление только этого
слагаемого объединения должно нарушать обе проверки покрытия.

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

Public detail/latest-run возвращают summary=None/saved_result=None для всех
non-SUCCEEDED; внутренний finalized-result read в PUBLISHING recovery сохраняется.
Оба читателя отвергают complete с incomplete reasons. Missing/invalid effective
classification fallback — counts-only без потенциально ignored имён. Restore
использует pinned policy_base_sha (= Run.base_sha) и отдельный фактический
diff_old_sha, без вымышленной provider metadata. Inline legacy без coverage и
прямой FindingsPostProcessor.process(coverage=None) получают
classification_unavailable/partial до summary/body, без clean по отсутствию данных.
Повреждённый сохранённый JSON даёт warning с run_id без raw JSON, путей или
секретов; ожидаемое отсутствие записи само по себе не предупреждение. Оба
читателя используют общий валидатор status/count invariants; restore считает
snapshot digest один раз. StoreReviewOutput явно отклоняет отсутствующий backend
summary до записи/commit/очереди, независимо от Python -O.
При none review body и check-run одинаково сообщают
«нет доступных для анализа изменений». Исторические GitHub-тела не перепубликуются,
даже если succeeded API теперь возвращает conservative fallback/null.

Committed regression inventory #213 (наличие теста не заменяет отчёт о запуске):

Точные метки и счётчики summary/body закреплены в
`test_clean_without_text_reasons.py::test_qualified_empty_reason_after_twenty_diagnostics_keeps_canonical_text`
и `::test_no_coverage_summary_uses_the_exact_unchecked_text_label`.

- `test_clean_without_text_github.py::test_github_fetch_proves_empty_regular_file_from_one_pinned_tree_request`,
  `::test_tree_cannot_prove_empty_with_unsupported_or_conflicting_evidence`,
  `::test_one_metadata_request_proves_multiple_empty_files`,
  `::test_no_tree_request_when_no_empty_addition_candidate_exists`,
  `::test_optional_mode_proof_does_not_swallow_cancellation`,
  `::test_generated_empty_addition_needs_no_mode_proof`,
  `::test_valid_tree_at_exact_metadata_budget_proves_empty`,
  `::test_valid_tree_over_metadata_budget_fails_for_cap`,
  `::test_optional_tree_proof_reuses_diff_token_when_refresh_would_fail`,
  `::test_optional_tree_http_failure_stays_missing_patch_with_reused_token`,
  `::test_huge_streamed_tree_aborts_before_unread_tail_and_closes_response` —
  adapter contract/cost, valid-JSON declared/streamed cap и identity request header,
  optional proof warning/fail-closed без повторного token lookup. Тело 12 000 000
  байт без declared length обрывается после первого chunk сверх 1 500 000
  (прочитано ≤1 565 536), unread tail не потребляется, response закрыт; прежний
  bounded blob fallback сохраняется, ordinary-file mode из него не выводится.
- `test_clean_without_text.py::test_empty_proof_survives_fetch_snapshot_storage_and_reload`,
  `::test_snapshot_does_not_erase_conflicting_fetched_size`,
  `::test_symlink_gitlink_and_unknown_modes_never_prove_empty`,
  `::test_executable_regular_empty_file_is_also_excluded`,
  `::test_persisted_marker_does_not_override_conflicting_snapshot_evidence`,
  `::test_processor_does_not_fetch_empty_blob_for_optional_cache_on_first_run_or_retry` —
  persisted evidence и стоимость retry.
  `::test_missing_or_contradictory_evidence_never_proves_empty` включает added с
  `previous_filename="old.py"`: наличие предыдущего пути запрещает empty_file/E
  даже при остальных корректных empty-file данных.
- `test_clean_without_text_postgres.py::test_empty_file_omission_persists_and_reloads_through_real_repository`,
  `::test_downgrade_rewrites_empty_file_to_missing_patch_before_restoring_old_constraint` —
  реальная PostgreSQL store/reload и обратная CHECK-миграция; пропуск без DB URL
  не является passing DB evidence.
- `test_clean_without_text_context.py::test_stage_omits_empty_addition_and_records_complete_policy_effective_coverage`,
  `::test_exclusion_only_stage_skips_before_any_model_input`,
  `::test_empty_marker_cannot_skip_invalid_identity_or_incomplete_inventory`,
  `::test_optional_builder_failure_preserves_effective_policy_coverage_for_finalization` —
  effective policy, ранний skip и conservative coverage.
- `test_clean_without_text_acceptance_postgres.py::test_actual_stage_persisted_empty_proof_controls_http_and_recovery_result` —
  настоящий StoreDiffSnapshot/reload → ReviewContextStage/BuildReviewContext → SQL trace
  (один context.build стадии с coverage, он же строка `context_payloads`; позднее чтение
  входа промпта записи не пишет, #168 ч.6) → StoreReviewOutput → восстановление
  публикации → authenticated HTTP detail/latest-run/check-run. Пара меняет только
  empty_file → missing_patch при одинаковом empty SHA, пути и counters.
- `test_clean_without_text_result.py`, `test_clean_without_text_coverage_record.py`,
  `test_clean_without_text_result_postgres.py` — deterministic summary/body и усилие,
  сохранённая classification с effective ignore, stale/malformed fail-closed;
  `test_clean_without_text_views.py`, `test_clean_without_text_views_postgres.py` —
  nullable projection, referenced response body, bounded batched legacy fallback,
  retained причины, supported risk и summary-only high/medium candidates без публикации.
- `test_clean_without_text_sent_context.py::test_model_boundary_loss_survives_both_reflection_returns_without_outer_fit_loss`,
  `::test_actual_budget_fit_flows_through_reflection_early_return_and_finalization`,
  `test_clean_without_text_gateway.py::test_gateway_hidden_fit_marks_legacy_unverified_output_and_persisted_result_partial` —
  фактический model input и обе reflection ветки до persisted outcome, включая
  скрытое внутреннее усечение при неизменном внешнем контексте.
- `test_clean_without_text_membership.py::test_restore_bounded_classification_then_actual_fit_reduces_only_full_allowed_patches`,
  `::test_unknown_membership_withholds_counts_without_inventing_reviewed_files` —
  полная membership за окном 20, отсутствие повторного уменьшения count и утечки ignored;
  неизвестная membership не создаёт выдуманных reviewed counts.
- `test_clean_without_text_persistence_postgres.py::test_context_restore_selects_one_explicit_classification_among_large_prompt_bodies`,
  `::test_referenced_result_and_raw_response_cannot_read_another_runs_body`,
  `::test_latest_nonsucceeded_runs_skip_saved_result_and_fallback_queries`,
  `::test_nonsucceeded_details_withhold_summary_and_saved_result_without_loading_results` —
  новейшая явная classification без загрузки поздних prompt-only тел, run-owned
  referenced body и succeeded-only batch/public gates; требуют реальную PostgreSQL.
- `test_clean_without_text_invariants.py::test_literal_finalized_coverage_and_summary_contract`,
  `::test_saved_result_rejects_literal_complete_with_retained_too_large_reason`,
  `::test_context_restore_rejects_literal_complete_with_retained_too_large_reason` —
  literal JSON/summary oracle и complete-инвариант обоих читателей.
- `test_clean_without_text_reasons.py::test_qualified_empty_reason_after_twenty_diagnostics_keeps_canonical_text`,
  `::test_persisted_empty_reason_variant_cannot_exceed_trusted_snapshot_capacity`,
  `::test_saved_reason_variant_schema_rejects_invalid_aggregates`,
  `::test_legacy_aggregate_does_not_infer_empty_qualification_from_added_status`,
  `::test_ignored_empty_paths_do_not_enter_semantic_aggregates_or_diagnostics`,
  `::test_saved_reason_variants_cannot_reclassify_visible_generic_diagnostics` —
  полный literal summary после projection→restore→finalization→saved-result read;
  canonical empty-текст для 21-й причины без 21-го пути; поддельный aggregate
  сверх доверенной snapshot capacity и подмена видимой generic причины отвергаются;
  invalid aggregates/bool-as-int/unknown keys не принимаются, legacy added сам по
  себе не получает empty-квалификацию, ignored пути не входят в агрегаты/diagnostics.
- `test_clean_without_text_sent_context.py::test_each_independent_patch_loss_withholds_reviewability` —
  отдельные omitted, prefix_cut, original_truncated и sent_truncated; ни один
  случай не полагается на другой признак потери patch, untrimmed control без потери.
- `test_clean_without_text_legacy.py::test_direct_postprocessor_without_coverage_withholds_clean`,
  `::test_queue_store_rejects_missing_backend_summary_before_writes` —
  прямой legacy process без coverage выдаёт точный unavailable/partial summary/body;
  summary guard вызывает InvalidReviewOutput до записи, commit и queue publish.
- `test_clean_without_text_restore_api.py::test_context_binding_requires_canonical_sha1_while_github_path_keeps_its_contract`,
  `::test_context_restore_hashes_durable_snapshots_once_and_rejects_mismatch`,
  `::test_corrupt_context_warns_with_run_id_while_absent_context_is_quiet`,
  `test_clean_without_text_views.py::test_corrupt_saved_result_warns_with_run_id_without_serialized_data` —
  общий canonical SHA-1 guard без сужения GitHub path contract, один snapshot hash
  при валидном и несовпавшем restore; warnings с run_id без содержимого, отсутствие
  сохранённой записи остаётся тихим.
- `test_clean_without_text_overview.py::test_body_budget_preserves_partial_backend_result_and_accounts_for_all_findings`,
  `::test_author_path_list_never_discloses_ignored_unsupported_or_unknown_scope`,
  `test_sqlalchemy_run_repository.py::test_get_post_processing_input_returns_none_allowed_v2_rules_for_legacy_run` —
  стык #170/#213: queue и inline сохраняют backend summary/partial coverage через
  перепаковку целых body-only bullets, все findings учтены как bullets + footer N,
  run_url не вытесняет факты неполноты. Fully reviewed=0 при fit-loss не запрещает
  список доступных текстовых входов; ignored/unknown scope скрывает все имена,
  неподдерживаемый путь не выводится. Repository сохраняет checked_paths только
  для review_patch != null, включая пустую строку; pins/live fallback не теряются.
- `test_clean_without_text_cache.py::test_proven_empty_file_is_readable_after_processing_and_retry_without_blob_requests`,
  `::test_unproven_empty_marker_cannot_synthesize_cached_file_content` — first run и
  retry с новым cache возвращают total_lines=0/lines=[] без GitHub blob reads;
  противоречивое доказательство не создаёт фиктивное содержимое.

**Карта тестов #130 и #168: политика, контекст и snapshot-путь.** Существующее покрытие
этих частей матрицы — файлы `tests/` по группам:

1. Загрузчик политики: `test_policy_documents.py`, `test_github_policy_source.py`,
   `test_github_snapshot_source.py`, `test_prepare_policy_snapshot.py`, `test_policy_budget.py`,
   `test_policy_snapshot.py`, `test_policy_snapshot_integrity.py`.
2. review-ignore: `test_review_ignore.py`.
3. DiffMap: `test_diff_map.py`, `test_diff_map_pinned_base.py`, `test_post_processing_diff_map.py`.
4. AST: `test_ast_resolution.py`, `test_tree_sitter_facts.py`, `test_ast_worker.py`,
   `test_subprocess_source_parser.py`.
5. Контекст: `test_review_context.py`, `test_review_context_stage.py`,
   `test_run_processor_context_stage.py`, `test_withheld_files_in_prompts.py`,
   `test_context_provenance.py` (роль, ревизия, путь и диапазоны строк фрагмента); что каждая
   production-композиция стадии пишет строку `context_payloads`, стережёт
   `test_context_stage_wiring.py`.
6. Snapshot-путь с закреплённым `review.system:v4` или `review.system:v5`:
   `test_review_path.py`, `test_snapshot_path_live_pin.py`,
   `test_review_prompt_v4.py`, `test_review_context_blocks.py`,
   `test_reflection_pass_snapshot_path.py`, `test_gateway_verifier_snapshot_path.py`,
   `test_llm_gateway_snapshot_reserve.py`, `test_gateway_repair_envelope.py`,
   `test_llm_verify_trace.py`, `test_gitlab_snapshot_path.py`,
   `test_review_output_policy_attribution.py` (строка атрибуции в §2.8), пять файлов
   `test_coverage_none_*` (строки технических пропусков в §2.8) и `test_retry_after.py`.
7. Snapshot на PostgreSQL: `test_policy_snapshot_postgres.py`,
   `test_policy_snapshot_integrity_postgres.py`, `test_run_policy_snapshot_origin_postgres.py`,
   `test_context_manifests_postgres.py`, `test_llm_verify_trace_postgres.py`,
   `test_rules_md_e2e_postgres.py`, `test_rules_md_e2e_applied_postgres.py`,
   `test_snapshot_path_e2e_postgres.py`, `test_snapshot_path_e2e_rag_postgres.py`,
   `test_context_manifests_migration.py` (upgrade, downgrade и повторный upgrade `context_manifests`),
   `test_run_context_origin_postgres.py` (поздние оплаченные конвенции не пишут историю контекста
   завершённого run), `test_run_policy_context_origin_postgres.py` (чтения политики и контекста
   у провайдера прекращаются со следующим запросом после снятия исходной установки).

Redis-кэши (кэш контекста и кэш токенов установки) проверяются только на классах
`FakeRedis` внутри самих тестов (`test_redis_context_cache.py`,
`test_redis_installation_token_cache.py`): в CI есть сервисы PostgreSQL и RabbitMQ, Redis нет.
