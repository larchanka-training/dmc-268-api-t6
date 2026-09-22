#!/usr/bin/env bash
set -euo pipefail
set +o xtrace

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=env-file.sh
source "${SCRIPT_DIR}/env-file.sh"

APP_DIR="${APP_DIR:-/opt/dmc-268-api}"
IMAGE="${1:-${IMAGE:-}}"
COMPOSE_FILE="${APP_DIR}/compose.yml"
STATE_FILE="${APP_DIR}/.deploy-state"
ENV_FILE="${APP_DIR}/.env"
ROLLBACK_SCRIPT="${APP_DIR}/rollback.sh"

logout_registry() {
  docker logout ghcr.io >/dev/null 2>&1 || true
}
trap logout_registry EXIT

if [[ -z "${IMAGE}" ]]; then
  echo "usage: deploy.sh <image-ref>" >&2
  exit 1
fi

mkdir -p "${APP_DIR}"
cd "${APP_DIR}"

REQUESTED_IMAGE="${IMAGE}"

if [[ -f "${ENV_FILE}" ]]; then
  POSTGRES_USER="${POSTGRES_USER:-$(read_compose_env_var POSTGRES_USER "${ENV_FILE}")}"
  POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-$(read_compose_env_var POSTGRES_PASSWORD "${ENV_FILE}")}"
  POSTGRES_DB="${POSTGRES_DB:-$(read_compose_env_var POSTGRES_DB "${ENV_FILE}")}"
fi

IMAGE="${REQUESTED_IMAGE}"

if [[ -z "${POSTGRES_PASSWORD:-}" ]]; then
  echo "POSTGRES_PASSWORD is required" >&2
  exit 1
fi

if [[ -f "${STATE_FILE}" ]]; then
  cp "${STATE_FILE}" "${STATE_FILE}.previous"
fi

if [[ -n "${GHCR_TOKEN:-}" ]]; then
  echo "${GHCR_TOKEN}" | docker login ghcr.io -u "${GHCR_USER:-github}" --password-stdin >/dev/null
  unset GHCR_TOKEN
fi

write_compose_env_file \
  "${ENV_FILE}" \
  "${IMAGE}" \
  "${POSTGRES_USER:-app}" \
  "${POSTGRES_PASSWORD}" \
  "${POSTGRES_DB:-app}"

docker pull "${IMAGE}"

if docker inspect dmc-268-api-bootstrap >/dev/null 2>&1; then
  docker rm -f dmc-268-api-bootstrap >/dev/null
fi

if ! docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" up -d --remove-orphans --wait --wait-timeout 180; then
  echo "compose up failed; rolling back" >&2
  "${ROLLBACK_SCRIPT}"
  exit 1
fi

{
  echo "current_image=${IMAGE}"
  echo "deployed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "${STATE_FILE}"

echo "deployed ${IMAGE}"
