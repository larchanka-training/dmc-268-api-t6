#!/usr/bin/env bash
set -euo pipefail

TARGET="${1:-}"
ATTEMPTS="${HEALTHCHECK_ATTEMPTS:-12}"
SLEEP_SECONDS="${HEALTHCHECK_SLEEP:-5}"

if [[ -z "${TARGET}" ]]; then
  echo "usage: healthcheck.sh <url>" >&2
  exit 1
fi

if [[ "${TARGET}" != */healthcheck ]]; then
  TARGET="${TARGET%/}/healthcheck"
fi

for attempt in $(seq 1 "${ATTEMPTS}"); do
  if body="$(curl -fsS --max-time 5 "${TARGET}")"; then
    if printf '%s' "${body}" | grep -q '"status":"ok"'; then
      echo "health check passed on attempt ${attempt}: ${body}"
      exit 0
    fi
    echo "unexpected health payload on attempt ${attempt}: ${body}" >&2
  else
    echo "health check attempt ${attempt}/${ATTEMPTS} failed" >&2
  fi
  sleep "${SLEEP_SECONDS}"
done

echo "health check failed after ${ATTEMPTS} attempts: ${TARGET}" >&2
exit 1
