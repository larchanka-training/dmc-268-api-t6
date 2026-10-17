#!/bin/sh
# Scheduled PostgreSQL backup of the application database (docs/MONITORING.md §9). Runs in the
# pg-backup service of deploy/monitoring/compose.yml as the role backup_agent (pg_read_all_data,
# CREATEDB), with the libpq environment PGHOST, PGUSER, PGPASSWORD and PGDATABASE.
#
#   pg-backup.sh loop   check every BACKUP_POLL_SECONDS, back up when the newest dump is older
#                       than BACKUP_INTERVAL_SECONDS (a failed attempt is retried after
#                       BACKUP_RETRY_SECONDS); the service command
#   pg-backup.sh once   one backup now (manual run: docker compose exec pg-backup ... once)
#
# One backup:
#   1. free disk of at least twice the database size (the dump and the scratch restore);
#   2. a REPEATABLE READ transaction on the source exports its snapshot and takes the fingerprint
#      in it: the Alembic revision and the row count of every table of schema public;
#   3. pg_dump --snapshot dumps exactly that snapshot, in custom format, into BACKUP_DIR;
#   4. restore check: the dump goes into a scratch database on the same server, which must give
#      the same fingerprint, line for line; a migration or writes during the dump cannot make the
#      two differ, both come from one snapshot;
#   5. rename to <db>-<UTC stamp>.dump and pruning of dumps older than BACKUP_RETENTION_DAYS.
# The dump keeps owners and grants for a real restore; the check restores without them. Status goes
# to TEXTFILE as metrics for node-exporter (alerts PostgresBackupStale and PostgresBackupFailed),
# and every loop pass writes HEARTBEAT (alert PostgresBackupMissing: the service stopped).
set -eu

BACKUP_DIR="${BACKUP_DIR:-/backups}"
TEXTFILE="${TEXTFILE:-/textfile/pg_backup.prom}"
HEARTBEAT="${HEARTBEAT:-/textfile/pg_backup_heartbeat.prom}"
INTERVAL="${BACKUP_INTERVAL_SECONDS:-86400}"
RETRY="${BACKUP_RETRY_SECONDS:-3600}"
POLL="${BACKUP_POLL_SECONDS:-300}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"
CHECK_DB="${BACKUP_CHECK_DB:-dmc_restore_check}"
: "${PGDATABASE:?PGDATABASE is required}"
# Server NOTICEs (dropdb --if-exists of a missing database) are noise in the service log.
export PGOPTIONS="${PGOPTIONS:-} -c client_min_messages=warning"

# The fingerprint of a database: the Alembic revision, then "<table>=<rows>" for every table of
# schema public in name order. pg_class, not information_schema: the latter hides tables the role
# reads only through pg_read_all_data. query_to_xml counts inside the current transaction.
FINGERPRINT_SQL="SELECT line FROM (
  SELECT 0 AS ord, 'alembic_version=' || coalesce((SELECT string_agg(version_num, ',') FROM alembic_version), '-') AS line
  UNION ALL
  SELECT 1, c.relname || '=' || (xpath('/row/c/text()', query_to_xml(
    format('SELECT count(*) AS c FROM public.%I', c.relname), false, true, '')))[1]::text
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE n.nspname = 'public' AND c.relkind = 'r'
) AS fingerprint ORDER BY ord, line;"
FINGERPRINT_END="__fingerprint_end__"
SNAPSHOT_WAIT_SECONDS="${BACKUP_SNAPSHOT_WAIT_SECONDS:-300}"

umask 077

log() {
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) pg-backup: $*"
}

# Dump names carry a sortable UTC stamp: the newest is the last one.
newest_dump() {
  find "${BACKUP_DIR}" -maxdepth 1 -name "${PGDATABASE}-*.dump" | sort | tail -n 1
}

# Refuses before anything is written when the disk of BACKUP_DIR cannot hold the dump and the
# scratch restore: at most the database size each. On the VPS the backup dir and the docker volumes
# share the root filesystem.
check_free_disk() {
  database_bytes="$(psql -X -At -c 'SELECT pg_database_size(current_database())')"
  free_kib="$(df -Pk "${BACKUP_DIR}" | awk 'NR == 2 { print $4 }')"
  need_kib=$((database_bytes * 2 / 1024))
  if [ "${free_kib}" -lt "${need_kib}" ]; then
    log "not enough free disk: ${free_kib} KiB free, ${need_kib} KiB needed (twice the database)"
    return 1
  fi
}

# Dumps the snapshot of an open REPEATABLE READ transaction and writes the fingerprint taken in it.
# psql reads its commands from a fifo, so the transaction stays open while pg_dump runs.
dump_with_fingerprint() {
  dump="$1"
  fingerprint="$2"
  work="$(mktemp -d)"
  mkfifo "${work}/commands"
  psql -X -q -At -v ON_ERROR_STOP=1 < "${work}/commands" > "${work}/output" &
  psql_pid=$!
  exec 3> "${work}/commands"
  printf '%s\n' "BEGIN ISOLATION LEVEL REPEATABLE READ, READ ONLY;" "SELECT pg_export_snapshot();" \
    "${FINGERPRINT_SQL}" "\\echo ${FINGERPRINT_END}" >&3
  waited=0
  until grep -qx "${FINGERPRINT_END}" "${work}/output"; do
    if ! kill -0 "${psql_pid}" 2> /dev/null || [ "${waited}" -ge "${SNAPSHOT_WAIT_SECONDS}" ]; then
      log "could not open the snapshot transaction"
      return 1
    fi
    sleep 1
    waited=$((waited + 1))
  done
  snapshot="$(head -n 1 "${work}/output")"
  sed -e '1d' -e "/^${FINGERPRINT_END}\$/d" "${work}/output" > "${fingerprint}"
  pg_dump --format=custom --compress=6 --snapshot="${snapshot}" --file="${dump}"
  printf '%s\n' "COMMIT;" >&3
  exec 3>&-
  wait "${psql_pid}"
  rm -rf "${work}"
}

restore_check() {
  dump="$1"
  fingerprint="$2"
  dropdb --if-exists "${CHECK_DB}"
  createdb "${CHECK_DB}"
  pg_restore --no-owner --no-privileges --exit-on-error --dbname="${CHECK_DB}" "${dump}"
  psql -X -At -d "${CHECK_DB}" -c "${FINGERPRINT_SQL}" > "${fingerprint}.restored"
  dropdb "${CHECK_DB}"
  if ! cmp -s "${fingerprint}" "${fingerprint}.restored"; then
    log "restore check failed: the restored database differs from the snapshot of the dump"
    return 1
  fi
  tables="$(($(wc -l < "${fingerprint}") - 1))"
  rows="$(awk -F= 'NR > 1 { sum += $2 } END { print sum + 0 }' "${fingerprint}")"
  log "restore check passed: $(head -n 1 "${fingerprint}"), ${tables} tables, ${rows} rows match"
}

backup_once() {
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  final="${BACKUP_DIR}/${PGDATABASE}-${stamp}.dump"
  partial="${final}.partial"
  rm -f "${BACKUP_DIR}"/*.partial "${BACKUP_DIR}"/*.fingerprint*
  check_free_disk
  dump_with_fingerprint "${partial}" "${partial}.fingerprint"
  restore_check "${partial}" "${partial}.fingerprint"
  rm -f "${partial}.fingerprint" "${partial}.fingerprint.restored"
  mv "${partial}" "${final}"
  find "${BACKUP_DIR}" -maxdepth 1 -name "${PGDATABASE}-*.dump" -mtime "+${RETENTION_DAYS}" -delete
  log "backup ${final##*/}: $(wc -c < "${final}") bytes"
}

# Metrics of the last attempt; the last success is the mtime of the newest dump, so a restart of
# the container loses nothing.
write_metrics() {
  run_success="$1"
  newest="$(newest_dump)"
  {
    echo "# HELP dmc_pg_backup_last_run_success 1 if the last backup attempt succeeded."
    echo "# TYPE dmc_pg_backup_last_run_success gauge"
    echo "dmc_pg_backup_last_run_success ${run_success}"
    echo "# HELP dmc_pg_backup_last_run_timestamp_seconds Time of the last backup attempt."
    echo "# TYPE dmc_pg_backup_last_run_timestamp_seconds gauge"
    echo "dmc_pg_backup_last_run_timestamp_seconds $(date +%s)"
    echo "# HELP dmc_pg_backup_files Dumps kept in the backup directory."
    echo "# TYPE dmc_pg_backup_files gauge"
    echo "dmc_pg_backup_files $(find "${BACKUP_DIR}" -maxdepth 1 -name "${PGDATABASE}-*.dump" | wc -l)"
    if [ -n "${newest}" ]; then
      echo "# HELP dmc_pg_backup_last_success_timestamp_seconds Time of the newest verified dump."
      echo "# TYPE dmc_pg_backup_last_success_timestamp_seconds gauge"
      echo "dmc_pg_backup_last_success_timestamp_seconds $(stat -c %Y "${newest}")"
      echo "# HELP dmc_pg_backup_last_size_bytes Size of the newest verified dump."
      echo "# TYPE dmc_pg_backup_last_size_bytes gauge"
      echo "dmc_pg_backup_last_size_bytes $(wc -c < "${newest}")"
    fi
    if [ -n "${LAST_DURATION:-}" ]; then
      echo "# HELP dmc_pg_backup_last_duration_seconds Duration of the last successful backup."
      echo "# TYPE dmc_pg_backup_last_duration_seconds gauge"
      echo "dmc_pg_backup_last_duration_seconds ${LAST_DURATION}"
    fi
  } > "${TEXTFILE}.tmp"
  chmod 644 "${TEXTFILE}.tmp"
  mv "${TEXTFILE}.tmp" "${TEXTFILE}"
}

# The backup runs in a subshell with errexit of its own. Never call attempt (or backup_once) as
# a condition of if, while, || or &&: the shell then ignores set -e in everything it runs, this
# subshell included, and a failed pg_dump would go on to the restore check. So attempt always
# returns 0 and reports through ATTEMPT_OK.
attempt() {
  started="$(date +%s)"
  set +e
  (
    set -e
    backup_once
  )
  status=$?
  set -e
  if [ "${status}" -eq 0 ]; then
    LAST_DURATION="$(( $(date +%s) - started ))"
    write_metrics 1
    ATTEMPT_OK=1
  else
    rm -f "${BACKUP_DIR}"/*.partial "${BACKUP_DIR}"/*.fingerprint*
    dropdb --if-exists "${CHECK_DB}" 2>/dev/null || true
    log "backup failed"
    write_metrics 0
    ATTEMPT_OK=0
  fi
}

# Every loop pass, attempt or not: a stopped or removed service leaves its textfile behind, so the
# other metrics alone would not show that nothing runs any more.
write_heartbeat() {
  {
    echo "# HELP dmc_pg_backup_heartbeat_timestamp_seconds Last loop pass of the pg-backup service."
    echo "# TYPE dmc_pg_backup_heartbeat_timestamp_seconds gauge"
    echo "dmc_pg_backup_heartbeat_timestamp_seconds $(date +%s)"
  } > "${HEARTBEAT}.tmp"
  chmod 644 "${HEARTBEAT}.tmp"
  mv "${HEARTBEAT}.tmp" "${HEARTBEAT}"
}

due() {
  newest="$(newest_dump)"
  [ -z "${newest}" ] && return 0
  [ $(( $(date +%s) - $(stat -c %Y "${newest}") )) -ge "${INTERVAL}" ]
}

case "${1:-loop}" in
  once)
    attempt
    [ "${ATTEMPT_OK}" -eq 1 ]
    ;;
  loop)
    # PID 1 of the container: stop at once on docker stop instead of waiting for SIGKILL. A dump
    # cut short leaves only a .partial file, removed by the next attempt.
    trap 'log "stopping"; exit 0' TERM INT
    log "every ${INTERVAL} s, keep ${RETENTION_DAYS} days, into ${BACKUP_DIR}"
    last_failure=0
    while :; do
      write_heartbeat
      if due && [ $(( $(date +%s) - last_failure )) -ge "${RETRY}" ]; then
        attempt
        if [ "${ATTEMPT_OK}" -ne 1 ]; then
          last_failure="$(date +%s)"
        fi
      fi
      # In the background: the trap runs only between commands, not during a foreground sleep.
      sleep "${POLL}" &
      wait $!
    done
    ;;
  *)
    echo "usage: pg-backup.sh [loop|once]" >&2
    exit 2
    ;;
esac
