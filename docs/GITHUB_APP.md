# Публичная страница GitHub App (#467)

Манифест prod App лежит в [`docs/github-app-manifest.json`](github-app-manifest.json).
Этот файл и страница ниже — описание, которое владелец вносит в интерфейс
GitHub. Код настройки App и значения секретов не меняет: client secret,
private key и webhook secret здесь не записываются.

Проверка 2026-11-25: публичный `GET /apps/{slug}` без авторизации.

## Владельцы

| App | Slug | Владелец | Тип | Страница |
| --- | --- | --- | --- | --- |
| staging | `dmc268-t6-reviewer` | `axyi` | User | https://github.com/apps/dmc268-t6-reviewer |
| prod | `ai-codereviewer-bot` | `dmc-268` | Organization | https://github.com/apps/ai-codereviewer-bot |

Владелец staging App — личный аккаунт `axyi`, slug `dmc268-t6-reviewer`.
Имя на странице — `dmc268-t6-reviewer`, описание — `AI code reviewer bot, staging (larchanka-training/dmc-268-api-t6)`, homepage — `https://staging-www.dmc268-t6.axyi.ru`. Webhook staging — `https://staging-api.dmc268-t6.axyi.ru/webhooks/github` (другой App, другие ключи и свой webhook secret, #37).

Владелец prod App — организация `dmc-268`, slug `ai-codereviewer-bot`.
Имя — `AI CodeReviewer Bot`, описание страницы — `AI code reviewer bot`, homepage — `https://www.dmc268-t6.axyi.ru/`. Анонимный `GET https://github.com/apps/ai-codereviewer-bot` ответил `200`, заголовок страницы — `GitHub Apps - AI CodeReviewer Bot · GitHub`, в теле есть это описание. Заметка #467 от 09.11, что slug `dmc268-t6-reviewer-prod` отвечал 404, историческая: зарегистрированный prod App — этот (`docs/SECRETS.md`, «Настройки prod App», создан 2026-11-09).

Права обеих App на 2026-11-25 совпали с манифестом: `actions: read`, `checks: write`, `contents: read`, `issues: write`, `metadata: read`, `pull_requests: write`, `statuses: read`. Поле `events` того же ответа у staging и у prod — `check_suite`, `pull_request`, `push`, `repository`, `status`, `workflow_run`. В этом списке есть `repository`. `updated_at` (staging `2026-11-09T21:24:09Z`, prod `2026-11-09T21:24:43Z`) — время последнего сохранения настроек, не отдельный снимок без `repository`. Заметка #467, что у staging днём 09.11 подписки на `repository` не было, про более раннее состояние; подписку включает владелец, код её не включает. Новых прав она не требует. Строка Events в `docs/SECRETS.md` («Настройки prod App») перечисляет тот же список, включая `repository`.

## Передача в организацию `larchanka-training`

В организацию `larchanka-training` staging App не передан: владелец по-прежнему `axyi`. Передачу начинает текущий владелец App. Если получатель — организация, у вызывающего должна быть роль owner этой организации (UI: настройки владельца → Developer settings → GitHub Apps → App → Advanced → Transfer ownership; тот же контракт у `POST /apps/{slug}/transfer` с телом `{"new_owner": "<login>"}`). Роль `axyi` в `larchanka-training` — `member`, не owner (#37, «Владелец и видимость»). Публичный `GET /apps/{slug}` эту роль не возвращает, роль в этом изменении не перечитывалась. Пока owner `larchanka-training` не даст `axyi` роль owner, передача staging App не проходит.

Prod App уже принадлежит организации `dmc-268`, не `axyi` и не `larchanka-training`. Перенос prod в `larchanka-training` — отдельное решение владельцев `dmc-268`. Для публичной страницы он не нужен. Смена владельца не создаёт новый App и не меняет slug; ключи и webhook secret остаются у App, установки с App не снимаются сами.

## Права

Каждое право — вызов или событие из SYSTEM_DESIGN §8.2. Лишних прав нет. `contents: write` нет. Прав организации и аккаунта нет. Расхождения с §8.2 нет: таблица повторяет перечень прав этого раздела.

| Право | Зачем |
| --- | --- |
| `pull_requests: write` | событие `pull_request`; `GET /pulls/{n}/files`; `POST /pulls/{n}/reviews`; для GraphQL `resolveReviewThread` §8.2 ожидает, что хватает этого права (OQ-9, не подтверждено) |
| `checks: write` | событие `check_suite`; `upsert_check_run` |
| `contents: read` | `GET /git/blobs/{sha}`, `GET /git/trees/{sha}`, чтение `.review/rules.md`. Записи в репозиторий нет |
| `metadata: read` | обязательное право; событие `repository`; `GET /repos/{owner}/{repo}` |
| `issues: write` | `POST /repos/{owner}/{repo}/labels` с телом `{"name": "ai-review"}` при подключении репозитория (§6.9). `pull_requests: write` для лейбла не хватает |
| `actions: read` | событие `workflow_run` |
| `statuses: read` | событие `status` и combined status |

## События

| Событие §8.2 | Подписка |
| --- | --- |
| `pull_request` (включая `labeled` / `unlabeled` / `closed`) | в `default_events` |
| `check_suite` | в `default_events` |
| `workflow_run` | в `default_events` |
| `status` | в `default_events` |
| `push` | в `default_events`; переключатель Subscribe to events включает владелец, код его не включает (§8.2) |
| `repository` (`privatized`, `publicized`, `renamed`, `transferred`) | в `default_events`; так же включается владельцем (§8.2). На 2026-11-25 включено у обеих App |
| `installation`, `installation_repositories` | `installation` и `installation_repositories` приходят без отдельной подписки, в массиве `events` их нет. `suspend` — действие `installation`, не отдельное имя события |
| `pull_request_review_thread` | `pull_request_review_thread` в MVP не подписан (§8.2: после MVP) |

Подписки staging и prod покрывают события §8.2, которые включаются тумблером, включая `repository`. Код подписку не включает.

## Страница

У prod уже выставлены имя, описание `AI code reviewer bot`, homepage `https://www.dmc268-t6.axyi.ru/` и публичная установка (анонимная страница открывается). Видимость «Any account» — пункт «Where can this GitHub App be installed?». Логотип заменяет только владелец App (общие настройки App, PNG или JPG, не меньше 200×200). Этот репозиторий файл логотипа не загружает в GitHub.

Шаги установки на чужой аккаунт и снятия страницы — в README, раздел «Установка». Ссылку на страницу в docs-site и на лендинге этот репозиторий не хардкодит: слаг UI получает на сборке (`VITE_GITHUB_APP_SLUG`).
