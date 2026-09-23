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
# One compose project per app dir: /opt/dmc-268-api (Terraform host), /opt/dmc-268-api-staging (course VPS).
COMPOSE_PROJECT="${COMPOSE_PROJECT:-$(basename "${APP_DIR}")}"
BOOTSTRAP_NAME="${BOOTSTRAP_NAME:-${COMPOSE_PROJECT}-bootstrap}"
# Named volume "postgres-data" of the compose project.
POSTGRES_VOLUME="${POSTGRES_VOLUME:-${COMPOSE_PROJECT}_postgres-data}"

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
  DEPLOY_MODE="${DEPLOY_MODE:-$(read_compose_env_var DEPLOY_MODE "${ENV_FILE}")}"
  EDGE_ALIAS="${EDGE_ALIAS:-$(read_compose_env_var EDGE_ALIAS "${ENV_FILE}")}"
fi

# ports: publish the API on host port 80 (dedicated Terraform host).
# edge: no host port; join the edge proxy network as EDGE_ALIAS (shared course VPS).
DEPLOY_MODE="${DEPLOY_MODE:-ports}"
if [[ "${DEPLOY_MODE}" != "ports" && "${DEPLOY_MODE}" != "edge" ]]; then
  echo "DEPLOY_MODE must be ports or edge, got: ${DEPLOY_MODE}" >&2
  exit 1
fi
if [[ "${DEPLOY_MODE}" == "edge" && ! "${EDGE_ALIAS:-}" =~ ^[a-z0-9]+(-[a-z0-9]+)+$ ]]; then
  echo "EDGE_ALIAS (<service>-<env>) is required in edge mode, got: ${EDGE_ALIAS:-}" >&2
  exit 1
fi
COMPOSE=(docker compose -p "${COMPOSE_PROJECT}" -f "${COMPOSE_FILE}" -f "${APP_DIR}/compose.${DEPLOY_MODE}.yml" --env-file "${ENV_FILE}")

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
  "${DEPLOY_MODE}" \
  "${EDGE_ALIAS:-}"

docker pull "${IMAGE}"

if docker inspect "${BOOTSTRAP_NAME}" >/dev/null 2>&1; then
  docker rm -f "${BOOTSTRAP_NAME}" >/dev/null
fi

if ! "${COMPOSE[@]}" up -d --remove-orphans --wait --wait-timeout 180; then
  echo "compose up failed; rolling back" >&2
  ROLLBACK_MODE=auto "${ROLLBACK_SCRIPT}"
  exit 1
fi

# The host is a shared root account: drop the GHCR credential right after the last pull
# (compose up pulls again because of pull_policy: always). The EXIT trap stays as a fallback.
logout_registry

{
  echo "current_image=${IMAGE}"
  echo "deployed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "${STATE_FILE}"

echo "deployed ${IMAGE}"
