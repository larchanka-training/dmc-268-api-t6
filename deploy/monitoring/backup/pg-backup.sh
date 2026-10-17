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
# One backup: pg_dump in custom format into BACKUP_DIR, then a restore check into a scratch
# database on the same server that must reproduce the Alembic revision of the source, then the
# rename to <db>-<UTC stamp>.dump and pruning of dumps older than BACKUP_RETENTION_DAYS. The dump
# keeps owners and grants for a real restore; the check restores without them. Status goes to
# TEXTFILE as metrics for node-exporter: alerts PostgresBackupStale and PostgresBackupFailed.
set -eu

BACKUP_DIR="${BACKUP_DIR:-/backups}"
TEXTFILE="${TEXTFILE:-/textfile/pg_backup.prom}"
INTERVAL="${BACKUP_INTERVAL_SECONDS:-86400}"
RETRY="${BACKUP_RETRY_SECONDS:-3600}"
POLL="${BACKUP_POLL_SECONDS:-300}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"
CHECK_DB="${BACKUP_CHECK_DB:-dmc_restore_check}"
: "${PGDATABASE:?PGDATABASE is required}"
# Server NOTICEs (dropdb --if-exists of a missing database) are noise in the service log.
export PGOPTIONS="${PGOPTIONS:-} -c client_min_messages=warning"

umask 077

log() {
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) pg-backup: $*"
}

# Dump names carry a sortable UTC stamp: the newest is the last one.
newest_dump() {
  find "${BACKUP_DIR}" -maxdepth 1 -name "${PGDATABASE}-*.dump" | sort | tail -n 1
}

restore_check() {
  dump="$1"
  dropdb --if-exists "${CHECK_DB}"
  createdb "${CHECK_DB}"
  pg_restore --no-owner --no-privileges --exit-on-error --dbname="${CHECK_DB}" "${dump}"
  expected="$(psql -X -At -c 'SELECT version_num FROM alembic_version')"
  restored="$(psql -X -At -d "${CHECK_DB}" -c 'SELECT version_num FROM alembic_version')"
  tables="$(psql -X -At -d "${CHECK_DB}" \
    -c "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")"
  dropdb "${CHECK_DB}"
  if [ -z "${expected}" ] || [ "${restored}" != "${expected}" ]; then
    log "restore check failed: alembic revision '${restored}', source '${expected}'"
    return 1
  fi
  log "restore check passed: revision ${restored}, ${tables} tables"
}

backup_once() {
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  final="${BACKUP_DIR}/${PGDATABASE}-${stamp}.dump"
  partial="${final}.partial"
  rm -f "${BACKUP_DIR}"/*.partial
  pg_dump --format=custom --compress=6 --file="${partial}"
  restore_check "${partial}"
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
    rm -f "${BACKUP_DIR}"/*.partial
    dropdb --if-exists "${CHECK_DB}" 2>/dev/null || true
    log "backup failed"
    write_metrics 0
    ATTEMPT_OK=0
  fi
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
