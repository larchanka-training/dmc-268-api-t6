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
# Status goes to TEXTFILE for node-exporter: alert PostgresBackupOffsiteStale.
set -eu

BACKUP_DIR="${BACKUP_DIR:-/backups}"
TEXTFILE="${TEXTFILE:-/textfile/pg_backup_offsite.prom}"
POLL="${BACKUP_OFFSITE_POLL_SECONDS:-900}"
RETENTION_DAYS="${BACKUP_OFFSITE_RETENTION_DAYS:-30}"
REMOTE="${BACKUP_OFFSITE_REMOTE:-secure:}"

log() {
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) backup-offsite: $*"
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
    echo "dmc_pg_backup_offsite_last_run_success ${run_success}"
    if [ -n "${LAST_SUCCESS:-}" ]; then
      echo "# HELP dmc_pg_backup_offsite_last_success_timestamp_seconds Time of the last successful pass."
      echo "# TYPE dmc_pg_backup_offsite_last_success_timestamp_seconds gauge"
      echo "dmc_pg_backup_offsite_last_success_timestamp_seconds ${LAST_SUCCESS}"
    fi
  } > "${TEXTFILE}.tmp"
  chmod 644 "${TEXTFILE}.tmp"
  mv "${TEXTFILE}.tmp" "${TEXTFILE}"
}

# Same rule as pg-backup.sh: never call attempt as a condition (if, while, ||, &&), or the shell
# ignores set -e in the subshell and a failed copy would go on to the delete.
attempt() {
  set +e
  (
    set -e
    sync_once
  )
  status=$?
  set -e
  if [ "${status}" -eq 0 ]; then
    LAST_SUCCESS="$(date +%s)"
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
