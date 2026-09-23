#!/usr/bin/env bash
set -euo pipefail
set +o xtrace

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=env-file.sh
source "${SCRIPT_DIR}/env-file.sh"

APP_DIR="${APP_DIR:-/opt/dmc-268-api}"
COMPOSE_FILE="${APP_DIR}/compose.yml"
STATE_FILE="${APP_DIR}/.deploy-state"
PREVIOUS_FILE="${STATE_FILE}.previous"
ENV_FILE="${APP_DIR}/.env"
REQUESTED_IMAGE="${1:-}"
BOOTSTRAP_NAME="${BOOTSTRAP_NAME:-dmc-268-api-bootstrap}"
BOOTSTRAP_IMAGE="${BOOTSTRAP_IMAGE:-nginx:1.27-alpine}"
# auto: a failed deploy is being undone; the failed image must not become the rollback target.
# manual: an operator rolls back a release; it becomes the previous release (mirrors :staging-previous).
ROLLBACK_MODE="${ROLLBACK_MODE:-manual}"

if [[ "${ROLLBACK_MODE}" != "auto" && "${ROLLBACK_MODE}" != "manual" ]]; then
  echo "ROLLBACK_MODE must be auto or manual, got: ${ROLLBACK_MODE}" >&2
  exit 1
fi

logout_registry() {
  docker logout ghcr.io >/dev/null 2>&1 || true
}
trap logout_registry EXIT

restore_bootstrap() {
  if [[ -f "${COMPOSE_FILE}" && -f "${ENV_FILE}" ]]; then
    docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" down --remove-orphans >/dev/null 2>&1 || true
  fi

  docker rm -f "${BOOTSTRAP_NAME}" >/dev/null 2>&1 || true
  docker pull "${BOOTSTRAP_IMAGE}"
  docker run -d --name "${BOOTSTRAP_NAME}" \
    --restart unless-stopped \
    --label dmc-268.role=bootstrap \
    -p "${API_HTTP_PORT}:80" "${BOOTSTRAP_IMAGE}"

  rm -f "${STATE_FILE}" "${PREVIOUS_FILE}"
  echo "restored bootstrap container (${BOOTSTRAP_IMAGE})"
}

if [[ -f "${ENV_FILE}" ]]; then
  POSTGRES_USER="${POSTGRES_USER:-$(read_compose_env_var POSTGRES_USER "${ENV_FILE}")}"
  POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-$(read_compose_env_var POSTGRES_PASSWORD "${ENV_FILE}")}"
  POSTGRES_DB="${POSTGRES_DB:-$(read_compose_env_var POSTGRES_DB "${ENV_FILE}")}"
  API_HTTP_PORT="${API_HTTP_PORT:-$(read_compose_env_var API_HTTP_PORT "${ENV_FILE}")}"
fi

# Host port for the API (and the bootstrap container). The shared course VPS keeps :80 for the UI.
API_HTTP_PORT="${API_HTTP_PORT:-80}"
if [[ ! "${API_HTTP_PORT}" =~ ^[1-9][0-9]{0,4}$ ]] || (( API_HTTP_PORT > 65535 )); then
  echo "API_HTTP_PORT must be a TCP port, got: ${API_HTTP_PORT}" >&2
  exit 1
fi

if [[ -n "${REQUESTED_IMAGE}" ]]; then
  IMAGE="${REQUESTED_IMAGE}"
elif [[ -f "${PREVIOUS_FILE}" ]]; then
  IMAGE="$(awk -F= '/^current_image=/{print $2}' "${PREVIOUS_FILE}")"
else
  echo "no previous release; restoring bootstrap" >&2
  restore_bootstrap
  exit 0
fi

if [[ -z "${IMAGE}" ]]; then
  echo "previous image reference is empty" >&2
  exit 1
fi

if [[ -z "${POSTGRES_PASSWORD:-}" ]]; then
  echo "POSTGRES_PASSWORD is required (expected in ${ENV_FILE})" >&2
  exit 1
fi

cd "${APP_DIR}"

if [[ -n "${GHCR_TOKEN:-}" ]]; then
  echo "${GHCR_TOKEN}" | docker login ghcr.io -u "${GHCR_USER:-github}" --password-stdin >/dev/null
  unset GHCR_TOKEN
fi

write_compose_env_file \
  "${ENV_FILE}" \
  "${IMAGE}" \
  "${POSTGRES_USER:-app}" \
  "${POSTGRES_PASSWORD}" \
  "${POSTGRES_DB:-app}" \
  "${API_HTTP_PORT}"

docker pull "${IMAGE}"
docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" up -d --remove-orphans --wait --wait-timeout 180

# Shared root account: drop the GHCR credential after the last pull (compose up, pull_policy: always).
logout_registry

if [[ "${ROLLBACK_MODE}" == "manual" && -f "${STATE_FILE}" ]]; then
  cp "${STATE_FILE}" "${PREVIOUS_FILE}"
fi

{
  echo "current_image=${IMAGE}"
  echo "deployed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "rolled_back=true"
} > "${STATE_FILE}"

echo "rolled back to ${IMAGE}"
