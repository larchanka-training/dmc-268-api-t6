#!/usr/bin/env bash
set -euo pipefail
set +o xtrace

APP_DIR="${APP_DIR:-/opt/dmc-268-api}"
IMAGE="${1:-${IMAGE:-}}"
COMPOSE_FILE="${APP_DIR}/compose.yml"
STATE_FILE="${APP_DIR}/.deploy-state"
ENV_FILE="${APP_DIR}/.env"

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
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
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

umask 077
{
  printf 'IMAGE=%s\n' "${IMAGE}"
  printf 'POSTGRES_USER=%s\n' "${POSTGRES_USER:-app}"
  printf 'POSTGRES_PASSWORD=%s\n' "${POSTGRES_PASSWORD}"
  printf 'POSTGRES_DB=%s\n' "${POSTGRES_DB:-app}"
} > "${ENV_FILE}"
chmod 600 "${ENV_FILE}"

if docker inspect dmc-268-api-bootstrap >/dev/null 2>&1; then
  docker rm -f dmc-268-api-bootstrap >/dev/null
fi

docker pull "${IMAGE}"
docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" up -d --remove-orphans --wait --wait-timeout 180

{
  echo "current_image=${IMAGE}"
  echo "deployed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "${STATE_FILE}"

echo "deployed ${IMAGE}"
