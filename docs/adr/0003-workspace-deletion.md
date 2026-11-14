# ADR 0003: Удаление workspace: баланс, подписки, данные и связь с GitHub App

| | |
|---|---|
| Статус | **Предложено.** Принимает техлид approve PR; до этого правила ниже не канон |
| Дата | 2026-11-23 (обновлено по результатам ревью архитектора и сверки с кодовой базой `main` и ADR 0002) |
| Владелец | Задача [#545](https://github.com/larchanka-training/dmc-268-api-t6/issues/545) (milestone `Спринт 5`) |
| Задача | [#545](https://github.com/larchanka-training/dmc-268-api-t6/issues/545), эпик [#369](https://github.com/larchanka-training/dmc-268-api-t6/issues/369) |
| Входные решения | [ADR 0002](0002-billing-monetization.md) (#370, PR #514 / #587: §5 виды и гранты `credit_ledger`, §5.2 `balance_holds`, §6 авто-пополнение и согласия, §9 право отказа 14 дней, §10.3 обработка событий Stripe, §10.4 идемпотентность Stripe, §11.2 БД-роль `Owner`, §15 Customer Portal [#372], §16 порядок внедрения); #457 (`runs.data_purged_at`); #496 (`installation.deleted`); #113 / #511 (rebinding); #553 (`auth_agent_tokens`, `auth_agent_device_flows`) |
| Изменяемый канон | [SYSTEM_DESIGN](../SYSTEM_DESIGN.md) §1 Р-15, §6.4, §6.5, §6.9, §10, §11, §12, §13, [BACKEND_ARCHITECTURE](../BACKEND_ARCHITECTURE.md), [ADR 0002](0002-billing-monetization.md) §4.3, §5, §10.3, §11.1; карта изменений — в §9 |

Документ проектный: код, миграции и зависимости им не меняются, реализация идёт
отдельными задачами api (#550, #551) и ui (ui#300, ui#301) после мержа зависимостей биллинга и ролей (§12).

---

## 1. Контекст (сверено с `main` и ADR 0002)

В ADR 0002 (§11.1, строка #545) действие «Удалить workspace» было исключено из матрицы ролей до
отдельного решения о правах, балансе, подписке Stripe, GDPR/152-ФЗ и вебхуке `installation.deleted`.

Опора на фактическое состояние `main` и принятого канона ADR 0002:

- **`workspaces` (`app/modules/workspaces/infrastructure/models.py`):** на `main` хранит `id`,
  `name`, `daily_budget_usd`, `created_at`, `updated_at`; soft-delete-полей (`status`, `deleted_at`,
  `deletion_enqueued_at`, `data_purged_at`) пока нет (вводятся задачей `#550`).
- **Роли (§11.2 ADR 0002):** роль `Owner` — **роль в БД (не claim в JWT)**, её вводит
  задача `#373`. До мержа `#373` БД-роли `Owner` не существует, следовательно удалить workspace некому.
- **Финансовые таблицы (`app/modules/billing/infrastructure/models.py`, `enums.py`):**
  - `payments`: `id`, `workspace_id`, `provider`, `provider_checkout_id`, `provider_payment_id`,
    `amount_usd`, `status` (`pending | paid | failed | expired`), `created_at`, `updated_at`, `paid_at`.
  - `credit_ledger`: `id`, `workspace_id`, `kind` (`top_up | usage_debit | adjustment` + целевые
    виды ADR 0002 §5: `subscription_grant`, `promo`, `expiry`, `refund`, `run_settlement`),
    `amount_usd`, `usage_event_id` (FK на `usage_events.id`), `payment_id` (FK на `payments.id`),
    `reason`, `created_at`.
  - Гранты, сроки годности и удержания прогонов (`balance_holds`), а также `billing_consents` (append-only)
    добавляются задачами эпика `#369`.
- **Данные прогонов, кэши и политики:**
  - Полный инвентарь всех таблиц схемы на момент принятия ADR и правила их утилизации зафиксированы в едином нормативном реестре в [§6](#6-матрица-данных-сроки-хранения-gdpr-и-механизм-очистки).
- **`installation.deleted` и `/api/auth/me` (`SYSTEM_DESIGN.md` §6.9, #496, #113 / #511):**
  `installation.deleted` на `main` выставляет `installation_available = false`, удаляет
  `github_user_repository_access`, сохраняя сам `Workspace` и `provider_installations`.

---

## 2. Решение кратко

1. **Авторизация:** удалить workspace может **только пользователь с БД-ролью `Owner`** (ADR 0002
   §11.2, `#373`). Workspace в «режиме без Owner» сначала требует назначения владельца (`#373`).
   Реализация удаления заблокирована до мержа `#373`.
2. **Двухэтапное подтверждение:** модал требует посимвольного ввода точного имени `workspace.name`.
   Эндпоинт `DELETE /api/workspaces/{id}` возвращает **`202 Accepted`** при асинхронном удалении
   (перевод в tombstone и постановка задачи очистки в очередь). Повторный вызов к уже удалённому
   workspace возвращает строго **`404 Not Found`**.
3. **Аннулирование баланса и 14-дневный возврат:** положительные свободные остатки грантов
   аннулируются бухгалтерской проводкой `kind = 'adjustment'` со строгим основанием
   `reason = 'workspace_deleted'`. При этом проводка аннулирования не входит в расчёт использованных
   услуг. Право на 14-дневный возврат сохраняется через поддержку (ADR 0002 §9).
4. **Stripe и VCS вне транзакции БД (`AGENTS.md`):** транзакция БД только переводит `workspaces` в
   tombstone (`status = 'deleted'`, `deleted_at = now()`), выключает авто-пополнение (#513),
   выставляет сигнал отмены прогонам и фиксирует `deletion_enqueued_at = now()`. Сетевая публикация задачи
   в RabbitMQ выполняется строго **после `commit()`**. Внешние сетевые вызовы (отмена подписки Stripe,
   отзыв методов оплаты, отзыв токенов провайдеров) выполняются фоновым воркером `WorkspaceDeletionWorker` (#551).
5. **Очистка данных и приоритет финансового аудита:**
   - Данные кода клиентов очищаются специализированным механизмом `purge_workspace_run_data(workspace_id)`
     (#551): исходные патчи `code_change_diffs`, манифесты и полезная нагрузка контекста `context_*`,
     ответы действий `run_action_responses` и политики `run_policy_*` очищаются немедленно (0 дней) после завершения активных прогонов.
   - Строки `repositories`, `rule_versions`, `runs`, `usage_events` и интеграций сохраняются как
     обезличенные tombstone для сохранения ссылочной целостности финансового журнала `credit_ledger`
     в течение **целевого срока 7 лет** (целевой срок фискального хранения для аудита Stripe/PCI-DSS и бухгалтерской отчётности, ожидает юридического подтверждения DPO/Legal, §11, #545).
6. **Граница с установками VCS и предотвращение воскрешения:**
   - При удалении Workspace запись `provider_installations` помечается `deleted_at = now()`.
   - Поиск привязки `link()` и логика `SqlAlchemyGitHubRepositoryRebinding` фильтруют удалённые
     воркспейсы (`workspaces.status != 'deleted'`), предотвращая доступ к tombstone.
   - Метод `link()` для установки удалённого воркспейса отказывает без 409 и без создания нового воркспейса по той же установке (#554); новая `installation.id` после переустановки App создаёт новый независимый Workspace.
7. **UX после удаления:** сброс клиентского кэша, редирект на соседний доступный workspace либо на
   онбординг `/repositories`. Прямой переход по сохранённому URL удалённого workspace отдаёт экран 404.

---

## 3. Авторизация и двухэтапное подтверждение

### 3.1. Матрица прав (дополнение к ADR 0002 §11.1)

| Действие | Owner (БД, #373) | Admin | Billing | Member | Без Owner (#373) |
|---|---|---|---|---|---|
| Удалить workspace (`DELETE /api/workspaces/{id}`) | **да** (с `confirmation_name`) | нет (`403`) | нет (`403`) | нет (`403`) | нет (`403`, сначала «Стать Owner») |

- Проверка роли `Owner` выполняется строго по таблице ролей БД (`#373`), без выведения прав из
  GitHub org permissions (`is_owner` через GitHub API запрещён по ADR 0002 §11.2).
- Вызов `DELETE /api/workspaces/{workspace_id}` требует действующего JWT и контекста авторизации `AuthScope`.

### 3.2. Процедура подтверждения

1. В настройках workspace (`/settings`, блок Danger Zone, видимый только для `role === 'owner'`)
   открывается модальное окно «Удаление воркспейса `{workspace.name}`» с предупреждением:
   > «Все данные репозиториев, правила и индексы кода будут безвозвратно удалены. Подписка Stripe отменяется без возврата за текущий период. Неиспользованный остаток баланса аннулируется (право 14-дневного возврата сохраняется через поддержку). Записи финансовых операций и метаданные прогонов сохраняются в течение 7 лет (целевой срок фискального аудита, ожидает юридического подтверждения DPO/Legal).»
2. Пользователь вводит точное имя `workspace.name`. Кнопка подтверждения активна только при точном
   посимвольном совпадении.
3. Формат запроса:
   ```http
   DELETE /api/workspaces/{workspace_id}
   Content-Type: application/json

   {
     "confirmation_name": "acme-corp"
   }
   ```
4. Контракт ответов:
   - `202 Accepted` — запрос принят, статус воркспейса переведён в `deleted`, задача фоновой очистки поставлена в очередь.
   - `422 Unprocessable Entity` (`code = 'workspace_name_mismatch'`) — передано неверное имя.
   - `403 Forbidden` — вызывающий не является `Owner` в БД.
   - `404 Not Found` — воркспейс не найден либо уже находится в статусе `deleted` (строгое поведение без развилок).

---

## 4. Баланс, гранты и 14-дневный возврат (согласовано с ADR 0002 §5, §9)

### 4.1. Аннулирование остатка в схеме `credit_ledger` и каноническая отмена прогонов

1. Перевод `workspaces.status = 'deleted'` в транзакции немедленно блокирует вебхуки, запуски, пополнения и claim задач.
2. Прогоны удаляемого воркспейса обрабатываются в строгом соответствии с жизненным циклом (SD §6.4, §6.9):
   - Прогоны в состоянии `queued` немедленно переводятся в терминальное состояние `state = 'cancelled'`, `error_code = 'workspace_deleted'`, `error_message = 'Workspace was deleted'` через канонический путь отмены (`RunRepository.cancel_run` / `run_repository.py:658-666`), выставляющий `finished_at = now()`, `cancellation_signal_requested_at` (при `attempt >= 1`) и вызывающий `notify_run_state` для оповещения UI по SSE.
   - Прогоны в активных состояниях `running` и `publishing` получают флаг кооперативной отмены `cancel_requested = true`. Прямой перевод активных прогонов в `cancelled` внутри транзакции `DELETE` запрещён: это создаёт гонку с воркером, удерживающим `lease_until`.
3. После коммита транзакции воркер фоновой очистки `WorkspaceDeletionWorker` (#551) ожидает перехода всех активных прогонов воркспейса в терминальные состояния (`succeeded`, `failed`, `cancelled`, `skipped`).
   - **Гарантия завершения ожидания (SD §6.4, :470, :481):**
     а) Реконсилер при каждом тике переводит в `cancelled` любой прогон в состоянии `queued` удаляемого воркспейса (`deletion_enqueued_at IS NOT NULL`), исключая бесконечное ожидание повторных попыток claim;
     б) Для зависших прогонов в `running` с истёкшим `lease_until` реконсилер переводит их напрямую в `cancelled` (а не в `queued`), фиксируя `error_code = 'workspace_deleted'`, `finished_at = now()`;
     в) Устанавливается предельный таймаут ожидания завершения прогонов воркером: **15 минут**. По истечении 15 минут `WorkspaceDeletionWorker` принудительно переводит любые остаточные нетерминальные прогоны воркспейса в `state = 'cancelled'`, `error_code = 'workspace_deletion_timeout'`, `finished_at = now()`, высвобождает `balance_holds` и переходит к следующим шагам биллинга и purge.
4. После того как все прогоны воркспейса стали терминальными, единая точка записи биллинга (ADR 0002 §3.4, §5.2) финализирует списания за фактически потреблённые ресурсы и высвобождает `balance_holds`.
5. Положительные свободные остатки активных грантов аннулируются отдельными проводками в `credit_ledger`:
   - `kind = 'adjustment'`;
   - `amount_usd = -free_grant_remainder` (`Numeric(12, 6)` до полного обнуления доступного остатка);
   - `reason = 'workspace_deleted'`.
6. Отрицательный баланс гранта (долг от `refund` или `dispute`, ADR 0002 §5) сохраняется в истории леджера и не закрывается фиктивными проводками.

### 4.2. Сохранение права отказа 14 дней (ADR 0002 §9)

- В таблице `payments` сохраняются метаданные `withdrawal_eligible`, `paid_at`, а также привязанные к `payment_id`
  строки списаний до момента удаления.
- **Исключение аннулирования из оказанных услуг:** списание `kind = 'adjustment'` с `reason = 'workspace_deleted'`
  **не входит** в объём «использованного» (`usage_debit`) и не признаётся полным оказанием услуг при расчёте суммы 14-дневного возврата.
- **Ручной канал возврата:** поскольку после удаления воркспейса пользовательский интерфейс и кнопка «Отказаться от покупки» (ADR 0002 §9) становятся недоступными, возврат оформляется через обращение в службу поддержки (`support@...`).
- Оператор поддержки проверяет соблюдение 14-дневного срока от `paid_at`, вычисляет неиспользованный остаток и инициирует возврат в Stripe, фиксируя операцию в `credit_ledger` строкой `kind = 'refund'`.

---

## 5. Подписка Stripe, внешние эффекты и транзакционные границы (`AGENTS.md`)

По `AGENTS.md` и `.agents/rules/backend.md` §3 ни одна DB-транзакция **не имеет права** содержать
внешний HTTP-вызов к Stripe, GitHub, GitLab или Bitbucket.

Последовательность шагов при удалении:

1. **Транзакция БД (атомарно, эндпоинт `DELETE /api/workspaces/{id}`, задача #550):**
   - Проверяет роль `Owner` в БД и совпадение `confirmation_name`.
   - Выставляет `workspaces.status = 'deleted'`, `workspaces.deleted_at = now()`.
   - Выключает настройки авто-пополнения (#513). Согласия в append-only таблице `billing_consents` не модифицируются.
   - Отменяет прогоны в очереди: для `queued` прогонов переиспользует канонический путь отмены (`RunRepository.cancel_run`), переводя в `state = 'cancelled'`, `error_code = 'workspace_deleted'`, `finished_at = now()`.
   - Для прогонов в `running` и `publishing` выставляет `cancel_requested = true` (сигнал кооперативной отмены по SD §6.4).
   - Для токенов агентов выставляет `auth_agent_tokens.revoked_at = now()` для всех токенов данного воркспейса (#553); удаляет записи `auth_agent_device_flows` для репозиториев воркспейса.
   - Помечает незавершённые Checkout-сессии пополнения в БД `payments.status = 'expired'`.
   - Помечает привязки установки `provider_installations.deleted_at = now()`.
   - Выставляет признак незавершённой очистки `workspaces.deletion_enqueued_at = now()`.
   - `commit()`.
   - **Публикация задачи (строго после commit):** публикует задачу в очередь RabbitMQ воркера `WorkspaceDeletionWorker` (#551) с ключом идемпотентности. Если публикация завершилась сетевой ошибкой или воркер упал до отправки, leader-реконсилятор гарантированно подбирает задачу.
2. **Внешние вызовы и фоновая очистка (строго после `commit`, воркер `WorkspaceDeletionWorker` #551):**
   - Доведение очистки гарантируется периодической leader-задачей реконсиляции (SD §6.4), разделяющей этапы жизненного цикла:
     1) **Основная очистка данных и биллинга:** подбирает воркспейсы с предикатом `deletion_enqueued_at IS NOT NULL AND data_purged_at IS NULL`;
     2) **Отложенные возвраты:** источником работы служат строки таблицы outbox со статусом `pending`, независимо от состояния `data_purged_at`;
     3) **Отложенная деперсонализация Stripe Customer:** подбирает воркспейсы с предикатом `customer_anonymization_due_at IS NOT NULL AND customer_anonymization_due_at <= now() AND customer_anonymized_at IS NULL`.
   - Шаги выполнения воркера #551:
     1. **Отзыв токенов VCS и интеграций:** отзыв токенов доступа GitLab (`token_ciphertext = NULL`, `credential_key_id = NULL`, `revoked_at = now()`, `intake_enabled = false`, соблюдая `ck_gitlab_connections_credential_pair` и `ck_gitlab_connections_enabled`), Bitbucket (`token_ciphertext = NULL`, `token_key_id = NULL`, `revoked_at = now()`, `intake_enabled = false`, `secret_ciphertext = NULL`, `secret_key_id = NULL`, соблюдая `ck_bitbucket_connections_token_pair` и `ck_bitbucket_repository_webhook_secrets_pair`) и прав GitHub App; дерегистрация вебхуков у провайдеров (GitLab project webhooks, Bitbucket repo webhooks).
     2. **Отмена подписки Stripe:** `stripe.Subscription.cancel(sub_id, idempotency_key=...)`.
     3. **Отвязка платежных методов:** `stripe.PaymentMethod.detach(...)` для сохранённых карт.
     4. **Погашение Checkout-сессий:** вызов `stripe.checkout.Session.expire(...)` для активных сессий у Stripe (только для сессий с `mode == 'payment'`).
     5. **Ожидание завершения прогонов:** воркер ожидает перехода всех активных прогонов воркспейса в терминальные состояния (`succeeded`, `failed`, `cancelled`, `skipped`) с таймаутом 15 минут.
     6. **Финализация биллинга:** расчет итогов прогонов единой точкой биллинга, снятие `balance_holds` и аннулирование остатка грантов проводками `kind = 'adjustment'`, `reason = 'workspace_deleted'`.
     7. **Очистка данных кода:** вызов процедуры `purge_workspace_run_data(workspace_id)` с фиксацией `runs.data_purged_at = now()`, `workspaces.data_purged_at = now()`, и установкой срока деперсонализации Customer `workspaces.customer_anonymization_due_at = now() + INTERVAL '30 days'`.
     8. **Отложенная деперсонализация Stripe Customer:** объект `stripe.Customer` сохраняется на 30 дней после закрытия окна возврата (для проведения возможных рефандов и урегулирования диспутов), после чего деперсонализируется воркером реконсиляции (#551) с фиксацией `workspaces.customer_anonymized_at = now()`.

### Обработка событий Stripe Webhook для Workspace в статусе `deleted`

Обработчики вебхуков Stripe не выполняют синхронных сетевых вызовов возврата в транзакции: при необходимости возврата создаётся задача в outbox для асинхронного проведения через `WorkspaceDeletionWorker` (#551).

| Событие Stripe | Поведение сервиса при `workspaces.status = 'deleted'` | Обоснование |
|---|---|---|
| `customer.subscription.deleted` | Игнорируется (HTTP 200) | Подтверждение отмены; воркспейс уже удалён, повторные действия не требуются. |
| `invoice.paid` | Для счетов `metadata.source != 'checkout'`: пополнение не начисляется, создаётся запись outbox на `stripe.Refund` с `idempotency_key = f"refund:invoice:{invoice_id}"`. Для счетов Checkout (`metadata.source == 'checkout'`): возврат не инициируется (только запись `provider_invoice_id`), возврат идёт по сессии Checkout | Защита от двойного возврата средств по Checkout (ADR 0002 §10.3). Возврат по абонентским счетам выполняет воркер #551. |
| `invoice.payment_failed` | Игнорируется (HTTP 200) | Фиксация неуспеха списания по уже отменённой подписке. |
| `charge.refunded` | Фиксируется в `credit_ledger` (`kind = 'refund'`) | Отражение возврата средств в бухгалтерском аудите без изменения статуса воркспейса. |
| `refund.updated` / `refund.failed` | При `refund.failed` создаётся проводка `credit_ledger` (`kind = 'adjustment'`, `reason = 'refund_failed'`) | Восстановление баланса и суточная сверка по ADR 0002 §10.3. |
| `charge.dispute.created` | Фиксируется в `credit_ledger` (`kind = 'adjustment'`, `reason = 'dispute'`) | Отражение претензии по ADR 0002 §5/§10.3 для бухгалтерского учёта. |
| `charge.dispute.closed` | При выигранном споре сторнируется проводкой `kind = 'adjustment'`, `reason = 'dispute_reversed'` | Завершение арбитража платежной системой по ADR 0002 §10.3. |
| `checkout.session.completed` | Обрабатывается строго при `mode == 'payment'` (при `mode == 'setup'` игнорируется). Платеж фиксируется как `payments.status = 'paid'`, кредиты не начисляются; создаётся запись outbox на `stripe.Refund` с `idempotency_key = f"refund:checkout:{session.id}"` и фиксацией в `credit_ledger` (`kind = 'refund'`) | Сохранение фискальной достоверности платежа с последующим полным возвратом через воркер #551 (ADR 0002 §10.3). |
| `checkout.session.expired` | Платеж маркируется `payments.status = 'expired'` | Штатное истечение неоплаченной сессии. |
| `payment_method.detached` | Игнорируется (HTTP 200) | Подтверждение отвязки способа оплаты. |
| Любое другое событие Stripe | Игнорируется (HTTP 200) | Изоляция удалённого воркспейса от фоновых событий. |

---

## 6. Матрица данных, сроки хранения, GDPR и механизм очистки

Инвентарь всех таблиц базы данных приложения на момент принятия ADR и правила их утилизации при удалении Workspace:

Принципиальный инвариант: **требования фискального аудита и ссылочной целостности базы данных имеют приоритет над каскадным удалением строк**. Внешние ключи, на которые опираются финансовые проводки, не должны становиться висячими.

| Категория сущностей / Таблицы базы данных | Действие при удалении Workspace | Срок хранения | Обоснование и нормативная база |
|---|---|---|---|
| **Токены агентов** (`auth_agent_tokens`, `auth_agent_device_flows`) (#553) | Для `auth_agent_tokens`: немедленно выставляется `revoked_at = now()`; строки сохраняются как tombstone. Для `auth_agent_device_flows`: немедленное удаление строк репозиториев воркспейса | **7 лет** (`auth_agent_tokens` для аудита, ожидает Legal) / **0 дней** (`device_flows`) | Мгновенный отзыв доступа CLI-агентов к репозиториям удалённого воркспейса. |
| **Секреты и токены интеграций** (`gitlab_connections`, `bitbucket_connections`, `bitbucket_repository_webhook_secrets`, `bitbucket_*_identities`, `bitbucket_publication_claims`) | Отзыв токенов у провайдера; обнуление секретов и ключей (`token_ciphertext = NULL`, `credential_key_id = NULL`, `token_key_id = NULL`, `secret_ciphertext = NULL`, `secret_key_id = NULL`, `revoked_at = now()`, `intake_enabled = false`). Строки сохраняются как tombstone | **7 лет** (id для FK, ожидает Legal) | `webhook_events` и `runs` имеют FK на `gitlab_connection_id`, `bitbucket_repository_id` и `bitbucket_run_origins`. Удаление строк ломает ссылочную целостность аудита. |
| **Индекс графа RAG** (`code_index_states`, `code_index_files`, `code_index_symbols`, `code_index_edges`, `code_index_imports`, `bitbucket_tree_checkpoint_pages`, `bitbucket_raw_checkpoint_blobs`, `bitbucket_published_manifests`) | Немедленное батчевое удаление (Hard Delete) | **0 дней** | Защита интеллектуальной собственности клиента (ADR 0001 §4). |
| **Правила и конвенции** (`rule_versions`, `repo_conventions`, `repo_convention_drafts`, `repository_rules_status`) | Обезличивание текстов (см. §6.1); строки сохраняются как tombstone. `repository_rules_status` удаляется явным `DELETE` в воркере #551 | **0 дней** (`repository_rules_status`) / **7 лет** (метаданные версий, ожидает Legal) | `runs.rule_version_id` имеет ограничение `NOT NULL FK -> rule_versions.id`. Удаление строк `rule_versions` нарушает FK прогонов. Строки `repositories` не удаляются через DELETE, поэтому `repository_rules_status` очищается явным `DELETE FROM repository_rules_status WHERE repository_id IN (...)`. |
| **Репозитории и привязки** (`repositories`, `provider_installations`, `code_changes`, `gitlab_project_bindings`, `bitbucket_run_origins`, `bitbucket_status_publications`) | Обезличивание названий и URL (см. §6.1); выставление `deleted_at = now()` на сущностях с soft-delete | **7 лет** (id для FK, ожидает Legal) | `runs.repository_id` имеет внешний ключ на `repositories.id`. Строки сохраняются для связности графа прогонов. |
| **Доступы и роли воркспейса** (`github_user_workspace_access`, `github_user_repository_access`, роли воркспейса `#373`, `github_installation_access_revocations`) | Немедленное удаление строк данного воркспейса (Hard Delete). Для `github_installation_access_revocations`: удаление по `provider_installation_id` связанных установок | **0 дней** | Мгновенный отзыв прав доступа пользователей к удалённому ресурсу. |
| **Вспомогательные таблицы интеграций** (`github_installation_removal_effects`, `bitbucket_push_retry_candidates`) | Не имеют `workspace_id` (привязаны к `delivery_id` / `webhook_event_id`); очищаются штатным воркером очистки вебхуков | **30 дней** | Штатный журнал дедупликации доставок. |
| **Глобальные сущности пользователей** (`github_user_profiles`, `auth_refresh_families`, `auth_refresh_sessions`, `github_user_installation_sync`) | **Не затрагиваются** при удалении воркспейса | До удаления аккаунта | Принадлежат пользователю глобально, не имеют `workspace_id`. `github_user_installation_sync` содержит счётчики поколений пользователя по `github_user_id`. |
| **Сырые вебхуки** (`webhook_events`) | Сохраняются неизменными; очистка payload не выполняется (во избежание нарушения `ck_webhook_events_payload_present`) | **30 дней** | Диагностический буфер по канону `SYSTEM_DESIGN.md` §10. Удаляются целиком воркером `webhook-worker` через 30 дней. |
| **Артефакты кода прогонов** (`code_change_diffs` [патчи], `context_payloads`, `context_manifests`, `run_action_responses`, `run_policy_documents`, `run_policy_snapshots`, `cached_file_blobs`, `review_candidate_checkpoints`) | Немедленное удаление через `purge_workspace_run_data` (#551) для всех прогонов воркспейса после их завершения с фиксацией `runs.data_purged_at = now()` | **0 дней** (исходный код клиента) | Защита исходного кода клиента. Воркер удаляет сырые файлы, диффы и тела ответов инструментов. |
| **Замечания и результаты** (`findings`, `comments`, `final_review_results`) | Обезличивание сниппетов и текстов (см. §6.1); комментарии сохраняются (содержат только id/хеши) | **0 дней** (код) / **7 лет** (аудит, ожидает Legal) | Удаление фрагментов кода при сохранении агрегированных счётчиков замечаний. |
| **Метаданные прогонов и аудит расхода** (`runs` без текста, `run_actions` без payload, `usage_events`) | Сохраняются в архиве | **7 лет** (ожидает Legal) | `credit_ledger.usage_event_id` ссылается на `usage_events.id`. Сохраняется для финансовой и биллинговой сверки. |
| **Финансовый леджер, согласия и воркспейс** (`workspaces` [`deleted`, имя обезличено], `payments`, `credit_ledger`, `billing_consents`) | Сохраняются в неизменяемом виде | **7 лет** (целевой срок фискального хранения, ожидает Legal) | Стандартный срок хранения бухгалтерских документов для финансового и налогового аудита (Stripe audit trail, #545). |
| **Каталог системных промптов** (`prompt_versions`) | Не затрагивается | Глобальный справочник | Общий версионированный справочник платформы. |

### 6.1. Спецификация обезличивания данных по колонкам

Для сохранения ссылочной целостности при очистке конфиденциальных данных воркер `purge_workspace_run_data` выполняет адресную модификацию колонок с соблюдением `NOT NULL` и `CHECK` ограничений:

| Таблица | Колонка | Тип данных и ограничения | Значение при обезличивании | Обоснование |
|---|---|---|---|---|
| `workspaces` | `name` | `VARCHAR NOT NULL` | `'Deleted Workspace ' || id::text` | Обезличивание названия воркспейса для исключения хранения ПДн (логина организации) в течение 7 лет |
| `repositories` | `full_name` | `VARCHAR NOT NULL` | `'deleted-repo-' || id::text` | Соблюдение NOT NULL при удалении названий |
| `repositories` | `web_url` | `VARCHAR NOT NULL` | `'https://deleted.local/' || id::text` | Соблюдение NOT NULL при удалении внешних ссылок |
| `provider_installations` | `account_external_id` | `VARCHAR NOT NULL` | `'redacted'` | Обезличивание внешнего ID аккаунта установки |
| `provider_installations` | `metadata` | `JSONB NOT NULL` | `'{}'::jsonb` | Очистка метаданных аккаунта установки |
| `provider_installations` | `deleted_at` | `TIMESTAMP NULL` | `now()` | Фиксация отключения установки |
| `code_changes` | `title` | `VARCHAR NOT NULL` | `'[DELETED]'` | Удаление названия PR |
| `code_changes` | `description` | `TEXT NULL` | `NULL` | Удаление описания PR |
| `code_changes` | `author_login` | `VARCHAR NULL` | `'deleted-user'` | Обезличивание автора PR (String(255) nullable в модели) |
| `code_changes` | `source_branch` | `VARCHAR NOT NULL` | `'deleted'` | Удаление имён веток |
| `code_changes` | `target_branch` | `VARCHAR NOT NULL` | `'deleted'` | Удаление имён веток |
| `code_changes` | `web_url` | `TEXT NOT NULL` | `'https://redacted.local'` | Обезличивание внешних ссылок на pull request |
| `rule_versions` | `rules` | `JSONB NOT NULL`, CHECK checksum | `'[]'::jsonb` (чексумма SHA-256 пересчитывается) | Удаление пользовательских текстов правил |
| `repository_rules_status` | — | Таблица целиком | Явный `DELETE FROM repository_rules_status WHERE repository_id IN (...)` | Удаление пользовательских правил и диагностик без нарушения `ck_repository_rules_status_present` |
| `repo_conventions` | `key_patterns` | `JSONB NOT NULL` | `'[]'::jsonb` | Удаление шаблонов конвенций |
| `repo_conventions` | `recommendations` | `JSONB NOT NULL` | `'[]'::jsonb` | Удаление рекомендаций конвенций |
| `repo_conventions` | `languages` | `JSONB NOT NULL` | `'{}'::jsonb` | Очистка языковых профилей (объект JSONB `dict[str, int]` в модели) |
| `repo_conventions` | `agents_md` | `TEXT NULL` | `''` (пустая строка) | Удаление инструкций и конвенций репозитория |
| `repo_convention_drafts` | `files` | `JSONB NOT NULL` | `'[]'::jsonb` | Очистка черновиков конвенций |
| `runs` | `review_body` | `TEXT NULL` | `NULL` | Удаление общего текста ревью |
| `runs` | `error_message` | `TEXT NULL` | `NULL` | Удаление возможных путей и сниппетов из ошибок |
| `runs` | `triggered_by` | `VARCHAR NULL` | `'deleted-user'` | Обезличивание инициатора прогона |
| `runs` | `data_purged_at` | `TIMESTAMP NULL` | `now()` | Фиксация факта очистки кода |
| `run_actions` | `request` | `JSONB NOT NULL` | `'{}'::jsonb` | Очистка параметров вызова инструментов |
| `run_actions` | `response` | `JSONB NULL` | `NULL` | Очистка результатов вызова (соблюдение `ck_run_actions_response_location`) |
| `run_actions` | `response_ref` | `TEXT NULL` | `NULL` | Обнуление ссылки на внешний payload (`TEXT` в модели) |
| `findings` | `title` | `VARCHAR NOT NULL` | `'[DELETED]'` | Удаление формулировки замечания |
| `findings` | `body` | `TEXT NOT NULL` | `''` (пустая строка) | Удаление подробного описания замечания |
| `findings` | `suggestion` | `TEXT NULL` | `NULL` | Удаление предложенного кода замены |
| `findings` | `file_path` | `VARCHAR NOT NULL` | `'[DELETED]'` | Защита структуры файлов репозитория |
| `findings` | `rule_name` | `VARCHAR NOT NULL` | `'REDACTED'` | Обезличивание названий правил |
| `findings` | `rule_link` | `VARCHAR NULL` | `NULL` | Удаление ссылок на правила |
| `final_review_results` | `result` | `JSONB NOT NULL` | `'{}'::jsonb` | Очистка текста итогового вердикта |
| `final_review_results` | `publication_plan` | `JSONB NOT NULL` | `'{}'::jsonb` | Очистка плана публикации |
| `comments` | — | Таблица целиком | Сохраняется неизменной | Не содержит открытого текста или ПДн (`finding_id`, `github_review_id`, `github_comment_id`, `findings_hash`) |

---

## 7. Граница с `installation.deleted` (#496) и rebinding (#113 / #511)

### 7.1. Семантика `installation.deleted`
Удаление приложения на стороне GitHub/Bitbucket **не удаляет** Workspace:
- Выставляется `installation_available = false`, удаляются права доступа к репозиториям.
- Сам Workspace, баланс, подписка и финансовая история сохраняются.
- **Доступ Owner к биллингу:** генерация сессии Stripe Customer Portal (`POST /api/workspaces/{id}/billing/portal-session`) закреплена за задачей #372 (ADR 0002 §15). Настоящий ADR 0003 уточняет, что Owner удалённого воркспейса сохраняет возможность получить сессию портала для просмотра чеков в течение 30-дневного окна (`deleted_at IS NOT NULL AND data_purged_at IS NULL`). Список воркспейсов `GET /api/workspaces` для обычных участников скрывает удалённый воркспейс, но возвращает его с признаком `deleted = true` исключительно пользователям с подтверждённой ролью `Owner` в БД.

### 7.2. Удаление Workspace и предотвращение коллизий при переустановке App
1. **Пометка установки и двойное значение `deleted_at`:**
   - На таблице `provider_installations` колонка `deleted_at` имеет двойное назначение: фиксация отключения App со стороны провайдера (`disable_installation` по вебхуку `installation.deleted`) либо удаление связанного воркспейса со стороны платформы.
2. **Запрет повторного открытия (reopen/connect):**
   - Для Bitbucket и GitLab методы повторного открытия отозванного подключения (`reopen_connection`, connect) строго запрещены, если связанный воркспейс находится в статусе `workspaces.status = 'deleted'`. Попытка повторного подключения завершается ошибкой `409 Conflict`.
3. **Фильтрация в rebinding и связывании (`link`):**
   - В `SqlAlchemyGitHubRepositoryRebinding.archived_workspace` и методах `rebind()` добавляется обязательное условие: `workspaces.status != 'deleted'`.
   - Решение по `link()` (#554, принято техлидом): для установки, связанной с удалённым воркспейсом (`deleted_at IS NOT NULL`), метод `link()` (`workspaces/infrastructure/github_installation_links.py`) отказывает в привязке без ошибки 409 и без создания нового воркспейса по той же установке. Новая `installation.id` после повторной установки App создаёт новый независимый Workspace.
4. **Устранение коллизии уникальности `(provider, external_id)`:**
   - Существующее ограничение `UniqueConstraint("provider", "external_id")` (`app/modules/repositories/infrastructure/models.py:50`) удаляется миграцией в задаче #551.
   - Вместо него создаётся частичный уникальный индекс:
     ```sql
     CREATE UNIQUE INDEX uq_provider_installations_active ON provider_installations (provider, external_id) WHERE deleted_at IS NULL;
     ```
   - В коде используется `on_conflict_do_nothing(index_elements=["provider", "external_id"], index_where=ProviderInstallation.deleted_at.is_(None))`.
   - Благодаря частичному индексу строки с `deleted_at IS NOT NULL` перестают занимать уникальную пару `(provider, external_id)`, исключая коллизии при повторных установках App.
5. **Повторная установка App:**
   - Повторная установка App той же организацией создаёт **новый независимый Workspace** с новым `id` и тарифом по умолчанию. Старый tombstone никогда не воскресает.

---

## 8. Пользовательский интерфейс и UX

1. **Раздел Danger Zone (`/settings`):** доступен строго при `role === 'owner'`.
2. **Диалог подтверждения:** содержит полный перечень правовых и финансовых последствий и поле посимвольного ввода `workspace.name`.
3. **Обработка завершения:**
   - Клиентский кэш сбрасывается.
   - Пользователь перенаправляется на следующий доступный активный Workspace либо на экран онбординга `/repositories`.
   - Прямой переход по ссылке удалённого ресурса отдаёт экран 404 с предложением перейти к доступным воркспейсам.

---

## 9. Карта изменений канона (вместе с реализацией)

| Документ / Спецификация | Требуемое изменение | Задача |
|---|---|---|
| [SYSTEM_DESIGN.md §1 Р-15](../SYSTEM_DESIGN.md#1-решения) | Замена каскадного удаления на сохранение метаданных и tombstone для фискального аудита | `#551` |
| [SYSTEM_DESIGN.md §6.4](../SYSTEM_DESIGN.md#64-состояния-run) | Правила реконсилера для удаляемого workspace: отмена queued и зависших running с истёкшим lease_until | `#551` |
| [SYSTEM_DESIGN.md §6.5](../SYSTEM_DESIGN.md#65-вход-и-сессия-d4) | Исключение удалённого `workspace_id` (`status = 'deleted'`) из сессий участников, сохранение доступа Owner к биллингу через portal-session | `#550` |
| [SYSTEM_DESIGN.md §6.9](../SYSTEM_DESIGN.md#69-подключение-репозиториев-установка-github-app-d10) | Разграничение `installation.deleted` и удаления воркспейса, отказ `link()` для tombstone-установок, исключение для Owner в получении portal-session | `#551` |
| [SYSTEM_DESIGN.md §10](../SYSTEM_DESIGN.md#10-стратегия-кэширования-контекстов) | Замена «до удаления Workspace (каскад)» на гибридную модель (0 дней очистка кода клиента, 7 лет хранение метаданных для фискального аудита) | `#551` |
| [SYSTEM_DESIGN.md §11](../SYSTEM_DESIGN.md#11-данные-согласование-с-erd-роли-6) | Замена концепции полного каскадного удаления на батчевую очистку через `purge_workspace_run_data` с сохранением строк `RunAction` без payload | `#551` |
| [SYSTEM_DESIGN.md §12](../SYSTEM_DESIGN.md#12-контракт-api--ui) | Исключение воркспейсов в статусе `deleted` из выдачи активных установок и сессий `/api/auth/me` (с исключением для Owner при получении portal-session) | `#550` |
| [SYSTEM_DESIGN.md §13](../SYSTEM_DESIGN.md#13-нефункциональные-требования) | Согласование сводной таблицы сроков хранения данных клиента при удалении Workspace | `#551` |
| [BACKEND_ARCHITECTURE.md](../BACKEND_ARCHITECTURE.md) | Фоновый воркер `WorkspaceDeletionWorker`: асинхронный вызов Stripe и отзыв VCS-токенов вне транзакций, батчевая очистка `purge_workspace_run_data` | `#551` |
| [ADR 0002 §4.3 и §5](0002-billing-monetization.md#43-подписка) | Переопределение для удалённого воркспейса: немедленная отмена подписки, возврат по запоздалым `invoice.paid`, аннулирование грантов через `adjustment` | `#551` |
| [ADR 0002 §10.3](0002-billing-monetization.md#103-charge-dispute) | Обработка диспутов, чарджбэков и сбоев рефандов по удалённым воркспейсам без изменения статуса `deleted` | `#551` |
| [ADR 0002 §11.1](0002-billing-monetization.md#111-матрица) | Ссылка в строке «Удаление workspace» на правила и авторизацию настоящего ADR 0003 | `#550` |
| [ADR 0002 §15](0002-billing-monetization.md#15-открытые-вопросы) | Реализация Customer Portal сессий закреплена за #372; ADR 0003 определяет доступ Owner в 30-дневном окне | `#372` / `#550` |
| `contracts/openapi.yaml` | Спецификация эндпоинта `DELETE /api/workspaces/{workspace_id}` (ответы `202`, `403`, `404`, `422`) | `#550` |

---

## 10. Последствия (Consequences)

### Положительные:
1. **Соответствие законодательству:** строгое соблюдение GDPR (ст. 17 «Право на забвение») и 152-ФЗ в части немедленного удаления исходного кода, сниппетов, текстов правил и диффов клиента, а также обезличивания персональных данных, при сохранении бухгалтерского журнала.
2. **Финансовая и налоговая безопасность:** сохранение ссылочной целостности бухгалтерского журнала `credit_ledger` и метаданных `runs` исключает риски сбоев при фискальных проверках в течение целевого срока 7 лет.
3. **Предсказуемость биллинга:** исключены фантомные списания Stripe за счёт гарантированной отмены подписок и отзыва платежных методов вне транзакций БД.
4. **Защита целостности базы данных:** сохранение обезличенных строк `repositories`, `rule_versions` и интеграций предотвращает ошибки нарушения внешних ключей `NOT NULL FK` при сохранении прогонов.

### Ограничения и риски:
1. Необходимость выделенного воркера фоновой очистки и реконсилятора Stripe-операций (#551).
2. Невозможность отмены операции удаления: действие необратимо, о чём пользователь явно предупреждается в диалоге.

---

## 10.1. Рассмотренные альтернативы

1. **Альтернатива 1: Полный каскадный Hard Delete (`ON DELETE CASCADE` на всех FK):**
   - *Суть:* физическое удаление всех связанных строк во всех таблицах базы данных при удалении Workspace.
   - *Почему отклонено:* разрушает финансовый журнал `credit_ledger` и делает невозможной обязательную налоговую и бухгалтерскую проверку за прошедшие периоды. Нарушает требования аудита Stripe и фискальных регуляторов.
2. **Альтернатива 2: Немедленное удаление сущности Stripe Customer:**
   - *Суть:* немедленный вызов `stripe.Customer.delete` в момент удаления Workspace.
   - *Почему отклонено:* делает невозможным проведение законного 14-дневного возврата неиспользованных средств, а также автоматическое урегулирование чарджбэков и диспутов.
3. **Альтернатива 3: Реанимация прежнего Workspace при повторной установке App:**
   - *Суть:* автоматическое снятие статуса `deleted` и повторное открытие доступа к старому воркспейсу, если GitHub App снова установлен на ту же организацию.
   - *Почему отклонено:* создает критическую уязвимость утечки исторических данных новому владельцу организации/аккаунта и нарушает гарантию безвозвратного удаления данных клиента.

---

## 11. Открытые вопросы (Open Issues)

| Вопрос | Ответственный | Срок / Веха |
|---|---|---|
| Подтверждение юристами точного срока хранения фискальных проводок для юрисдикций РФ/ЕС/США (7 лет vs 10 лет) | DPO / Юридический отдел (Legal) | Спринт 5 |
| Реализация авто-возврата Stripe Refund через outbox-воркер при возникновении `invoice.paid` после фиксации tombstone | Исполнитель задачи `#551` | Задача `#551` |

---

## 12. Порядок внедрения и зависимости реализации

Реализация функциональности декомпозирована на зависимые этапы:

1. **Обязательные предварительные условия (эпик `#369`):**
   - `#373` — поддержка БД-ролей (`Owner`) и контекста авторизации.
   - `#371` — гранты `credit_ledger`, `balance_holds`, финализация удержаний.
   - `#372` — подписки Stripe, append-only согласия `billing_consents` и эндпоинт сессий Customer Portal.
2. **Backend API (#550, ветка `feat/550-workspace-deletion`):**
   - Миграция таблицы `workspaces` (поля `status`, `deleted_at`, `deletion_enqueued_at`, `data_purged_at`, `customer_anonymization_due_at`, `customer_anonymized_at`).
   - Реализация эндпоинта `DELETE /api/workspaces/{id}` с авторизацией по БД-роли `Owner` и проверкой имени.
   - Каноническая отмена прогонов (`RunRepository.cancel_run` для `queued`, `cancel_requested = true` для `running`/`publishing`).
   - Немедленный отзыв токенов агентов `auth_agent_tokens.revoked_at = now()` и удаление `auth_agent_device_flows` (#553).
   - Обработчики вебхуков Stripe для удалённого воркспейса (фиксация фискальной записи `paid`, отказ в начислении кредитов, постановка задачи на возврат в outbox).
   - Фиксация флага `deletion_enqueued_at` в транзакции и публикация задачи в RabbitMQ **строго после `commit()`**.
3. **Фоновая обработка и очистка данных (#551):**
   - Воркер `WorkspaceDeletionWorker` и периодическая leader-задача реконсиляции (SD §6.4).
   - Внешние вызовы: отзыв VCS-токенов, дерегистрация вебхуков у провайдеров, отмена подписки Stripe, отвязка платежных методов, погашение `stripe.checkout.Session.expire`.
   - Проведение возвратов по задачам outbox с ключами идемпотентности и фиксацией `credit_ledger kind='refund'`.
   - Ожидание терминальности активных прогонов воркспейса (таймаут 15 мин), снятие `balance_holds`, аннулирование доступных грантов проводками `kind = 'adjustment'`, `reason = 'workspace_deleted'`.
   - Процедура `purge_workspace_run_data(workspace_id)` для удаления кода прогонов, контекста, явного `DELETE FROM repository_rules_status` и RAG-индекса, фиксация `runs.data_purged_at = now()` и `workspaces.data_purged_at = now()`.
   - Обезличивание репозиториев, версий правил, конвенций, findings и результатов ревью по спецификации §6.1.
   - Миграция `provider_installations`: удаление старого `UniqueConstraint` и создание частичного уникального индекса `WHERE deleted_at IS NULL` с поддержкой `index_where`.
   - Запрет `reopen_connection` и connect в Bitbucket/GitLab для воркспейсов в статусе `deleted`.
   - Фильтрация повторной привязки в `SqlAlchemyGitHubRepositoryRebinding` (`status != 'deleted'`) и отказ `link()` для tombstone-установок.
   - Деперсонализация Stripe Customer через 30 дней по предикату `customer_anonymization_due_at`.
4. **Пользовательский интерфейс (UI):**
   - `ui#300` — блок Danger Zone в `/settings` и диалог ввода имени воркспейса для роли `Owner`.
   - `ui#301` — перенаправление пользователя после удаления и обработка экрана 404 (на паузе до ADR 0003).
