# TEST_PLAN §3.2.1–3.2.5: зафиксированные raw baselines

Вынесено из [TEST_PLAN §3.2.1–3.2.5](../TEST_PLAN.md#321-зафиксированный-raw-baseline-82-2026-10-13) в #380. Правила сопоставления и счётчики — в TEST_PLAN §3.2.

## 3.2.1. Зафиксированный raw baseline #82 (2026-10-13)

Это историческое измерение; исходные raw bytes/digests доступны на
[`76df6d4`](https://github.com/larchanka-training/dmc-268-api-t6/tree/76df6d4d0f431ce4c02e6708410c000c66f6135a/test-prs-dataset/responses).
Baseline §3.2.2 заменил весь response set одной новой съёмкой без drift. Новый
sidecar содержит проверенные API-order prefixes при совпадающих PR head/revision:
CLEAN-03/LOG-03/SEC-04 по 1 сообщению, RES-03/SYN-04 по 20 и uncertainty marker.
Strict schema и canonical/active binding проверяются до calls/staging; все
canonical case.json identity inputs и shaping modules входят в static provenance.
Результаты ниже относятся только к capture 2026-10-13 и не измеряют новые
prefixes; новая полная съёмка описана в §3.2.2; текущий baseline — §3.2.5.

[Actions run 38468081001, attempt 1](https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/38468081001)
снял все 24 первых ответа на source SHA `80ede3cd274d745616b8a24082ff7d1d00f5b736`;
начало `2026-10-13T21:06:56.682693Z`. Engine `fast`, primary `mistral-small-4`,
configured fallback `mistral-small-3.2-24b`, configured provider `eurouter`.
Все первые вызовы — `answer` / `primary`, модель `mistral-small-4`, serving-provider
label `mistral`; все gateway statuses `accepted`, paid-metadata errors отсутствуют,
`baseline_publishable=true`. Prompt `review/prompts/review.system.v2.md` (`v2`),
SHA-256 `9177e358c23d1a7cd8987cbf667cf8b705c2afb27eaf8b2237480e4531431d75`.
Static digest `sha256-v1:9b28cf4c284ac07a03a5a35a62e3c2e48dc38264076a55d1a3d31724bacc2c17`,
corpus digest `sha256-v1:1ab8ca9a16cc50c357b39e1571c73b1e6674752737c444bd8b58d3f2aab81dfa`.

Пять real-кейсов в этой исторической съёмке получили проверенные offline snapshots сообщений точной
`source.revision` из `review/eval/pr-commit-messages.json`, а не восстановленный
полный исторический PR commit list. Все 19 synthetic-кейсов получили пустые commit
tuples; SEC-05 исключён. Patch/pre-image/truth не менялись. Все 24 raw ответа и
manifest скопированы целиком byte-for-byte; SHA-256/размер каждого ответа сверены
с safe metadata, оба manifest идентичны. Final validation: 24/24; два offline JSON
отчёта byte-identical, `warnings=[]`; отчёт после удаления validator diagnostics
byte-identical live safe report.

| Метрика | Измеренный raw baseline |
| --- | --- |
| Validity | 29.2% (7/24 первых ответов) |
| Micro TP / FP / FN | 2 / 2 / 17 |
| Precision | 50.0% (2/4 predictions) |
| Recall | 10.5% (2/19 truths) |
| Critical Recall | 40.0% (2/5 critical truths) |
| Verdict agreement | 16.7% (4/24 кейсов) |
| Severity mismatches | 1/2 matched findings: LOG-02, predicted high / truth critical |

| Категория | TP / FP / FN | Precision | Recall |
| --- | --- | --- | --- |
| security | 1 / 1 / 3 | 50.0% (1/2) | 25.0% (1/4) |
| correctness | 1 / 1 / 4 | 50.0% (1/2) | 20.0% (1/5) |
| performance | 0 / 0 / 5 | Не определена (0 predictions) | 0.0% (0/5) |
| readability | 0 / 0 / 5 | Не определена (0 predictions) | 0.0% (0/5) |

Невалидны 17/24 первых ответов: только custom-rule attribution prefix — 8;
prefix + `start_line >= line` — 4; prefix + range + severity/confidence ordering — 2;
только range — 3. Нет пустых/отсутствующих первых ответов или JSON parse failures.
Их predictions не интерпретируются; истины остаются FN, кейсы не исключаются из
знаменателя. Validity real — 0/5, synthetic — 7/19; по классам security 2/4,
resource 1/5, logic 1/5, syntax 1/5, clean 2/5. Полные case IDs, class counts,
проверка artifact hashes и сравнение с историческими Mistral/Nemotron измерениями
даны в [README корпуса](../../test-prs-dataset/README.md#recorded-mistral-baseline--2026-10-13).

Исторический Mistral 2026-10-07: validity 5/24, TP/FP/FN 1/1/18, precision 1/2,
recall 1/19, critical recall 1/5, verdict agreement 2/24. Это два отдельных захвата
с разными static inputs; различия не доказывают причинное улучшение от commit
context. Ручной semantic/Hallucination Rate oracle не выполнялся; raw baseline
не устанавливает качество production verifier/final output и целевые quality gates.

Экспорт содержит configured цены USD/миллион input/cache-read/output tokens:
primary `0.56125 / 0.56125 / 2.35725`, fallback `0.2245 / 0.2245 / 0.449`;
nominal fast policy limit USD 0.50/кейс (USD 12 для 24 кейсов), 4 calls/attempt,
90s timeout. Полные настройки сохранены в manifest. Actual spend, billed-token
totals и остаток баланса в artifacts отсутствуют; они не выводятся из policy limits.

## 3.2.2. Зафиксированный PR-prefix raw baseline #82 (2026-10-14)

Исторический capture, заменённый текущим post-rebase baseline §3.2.3; original
raw bytes/manifest доступны на [revision bf09117](https://github.com/larchanka-training/dmc-268-api-t6/tree/bf0911713b3f530add49443b5b13df5720e6521e/test-prs-dataset/responses).

[Actions run 38560319651, attempt 1](https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/38560319651)
снял все 24 первых raw ответа на frozen source SHA `5173792fd2997d5401aed4b5787d536254249a26`;
начало `2026-10-14T11:58:17.004986Z`. Один согласованный платный capture,
engine `fast`, primary `mistral-small-4`, configured fallback `mistral-small-3.2-24b`,
configured provider `eurouter`, raw export включён. Пять real cases получают
проверенные API-order PR prefixes: CLEAN-03/LOG-03/SEC-04 по 1 сообщению,
RES-03/SYN-04 первые 20 плюс `[Additional commits may be omitted]`.
Все 19 synthetic histories пусты; SEC-05 исключён, corpus/truth не менялись.

Prompt v2 SHA-256 `9177e358c23d1a7cd8987cbf667cf8b705c2afb27eaf8b2237480e4531431d75`.
Static digest `sha256-v1:09c0c809ca0469f300c50c281848edded260fdbe3373d414e294bbb39c012736`,
corpus digest `sha256-v1:1ab8ca9a16cc50c357b39e1571c73b1e6674752737c444bd8b58d3f2aab81dfa`.
Все первые calls `answer`, модель `mistral-small-4`, serving-provider label `mistral`
(digest `sha256-v1:8e1af0131864f4488dc577f497afcf5c87b74b237e86f0322abf99e8c2ef018a`).
Gateway statuses `accepted`, paid-metadata errors отсутствуют,
`baseline_publishable=true`, nonpublishable list пуст. Все 25 файлов импортированы
byte-for-byte; hashes/sizes 24 ответов совпали с safe metadata, оба manifest
идентичны, SHA-256 `264dde15691f1fba3bdea64d81276c1d644c7820628e8a29e1a64860b4c94673`.
Final corpus 24/24, два replay JSON byte-identical/`warnings=[]`; redacted offline
report byte-identical live safe report. Поздние repair/fallback ответы не подставлялись.

| Метрика | Измеренный PR-prefix raw baseline |
| --- | --- |
| Validity | 33.3% (8/24) |
| Micro TP / FP / FN | 2 / 1 / 17 |
| Precision | 66.7% (2/3 predictions) |
| Recall | 10.5% (2/19 truths) |
| Critical Recall | 40.0% (2/5) |
| Verdict agreement | 16.7% (4/24) |
| Severity mismatches | 2/2: LOG-02/LOG-05, predicted high / truth critical |

Security TP/FP/FN 0/0/4; correctness 2/1/3; performance 0/0/5; readability 0/0/5.
Validity real 0/5, synthetic 8/19. Невалидные 16 первых ответов остаются в знаменателе;
их predictions не интерпретируются, truths остаются FN. Полные case IDs/class
counts приведены в [README корпуса](../../test-prs-dataset/README.md#recorded-mistral-baseline--2026-10-14).
Сравнение с исторической snapshot съёмкой §3.2.1 не является controlled experiment
и не доказывает эффект commit context. Manual semantic/Hallucination Rate oracle
не выполнялся; production verifier/final-output quality оцениваются отдельно.

Configured цены/fast policy совпадают с §3.2.1 и полными настройками manifest:
nominal USD 0.50/кейс (USD 12 для 24), 4 calls/attempt, 90s timeout. Actual spend,
billed-token totals и остаток баланса отсутствуют в artifacts.

## 3.2.3. Зафиксированный post-rebase raw baseline #82 (2026-10-15)

Исторический baseline; следующая полная съёмка v3 — §3.2.4 (20.10.2026), текущий baseline v5 — §3.2.5 (28.10.2026).
Original raw bytes и manifest сохранены [на pre-import revision](https://github.com/larchanka-training/dmc-268-api-t6/tree/47f9add7c4df2b7c32db0091d39296cdd2e4810c/test-prs-dataset/responses).

[Actions run 38701845865, attempt 1](https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/38701845865)
снял все 24 первых raw ответа на frozen source SHA `a1cc6229c6a6fdba65ecb640c60ca823e262a6aa`;
начало `2026-10-15T07:53:56.845426Z` (09:53:56 Europe/Warsaw). Это один дополнительно
разрешённый платный capture после rebase на main `f27d4c2ccd5854ac736ad8c0ecc1fe30d3b08140`:
merged #149 изменил pinned parser/schema ReviewOutput. Prompt, frozen API-order
PR prefixes и corpus/truth не менялись. Engine `fast`, primary `mistral-small-4`,
configured fallback `mistral-small-3.2-24b`, provider `eurouter`, raw export включён.
Real prefixes: CLEAN-03/LOG-03/SEC-04 по 1 сообщению, RES-03/SYN-04 первые 20 плюс
`[Additional commits may be omitted]`; 19 synthetic histories пусты, SEC-05 исключён.

Prompt v2 SHA-256 `9177e358c23d1a7cd8987cbf667cf8b705c2afb27eaf8b2237480e4531431d75`.
Static digest `sha256-v1:77d46f9c731dc26aded94c7ec60cb860314fe7835e6041a318d63b8ddc690449`,
corpus digest `sha256-v1:1ab8ca9a16cc50c357b39e1571c73b1e6674752737c444bd8b58d3f2aab81dfa`.
Все 24 первых calls — `answer`, primary `mistral-small-4`, serving-provider label
`mistral` (digest `sha256-v1:8e1af0131864f4488dc577f497afcf5c87b74b237e86f0322abf99e8c2ef018a`).
Gateway statuses `accepted`, paid-metadata errors отсутствуют,
`baseline_publishable=true`, nonpublishable list пуст. Все 25 файлов импортированы
вместе byte-for-byte: hashes/sizes ответов совпали с safe metadata, raw/safe
manifest идентичны, SHA-256 `c7f52e42004f2d1149b9d3dd80956eea1d3e17cee9fa93ff0e5625686c0694df`.
Final corpus 24/24; два replay JSON byte-identical, `warnings=[]`, redacted offline
report byte-identical live safe report. Repair/fallback не подменяют первый ответ.

| Метрика | Исторический raw baseline 2026-10-15 |
| --- | --- |
| Validity | 29.2% (7/24) |
| Micro TP / FP / FN | 2 / 2 / 17 |
| Precision | 50.0% (2/4 predictions) |
| Recall | 10.5% (2/19 truths) |
| Critical Recall | 40.0% (2/5) |
| Verdict agreement | 20.8% (5/24) |
| Severity mismatches | 1/2: LOG-02, predicted high / truth critical |

Security TP/FP/FN 1/1/3, precision 1/2, recall 1/4; correctness 1/1/4,
precision 1/2, recall 1/5; performance и readability по 0/0/5, precision не определена,
recall 0/5. Validity real 1/5, synthetic 6/19. Все 17 невалидных первых ответов
остаются в знаменателе: predictions не интерпретируются, truths остаются FN.
Case IDs и распределение по классам — [README корпуса](../../test-prs-dataset/README.md#recorded-mistral-baseline--2026-10-15).
§3.2.1 и §3.2.2 сохраняют исторические capture/метрики/digests. Сравнение разных
captures не является controlled experiment и не доказывает качество или эффект
изменения parser/schema/commit context. Manual semantic/Hallucination Rate oracle
не выполнялся; production verifier/final-output quality оцениваются отдельно.

Effective settings совпадают с предыдущей согласованной конфигурацией (§3.2.1/§3.2.2):
nominal USD 0.50/кейс (USD 12 для 24), 4 calls/attempt, 90s timeout, полные
настройки в manifest. Configured цены/лимиты не измеряют actual spend,
billed-token totals или баланс — этих величин в artifacts нет.

## 3.2.4. Зафиксированный v3 raw baseline #178 (2026-10-20)

Исторический baseline; текущая съёмка `review.system.v5` — §3.2.5 (28.10.2026).
Original raw bytes и manifest сохранены [на source revision съёмки v5](https://github.com/larchanka-training/dmc-268-api-t6/tree/973218054ef2c9a32f1e75a955284323196ffafd/test-prs-dataset/responses).

[Actions run 39443263258, attempt 1](https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/39443263258)
снял все 24 первых raw ответа на source `47f9add7c4df2b7c32db0091d39296cdd2e4810c`,
ветка `feat/178-first-answer-format`, начало `2026-10-20T14:53:06.772709Z`
(16:53:06 Europe/Warsaw). После публикации и remote verification candidate пользователь
разрешил «сделай 1 прогон» на предложенные nominal USD 12 / 24 кейса / прежнюю
пару моделей / raw export. Выполнен ровно один workflow без rerun.
Source включает merged #184 (#177 часть 2) и #133, main на момент capture —
`2326273e6ab7fcea89d1cf3b94b88a4be66ca5e9`. Пользователь осознанно выбрал запуск
до pending pinned-изменений #230/#229/#227; их последующий merge может дать drift,
новый платный прогон этим решением не разрешён. Required CI replay на main после
merge выполнен: [run 39594593880, job 119203652386](https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/39594593880/job/119203652386)
на `8a621798d01bd72a5ce6c343addc0d00661af724`, шаг «Gold corpus and recorded replay»:
24 кейса, строк `Warning:` нет.

Engine `fast`, configured provider `eurouter`, primary `mistral-small-4`, fallback
`mistral-small-3.2-24b`, raw export включён. Corpus/truth и frozen PR prefixes не
менялись: 19 synthetic + 5 real, SEC-05 исключён. Prompt `review.system.v3`, SHA-256
`eb851e7fc2329eb7bdff015ec0fda945c46111ec5c884934ed6f6d6803883452`.
46 pins, static digest
`sha256-v1:387834e02e34318527ee2c48e25e2fb63743b65a6b33a5739c25c7b877d8ca3a`;
corpus digest
`sha256-v1:1ab8ca9a16cc50c357b39e1571c73b1e6674752737c444bd8b58d3f2aab81dfa`.
Все 24 first calls — answer/primary/mistral-small-4, serving-provider label mistral,
gateway accepted, paid-metadata errors false; publishable=true, nonpublishable IDs
пусты. Это проверка целостности, не порог качества и не число repair-вызовов.

Полные 25 файлов импортированы byte-for-byte после проверки safe/raw artifacts
`eval-live-39443263258-1` и `eval-live-raw-39443263258-1`. Все hashes/sizes совпали;
raw/safe manifest SHA-256
`1a6a2c6aea7bdea6026c592ef9e59c62823aefec7eac0279f125ff09d0e161a2`.
Final corpus validation 24/24, два offline report byte-identical, `warnings=[]`;
после удаления только validator diagnostics offline совпадает с live safe report
byte-for-byte. Raw-first validity измерена до нормализации/repair/fallback и verifier.

| Метрика | Исторический 2026-10-15 | Исторический 2026-10-20 |
| --- | --- | --- |
| Validity | 29.2% (7/24) | 41.7% (10/24) |
| Micro TP / FP / FN | 2 / 2 / 17 | 3 / 5 / 16 |
| Precision | 50.0% (2/4) | 37.5% (3/8) |
| Recall | 10.5% (2/19) | 15.8% (3/19) |
| Critical Recall | 40.0% (2/5) | 40.0% (2/5) |
| Verdict agreement | 20.8% (5/24) | 16.7% (4/24) |
| Severity mismatches | 1/2 matched | 2/3 matched: LOG-02 high/critical, LOG-04 high/medium |

| Category | TP | FP | FN | Precision | Recall |
| --- | --- | --- | --- | --- | --- |
| security | 1 | 0 | 3 | 100.0% (1/1) | 25.0% (1/4) |
| correctness | 2 | 5 | 3 | 28.6% (2/7) | 40.0% (2/5) |
| performance | 0 | 0 | 5 | undefined | 0.0% (0/5) |
| readability | 0 | 0 | 5 | undefined | 0.0% (0/5) |

| Case class | Validity | TP / FP / FN | Verdict agreement |
| --- | --- | --- | --- |
| security | 1/4 | 1 / 1 / 3 | 1/4 |
| resource | 2/5 | 0 / 2 / 5 | 0/5 |
| logic | 2/5 | 2 / 0 / 3 | 1/5 |
| syntax | 2/5 | 0 / 0 / 5 | 0/5 |
| clean | 3/5 | 0 / 2 / 0 | 2/5 |

Real validity 1/5, synthetic 9/19. 14 невалидных ответов остаются в знаменателе,
predictions не интерпретируются, truths остаются FN. Пересекающиеся причины:
27 attribution omissions в 12 кейсах, 14 equal ranges в четырёх, один order case;
reversed ranges, empty/missing answers, JSON parse и другие ошибки отсутствуют.
Все case IDs и непересекающиеся группы — [README корпуса](../../test-prs-dataset/README.md#recorded-mistral-baseline--2026-10-20).

Результаты смешанные: validity/TP/recall выросли, FP выросли, precision и verdict
agreement снизились, critical recall прежний. Весь cohort сохранён, без
cherry-picking или повторной съёмки ради улучшения метрик. Разные captures не
являются controlled experiment; semantic improvement/Hallucination Rate,
production verifier/final-output quality и repair/latency/cost saving не измерены.
Effective settings полностью совпадают с историческим manifest (§3.2.3), включая
nominal USD 0.50/кейс (USD 12 для 24), 4 calls/attempt и 90s timeout.
Actual spend, billed tokens и баланс artifacts не сообщают. Verifier в этом legacy
eval не запускается; pins для неиспользуемого verifier не добавлены.

## 3.2.5. Зафиксированный v5 raw baseline #144 (2026-10-28)

[Actions run 40774280790, attempt 1](https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/40774280790)
снял все 24 первых raw ответа на source `973218054ef2c9a32f1e75a955284323196ffafd`
(тогдашний head `main`), начало `2026-10-28T21:43:37.394394Z` (22:43:37 Europe/Warsaw).
Это единственная платная пересъёмка #130 R15, которая переводит записанный replay и
eval-baseline с `review.system.v3` на `review.system.v5` (#144 AC 8). Техлид разрешил
один платный capture; выполнен ровно один workflow без rerun и подмены кейсов.

Inputs: `review_prompt=v5`, `policy_arm=none`, `corpus=main`, `export_raw_responses=true`,
model пуст (`vars.LLM_MODEL`). Engine `fast`, configured provider `eurouter`, primary
`mistral-small-4`, fallback `mistral-small-3.2-24b`. Corpus/truth и frozen PR prefixes
не менялись: 19 synthetic + 5 real, SEC-05 исключён. Prompt `review.system.v5`, SHA-256
`3bd7d39939b60d0c16b08090a4d3e5f0cbdb8a731fa4bde91bd6570546e085c9`. Плечо `none` — путь
snapshot без `.review/rules.md` (`policy_arm_digest` null): запрос несёт закреплённые
service defaults, AGENTS.md в кейсах main-корпуса нет; ответы проверяются как
ReviewOutput v2 (`evidence_refs`, без префикса атрибуции v1). RAG в eval нет (#146):
индекса нет, поэтому секция `<retrieved_excerpts>` пуста в каждом запросе — форма
продакшна с выключенным тумблером RAG. Стадии verifier в eval нет (#217, без
исполнителя): качество verifier и final output не измеряется.
52 pins, static digest
`sha256-v1:7b8bba23a40bf167fac3d441434896e3417332bccb200b21ae71ff8a7f838508`;
corpus digest
`sha256-v1:1ab8ca9a16cc50c357b39e1571c73b1e6674752737c444bd8b58d3f2aab81dfa`.
Все 24 first calls — answer/primary/mistral-small-4, serving-provider label mistral,
gateway accepted, paid-metadata errors false; publishable=true, nonpublishable IDs
пусты. Каждый кейс — ровно один платный вызов `review`: repair и fallback не
вызывались. Это проверка целостности, не порог качества.

Manifest впервые хранит usage каждого платного вызова по журналу шлюза (#163):
24 вызова, 242 989 входных токенов (из них 160 504 cache read), 9 068 выходных,
`cost_usd` 0.022242 — стоимость от провайдера, если ответ её нёс, иначе configured
цена × токены; это не billed spend и не остаток баланса.

Полные 25 файлов импортированы byte-for-byte после проверки safe/raw artifacts
`eval-live-main-none-40774280790-1` (ID `12854471057`) и
`eval-live-raw-main-none-40774280790-1` (ID `12854221376`). Все hashes/sizes совпали;
raw/safe manifest SHA-256
`bb00e2a025524add39721170d7a78b1fb73bb5679c75c3ffcdb2d853b6f06a76`.
Final corpus validation 24/24, два offline report byte-identical, `warnings=[]`;
после удаления только validator diagnostics offline совпадает с live safe report
byte-for-byte. Raw-first validity измерена до нормализации/repair/fallback и verifier.
Required CI replay на main после merge выполнен: [run 40783827466, job 123151914272](https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/40783827466/job/123151914272)
на `70261a1c0f39af25903d40d35b294d638b9fbd19`, шаг «Gold corpus and recorded replay»:
24 кейса, строк `Warning:` нет.

| Метрика | Исторический 2026-10-20 (v3) | Текущий 2026-10-28 (v5) |
| --- | --- | --- |
| Validity | 41.7% (10/24) | 87.5% (21/24) |
| Micro TP / FP / FN | 3 / 5 / 16 | 5 / 13 / 14 |
| Precision | 37.5% (3/8) | 27.8% (5/18) |
| Recall | 15.8% (3/19) | 26.3% (5/19) |
| Critical Recall | 40.0% (2/5) | 80.0% (4/5) |
| Verdict agreement | 16.7% (4/24) | 37.5% (9/24) |
| Severity mismatches | 2/3 matched | 3/5 matched: LOG-02 high/critical, LOG-04 high/medium, LOG-05 high/critical |

| Category | TP | FP | FN | Precision | Recall |
| --- | --- | --- | --- | --- | --- |
| security | 2 | 0 | 2 | 100.0% (2/2) | 50.0% (2/4) |
| correctness | 3 | 8 | 2 | 27.3% (3/11) | 60.0% (3/5) |
| performance | 0 | 0 | 5 | undefined | 0.0% (0/5) |
| readability | 0 | 5 | 5 | 0.0% (0/5) | 0.0% (0/5) |

| Case class | Validity | TP / FP / FN | Verdict agreement |
| --- | --- | --- | --- |
| security | 4/4 | 2 / 5 / 2 | 2/4 |
| resource | 4/5 | 0 / 5 / 5 | 0/5 |
| logic | 4/5 | 3 / 0 / 2 | 2/5 |
| syntax | 4/5 | 0 / 1 / 5 | 1/5 |
| clean | 5/5 | 0 / 2 / 0 | 4/5 |

Real validity 2/5, synthetic 19/19: все три невалидных ответа (LOG-03, RES-03, SYN-04) —
real-кейсы; ни один прежде валидный кейс не стал невалидным. Они остаются в
знаменателе, predictions не интерпретируются, truths остаются FN. Причина у всех
трёх одна: 13 equal ranges (3/3/7); reversed ranges, attribution, empty/missing
answers, JSON parse и другие ошибки отсутствуют. SYN-04 ещё ставит `info` перед
`high`, но ReviewOutputV2 проверяет порядок только после полей, поэтому validator
эту ошибку не печатает. Пропущенная critical truth — RES-05.
Все case IDs и группы — [README корпуса](../../test-prs-dataset/README.md#recorded-mistral-baseline--2026-10-28).

Съёмки различаются не только промптом: v3 — legacy envelope и контракт v1 с префиксом
атрибуции, v5 — envelope snapshot (плечо `none`) и контракт v2 с `evidence_refs` без
префикса. 27 attribution omissions v3 (12 кейсов) под v2 невозможны, поэтому рост
validity в основном даёт смена контракта, а не лучшее соблюдение формата; ссылки
evidence против manifest вызова replay не проверяет. Результаты смешанные: validity,
TP, recall, critical recall и verdict agreement выросли, FP выросли (5 → 13, readability
0 → 5), precision снизилась. Весь cohort сохранён, без cherry-picking или повторной
съёмки. Разные captures не являются controlled experiment; semantic
improvement/Hallucination Rate, production verifier/final-output quality и
repair/latency/cost saving не измерены. Effective settings полностью совпадают с
§3.2.4, включая nominal USD 0.50/кейс (USD 12 для 24), 4 calls/attempt и 90s timeout.
