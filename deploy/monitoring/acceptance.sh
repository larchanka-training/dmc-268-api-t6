#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
# Acceptance evidence of the monitoring stack (#186, docs/MONITORING.md §10), run on the host by the
# manual workflow monitoring-acceptance.yml after deploy.sh. It changes nothing on the host except a
# scratch database of the restore drill, dropped at the end, and, with SEND_TEST_ALERT=1, one test
# alert to the team chat. The report carries counts, names and errors only: no passwords, no review
# content.
#
# Inputs (environment):
#   API_DIR           application dir with .env (/opt/dmc-268-api-staging)
#   MONITORING_DIR    this stack's dir with the .env of deploy.sh, default: the directory of this script
#   SEND_TEST_ALERT   1: send one test alert through Alertmanager and wait for its delivery; default 1
#   RUN_URL           link of the workflow run, put into the test alert
set -euo pipefail
set +o xtrace

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MONITORING_DIR="${MONITORING_DIR:-${SCRIPT_DIR}}"
API_DIR="${API_DIR:-/opt/dmc-268-api}"
API_PROJECT="${API_PROJECT:-$(basename "${API_DIR}")}"
API_ENV_FILE="${API_DIR}/.env"
COMPOSE_PROJECT="${MONITORING_PROJECT:-dmc-268-monitoring}"
ENV_FILE="${MONITORING_DIR}/.env"
SEND_TEST_ALERT="${SEND_TEST_ALERT:-1}"
RUN_URL="${RUN_URL:-manual run}"
if [[ "${RUN_URL}" == *'"'* || "${RUN_URL}" == *\\* ]]; then
  echo "RUN_URL must not contain quotes or backslashes" >&2
  exit 1
fi
# Thresholds of the alerts PostgresBackupStale and PostgresBackupOffsiteStale.
DUMP_MAX_AGE_SECONDS=$((26 * 3600))
OFFSITE_MAX_AGE_SECONDS=$((3 * 3600))
# Alertmanager waits group_wait (90 s for events) before the first notification of a group.
ALERT_DELIVERY_TIMEOUT_SECONDS="${ALERT_DELIVERY_TIMEOUT_SECONDS:-180}"
DRILL_DB=restore_drill_acceptance

if [[ -f "${SCRIPT_DIR}/env-file.sh" ]]; then
  # shellcheck source=../scripts/env-file.sh
  source "${SCRIPT_DIR}/env-file.sh"
else
  # shellcheck source=../scripts/env-file.sh
  source "${SCRIPT_DIR}/../scripts/env-file.sh"
fi

for file in "${API_ENV_FILE}" "${ENV_FILE}"; do
  if [[ ! -f "${file}" ]]; then
    echo "${file} not found: deploy the application and the monitoring stack first" >&2
    exit 1
  fi
done
setting() { read_compose_env_var "$1" "${ENV_FILE}" || true; }
POSTGRES_USER="$(read_compose_env_var POSTGRES_USER "${API_ENV_FILE}" || true)"
POSTGRES_USER="${POSTGRES_USER:-app}"
POSTGRES_DB="$(setting POSTGRES_DB)"
POSTGRES_DB="${POSTGRES_DB:-app}"
for identifier in "${POSTGRES_USER}" "${POSTGRES_DB}"; do
  if [[ ! "${identifier}" =~ ^[a-z_][a-z0-9_]*$ ]]; then
    echo "POSTGRES_USER and POSTGRES_DB must be lowercase SQL identifiers" >&2
    exit 1
  fi
done
BACKUP_DIR="$(setting BACKUP_DIR)"
TEXTFILE_DIR="$(setting TEXTFILE_DIR)"
TEXTFILE_DIR="${TEXTFILE_DIR:-${MONITORING_DIR}/textfile}"

COMPOSE=(docker compose -p "${COMPOSE_PROJECT}")
POSTGRES_CONTAINER="$(docker ps -q \
  --filter "label=com.docker.compose.project=${API_PROJECT}" \
  --filter "label=com.docker.compose.service=postgres")"
if [[ -z "${POSTGRES_CONTAINER}" || "${POSTGRES_CONTAINER}" == *$'\n'* ]]; then
  echo "expected one running postgres container of project ${API_PROJECT}" >&2
  exit 1
fi

FAILED=()
RESULTS=()
# One line of the summary; a failed check fails the run at the end, after every other check ran.
record() {
  local status="$1" name="$2" detail="$3"
  RESULTS+=("$(printf '%-4s  %-34s  %s' "${status}" "${name}" "${detail}")")
  if [[ "${status}" == FAIL ]]; then FAILED+=("${name}"); fi
}
section() { printf '\n== %s\n' "$1"; }
now() { date -u +%s; }
# psql in the application's postgres container. As a monitoring role the password comes from this
# stack's .env and reaches docker through the environment (-e NAME without a value), not argv.
psql_app() {
  docker exec -i "${POSTGRES_CONTAINER}" psql -X -q -At -v ON_ERROR_STOP=1 \
    -U "${POSTGRES_USER}" "$@"
}
psql_role() {
  local role="$1" password="$2"
  shift 2
  PGPASSWORD="${password}" docker exec -i -e PGPASSWORD "${POSTGRES_CONTAINER}" \
    psql -X -q -At -v ON_ERROR_STOP=1 -h 127.0.0.1 -U "${role}" -d "${POSTGRES_DB}" "$@"
}
promql() {
  "${COMPOSE[@]}" exec -T prometheus promtool query instant http://localhost:9090 "$1"
}
# The value of a metric in a textfile of node-exporter, empty when absent.
textfile_metric() {
  local file="${TEXTFILE_DIR}/$1"
  [[ -f "${file}" ]] || return 0
  awk -v name="$2" '$1 == name { printf "%d", $2 }' "${file}"
}
age() {
  local seconds="$1"
  printf '%dh %02dm' $((seconds / 3600)) $((seconds % 3600 / 60))
}

printf 'Monitoring acceptance on %s, %s\n' "$(hostname)" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"

section "Stack ${COMPOSE_PROJECT}"
services="$("${COMPOSE[@]}" ps --format '{{.Service}} {{.State}} {{.Health}}')"
printf '%s\n' "${services}"
not_running="$(awk '$2 != "running" || ($3 != "" && $3 != "healthy") { print $1 }' <<<"${services}" | paste -sd, -)"
if [[ -z "${services}" ]]; then
  record FAIL "stack services" "no service of ${COMPOSE_PROJECT} is running"
elif [[ -n "${not_running}" ]]; then
  record FAIL "stack services" "not running or unhealthy: ${not_running}"
else
  record OK "stack services" "$(wc -l <<<"${services}" | tr -d ' ') running, healthchecks healthy"
fi

section "Prometheus targets"
targets="$(promql 'up' || true)"
printf '%s\n' "${targets}"
down="$(grep -E '=> 0 @' <<<"${targets}" | sed -E 's/.*job="([^"]+)".*/\1/' | sort -u | paste -sd, - || true)"
total="$(grep -cE '=> [01] @' <<<"${targets}" || true)"
if [[ "${total}" -eq 0 ]]; then
  record FAIL "prometheus targets" "query up returned nothing"
elif [[ -n "${down}" ]]; then
  record FAIL "prometheus targets" "down: ${down}"
else
  record OK "prometheus targets" "${total} up"
fi

section "Dashboard data (DoD: LLM latency, queue size, RPS)"
GRAFANA_DB_PASSWORD="$(setting GRAFANA_DB_PASSWORD)"
llm="$(psql_role grafana_reader "${GRAFANA_DB_PASSWORD}" -F ' ' -c \
  "SELECT count(duration_ms),
          coalesce(round((percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms) / 1000.0)::numeric, 1), 0),
          coalesce(round((percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) / 1000.0)::numeric, 1), 0)
     FROM run_actions WHERE tool = 'llm.call' AND started_at > now() - interval '7 days'" 2>&1 || true)"
if [[ "${llm}" =~ ^([0-9]+)\ ([0-9.]+)\ ([0-9.]+)$ ]]; then
  echo "LLM calls, 7 days (as grafana_reader): ${BASH_REMATCH[1]}, p50 ${BASH_REMATCH[2]} s, p95 ${BASH_REMATCH[3]} s"
  record OK "LLM latency (Grafana source)" "${BASH_REMATCH[1]} calls in 7 days, p95 ${BASH_REMATCH[3]} s"
else
  echo "${llm}"
  record FAIL "LLM latency (Grafana source)" "query as grafana_reader failed"
fi
queues="$(promql 'sum by (queue) (rabbitmq_detailed_queue_messages_ready)' || true)"
printf 'Ready messages by queue:\n%s\n' "${queues}"
if grep -q 'queue=' <<<"${queues}"; then
  record OK "queue size (RabbitMQ)" "$(grep -c 'queue=' <<<"${queues}") queues reported"
else
  record FAIL "queue size (RabbitMQ)" "no rabbitmq_detailed_queue_messages_ready series"
fi
rps="$(promql 'sum by (host) (rate(caddy_http_request_duration_seconds_count[5m]))' || true)"
printf 'Requests per second by host (5m):\n%s\n' "${rps}"
if grep -q 'host=' <<<"${rps}"; then
  record OK "RPS (edge proxy)" "$(grep -c 'host=' <<<"${rps}") hosts reported"
else
  record FAIL "RPS (edge proxy)" "no caddy_http_request_duration_seconds_count series"
fi

section "Monitoring roles cannot read trace bodies, reviews or diffs"
PG_EXPORTER_PASSWORD="$(setting PG_EXPORTER_PASSWORD)"
denied_ok=1
for role in grafana_reader metrics_exporter; do
  if [[ "${role}" == grafana_reader ]]; then password="${GRAFANA_DB_PASSWORD}"; else password="${PG_EXPORTER_PASSWORD}"; fi
  allowed="$(psql_role "${role}" "${password}" -c 'SELECT count(run_id) FROM run_actions' 2>&1 || true)"
  echo "${role}: SELECT count(run_id) FROM run_actions -> ${allowed}"
  if [[ ! "${allowed}" =~ ^[0-9]+$ ]]; then denied_ok=0; fi
  for query in "SELECT request FROM run_actions LIMIT 1" "SELECT review_body FROM runs LIMIT 1"; do
    out="$(psql_role "${role}" "${password}" -c "${query}" 2>&1 || true)"
    if [[ "${out}" == *"permission denied"* ]]; then
      echo "${role}: ${query} -> $(grep -m1 'permission denied' <<<"${out}")"
    else
      # The run log is public: a readable column is reported, its content is not.
      echo "${role}: ${query} -> NOT DENIED (output hidden)"
      denied_ok=0
    fi
  done
done
if [[ "${denied_ok}" -eq 1 ]]; then
  record OK "role isolation" "permission denied on run_actions.request and runs.review_body"
else
  record FAIL "role isolation" "a monitoring role read a closed column or lost its metadata columns"
fi

section "PostgreSQL backups"
last_success="$(textfile_metric pg_backup.prom dmc_pg_backup_last_success_timestamp_seconds)"
last_size="$(textfile_metric pg_backup.prom dmc_pg_backup_last_size_bytes)"
# Dump names carry their UTC time (<db>-<UTC>.dump): the last name is the newest dump.
dumps="$(find "${BACKUP_DIR}" -maxdepth 1 -name "${POSTGRES_DB}-*.dump" -type f 2>/dev/null | sort || true)"
newest="$(tail -1 <<<"${dumps}")"
newest="${newest##*/}"
echo "backup dir: ${BACKUP_DIR}, dumps: $(grep -c . <<<"${dumps}" || true)"
echo "newest dump: ${newest:-none}"
if [[ -n "${last_success}" && -n "${newest}" ]]; then
  dump_age=$(($(now) - last_success))
  echo "last verified dump: $(age "${dump_age}") ago, ${last_size:-?} bytes"
  if ((dump_age <= DUMP_MAX_AGE_SECONDS)); then
    record OK "verified dump < 26 h" "$(age "${dump_age}") old, ${newest}"
  else
    record FAIL "verified dump < 26 h" "last verified dump is $(age "${dump_age}") old"
  fi
else
  record FAIL "verified dump < 26 h" "no dump in ${BACKUP_DIR} or no metric dmc_pg_backup_last_success_timestamp_seconds"
fi
if [[ -n "$(setting BACKUP_S3_BUCKET)" ]]; then
  offsite_success="$(textfile_metric pg_backup_offsite.prom dmc_pg_backup_offsite_last_success_timestamp_seconds)"
  if [[ -n "${offsite_success}" ]] && (($(now) - offsite_success <= OFFSITE_MAX_AGE_SECONDS)); then
    record OK "off-host copy (R2) < 3 h" "$(age $(($(now) - offsite_success))) ago, bucket $(setting BACKUP_S3_BUCKET)"
  else
    record FAIL "off-host copy (R2) < 3 h" "no successful copy in the last 3 hours"
  fi
else
  record FAIL "off-host copy (R2) < 3 h" "BACKUP_S3_* are not set: dumps stay on the VPS only"
fi

section "Restore drill: newest dump into the scratch database ${DRILL_DB} (MONITORING.md §9, step 1)"
drop_drill() {
  docker exec "${POSTGRES_CONTAINER}" dropdb -U "${POSTGRES_USER}" --if-exists "${DRILL_DB}" >/dev/null 2>&1 || true
}
trap drop_drill EXIT
if [[ -n "${newest}" ]]; then
  dump="${BACKUP_DIR}/${newest}"
  drop_drill
  if docker exec "${POSTGRES_CONTAINER}" createdb -U "${POSTGRES_USER}" -O "${POSTGRES_USER}" "${DRILL_DB}" \
    && docker exec -i "${POSTGRES_CONTAINER}" pg_restore -U "${POSTGRES_USER}" -d "${DRILL_DB}" \
      --no-owner --role="${POSTGRES_USER}" --exit-on-error --single-transaction <"${dump}"; then
    restored_rev="$(psql_app -d "${DRILL_DB}" -c 'SELECT version_num FROM alembic_version' 2>&1 || true)"
    live_rev="$(psql_app -d "${POSTGRES_DB}" -c 'SELECT version_num FROM alembic_version' 2>&1 || true)"
    tables="$(psql_app -d "${DRILL_DB}" -c "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'" 2>&1 || true)"
    runs="$(psql_app -d "${DRILL_DB}" -c 'SELECT count(*) FROM runs' 2>&1 || true)"
    echo "restored: revision ${restored_rev}, ${tables} tables, ${runs} runs; live revision ${live_rev}"
    if [[ -n "${restored_rev}" && "${tables}" =~ ^[0-9]+$ && "${tables}" -gt 0 && "${runs}" =~ ^[0-9]+$ ]]; then
      note=""
      if [[ "${restored_rev}" != "${live_rev}" ]]; then note=" (live is ${live_rev}: migrated after the dump)"; fi
      record OK "restore drill" "${newest} -> revision ${restored_rev}${note}, ${tables} tables, ${runs} runs"
    else
      record FAIL "restore drill" "restored database has no alembic revision or tables"
    fi
  else
    record FAIL "restore drill" "createdb or pg_restore of ${newest} failed"
  fi
  drop_drill
  echo "scratch database ${DRILL_DB} dropped"
else
  record FAIL "restore drill" "no dump to restore"
fi

section "Test alert to the team chat"
notifications() {
  "${COMPOSE[@]}" exec -T alertmanager wget -qO- http://localhost:9093/metrics \
    | awk -v name="$1" 'index($0, name "{integration=\"telegram\"") == 1 { sum += $2 } END { printf "%d", sum }'
}
if [[ "${SEND_TEST_ALERT}" != 1 ]]; then
  record SKIP "test alert" "SEND_TEST_ALERT is not 1"
elif ! grep -q 'telegram_configs' "${MONITORING_DIR}/alertmanager/alertmanager.yml" 2>/dev/null; then
  record FAIL "test alert" "Telegram is not configured (ALERT_TELEGRAM_BOT_TOKEN, ALERT_TELEGRAM_CHAT_ID)"
else
  sent_before="$(notifications alertmanager_notifications_total)"
  failed_before="$(notifications alertmanager_notifications_failed_total)"
  # kind=event: the receiver without RESOLVED, so the chat gets exactly one message. Without an end
  # time Alertmanager resolves the alert after resolve_timeout (5 min). Values in double quotes: the
  # matcher parser of amtool rejects spaces and ':' in bare values.
  "${COMPOSE[@]}" exec -T alertmanager amtool alert add MonitoringAcceptanceTest \
    severity=info kind=event environment=staging \
    "--annotation=summary=\"Test alert of the monitoring acceptance run, no action needed\"" \
    "--annotation=description=\"${RUN_URL}\"" \
    --alertmanager.url=http://localhost:9093
  echo "alert MonitoringAcceptanceTest sent to Alertmanager, waiting for the Telegram notification"
  deadline=$(($(now) + ALERT_DELIVERY_TIMEOUT_SECONDS))
  sent="${sent_before}" failed="${failed_before}"
  while (($(now) < deadline)); do
    sent="$(notifications alertmanager_notifications_total)"
    failed="$(notifications alertmanager_notifications_failed_total)"
    if ((sent > sent_before || failed > failed_before)); then break; fi
    sleep 10
  done
  echo "telegram notifications: sent ${sent_before} -> ${sent}, failed ${failed_before} -> ${failed}"
  if ((failed > failed_before)); then
    record FAIL "test alert" "Alertmanager failed to deliver to Telegram (docker compose logs alertmanager)"
  elif ((sent > sent_before)); then
    record OK "test alert" "delivered to Telegram: MonitoringAcceptanceTest"
  else
    record FAIL "test alert" "no Telegram notification within ${ALERT_DELIVERY_TIMEOUT_SECONDS} s"
  fi
fi

section "Summary"
printf '%s\n' "${RESULTS[@]}"
if ((${#FAILED[@]} > 0)); then
  printf '\nfailed: %s\n' "$(IFS=,; echo "${FAILED[*]}")"
  exit 1
fi
printf '\nall checks passed\n'
