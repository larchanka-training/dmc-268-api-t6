#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
# Deploy the monitoring stack on the host (docs/MONITORING.md). Run as root after the application
# deploy: it reads the application's .env and joins its network, and changes neither, except the
# PostgreSQL roles it keeps in sync: grafana_reader and metrics_exporter (read-only, metadata
# columns) and backup_agent (pg_read_all_data for pg_dump of the pg-backup service).
#
# Inputs (environment):
#   API_DIR                    application dir with .env and env-file.sh (/opt/dmc-268-api-staging)
#   MONITORING_DIR             this stack's dir, default: the directory of this script
#   GRAFANA_ROOT_URL           public Grafana URL in edge mode (https://grafana.<APP_DOMAIN>/)
#   BACKUP_DIR                 host dir of the PostgreSQL dumps, default /var/backups/dmc-268/<project>
#   GRAFANA_ADMIN_PASSWORD     set: the Grafana admin password (GitHub secret, the team reads it
#                              there); empty or unset: keep the one in .env, generated on a fresh host
#   ALERT_TELEGRAM_BOT_TOKEN   } both set: alerts go to that Telegram chat; both empty: alerts are
#   ALERT_TELEGRAM_CHAT_ID     } not delivered; unset: keep the previous alert config (manual run)
#   BACKUP_S3_ENDPOINT, BACKUP_S3_BUCKET, BACKUP_S3_ACCESS_KEY_ID, BACKUP_S3_SECRET_ACCESS_KEY,
#   BACKUP_ENCRYPTION_PASSPHRASE
#                              all set: encrypted off-host copy of the dumps (compose.offsite.yml);
#                              all empty: dumps stay on this host; unset: keep the previous setting
set -euo pipefail
set +o xtrace

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MONITORING_DIR="${MONITORING_DIR:-${SCRIPT_DIR}}"
API_DIR="${API_DIR:-/opt/dmc-268-api}"
API_PROJECT="${API_PROJECT:-$(basename "${API_DIR}")}"
API_ENV_FILE="${API_DIR}/.env"
COMPOSE_PROJECT="${MONITORING_PROJECT:-dmc-268-monitoring}"
ENV_FILE="${MONITORING_DIR}/.env"
GRAFANA_ALIAS="${GRAFANA_ALIAS:-grafana-staging}"
# Container name of the edge proxy (project dmc-268-edge): unambiguous on the shared network.
CADDY_METRICS_TARGET="${CADDY_METRICS_TARGET:-dmc-268-edge-caddy-1:9180}"
# Alertmanager and Prometheus run as nobody; pg-backup as postgres of the alpine image.
NOBODY_UID=65534
POSTGRES_IMAGE_UID=70

# On the host env-file.sh is uploaded next to this script; in the repository it is a sibling dir.
if [[ -f "${SCRIPT_DIR}/env-file.sh" ]]; then
  # shellcheck source=../scripts/env-file.sh
  source "${SCRIPT_DIR}/env-file.sh"
else
  # shellcheck source=../scripts/env-file.sh
  source "${SCRIPT_DIR}/../scripts/env-file.sh"
fi
# shellcheck source=alertmanager-config.sh
source "${SCRIPT_DIR}/alertmanager-config.sh"

generate_password() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 24
  else
    od -An -N24 -tx1 /dev/urandom | tr -d ' \n'
  fi
}

# A setting kept in .env by the previous deploy, empty on a fresh host.
previous_setting() {
  if [[ -f "${ENV_FILE}" ]]; then read_compose_env_var "$1" "${ENV_FILE}" || true; fi
}

# A password of this stack: kept from .env, generated once on a fresh host.
stack_password() {
  local name="$1" value=""
  if [[ -f "${ENV_FILE}" ]]; then
    value="$(read_compose_env_var "${name}" "${ENV_FILE}" || true)"
  fi
  if [[ -z "${value}" ]]; then
    value="$(generate_password)"
    echo "generated ${name} on the host (stored in ${ENV_FILE})" >&2
  fi
  if [[ ! "${value}" =~ ^[0-9a-f]{48}$ ]]; then
    echo "${name} in ${ENV_FILE} is not a generated password" >&2
    return 1
  fi
  printf '%s' "${value}"
}

# The digest of a config dir, set as a container label: compose recreates the service exactly
# when its configuration changed. Secret files are left out (Alertmanager reads the token per send).
config_digest() {
  (cd "${MONITORING_DIR}" && find "$@" -type f ! -path '*/secrets/*' -print0 | sort -z \
    | xargs -0 sha256sum | sha256sum | cut -c1-16)
}

if [[ ! -f "${API_ENV_FILE}" ]]; then
  echo "${API_ENV_FILE} not found: deploy the application stack first" >&2
  exit 1
fi

POSTGRES_USER="$(read_compose_env_var POSTGRES_USER "${API_ENV_FILE}" || true)"
POSTGRES_DB="$(read_compose_env_var POSTGRES_DB "${API_ENV_FILE}" || true)"
REDIS_PASSWORD="$(read_compose_env_var REDIS_PASSWORD "${API_ENV_FILE}" || true)"
DEPLOY_MODE="$(read_compose_env_var DEPLOY_MODE "${API_ENV_FILE}" || true)"
POSTGRES_USER="${POSTGRES_USER:-app}"
POSTGRES_DB="${POSTGRES_DB:-app}"
DEPLOY_MODE="${DEPLOY_MODE:-ports}"
# Both names go into SQL identifiers below.
for identifier in "${POSTGRES_USER}" "${POSTGRES_DB}"; do
  if [[ ! "${identifier}" =~ ^[a-z_][a-z0-9_]*$ ]]; then
    echo "POSTGRES_USER and POSTGRES_DB must be lowercase SQL identifiers" >&2
    exit 1
  fi
done
if [[ "${DEPLOY_MODE}" != "ports" && "${DEPLOY_MODE}" != "edge" ]]; then
  echo "DEPLOY_MODE in ${API_ENV_FILE} must be ports or edge, got: ${DEPLOY_MODE}" >&2
  exit 1
fi

if [[ "${DEPLOY_MODE}" == "edge" ]]; then
  if [[ ! "${GRAFANA_ROOT_URL:-}" =~ ^https://[A-Za-z0-9.-]+/$ ]]; then
    echo "GRAFANA_ROOT_URL must be https://<host>/ in edge mode, got: ${GRAFANA_ROOT_URL:-}" >&2
    exit 1
  fi
  GRAFANA_COOKIE_SECURE=true
else
  # Loopback only (compose.ports.yml), reached through an SSH tunnel.
  GRAFANA_ROOT_URL="http://localhost:3000/"
  GRAFANA_COOKIE_SECURE=false
fi

API_NETWORK="${API_PROJECT}_default"
if ! docker network inspect "${API_NETWORK}" >/dev/null 2>&1; then
  echo "network ${API_NETWORK} of the application stack not found" >&2
  exit 1
fi
POSTGRES_CONTAINER="$(docker ps -q \
  --filter "label=com.docker.compose.project=${API_PROJECT}" \
  --filter "label=com.docker.compose.service=postgres")"
if [[ -z "${POSTGRES_CONTAINER}" || "${POSTGRES_CONTAINER}" == *$'\n'* ]]; then
  echo "expected one running postgres container of project ${API_PROJECT}" >&2
  exit 1
fi

# Telegram settings: validated before anything on the host changes.
ALERTS_CONFIGURED=keep
if [[ -n "${ALERT_TELEGRAM_BOT_TOKEN+set}" || -n "${ALERT_TELEGRAM_CHAT_ID+set}" ]]; then
  token="${ALERT_TELEGRAM_BOT_TOKEN:-}"
  chat_id="${ALERT_TELEGRAM_CHAT_ID:-}"
  if [[ -n "${token}" && -n "${chat_id}" ]]; then
    if [[ ! "${token}" =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]]; then
      echo "ALERT_TELEGRAM_BOT_TOKEN is not a Telegram bot token" >&2
      exit 1
    fi
    if [[ ! "${chat_id}" =~ ^-?[0-9]{1,20}$ ]]; then
      echo "ALERT_TELEGRAM_CHAT_ID must be a numeric Telegram chat id" >&2
      exit 1
    fi
    ALERTS_CONFIGURED=telegram
  elif [[ -z "${token}" && -z "${chat_id}" ]]; then
    ALERTS_CONFIGURED=none
  else
    echo "set both ALERT_TELEGRAM_BOT_TOKEN and ALERT_TELEGRAM_CHAT_ID, or neither" >&2
    exit 1
  fi
fi
unset ALERT_TELEGRAM_BOT_TOKEN

# Off-host backup copy (docs/MONITORING.md §9), validated before anything on the host changes.
OFFSITE_KEYS=(BACKUP_S3_ENDPOINT BACKUP_S3_BUCKET BACKUP_S3_ACCESS_KEY_ID BACKUP_S3_SECRET_ACCESS_KEY
  BACKUP_ENCRYPTION_PASSPHRASE)
OFFSITE_CONFIGURED=keep
offsite_given=0
offsite_set=0
for name in "${OFFSITE_KEYS[@]}"; do
  if [[ -n "${!name+set}" ]]; then offsite_given=1; fi
  if [[ -n "${!name:-}" ]]; then offsite_set=$((offsite_set + 1)); fi
done
if ((offsite_given)); then
  if ((offsite_set == 0)); then
    OFFSITE_CONFIGURED=none
  elif ((offsite_set == ${#OFFSITE_KEYS[@]})); then
    # Plain HTTP only for a local S3 stand-in: the keys would cross the network in clear.
    if [[ ! "${BACKUP_S3_ENDPOINT}" =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?/?$ ]] \
      && ! [[ "${BACKUP_S3_ALLOW_HTTP:-}" == 1 && "${BACKUP_S3_ENDPOINT}" =~ ^http://[A-Za-z0-9.-]+(:[0-9]+)?/?$ ]]; then
      echo "BACKUP_S3_ENDPOINT must be https://<host>, got: ${BACKUP_S3_ENDPOINT}" >&2
      exit 1
    fi
    if [[ ! "${BACKUP_S3_BUCKET}" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]]; then
      echo "BACKUP_S3_BUCKET is not a valid bucket name: ${BACKUP_S3_BUCKET}" >&2
      exit 1
    fi
    for name in BACKUP_S3_ACCESS_KEY_ID BACKUP_S3_SECRET_ACCESS_KEY; do
      if [[ ! "${!name}" =~ ^[A-Za-z0-9/+=_-]{8,128}$ ]]; then
        echo "${name} has an unexpected format" >&2
        exit 1
      fi
    done
    if ((${#BACKUP_ENCRYPTION_PASSPHRASE} < 32)) || [[ "${BACKUP_ENCRYPTION_PASSPHRASE}" == *$'\n'* ]]; then
      echo "BACKUP_ENCRYPTION_PASSPHRASE must be one line of at least 32 characters" >&2
      exit 1
    fi
    OFFSITE_CONFIGURED=s3
  else
    echo "set all of ${OFFSITE_KEYS[*]}, or none" >&2
    exit 1
  fi
fi

# The admin password of Grafana: from the environment when set (GitHub secret), otherwise the one in
# .env. Written to .env unquoted, so only characters compose reads literally.
GRAFANA_ADMIN_PATTERN='^[A-Za-z0-9._~+/=-]{16,128}$'
if [[ -n "${GRAFANA_ADMIN_PASSWORD:-}" ]]; then
  if [[ ! "${GRAFANA_ADMIN_PASSWORD}" =~ ${GRAFANA_ADMIN_PATTERN} ]]; then
    echo "GRAFANA_ADMIN_PASSWORD must be 16-128 characters of A-Z a-z 0-9 . _ ~ + / = -" >&2
    exit 1
  fi
else
  GRAFANA_ADMIN_PASSWORD="$(previous_setting GRAFANA_ADMIN_PASSWORD)"
  if [[ ! "${GRAFANA_ADMIN_PASSWORD}" =~ ${GRAFANA_ADMIN_PATTERN} ]]; then
    GRAFANA_ADMIN_PASSWORD="$(stack_password GRAFANA_ADMIN_PASSWORD)"
  fi
fi
GRAFANA_DB_PASSWORD="$(stack_password GRAFANA_DB_PASSWORD)"
PG_EXPORTER_PASSWORD="$(stack_password PG_EXPORTER_PASSWORD)"
PG_BACKUP_PASSWORD="$(stack_password PG_BACKUP_PASSWORD)"
# Dumps of the application database, outside every stack dir (docs/MONITORING.md §9).
BACKUP_DIR="${BACKUP_DIR:-/var/backups/dmc-268/${API_PROJECT}}"

# The crypt remote of rclone takes the passphrase in its obscured form. Obscuring is randomised, so
# the previous value is reused while the passphrase is the same: a new value would recreate the
# service on every deploy. The passphrase reaches rclone through stdin, never argv.
OFFSITE_SETTINGS=(BACKUP_S3_PROVIDER BACKUP_S3_ENDPOINT BACKUP_S3_BUCKET BACKUP_S3_ACCESS_KEY_ID
  BACKUP_S3_SECRET_ACCESS_KEY BACKUP_ENCRYPTION_PASSWORD_OBSCURED BACKUP_ENCRYPTION_PASSPHRASE_SHA256)
case "${OFFSITE_CONFIGURED}" in
  s3)
    BACKUP_S3_PROVIDER="${BACKUP_S3_PROVIDER:-Cloudflare}"
    BACKUP_ENCRYPTION_PASSPHRASE_SHA256="$(printf '%s' "${BACKUP_ENCRYPTION_PASSPHRASE}" | sha256sum | cut -d' ' -f1)"
    BACKUP_ENCRYPTION_PASSWORD_OBSCURED=""
    if [[ "$(previous_setting BACKUP_ENCRYPTION_PASSPHRASE_SHA256)" == "${BACKUP_ENCRYPTION_PASSPHRASE_SHA256}" ]]; then
      BACKUP_ENCRYPTION_PASSWORD_OBSCURED="$(previous_setting BACKUP_ENCRYPTION_PASSWORD_OBSCURED)"
    fi
    if [[ -z "${BACKUP_ENCRYPTION_PASSWORD_OBSCURED}" ]]; then
      rclone_image="$(sed -n 's/^    image: \(rclone\/rclone:.*\)$/\1/p' "${MONITORING_DIR}/compose.offsite.yml")"
      BACKUP_ENCRYPTION_PASSWORD_OBSCURED="$(printf '%s' "${BACKUP_ENCRYPTION_PASSPHRASE}" \
        | docker run --rm -i --entrypoint rclone "${rclone_image}" obscure -)"
    fi
    if [[ ! "${BACKUP_ENCRYPTION_PASSWORD_OBSCURED}" =~ ^[A-Za-z0-9_-]+$ ]]; then
      echo "failed to prepare the backup encryption key" >&2
      exit 1
    fi
    echo "backups: encrypted off-host copy to bucket ${BACKUP_S3_BUCKET}"
    ;;
  none)
    for name in "${OFFSITE_SETTINGS[@]}"; do unset "${name}"; done
    echo "backups: BACKUP_S3_* are not set; dumps stay on this host only" >&2
    ;;
  keep)
    for name in "${OFFSITE_SETTINGS[@]}"; do printf -v "${name}" '%s' "$(previous_setting "${name}")"; done
    if [[ -n "${BACKUP_ENCRYPTION_PASSWORD_OBSCURED}" ]]; then OFFSITE_CONFIGURED=s3; fi
    ;;
esac
unset BACKUP_ENCRYPTION_PASSPHRASE

# Read-only roles. Grants are rebuilt on every deploy: REVOKE on a table also drops its column
# privileges, so the list below is exactly what the roles can read. No column holds review
# content: run_actions.request/response, runs.review_body/error_message, diffs and findings stay
# closed. The statements carry no secret and run with visible errors.
run_psql() {
  docker exec -i "${POSTGRES_CONTAINER}" \
    psql -X -q -v ON_ERROR_STOP=1 -U "${POSTGRES_USER}" -d "${POSTGRES_DB}"
}
run_psql <<SQL
SET client_min_messages = warning;
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'grafana_reader') THEN
    CREATE ROLE grafana_reader LOGIN;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'metrics_exporter') THEN
    CREATE ROLE metrics_exporter LOGIN;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'backup_agent') THEN
    CREATE ROLE backup_agent LOGIN;
  END IF;
END
\$\$;
ALTER ROLE grafana_reader WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION CONNECTION LIMIT 5;
ALTER ROLE metrics_exporter WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION CONNECTION LIMIT 3;
ALTER ROLE grafana_reader SET default_transaction_read_only = on;
ALTER ROLE grafana_reader SET statement_timeout = '30s';
ALTER ROLE metrics_exporter SET default_transaction_read_only = on;
ALTER ROLE metrics_exporter SET statement_timeout = '15s';
GRANT pg_monitor TO metrics_exporter;
GRANT CONNECT ON DATABASE "${POSTGRES_DB}" TO grafana_reader, metrics_exporter;
GRANT USAGE ON SCHEMA public TO grafana_reader, metrics_exporter;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM grafana_reader, metrics_exporter;
GRANT SELECT (id, state, trigger, engine, attempt, error_code, available_at, created_at, started_at, finished_at)
  ON runs TO grafana_reader, metrics_exporter;
GRANT SELECT (run_id, tool, started_at, duration_ms) ON run_actions TO grafana_reader, metrics_exporter;
GRANT SELECT (provider, model, operation, tokens_in, tokens_out, cache_read_tokens, cost_usd, created_at)
  ON usage_events TO grafana_reader;
-- pg-backup: reads everything for pg_dump; CREATEDB only for the scratch database of the restore
-- check, which it creates, owns and drops. No superuser, no write access to the application data.
ALTER ROLE backup_agent WITH LOGIN NOSUPERUSER CREATEDB NOCREATEROLE NOREPLICATION CONNECTION LIMIT 3;
GRANT pg_read_all_data TO backup_agent;
GRANT CONNECT ON DATABASE "${POSTGRES_DB}" TO backup_agent;
SQL
# Passwords through stdin, never argv; output and errors are dropped because psql would echo the
# failing statement, password included.
if ! run_psql >/dev/null 2>&1 <<SQL
ALTER ROLE grafana_reader PASSWORD '${GRAFANA_DB_PASSWORD}';
ALTER ROLE metrics_exporter PASSWORD '${PG_EXPORTER_PASSWORD}';
ALTER ROLE backup_agent PASSWORD '${PG_BACKUP_PASSWORD}';
SQL
then
  echo "failed to set the passwords of the monitoring roles in PostgreSQL" >&2
  exit 1
fi

umask 077
mkdir -p "${MONITORING_DIR}/alertmanager/secrets" "${MONITORING_DIR}/prometheus/targets" \
  "${MONITORING_DIR}/textfile"
# pg-backup writes its metrics here; node-exporter (nobody) reads them.
chown "${POSTGRES_IMAGE_UID}:${POSTGRES_IMAGE_UID}" "${MONITORING_DIR}/textfile"
chmod 755 "${MONITORING_DIR}/textfile"
mkdir -p "${BACKUP_DIR}"
chown "${POSTGRES_IMAGE_UID}:${POSTGRES_IMAGE_UID}" "${BACKUP_DIR}"
chmod 700 "${BACKUP_DIR}"

TOKEN_FILE="${MONITORING_DIR}/alertmanager/secrets/telegram_bot_token"
AM_CONFIG="${MONITORING_DIR}/alertmanager/alertmanager.yml"
trap 'rm -f "${TOKEN_FILE}.tmp"' EXIT
case "${ALERTS_CONFIGURED}" in
  telegram)
    printf '%s' "${token}" > "${TOKEN_FILE}.tmp"
    chown "${NOBODY_UID}:${NOBODY_UID}" "${TOKEN_FILE}.tmp"
    chmod 400 "${TOKEN_FILE}.tmp"
    mv -f "${TOKEN_FILE}.tmp" "${TOKEN_FILE}"
    render_alertmanager_config "${AM_CONFIG}" "${chat_id}"
    echo "alerts: Telegram chat configured"
    ;;
  none)
    rm -f "${TOKEN_FILE}"
    render_alertmanager_config "${AM_CONFIG}" ""
    echo "alerts: ALERT_TELEGRAM_BOT_TOKEN and ALERT_TELEGRAM_CHAT_ID are not set; alerts are not delivered" >&2
    ;;
  keep)
    if [[ ! -f "${AM_CONFIG}" ]]; then
      render_alertmanager_config "${AM_CONFIG}" ""
      echo "alerts: no Telegram settings; alerts are not delivered" >&2
    fi
    ;;
esac
unset token
chmod 755 "${MONITORING_DIR}/alertmanager" "${MONITORING_DIR}/alertmanager/secrets"
chmod 644 "${AM_CONFIG}"

# Request metrics of the edge proxy exist in edge mode only.
CADDY_TARGETS="${MONITORING_DIR}/prometheus/targets/caddy.json"
if [[ "${DEPLOY_MODE}" == "edge" ]]; then
  printf '[{"targets": ["%s"]}]\n' "${CADDY_METRICS_TARGET}" > "${CADDY_TARGETS}.tmp"
else
  echo '[]' > "${CADDY_TARGETS}.tmp"
fi
chmod 644 "${CADDY_TARGETS}.tmp"
mv -f "${CADDY_TARGETS}.tmp" "${CADDY_TARGETS}"
# Read by nobody (Prometheus) and grafana (uid 472) through read-only mounts.
find "${MONITORING_DIR}/prometheus" "${MONITORING_DIR}/grafana" "${MONITORING_DIR}/postgres-exporter" \
  "${MONITORING_DIR}/alertmanager/templates" "${MONITORING_DIR}/backup" -type d -exec chmod 755 {} +
find "${MONITORING_DIR}/prometheus" "${MONITORING_DIR}/grafana" "${MONITORING_DIR}/postgres-exporter" \
  "${MONITORING_DIR}/alertmanager/templates" "${MONITORING_DIR}/backup" -type f -exec chmod 644 {} +

{
  printf 'API_NETWORK=%s\n' "$(escape_compose_value "${API_NETWORK}")"
  printf 'POSTGRES_DB=%s\n' "$(escape_compose_value "${POSTGRES_DB}")"
  printf 'REDIS_PASSWORD=%s\n' "$(escape_compose_value "${REDIS_PASSWORD}")"
  printf 'GRAFANA_ADMIN_PASSWORD=%s\n' "${GRAFANA_ADMIN_PASSWORD}"
  printf 'GRAFANA_DB_PASSWORD=%s\n' "${GRAFANA_DB_PASSWORD}"
  printf 'PG_EXPORTER_PASSWORD=%s\n' "${PG_EXPORTER_PASSWORD}"
  printf 'PG_BACKUP_PASSWORD=%s\n' "${PG_BACKUP_PASSWORD}"
  printf 'BACKUP_DIR=%s\n' "$(escape_compose_value "${BACKUP_DIR}")"
  if [[ "${OFFSITE_CONFIGURED}" == s3 ]]; then
    for name in "${OFFSITE_SETTINGS[@]}"; do
      printf '%s=%s\n' "${name}" "$(escape_compose_value "${!name}")"
    done
  fi
  printf 'GRAFANA_ROOT_URL=%s\n' "$(escape_compose_value "${GRAFANA_ROOT_URL}")"
  printf 'GRAFANA_COOKIE_SECURE=%s\n' "${GRAFANA_COOKIE_SECURE}"
  printf 'GRAFANA_ALIAS=%s\n' "$(escape_compose_value "${GRAFANA_ALIAS}")"
  printf 'TEXTFILE_DIR=%s\n' "$(escape_compose_value "${TEXTFILE_DIR:-${MONITORING_DIR}/textfile}")"
  printf 'PROMETHEUS_CONFIG_DIGEST=%s\n' "$(config_digest prometheus)"
  printf 'ALERTMANAGER_CONFIG_DIGEST=%s\n' "$(config_digest alertmanager)"
  printf 'GRAFANA_CONFIG_DIGEST=%s\n' "$(config_digest grafana/provisioning)"
  printf 'POSTGRES_EXPORTER_CONFIG_DIGEST=%s\n' "$(config_digest postgres-exporter)"
  printf 'PG_BACKUP_CONFIG_DIGEST=%s\n' "$(config_digest backup)"
} > "${ENV_FILE}.tmp"
chmod 600 "${ENV_FILE}.tmp"
mv -f "${ENV_FILE}.tmp" "${ENV_FILE}"

COMPOSE=(docker compose -p "${COMPOSE_PROJECT}" -f "${MONITORING_DIR}/compose.yml"
  -f "${MONITORING_DIR}/compose.${DEPLOY_MODE}.yml" --env-file "${ENV_FILE}")
# Without the off-host settings the service is left out, and --remove-orphans removes a running one.
if [[ "${OFFSITE_CONFIGURED}" == s3 ]]; then
  COMPOSE+=(-f "${MONITORING_DIR}/compose.offsite.yml")
fi
"${COMPOSE[@]}" up -d --remove-orphans --wait --wait-timeout 180

# GF_SECURITY_ADMIN_PASSWORD applies only when the Grafana volume is created: keep .env the
# source of truth for later deploys too. The password goes through stdin, not argv.
if ! printf '%s' "${GRAFANA_ADMIN_PASSWORD}" \
  | "${COMPOSE[@]}" exec -T grafana grafana cli --homepath /usr/share/grafana \
    admin reset-admin-password --password-from-stdin >/dev/null 2>&1; then
  echo "failed to sync the Grafana admin password" >&2
  exit 1
fi

echo "monitoring stack deployed: Grafana ${GRAFANA_ROOT_URL}"
