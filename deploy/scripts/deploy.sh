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
# Compose project "dmc-268-api" + volume "postgres-data" from compose.yml.
POSTGRES_VOLUME="${POSTGRES_VOLUME:-dmc-268-api_postgres-data}"

logout_registry() {
  docker logout ghcr.io >/dev/null 2>&1 || true
}
trap logout_registry EXIT

generate_password() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 24
  else
    od -An -N24 -tx1 /dev/urandom | tr -d ' \n'
  fi
}

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
  API_HTTP_PORT="${API_HTTP_PORT:-$(read_compose_env_var API_HTTP_PORT "${ENV_FILE}")}"
fi

# Host port for the API (and the bootstrap container). The shared course VPS keeps :80 for the UI.
API_HTTP_PORT="${API_HTTP_PORT:-80}"
if [[ ! "${API_HTTP_PORT}" =~ ^[1-9][0-9]{0,4}$ ]] || (( API_HTTP_PORT > 65535 )); then
  echo "API_HTTP_PORT must be a TCP port, got: ${API_HTTP_PORT}" >&2
  exit 1
fi

IMAGE="${REQUESTED_IMAGE}"

# Postgres fixes the password when the volume is initialised: generate one only on a fresh host,
# keep it in .env (0600) and reuse it on every later deploy and rollback.
if [[ -z "${POSTGRES_PASSWORD:-}" ]]; then
  if [[ -f "${ENV_FILE}" ]] || docker volume inspect "${POSTGRES_VOLUME}" >/dev/null 2>&1; then
    echo "POSTGRES_PASSWORD is not set and ${ENV_FILE} has none, but ${ENV_FILE} or volume ${POSTGRES_VOLUME} already exists; refusing to generate a new password" >&2
    exit 1
  fi
  POSTGRES_PASSWORD="$(generate_password)"
  if [[ ! "${POSTGRES_PASSWORD}" =~ ^[0-9a-f]{48}$ ]]; then
    echo "failed to generate POSTGRES_PASSWORD" >&2
    exit 1
  fi
  echo "generated POSTGRES_PASSWORD on the host (stored in ${ENV_FILE})"
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
  "${POSTGRES_DB:-app}" \
  "${API_HTTP_PORT}"

docker pull "${IMAGE}"

if docker inspect dmc-268-api-bootstrap >/dev/null 2>&1; then
  docker rm -f dmc-268-api-bootstrap >/dev/null
fi

if ! docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" up -d --remove-orphans --wait --wait-timeout 180; then
  echo "compose up failed; rolling back" >&2
  ROLLBACK_MODE=auto "${ROLLBACK_SCRIPT}"
  exit 1
fi

{
  echo "current_image=${IMAGE}"
  echo "deployed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "${STATE_FILE}"

echo "deployed ${IMAGE}"
