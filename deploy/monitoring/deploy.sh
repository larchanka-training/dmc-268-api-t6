#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
# Deploy the monitoring stack on the host (docs/MONITORING.md). Run as root after the application
# deploy: it reads the application's .env and joins its network, and changes neither, except the
# two read-only PostgreSQL roles it keeps in sync (grafana_reader, metrics_exporter).
#
# Inputs (environment):
#   API_DIR                    application dir with .env and env-file.sh (/opt/dmc-268-api-staging)
#   MONITORING_DIR             this stack's dir, default: the directory of this script
#   GRAFANA_ROOT_URL           public Grafana URL in edge mode (https://<api host>/grafana/)
#   ALERT_TELEGRAM_BOT_TOKEN   } both set: alerts go to that Telegram chat; both empty: alerts are
#   ALERT_TELEGRAM_CHAT_ID     } not delivered; unset: keep the previous alert config (manual run)
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
# Alertmanager and Prometheus run as nobody.
NOBODY_UID=65534

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
  if [[ ! "${GRAFANA_ROOT_URL:-}" =~ ^https://[A-Za-z0-9.-]+/grafana/$ ]]; then
    echo "GRAFANA_ROOT_URL must be https://<host>/grafana/ in edge mode, got: ${GRAFANA_ROOT_URL:-}" >&2
    exit 1
  fi
  GRAFANA_COOKIE_SECURE=true
else
  # Loopback only (compose.ports.yml), reached through an SSH tunnel.
  GRAFANA_ROOT_URL="http://localhost:3000/grafana/"
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

GRAFANA_ADMIN_PASSWORD="$(stack_password GRAFANA_ADMIN_PASSWORD)"
GRAFANA_DB_PASSWORD="$(stack_password GRAFANA_DB_PASSWORD)"
PG_EXPORTER_PASSWORD="$(stack_password PG_EXPORTER_PASSWORD)"

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
SQL
# Passwords through stdin, never argv; output and errors are dropped because psql would echo the
# failing statement, password included.
if ! run_psql >/dev/null 2>&1 <<SQL
ALTER ROLE grafana_reader PASSWORD '${GRAFANA_DB_PASSWORD}';
ALTER ROLE metrics_exporter PASSWORD '${PG_EXPORTER_PASSWORD}';
SQL
then
  echo "failed to set the passwords of the monitoring roles in PostgreSQL" >&2
  exit 1
fi

umask 077
mkdir -p "${MONITORING_DIR}/alertmanager/secrets" "${MONITORING_DIR}/prometheus/targets" \
  "${MONITORING_DIR}/textfile"
chmod 755 "${MONITORING_DIR}/textfile"

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
  "${MONITORING_DIR}/alertmanager/templates" -type d -exec chmod 755 {} +
find "${MONITORING_DIR}/prometheus" "${MONITORING_DIR}/grafana" "${MONITORING_DIR}/postgres-exporter" \
  "${MONITORING_DIR}/alertmanager/templates" -type f -exec chmod 644 {} +

{
  printf 'API_NETWORK=%s\n' "$(escape_compose_value "${API_NETWORK}")"
  printf 'POSTGRES_DB=%s\n' "$(escape_compose_value "${POSTGRES_DB}")"
  printf 'REDIS_PASSWORD=%s\n' "$(escape_compose_value "${REDIS_PASSWORD}")"
  printf 'GRAFANA_ADMIN_PASSWORD=%s\n' "${GRAFANA_ADMIN_PASSWORD}"
  printf 'GRAFANA_DB_PASSWORD=%s\n' "${GRAFANA_DB_PASSWORD}"
  printf 'PG_EXPORTER_PASSWORD=%s\n' "${PG_EXPORTER_PASSWORD}"
  printf 'GRAFANA_ROOT_URL=%s\n' "$(escape_compose_value "${GRAFANA_ROOT_URL}")"
  printf 'GRAFANA_COOKIE_SECURE=%s\n' "${GRAFANA_COOKIE_SECURE}"
  printf 'GRAFANA_ALIAS=%s\n' "$(escape_compose_value "${GRAFANA_ALIAS}")"
  printf 'TEXTFILE_DIR=%s\n' "$(escape_compose_value "${TEXTFILE_DIR:-${MONITORING_DIR}/textfile}")"
  printf 'PROMETHEUS_CONFIG_DIGEST=%s\n' "$(config_digest prometheus)"
  printf 'ALERTMANAGER_CONFIG_DIGEST=%s\n' "$(config_digest alertmanager)"
  printf 'GRAFANA_CONFIG_DIGEST=%s\n' "$(config_digest grafana/provisioning)"
  printf 'POSTGRES_EXPORTER_CONFIG_DIGEST=%s\n' "$(config_digest postgres-exporter)"
} > "${ENV_FILE}.tmp"
chmod 600 "${ENV_FILE}.tmp"
mv -f "${ENV_FILE}.tmp" "${ENV_FILE}"

COMPOSE=(docker compose -p "${COMPOSE_PROJECT}" -f "${MONITORING_DIR}/compose.yml"
  -f "${MONITORING_DIR}/compose.${DEPLOY_MODE}.yml" --env-file "${ENV_FILE}")
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
