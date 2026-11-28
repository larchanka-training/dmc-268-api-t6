#!/usr/bin/env bash
# Deploy the review bot (#601) on the host: write review-bot.env from the CI secrets bundle and
# start the compose project dmc-268-review-bot. Run from the uploaded review_bot folder.
#
# Inputs (environment):
#   REVIEW_BOT_DIR          this folder on the host, default: the directory of this script
#   REVIEW_BOT_SECRETS_B64  base64 of "NAME=<base64 of value>" lines, the format of
#                           deploy/scripts/env-file.sh; only the names below are accepted
set -euo pipefail
set +o xtrace

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REVIEW_BOT_DIR="${REVIEW_BOT_DIR:-${SCRIPT_DIR}}"
PROJECT=dmc-268-review-bot
ENV_FILE="${REVIEW_BOT_DIR}/review-bot.env"
KEYS=(REVIEW_BOT_APP_ID REVIEW_BOT_APP_PRIVATE_KEY REVIEW_BOT_TELEGRAM_BOT_TOKEN REVIEW_BOT_TELEGRAM_CHAT_ID REVIEW_BOT_TEAM)

# Values are written single-quoted, as env-file.sh does (write_env_file intentionally mirrors
# deploy/scripts/env-file.sh because CI uploads only review_bot/ to /opt/dmc-268-review-bot):
# Compose reads them across lines (PEM) without interpolation, so a value may contain neither '
# nor a trailing backslash. Everything is checked before the file is replaced; names only, never
# values, reach the log.
write_env_file() (
  set -o pipefail
  bundle="$1"
  umask 077
  tmp="$(mktemp "${REVIEW_BOT_DIR}/.review-bot.env.XXXXXX")"
  trap 'rm -f "${tmp}"' EXIT
  if ! decoded="$(printf '%s' "${bundle}" | base64 -d 2>/dev/null)"; then
    echo "review bot secrets bundle is not valid base64" >&2
    exit 1
  fi
  seen=()
  while IFS= read -r line; do
    [[ -n "${line}" ]] || continue
    name="${line%%=*}"
    if [[ ! "${name}" =~ ^[A-Z][A-Z0-9_]*$ ]]; then
      echo "review bot secrets bundle has a malformed line" >&2
      exit 1
    fi
    if [[ " ${KEYS[*]} " != *" ${name} "* ]]; then
      echo "review bot secret ${name} is not in the allowlist of deploy.sh" >&2
      exit 1
    fi
    if ! value="$(printf '%s' "${line#*=}" | base64 -d 2>/dev/null)"; then
      echo "review bot secret ${name}: value is not valid base64" >&2
      exit 1
    fi
    if [[ "${value}" == *"'"* || "${value}" == *\\ ]]; then
      echo "review bot secret ${name}: single quotes and a trailing backslash are not supported" >&2
      exit 1
    fi
    printf "%s='%s'\n" "${name}" "${value}" >> "${tmp}"
    seen+=("${name}")
  done <<< "${decoded}"
  for name in "${KEYS[@]}"; do
    if [[ " ${seen[*]-} " != *" ${name} "* ]]; then
      echo "review bot secret ${name} is missing from the bundle" >&2
      exit 1
    fi
  done
  chmod 600 "${tmp}"
  mv -f "${tmp}" "${ENV_FILE}"
  trap - EXIT
)

# Tests source this file for write_env_file only.
if [[ "${REVIEW_BOT_DEPLOY_SOURCE_ONLY:-}" == 1 ]]; then
  return 0
fi

if [[ -z "${REVIEW_BOT_SECRETS_B64:-}" ]]; then
  echo "REVIEW_BOT_SECRETS_B64 is empty: nothing to deploy" >&2
  exit 1
fi
write_env_file "${REVIEW_BOT_SECRETS_B64}"
echo "review-bot.env written (${#KEYS[@]} keys)"

cd "${REVIEW_BOT_DIR}"
docker compose -p "${PROJECT}" -f compose.yml up -d --build --remove-orphans --wait --wait-timeout 240
docker compose -p "${PROJECT}" -f compose.yml ps
