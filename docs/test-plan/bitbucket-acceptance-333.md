# TEST_PLAN: Bitbucket Cloud acceptance (#333)

Вынесено из [TEST_PLAN, раздел Bitbucket Cloud acceptance](../TEST_PLAN.md#bitbucket-cloud-acceptance-333) в #380. Методология и общие нормы — в TEST_PLAN.

## Bitbucket Cloud acceptance (#333)

Browser API сверяется с `contracts/openapi.yaml` в `tests/test_openapi_contract_bitbucket.py`; входящий provider webhook описан отдельно в [`BITBUCKET_WEBHOOK.md`](../BITBUCKET_WEBHOOK.md). PostgreSQL/ASGI набор `tests/test_bitbucket_*` проверяет UUID и portal workspace изоляцию, private repo connect/disconnect, ключи и секреты, HMAC raw bytes, durable receipt/replay, один Run на PR/head, pinned diff/source, worker, RAG, inline/summary markers и commit status. `tests/test_deploy_staging_bundle.py` и `tests/test_deploy_staging_rollback.py` проверяют CI bundle → 0600 env-файлы → контейнеры/rollback. Запускать с реальным `TEST_DATABASE_URL`, иначе skipped PG тесты не являются доказательством. UI `pnpm test` проверяет выбор workspace и границу Zod; `pnpm --dir docs-site check-types && pnpm --dir docs-site build` проверяет публичную инструкцию.

Отдельный live gate на disposable private Bitbucket Cloud repo/PR: зафиксировать URL PR,
head SHA и deployed backend/UI SHA; подключить репозиторий к portal workspace, вручную
настроить repository webhook с одноразовым секретом, доставить подписанные PR/push
события, дождаться succeeded Run, проверить inline comments, ровно один summary,
commit status и число публикаций после повтора доставки. Native применяемое
предложение нужно подтвердить реальным API-запросом и записать, показывает ли
Bitbucket UI действие Apply для созданного предложения: текстовый fenced patch остаётся
ручным fallback и не закрывает AC4. В отчёт записывать только санитизированные
идентификаторы/счётчики, без токена и секрета. Полные backend/UI gates обязательны
на итоговом diff.

Проверки выполнять в выбранном portal workspace на приватном UUID locator; участник
видит подключение, посторонний — нет. Runtime-токену нужны
`read:repository:bitbucket`, `read:pullrequest:bitbucket` и `read:user:bitbucket`:
проверить их на private repo/PR GET, GET /user, inline comment POST и commit-status POST/PUT.
Токен с прежними правами перевыпустить и обновить в connection. Без второго scope и connect,
и `PUT credential` возвращают 422; после неудачного PUT `credentialGeneration` не
меняется. Проверить URL и имена защищённых staging-переменных, не раскрывая keyring;
backend/UI должны быть развернуты с зафиксированными SHA и миграцией.

До отключения репозитория провести следующие provider-пробы:

- Подписать все пять событий `repo:push`, `pullrequest:created`, `updated`,
  `fulfilled`, `rejected`. Неверная подпись не создаёт receipt/Run и не переводит
  binding из `awaiting_webhook` в `active`; первая верная переводит. Реальные merge и
  decline дают соответствующие receipts, закрывают PR и отменяют активный Run без
  нового ревью. Пять wire events из ответов connect и ротации webhook secret должны
  совпасть с настройкой webhook; старую трёхсобытийную настройку пересоздать.
  Два signed update с одинаковым `updated_on`, но разными head не
  теряют новый head; `diagnostics:ping` без `repository.uuid` отвечает 400 без receipt.
- Повторить доставку до и после перезапуска worker: на PR/head остаются один Run,
  прежние inline markers, одна summary и один tenant-scoped status key. Проверить
  pinned source/destination SHA на private fork, недоступном runtime-токену напрямую;
  отдельный ограниченный отказ для недоступного fork допустим только тогда, когда API
  репозитория назначения не может установить head источника. Повторная доставка или
  рестарт не должны откатывать более новый Run того же head. После удаления fork
  branch (`source.commit=null`) merge/decline не запускают ревью.
  При ротации токена смена автора статуса должна дать явную ошибку владения, а не
  сменить ключ. Проверить детерминированный ключ `AI-REVIEW-` + 30 hex (≤40 символов)
  и portal Run URL на закреплённом head. После disconnect новые доставки/очередная
  работа отклоняются, соседняя привязка продолжает работать.
- Rename+edit сохраняет old/new пути и inline координаты. Сверить отсутствующий
  diffstat, несколько страниц diffstat/commits, merge-base и закреплённый base SHA
  после движения destination tip. Ретаргет или новый source head не публикует
  устаревшее ревью; после ретаргета старый `INPROGRESS` становится `STOPPED` под тем
  же ключом на закреплённом head, в том числе после начатой попытки. Отдельно
  проверить draft toggle, skipped/failed → `STOPPED`, description, portal target URL,
  merge checks и 403 при попытке записать status в недоступный source fork. Принятое
  ограничение P3 №52: `INPROGRESS` на вытесненном старом head не меняется на `STOPPED`.
- Раздельно записать число запросов, `X-RateLimit-*` и 429/`Retry-After` для
  `/src/{commit}/` и `/src/{commit}/{path}`, а также для обязательных diff/blob и
  необязательных context-чтений. Проверить, один бакет квот у listing/raw или разные,
  и обе формы `Retry-After`: секунды и HTTP-date. Исчерпание context оставляет
  diff-only ревью при доступных обязательных данных; полный бюджет даёт явный код
  ошибки. Не создавать нагрузку ради искусственного 429 на общем репозитории.

В таблице доказательств указать migration head и public API origin вместе с URL
одноразового PR, UUID, deployed SHA, статусом/временем доставок, Run ID,
счётчиками, координатами и санитизированной формой REST-пробы native
suggestion; не сохранять токен, secret, signed payload или ciphertext. Публичные
read-only UUID-пробы 2026-10-26 подтверждают лишь форму локатора; доступа к
одноразовому приватному репозиторию и токену пока нет. Локальные
MockTransport/PostgreSQL тесты не закрывают live gate.

Для RAG-индекса отдельно проверить восстановление и инкремент. Если индекс
одноразового репозитория был неполным до исправления `diffstat`, после выкладки
фикса выключить и включить RAG: ожидать full build с `reason=no_index` и все
файлы текущего head. Затем добавить в `main` два индексируемых probe-файла и
дождаться их появления в индексе. Следующим коммитом добавить третий файл,
удалить один probe-файл и переименовать другой, не меняя `.review/rules.md`,
`go.mod` или `go.work`. Проверить `mode=incremental reason=-`, ненулевые
`written` и `deleted`, индексированный SHA нового head, наличие новых путей
и отсутствие удалённого и старого путей. Повторная сборка того же head
(`UNCHANGED`) и full build после RAG off/on сами по себе не доказывают
исправление инкремента.
