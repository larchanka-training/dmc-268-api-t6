#!/bin/sh
# Off-host copy of the verified PostgreSQL dumps (docs/MONITORING.md §9). Runs in the
# backup-offsite service of deploy/monitoring/compose.offsite.yml with rclone remotes from the
# environment: `store` is the S3-compatible bucket (Cloudflare R2 on staging), `secure` is a crypt
# remote over store:<bucket>/postgres. Dumps are encrypted on the host before they leave it; the
# bucket never sees a plaintext dump.
#
#   offsite.sh loop   every BACKUP_OFFSITE_POLL_SECONDS: copy new dumps, delete remote copies older
#                     than BACKUP_OFFSITE_RETENTION_DAYS; the service command
#   offsite.sh once   one pass now
#
# Status goes to TEXTFILE for node-exporter: alert PostgresBackupOffsiteStale. BACKUP_ENVIRONMENT
# (staging or prod) labels every series; each environment has its own service, dump dir and remote.
set -eu

BACKUP_DIR="${BACKUP_DIR:-/backups}"
TEXTFILE="${TEXTFILE:-/textfile/pg_backup_offsite.prom}"
POLL="${BACKUP_OFFSITE_POLL_SECONDS:-900}"
RETENTION_DAYS="${BACKUP_OFFSITE_RETENTION_DAYS:-30}"
REMOTE="${BACKUP_OFFSITE_REMOTE:-secure:}"
case "${BACKUP_ENVIRONMENT:-}" in
  staging | prod) ;;
  *)
    echo "offsite.sh: BACKUP_ENVIRONMENT must be staging or prod" >&2
    exit 2
    ;;
esac
LABELS="{environment=\"${BACKUP_ENVIRONMENT}\"}"

log() {
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) backup-offsite: $*"
}

# Dump names carry a sortable UTC stamp: the newest is the last one. Prints its mtime, which is
# also the time pg-backup reports as the last success, or nothing without a dump.
newest_dump_time() {
  newest="$(find "${BACKUP_DIR}" -maxdepth 1 -name '*.dump' | sort | tail -n 1)"
  [ -z "${newest}" ] || stat -c %Y "${newest}"
}

# Only finished dumps: *.partial files of a running pg-backup are never copied. --immutable
# refuses to overwrite a remote dump that differs, which a dump never should.
sync_once() {
  rclone copy "${BACKUP_DIR}" "${REMOTE}" --include '*.dump' --immutable --no-traverse
  rclone delete "${REMOTE}" --include '*.dump' --min-age "${RETENTION_DAYS}d"
}

write_metrics() {
  run_success="$1"
  {
    echo "# HELP dmc_pg_backup_offsite_last_run_success 1 if the last off-host copy pass succeeded."
    echo "# TYPE dmc_pg_backup_offsite_last_run_success gauge"
    echo "dmc_pg_backup_offsite_last_run_success${LABELS} ${run_success}"
    if [ -n "${LAST_SUCCESS:-}" ]; then
      echo "# HELP dmc_pg_backup_offsite_last_success_timestamp_seconds Time of the last successful pass."
      echo "# TYPE dmc_pg_backup_offsite_last_success_timestamp_seconds gauge"
      echo "dmc_pg_backup_offsite_last_success_timestamp_seconds${LABELS} ${LAST_SUCCESS}"
    fi
    if [ -n "${LAST_COPIED_DUMP:-}" ]; then
      echo "# HELP dmc_pg_backup_offsite_last_copied_dump_timestamp_seconds Time (mtime) of the newest dump that existed when the last successful pass began."
      echo "# TYPE dmc_pg_backup_offsite_last_copied_dump_timestamp_seconds gauge"
      echo "dmc_pg_backup_offsite_last_copied_dump_timestamp_seconds${LABELS} ${LAST_COPIED_DUMP}"
    fi
  } > "${TEXTFILE}.tmp"
  chmod 644 "${TEXTFILE}.tmp"
  mv "${TEXTFILE}.tmp" "${TEXTFILE}"
}

# Same rule as pg-backup.sh: never call attempt as a condition (if, while, ||, &&), or the shell
# ignores set -e in the subshell and a failed copy would go on to the delete.
# The newest dump is read before the copy starts: a dump finished while rclone runs may have been
# skipped, so a successful pass vouches only for the dump that was there when it began.
attempt() {
  covered="$(newest_dump_time)"
  set +e
  (
    set -e
    sync_once
  )
  status=$?
  set -e
  if [ "${status}" -eq 0 ]; then
    LAST_SUCCESS="$(date +%s)"
    LAST_COPIED_DUMP="${covered}"
    write_metrics 1
    ATTEMPT_OK=1
  else
    log "off-host copy failed"
    write_metrics 0
    ATTEMPT_OK=0
  fi
}

case "${1:-loop}" in
  once)
    attempt
    [ "${ATTEMPT_OK}" -eq 1 ]
    ;;
  loop)
    trap 'log "stopping"; exit 0' TERM INT
    log "every ${POLL} s to ${REMOTE}, keep ${RETENTION_DAYS} days"
    while :; do
      attempt
      sleep "${POLL}" &
      wait $!
    done
    ;;
  *)
    echo "usage: offsite.sh [loop|once]" >&2
    exit 2
    ;;
esac
