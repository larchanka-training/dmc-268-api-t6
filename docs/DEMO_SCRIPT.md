# Сценарий демо v1.0

Показ на 7–10 минут на staging: от PR с багами и `.review/rules.md` до дашборда аналитики.
Сверено со staging 09.11.2026 (api `4ffabb10`, ui `cb3dd355`); экранные тексты интерфейса — на
английском, как в staging UI; тексты check-run на GitHub — на русском, как их пишет бот. Как
устроена цепочка за кадром — [ARCHITECTURE.md](ARCHITECTURE.md).

## 1. Начальное состояние

Показ идёт в `axyi/dmc268-t6-sandbox`: на него установлен staging App, поэтому песочница есть в
списке Repositories staging UI. Готовить за 1–2 дня до показа и проверять за 15 минут до начала.

| Что | Должно быть | Где проверить |
|---|---|---|
| Стенд | `{"status":"ok"}`; последний CI/CD на `main` api зелёный, staging выкачен из него | [staging-api /healthcheck](https://staging-api.dmc268-t6.axyi.ru/healthcheck), Actions api |
| Вход | браузер вошёл в [staging UI](https://staging-ui.dmc268-t6.axyi.ru) через GitHub под пользователем с доступом к установке App на песочнице; у этого пользователя на GitHub право triage или выше в песочнице, иначе на шаге 3 лейбл не поставить | Repositories: в списке есть `axyi/dmc268-t6-sandbox`; на демо-PR справа у Labels есть шестерёнка |
| Правила | `.review/rules.md` лежит в `main` песочницы. Правила читаются из base-коммита PR ([RULES_FORMAT_SPEC.md §3.1](RULES_FORMAT_SPEC.md#31-ревизия-и-тип-объекта)), поэтому в демо-PR файл не добавляют и не меняют | GitHub, `main` песочницы |
| CI | `.github/workflows/ci.yml` в `main` песочницы: job `check` на каждый PR | GitHub, `main` песочницы |
| Настройки репозитория | Automatic review — включён; CI waiting — `Auto`; Verdict — `Comment`; Comment limit — `10`; Branch filter — пусто; Severity threshold — `All`; RAG — `Ready` | staging UI → Repositories → Settings песочницы |
| Бюджет | у workspace `GitHub: axyi` нет надписи «The daily budget is used up, reviews are paused until 00:00 UTC» | staging UI → Analytics → Budget today |
| Демо-PR | открыт заранее, CI зелёный, лейбла `ai-review` нет | вкладка 2 |
| Запасной PR | подготовлен за 1–2 дня до показа: окно `7 days` в Analytics скользящее, и прогон старше 7 суток из него выпадет. Лейбл поставлен, ревью опубликовано, в нём хотя бы один inline-комментарий в `shop/orders.py`, в Checks `AI Review` — «AI-ревью: …». Inline-комментариев нет — закрыть этот PR и подготовить запасной заново, на ветке `demo/<дата>-backup-2` | вкладка 5 |
| Вкладки | 1 — `.review/rules.md` в `main`; 2 — демо-PR; 3 — staging UI, Settings песочницы; 4 — staging UI, Analytics; 5 — запасной PR | — |

На 09.11.2026 Automatic review на staging выключен во всех подключённых репозиториях. Тот, кто
готовит показ, включает его для песочницы и после показа возвращает как было.

Почему демо-PR открывают заранее. Лейбл запускает ревью, только когда CI на head-коммите зелёный
([PIPELINE_SPEC.md §8.1](PIPELINE_SPEC.md#81-условие-github)). Если PR открыт заранее, CI к
показу уже прошёл, и Run создаётся сразу после лейбла.

Почему каждый показ идёт на новом PR. Ревью по лейблу запускается для пары (PR, head-коммит)
только один раз. Повторный лейбл на том же коммите ничего не запустит.

Подготовка обоих PR — одна и та же, с разными ветками (`<дата>` — день показа, `ГГГГММДД`).

1. Клон и ветка:

   ```bash
   git clone git@github.com:axyi/dmc268-t6-sandbox.git && cd dmc268-t6-sandbox
   git switch -c demo/<дата>-live origin/main      # для запасного: demo/<дата>-backup
   ```

2. Создать файл `shop/orders.py` и вставить в него весь блок кода из приложения A.
3. Коммит, push и PR:

   ```bash
   git add shop/orders.py && git commit -m "feat(shop): add order lookups"
   git push -u origin HEAD
   gh pr create --base main --title "feat(shop): add order lookups" --body "Order lookups for the account page."
   ```

На запасном PR лейбл `ai-review` ставят сразу и дожидаются ревью. На демо-PR лейбл не ставят.

## 2. Сценарий

T0 — момент, когда на шаге 3 поставлен лейбл. Время в первом столбце отсчитывается от начала
показа.

| Время | Шаг | Действие | Результат на экране |
|---|---|---|---|
| 0:00–0:45 | 1. Правила команды | Вкладка 1: `.review/rules.md` | Разделы `# Security` (параметризованный SQL), `## Python` (`except Exception` только на границе фоновой задачи), `# Performance` (запрос на каждый элемент списка), блок `review-ignore` |
| 0:45–1:30 | 2. PR с багами | Вкладка 2 → Files changed | Новый файл `shop/orders.py`; во вкладке Checks — зелёный `CI / check` |
| 1:30–2:00 | 3. Триггер | Справа Labels → `ai-review`. Засечь T0 | Лейбл `ai-review` на PR; когда worker берёт Run, в Checks появляется `AI Review` — «AI-ревью выполняется» |
| 2:00–3:30 | 4. Правила в портале | Вкладка 3: Settings песочницы | Automatic review включён; CI waiting — `Auto (wait for CI when the commit has checks)`; панель Review rules — `File found`, `.review/rules.md`, `main`, текст файла, Ignored paths `**/*.min.js` и `generated/**` |
| 3:30–4:30 | 5. Прогон идёт | Вкладка 3: Runs → строка демо-PR | Страница `Run for PR #<N>`: этапы `Collecting context` → `Model analysis` → `Publishing`, растёт список `Review session`, модель, `Token limit`, `Cost limit` |
| 4:30–6:00 | 6. Ревью на PR | Вкладка 2 → Conversation. Ревью нет — §3 | Ревью от `dmc268-t6-reviewer[bot]`: `Executive summary` с `Verdict` и `Risk Score`, `Risk summary`; inline-комментарии в `shop/orders.py` (ожидаются, см. под таблицей); в Checks `AI Review` — «AI-ревью: <вердикт>» |
| 6:00–7:00 | 7. Цена прогона | Вкладка 3: Run, обновить | Status `succeeded`; Findings по severity; Duration; Input tokens, Output tokens, Cost; дифф с находками, фильтр по Severity |
| 7:00–7:30 | 8. Аудит | Вкладка 3: Audit | Верхняя строка — демо-PR: Trigger `webhook`, Status `succeeded`, Cost |
| 7:30–8:30 | 9. Аналитика | Вкладка 4: `7 days`, репозиторий `axyi/dmc268-t6-sandbox` | Плитки `Runs: Succeeded`, `Pull requests: Blocking` / `Attention` / `Clean`, `Critical`…`Info`, `Cost`; график `Findings by day`; блок `Budget today` |
| +1:00 | 10. Playground — если вошёл в релиз | Показать проверку правила без PR | На api `4ffabb10` и ui `cb3dd355` Playground нет: шаг пропускается |

Итого 8:30, с Playground — 9:30.

Что модель найдёт в `shop/orders.py`, ожидаемо, но не гарантированно: LLM не детерминирован.
Гарантированы check-run `AI Review` и ревью с `Executive summary`. Хотя бы один inline-комментарий
ожидается, но тоже не гарантирован: при вердикте `clean` его может не быть. Конкретные находки
не гарантированы тем более.

| Строка в листинге | Дефект | Правило |
|---|---|---|
| `find_orders` | значение пользователя подставлено в SQL через f-string | `# Security` в `.review/rules.md` |
| `load_orders` | отдельный запрос на каждый id | `# Performance` в `.review/rules.md` |
| `amount_due` | `except Exception` вне фоновой задачи | `## Python` в `.review/rules.md` |
| `amount_due` | деление на 100 возвращает float, функция обещает центы (`int`) | `AGENTS.md` песочницы: деньги в целых центах |

## 3. Запасной путь: ревью не пришло

Порог — T0 + 4 минуты, это цель p95 времени до ревью для движка `fast` в SLA prod
([MONITORING.md §12](MONITORING.md#12-sla-prod)). Run создаётся сразу после лейбла, поэтому
порог считают по секундомеру, засечённому на шаге 3, а не по часам показа: при T0 в 1:30–2:00
это 5:30–6:00 от начала. Если на шаге 6 ревью ещё нет, ведущий продолжает шаг 5. К порогу ревью
не пришло — ведущий переключается на запасной PR.

1. Причину назвать по `AI Review` во вкладке Checks демо-PR и по странице Runs, не больше 20
   секунд. Check-run создаётся, когда worker берёт Run, а не когда Run создан
   ([PIPELINE_SPEC.md §1](PIPELINE_SPEC.md#1-состояния-и-переходы), T1 и T4):
   - `AI Review` нет, строки демо-PR в Runs нет: Run не создан. Причины: Automatic review
     выключен, CI не зелёный, лейбл не `ai-review`, PR закрыт.
   - `AI Review` нет, строка демо-PR в Runs есть со статусом `queued`: Run создан и ждёт worker.
   - «AI-ревью пропущено»: Run пропущен, причина — в тексте check-run. Например, Branch filter не
     подходит: «Целевая ветка PR не подходит под фильтр веток репозитория.»
   - «AI-ревью выполняется»: прогон идёт дольше цели, модель отвечает медленно. В тексте
     check-run всегда стоит «попытка N из 3»; N больше 1 значит, что предыдущая попытка
     прервалась и worker взял Run заново.
   - «AI-ревью не выполнено»: прогон упал, причина — в поле `Reason` на странице Run.
   - «AI-ревью отменено»: прогон отменён, причина — в тексте check-run. Например, в ветку PR
     пришёл новый коммит: «В PR новый коммит — ревью будет для него.»
2. Вкладка 5: шаги 6 и 7 идут на запасном PR и его Run. На шаге 8 в Audit показывают строку
   запасного PR, а не верхнюю. Его прогон старше, поэтому строка ниже. Строка демо-PR, если Run
   создан, стоит выше, в текущем статусе. Шаг 9 не меняется: прогон запасного PR попадает в окно
   `7 days`.
3. Ревью пришло, но без inline-комментариев: шаг 6 закончить на `Executive summary` демо-PR,
   inline-комментарии показать на запасном PR (вкладка 5), шаг 7 — на Run демо-PR.
4. Restart во время показа не нажимать: новый прогон займёт столько же. После показа:
   - Restart на завершённом Run;
   - если Run нет, новый push в ветку демо-PR.

Хронометраж по запасному пути: шаги 6–9 сдвигаются на минуту, итог — 9:30. Playground при
переходе на запасной путь пропускается.

## 4. После показа

- Демо-PR и запасной PR закрыть без мержа, ветки `demo/<дата>-*` удалить.
- Automatic review песочницы вернуть в состояние до подготовки.

## 5. Протокол репетиций

| Дата | Стенд | T0 → «AI-ревью выполняется» | T0 → ревью на PR | Итог | Отклонения |
|---|---|---|---|---|---|
| — | — | — | — | — | репетиции ещё не было |

## Приложение A. `shop/orders.py`

```python
"""Order lookups for the account page."""

import sqlite3

from .models import Order
from .pricing import order_total, with_tax


def find_orders(conn: sqlite3.Connection, email: str) -> list[Order]:
    """Return the orders of one customer."""
    query = f"SELECT order_id, total_cents, customer_email FROM orders WHERE customer_email = '{email}'"
    return [Order(*row) for row in conn.execute(query).fetchall()]


def load_orders(conn: sqlite3.Connection, order_ids: list[int]) -> list[Order]:
    """Load the orders with the given ids."""
    orders = []
    for order_id in order_ids:
        row = conn.execute(
            "SELECT order_id, total_cents, customer_email FROM orders WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        orders.append(Order(*row))
    return orders


def amount_due(conn: sqlite3.Connection, order_id: int) -> int:
    """Return the amount to pay for an order with tax, in cents; 0 if it is missing."""
    try:
        order = load_orders(conn, [order_id])[0]
    except Exception:
        return 0
    return with_tax(order_total(order)) / 100
```
