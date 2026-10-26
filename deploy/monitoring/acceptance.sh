#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
# Acceptance evidence of the monitoring stack (#186, docs/MONITORING.md §10), run on the host by the
# manual workflow monitoring-acceptance.yml after deploy.sh. It changes nothing on the host except a
# scratch database of the restore drill, dropped at the end, and, with SEND_TEST_ALERT=1, one test
# alert to the team chat. The run log is public: the report carries counts, names and the first line
# of an error only, never passwords, row content or the DETAIL of an error.
#
# Every check runs to the end; the failed ones are listed in the summary and fail the run. The last
# line of a run without a failed check is "all checks passed".
#
# Inputs (environment):
#   API_DIR                         application dir with .env (/opt/dmc-268-api-staging)
#   MONITORING_DIR                  this stack's dir with the .env of deploy.sh, default: the
#                                   directory of this script
#   SEND_TEST_ALERT                 1: send one test alert and wait until Telegram accepts it; default 1
#   RUN_URL, RUN_ID                 link and id of the workflow run, put into the test alert
#   ALERT_DELIVERY_TIMEOUT_SECONDS  wait for Telegram, default 360: longer than group_interval (5 min),
#                                   since any event alert of staging in the last 5 min keeps the
#                                   group of the test alert open (the event route groups by
#                                   environment only)
#   ALERT_POLL_SECONDS              interval of the Alertmanager metric reads, default 10
#
# Needs timeout(1) of coreutils on the host: every client call is limited, so a hung one fails its
# check instead of eating the command_timeout of the workflow, which would end the run without a report.
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
RUN_ID="${RUN_ID:-manual-$(date -u +%Y%m%dT%H%M%SZ)}"
if [[ "${RUN_URL}" == *'"'* || "${RUN_URL}" == *\\* ]]; then
  echo "RUN_URL must not contain quotes or backslashes" >&2
  exit 1
fi
if [[ ! "${RUN_ID}" =~ ^[A-Za-z0-9_-]+$ ]]; then
  echo "RUN_ID must be letters, digits, _ or -" >&2
  exit 1
fi
# Thresholds of the alerts PostgresBackupStale and PostgresBackupOffsiteStale.
DUMP_MAX_AGE_SECONDS=$((26 * 3600))
OFFSITE_MAX_AGE_SECONDS=$((3 * 3600))
ALERT_DELIVERY_TIMEOUT_SECONDS="${ALERT_DELIVERY_TIMEOUT_SECONDS:-360}"
ALERT_POLL_SECONDS="${ALERT_POLL_SECONDS:-10}"
DRILL_DB=restore_drill_acceptance
if ! command -v timeout >/dev/null; then
  echo "timeout(1) not found: install coreutils" >&2
  exit 1
fi

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
DEPLOY_MODE="$(read_compose_env_var DEPLOY_MODE "${API_ENV_FILE}" || true)"
DEPLOY_MODE="${DEPLOY_MODE:-ports}"
POSTGRES_DB="$(setting POSTGRES_DB)"
POSTGRES_DB="${POSTGRES_DB:-app}"
for identifier in "${POSTGRES_USER}" "${POSTGRES_DB}"; do
  if [[ ! "${identifier}" =~ ^[a-z_][a-z0-9_]*$ ]]; then
    echo "POSTGRES_USER and POSTGRES_DB must be lowercase SQL identifiers" >&2
    exit 1
  fi
done
if [[ "${DEPLOY_MODE}" != edge && "${DEPLOY_MODE}" != ports ]]; then
  echo "DEPLOY_MODE in ${API_ENV_FILE} must be edge or ports" >&2
  exit 1
fi
BACKUP_DIR="$(setting BACKUP_DIR)"
TEXTFILE_DIR="$(setting TEXTFILE_DIR)"
TEXTFILE_DIR="${TEXTFILE_DIR:-${MONITORING_DIR}/textfile}"
OFFSITE_BUCKET="$(setting BACKUP_S3_BUCKET)"

COMPOSE=(docker compose -p "${COMPOSE_PROJECT}")
# The files deploy.sh starts the stack with: the expected services come from them.
COMPOSE_FILES=(-f "${MONITORING_DIR}/compose.yml" -f "${MONITORING_DIR}/compose.${DEPLOY_MODE}.yml")
if [[ -n "${OFFSITE_BUCKET}" ]]; then COMPOSE_FILES+=(-f "${MONITORING_DIR}/compose.offsite.yml"); fi
FAILED=()
RESULTS=()
# One line of the summary; a failed check fails the run at the end, after every other check ran.
record() {
  local status="$1" name="$2" detail="$3"
  RESULTS+=("$(printf '%-4s  %-30s  %s' "${status}" "${name}" "${detail}")")
  if [[ "${status}" == FAIL ]]; then FAILED+=("${name}"); fi
}
section() { printf '\n== %s\n' "$1"; }
now() { date -u +%s; }
# stderr of a command whose stdout is parsed: a warning of docker compose is not a service name.
STDERR_FILE="$(mktemp)"
DRILL_STARTED=""
# The temporary file, and the scratch database of an aborted drill; the regular drop is checked.
cleanup() {
  rm -f "${STDERR_FILE}"
  if [[ -n "${DRILL_STARTED}" ]]; then drop_drill >/dev/null 2>&1 || true; fi
}
trap cleanup EXIT
# Runs a command with a time limit and reports a timeout as an error line.
limited() {
  local seconds="$1" status=0
  shift
  timeout "${seconds}" "$@" || status=$?
  if ((status == 124)); then echo "error: timed out after ${seconds} s" >&2; fi
  return "${status}"
}
# The first line of an error from psql, pg_restore or a client, without DETAIL or row content.
ERROR_LINE_RE='^(ERROR|FATAL|psql|pg_restore|createdb|dropdb|Error|error)[: ]'
# The error psql prints for a missing grant (two spaces after ERROR:, as psql writes it).
DENIED_RE='^ERROR:  permission denied for table '
error_line() {
  local line
  line="$(head -n 1 <<<"$1")"
  if [[ "${line}" =~ ${ERROR_LINE_RE} ]]; then
    printf '%s' "${line}"
  else
    printf '(output hidden)'
  fi
}
psql_app() {
  limited 60 docker exec -i "${POSTGRES_CONTAINER}" psql -X -q -At -v ON_ERROR_STOP=1 \
    -U "${POSTGRES_USER}" "$@"
}
# As a monitoring role: the password from this stack's .env reaches docker through the environment
# (-e NAME without a value), not argv.
psql_role() {
  local role="$1" password="$2"
  shift 2
  PGPASSWORD="${password}" limited 60 docker exec -i -e PGPASSWORD "${POSTGRES_CONTAINER}" \
    psql -X -q -At -v ON_ERROR_STOP=1 -h "${POSTGRES_ADDRESS}" -U "${role}" -d "${POSTGRES_DB}" "$@"
}
promql() {
  limited 60 "${COMPOSE[@]}" exec -T prometheus promtool query instant http://localhost:9090 "$1"
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

printf 'Monitoring acceptance, %s, deploy mode %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${DEPLOY_MODE}"

# The checks of the database need the postgres container of the application. Without it they fail
# with this reason and the other checks still run.
POSTGRES_PROBLEM="" POSTGRES_CONTAINER="" POSTGRES_ADDRESS=""
if ! out="$(limited 60 docker ps -q \
  --filter "label=com.docker.compose.project=${API_PROJECT}" \
  --filter "label=com.docker.compose.service=postgres" 2>&1)"; then
  POSTGRES_PROBLEM="docker ps failed: $(error_line "${out}")"
elif [[ -z "${out}" || "${out}" == *$'\n'* ]]; then
  POSTGRES_PROBLEM="expected one running postgres container of project ${API_PROJECT}"
else
  POSTGRES_CONTAINER="${out}"
  # The monitoring roles connect over the application network, like Grafana and postgres-exporter:
  # loopback inside the postgres image is trust and would accept a wrong password.
  if ! out="$(limited 60 docker inspect -f \
    "{{with index .NetworkSettings.Networks \"${API_PROJECT}_default\"}}{{.IPAddress}}{{end}}" \
    "${POSTGRES_CONTAINER}" 2>&1)"; then
    POSTGRES_PROBLEM="docker inspect of postgres failed: $(error_line "${out}")"
  elif [[ ! "${out}" =~ ^[0-9a-f.:]+$ ]]; then
    POSTGRES_PROBLEM="postgres has no address on network ${API_PROJECT}_default"
  else
    POSTGRES_ADDRESS="${out}"
  fi
fi
if [[ -n "${POSTGRES_PROBLEM}" ]]; then echo "postgres: ${POSTGRES_PROBLEM}"; fi

section "Stack ${COMPOSE_PROJECT}"
expected="" services=""
if ! expected="$(limited 60 "${COMPOSE[@]}" "${COMPOSE_FILES[@]}" --env-file "${ENV_FILE}" \
  config --services 2>"${STDERR_FILE}")"; then
  record FAIL "stack services" "docker compose config failed: $(error_line "$(cat "${STDERR_FILE}")")"
elif ! services="$(limited 60 "${COMPOSE[@]}" ps -a --format '{{.Service}} {{.State}} {{.Health}}' \
  2>"${STDERR_FILE}")"; then
  record FAIL "stack services" "docker compose ps failed: $(error_line "$(cat "${STDERR_FILE}")")"
else
  printf '%s\n' "${services}"
  problems=()
  while read -r service; do
    [[ -n "${service}" ]] || continue
    state="$(awk -v s="${service}" '$1 == s { print $2 " " $3; exit }' <<<"${services}")"
    case "${state}" in
      "running " | "running healthy") ;;
      "") problems+=("${service} missing") ;;
      *) problems+=("${service} ${state% }") ;;
    esac
  done <<<"${expected}"
  if ((${#problems[@]} > 0)); then
    record FAIL "stack services" "$(IFS=,; echo "${problems[*]}")"
  else
    record OK "stack services" "$(grep -c . <<<"${expected}") expected services running, healthchecks healthy"
  fi
fi

section "Prometheus targets"
if targets="$(promql 'up' 2>&1)"; then
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
else
  record FAIL "prometheus targets" "promtool failed: $(error_line "${targets}")"
fi

section "Dashboard data (DoD: LLM latency, queue size, RPS)"
GRAFANA_DB_PASSWORD="$(setting GRAFANA_DB_PASSWORD)"
# The default time range of the dashboard (now-24h): OK means the panels show data as opened.
if [[ -n "${POSTGRES_PROBLEM}" ]]; then
  record FAIL "LLM latency (Grafana source)" "${POSTGRES_PROBLEM}"
elif llm="$(psql_role grafana_reader "${GRAFANA_DB_PASSWORD}" -F ' ' -c \
  "SELECT count(duration_ms),
          coalesce(round((percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms) / 1000.0)::numeric, 1), 0),
          coalesce(round((percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) / 1000.0)::numeric, 1), 0)
     FROM run_actions WHERE tool = 'llm.call' AND started_at > now() - interval '24 hours'" 2>&1)" \
  && [[ "${llm}" =~ ^([0-9]+)\ ([0-9.]+)\ ([0-9.]+)$ ]]; then
  calls="${BASH_REMATCH[1]}" p50="${BASH_REMATCH[2]}" p95="${BASH_REMATCH[3]}"
  echo "LLM calls, 24 h (as grafana_reader): ${calls}, p50 ${p50} s, p95 ${p95} s"
  if ((calls > 0)); then
    record OK "LLM latency (Grafana source)" "${calls} calls in 24 h, p95 ${p95} s"
  else
    record FAIL "LLM latency (Grafana source)" "no llm.call in 24 h: the panels are empty; run a review, then rerun"
  fi
else
  echo "LLM query as grafana_reader: $(error_line "${llm}")"
  record FAIL "LLM latency (Grafana source)" "query as grafana_reader failed"
fi
# The queues of the review pipeline, as in the queue alerts (alerts.yml).
if queues="$(promql 'sum by (queue) (rabbitmq_detailed_queue_messages_ready{queue=~"review\\.run\\..+|review\\.publish"})' 2>&1)"; then
  printf 'Ready messages in the review queues:\n%s\n' "${queues}"
  if grep -q 'queue="review\.publish"' <<<"${queues}" && grep -q 'queue="review\.run\.' <<<"${queues}"; then
    record OK "queue size (RabbitMQ)" "$(grep -c 'queue=' <<<"${queues}") review queues reported"
  else
    record FAIL "queue size (RabbitMQ)" "no series for review.run.* and review.publish"
  fi
else
  record FAIL "queue size (RabbitMQ)" "promtool failed: $(error_line "${queues}")"
fi
if [[ "${DEPLOY_MODE}" != edge ]]; then
  record SKIP "RPS (edge proxy)" "no edge proxy in deploy mode ${DEPLOY_MODE}"
# The selector of the RPS panels: per_host of Caddy puts requests with an unknown Host (scanners
# hitting the IP) into _other, which the dashboard does not show.
elif rps="$(promql 'sum by (host) (rate(caddy_http_request_duration_seconds_count{host!="_other"}[5m]))' 2>&1)"; then
  printf 'Requests per second by host (5m):\n%s\n' "${rps}"
  hosts="$(grep -c 'host=' <<<"${rps}" || true)"
  active="$(awk '/host=/ { sub(/.*=> /, ""); sub(/ @.*/, ""); if ($0 + 0 > 0) n++ } END { print n + 0 }' <<<"${rps}")"
  if ((hosts == 0)); then
    record FAIL "RPS (edge proxy)" "no caddy_http_request_duration_seconds_count series of an application host"
  elif ((active == 0)); then
    record FAIL "RPS (edge proxy)" "no requests in 5 min on ${hosts} hosts: open the app, then rerun"
  else
    record OK "RPS (edge proxy)" "${active} of ${hosts} hosts with requests in 5 min"
  fi
else
  record FAIL "RPS (edge proxy)" "promtool failed: $(error_line "${rps}")"
fi

section "Monitoring roles cannot read trace bodies, reviews or diffs"
PG_EXPORTER_PASSWORD="$(setting PG_EXPORTER_PASSWORD)"
isolation=()
if [[ -n "${POSTGRES_PROBLEM}" ]]; then
  isolation+=("${POSTGRES_PROBLEM}")
else
  for role in grafana_reader metrics_exporter; do
    if [[ "${role}" == grafana_reader ]]; then password="${GRAFANA_DB_PASSWORD}"; else password="${PG_EXPORTER_PASSWORD}"; fi
    if out="$(psql_role "${role}" "${password}" -c 'SELECT count(run_id) FROM run_actions' 2>&1)" \
      && [[ "${out}" =~ ^[0-9]+$ ]]; then
      echo "${role}: SELECT count(run_id) FROM run_actions -> ${out}"
    else
      echo "${role}: SELECT count(run_id) FROM run_actions -> $(error_line "${out}")"
      isolation+=("${role} cannot read its metadata columns")
    fi
    for query in "SELECT request FROM run_actions LIMIT 1" "SELECT review_body FROM runs LIMIT 1"; do
      # Denied only by the exit code and the error psql prints for a missing grant. The output of a
      # query that succeeded is row content and never printed: the run log is public.
      if out="$(psql_role "${role}" "${password}" -c "${query}" 2>&1)"; then
        echo "${role}: ${query} -> NOT DENIED (output hidden)"
        isolation+=("${role}: ${query%% FROM*} readable")
      elif [[ "$(head -n 1 <<<"${out}")" =~ ${DENIED_RE} ]]; then
        echo "${role}: ${query} -> $(head -n 1 <<<"${out}")"
      else
        echo "${role}: ${query} -> $(error_line "${out}")"
        isolation+=("${role}: ${query%% FROM*} failed without permission denied")
      fi
    done
  done
fi
if ((${#isolation[@]} == 0)); then
  record OK "role isolation" "permission denied on run_actions.request and runs.review_body"
else
  record FAIL "role isolation" "$(IFS=';'; echo "${isolation[*]}")"
fi

section "PostgreSQL backups"
last_success="$(textfile_metric pg_backup.prom dmc_pg_backup_last_success_timestamp_seconds)"
last_size="$(textfile_metric pg_backup.prom dmc_pg_backup_last_size_bytes)"
# Dump names carry their UTC time (<db>-<UTC>.dump): the last name is the newest dump.
dumps="$(find "${BACKUP_DIR}" -maxdepth 1 -name "${POSTGRES_DB}-*.dump" -type f 2>/dev/null | sort || true)"
newest="$(tail -n 1 <<<"${dumps}")"
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
  record FAIL "verified dump < 26 h" "no dump in the backup dir or no metric dmc_pg_backup_last_success_timestamp_seconds"
fi
if [[ -n "${OFFSITE_BUCKET}" ]]; then
  offsite_success="$(textfile_metric pg_backup_offsite.prom dmc_pg_backup_offsite_last_success_timestamp_seconds)"
  if [[ -n "${offsite_success}" ]] && (($(now) - offsite_success <= OFFSITE_MAX_AGE_SECONDS)); then
    record OK "off-host copy (R2) < 3 h" "$(age $(($(now) - offsite_success))) ago"
  elif [[ -n "${offsite_success}" ]]; then
    record FAIL "off-host copy (R2) < 3 h" "last successful copy $(age $(($(now) - offsite_success))) ago"
  else
    record FAIL "off-host copy (R2) < 3 h" "no successful copy yet"
  fi
else
  record FAIL "off-host copy (R2) < 3 h" "BACKUP_S3_* are not set: dumps stay on the VPS only"
fi

section "Restore drill: newest dump into the scratch database ${DRILL_DB} (MONITORING.md §9, restore, step 1)"
# --force: a session left by a cancelled run would otherwise keep the database.
drop_drill() {
  limited 60 docker exec "${POSTGRES_CONTAINER}" dropdb -U "${POSTGRES_USER}" --force --if-exists "${DRILL_DB}" 2>&1
}
if [[ -n "${POSTGRES_PROBLEM}" ]]; then
  record FAIL "restore drill" "${POSTGRES_PROBLEM}"
  record SKIP "scratch db cleanup" "no drill"
elif [[ -n "${newest}" ]]; then
  dump="${BACKUP_DIR}/${newest}"
  restore_error=""
  DRILL_STARTED=1
  if ! out="$(drop_drill)"; then
    restore_error="dropdb of a previous drill failed: $(error_line "${out}")"
  elif ! out="$(limited 60 docker exec "${POSTGRES_CONTAINER}" createdb -U "${POSTGRES_USER}" -O "${POSTGRES_USER}" "${DRILL_DB}" 2>&1)"; then
    restore_error="createdb failed: $(error_line "${out}")"
  elif ! out="$(limited 300 docker exec -i "${POSTGRES_CONTAINER}" pg_restore -U "${POSTGRES_USER}" -d "${DRILL_DB}" \
    --no-owner --role="${POSTGRES_USER}" --exit-on-error --single-transaction <"${dump}" 2>&1)"; then
    # Only the error line: its DETAIL may quote a row of the dump.
    restore_error="pg_restore of ${newest} failed: $(grep -m 1 -E '^(pg_restore: )?error:' <<<"${out}" || echo '(output hidden)')"
  fi
  if [[ -n "${restore_error}" ]]; then
    echo "${restore_error}"
    record FAIL "restore drill" "${restore_error}"
  else
    restored_rev="$(psql_app -d "${DRILL_DB}" -c 'SELECT version_num FROM alembic_version' 2>&1 || true)"
    live_rev="$(psql_app -d "${POSTGRES_DB}" -c 'SELECT version_num FROM alembic_version' 2>&1 || true)"
    tables="$(psql_app -d "${DRILL_DB}" -c "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'" 2>&1 || true)"
    runs="$(psql_app -d "${DRILL_DB}" -c 'SELECT count(*) FROM runs' 2>&1 || true)"
    revision='^[0-9a-z_]+$'
    if [[ ! "${live_rev}" =~ ${revision} ]]; then live_rev=""; fi
    if [[ "${restored_rev}" =~ ${revision} && "${tables}" =~ ^[0-9]+$ && "${tables}" -gt 0 && "${runs}" =~ ^[0-9]+$ ]]; then
      echo "restored: revision ${restored_rev}, ${tables} tables, ${runs} runs; live revision ${live_rev:-unknown}"
      note=""
      if [[ -z "${live_rev}" ]]; then
        note=" (live revision unknown)"
      elif [[ "${restored_rev}" != "${live_rev}" ]]; then
        note=" (live is ${live_rev}: migrated after the dump)"
      fi
      record OK "restore drill" "${newest} -> revision ${restored_rev}${note}, ${tables} tables, ${runs} runs"
    else
      echo "restored database: revision $(error_line "${restored_rev}"), tables $(error_line "${tables}")"
      record FAIL "restore drill" "restored database has no alembic revision, tables or runs"
    fi
  fi
  left=""
  if out="$(drop_drill)" \
    && left="$(psql_app -d postgres -c "SELECT count(*) FROM pg_database WHERE datname = '${DRILL_DB}'" 2>&1)" \
    && [[ "${left}" == 0 ]]; then
    echo "scratch database ${DRILL_DB} dropped"
    record OK "scratch db cleanup" "${DRILL_DB} dropped, absent from pg_database"
  else
    echo "scratch database ${DRILL_DB} was not dropped: $(error_line "${out}${left:-}")"
    record FAIL "scratch db cleanup" "${DRILL_DB} is left on the host: dropdb --force ${DRILL_DB}"
  fi
else
  record FAIL "restore drill" "no dump to restore"
  record SKIP "scratch db cleanup" "no drill"
fi

section "Test alert to the team chat"
# Requests to the Telegram API (Alertmanager notify/metrics.go): notifications_total counts attempts
# before the request and failed_total only after all retries, so neither shows a delivery. Prints
# "<requests> <failed requests>"; a failed read returns non-zero instead of zeros.
telegram_requests() {
  local metrics
  metrics="$(limited 30 "${COMPOSE[@]}" exec -T alertmanager wget -qO- http://localhost:9093/metrics 2>&1)" || return 1
  awk '
    index($0, "alertmanager_notification_requests_total{integration=\"telegram\"") == 1 { total += $2 }
    index($0, "alertmanager_notification_requests_failed_total{integration=\"telegram\"") == 1 { failed += $2 }
    END { printf "%d %d", total, failed }
  ' <<<"${metrics}"
}
if [[ "${SEND_TEST_ALERT}" != 1 ]]; then
  record SKIP "test alert" "SEND_TEST_ALERT is not 1"
elif ! grep -q 'telegram_configs' "${MONITORING_DIR}/alertmanager/alertmanager.yml" 2>/dev/null; then
  record FAIL "test alert" "Telegram is not configured (ALERT_TELEGRAM_BOT_TOKEN, ALERT_TELEGRAM_CHAT_ID)"
elif ! before="$(telegram_requests)"; then
  record FAIL "test alert" "cannot read the metrics of Alertmanager"
else
  read -r total_before failed_before <<<"${before}"
  # kind=event: the receiver without RESOLVED, so the chat gets one message. run_id makes it a new
  # alert: with the labels of a previous run it would only refresh that one and send nothing.
  # Without an end time Alertmanager resolves it after resolve_timeout (5 min). Values in double
  # quotes: the matcher parser of amtool rejects spaces and ':' in bare values.
  if ! out="$(limited 60 "${COMPOSE[@]}" exec -T alertmanager amtool alert add MonitoringAcceptanceTest \
    severity=info kind=event environment=staging "run_id=${RUN_ID}" \
    "--annotation=summary=\"Test alert of the monitoring acceptance run, no action needed\"" \
    "--annotation=description=\"${RUN_URL}\"" \
    --alertmanager.url=http://localhost:9093 2>&1)"; then
    record FAIL "test alert" "amtool alert add failed: $(error_line "${out}")"
  else
    echo "alert MonitoringAcceptanceTest (run_id ${RUN_ID}) sent to Alertmanager, waiting for Telegram"
    deadline=$(($(now) + ALERT_DELIVERY_TIMEOUT_SECONDS))
    total="${total_before}" failed="${failed_before}" reads_ok=0 reads_failed=0
    while :; do
      # One line per read: the SSH session of the workflow sends no keepalive, and a quiet
      # connection could be dropped during the wait.
      if current="$(telegram_requests)"; then
        reads_ok=$((reads_ok + 1))
        read -r total failed <<<"${current}"
        echo "read $((reads_ok + reads_failed)): telegram requests +$((total - total_before)), failed +$((failed - failed_before))"
        # A failed first request with a successful retry is a delivery.
        if (((total - total_before) - (failed - failed_before) > 0)); then break; fi
      else
        reads_failed=$((reads_failed + 1))
        echo "read $((reads_ok + reads_failed)): cannot read the metrics of Alertmanager"
      fi
      (($(now) < deadline)) || break
      sleep "${ALERT_POLL_SECONDS}"
    done
    echo "telegram requests: ${total_before} -> ${total}, failed ${failed_before} -> ${failed}, unreadable metric reads ${reads_failed}"
    # The counters cover every notification to Telegram, not only this alert (docs/MONITORING.md §10).
    if (((total - total_before) - (failed - failed_before) > 0)); then
      record OK "test alert" "a notification to Telegram was accepted after the test alert"
    elif ((failed > failed_before)); then
      record FAIL "test alert" "every request to Telegram failed (token, chat id, network): docker compose logs alertmanager"
    elif ((reads_ok == 0)); then
      record FAIL "test alert" "cannot read the metrics of Alertmanager during the wait (${reads_failed} reads)"
    else
      record FAIL "test alert" "no request to Telegram within ${ALERT_DELIVERY_TIMEOUT_SECONDS} s"
    fi
  fi
fi

section "Summary"
printf '%s\n' "${RESULTS[@]}"
if ((${#FAILED[@]} > 0)); then
  printf '\nfailed: %s\n' "$(IFS=,; echo "${FAILED[*]}")"
  exit 1
fi
printf '\nall checks passed\n'
