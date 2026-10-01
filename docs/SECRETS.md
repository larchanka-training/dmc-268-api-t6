# Secrets — API (команда 6)

| | |
|---|---|
| Статус | перечень и правила обращения |
| Владелец | инфраструктура (роль 3) |
| Связанные документы | [CICD.md](CICD.md), [INFRASTRUCTURE.md](INFRASTRUCTURE.md) |

Секреты живут в GitHub (organization, repository, Environment `staging`) или в окружении оператора. В git, образ и логи CI они не попадают. Цель выката (курсовой VPS или Terraform-хост) выбирается автоматически — [CICD.md](CICD.md#8-курсовой-vps-и-edge-прокси).

---

## 1. Перечень

### Organization — курсовой VPS

Заданы курсом на уровне организации, одинаковы в репозиториях API и UI.

| Имя | Тип | Зачем |
|---|---|---|
| `VPS_DMC268_IP_T6` | variable | IPv4 курсового VPS, SSH на порт 22 |
| `VPS_DMC268_U` | secret | SSH-пользователь (`root`) |
| `VPS_DMC268_P` | secret | SSH-пароль; уходит только на VPS, никогда на Terraform-хост |
| `AI_DMC268_T6` | secret | LLM-ключ приложения. В контейнеры пока не пробрасывается: имена переменных LLM (`LLM_API_KEYS` и др.) появятся с #33, тогда ключ уйдёт в `app.env` тем же путём, что секреты Environment `staging` ниже |

### Repository — variables

| Variable | Обязателен | Пример |
|---|---|---|
| `STAGING_SSH_FINGERPRINT` | да, для обеих целей | `SHA256:…` host key VPS (`ssh-keyscan -p 22 <VPS_DMC268_IP_T6> \| ssh-keygen -lf - -E sha256`). Значение в Environment `staging` перекрывает repository. При переключении между курсовым VPS и Terraform-хостом fingerprint нужно обновить: у хостов разные ключи, и при несовпадении jobs с SSH падают (fail-closed) |
| `APP_DOMAIN` | да, для курсового VPS | `dmc268-t6.axyi.ru` — базовый домен маршрутов edge-прокси |

### GitHub Environment `staging` — secrets

Доступ к машине, реестру и данным, а также секреты приложения.

| Secret | Обязателен | Куда уходит | Зачем |
|---|---|---|---|
| `STAGING_SSH_KEY` | да, для Terraform-хоста | SCP/SSH на VM | приватный ключ к `hcloud_ssh_key.ci` |
| `POSTGRES_PASSWORD` | нет | `<APP_DIR>/.env` на хосте | пароль PostgreSQL. Если не задан, `deploy.sh` генерирует его при первом выкате и хранит в `.env` (0600). После инициализации тома пароль не менять: Postgres его не перечитывает |

Секреты приложения. GitHub не принимает имена секретов с префиксом `GITHUB_` (HTTP 422), поэтому секреты App заведены как `GH_*`, а в контейнере у них имена из `.env.example`. Сопоставление делает шаг «Bundle application secrets» в `deploy-staging`. Незаданный секрет в контейнер не попадает совсем, а не приходит пустой строкой.

| Secret | Тип значения | Переменная в контейнере | Контейнеры | Зачем |
|---|---|---|---|---|
| `GH_APP_ID` | число | `GITHUB_APP_ID` | `api`, `worker` | App ID staging App `dmc268-t6-reviewer` (#37) |
| `GH_APP_PRIVATE_KEY` | многострочный PEM | `GITHUB_APP_PRIVATE_KEY` | `api`, `worker` | подпись JWT App для installation token |
| `GH_WEBHOOK_SECRET` | строка | `GITHUB_WEBHOOK_SECRET` | `api` | проверка подписи вебхуков; без него диспетчер вебхуков выключен |
| `GH_CLIENT_ID` | строка | `GITHUB_CLIENT_ID` | `api` | user authorization App (auth-api, #11); отличается от App ID |
| `GH_CLIENT_SECRET` | строка | `GITHUB_CLIENT_SECRET` | `api` | обмен OAuth-кода (auth-api, #11) |
| `AUTH_JWT_PRIVATE_KEY` | многострочный PEM (RSA) | `AUTH_JWT_PRIVATE_KEY` | `api` | подпись локального access JWT |
| `AUTH_JWT_PUBLIC_KEY` | многострочный PEM (RSA) | `AUTH_JWT_PUBLIC_KEY` | `api` | проверка access JWT |

`AUTH_JWT_ISSUER` (`dmc-268-api`) и `AUTH_JWT_AUDIENCE` (`dmc-268-ui`) — не секреты: они фиксированы в `deploy/compose/staging.yml`. Домен cookie refresh не настраивается: cookie host-only с `Path=/api/auth`, а UI и API работают с одного origin `staging-ui.<APP_DOMAIN>` (маршрут `/api/*` в `deploy/edge/Caddyfile`).

Пара ключей JWT создаётся один раз и заводится в Environment `staging`:

```bash
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out jwt.pem
openssl rsa -in jwt.pem -pubout -out jwt.pub
gh secret set AUTH_JWT_PRIVATE_KEY --env staging -R larchanka-training/dmc-268-api-t6 < jwt.pem
gh secret set AUTH_JWT_PUBLIC_KEY --env staging -R larchanka-training/dmc-268-api-t6 < jwt.pub
rm jwt.pem jwt.pub
```

Смена пары разлогинивает всех: ранее выданные access JWT перестают проверяться.

### Генерируются на хосте

В GitHub не заводятся. `deploy.sh` создаёт их при первом выкате (48 hex-символов), хранит в `<APP_DIR>/.env` (0600) и переиспользует при каждом выкате и откате.

| Имя | Куда уходит | Примечание |
|---|---|---|
| `RABBITMQ_PASSWORD` (пользователь `RABBITMQ_USER`, по умолчанию `app`) | контейнер `rabbitmq`; `RABBITMQ_URL` в `api` и `worker` | RabbitMQ, как Postgres, применяет учётные данные только на пустом томе `rabbitmq-data`. Если том есть, а пароля в `.env` нет, `deploy.sh` отказывается генерировать новый |
| `REDIS_PASSWORD` | контейнер `redis`; `REDIS_URL` в `api` и `worker` | Redis — кэш без тома: новый пароль только сбрасывает кэш |

`GITHUB_TOKEN` выдаёт Actions сам. В репозиторий его не кладут. Push в GHCR — `packages: write`; pull на staging — `packages: read`.

### GitHub Environment `staging` — variables

Публичные или низкорисковые значения. Их видно в логах — это нормально.

| Variable | Обязателен | Пример |
|---|---|---|
| `STAGING_HOST` | для Terraform-хоста | IPv4 или FQDN из Terraform output `ssh_host`; пусто → выкат на курсовой VPS |
| `STAGING_SSH_PORT` | для Terraform-хоста | `22022` — Terraform output `ssh_port`; без него jobs с SSH падают первым шагом |
| `STAGING_SSH_FINGERPRINT` | да | SHA256 host key fingerprint для `appleboy/scp-action` и `appleboy/ssh-action`, формат `SHA256:<43 символа base64>`. С пустым значением appleboy принимает любой host key, поэтому jobs с SSH падают первым шагом |
| `STAGING_SSH_USER` | для Terraform-хоста | `root` после cloud-init |
| `STAGING_HEALTH_URL` | нет | иначе `http://$STAGING_HOST/healthcheck` |
| `POSTGRES_USER` | нет | иначе `app` |
| `POSTGRES_DB` | нет | иначе `app` |

### Только локально у оператора

CI **не** делает `terraform apply`. Эти значения в GitHub Actions не заводят.

| Secret | Зачем |
|---|---|
| `HCLOUD_TOKEN` | Hetzner Cloud API |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | Object Storage, remote state |
| приватный SSH-ключ оператора | ручной вход на VM |

---

## 2. Хранение в CI/CD

Для GitHub App callback сервису также нужны следующие переменные окружения. Их значения
задаются только в менеджере секретов или локальном `.env`; в репозиторий не записываются.

| Имя | Назначение |
|---|---|
| `GITHUB_CLIENT_ID` | Client ID GitHub App для обмена кода; отличается от `GITHUB_APP_ID` |
| `GITHUB_CLIENT_SECRET` | Client secret GitHub App для обмена кода |
| `AUTH_JWT_PRIVATE_KEY` | PEM RSA private key для подписи локального access JWT |
| `AUTH_JWT_PUBLIC_KEY` | Соответствующий PEM RSA public key для проверки JWT |
| `AUTH_JWT_ISSUER` | Фиксированное значение `iss` (`dmc-268-api`) |
| `AUTH_JWT_AUDIENCE` | Фиксированное значение `aud` (`dmc-268-ui`) |

OAuth-код и client secret отправляются в теле POST запроса к GitHub, а не в URL.
GitHub user access token используется только для чтения профиля и установок во время
callback; в PostgreSQL хранится только хеш локального refresh token.

1. Settings → Environments → **staging**.
2. Secrets: `STAGING_SSH_KEY` (только для Terraform-хоста), при желании `POSTGRES_PASSWORD`, секреты приложения из §1. Для выката как такового на курсовой VPS environment secrets не нужны — хватает organization secrets и repository variables; без секретов приложения стенд поднимается с выключенными вебхуками и авторизацией.
3. Variables: хост, SSH-порт, SHA256 SSH fingerprint, SSH-пользователь, опционально health URL и имя БД.

   Fingerprint после `terraform apply` (формат appleboy — строка `SHA256:…` из вывода):

   ```bash
   ssh-keyscan -p "${STAGING_SSH_PORT}" -H "${STAGING_HOST}" 2>/dev/null | ssh-keygen -lf - -E sha256
   ```
4. Protection rules: **Deployment branches and tags** → только `main`. Workflow Rollback и сам пропускает jobs вне `refs/heads/main`, но branch policy закрывает доступ к secrets environment для любого другого workflow и ветки.
   Required reviewers — решение при настройке environment. `deploy-staging`, `promote-staging` и `rollback` ссылаются на `staging`, поэтому каждый выкат запросит подтверждение дважды (deploy и promotion), а job, ожидающий подтверждения, возможно, держит группу `staging-deploy` и блокирует rollback (не проверено). Рекомендация для staging: без required reviewers; контроль — branch policy `main` и review PR.
5. Actions → General: **Allow GitHub Actions to create and approve pull requests** не нужен. Secret scanning и push protection — включить.

Jobs `deploy-staging`, `promote-staging` (CI/CD) и workflow Rollback ссылаются на `environment: staging`. Без значений environment пайплайн не выкатит стенд. `promote-staging` нужен environment ради SSH-чтения `.deploy-state` (про required reviewers — п. 4).

SSH на Terraform-хосте открыт миру намеренно: у GitHub-hosted runners нет стабильных egress IP. Защита — нестандартный порт (`STAGING_SSH_PORT`), вход только по ключу и fail2ban; подробности и break-glass — [INFRASTRUCTURE.md](INFRASTRUCTURE.md#41-ssh-доступ). Курсовой VPS общий: там порт 22 и пароль, sshd и firewall не меняем.

`ACTIONS_STEP_DEBUG` / `ACTIONS_RUNNER_DEBUG` для environment `staging` не включать: отладочные логи могут повторить переданные на SSH переменные.

---

## 3. Передача на staging

```mermaid
flowchart LR
  gha["GitHub Environment"] --> ssh["SSH/SCP, debug: false"]
  ssh --> script["deploy.sh / rollback.sh"]
  script --> envfile["<APP_DIR>/.env\nchmod 600"]
  script --> appenv["<APP_DIR>/app.env, api.env\nchmod 600"]
  envfile --> compose["docker compose --env-file"]
  appenv --> compose2["env_file: api, worker"]
  script --> logout["docker logout ghcr.io"]
```

1. Runner забирает secret/var только в job с `environment: staging`.
2. `appleboy/ssh-action` с `debug: false` передаёт в скрипт одноразовый `GITHUB_TOKEN` и, если задан, `POSTGRES_PASSWORD`. SSH-пароль VPS передаётся только как `password` действия, в скрипт он не попадает.
3. `deploy.sh` пишет `.env` с `umask 077` и `chmod 600`; без `POSTGRES_PASSWORD` генерирует пароль один раз на свежем хосте и отказывается генерировать, если `.env` или том Postgres уже есть. Пароль в stdout не печатается.
4. Compose читает `.env` на VM. Postgres, RabbitMQ и Redis слушают только docker-сеть проекта, `ports:` у них нет.
5. Секреты приложения шаг «Bundle application secrets» собирает в одну строку `APP_SECRETS_B64`: base64 от строк `ИМЯ=<base64 значения>`, только для заданных секретов. Строка маскируется (`::add-mask::`) и уходит через `envs:` SSH-действия; многострочные PEM так не ломаются в SSH и shell. На хосте `write_app_env_files` (`env-file.sh`) раскладывает значения по allowlist: `app.env` (`api` и `worker`) и `api.env` (только `api`), в одинарных кавычках — Compose читает их буквально, без подстановки `$`. Имя вне allowlist или значение с `'` валит выкат до `compose up`.
6. `GITHUB_TOKEN` нужен лишь для pull. Логин идёт во временный `DOCKER_CONFIG` (`mktemp -d`), а не в `/root/.docker/config.json`: выкаты API и UI под общим root не мешают друг другу. После последнего pull — `docker logout`, при выходе каталог удаляется; `unset GHCR_TOKEN`.

Rollback ничего из GitHub не шлёт повторно: пароли Postgres, RabbitMQ и Redis берёт из лежащего `.env`, а `app.env` и `api.env` не трогает — откатанные `api` и `worker` стартуют с теми же секретами. Секреты не привязаны к образу: откат образа не возвращает предыдущие значения секретов.

---

## 4. Git

В репозитории нет живых секретов. Игнор:

- `.env`, `*.tfvars`, `*.backend.hcl`, `*.tfstate*`
- ключи (`*.pem`, `id_rsa`, `id_ed25519`, `.aws/`)
- `credentials`, `credentials.json`

В git только `*.example`. Локально:

```bash
cp .env.example .env
cp terraform/api-staging/environments/staging.tfvars.example terraform/api-staging/environments/staging.tfvars
cp terraform/api-staging/environments/staging.backend.hcl.example terraform/api-staging/environments/staging.backend.hcl
```

PR и `main` гоняют Gitleaks (`--redact`) и Trivy scanner `secret`. Находка останавливает выкат.

---

## 5. Логи CI

| Мера | Как |
|---|---|
| Маскирование | GitHub скрывает значения `secrets.*` и `GITHUB_TOKEN` |
| Нет `set -x` | `set +o xtrace` в SSH-скрипте и на VM |
| Нет debug SSH | `debug: false` у appleboy |
| Нет echo пароля | скрипты печатают только image ref |
| Login в реестр | пароль в stdin, stdout login глушится |
| Gitleaks | `--redact`, чтобы находка не продублировала секрет |

Не делайте `echo "$POSTGRES_PASSWORD"`, `env`, `cat .env` в workflow.

Имена переменных в контейнере без значений — не через `env | cut -d= -f1`: строки многострочного PEM не содержат `=` и печатаются целиком. Безопасно:

```bash
cd /opt/dmc-268-api-staging
docker compose -p dmc-268-api-staging -f compose.yml -f compose.edge.yml --env-file .env \
  exec -T api python -c "import os; print(' '.join(sorted(os.environ)))"
```

---

## 6. Permissions

Workflow по умолчанию: `contents: read`. Расширение точечное.

| Job / workflow | Permissions | Почему |
|---|---|---|
| `secret-scan`, terraform, docker build/scan | `contents: read` | только checkout |
| `push-image` | `contents: read`, `packages: write`, `actions: read` | push в GHCR; `actions: read` — скачать artifact образа, прошедшего Trivy |
| `deploy-staging` | `contents: read`, `packages: read` | pull образа на VM (одноразовый `GITHUB_TOKEN`) |
| `promote-staging` (CI/CD) | `contents: read`, `packages: write` | `:staging-previous` ← `:staging`, `:staging` ← проверенный digest после health check; по SSH только читает `.deploy-state`, токен на VM не передаётся |
| Rollback staging: `rollback` | `contents: read`, `packages: read` | pull образа на VM |
| Rollback staging: `promote-staging` | `contents: read`, `packages: write` | синхронизировать `:staging` с откатанным образом |

`security-events`, `id-token`, `pull-requests` не выдаются; `actions: read` есть только у `push-image`. `packages: write` — только у jobs `promote-staging`, на VM этот токен не уходит. `HCLOUD_TOKEN` в Actions нет — apply вне CI.
