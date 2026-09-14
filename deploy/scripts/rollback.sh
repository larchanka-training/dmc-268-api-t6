#!/usr/bin/env bash
set -euo pipefail
set +o xtrace

APP_DIR="${APP_DIR:-/opt/dmc-268-api}"
COMPOSE_FILE="${APP_DIR}/compose.yml"
STATE_FILE="${APP_DIR}/.deploy-state"
PREVIOUS_FILE="${STATE_FILE}.previous"
ENV_FILE="${APP_DIR}/.env"
REQUESTED_IMAGE="${1:-}"

logout_registry() {
  docker logout ghcr.io >/dev/null 2>&1 || true
}
trap logout_registry EXIT

if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
fi

if [[ -n "${REQUESTED_IMAGE}" ]]; then
  IMAGE="${REQUESTED_IMAGE}"
elif [[ -f "${PREVIOUS_FILE}" ]]; then
  IMAGE="$(awk -F= '/^current_image=/{print $2}' "${PREVIOUS_FILE}")"
else
  echo "no previous deployment recorded and no image tag provided" >&2
  exit 1
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

umask 077
{
  printf 'IMAGE=%s\n' "${IMAGE}"
  printf 'POSTGRES_USER=%s\n' "${POSTGRES_USER:-app}"
  printf 'POSTGRES_PASSWORD=%s\n' "${POSTGRES_PASSWORD}"
  printf 'POSTGRES_DB=%s\n' "${POSTGRES_DB:-app}"
} > "${ENV_FILE}"
chmod 600 "${ENV_FILE}"

docker pull "${IMAGE}"
docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" up -d --remove-orphans --wait --wait-timeout 180

{
  echo "current_image=${IMAGE}"
  echo "deployed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "rolled_back=true"
} > "${STATE_FILE}"

echo "rolled back to ${IMAGE}"
